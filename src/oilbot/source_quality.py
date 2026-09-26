"""Read-only, evidence-backed source qualification and collector health.

Network success is not identity verification or permission to process content.
These reports never fetch, activate sources, initialize journals, or call a model.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import asdict
import json
import math
from pathlib import Path
import sqlite3
from statistics import median

from .clock import epoch_ns, utc_now
from .schema import digest
from .sources import allowed

SUCCESS = {"OK", "EMPTY", "UNCHANGED", "RECOVERED_UNPARSED_RESPONSE"}
REVIEW_FIELDS = {"identity", "capture", "model_processing", "timestamps", "revisions", "attribution"}


def read_evidence(path):
    """Consistent SQLite read transaction, including WAL; never initialize storage."""
    path = Path(path).resolve()
    if not path.exists():
        return [], {}, "MISSING"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("unsupported news journal schema")
        records = [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute("SELECT * FROM records ORDER BY seq")]
        cursors = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM cursors")}
    return records, cursors, "PRESENT"


def load_qualification(path):
    value = json.loads(Path(path).read_text())
    if value.get("schema") != "source-qualification-v1":
        raise ValueError("unsupported source qualification schema")
    candidates = value.get("candidates", [])
    if not isinstance(candidates, list) or any(not isinstance(s, str) or not s for s in candidates) or len(set(candidates)) != len(candidates):
        raise ValueError("unique candidate source IDs required")
    reviews = {}
    for review in value.get("reviews", []):
        required = {"source", "source_policy_hash", "profile_hash", "reviewed_at", "expires_at", "reviewer", "checks"}
        if set(review) != required or not isinstance(review["reviewer"], str) or not review["reviewer"].strip():
            raise ValueError("complete dated source review and reviewer required")
        if review["source"] in reviews or set(review["checks"]) != REVIEW_FIELDS:
            raise ValueError("one review per source with all qualification checks required")
        if epoch_ns(review["expires_at"]) <= epoch_ns(review["reviewed_at"]):
            raise ValueError("review expiry must follow review time")
        for name, check in review["checks"].items():
            positive = "permitted" if name in {"capture", "model_processing"} else "verified"
            if set(check) != {"status", "evidence"} or check["status"] not in {positive, "pending", "prohibited"}:
                raise ValueError("invalid qualification check: " + name)
            if not isinstance(check["evidence"], str) or (check["status"] != "pending" and not check["evidence"].strip()):
                raise ValueError("reviewed checks require evidence references")
        reviews[review["source"]] = review
    return candidates, reviews


def _seconds(after, before):
    return (epoch_ns(after) - epoch_ns(before)) / 1e9


def _summary(values):
    ordered = sorted(values)
    return {"samples": len(values), "median": median(values) if values else None,
            "p95": ordered[math.ceil(len(values) * .95) - 1] if values else None,
            "max": max(values) if values else None}


def _live_observation(row):
    p = row["payload"]
    return (row["kind"] == "observation" and not p.get("synthetic")
            and p.get("delivery") != "local_import")


def _transport(source, observations):
    # Old or unbound source-policy evidence cannot qualify a changed endpoint/parser.
    return [r for r in observations if _live_observation(r)
            and r["payload"].get("source_policy") == digest(source)
            and r["payload"].get("requested_url", r["payload"].get("url")) == source["url"]]


def source_health(source, records, cursors, *, now, window_seconds):
    sid = source["id"]
    by_id = {r["id"]: r for r in records}
    observations = [r for r in records if r["kind"] == "observation" and r["payload"].get("source_id") == sid]
    transport = _transport(source, observations)
    transport_ids = {r["id"] for r in transport}
    recent = lambda r: 0 <= _seconds(now, r["available_at"]) <= window_seconds
    main_health, detail_health = [], []
    for row in records:
        p = row["payload"]
        if row["kind"] != "source_health" or p.get("source_id") != sid:
            continue
        refs = p.get("input_revision_ids", [])
        linked = [by_id[rid] for rid in refs if rid in by_id and by_id[rid]["kind"] == "observation"]
        if linked and not any(_live_observation(r) for r in linked):
            continue  # Local imports and fixture recoveries are not collector activity.
        if p.get("scope") == "detail" or p.get("status") == "DETAIL_FAILED":
            detail_health.append(row)
        elif any(rid in transport_ids for rid in refs) or (not refs and p.get("source_policy") == digest(source)):
            main_health.append(row)
    receipts = [r for r in records if r["kind"] == "parse_receipt"
                and any(ref in transport_ids for ref in r["payload"].get("input_revision_ids", []))]
    parsed_ids = {rid for r in receipts for rid in r["payload"].get("input_revision_ids", [])}
    successful = [r for r in transport if r["payload"].get("status") in {200, 304} and r["id"] in parsed_ids]
    last = transport[-1] if transport else None
    last_ok = successful[-1] if successful else None
    last_health = main_health[-1] if main_health else None
    age = _seconds(now, last_ok["available_at"]) if last_ok else None
    threshold = source["poll_seconds"] * 3
    failures = 0
    for row in reversed(main_health):
        if row["payload"]["status"] in SUCCESS | {"OK_DETAIL_INCOMPLETE"}:
            break
        failures += 1
    warnings = []
    latest_status = last_health["payload"]["status"] if last_health else None
    if latest_status and latest_status not in SUCCESS | {"OK_DETAIL_INCOMPLETE"}:
        warnings.append("LATEST_COLLECTION_FAILED")
    if not last_ok:
        warnings.append("NO_SUCCESSFUL_COLLECTION")
    elif age > threshold:
        warnings.append("STALE_COLLECTION")
    if age is not None and age < 0:
        warnings.append("CLOCK_ORDER_ERROR")
    if last and last["id"] not in parsed_ids and last["payload"].get("status") in {200, 304}:
        warnings.append("UNPARSED_RESPONSE")
    if latest_status == "OK_DETAIL_INCOMPLETE":
        warnings.append("DETAIL_COLLECTION_INCOMPLETE")
    if not source["enabled"]:
        state = "DISABLED"
    elif "LATEST_COLLECTION_FAILED" in warnings:
        state = "FAILING"
    elif not last_ok:
        state = "NEVER_SUCCEEDED"
    elif "STALE_COLLECTION" in warnings:
        state = "STALE"
    else:
        state = "DEGRADED" if warnings else "HEALTHY"
    latency, invalid_latency = [], 0
    for receipt in receipts:
        if not recent(receipt):
            continue
        for ref in receipt["payload"].get("input_revision_ids", []):
            if ref in transport_ids:
                delay = _seconds(receipt["available_at"], by_id[ref]["available_at"]) * 1000
                if delay < 0:
                    invalid_latency += 1
                else:
                    latency.append(delay)
    stories = [r for r in records if r["kind"] == "story_revision" and r["payload"].get("source_id") == sid and recent(r)]
    live_story = [r for r in stories if any(ref in by_id and _live_observation(by_id[ref]) for ref in r["payload"].get("input_revision_ids", []))]
    times = [r["payload"].get("published_at") for r in live_story]
    publication_lags, future_publications, invalid_publications = [], 0, 0
    for row in live_story:
        p = row["payload"]
        if p.get("published_at"):
            try:
                lag = _seconds(p["observed_at"], p["published_at"])
                if lag < 0:
                    future_publications += 1
                else:
                    publication_lags.append(lag)
            except ValueError:
                invalid_publications += 1
    cursor = cursors.get("source:" + sid, {})
    circuit = cursor.get("circuit")
    if source["enabled"] and circuit and circuit.get("source_policy") == digest(source):
        state = "CIRCUIT_OPEN"
        warnings.append("OPERATOR_REVIEW_REQUIRED")
    next_poll = cursor.get("next_poll")
    backoff = bool(failures and next_poll and _seconds(next_poll, now) > 0)
    window_health = [r for r in main_health if recent(r)]
    intervals = [_seconds(b["available_at"], a["available_at"])
                 for a, b in zip(transport, transport[1:]) if recent(b)]
    return {"source": sid, "enabled": source["enabled"], "state": state, "warnings": warnings,
            "last_response_at": last["available_at"] if last else None,
            "last_http_status": last["payload"].get("status") if last else None,
            "last_success_at": last_ok["available_at"] if last_ok else None,
            "seconds_since_success": age, "stale_after_seconds": threshold,
            "last_health_status": latest_status, "consecutive_failed_health_events": failures,
            "next_poll_at": next_poll, "backoff_active": backoff, "circuit": circuit,
            "window": {"transport_responses": sum(recent(r) for r in transport),
                       "response_interval_seconds": _summary([v for v in intervals if v >= 0]),
                       "negative_response_intervals": sum(v < 0 for v in intervals),
                       "health_events": dict(Counter(r["payload"]["status"] for r in window_health)),
                       "detail_failures": sum(recent(r) for r in detail_health),
                       "story_revisions": len(live_story), "local_or_fixture_revisions": len(stories) - len(live_story),
                       "corrections": sum(r["payload"].get("status") == "correction" for r in live_story),
                       "withdrawals_or_deletions": sum(r["payload"].get("status") in {"withdrawal", "deleted"} for r in live_story),
                       "missing_publication_times": sum(t is None for t in times),
                       "future_publication_times": future_publications, "invalid_publication_times": invalid_publications,
                       "capture_to_parse_ms": _summary(latency), "negative_capture_to_parse_samples": invalid_latency,
                       "publication_to_receipt_seconds": _summary(publication_lags)},
            "evidence_ids": {"last_response": last["id"] if last else None,
                             "last_success": last_ok["id"] if last_ok else None,
                             "last_health": last_health["id"] if last_health else None}}


def collection_health(config, *, now=None, window_seconds=86400):
    if type(window_seconds) is not int or window_seconds <= 0:
        raise ValueError("positive integer report window required")
    now = now or utc_now()
    records, cursors, storage = read_evidence(config.db("news"))
    rows = [source_health(source, records, cursors, now=now, window_seconds=window_seconds) for source in config.sources]
    return {"schema": "collection-health-v1", "evaluated_at": now, "storage": storage,
            "window_seconds": window_seconds, "config_hash": digest(config.raw),
            "records_hash": digest(records), "cursors_hash": digest(cursors),
            "summary": dict(Counter(row["state"] for row in rows)), "sources": rows,
            "limitations": ["Health measures capture/parse, not publisher completeness or truth.",
                            "No observed HTTP response is not proof of downtime.",
                            "Publication-to-receipt delay is not original-event latency.",
                            "Backoff does not make a stale collector healthy; 304 proves transport only."]}


def qualify_sources(config, profiles, candidates, reviews, *, source_ids=None, now=None, evidence_max_age_seconds=86400):
    if type(evidence_max_age_seconds) is not int or evidence_max_age_seconds <= 0:
        raise ValueError("positive integer evidence age required")
    now = now or utc_now()
    records, cursors, storage = read_evidence(config.db("news"))
    registry = {s["id"]: s for s in config.sources}
    selected = source_ids if source_ids else sorted(set(registry) | set(candidates))
    if set(selected) - (set(registry) | set(profiles)):
        raise ValueError("unknown source selected")
    by_id = {r["id"]: r for r in records}
    parsed = {}
    duplicate_samples = set()
    for r in records:
        if r["kind"] == "parse_receipt":
            for ref in r["payload"].get("input_revision_ids", []):
                parsed[ref] = r
        elif r["kind"] == "story_receipt":
            refs = [by_id[ref] for ref in r["payload"].get("input_revision_ids", []) if ref in by_id]
            if any(ref["kind"] == "story_revision" and ref["payload"].get("source_id") == r["payload"].get("source_id") for ref in refs):
                duplicate_samples.update(ref["id"] for ref in refs if ref["kind"] == "observation")
    output = []
    for sid in sorted(set(selected)):
        source, profile, review = registry.get(sid), profiles.get(sid), reviews.get(sid)
        policy_hash = digest(source) if source else None
        profile_hash = digest(asdict(profile)) if profile else None
        checks = {"registered_endpoint": bool(source and allowed(source["url"], source)),
                  "profile_present": profile is not None}
        reasons = []
        valid_review = bool(review and source and profile and review["source_policy_hash"] == policy_hash
                            and review["profile_hash"] == profile_hash
                            and epoch_ns(review["reviewed_at"]) <= epoch_ns(now) < epoch_ns(review["expires_at"]))
        checks["current_bound_review"] = valid_review
        for name in sorted(REVIEW_FIELDS):
            expected = "permitted" if name in {"capture", "model_processing"} else "verified"
            checks[name] = bool(valid_review and review["checks"][name]["status"] == expected)
        observations = _transport(source, [r for r in records if r["payload"].get("source_id") == sid]) if source else []
        latest = observations[-1] if observations else None
        health = source_health(source, records, cursors, now=now, window_seconds=evidence_max_age_seconds) if source else None
        checks["latest_collection_ok"] = bool(health and health["last_health_status"] in SUCCESS)
        checks["recent_endpoint_response"] = bool(latest and latest["payload"].get("status") in {200, 304}
            and allowed(latest["payload"]["url"], source)
            and 0 <= _seconds(now, latest["available_at"]) <= evidence_max_age_seconds)
        # 304s, empty feeds, fixture data and local imports cannot establish parser compatibility.
        samples = []
        sample_observations = [r for r in records if _live_observation(r) and r["payload"].get("source_id") == sid
                               and r["payload"].get("source_policy") == policy_hash
                               and source and allowed(r["payload"]["url"], source)]
        for row in sample_observations:
            receipt = parsed.get(row["id"])
            if row["payload"].get("status") != 200 or not receipt or not 0 <= _seconds(now, row["available_at"]) <= evidence_max_age_seconds:
                continue
            revisions = [by_id.get(rid) for rid in receipt["payload"].get("revision_ids", [])]
            if any(r and r["kind"] == "story_revision" for r in revisions) or row["id"] in duplicate_samples:
                samples.append(row)
        checks["parsed_nonempty_sample"] = bool(samples)
        if latest and latest["payload"].get("status") == 200:
            checks["latest_response_parsed"] = latest["id"] in parsed
        else:
            checks["latest_response_parsed"] = bool(latest and latest["payload"].get("status") == 304 and samples)
        if profile and source:
            checks["configured_model_permission"] = source["rights"]["model_processing"] == "permitted" and profile.model_processing == "permitted"
        else:
            checks["configured_model_permission"] = False
        capture_checks = set(checks) - {"model_processing", "configured_model_permission"}
        for name in sorted(capture_checks):
            if not checks[name]:
                reasons.append(name.upper() + "_REQUIRED")
        ready = not reasons
        output.append({"source": sid, "pilot_candidate": sid in candidates,
                       "collector_enabled": bool(source and source["enabled"]),
                       "catalog_enabled": bool(profile and profile.enabled),
                       "endpoint": source["url"] if source else None,
                       "adapter": source["adapter"] if source else None,
                       "poll_seconds": source["poll_seconds"] if source else None,
                       "source_policy_hash": policy_hash, "profile_hash": profile_hash,
                       "status": "READY_FOR_CAPTURE_REVIEW" if ready else "BLOCKED",
                       "capture_checks_passed": ready,
                       "model_checks_passed": ready and checks["model_processing"] and checks["configured_model_permission"],
                       "checks": checks, "blockers": reasons,
                       "model_blockers": [k.upper() + "_REQUIRED" for k in ("model_processing", "configured_model_permission") if not checks[k]],
                       "independence_group": profile.independence_group if profile and profile.independence_verified else None,
                       "declared_timestamp_precision": source.get("timestamp_precision") if source else None,
                       "declared_revision_policy": source.get("revision_policy") if source else None,
                       "review": review, "latest_observation_id": latest["id"] if latest else None,
                       "parser_sample_ids": [r["id"] for r in samples[-3:]]})
    return {"schema": "source-qualification-report-v1", "evaluated_at": now, "storage": storage,
            "evidence_max_age_seconds": evidence_max_age_seconds, "records_hash": digest(records),
            "configuration_changed": False, "sources": output,
            "summary": dict(Counter(r["status"] for r in output)),
            "limitations": ["Archived evidence only; no network probes or automatic activation.",
                            "Reviewed permission and identity are separate from HTTP/parser success.",
                            "Unknown independence remains unknown; it does not block capture.",
                            "Existing collector enabled flags are reported, not newly authorized."]}
