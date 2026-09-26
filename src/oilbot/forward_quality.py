"""Read-only forward dataset diagnostics. Never decodes raw response bodies."""
from collections import Counter
from contextlib import ExitStack, closing
from datetime import timedelta
import json
import sqlite3

from .clock import instant, seconds, utc_now
from .schema import digest
from .source_quality import source_health, _summary

KINDS = {
    "news": ("observation", "story_revision", "story_receipt", "parse_receipt", "commit_receipt", "source_health", "late_story_parse"),
    "forward": ("forward_start", "fast_event", "forward_processing", "candidate_episode_link", "linker_processing", "forward_evidence_transition", "forward_link_review"),
    "runtime": ("runtime_gap", "clock_health", "storage_sample"),
}


def storage_sizes(config):
    """Known journal/WAL/log files only; no recursive traversal or checkpoint."""
    files = {}
    for name in ("news", "forward", "runtime", "analysis", "linker"):
        for suffix in (".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".log"):
            path = config.root / (name + suffix)
            try:
                files[path.name] = path.stat().st_size
            except FileNotFoundError:
                pass
    return {"files_bytes": files, "total_bytes": sum(files.values())}


def forward_quality(config, *, now=None, window_seconds=86400):
    if config.raw.get("pipeline") != "forward":
        raise ValueError("forward-quality requires a forward configuration")
    if type(window_seconds) is not int or window_seconds <= 0:
        raise ValueError("positive integer report window required")
    now = instant(now or utc_now()).isoformat(timespec="microseconds")
    start = (instant(now) - timedelta(seconds=window_seconds)).isoformat(timespec="microseconds")
    rows, watermarks, cursors, future_records = {}, {}, {}, {}
    recovery = {}
    # Pin each database before reading payloads. These are individually consistent
    # WAL snapshots, not an atomic cross-database snapshot; expose their fences.
    with ExitStack() as stack:
        databases = {}
        for name in KINDS:
            path = config.db(name)
            rows[name] = []
            watermarks[name] = None
            if not path.exists():
                continue
            db = stack.enter_context(closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)))
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("unsupported journal schema")
            watermarks[name] = db.execute("SELECT COALESCE(MAX(seq),0) FROM records").fetchone()[0]
            future_records[name] = db.execute("SELECT COUNT(*) FROM records WHERE available_at>?", (now,)).fetchone()[0]
            databases[name] = db
        for name, db in databases.items():
            placeholders = ",".join("?" for _ in KINDS[name])
            # SQL removes bulky content before it crosses into Python. This is an
            # offline metadata scan, never a collector operation.
            query = f"""SELECT seq,id,kind,available_at,
                json_remove(payload,'$.body_b64','$.text','$.title','$.headline',
                    '$.source_profile','$.links','$.literal_evidence','$.evidence') AS payload
                FROM records WHERE kind IN ({placeholders}) AND available_at<=? ORDER BY seq"""
            rows[name] = [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute(query, (*KINDS[name], now))]
            if name == "news":
                cursors = {r[0]: json.loads(r[1]) for r in db.execute("SELECT key,value FROM cursors WHERE key LIKE 'source:%' OR key='recovery:index'")}
                recovery = dict(db.execute("SELECT state,COUNT(*) FROM parse_work GROUP BY state"))
    recent = lambda row: start < row["available_at"] <= now
    news, forward, runtime = rows["news"], rows["forward"], rows["runtime"]
    stories = [r for r in news if r["kind"] == "story_revision"]
    window_stories = [r for r in stories if recent(r)]
    events = [r for r in forward if r["kind"] == "fast_event"]
    window_events = [r for r in events if recent(r)]
    processing = [r for r in forward if r["kind"] == "forward_processing" and recent(r)]
    links = [r for r in forward if r["kind"] == "candidate_episode_link" and recent(r)]
    linker = [r for r in forward if r["kind"] == "linker_processing" and recent(r)]
    duplicates = sum(r["kind"] == "story_receipt" and recent(r) for r in news)
    revisions = sum(bool(r["payload"].get("supersedes_id")) for r in window_stories)
    observations = {r["id"]: r for r in news if r["kind"] == "observation"}

    def lag_summary(records, after, before):
        values, missing, negative, invalid = [], 0, 0, 0
        for row in records:
            a, b = after(row), before(row)
            if a is None or b is None:
                missing += 1
                continue
            try:
                value = seconds(a, b)
            except (ValueError, TypeError):
                invalid += 1
                continue
            if value < 0:
                negative += 1
            else:
                values.append(value)
        return {**_summary(values), "missing": missing, "negative": negative, "invalid": invalid, "unit": "seconds"}

    def ratio(numerator, denominator):
        return {"numerator": numerator, "denominator": denominator,
                "fraction": numerator / denominator if denominator else None}

    def daily(records):
        return dict(sorted(Counter(r["available_at"][:10] for r in records).items()))

    health = [source_health(s, news, cursors, now=now, window_seconds=window_seconds) for s in config.sources]
    sampled = [r for r in runtime if r["kind"] == "storage_sample" and recent(r)]
    growth = None
    if len(sampled) >= 2:
        first, last = sampled[0], sampled[-1]
        elapsed = seconds(last["available_at"], first["available_at"])
        growth = {"first_sample_id": first["id"], "last_sample_id": last["id"], "elapsed_seconds": elapsed,
                  "net_bytes": last["payload"]["total_bytes"] - first["payload"]["total_bytes"]}
        growth["net_bytes_per_day"] = growth["net_bytes"] * 86400 / elapsed if elapsed > 0 else None
    excluded = Counter(r["payload"].get("exclusion") for r in processing if r["payload"].get("exclusion"))
    gaps = [r for r in runtime if r["kind"] == "runtime_gap" and recent(r)]
    clocks = [r for r in runtime if r["kind"] == "clock_health" and recent(r)]
    latest_clock = {}
    for row in clocks:
        latest_clock[row["payload"]["component"]] = {"id": row["id"], **row["payload"]}
    live_stories = [r for r in window_stories if any(
        ref in observations and observations[ref]["payload"].get("delivery") == "http"
        and not observations[ref]["payload"].get("synthetic") for ref in r["payload"]["input_revision_ids"])]
    commits = [r for r in news if r["kind"] == "commit_receipt" and recent(r)]
    commit_ms = [r["payload"].get("receive_to_commit_ms") for r in commits]
    return {"schema": "forward-quality-v1", "evaluated_at": now, "window_start_exclusive": start,
        "window_seconds": window_seconds, "config_hash": digest(config.raw), "journal_max_seq": watermarks,
        "current_cursors_hash": digest(cursors), "future_dated_records_excluded": future_records,
        "totals": {"stories_captured": len({r["payload"]["story_id"] for r in stories}),
                   "story_revisions": len(stories), "fast_events": len(events)},
        "window": {"story_revisions": len(window_stories), "new_stories_by_utc_day": daily([r for r in window_stories if not r["payload"].get("supersedes_id")]),
            "fast_events_by_utc_day": daily(window_events), "events_by_type": dict(Counter(r["payload"]["event_type"] for r in window_events)),
            "revision_rate": ratio(revisions, len(window_stories)),
            "duplicate_receipt_rate": ratio(duplicates, duplicates + len(window_stories)),
            "candidate_link_rate": ratio(sum(any(s["emitted"] > 0 for s in r["payload"]["searches"]) for r in linker), len(linker)),
            "candidate_links": len(links), "linker_evaluations": len(linker),
            "linker_truncated_evaluations": sum(any(s["search_truncated"] or s["links_truncated"] for s in r["payload"]["searches"]) for r in linker),
            "exclusions": dict(excluded), "excluded_initial_snapshots": excluded["INITIAL_SNAPSHOT"],
            "late_story_parses": sum(r["kind"] == "late_story_parse" and recent(r) for r in news),
            "evidence_transitions": dict(Counter(r["payload"]["state"] for r in forward if r["kind"] == "forward_evidence_transition" and recent(r))),
            "publication_to_receipt": lag_summary(live_stories, lambda r: r["payload"].get("local_received_at"), lambda r: r["payload"].get("published_at")),
            "receipt_to_classification": lag_summary(window_events, lambda r: r["available_at"], lambda r: r["payload"].get("received_at")),
            "receipt_to_durable_commit_ms": {**_summary([v for v in commit_ms if v is not None and v >= 0]),
                "missing": sum(v is None for v in commit_ms), "negative": sum(v is not None and v < 0 for v in commit_ms)},
            "runtime_gaps": dict(Counter(r["payload"]["reason"] for r in gaps))},
        "sources": health, "source_states": dict(Counter(r["state"] for r in health)),
        "recovery": {"indexed_work_by_state": recovery, "legacy_index": cursors.get("recovery:index")},
        "clock": {"latest_by_component": latest_clock, "states": dict(Counter(r["payload"]["state"] for r in clocks)), "absolute_accuracy": "NOT_MEASURED"},
        "storage": {**storage_sizes(config), "window_growth": growth},
        "limitations": ["Metadata scan is offline and grows with history; raw bodies are not decoded.",
            "Journal snapshots are individually consistent, not atomic across databases; sequence fences identify inputs.",
            "Window and UTC days use record availability, not publisher time. Missing days are not zero-activity evidence.",
            "Captured totals include baseline and excluded stories; event counts are hypotheses, not unique incidents.",
            "Candidate rate counts event-policy evaluations; relinking under another policy adds evaluations.",
            "Duplicate rate measures unchanged receipts, not independent publishers or semantic duplication.",
            "Negative/missing delays are reported separately, not silently clamped. Clock absolute accuracy is unknown.",
            "Runtime gaps are observations, not measured downtime. No gaps does not prove continuous operation.",
            "Storage sizes are current filesystem samples; WAL checkpointing can make net growth negative."]}
