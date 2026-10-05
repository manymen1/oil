"""Isolated WPSR table 9 vintages; not a release surprise or trading signal."""
from contextlib import closing
from copy import deepcopy
import base64
import csv
from datetime import timedelta
from decimal import Decimal
import io
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil

from .clock import epoch_ns, instant, utc_now
from .macro import MAX_BYTES, MacroRecorder, eia_date, fetch_macro, number, read_macro
from .schema import digest

VERSION = "eia-detail-v1"
SOURCES = {"eia": {"url": "https://ir.eia.gov/wpsr/table9.csv", "interval_seconds": 3600,
                   "owner": "U.S. Energy Information Administration", "series": "WPSR table 9"}}
REFINERY = {"Crude Oil Inputs": "crude_inputs", "Gross Inputs": "gross_inputs",
            "Operable Capacity": "operable_capacity", "Percent Utilization": "utilization"}


def parse_detail(body):
    if len(body) > MAX_BYTES:
        raise ValueError("EIA detail size limit")
    rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig" if body.startswith(b"\xef\xbb\xbf") else "cp1252")), strict=True))
    if not rows or len(rows[0]) != 8 or rows[0][:2] != ["STUB_1", "STUB_2"]:
        raise ValueError("unknown EIA table 9 header")
    dates = [eia_date(x) for x in rows[0][2:]]
    current, previous = dates[:2]
    if current - previous != timedelta(days=7) or dates[4] != current or dates[5] != dates[2] or not dates[3] < dates[2] < previous:
        raise ValueError("EIA weekly/average column layout changed")
    facts, subsection = {}, None
    for row in rows[1:]:
        if not row or not any(row):
            continue
        if row[0] == "STUB_1":
            raise ValueError("unexpected second table header")
        section = row[0].strip()
        if section not in {"Refiner Inputs and Utilization", "Stocks (Million Barrels)"}:
            subsection = None
            continue
        if len(row) != 8:
            raise ValueError("malformed EIA detail row")
        label = row[1].strip()
        key = None
        if section == "Refiner Inputs and Utilization":
            if label in REFINERY:
                subsection = REFINERY[label]
                key = "us_" + subsection
            elif label == "Gulf Coast (PADD 3)":
                if subsection is None:
                    raise ValueError("refinery district without measure context")
                key = "padd3_" + subsection
            elif label not in {"East Coast (PADD 1)", "Midwest (PADD 2)", "Rocky Mountain (PADD 4)", "West Coast (PADD 5)"}:
                raise ValueError("unknown refinery measure")
        else:
            subsection = None
            if label == "Cushing, Oklahoma":
                key = "cushing_crude_stocks"
        if key is None:
            continue
        if key in facts:
            raise ValueError("duplicate EIA detail measure")
        level, prior = map(number, row[2:4])  # never the four-week-average columns
        stock, utilization = key == "cushing_crude_stocks", key.endswith("utilization")
        upper = 10000 if stock else 150 if utilization else 100000
        if min(level, prior) < 0 or max(level, prior) > upper:
            raise ValueError("EIA detail numeric bounds")
        if not stock and not utilization and any(v != v.to_integral_value() for v in (level, prior)):
            raise ValueError("expected integer thousand-barrel/day flow")
        units = "million_barrels" if stock else "percent" if utilization else "thousand_barrels_per_day"
        facts[key] = {"level": str(level), "previous_level": str(prior), "change": str(level-prior),
                      "units": units, "change_units": "percentage_points" if utilization else units,
                      "source_section": section, "source_label": label}
    required = {f"{region}_{measure}" for region in ("us", "padd3") for measure in REFINERY.values()} | {"cushing_crude_stocks"}
    if set(facts) != required:
        raise ValueError("required EIA detail measures missing")
    for region in ("us", "padd3"):
        for field in ("level", "previous_level"):
            capacity = Decimal(facts[f"{region}_operable_capacity"][field])
            gross = Decimal(facts[f"{region}_gross_inputs"][field])
            utilization = Decimal(facts[f"{region}_utilization"][field])
            if capacity <= 0 or abs(gross / capacity * 100 - utilization) > Decimal(".2"):
                raise ValueError("refinery capacity/utilization inconsistency")
    return [{"period": current.isoformat(), "previous_period": previous.isoformat(), "units": "per_fact",
             "facts": facts, "published_at": None, "consensus_surprise": None}]


class DetailRecorder(MacroRecorder):
    version = VERSION
    sources = SOURCES
    parsers = {"eia": parse_detail}

    def policy(self, delivery):
        return detail_policy(delivery)

    def fetch(self, source, *, session=None, contact=None):
        return fetch_macro(source, session=session, contact=contact, sources=self.sources)


def detail_policy(delivery):
    if delivery not in {"http", "local_import"}:
        raise ValueError("unknown EIA detail delivery")
    return {"schema": VERSION, "sources": SOURCES, "delivery": delivery,
            "model_processing": "not_used", "broker_execution": "disabled"}


def detail_health(root, schedule, *, at=None):
    """Metadata-only operational check; use detail_report for raw evidence validation.

    Hourly collection permits 62 minutes after a scheduled publication. No
    service restart, parse recovery, network request or journal creation here.
    """
    from .macro_scheduler import calendar, validate_schedule
    at = at or utc_now()
    issues = []
    result = {"schema": "eia-detail-health-v1", "checked_at": at,
              "root": str(Path(root).resolve()), "issues": issues,
              "trade_authorized": False, "broker_execution": "disabled",
              "verification": "operational_metadata_only"}
    try:
        validate_schedule(schedule)
        policy = deepcopy(schedule)
        policy["sources"]["eia"]["publication_grace_seconds"] = max(
            3720, policy["sources"]["eia"]["publication_grace_seconds"])
        plan = calendar(policy, "eia", at)
        result["calendar"] = {k: plan[k] for k in ("valid", "expected_release", "next_release")}
        if not plan["valid"]:
            issues.append("CALENDAR_EXPIRED_OR_NOT_STARTED")
        path = Path(root) / "macro.sqlite3"
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")

            def tail(kind):
                row = db.execute("SELECT seq,id,available_at,json_remove(payload,'$.body_base64') AS payload "
                    "FROM records WHERE kind=? ORDER BY seq DESC LIMIT 1", (kind,)).fetchone()
                return {**dict(row), "payload": json.loads(row["payload"])} if row else None

            policies = list(db.execute("SELECT id,payload FROM records WHERE kind='macro_policy'"))
            expected = detail_policy("http")
            if len(policies) != 1 or policies[0]["id"] != digest(expected) or json.loads(policies[0]["payload"]) != expected:
                raise ValueError("HTTP detail policy required")
            capture, revision = tail("macro_capture"), tail("macro_revision")
            attempt, error = tail("macro_attempt"), tail("macro_fetch_error")
            result["last_capture_id"] = capture["id"] if capture else None
            age = (epoch_ns(at) - epoch_ns(capture["available_at"])) / 1e9 if capture else None
            result["capture_age_seconds"] = age
            if age is None or not 0 <= age <= 10800:
                issues.append("CAPTURE_STALE_OR_MISSING")
            if capture:
                parsed = db.execute("SELECT payload FROM records WHERE kind='macro_parse' "
                    "AND json_extract(payload,'$.capture_id')=? ORDER BY seq DESC LIMIT 1", (capture["id"],)).fetchone()
                if capture["payload"]["status"] != 200 or not parsed or json.loads(parsed[0])["status"] != "OK":
                    issues.append("LATEST_CAPTURE_FAILED_OR_UNPARSED")
            denied = db.execute("SELECT 1 FROM records WHERE kind='macro_capture' "
                "AND json_extract(payload,'$.status') IN (401,403) LIMIT 1").fetchone()
            if denied:
                issues.append("ACCESS_DENIAL_LATCHED")
            pending = db.execute("SELECT COUNT(*) FROM records WHERE kind='macro_capture' AND id NOT IN "
                "(SELECT json_extract(payload,'$.capture_id') FROM records WHERE kind='macro_parse')").fetchone()[0]
            result["pending_parses"] = pending
            if pending:
                issues.append("PENDING_PARSES")
            if error and (not capture or error["seq"] > capture["seq"]):
                issues.append("LATEST_FETCH_FAILED")
            if attempt and (not capture or attempt["seq"] > capture["seq"]):
                age = (epoch_ns(at) - epoch_ns(attempt["available_at"])) / 1e9
                if not 0 <= age <= 120:
                    issues.append("UNFINISHED_OR_FAILED_ATTEMPT")
            period = revision["payload"]["observation"]["period"] if revision else None
            result["latest_period"] = period
            if not period:
                issues.append("NO_PARSED_DETAIL_OBSERVATION")
            else:
                period_age = (instant(at).date() - instant(period + "T00:00:00Z").date()).days
                if not 0 <= period_age <= 14 or epoch_ns(revision["available_at"]) > epoch_ns(at):
                    issues.append("REPORTING_PERIOD_STALE_OR_FUTURE")
                if plan["expected_release"] and period < plan["expected_release"]["period"]:
                    issues.append("EXPECTED_RELEASE_MISSING")
        result["free_bytes"] = shutil.disk_usage(root).free
        if result["free_bytes"] < 256 * 1024**2:
            issues.append("LOW_DISK_SPACE")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        issues.append("HEALTH_CHECK_ERROR:" + type(exc).__name__)
    result["status"] = "DEGRADED" if issues else "HEALTHY"
    return result


def detail_report(path, *, at):
    cutoff = epoch_ns(at)
    if cutoff > epoch_ns(utc_now()):
        raise ValueError("future EIA detail cutoff")
    # Bound the read against concurrent writers while validating policy and revisions.
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute("SELECT * FROM records ORDER BY seq")
            if epoch_ns(r["available_at"]) <= cutoff]
    policies = [r for r in rows if r["kind"] == "macro_policy"]
    if len(policies) != 1 or policies[0]["payload"] != detail_policy(policies[0]["payload"].get("delivery")) or digest(policies[0]["payload"]) != policies[0]["id"]:
        raise ValueError("EIA detail policy mismatch or unavailable")
    revisions = read_macro(path, through=at, version=VERSION, parsers=DetailRecorder.parsers, _rows=rows)
    newest = max(revisions, key=lambda r: (r["payload"]["observation"]["period"], r["seq"]), default=None)
    captures = [r for r in rows if r["kind"] == "macro_capture"]
    latest = captures[-1] if captures else None
    parses = {r["payload"]["capture_id"]: r for r in rows if r["kind"] == "macro_parse"}
    issues = []
    if not newest:
        issues.append("NO_PARSED_DETAIL_OBSERVATION")
    if not latest or latest["id"] not in parses or parses[latest["id"]]["payload"]["status"] != "OK":
        issues.append("LATEST_CAPTURE_FAILED_UNPARSED_OR_MISSING")
    else:
        p = latest["payload"]
        raw = base64.b64decode(p["body_base64"], validate=True)
        if (p["source"] != "eia" or p["status"] != 200 or hashlib.sha256(raw).hexdigest() != p["body_sha256"]
                or p["delivery"] != policies[0]["payload"]["delivery"]
                or epoch_ns(p["received_at"]) != epoch_ns(latest["available_at"])
                or epoch_ns(parses[latest["id"]]["available_at"]) < epoch_ns(latest["available_at"])):
            raise ValueError("EIA detail latest capture binding mismatch")
        latest_value = parse_detail(raw)[0]
        if not newest or latest_value != newest["payload"]["observation"]:
            issues.append("LATEST_CAPTURE_DIFFERS_FROM_LATEST_PERIOD")
    if not latest or (cutoff - epoch_ns(latest["available_at"])) / 1e9 > 86400:
        issues.append("CAPTURE_STALE_OR_MISSING")
    errors = [r for r in rows if r["kind"] == "macro_fetch_error"]
    if errors and (not latest or errors[-1]["seq"] > latest["seq"]):
        issues.append("LATEST_FETCH_FAILED")
    if newest and (instant(at).date() - instant(newest["payload"]["observation"]["period"] + "T00:00:00Z").date()).days > 14:
        issues.append("REPORTING_PERIOD_STALE")
    if policies[0]["payload"]["delivery"] != "http":
        issues.append("LOCAL_IMPORT_NOT_PROSPECTIVE")
    return {"schema": "eia-detail-report-v1", "through": at, "issues": issues,
            "latest_revision": newest, "last_capture_id": latest["id"] if latest else None,
            "initial_snapshot": newest["payload"]["initial_snapshot"] if newest else None,
            "trade_authorized": False, "price_direction": None, "consensus_surprise": None,
            "limitations": ["Week-over-week change is not a consensus surprise.",
                "Stock levels, flow rates and utilization percentage points are separate measures.",
                "Initial/current endpoints do not reconstruct historical first-release availability."]}
