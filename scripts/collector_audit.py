"""Offline audit export. Opens source journals read-only; never polls or classifies into them."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

from oilbot.clock import utc_now
from oilbot.config import load_config
from oilbot.forward import VERSION, classify
from oilbot.forward_quality import forward_quality, storage_sizes
from oilbot.replay import ReplayReader, load_manifest
from oilbot.schema import digest


def save(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_journal(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        records = [{**dict(row), "payload": json.loads(row["payload"])}
                   for row in db.execute("SELECT * FROM records ORDER BY seq")]
        work = [dict(row) for row in db.execute("SELECT * FROM parse_work ORDER BY observation_seq")]
        return records, work


def coverage(records, assets, sample_size):
    """Revision-grain sample; historical processing and current offline probes stay separate."""
    stories = {r["id"]: r for r in records if r["kind"] == "story_revision"}
    processing, events = defaultdict(list), defaultdict(list)
    for row in records:
        if row["kind"] == "forward_processing":
            for rid in row["payload"].get("input_revision_ids", []):
                if rid in stories:
                    processing[rid].append(row)
        elif row["kind"] == "fast_event":
            events[row["payload"]["story_revision_id"]].append(row["payload"]["event_type"])
    strata = defaultdict(list)
    for rid, row in stories.items():
        history = sorted(processing[rid], key=lambda r: (r["available_at"], r["id"]))
        exclusion = history[-1]["payload"].get("exclusion") if history else None
        disposition = ("MATCHED" if events[rid] else "EXCLUDED" if exclusion else
                       "UNMATCHED" if history else "UNPROCESSED")
        strata[(row["payload"]["source_id"], disposition)].append({
            "story_revision_id": rid, "story_id": row["payload"]["story_id"],
            "source": row["payload"]["source_id"], "historical_disposition": disposition,
            "historical_event_types": events[rid], "exclusion": exclusion,
            "processing_versions": sorted({r["payload"].get("transform", "unknown") for r in history}),
            "title": row["payload"]["title"], "url": row["payload"]["url"],
            "received_at": row["payload"].get("local_received_at"),
            "published_at": row["payload"].get("published_at"),
            "offline_headline_probe_version": VERSION,
            "offline_headline_probe_types": [e["event_type"] for e in classify(row["payload"], assets)],
            "human_review": {"status": "UNREVIEWED", "event_types": None, "oil_relevance": None,
                             "claim_origin": None, "reviewer": None, "reviewed_at": None, "notes": None}})
    sample, denominators = [], []
    for (source, disposition), values in sorted(strata.items()):
        selected = sorted(values, key=lambda r: digest(["audit-sample-v1", r["story_revision_id"]]))[:sample_size]
        sample.extend(selected)
        denominators.append({"source": source, "historical_disposition": disposition,
                             "population_revisions": len(values), "sample_revisions": len(selected)})
    return {"grain": "story_revision", "selection": "lowest SHA256(audit-sample-v1, revision ID) per source/disposition",
            "strata": denominators, "sample": sample, "human_reviewed": 0,
            "precision": None, "recall": None,
            "limits": ["Offline regex probes are not human labels, historical events or first-party assessments.",
                       "No matched examples can be supplied for empty matched strata.",
                       "Stratified samples require population weights; exclusions are not eligible classifier misses.",
                       "Related revisions are not independent episodes; no accuracy claim before adjudication."]}


def build_pack(config, out, *, sample_size=3, window_seconds=604800):
    if config.raw.get("pipeline") != "forward":
        raise ValueError("forward configuration required")
    if type(sample_size) is not int or sample_size < 1 or type(window_seconds) is not int or window_seconds < 1:
        raise ValueError("positive sample size and window required")
    # This bounded exporter must not silently omit market data or invent missing journals.
    names = ["analysis", "forward", "news", "runtime"]
    if any(not config.db(name).is_file() for name in names):
        raise ValueError("all four existing journals required; missing journals are not empty datasets")
    if (config.root / "quotes").exists() and any((config.root / "quotes").rglob("*")):
        raise ValueError("news-only audit cannot omit an existing quote archive")
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    snapshot = out / "snapshot"
    snapshot.mkdir()
    started = utc_now()
    original_sizes = storage_sizes(config)
    files, records, checks, work = {}, [], {}, []
    for name in names:
        target = snapshot / (name + ".sqlite3")
        with closing(sqlite3.connect(config.db(name).resolve().as_uri() + "?mode=ro", uri=True)) as src:
            src.execute("PRAGMA query_only=ON")
            if src.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("unsupported journal schema")
            with closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
                integrity = [r[0] for r in dst.execute("PRAGMA integrity_check")]
                if integrity != ["ok"]:
                    raise ValueError("backup integrity failed: " + name)
        rows, pending = read_journal(target)
        records.extend(rows)
        if name == "news":
            work = pending
        checks[name] = {"integrity_check": integrity, "records": len(rows),
                       "max_seq": max((r["seq"] for r in rows), default=0),
                       "kinds": dict(Counter(r["kind"] for r in rows))}
        files[target.name] = checksum(target)
    reader = ReplayReader(records)  # Fails on missing/future dependencies across snapshots.
    save(snapshot / "market.json", {"records": [], "gaps": []})
    files["market.json"] = checksum(snapshot / "market.json")
    repo = Path(__file__).resolve().parents[1]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).strip())
    manifest = {"schema": "oil-manifest-v1", "created_at": utc_now(), "code_commit": commit,
                "dirty": dirty, "config": config.raw, "config_hash": digest(config.raw), "files": files,
                "record_count": len(records), "records_hash": digest(reader.records),
                "decision_inputs_hash": digest(reader.decision_inputs()), "dataset_role": "forward_news_only",
                "economic_evaluation": "unavailable"}
    save(snapshot / "manifest.json", manifest)
    load_manifest(snapshot / "manifest.json")
    restored = out / "restore-check"
    shutil.copytree(snapshot, restored)
    _, restored_reader, _ = load_manifest(restored / "manifest.json")
    now = utc_now()
    quality = forward_quality(replace(config, root=snapshot), now=now, window_seconds=window_seconds)
    quality["storage"]["measurement_basis"] = "backup files, not live filesystem growth"
    save(out / "quality.json", quality)
    sampled = coverage(reader.records, config.raw["assets"], sample_size)
    save(out / "coverage-sample.json", sampled)
    observations = {r["id"] for r in records if r["kind"] == "observation"}
    parsed = {rid for r in records if r["kind"] == "parse_receipt"
              for rid in r["payload"].get("input_revision_ids", [])}
    indexed = {r["observation_id"] for r in work}
    recovery = {"observations": len(observations), "indexed_work": len(work),
                "observations_without_index": sorted(observations - indexed),
                "observations_without_parse_receipt": sorted(observations - parsed),
                "orphan_work_ids": sorted(indexed - observations),
                "indexed_states": dict(Counter(r["state"] for r in work)),
                "unprocessed_story_revisions": sum(s["population_revisions"] for s in sampled["strata"]
                                                   if s["historical_disposition"] == "UNPROCESSED")}
    heartbeat = [r for r in reader.records if r["kind"] == "heartbeat"]
    audit = {"schema": "collector-audit-v1", "started_at": started, "completed_at": now,
             "source_root": str(config.root), "source_config": str(config.path),
             "code_commit": commit, "code_dirty": dirty, "journals": checks, "recovery": recovery,
             "last_heartbeat": heartbeat[-1]["available_at"] if heartbeat else None,
             "source_storage_sample": original_sizes,
             "restore": {"verified": True, "records": len(restored_reader.records),
                         "records_hash": digest(restored_reader.records), "location": "restore-check",
                         "scope": "same-host separate-directory checksum and causal replay; not disaster recovery"},
             "limitations": ["No network requests, collection start, circuit reset, migration or source-journal writes.",
                 "Sequential database backups are not one atomic cross-journal instant; replay dependency checks passed.",
                 "Recovery reconciliation is a static check, not a crash/restart fault-injection test.",
                 "Response intervals are receipt gaps, not request-start poll cadence or measured downtime.",
                 "Human review, reviewer workload, publication completeness and multi-day soak remain unverified.",
                 "Missing runtime storage samples mean storage growth is unknown, not zero."]}
    save(out / "audit.json", audit)
    shutil.copy2(__file__, out / "audit_script.py")
    save(out / "checksums.json", {str(p.relative_to(out)): checksum(p)
                                 for p in sorted(out.rglob("*")) if p.is_file()})
    return audit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/forward.yaml")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=3)
    parser.add_argument("--window-seconds", type=int, default=604800)
    args = parser.parse_args()
    print(json.dumps(build_pack(load_config(args.config), args.out,
                              sample_size=args.sample_size, window_seconds=args.window_seconds), indent=2))
