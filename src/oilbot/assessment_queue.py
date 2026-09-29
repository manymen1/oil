"""Durable, fail-closed operational assessment admission worker.

Reads capture journals without writing to them. This version performs only
admission checks: it has no model adapter or economic interpretation policy.
ABSTAINED is a machine admission decision, never a reviewed economic label.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import signal
import sqlite3
import threading

from .clock import instant, utc_now
from .config import load_config
from .schema import digest
from .store import Journal, component_lock

VERSION = "assessment-admission-v1"


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def row_value(row):
    return {**dict(row), "payload": json.loads(row["payload"])} if row else None


class AdmissionWorker:
    def __init__(self, config, destination):
        if config.raw.get("pipeline") != "forward" or not config.raw.get("operational_queue"):
            raise ValueError("enabled operational forward queue required")
        self.config = config
        self.destination = Path(destination).resolve()
        if config.root == self.destination or config.root in self.destination.parents:
            raise ValueError("admission sidecar must be outside capture root")
        for path in (config.db("news"), config.db("forward"), config.db("runtime"), config.db("analysis")):
            if self.destination.exists() and path.exists() and self.destination.samefile(path):
                raise ValueError("admission sidecar cannot alias capture")
        # Capture must already exist; a bad path must never initialize journals.
        with closing(readonly(config.db("news"))) as db:
            db.execute("SELECT id FROM records LIMIT 1")
        with closing(readonly(config.db("forward"))) as db:
            epochs = {r["key"]: json.loads(r["value"]) for r in db.execute(
                "SELECT key,value FROM cursors WHERE key IN ('forward:epoch','operational:epoch')")}
        if set(epochs) != {"forward:epoch", "operational:epoch"}:
            raise ValueError("capture epochs required")
        self.policy = {"version": VERSION, "capture_root": str(config.root), "epochs": epochs,
            "sources": config.sources, "semantic_policy": "UNQUALIFIED", "model_calls": False,
            "meaning": "Machine admission only; no independent economic label or trading eligibility"}
        self.policy_hash = digest(self.policy)
        self.policy_id = "admission-policy:" + self.policy_hash
        if self.destination.exists():
            with closing(readonly(self.destination)) as db:
                first = db.execute("SELECT kind,payload FROM records ORDER BY seq LIMIT 1").fetchone()
                if not first or first["kind"] != "admission_policy" or json.loads(first["payload"]) != self.policy:
                    raise ValueError("different admission policy or foreign sidecar; use a new destination")
        self.output = Journal(self.destination)
        with self.output.transaction() as db:
            first = db.execute("SELECT kind,payload FROM records ORDER BY seq LIMIT 1").fetchone()
            if first and (first["kind"] != "admission_policy" or json.loads(first["payload"]) != self.policy):
                raise ValueError("different admission policy; use a new destination")
            prior = db.execute("SELECT payload FROM records WHERE id=?", (self.policy_id,)).fetchone()
            if prior is None:
                self.output.append("admission_policy", self.policy, record_id=self.policy_id, db=db)
            db.execute("CREATE INDEX IF NOT EXISTS admission_story ON records(json_extract(payload,'$.story_id'),seq) WHERE kind='admission_resolution'")

    def _resolution(self, candidate, story):
        p = candidate["payload"]
        reasons = []
        source = next((s for s in self.config.sources if s["id"] == p.get("source_id")), None)
        if not story or story["kind"] != "story_revision":
            return "FAILED", ["CAPTURED_STORY_MISSING"]
        if (p.get("story_hash") != digest(story) or p.get("story_id") != story["payload"].get("story_id")
                or p.get("source_id") != story["payload"].get("source_id")):
            return "FAILED", ["CAPTURE_BINDING_MISMATCH"]
        if instant(story["available_at"]) > instant(candidate["available_at"]):
            return "FAILED", ["CANDIDATE_PREDATES_STORY"]
        if source is None or not source["enabled"]:
            reasons.append("SOURCE_NOT_ENABLED")
        if p.get("forward_capture_candidate") is not True or p.get("exclusions"):
            reasons.append("CAPTURE_EXCLUDED")
        if p.get("story_status") in {"correction", "withdrawal", "deleted"}:
            reasons.append("LIFECYCLE_REQUIRES_REVIEW")
        current_rights = source["rights"]["model_processing"] if source else "unknown"
        if current_rights != "permitted" or p.get("model_processing") != "permitted":
            reasons.append("SOURCE_MODEL_RIGHTS_NOT_PERMITTED")
        # Permitted processing is not a validated economic policy. This version
        # intentionally cannot upgrade any candidate to an eligible assessment.
        reasons.append("AUTOMATED_ECONOMIC_POLICY_UNQUALIFIED")
        return "ABSTAINED", reasons

    def run_once(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("bounded positive batch size required")
        processed = 0
        counts = {"ABSTAINED": 0, "FAILED": 0}
        with self.output.transaction() as out, closing(readonly(self.config.db("forward"))) as forward, closing(readonly(self.config.db("news"))) as news:
            forward.execute("BEGIN")
            news.execute("BEGIN")
            cursor_row = out.execute("SELECT value FROM cursors WHERE key='admission:progress'").fetchone()
            cursor = json.loads(cursor_row[0]) if cursor_row else {"seq": 0}
            if cursor["seq"]:
                previous = row_value(forward.execute("SELECT * FROM records WHERE seq=?", (cursor["seq"],)).fetchone())
                if not previous or digest(previous) != cursor["candidate_hash"]:
                    raise ValueError("capture cursor history changed; refuse to skip or rewind")
            at = utc_now()
            last = out.execute("SELECT available_at FROM records ORDER BY seq DESC LIMIT 1").fetchone()
            if last and instant(at) < instant(last["available_at"]):
                raise ValueError("admission clock regressed")
            candidates = forward.execute("SELECT * FROM records WHERE kind='operational_review_candidate' AND seq>? ORDER BY seq LIMIT ?", (cursor["seq"], limit)).fetchall()
            for raw in candidates:
                candidate = row_value(raw)
                if instant(candidate["available_at"]) > instant(at):
                    break  # Do not drop future-dated inputs or advance past them.
                p = candidate["payload"]
                story = row_value(news.execute("SELECT * FROM records WHERE id=?", (p.get("story_revision_id"),)).fetchone())
                state, reasons = self._resolution(candidate, story)
                previous = out.execute("SELECT id FROM records WHERE kind='admission_resolution' AND json_extract(payload,'$.story_id')=? ORDER BY seq DESC LIMIT 1", (p.get("story_id"),)).fetchone()
                previous_id = previous[0] if previous else None
                rid = digest([VERSION, self.policy_hash, candidate["id"]])
                self.output.append("admission_resolution", {
                    "candidate_id": candidate["id"], "candidate_hash": digest(candidate), "candidate_available_at": candidate["available_at"],
                    "story_id": p.get("story_id"), "story_revision_id": p.get("story_revision_id"),
                    "story_hash": digest(story) if story else None, "source_id": p.get("source_id"),
                    "state": state, "reason_codes": reasons, "capture_exclusions": p.get("exclusions", []),
                    "resolved_at": at, "policy_hash": self.policy_hash, "reviewer_type": "deterministic_policy",
                    "supersedes_resolution_id": previous_id, "input_revision_ids": [self.policy_id] + ([previous_id] if previous_id else []),
                    "external_input_ids": [candidate["id"]] + ([story["id"]] if story else []),
                    "economic_assessment_created": False, "model_invoked": False,
                    "research_eligible": False, "trade_authorized": False}, record_id=rid, available_at=at, db=out)
                cursor = {"seq": candidate["seq"], "candidate_hash": digest(candidate), "resolved_at": at}
                self.output.set_cursor(out, "admission:progress", cursor)
                processed += 1
                counts[state] += 1
            # Append even on an idle pass: this is a worker heartbeat, not a claim
            # that unresolved economic research is complete.
            self.output.append("admission_heartbeat", {"processed": processed, "states": counts,
                "candidate_cursor": cursor["seq"], "policy_hash": self.policy_hash}, available_at=at, db=out)
        return {"processed": processed, "states": counts, "candidate_cursor": cursor["seq"], "policy_hash": self.policy_hash}


def report(path):
    with closing(readonly(path)) as db:
        db.execute("BEGIN")
        rows = [row_value(r) for r in db.execute("SELECT * FROM records WHERE kind='admission_resolution' ORDER BY seq")]
        heartbeat = row_value(db.execute("SELECT * FROM records WHERE kind='admission_heartbeat' ORDER BY seq DESC LIMIT 1").fetchone())
    latest = {r["payload"]["story_id"]: r for r in rows}
    return {"schema": VERSION, "resolutions": rows, "resolved_candidates": len(rows), "current_stories": len(latest),
        "current_resolution_ids": sorted(r["id"] for r in latest.values()), "last_heartbeat": heartbeat,
        "economic_assessments": 0, "trade_authorized": False,
        "limitations": ["Admission decisions only; not semantic reviews or economic labels.",
                        "Only operational queue candidates are consumed; unmatched stories and fast-only events remain separate.",
                        "Policy changes require a new sidecar; pending human review is not erased."]}


def snapshot_resolutions(archive, path, *, through):
    """Bind machine admission history to a verified capture snapshot for export.

    This returns a separate record stream, never economic_assessments. A sidecar
    newer than the snapshot fails closed if its visible inputs are absent.
    """
    with closing(readonly(path)) as db:
        db.execute("BEGIN")
        policy_row = row_value(db.execute("SELECT * FROM records WHERE kind='admission_policy' ORDER BY seq LIMIT 1").fetchone())
        rows = [row_value(r) for r in db.execute("SELECT * FROM records WHERE kind='admission_resolution' ORDER BY seq")]
    if not policy_row or policy_row["payload"].get("version") != VERSION:
        raise ValueError("unsupported admission policy")
    policy = policy_row["payload"]
    policy_hash = digest(policy)
    if policy_row["id"] != "admission-policy:" + policy_hash or policy["sources"] != archive.manifest["config"]["sources"]:
        raise ValueError("admission policy or source binding mismatch")
    with closing(readonly(archive.path.parent / "forward.sqlite3")) as db:
        epochs = {r["key"]: json.loads(r["value"]) for r in db.execute(
            "SELECT key,value FROM cursors WHERE key IN ('forward:epoch','operational:epoch')")}
    if epochs != policy["epochs"]:
        raise ValueError("admission capture epochs mismatch")
    selected, prior = [], {}
    for row in rows:
        if instant(row["available_at"]) > instant(through):
            continue
        p = row["payload"]
        candidate = archive.records.get(p["candidate_id"])
        if not candidate or candidate["kind"] != "operational_review_candidate" or digest(candidate) != p["candidate_hash"]:
            raise ValueError("admission candidate missing or changed in snapshot")
        if (any(p[k] != candidate["payload"].get(k) for k in ("story_revision_id", "story_id", "source_id"))
                or p["candidate_available_at"] != candidate["available_at"]):
            raise ValueError("admission story/candidate binding mismatch")
        story = archive.records.get(p["story_revision_id"])
        if (story is not None and digest(story) != p["story_hash"]) or (story is None and p["story_hash"] is not None):
            raise ValueError("admission story missing or changed in snapshot")
        if (p["policy_hash"] != policy_hash or row["id"] != digest([VERSION, policy_hash, candidate["id"]])
                or p["resolved_at"] != row["available_at"] or instant(candidate["available_at"]) > instant(row["available_at"])
                or instant(policy_row["available_at"]) > instant(row["available_at"])):
            raise ValueError("admission identity or availability mismatch")
        if (p["state"] not in {"ABSTAINED", "FAILED"} or p["reviewer_type"] != "deterministic_policy"
                or any(p[k] is not False for k in ("model_invoked", "research_eligible", "trade_authorized", "economic_assessment_created"))):
            raise ValueError("admission must not become an economic assessment")
        if p["supersedes_resolution_id"] != prior.get(p["story_id"]):
            raise ValueError("admission supersession mismatch")
        prior[p["story_id"]] = row["id"]
        selected.append(row)
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True, type=Path, help="Separate admission journal, outside capture root")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    if args.report:
        print(json.dumps(report(args.out), indent=2))
        return
    config = load_config(args.config)
    with component_lock(args.out.resolve().parent, "assessment-admission"):
        worker = AdmissionWorker(config, args.out)
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        while not stop.is_set():
            print(json.dumps(worker.run_once(args.limit)), flush=True)
            if args.once:
                break
            stop.wait(30)


if __name__ == "__main__":
    main()
