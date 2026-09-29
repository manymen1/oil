"""Offline, append-only operational assessments bound to a verified snapshot.

This is development review tooling, not independent economic validation. Human
judgments and assistant judgments retain separate provenance. No capture writes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import uuid

from .clock import instant, utc_now
from .economic import VERSION, build_transition
from .forward_review import episode_map
from .schema import canonical, digest
from .story_review import Archive

SCHEMA = "economic-review-v1"
REQUIRED = {"event_id", "story_revision_id", "episode_id", "reviewer", "reviewer_type",
            "reason", "episode_reviewed", "novel", "stage", "state_before", "state_after",
            "assertion", "confirmation_level", "mechanism", "evidence"}
OPTIONAL = {"claim_origin", "asset", "asset_type", "location", "severity", "estimated_duration",
            "unresolved_entities", "authorization", "assessment_id", "supersedes_assessment_id"}


def events(archive):
    # Prefer fast events already available when a revision first entered the
    # queue. A classifier that runs later must not erase an earlier subject.
    # A queued body-only candidate is real, not a fabricated fast_event.
    result = {k: r for k, r in archive.records.items() if r["kind"] == "fast_event"}
    queued = set()
    for k, row in sorted(archive.records.items(), key=lambda item: (instant(item[1]["available_at"]), item[0])):
        if row["kind"] != "operational_review_candidate":
            continue
        sid = row["payload"]["story_revision_id"]
        if sid in queued:
            continue
        queued.add(sid)
        matches = [r for r in result.values() if r["payload"]["story_revision_id"] == sid]
        if any(instant(r["available_at"]) <= instant(row["available_at"]) for r in matches):
            continue
        for match in matches:
            del result[match["id"]]
        result[k] = row
    return result


def mapping_at(archive, at):
    if "forward.sqlite3" not in archive.manifest["files"]:
        raise ValueError("verified forward journal required")
    mapping = episode_map(archive.path.parent / "forward.sqlite3", through=at)
    for eid, row in events(archive).items():
        if row["kind"] != "operational_review_candidate" or instant(row["available_at"]) > instant(at):
            continue
        story = archive.stories[row["payload"]["story_revision_id"]]
        p = story["payload"]
        later = [r for r in archive.stories.values() if r["payload"]["story_id"] == p["story_id"]
                 and instant(story["available_at"]) < instant(r["available_at"]) <= instant(at)]
        state = {"correction": "CORRECTED", "withdrawal": "WITHDRAWN", "deleted": "DELETED"}.get(p.get("status"), "ACTIVE")
        if later:
            state = "SUPERSEDED"
        mapping["episodes"].append({"id": "operational-episode:" + digest([eid]), "event_ids": [eid],
                                    "review_required": False})
        mapping["evidence_states"][eid] = state
    return mapping


def queue(archive):
    """Metadata only: all captured candidates, including excluded baselines."""
    rows = []
    for eid, event in sorted(events(archive).items()):
        story = archive.stories[event["payload"]["story_revision_id"]]
        rows.append({"event_id": eid, "story_revision_id": story["id"],
                     "source": story["payload"]["source_id"],
                     "candidate_kind": event["kind"], "event_type": event["payload"].get("event_type"),
                     "received_at": story["payload"]["local_received_at"],
                     "initial_snapshot": story["payload"].get("initial_snapshot", True)})
    return {"schema": SCHEMA, "role": "development", "manifest_records_hash": archive.manifest["records_hash"],
            "captured_events": len(rows), "events": rows,
            "subject_policy": "Fast events known at first queue availability, otherwise the first queued revision; no synthetic classifier records.",
            "limitations": ["Candidate census, not a holdout or a sample of missed events.",
                            "Use story-review coverage for unmatched stories and source gaps."]}


def template(archive, event_id):
    event = events(archive).get(event_id)
    if event is None:
        raise ValueError("unknown captured event")
    at = utc_now()
    if instant(event["available_at"]) > instant(at):
        raise ValueError("cannot review a future-available event")
    mapping = mapping_at(archive, at)
    group = next(g for g in mapping["episodes"] if event_id in g["event_ids"])
    story = archive.stories[event["payload"]["story_revision_id"]]
    return {"event": event, "story": story, "episode": group,
            "assessment": {"event_id": event_id, "story_revision_id": story["id"],
                "episode_id": group["id"], "reviewer": "", "reviewer_type": "human", "reason": "",
                "episode_reviewed": False, "novel": False, "stage": None,
                "state_before": "UNKNOWN", "state_after": "UNKNOWN", "assertion": "unclear",
                "confirmation_level": "UNVERIFIED", "mechanism": "unknown", "claim_origin": None,
                "asset": None, "asset_type": None, "location": None, "severity": None,
                "estimated_duration": None, "unresolved_entities": [], "evidence": []}}


def outside_snapshot(archive, path):
    target = Path(path).resolve()
    if target == archive.path.parent or archive.path.parent in target.parents:
        raise ValueError("output must be outside the checksum-bound snapshot directory")
    if target.exists() and any(target.samefile(archive.path.parent / name) for name in archive.manifest["files"]):
        raise ValueError("output cannot alias a captured journal")
    return target


def sidecar(archive, path):
    target = outside_snapshot(archive, path)
    if target.exists():
        with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables != {"economic_assessments"}:
                raise ValueError("destination is not an economic assessment sidecar")
    return target


def history(archive, path, *, through=None):
    path = sidecar(archive, path)
    if not path.exists():
        return []
    at = instant(through or utc_now())
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = [json.loads(r[0]) for r in db.execute("SELECT record FROM economic_assessments ORDER BY seq")]
    if any(r["manifest_records_hash"] != archive.manifest["records_hash"] for r in rows):
        raise ValueError("assessment snapshot mismatch; use a separate sidecar per snapshot")
    return [r for r in rows if instant(r["available_at"]) <= at]


def assess(archive, path, value):
    if not isinstance(value, dict) or not REQUIRED <= value.keys() or value.keys() - REQUIRED - OPTIONAL:
        raise ValueError("complete assessment fields required; timestamps are assigned at save time")
    if value["reviewer_type"] == "assistant" and (not isinstance(value.get("authorization"), str) or not value["authorization"].strip()):
        raise ValueError("assistant review requires a recorded authorization reference")
    for key in ("assessment_id", "supersedes_assessment_id"):
        if value.get(key) is not None and (not isinstance(value[key], str) or not value[key].strip()):
            raise ValueError(key + " must be nonempty text or null")
    event = events(archive).get(value["event_id"])
    if event is None or value["story_revision_id"] not in archive.stories:
        raise ValueError("assessment requires captured event and story")
    story = archive.stories[value["story_revision_id"]]
    at = utc_now()
    aid = value.get("assessment_id") or str(uuid.uuid4())
    submitted = {**value, "assessment_id": aid,
                 "supersedes_assessment_id": value.get("supersedes_assessment_id")}
    row = {"id": aid, "kind": "economic_assessment", "available_at": at, "payload": submitted,
           "schema": SCHEMA, "manifest_records_hash": archive.manifest["records_hash"],
           "event_hash": digest(event), "story_hash": digest(story), "bridge_version": VERSION}
    # Validate before creating any sidecar. Semantic support remains human judgment.
    row["transition_at_review"] = build_transition(event, story, row, mapping_at(archive, at), evaluated_at=at,
                                                  evidence_stories=archive.stories)
    path = sidecar(archive, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS economic_assessments (
              seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
              event_id TEXT NOT NULL, record TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS economic_event ON economic_assessments(event_id,seq);
            CREATE TRIGGER IF NOT EXISTS immutable_economic_update BEFORE UPDATE ON economic_assessments
              BEGIN SELECT RAISE(ABORT,'assessments are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_economic_delete BEFORE DELETE ON economic_assessments
              BEGIN SELECT RAISE(ABORT,'assessments are immutable'); END;
        """)
        with db:
            db.execute("BEGIN IMMEDIATE")
            first = db.execute("SELECT record FROM economic_assessments ORDER BY seq LIMIT 1").fetchone()
            if first and json.loads(first[0])["manifest_records_hash"] != row["manifest_records_hash"]:
                raise ValueError("assessment snapshot mismatch; use a separate sidecar per snapshot")
            prior = db.execute("SELECT record FROM economic_assessments WHERE id=?", (aid,)).fetchone()
            if prior:
                old = json.loads(prior[0])
                if old["payload"] != submitted or old["event_hash"] != row["event_hash"] or old["story_hash"] != row["story_hash"]:
                    raise ValueError("assessment ID collision")
                return old
            latest = db.execute("SELECT id,record FROM economic_assessments WHERE event_id=? ORDER BY seq DESC LIMIT 1",
                                (event["id"],)).fetchone()
            if (latest[0] if latest else None) != submitted["supersedes_assessment_id"]:
                raise ValueError("supersedes_assessment_id must name the latest assessment for this event")
            last = db.execute("SELECT record FROM economic_assessments ORDER BY seq DESC LIMIT 1").fetchone()
            if last and instant(at) < instant(json.loads(last[0])["available_at"]):
                raise ValueError("assessment clock regressed")
            db.execute("INSERT INTO economic_assessments(id,event_id,record) VALUES(?,?,?)",
                       (aid, event["id"], canonical(row)))
    return row


def report(archive, path, *, through=None):
    at = through or utc_now()
    mapping = mapping_at(archive, at)
    rows = history(archive, path, through=at)
    latest = {r["payload"]["event_id"]: r for r in rows}
    captured = events(archive)
    transitions, invalid = [], []
    for eid, assessment in sorted(latest.items()):
        try:
            event = captured[eid]
            story = archive.stories[assessment["payload"]["story_revision_id"]]
            if digest(event) != assessment["event_hash"] or digest(story) != assessment["story_hash"]:
                raise ValueError("assessment input hash mismatch")
            transitions.append(build_transition(event, story, assessment, mapping, evaluated_at=at,
                                                 evidence_stories=archive.stories))
        except (ValueError, KeyError) as exc:
            invalid.append({"event_id": eid, "assessment_id": assessment["id"], "error": str(exc)})
    available = {eid for eid, r in captured.items() if instant(r["available_at"]) <= instant(at)}
    eligible = [r for r in transitions if r["payload"]["research_eligible"]]
    return {"schema": SCHEMA, "bridge_version": VERSION, "evaluated_at": at,
            "manifest_records_hash": archive.manifest["records_hash"], "mapping_hash": digest(mapping),
            "assessment_history_hash": digest(rows), "captured_events": len(available),
            "reviewed_events": len(latest), "unreviewed_event_ids": sorted(available - latest.keys()),
            "assessment_revisions": len(rows), "reviewer_types": dict(Counter(r["payload"]["reviewer_type"] for r in latest.values())),
            "queue_states": {eid: ("FAILED" if any(r["event_id"] == eid for r in invalid) else
                "ASSESSED" if any(r["payload"]["event_id"] == eid and r["payload"]["research_eligible"] for r in transitions) else
                "ABSTAINED" if eid in latest else "PENDING") for eid in sorted(available)},
            "eligible_transitions": len(eligible),
            "eligible_episode_groups": len({r["payload"]["episode_id"] for r in eligible}),
            "exclusion_reasons": dict(Counter(reason for r in transitions for reason in r["payload"]["abstention_reasons"])),
            "invalid_assessments": invalid, "transitions": transitions,
            "validation_status": "HUMAN_SEMANTIC_VALIDATION_REQUIRED", "trade_authorized": False,
            "limitations": ["Schema checks and literal spans do not validate economic interpretation.",
                            "Episode groups may still share crisis-level dependence.",
                            "Development diagnostics are not holdout accuracy or evidence of trading edge.",
                            "Snapshot lifecycle ends at capture; later corrections are not known.",
                            "Re-evaluation time is not historical receipt or original review time."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("queue")
    show = sub.add_parser("template")
    show.add_argument("--event", required=True)
    save = sub.add_parser("assess")
    save.add_argument("--file", required=True, type=Path)
    for name in ("history", "report"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--through")
    dataset = sub.add_parser("dataset", help="Export original dated assessments with causal features and separate labels")
    dataset.add_argument("--assessments", required=True, type=Path)
    dataset.add_argument("--market-root", type=Path)
    dataset.add_argument("--admissions", type=Path, help="Optional separate machine admission journal, not economic labels")
    dataset.add_argument("--through")
    for name in ("assess", "history", "report"):
        sub.choices[name].add_argument("--assessments", required=True, type=Path)
    for cmd in sub.choices.values():
        cmd.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    archive = Archive(args.manifest)
    if args.out:
        target = outside_snapshot(archive, args.out)
        if target.exists():
            raise ValueError("output already exists")
        if getattr(args, "assessments", None) and target == args.assessments.resolve():
            raise ValueError("output cannot be the assessment sidecar")
    if args.command == "dataset":
        if not args.out:
            raise ValueError("dataset requires a new --out directory")
        from .economic_dataset import build_economic_dataset
        result = build_economic_dataset(archive, args.assessments, target,
            market_root=args.market_root, through=args.through, admissions=args.admissions)
        print(json.dumps(result, indent=2))
        return
    if args.command == "queue":
        result = queue(archive)
    elif args.command == "template":
        result = template(archive, args.event)
    elif args.command == "assess":
        result = assess(archive, args.assessments, json.loads(args.file.read_text()))
    elif args.command == "history":
        result = history(archive, args.assessments, through=args.through)
    else:
        result = report(archive, args.assessments, through=args.through)
    if args.out:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x") as stream:
            stream.write(canonical(result) + "\n")
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
