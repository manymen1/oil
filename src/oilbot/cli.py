from __future__ import annotations

import argparse
import json
import shutil
import signal
import sqlite3
import threading
from pathlib import Path

from .clock import seconds, stamp, utc_now
from .config import load_config
from .extract import AnalysisWorker, CodexExtractor
from .incidents import IncidentReducer
from .market import FixtureAdapter, atomic_json, qualify, read_archive, record_adapter
from .replay import export_manifest, load_manifest
from .report import build_report, write_report
from .research import freeze_protocol, research_candidate
from .schema import digest
from .sources import NewsCollector
from .store import Journal, component_lock


def preflight(config):
    return {"mode": "observe", "broker_execution": "disabled", "pilot_ready": True,
            "economic_evaluation": "unavailable", "storage": str(config.root),
            "codex_available": bool(shutil.which(config.extraction.get("binary", "codex"))),
            "sources": [{"id": s["id"], "enabled": s["enabled"], "model_processing": s["rights"]["model_processing"],
                         "endpoint_qualification": s["qualification"]} for s in config.sources],
            "blockers": ["LIVE_MARKET_DATA_UNQUALIFIED", "NO_BROKER_ADAPTER", "SOURCE_MODEL_RIGHTS_REQUIRE_QUALIFICATION"],
            "next": "Capture public source payloads and fixtures; review quality and data budget."}


def status(config):
    output = preflight(config)
    output["journals"] = {}
    for name in ("news", "analysis", "runtime"):
        store = Journal(config.db(name))
        from collections import Counter
        output["journals"][name] = dict(Counter(r["kind"] for r in store.records()))
    output["attempts_by_day"] = Journal(config.db("analysis")).budget()
    records, gaps = read_archive(config.root / "quotes")
    output["market"] = qualify(records, gaps)
    output["heartbeats"] = [r for r in Journal(config.db("runtime")).records("heartbeat")][-3:]
    return output


def record(config, component: str, *, once: bool, fixture: Path | None, stop=None):
    stop = stop or threading.Event()
    runtime = Journal(config.db("runtime"))
    with component_lock(config.root, component):
        prior = runtime.cursor("runtime:" + component)
        current = stamp()
        if prior:
            runtime.append("runtime_gap", {"component": component, "reason": "RESTART_OR_SLEEP",
                                          "previous": prior, "current": current})
        runtime.append("runtime_start", {"component": component, "clock": current, "config_hash": digest(config.raw)})
        if component == "news":
            collector = NewsCollector(Journal(config.db("news")), config.sources)
            work = lambda: collector.poll_once()
        elif component == "analysis":
            news, analysis = Journal(config.db("news")), Journal(config.db("analysis"))
            reducer = IncidentReducer(news, analysis, config.raw["assets"])
            worker = AnalysisWorker(news, analysis, CodexExtractor(config.extraction), config.extraction, reducer,
                                    {s["id"]: s["rights"]["model_processing"] for s in config.sources})
            def work():
                result = worker.run_once()
                for incident in analysis.records("incident_revision"):
                    research_candidate(analysis, incident)
                return result
        else:
            done = False
            def work():
                nonlocal done
                if not fixture or done:
                    return {"status": "WAITING_FOR_QUALIFIED_LIVE_FEED", "economic_evaluation": "unavailable"}
                result = record_adapter(FixtureAdapter(fixture), config.root / "quotes")
                done = True
                return result
        last = current
        try:
            while not stop.is_set():
                current = stamp()
                if seconds(current["utc"], last["utc"]) > 90:
                    runtime.append("runtime_gap", {"component": component, "reason": "HEARTBEAT_DELAY_OR_SLEEP",
                                                  "previous": last, "current": current})
                if current["boot_id"] == last["boot_id"]:
                    drift = seconds(current["utc"], last["utc"]) - (current["monotonic_ns"] - last["monotonic_ns"]) / 1e9
                    if abs(drift) > 5:
                        runtime.append("runtime_gap", {"component": component, "reason": "CLOCK_STEP_OR_SUSPEND",
                                                      "wall_minus_monotonic_seconds": drift, "previous": last, "current": current})
                result = work()
                last = stamp()
                with runtime.transaction() as db:
                    runtime.append("heartbeat", {"component": component, "clock": last, "result": result}, db=db)
                    runtime.set_cursor(db, "runtime:" + component, last)
                if once:
                    return result
                stop.wait(5 if component == "analysis" and result.get("pending") else 30)
        finally:
            runtime.append("runtime_stop", {"component": component, "clock": stamp()})
    return {"stopped": component}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Oil observation and offline research; no broker execution")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "status", "record", "snapshot", "freeze-protocol", "adjudicate", "cluster", "claim", "ingest-news", "demo"):
        child = sub.add_parser(command)
        child.add_argument("--config", default="configs/observe.yaml")
        if command == "record":
            child.add_argument("--component", choices=("news", "market", "analysis"), required=True)
            child.add_argument("--once", action="store_true")
            child.add_argument("--fixture", type=Path)
        if command in {"snapshot", "demo"}:
            child.add_argument("--out", type=Path, required=True)
        if command == "demo":
            child.add_argument("--quotes", type=Path, default=Path("tests/fixtures/market.json"))
        if command == "freeze-protocol":
            child.add_argument("--protocol", type=Path, required=True)
        if command == "adjudicate":
            child.add_argument("--operation", choices=("merge", "split", "link"), required=True)
            child.add_argument("--story", action="append", required=True)
            child.add_argument("--target-incident", required=True)
            child.add_argument("--reason", required=True)
        if command == "cluster":
            child.add_argument("--episode", required=True)
            child.add_argument("--incident", action="append", required=True)
            child.add_argument("--reason", required=True)
        if command in {"claim", "ingest-news"}:
            child.add_argument("--profiles", type=Path, default=Path("configs/source-profiles.json"))
            child.add_argument("--file", type=Path, required=True)
        if command == "ingest-news":
            child.add_argument("--source", required=True)
    child = sub.add_parser("source-profiles")
    child.add_argument("--file", type=Path, default=Path("configs/source-profiles.json"))
    for command in ("qualify-sources", "collection-health"):
        child = sub.add_parser(command, help="Read-only source evidence report; no network calls")
        child.add_argument("--config", default="configs/observe.yaml")
        child.add_argument("--out", type=Path)
        if command == "qualify-sources":
            child.add_argument("--profiles", type=Path, default=Path("configs/source-profiles.json"))
            child.add_argument("--reviews", type=Path, default=Path("configs/source-qualification.json"))
            child.add_argument("--source", action="append")
            child.add_argument("--evidence-max-age-seconds", type=int, default=86400)
        else:
            child.add_argument("--window-seconds", type=int, default=86400)
    child = sub.add_parser("import-databento")
    child.add_argument("--file", type=Path, required=True)
    child.add_argument("--registry", type=Path, required=True)
    child.add_argument("--schema", choices=("mbp-1", "trades"), default="mbp-1")
    child.add_argument("--out", type=Path, required=True)
    child = sub.add_parser("dataset")
    child.add_argument("--manifest", type=Path, required=True)
    child.add_argument("--market", type=Path)
    child.add_argument("--out", type=Path, required=True)
    child.add_argument("--policy", type=Path, required=True)
    child.add_argument("--support", type=Path)
    child = sub.add_parser("event-study")
    child.add_argument("--dataset", type=Path, required=True)
    child.add_argument("--validation-start", required=True)
    child.add_argument("--test-start", required=True)
    child.add_argument("--out", type=Path, required=True)
    child = sub.add_parser("fit-support")
    child.add_argument("--dataset", type=Path, required=True)
    child.add_argument("--validation-start", required=True)
    child.add_argument("--test-start", required=True)
    child.add_argument("--available-at", required=True)
    child.add_argument("--horizon", type=int, required=True)
    child.add_argument("--execution-role", choices=("CL1", "MCL1"), default="MCL1")
    child.add_argument("--out", type=Path, required=True)
    for command in ("replay", "report"):
        child = sub.add_parser(command)
        child.add_argument("--manifest", type=Path, required=True)
        if command == "report":
            child.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {"qualify-sources", "collection-health"}:
            from .source_quality import collection_health, qualify_sources, load_qualification
            from .provenance import load_profiles
            config = load_config(args.config)
            if args.command == "qualify-sources":
                candidates, reviews = load_qualification(args.reviews)
                result = qualify_sources(config, load_profiles(args.profiles), candidates, reviews,
                    source_ids=args.source, evidence_max_age_seconds=args.evidence_max_age_seconds)
            else:
                result = collection_health(config, window_seconds=args.window_seconds)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                with args.out.open("x") as stream:
                    json.dump(result, stream, indent=2, ensure_ascii=False)
                    stream.write("\n")
        elif args.command == "source-profiles":
            from .provenance import load_profiles
            from dataclasses import asdict
            result = [asdict(p) for p in load_profiles(args.file).values()]
        elif args.command == "import-databento":
            from .databento import DatabentoHistoricalAdapter
            adapter = DatabentoHistoricalAdapter(args.file, args.registry, schema=args.schema)
            if args.out.exists():
                raise ValueError("import destination already exists")
            result = record_adapter(adapter, args.out)
            atomic_json(args.out / "provenance.json", adapter.provenance())
        elif args.command == "dataset":
            from .dataset import build_dataset
            from .strategy import StrategyRules
            from .outcomes import OutcomePolicy
            policy = json.loads(args.policy.read_text())
            result = build_dataset(args.manifest, args.out, market_root=args.market,
                rules=StrategyRules(**policy["strategy"]), policy=OutcomePolicy(**policy["execution"]),
                roll_days=policy["roll_days"], supports=json.loads(args.support.read_text()) if args.support else [])
        elif args.command in {"fit-support", "event-study"}:
            from .dataset import read_dataset, fit_support
            from .study import event_study, partition_episodes
            metadata, rows = read_dataset(args.dataset)
            if args.command == "fit-support":
                from .clock import epoch_ns
                if epoch_ns(args.available_at) > epoch_ns(args.validation_start):
                    raise ValueError("support must be available by the validation start")
                development = partition_episodes(rows, [args.validation_start, args.test_start])["development"]
                rows = [{**r, "episode_id": r["original_episode_id"]} for r in development]
            result = (fit_support(rows, available_at=args.available_at, horizon=args.horizon, execution_role=args.execution_role)
                      if args.command == "fit-support" else event_study(rows, [args.validation_start, args.test_start]))
            if args.out.exists():
                raise ValueError("output already exists")
            args.out.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(args.out, result)
        elif args.command in {"replay", "report"}:
            manifest, reader, market = load_manifest(args.manifest)
            if args.command == "replay":
                result = {"verified": True, "records": len(reader.records), "decisions": len(reader.decision_inputs()),
                          "decision_inputs_hash": digest(reader.decision_inputs()), "economic_evaluation": "unavailable"}
            else:
                view = write_report(build_report(manifest, reader, market), args.out)
                result = {"json": str(args.out.resolve()), "html": str(view.resolve()), "economic_evaluation": "unavailable"}
        else:
            config = load_config(args.config)
            if args.command == "preflight":
                result = preflight(config)
            elif args.command == "status":
                result = status(config)
            elif args.command == "snapshot":
                result = {"manifest": str(export_manifest(config, args.out))}
            elif args.command == "demo":
                from .demo import run_demo
                result = run_demo(config, args.out, args.quotes)
            elif args.command == "freeze-protocol":
                result = {"protocol_id": freeze_protocol(Journal(config.db("analysis")), json.loads(args.protocol.read_text()))}
            elif args.command == "adjudicate":
                reducer = IncidentReducer(Journal(config.db("news")), Journal(config.db("analysis")), config.raw["assets"])
                result = {"adjudication_id": reducer.adjudicate(story_ids=args.story, target_incident=args.target_incident,
                                                              operation=args.operation, reason=args.reason)}
            elif args.command == "cluster":
                from .clusters import assign_episode
                result = {"assignment_id": assign_episode(Journal(config.db("analysis")), episode_id=args.episode,
                          incident_ids=args.incident, reason=args.reason)}
            elif args.command == "claim":
                from .provenance import load_profiles, record_claim
                result = {"claim_id": record_claim(Journal(config.db("news")), Journal(config.db("analysis")),
                          load_profiles(args.profiles), json.loads(args.file.read_text()))}
            elif args.command == "ingest-news":
                from dataclasses import asdict
                from .provenance import load_profiles, StructuredNewsAdapter
                from .sources import allowed
                source = next((s for s in config.sources if s["id"] == args.source), None)
                if source is None:
                    raise ValueError("source must be registered in observation config")
                profile = load_profiles(args.profiles)[args.source]
                source = {**source, "profile": asdict(profile)}
                body = args.file.read_bytes()
                if len(body) > 8 * 1024 * 1024:
                    raise ValueError("news batch exceeds 8 MiB")
                news = Journal(config.db("news"))
                now = stamp()
                oid = news.capture(source, {"body": body, "url": source["url"], "status": 200,
                    "content_type": "application/json", "headers": {}, "started": now, "first_byte": now,
                    "received": now, "delivery": "local_import", "parser": "structured", "source_profile": asdict(profile)})
                items = StructuredNewsAdapter().parse(body, source["url"], "application/json")
                if any(not allowed(item.url, source) for item in items):
                    raise ValueError("news item publisher URL outside registered source hosts")
                result = {"observation_id": oid, "revision_ids": news.accept_items(source, oid, items,
                    news.cursor("source:" + source["id"], {}))}
            else:
                stop = threading.Event()
                signal.signal(signal.SIGTERM, lambda *_: stop.set())
                signal.signal(signal.SIGINT, lambda *_: stop.set())
                result = record(config, args.component, once=args.once, fixture=args.fixture, stop=stop)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc), "mode": "observe"}))
        return 2
