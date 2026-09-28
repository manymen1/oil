"""Offline revision-bound annotations. Never opens capture journals for writing."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import uuid

from .clock import instant, utc_now
from .forward import VERSION, classify
from .replay import load_manifest
from .schema import canonical, digest

SCHEMA = "story-review-v1"
FAMILIES = {"attack", "disruption", "restoration", "sanctions", "ceasefire", "other", "none"}
ASSERTIONS = {"asserted", "denied", "hypothetical", "historical", "unclear"}
RELEVANCE = {"relevant", "irrelevant", "uncertain"}
MECHANISMS = {"supply", "transport", "demand", "risk_sentiment", "unknown"}
DECISIONS = {"label", "accept", "reject", "correct", "add_missed", "uncertain"}
LABELS = {"oil_relevance", "event_family", "assertion", "claimant", "oil_mechanism"}


def family(event_type):
    if event_type in {"TANKER_ATTACK", "TANKER_SEIZURE", "MILITARY_STRIKE", "MISSILE_ATTACK", "DRONE_ATTACK"}:
        return "attack"
    if event_type in {"SHIPPING_RESTRICTION", "PRODUCTION_SUSPENDED", "EXPORT_TERMINAL_CLOSED", "PIPELINE_OUTAGE", "OPEC_OUTPUT_CUT"}:
        return "disruption"
    if event_type in {"SHIPPING_RESTORED", "PRODUCTION_RESTORED", "EXPORT_TERMINAL_REOPENED", "PIPELINE_RESTORED", "OPEC_OUTPUT_INCREASE"}:
        return "restoration"
    if event_type.startswith("SANCTIONS_"):
        return "sanctions"
    if event_type.startswith("CEASEFIRE_"):
        return "ceasefire"
    return "other"


class Archive:
    def __init__(self, manifest):
        self.path = Path(manifest).resolve()
        self.manifest, reader, _ = load_manifest(self.path)
        self.records = {r["id"]: r for r in reader.records}
        self.stories = {k: r for k, r in self.records.items() if r["kind"] == "story_revision"}
        self.events, self.processing = defaultdict(list), defaultdict(list)
        for row in reader.records:
            if row["kind"] == "fast_event":
                self.events[row["payload"]["story_revision_id"]].append(row)
            elif row["kind"] == "forward_processing":
                for rid in row["payload"].get("input_revision_ids", []):
                    if rid in self.stories:
                        self.processing[rid].append(row)

    def disposition(self, rid):
        processing = self.processing[rid]
        if self.events[rid]:
            return "MATCHED"
        if processing and processing[-1]["payload"].get("exclusion"):
            return "EXCLUDED"
        return "UNMATCHED" if processing else "UNPROCESSED"

    def show(self, rid):
        if rid not in self.stories:
            raise ValueError("unknown story revision")
        row = self.stories[rid]
        return {"story_revision_id": rid, "story_hash": digest(row), "story": row["payload"],
                "available_at": row["available_at"], "historical_disposition": self.disposition(rid),
                "historical_events": self.events[rid], "historical_processing": self.processing[rid]}

    def sample(self, path):
        data = json.loads(Path(path).read_text())
        if data.get("manifest_records_hash", self.manifest["records_hash"]) != self.manifest["records_hash"]:
            raise ValueError("sample snapshot mismatch")
        rows = data["sample"]
        ids = [r["story_revision_id"] for r in rows]
        if len(ids) != len(set(ids)) or not set(ids) <= self.stories.keys():
            raise ValueError("sample requires distinct captured revision IDs")
        cohort = self.cohort(data.get("cohort"))
        if not set(ids) <= cohort:
            raise ValueError("sample contains revisions outside its frozen cohort")
        population = Counter((self.stories[rid]["payload"]["source_id"], self.disposition(rid)) for rid in cohort)
        selected = Counter((self.stories[rid]["payload"]["source_id"], self.disposition(rid)) for rid in ids)
        strata = {(s["source"], s["historical_disposition"]): s for s in data["strata"]}
        if len(strata) != len(data["strata"]) or set(strata) != set(population):
            raise ValueError("sample must declare every nonempty population stratum")
        for key, count in population.items():
            s = strata[key]
            if s["population_revisions"] != count or s["sample_revisions"] != selected[key]:
                raise ValueError("sample denominators do not match snapshot")
        for row in rows:
            rid = row["story_revision_id"]
            if row["source"] != self.stories[rid]["payload"]["source_id"] or row["historical_disposition"] != self.disposition(rid):
                raise ValueError("sample source/disposition mismatch")
        return data

    def cohort(self, definition=None):
        if definition is None:
            return set(self.stories)
        if not isinstance(definition, dict) or set(definition) != {"received_after", "excluded_story_ids", "nonbaseline_only"}:
            raise ValueError("invalid sample cohort")
        if type(definition["nonbaseline_only"]) is not bool or not isinstance(definition["excluded_story_ids"], list) or any(not isinstance(x, str) for x in definition["excluded_story_ids"]):
            raise ValueError("invalid cohort exclusions")
        after = instant(definition["received_after"])
        excluded = set(definition["excluded_story_ids"])
        result = set()
        for rid, row in self.stories.items():
            p = row["payload"]
            receipt = p.get("local_received_at")
            if p["story_id"] in excluded or not receipt or instant(receipt) <= after:
                continue
            if definition["nonbaseline_only"] and self.disposition(rid) in {"EXCLUDED", "UNPROCESSED"}:
                continue
            result.add(rid)
        return result


def freeze_sample(archive, *, per_stratum, received_after, exclude_sample):
    if type(per_stratum) is not int or per_stratum < 1:
        raise ValueError("positive per-stratum sample size required")
    prior = json.loads(Path(exclude_sample).read_text())
    old_ids = {r["story_revision_id"] for r in prior["sample"]}
    if not old_ids <= archive.stories.keys():
        raise ValueError("new cumulative snapshot must contain all tuning sample revisions")
    excluded_story_ids = {archive.stories[rid]["payload"]["story_id"] for rid in old_ids}
    # Exclude known linked episode/report peers as well, including provisional
    # candidates. Conservative over-exclusion is safer than known overlap.
    excluded_events = {r["id"] for rid in old_ids for r in archive.events[rid]}
    edges = [r["payload"] for r in archive.records.values() if r["kind"] in {"candidate_episode_link", "forward_link_review"}]
    changed = True
    while changed:
        changed = False
        for p in edges:
            pair = {p.get("left_event_id", p.get("from_event_id")), p.get("right_event_id", p.get("event_id"))} - {None}
            if pair & excluded_events and not pair <= excluded_events:
                excluded_events.update(pair)
                changed = True
    for rid, events in archive.events.items():
        if any(e["id"] in excluded_events for e in events):
            excluded_story_ids.add(archive.stories[rid]["payload"]["story_id"])
    definition = {"received_after": instant(received_after).isoformat(),
                  "excluded_story_ids": sorted(excluded_story_ids), "nonbaseline_only": True}
    groups = defaultdict(list)
    for rid in archive.cohort(definition):
        groups[(archive.stories[rid]["payload"]["source_id"], archive.disposition(rid))].append(rid)
    selected, strata = [], []
    for (source, disposition), ids in sorted(groups.items()):
        picked = sorted(ids, key=lambda rid: digest(["story-holdout-v1",rid]))[:per_stratum]
        strata.append({"source":source,"historical_disposition":disposition,
                       "population_revisions":len(ids),"sample_revisions":len(picked)})
        selected.extend({"story_revision_id":rid,"source":source,"historical_disposition":disposition} for rid in picked)
    return {"schema":"story-sample-v1", "role":"holdout", "created_at":utc_now(),
            "cohort":definition,"manifest_records_hash":archive.manifest["records_hash"],
            "tuning_sample_hash":digest(prior),"sample":selected,"strata":strata,
            "limitations":["Unlinked same-episode/conflict dependence remains possible; manual disjointness review required.",
                           "No text or diagnostic inspected during selection. Freeze before labeling."]}


def check_sidecar(archive, path):
    target = Path(path).resolve()
    if target == archive.path or target.parent == archive.path.parent or target == archive.path.parent:
        raise ValueError("annotations must be outside the checksum-bound snapshot directory")
    if target.exists() and any(target.samefile(archive.path.parent / name) for name in archive.manifest["files"]):
        raise ValueError("annotations cannot alias a captured journal")
    if target.exists():
        with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "records" in tables or (tables and "annotations" not in tables):
                raise ValueError("annotation destination is not a review sidecar")
    return target


def annotation_history(path, *, through=None):
    path = Path(path)
    if not path.exists():
        return []
    at = instant(through or utc_now()).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return [json.loads(r[0]) for r in db.execute("SELECT payload FROM annotations WHERE annotated_at<=? ORDER BY seq", (at,))]


def validate_annotation(archive, value):
    required = LABELS | {"story_revision_id", "decision", "reviewer", "reviewer_type", "evidence", "reason"}
    optional = {"supersedes_annotation_id", "annotation_id", "authorization"}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise ValueError("complete annotation fields required; timestamps are assigned at save time")
    rid = value["story_revision_id"]
    if rid not in archive.stories:
        raise ValueError("annotation requires an exact captured story revision")
    for field, choices in (("decision", DECISIONS), ("event_family", FAMILIES), ("assertion", ASSERTIONS),
                           ("oil_relevance", RELEVANCE), ("oil_mechanism", MECHANISMS),
                           ("reviewer_type", {"human", "assistant"})):
        if value[field] not in choices:
            raise ValueError("invalid " + field)
    for field in ("reviewer", "reason"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError(field + " required")
    if value["claimant"] is not None and (not isinstance(value["claimant"], str) or not value["claimant"].strip()):
        raise ValueError("claimant must be a named originator or null")
    if value["reviewer_type"] == "assistant" and (not isinstance(value.get("authorization"), str) or not value["authorization"].strip()):
        raise ValueError("assistant annotations require a recorded offline-review authorization; no rights change implied")
    if value["decision"] in {"accept", "reject", "correct"} and not archive.events[rid]:
        raise ValueError("accept/reject/correct requires historical events; use add_missed or uncertain for unmatched stories")
    if value["decision"] == "add_missed" and (archive.events[rid] or value["event_family"] == "none"):
        raise ValueError("add_missed requires a previously unmatched event family")
    evidence = value["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("exact supporting evidence required")
    covered = set()
    for span in evidence:
        if not isinstance(span, dict) or set(span) != {"text_field", "start", "end", "quote", "supports"}:
            raise ValueError("evidence requires field, offsets, quote and supported labels")
        field, a, b = span["text_field"], span["start"], span["end"]
        text = archive.stories[rid]["payload"].get(field, "") if field in {"title", "text"} else ""
        if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(text) or text[a:b] != span["quote"]:
            raise ValueError("evidence does not match exact captured text")
        if not isinstance(span["supports"], list) or not span["supports"] or not set(span["supports"]) <= LABELS:
            raise ValueError("evidence supports must name annotation fields")
        covered.update(span["supports"])
        if "claimant" in span["supports"] and value["claimant"] is not None and value["claimant"].casefold() not in span["quote"].casefold():
            raise ValueError("named claimant must occur in its cited evidence; subject alone is not attribution")
    needed = {"oil_relevance", "event_family", "assertion"}
    if value["claimant"] is not None:
        needed.add("claimant")
    if value["oil_mechanism"] != "unknown":
        needed.add("oil_mechanism")
    if not needed <= covered:
        raise ValueError("every non-unknown label needs supporting evidence")
    return value


def annotate(archive, path, value):
    value = validate_annotation(archive, value)
    path = check_sidecar(archive, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS annotations (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
              story_revision_id TEXT NOT NULL, annotated_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS annotation_story ON annotations(story_revision_id,seq);
            CREATE TRIGGER IF NOT EXISTS immutable_annotation_update BEFORE UPDATE ON annotations
              BEGIN SELECT RAISE(ABORT,'annotations are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_annotation_delete BEFORE DELETE ON annotations
              BEGIN SELECT RAISE(ABORT,'annotations are immutable'); END;
        """)
        with db:
            db.execute("BEGIN IMMEDIATE")
            aid = value.get("annotation_id") or str(uuid.uuid4())
            rid = value["story_revision_id"]
            supplied = {**value, "annotation_id": aid, "supersedes_annotation_id": value.get("supersedes_annotation_id")}
            existing = db.execute("SELECT payload FROM annotations WHERE id=?", (aid,)).fetchone()
            if existing:
                prior = json.loads(existing[0])
                if prior["submitted"] != supplied or prior["story_hash"] != digest(archive.stories[rid]):
                    raise ValueError("annotation ID collision")
                return prior
            latest = db.execute("SELECT id,payload,annotated_at FROM annotations WHERE story_revision_id=? ORDER BY seq DESC LIMIT 1", (rid,)).fetchone()
            if (latest[0] if latest else None) != supplied["supersedes_annotation_id"]:
                raise ValueError("supersedes_annotation_id must name the latest annotation for this revision")
            at = utc_now()
            if latest and (json.loads(latest[1])["story_hash"] != digest(archive.stories[rid]) or instant(at) < instant(latest[2])):
                raise ValueError("story binding changed or annotation clock regressed")
            if instant(archive.stories[rid]["available_at"]) > instant(at):
                raise ValueError("cannot annotate a future-available story")
            result = {"schema": SCHEMA, **supplied, "submitted": supplied, "annotated_at": at,
                      "story_hash": digest(archive.stories[rid]), "manifest_records_hash": archive.manifest["records_hash"],
                      "historical_event_ids": [r["id"] for r in archive.events[rid]],
                      "confirmation_granted": False, "historical_output_modified": False}
            db.execute("INSERT INTO annotations(id,story_revision_id,annotated_at,payload) VALUES(?,?,?,?)",
                       (aid, rid, at, canonical(result)))
            return result


def diagnostic(archive, sample, *, text_field="title"):
    from .interpretation import policy
    from .forward import RULES, ATTRIBUTION, QUALIFIERS
    if text_field not in {"title", "text"}:
        raise ValueError("diagnostic text field must be captured title or text")
    at = utc_now()
    if any(instant(archive.stories[s["story_revision_id"]]["available_at"]) > instant(at) for s in sample["sample"]):
        raise ValueError("diagnostic cannot use future-available stories")
    p = {"classifier_version": VERSION, "interpretation": policy(), "rules": RULES,
         "attribution": ATTRIBUTION.pattern, "qualifiers": QUALIFIERS.pattern,
         "assets": archive.manifest["config"]["assets"], "text_field": text_field}
    def evaluate(story):
        result = classify({**story, "title": story.get(text_field, "")}, archive.manifest["config"]["assets"])
        for event in result:
            event["evidence"]["field"] = text_field
            for key in ("literal_evidence", "interpretation_evidence", "attribution_evidence"):
                for span in event.get(key, []):
                    span["text_field"] = text_field
        return result
    return {"schema": "story-diagnostic-v1", "evaluated_at": at, "policy": p, "policy_hash": digest(p),
            "manifest_records_hash": archive.manifest["records_hash"], "sample_hash": digest(sample),
            "historical_reclassification": False, "results": [
                {"story_revision_id": s["story_revision_id"], "story_hash": digest(archive.stories[s["story_revision_id"]]),
                 "original_received_at": archive.stories[s["story_revision_id"]]["payload"].get("local_received_at"),
                 "original_available_at": archive.stories[s["story_revision_id"]]["available_at"],
                 "events": evaluate(archive.stories[s["story_revision_id"]]["payload"])}
                for s in sample["sample"]],
            "limitations": ["Offline probe of the declared captured text field; no first-party resolver or original availability rewrite.",
                            "Text-field diagnostics do not activate feed-text classification in the live recorder.",
                            "Evaluation time is not receipt time. Labels and later knowledge cannot become historical input."]}


def coverage_report(archive, sample, annotations, *, diagnostic_result=None, through=None, target="candidate_discovery"):
    if target not in {"candidate_discovery", "asserted_oil_event"}:
        raise ValueError("unsupported evaluation target")
    at = instant(through or utc_now()).isoformat(timespec="microseconds")
    if any(instant(archive.stories[s["story_revision_id"]]["available_at"]) > instant(at) for s in sample["sample"]):
        raise ValueError("report sample contains future-available stories")
    active = {}
    for row in annotations:
        rid = row["story_revision_id"]
        if instant(row["annotated_at"]) <= instant(at):
            active[rid] = row
    predictions = {rid: [r["payload"] for r in archive.events[rid]] for rid in archive.stories}
    if diagnostic_result is not None:
        if diagnostic_result["manifest_records_hash"] != archive.manifest["records_hash"] or diagnostic_result["sample_hash"] != digest(sample):
            raise ValueError("diagnostic snapshot/sample mismatch")
        if instant(diagnostic_result["evaluated_at"]) > instant(at):
            raise ValueError("diagnostic is not available as of report time")
        if digest(diagnostic_result["policy"]) != diagnostic_result["policy_hash"]:
            raise ValueError("diagnostic policy mismatch")
        prediction_rows = diagnostic_result["results"]
        if Counter(r["story_revision_id"] for r in prediction_rows) != Counter(r["story_revision_id"] for r in sample["sample"]):
            raise ValueError("diagnostic must cover the sample exactly once")
        predictions = {}
        for row in prediction_rows:
            rid = row["story_revision_id"]
            if row["story_hash"] != digest(archive.stories[rid]):
                raise ValueError("diagnostic story hash mismatch")
            predictions[rid] = row["events"]
    strata = {(r["source"], r["historical_disposition"]): r for r in sample["strata"]}
    groups, details = {}, []
    for selected in sample["sample"]:
        rid = selected["story_revision_id"]
        source = archive.stories[rid]["payload"]["source_id"]
        disposition = archive.disposition(rid)
        stratum = strata[(source, disposition)]
        weight = stratum["population_revisions"] / stratum["sample_revisions"]
        label = active.get(rid)
        if label and label["story_hash"] != digest(archive.stories[rid]):
            raise ValueError("annotation story hash mismatch")
        baseline = disposition == "EXCLUDED"
        # Legacy emitted hits are classifier predictions, never proof of occurrence.
        # New explicit modality/relevance flags abstain from asserted-oil predictions.
        predicted = {family(e["event_type"]) for e in predictions[rid] if e.get("oil_relevance") != "irrelevant"}
        if target == "asserted_oil_event":
            predicted = {family(e["event_type"]) for e in predictions[rid]
                         if e.get("assertion", "asserted" if not e.get("qualifier") else "unclear") == "asserted"
                         and e.get("oil_relevance", "relevant") == "relevant"}
        unresolved_predictions = target == "asserted_oil_event" and bool(predictions[rid]) and any(
            e.get("assertion", "asserted" if not e.get("qualifier") else "unclear") == "unclear" or e.get("oil_relevance", "relevant") == "uncertain"
            for e in predictions[rid])
        true_family = label["event_family"] if label else None
        uncertain = (not label or label["oil_relevance"] == "uncertain" or label["assertion"] == "unclear"
                     or unresolved_predictions)
        eligible_assertions = {"asserted", "denied", "hypothetical"} if target == "candidate_discovery" else {"asserted"}
        positive = bool(label and label["oil_relevance"] == "relevant" and label["assertion"] in eligible_assertions and true_family != "none")
        families = predicted | ({true_family} if true_family else set()) or {"none"}
        outcomes = {}
        for fam in families:
            key = (source, fam, label["reviewer_type"] if label else "unreviewed")
            group = groups.setdefault(key, {"source": source, "event_family": fam, "reviewer_type": key[2],
                "sample_rows": 0, "excluded": 0, "unreviewed": 0, "uncertain": 0,
                "matched": 0, "missed": 0, "incorrect": 0, "correct_negative": 0,
                "weighted_matched": 0., "weighted_missed": 0., "weighted_incorrect": 0.})
            group["sample_rows"] += 1
            if baseline or disposition == "UNPROCESSED":
                outcome = "excluded"
            elif not label:
                outcome = "unreviewed"
            elif uncertain:
                outcome = "uncertain"
            elif positive and true_family == fam:
                outcome = "matched" if fam in predicted else "missed"
            else:
                outcome = "incorrect" if fam in predicted else "correct_negative"
            group[outcome] += 1
            if outcome in {"matched", "missed", "incorrect"}:
                group["weighted_" + outcome] += weight
            outcomes[fam] = outcome
        details.append({"story_revision_id": rid, "source": source, "disposition": disposition,
                        "stratum_weight": weight, "annotation_id": label["annotation_id"] if label else None,
                        "outcomes_by_family": outcomes})
    for group in groups.values():
        tp, fn, fp = (group["weighted_" + k] for k in ("matched", "missed", "incorrect"))
        complete = not group["unreviewed"] and not group["uncertain"]
        group["weighted_precision"] = tp / (tp + fp) if complete and tp + fp else None
        group["weighted_recall"] = tp / (tp + fn) if complete and tp + fn else None
    return {"schema": "story-coverage-v1", "as_of": at, "target": target, "manifest_records_hash": archive.manifest["records_hash"],
            "sample_hash": digest(sample), "evaluation_basis": "offline_diagnostic" if diagnostic_result else "historical_output",
            "diagnostic_policy_hash": diagnostic_result["policy_hash"] if diagnostic_result else None,
            "sample_revisions": len(sample["sample"]), "strata": sample["strata"],
            "annotations_by_reviewer_type": dict(Counter(active[s["story_revision_id"]]["reviewer_type"]
                for s in sample["sample"] if s["story_revision_id"] in active)),
            "groups": [groups[k] for k in sorted(groups)], "rows": details,
            "limitations": ["Revision/family counts are multi-label, not independent event totals.",
                "Baseline/other excluded and unprocessed revisions are outside forward recall.",
                "Legacy unqualified hits count as predictions, not fact confirmation; explicit new uncertainty abstains.",
                "Weights are population/sample within source and original disposition, not event family.",
                "Assistant annotations are separate from human labels and are not independent ground truth.",
                "Candidate discovery includes relevant asserted, denied and hypothetical claims; it is not event occurrence.",
                "Asserted-oil target is separate. Historical/unclear assertions never count as new positive events.",
                "Recall concerns captured material only, not publisher/feed completeness.",
                "Tuning-sample results are descriptive; fresh untouched post-soak holdout remains required."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--exclude-sample",type=Path,required=True)
    p.add_argument("--received-after",required=True)
    p.add_argument("--per-stratum",type=int,default=3)
    p.add_argument("--out",type=Path,required=True)
    p = sub.add_parser("browse")
    p.add_argument("--sample", type=Path)
    p.add_argument("--source")
    p.add_argument("--disposition", choices=("MATCHED", "UNMATCHED", "EXCLUDED", "UNPROCESSED"))
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--offset", type=int, default=0)
    p = sub.add_parser("show")
    p.add_argument("--revision", required=True)
    p = sub.add_parser("annotate")
    p.add_argument("--annotations", type=Path, required=True)
    p.add_argument("--file", type=Path, required=True)
    p = sub.add_parser("history")
    p.add_argument("--annotations", type=Path, required=True)
    p.add_argument("--through")
    for cmd in ("diagnose", "coverage"):
        p = sub.add_parser(cmd)
        p.add_argument("--sample", type=Path, required=True)
        p.add_argument("--out", type=Path, required=True)
        if cmd == "diagnose":
            p.add_argument("--text-field", choices=("title", "text"), default="title")
        if cmd == "coverage":
            p.add_argument("--annotations", type=Path, required=True)
            p.add_argument("--diagnostic", type=Path)
            p.add_argument("--through")
            p.add_argument("--target", choices=("candidate_discovery", "asserted_oil_event"), default="candidate_discovery")
    args = parser.parse_args(argv)
    archive = Archive(args.manifest)
    if args.command == "sample":
        result = freeze_sample(archive,per_stratum=args.per_stratum,received_after=args.received_after,exclude_sample=args.exclude_sample)
        args.out.parent.mkdir(parents=True,exist_ok=True)
        with args.out.open("x") as stream:
            json.dump(result,stream,indent=2)
    elif args.command == "browse":
        if not 1 <= args.limit <= 100 or args.offset < 0:
            raise ValueError("limit 1..100 and nonnegative offset required")
        ids = [r["story_revision_id"] for r in archive.sample(args.sample)["sample"]] if args.sample else list(archive.stories)
        rows = [{"story_revision_id": rid, "source": archive.stories[rid]["payload"]["source_id"],
                 "title": archive.stories[rid]["payload"]["title"], "disposition": archive.disposition(rid)} for rid in ids]
        rows = [r for r in rows if (not args.source or r["source"] == args.source) and
                (not args.disposition or r["disposition"] == args.disposition)]
        result = {"total": len(rows), "offset": args.offset, "stories": rows[args.offset:args.offset + args.limit]}
    elif args.command == "show":
        result = archive.show(args.revision)
    elif args.command == "annotate":
        result = annotate(archive, args.annotations, json.loads(args.file.read_text()))
    elif args.command == "history":
        result = annotation_history(args.annotations, through=args.through)
    else:
        sample = archive.sample(args.sample)
        result = (diagnostic(archive, sample, text_field=args.text_field) if args.command == "diagnose" else coverage_report(
            archive, sample, annotation_history(args.annotations, through=args.through), through=args.through, target=args.target,
            diagnostic_result=json.loads(args.diagnostic.read_text()) if args.diagnostic else None))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x") as stream:
            json.dump(result, stream, indent=2)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
