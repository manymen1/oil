"""Release-aware numeric-source polling and independent read-only health.

No broker, model or strategy execution. Schedules are dated assumptions, never
publication timestamps. Collector attempt/backoff state remains authoritative.
"""
from contextlib import closing
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import time
from zoneinfo import ZoneInfo

from .clock import epoch_ns, instant, utc_now
from .macro import MacroRecorder, SOURCES
from .schema import digest
from .store import Journal, component_lock

MINIMUM_FREE_BYTES = 256 * 1024 * 1024


def load_contact(path):
    """Read one private assignment, never execute an environment file as code."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        stat = os.fstat(stream.fileno())
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077 or stat.st_size > 4096:
            raise ValueError("contact file requires owner-only permissions and at most 4096 bytes")
        tokens = shlex.split(stream.read())
    if tokens[:1] == ["export"]:
        tokens = tokens[1:]
    if len(tokens) != 1 or not tokens[0].startswith("OILBOT_SOURCE_CONTACT="):
        raise ValueError("contact file must contain only OILBOT_SOURCE_CONTACT assignment")
    value = tokens[0].split("=", 1)[1]
    if not re.fullmatch(r"[^\s<>]+@[^\s<>]+\.[^\s<>]+|https://[^\s<>]+", value):
        raise ValueError("valid owner contact required")
    return value


def load_schedule(path):
    policy = json.loads(Path(path).read_text())
    validate_schedule(policy)
    return policy


def validate_schedule(p):
    if p["schema"] != "macro-scheduler-v1" or p["timezone"] != "America/New_York" or set(p["sources"]) != set(SOURCES):
        raise ValueError("unsupported macro schedule")
    start, end = date.fromisoformat(p["calendar_start"]), date.fromisoformat(p["calendar_end"])
    reviewed = date.fromisoformat(p["reviewed_at"])
    if not start <= reviewed < end or not 1 <= (end - start).days <= 366:
        raise ValueError("bounded reviewed calendar required")
    for key, lo, hi in (("tick_seconds", 1, 60), ("heartbeat_stale_seconds", 120, 600)):
        if type(p[key]) is not int or not lo <= p[key] <= hi:
            raise ValueError("invalid scheduler timing")
    for source, s in p["sources"].items():
        if (s["weekday"], s["release_time"], s["period_lag_days"]) != ((2, "10:30", 5) if source == "eia" else (4, "15:30", 3)):
            raise ValueError("unqualified weekly release convention")
        if not isinstance(s["schedule_url"], str) or not s["schedule_url"].startswith("https://"):
            raise ValueError("calendar provenance URL required")
        for key in ("window_before_seconds", "window_after_seconds", "window_poll_seconds", "background_poll_seconds", "publication_grace_seconds"):
            if type(s[key]) is not int or not 0 <= s[key] <= 172800:
                raise ValueError("invalid poll policy")
        if not SOURCES[source]["interval_seconds"] <= s["window_poll_seconds"] <= s["background_poll_seconds"]:
            raise ValueError("polling below source limit")
        if not s["window_after_seconds"] or not s["publication_grace_seconds"]:
            raise ValueError("release window and grace required")
        for original, shifted in s["overrides"].items():
            day, target = date.fromisoformat(original), datetime.fromisoformat(shifted)
            if not start <= day < end or day.weekday() != s["weekday"] or target.tzinfo is not None:
                raise ValueError("invalid calendar exception")
            if not 0 <= (target.date() - day).days <= 7 or not start <= target.date() < end:
                raise ValueError("invalid shifted release")


def calendar(policy, source, at):
    zone = ZoneInfo(policy["timezone"])
    now = instant(at)
    day = now.astimezone(zone).date()
    start, end = date.fromisoformat(policy["calendar_start"]), date.fromisoformat(policy["calendar_end"])
    s = policy["sources"][source]
    valid = start <= day < end
    events = []
    cursor = start
    while cursor < end:
        if cursor.weekday() == s["weekday"]:
            local = datetime.fromisoformat(s["overrides"].get(cursor.isoformat(), cursor.isoformat() + "T" + s["release_time"]))
            release = local.replace(tzinfo=zone).astimezone(now.tzinfo)
            events.append({"release_at": release.isoformat(),
                           "period": (cursor - timedelta(days=s["period_lag_days"])).isoformat()})
        cursor += timedelta(days=1)
    events.sort(key=lambda e: instant(e["release_at"]))
    active = next((e for e in events if instant(e["release_at"]) - timedelta(seconds=s["window_before_seconds"])
                   <= now <= instant(e["release_at"]) + timedelta(seconds=s["window_after_seconds"])), None)
    expected = [e for e in events if instant(e["release_at"]) + timedelta(seconds=s["publication_grace_seconds"]) <= now]
    upcoming = next((e for e in events if instant(e["release_at"]) > now), None)
    return {"valid": valid, "mode": "RELEASE_WINDOW" if active else "BACKGROUND",
            "poll_seconds": s["window_poll_seconds"] if active else s["background_poll_seconds"],
            "active_release": active, "expected_release": expected[-1] if expected else None,
            "next_release": upcoming}


def readonly_rows(path, *, summary=False):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if summary:
            # Indexed tail reads, not a scan over years of heartbeat payloads.
            rows = list(db.execute("SELECT * FROM records WHERE kind IN ('scheduler_policy','macro_policy')"))
            for kind in ("scheduler_start", "scheduler_tick", "scheduler_health"):
                rows.extend(db.execute("SELECT * FROM records WHERE kind=? ORDER BY seq DESC LIMIT 1", (kind,)))
            for source in SOURCES:
                rows.extend(db.execute("SELECT * FROM records WHERE kind='scheduler_source' AND json_extract(payload,'$.source')=? ORDER BY seq DESC LIMIT 1", (source,)))
        else:
            rows = db.execute("SELECT seq,id,kind,available_at,recorded_at,json_remove(payload,'$.body_base64') AS payload FROM records ORDER BY seq")
        return sorted([{**dict(r), "payload": json.loads(r["payload"])} for r in rows], key=lambda r: r["seq"])


def worker_policy(root, policy):
    return {"schema": "macro-worker-v1", "root": str(Path(root).resolve()), "schedule": policy,
            "model_processing": False, "trade_authorized": False}


def verify_state(rows, identity):
    policies = [r for r in rows if r["kind"] == "scheduler_policy"]
    if len(policies) != 1 or policies[0]["id"] != digest(identity) or policies[0]["payload"] != identity:
        raise ValueError("different scheduler policy or capture root; use a new state directory")


def health(root, state, policy, *, at=None, contact_configured=None):
    """No DB creation, recovery, network, state reset or service mutation."""
    validate_schedule(policy)
    at = at or utc_now()
    now = instant(at)
    issues, sources = [], {}
    result = {"schema": "macro-health-v1", "checked_at": at, "root": str(Path(root).resolve()),
              "state": str(Path(state).resolve()), "sources": sources, "issues": issues,
              "trade_authorized": False, "broker_execution": "disabled"}
    try:
        rows = readonly_rows(Path(root) / "macro.sqlite3")
        states = readonly_rows(Path(state) / "scheduler.sqlite3", summary=True)
        result["free_bytes"] = min(shutil.disk_usage(root).free, shutil.disk_usage(state).free)
        if result["free_bytes"] < MINIMUM_FREE_BYTES:
            issues.append("LOW_DISK_SPACE")
        verify_state(states, worker_policy(root, policy))
        ticks = [r for r in states if r["kind"] == "scheduler_tick"]
        starts = [r for r in states if r["kind"] == "scheduler_start"]
        last = ticks[-1] if ticks else None
        age = (now - instant(last["available_at"])).total_seconds() if last else None
        result["heartbeat_age_seconds"] = age
        if age is None or not 0 <= age <= policy["heartbeat_stale_seconds"]:
            issues.append("WORKER_HEARTBEAT_STALE")
        if starts and (not last or starts[-1]["seq"] > last["seq"]):
            issues.append("WORKER_CYCLE_INCOMPLETE")
        if any(instant(r["available_at"]) > now for r in rows + states):
            issues.append("CLOCK_REGRESSION")
        macro_policies = [r for r in rows if r["kind"] == "macro_policy"]
        if len(macro_policies) != 1 or macro_policies[0]["payload"].get("delivery") != "http":
            issues.append("HTTP_CAPTURE_POLICY_REQUIRED")
        if not calendar(policy, "eia", at)["valid"]:
            issues.append("RELEASE_CALENDAR_EXPIRED_OR_NOT_STARTED")
        if contact_configured is False:
            issues.append("EIA_OWNER_CONTACT_REQUIRED")
        for source in SOURCES:
            plan = calendar(policy, source, at)
            captures = [r for r in rows if r["kind"] == "macro_capture" and r["payload"]["source"] == source]
            parses = [r for r in rows if r["kind"] == "macro_parse" and r["payload"]["source"] == source]
            revisions = [r for r in rows if r["kind"] == "macro_revision" and r["payload"]["source"] == source]
            successes = [r for r in parses if r["payload"]["status"] == "OK"]
            completed = {r["payload"]["capture_id"] for r in parses}
            pending = sum(r["id"] not in completed for r in captures)
            latest_period = max((r["payload"]["observation"]["period"] for r in revisions), default=None)
            successful = successes[-1] if successes else None
            capture_by_id = {r["id"]: r for r in captures}
            last_receipt = capture_by_id[successful["payload"]["capture_id"]]["available_at"] if successful else None
            success_age = (now - instant(last_receipt)).total_seconds() if last_receipt else None
            source_issues = []
            if pending:
                source_issues.append("PENDING_RAW_PARSE")
            if any(r["payload"]["status"] in {401, 403} for r in captures):
                source_issues.append("ACCESS_DENIAL_LATCHED")
            active = plan["active_release"]
            warmup = (active is not None and now < instant(active["release_at"])
                      - timedelta(seconds=policy["sources"][source]["window_before_seconds"]) + timedelta(seconds=180))
            if success_age is None or success_age < 0 or (not warmup and success_age > plan["poll_seconds"] + 180):
                source_issues.append("SUCCESSFUL_POLL_STALE")
            expected = plan["expected_release"]
            if expected and (latest_period is None or latest_period < expected["period"]):
                source_issues.append("EXPECTED_RELEASE_MISSING")
            if captures and captures[-1]["payload"]["status"] == 429:
                source_issues.append("RATE_LIMIT_BACKOFF")
            results = [r for r in states if r["kind"] == "scheduler_source" and r["payload"]["source"] == source]
            latest_result = results[-1]["payload"]["result"] if results else None
            if (latest_result and latest_result["status"] in {"FAILED", "BLOCKED"}
                    and (not successful or instant(successful["available_at"]) <= instant(results[-1]["available_at"]))):
                source_issues.append("LAST_COLLECTION_" + latest_result["status"])
            # Check the collector ledger too: a manual poll can fail after the worker's last tick.
            failures = [r for r in rows if r["kind"] == "macro_fetch_error" and r["payload"]["source"] == source]
            failures += [r for r in parses if r["payload"]["status"] != "OK"]
            if failures and (not successful or max(r["seq"] for r in failures) > successful["seq"]):
                source_issues.append("LATEST_SOURCE_ATTEMPT_FAILED")
            sources[source] = {"schedule": plan, "latest_period": latest_period,
                "last_successful_receipt_at": last_receipt, "success_age_seconds": success_age,
                "pending_parses": pending, "issues": sorted(source_issues)}
            issues.extend(source + ":" + value for value in source_issues)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        issues.append("HEALTH_CHECK_ERROR:" + type(exc).__name__)
    result["issues"] = sorted(set(issues))
    result["status"] = "DEGRADED" if issues else "HEALTHY"
    return result


class MacroWorker:
    def __init__(self, root, state, policy, *, contact=None):
        validate_schedule(policy)
        self.root, self.state = Path(root).resolve(), Path(state).resolve()
        if self.root == self.state or self.root in self.state.parents or self.state in self.root.parents:
            raise ValueError("scheduler state must be separate from capture root")
        self.policy, self.contact = policy, contact
        self.identity = worker_policy(root, policy)
        path = self.state / "scheduler.sqlite3"
        if path.exists():
            verify_state(readonly_rows(path, summary=True), self.identity)
        self.store = Journal(path)
        with self.store.transaction() as db:
            rows = db.execute("SELECT * FROM records WHERE kind='scheduler_policy'").fetchall()
            if db.execute("SELECT 1 FROM records LIMIT 1").fetchone():
                verify_state([{**dict(r), "payload": json.loads(r["payload"])} for r in rows], self.identity)
            else:
                self.store.append("scheduler_policy", self.identity, record_id=digest(self.identity), db=db)
            db.execute("CREATE INDEX IF NOT EXISTS scheduler_source_tail ON records(json_extract(payload,'$.source'),seq) WHERE kind='scheduler_source'")
        self.recorder = MacroRecorder(self.root)

    def latest(self, kind):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM records WHERE kind=? ORDER BY seq DESC LIMIT 1", (kind,)).fetchone()
            return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def run_once(self):
        with component_lock(self.state, "scheduler"):
            at = utc_now()
            previous = [self.latest(kind) for kind in ("scheduler_start", "scheduler_tick", "scheduler_health")]
            if any(r and epoch_ns(at) < epoch_ns(r["available_at"]) for r in previous):
                raise ValueError("scheduler clock regressed")
            start_id = self.store.append("scheduler_start", {"policy_id": digest(self.identity)}, available_at=at)
            results = {}
            for source in SOURCES:
                now = utc_now()
                plan = calendar(self.policy, source, now)
                if min(shutil.disk_usage(self.root).free, shutil.disk_usage(self.state).free) < MINIMUM_FREE_BYTES:
                    results[source] = {"status": "BLOCKED", "reason": "LOW_DISK_SPACE"}
                    continue
                if not plan["valid"]:
                    results[source] = {"status": "BLOCKED", "reason": "RELEASE_CALENDAR_EXPIRED_OR_NOT_STARTED"}
                    continue
                # Recovery also runs outside poll windows, without fetching.
                try:
                    with component_lock(self.root, "macro"):
                        self.recorder.recover()
                    attempts = [r for r in self.recorder.store.records("macro_attempt") if r["payload"]["source"] == source]
                    checks = [r for r in readonly_rows(self.store.path, summary=True) if r["kind"] == "scheduler_source" and r["payload"]["source"] == source]
                    last_check = max((instant(r["available_at"]) for r in attempts[-1:] + checks), default=None)
                    if last_check and (instant(now) - last_check).total_seconds() < plan["poll_seconds"]:
                        results[source] = {"status": "WAIT", "reason": "SCHEDULE_INTERVAL"}
                        continue
                    result = self.recorder.collect(source, contact=self.contact if source == "eia" else None)
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    # Never journal exception text that may contain URLs/contact details.
                    result = {"status": "FAILED", "reason": type(exc).__name__}
                self.store.append("scheduler_source", {"source": source, "result": result, "mode": plan["mode"]})
                results[source] = result
            self.store.append("scheduler_tick", {"start_id": start_id, "results": results})
            report = health(self.root, self.state, self.policy, contact_configured=bool(self.contact))
            old = self.latest("scheduler_health")
            changed = (old["payload"]["issues"] != report["issues"]) if old else bool(report["issues"])
            report["state_changed"] = changed
            self.store.append("scheduler_health", report)
            if changed:
                self.store.append("scheduler_alert", {"checked_at": report["checked_at"], "status": report["status"],
                    "issues": report["issues"], "previous_issues": old["payload"]["issues"] if old else None})
            return {"results": results, "health": report, "trade_authorized": False}

    def run(self, stop, *, seconds=None, emit=None):
        if seconds is not None and (type(seconds) is not int or not 1 <= seconds <= 86400):
            raise ValueError("run duration must be 1..86400 seconds")
        deadline = time.monotonic() + seconds if seconds is not None else None
        # A process lock prevents parallel serving loops; individual ticks also
        # take the state lock, so --once cannot overlap a serving tick.
        with component_lock(self.state, "scheduler_process"):
            while not stop.is_set() and (deadline is None or time.monotonic() < deadline):
                result = self.run_once()
                if emit and result["health"]["state_changed"]:
                    emit(result["health"])
                remaining = self.policy["tick_seconds"] if deadline is None else max(0, min(self.policy["tick_seconds"], deadline - time.monotonic()))
                stop.wait(remaining)
