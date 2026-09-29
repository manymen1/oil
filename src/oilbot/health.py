"""Independent read-only collector checks; optional append-only monitoring journal.

Also runnable as a frozen standalone file. Never restarts workers, resets source
circuits, deletes data, or treats a later healthy check as filling a capture gap.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import yaml


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result


def verify(run):
    run = Path(run).resolve()
    provenance = json.loads((run / "provenance.json").read_text())
    failures = []
    if not provenance.get("files") or "config.yaml" not in provenance["files"]:
        raise ValueError("frozen file inventory required")
    for name, expected in provenance["files"].items():
        path = (run / name).resolve()
        if run not in path.parents or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            failures.append(name)
    return failures


def inspect(run, unit, *, now=None, service=None, minimum_free_bytes=1024**3):
    run = Path(run).resolve()
    now = now or datetime.now(timezone.utc)
    result = {"schema": "collector-health-v1", "checked_at": now.isoformat(), "run": str(run),
              "unit": unit, "issues": [], "workers": {}, "sources": {}, "trade_authorized": False}
    issues = result["issues"]
    try:
        result["hash_mismatches"] = verify(run)
        if result["hash_mismatches"]:
            issues.append("FROZEN_FILES_CHANGED")
        config = yaml.safe_load((run / "config.yaml").read_text())
        root = Path(config["storage"]["root"])
        if not root.is_absolute():
            root = run / root
        if config.get("pipeline") != "forward" or config.get("broker_execution") != "disabled":
            issues.append("UNEXPECTED_COLLECTOR_CONFIGURATION")
        if service is None:
            proc = subprocess.run(["systemctl", "--user", "show", unit,
                "--property=ActiveState,SubState,MainPID,NRestarts"], text=True, capture_output=True, timeout=10)
            service = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
            result["service_query_exit"] = proc.returncode
        result["service"] = service
        if service.get("ActiveState") != "active" or service.get("SubState") != "running":
            issues.append("SERVICE_NOT_RUNNING")
        components = ("news", "forward", "linker", "operational") if config.get("operational_queue") else ("news", "forward", "linker")
        for journal in ("runtime", "news"):
            with closing(sqlite3.connect((root / (journal + ".sqlite3")).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                db.execute("BEGIN")
                if journal == "runtime":
                    for component in components:
                        row = db.execute("SELECT available_at FROM records WHERE kind='heartbeat' AND json_extract(payload,'$.component')=? ORDER BY seq DESC LIMIT 1", (component,)).fetchone()
                        age = (now - timestamp(row[0])).total_seconds() if row else None
                        limit = 240 if component == "news" else 90
                        result["workers"][component] = {"last_heartbeat": row[0] if row else None, "age_seconds": age, "limit_seconds": limit}
                        if age is None or not 0 <= age <= limit:
                            issues.append("WORKER_STALE:" + component)
                else:
                    for source in config["sources"]:
                        if not source["enabled"]:
                            continue
                        sid = source["id"]
                        row = db.execute("SELECT available_at,payload FROM records WHERE kind='source_health' AND json_extract(payload,'$.source_id')=? ORDER BY seq DESC LIMIT 1", (sid,)).fetchone()
                        cursor = db.execute("SELECT value FROM cursors WHERE key=?", ("source:" + sid,)).fetchone()
                        state = json.loads(cursor[0]) if cursor else {}
                        age = (now - timestamp(row[0])).total_seconds() if row else None
                        status = json.loads(row[1])["status"] if row else None
                        result["sources"][sid] = {"last_health_at": row[0] if row else None, "age_seconds": age,
                            "status": status, "failures": state.get("failures"), "circuit": state.get("circuit")}
                        if age is None or not 0 <= age <= max(240, 3 * source["poll_seconds"]):
                            issues.append("SOURCE_STALE:" + sid)
                        if state.get("circuit") or state.get("failures") or status not in {"OK", "UNCHANGED", "EMPTY"}:
                            issues.append("SOURCE_DEGRADED:" + sid)
        result["free_bytes"] = shutil.disk_usage(run).free
        if result["free_bytes"] < minimum_free_bytes:
            issues.append("LOW_DISK_SPACE")
        for review in config.get("first_party_reviews", []):
            if review.get("status") == "verified" and timestamp(review["expires_at"]) <= now:
                issues.append("IDENTITY_REVIEW_EXPIRED:" + review["source_id"])
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        issues.append("HEALTH_CHECK_ERROR:" + type(exc).__name__)
        result["error"] = str(exc)
    result["status"] = "DEGRADED" if issues else "HEALTHY"
    return result


def save_check(path, result):
    """Durable state transitions. Gap bounds are observations, not exact outages."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS checks (seq INTEGER PRIMARY KEY, checked_at TEXT NOT NULL, record TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS checks_no_update BEFORE UPDATE ON checks BEGIN SELECT RAISE(ABORT,'immutable'); END;
            CREATE TRIGGER IF NOT EXISTS checks_no_delete BEFORE DELETE ON checks BEGIN SELECT RAISE(ABORT,'immutable'); END;
        """)
        with db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT record FROM checks ORDER BY seq DESC LIMIT 1").fetchone()
            old = json.loads(prior[0]) if prior else None
            if old and (old["run"] != result["run"] or old["unit"] != result["unit"]):
                raise ValueError("monitor journal belongs to another deployment")
            if old and timestamp(result["checked_at"]) < timestamp(old["checked_at"]):
                raise ValueError("monitor clock regressed")
            record = {**result, "previous_check_at": old["checked_at"] if old else None,
                "state_changed": old is None or old["issues"] != result["issues"],
                "monitor_gap_seconds": (timestamp(result["checked_at"]) - timestamp(old["checked_at"])).total_seconds() if old else None,
                "gap_policy": "No interpolation or backfill. Inspect heartbeat and source receipt bounds; monitor unavailable while host is off."}
            db.execute("INSERT INTO checks(checked_at,record) VALUES(?,?)", (record["checked_at"], json.dumps(record, sort_keys=True)))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--unit", default="oilbot-operational.service")
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if args.verify_only:
        failures = verify(args.run)
        print(json.dumps({"hash_mismatches": failures}))
        return bool(failures)
    result = inspect(args.run, args.unit)
    if args.journal:
        result = save_check(args.journal, result)
    print(json.dumps(result, sort_keys=True))
    return result["status"] != "HEALTHY"


if __name__ == "__main__":
    raise SystemExit(main())
