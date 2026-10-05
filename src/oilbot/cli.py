from __future__ import annotations

import argparse
from contextlib import closing
import json
import shutil
import signal
import sqlite3
import threading
from pathlib import Path

from .clock import instant, seconds, stamp, utc_now
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
    forward = config.raw.get("pipeline") == "forward"
    now = instant(utc_now())
    sources = {s["id"]: s for s in config.sources}
    return {"mode": "observe", "broker_execution": "disabled", "pilot_ready": True,
            "economic_evaluation": "unavailable", "storage": str(config.root),
            "codex_available": bool(shutil.which(config.extraction.get("binary", "codex"))),
            "sources": [{"id": s["id"], "enabled": s["enabled"], "model_processing": s["rights"]["model_processing"],
                         "endpoint_qualification": s["qualification"]} for s in config.sources],
            "blockers": ([] if forward else
                         ["LIVE_MARKET_DATA_UNQUALIFIED", "NO_BROKER_ADAPTER", "SOURCE_MODEL_RIGHTS_REQUIRE_QUALIFICATION"]),
            "deferred": ["CI_RUN_UNVERIFIED", "LIVE_MARKET_DATA", "STRATEGY"] if forward else [],
            "pipeline": config.raw.get("pipeline", "reviewed"),
            "forward_recorder_ready": forward,
            "first_party_reviews": [{"source": r["source_id"], "status": r["status"], "scope": r["scope"],
                "expires_at": r["expires_at"], "registration_matches": digest(sources[r["source_id"]]) == r["source_policy_hash"],
                "time_valid": instant(r["reviewed_at"]) <= now < instant(r["expires_at"])} for r in config.raw.get("first_party_reviews", [])],
            "planned_market_provider": config.raw["market"].get("planned_provider"),
            "live_market_ready": False,
            "next": "Accumulate auditable forward news and review candidate incident links."}


def status(config):
    output = preflight(config)
    output["journals"] = {}
    output["missing_journals"] = []
    output["attempts_by_day"] = {}
    output["recovery"] = {"indexed_work_by_state": {}, "legacy_index": None}
    output["heartbeats"] = []
    for name in ("news", "analysis", "runtime", "forward"):
        path = config.db(name)
        if not path.exists():
            output["journals"][name] = {}
            output["missing_journals"].append(name)
            continue
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            output["journals"][name] = dict(db.execute("SELECT kind,COUNT(*) FROM records GROUP BY kind"))
            if name == "analysis":
                output["attempts_by_day"] = dict(db.execute("SELECT day,attempts FROM budget ORDER BY day"))
            elif name == "news":
                output["recovery"]["indexed_work_by_state"] = dict(db.execute(
                    "SELECT state,COUNT(*) FROM parse_work GROUP BY state"))
                row = db.execute("SELECT value FROM cursors WHERE key='recovery:index'").fetchone()
                output["recovery"]["legacy_index"] = json.loads(row[0]) if row else None
            elif name == "runtime":
                output["heartbeats"] = [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute(
                    "SELECT * FROM records WHERE kind='heartbeat' ORDER BY seq DESC LIMIT 3")][::-1]
    if config.raw.get("pipeline") == "forward":
        output["market"] = {"status": "DISABLED_NEWS_ONLY"}
    else:
        records, gaps = read_archive(config.root / "quotes")
        output["market"] = qualify(records, gaps)
    return output


def record(config, component: str, *, once: bool, fixture: Path | None, stop=None):
    if config.raw.get("pipeline") == "forward" and fixture is not None:
        raise ValueError("fixtures are not allowed in forward collection; use the isolated offline demo")
    stop = stop or threading.Event()
    runtime = Journal(config.db("runtime"))
    with component_lock(config.root, component):
        prior = runtime.cursor("runtime:" + component)
        current = stamp()
        if prior:
            runtime.append("runtime_gap", {"component": component, "reason": "RESTART_OR_SLEEP",
                                          "previous": prior, "current": current})
        runtime.append("runtime_start", {"component": component, "clock": current, "config_hash": digest(config.raw)})
        from .operations import clock_sample, sample_storage
        runtime.append("clock_health", clock_sample(component, prior, current))
        if component == "news":
            collector = NewsCollector(Journal(config.db("news")), config.sources)
            work = lambda: collector.poll_once()
        elif component == "forward":
            from .forward import ForwardRecorder
            worker = ForwardRecorder(Journal(config.db("news")), Journal(config.db("forward")), config.raw["assets"],
                asset_registry_version=config.raw["asset_registry_version"], source_registry_version=config.raw["registry_version"],
                sources=config.sources, first_party_reviews=config.raw.get("first_party_reviews", []))
            work = worker.run_once
        elif component == "linker":
            from .linking import LinkerWorker
            worker = LinkerWorker(Journal(config.db("news")), Journal(config.db("forward")))
            work = worker.run_once
        elif component == "operational":
            if config.raw.get("pipeline") != "forward" or not config.raw.get("operational_queue"):
                raise ValueError("operational queue must be enabled in a forward configuration")
            from .operational import OperationalQueue
            worker = OperationalQueue(Journal(config.db("news")), Journal(config.db("forward")), config.raw["assets"], config.sources)
            work = worker.run_once
        elif component == "analysis":
            if config.raw.get("pipeline") == "forward":
                raise ValueError("forward pipeline uses --component forward; model analysis is disabled")
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
                if config.raw.get("pipeline") == "forward":
                    return {"status": "DISABLED_NEWS_ONLY", "economic_evaluation": "deferred"}
                if not fixture or done:
                    return {"status": "WAITING_FOR_QUALIFIED_LIVE_FEED", "economic_evaluation": "unavailable"}
                result = record_adapter(FixtureAdapter(fixture), config.root / "quotes")
                done = True
                return result
        last = current
        last_clock = current
        try:
            while not stop.is_set():
                current = stamp()
                if seconds(current["utc"], last["utc"]) > 90:
                    runtime.append("runtime_gap", {"component": component, "reason": "HEARTBEAT_DELAY_OR_SLEEP",
                                                  "previous": last, "current": current})
                if current["boot_id"] == last["boot_id"]:
                    drift = seconds(current["utc"], last["utc"]) - (current["monotonic_ns"] - last["monotonic_ns"]) / 1e9
                    if abs(drift) > 5:
                        runtime.append("clock_health", clock_sample(component, last, current))
                        runtime.append("runtime_gap", {"component": component, "reason": "CLOCK_STEP_OR_SUSPEND",
                                                      "wall_minus_monotonic_seconds": drift, "previous": last, "current": current})
                result = work()
                finished = stamp()
                # Compare heartbeat-to-heartbeat, including time inside work().
                # Comparing only before work misses long calls and clock steps.
                if seconds(finished["utc"], last["utc"]) > 90 and seconds(current["utc"], last["utc"]) <= 90:
                    runtime.append("runtime_gap", {"component": component, "reason": "WORK_OR_HEARTBEAT_DELAY",
                        "previous": last, "current": finished})
                clock = clock_sample(component, last, finished)
                if clock["state"] != "NO_STEP_DETECTED" or finished["monotonic_ns"] - last_clock["monotonic_ns"] >= 60 * 10**9:
                    runtime.append("clock_health", clock)
                    last_clock = finished
                if component == "news":
                    sample_storage(runtime, config)
                last = finished
                with runtime.transaction() as db:
                    runtime.append("heartbeat", {"component": component, "clock": last, "result": result}, db=db)
                    runtime.set_cursor(db, "runtime:" + component, last)
                if once:
                    return result
                stop.wait(1 if component in {"forward", "linker", "operational"} else (5 if component == "analysis" and result.get("pending") else 30))
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
            child.add_argument("--component", choices=("news", "market", "analysis", "forward", "linker", "operational"), required=True)
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
    child = sub.add_parser("forward-candidates", help="Read-only, paginated candidate incident links; never merges")
    child.add_argument("--config", default="configs/forward.yaml")
    child.add_argument("--after-seq", type=int, default=0)
    child.add_argument("--limit", type=int, default=100)
    child = sub.add_parser("operational-queue", help="Read-only captured-text assessment queue; no model or confirmation")
    child.add_argument("--config", default="configs/forward.yaml")
    child.add_argument("--after-seq", type=int, default=0)
    child.add_argument("--limit", type=int, default=100)
    child.add_argument("--include-baseline", action="store_true")
    child = sub.add_parser("review-forward-link", help="Append a human decision; never edits evidence")
    child.add_argument("--config", default="configs/forward.yaml")
    child.add_argument("--candidate", required=True)
    child.add_argument("--decision", choices=("SAME_EVENT", "SAME_EPISODE", "SYNDICATED_REPORT", "UNRELATED", "UNCERTAIN"), required=True)
    child.add_argument("--reason", required=True)
    child.add_argument("--reviewer", required=True)
    child.add_argument("--supersedes-review")
    child.add_argument("--review-id")
    child = sub.add_parser("propose-forward-link", help="Propose a relation between captured events; separate review required")
    child.add_argument("--config", default="configs/forward.yaml")
    child.add_argument("--left", required=True)
    child.add_argument("--right", required=True)
    child.add_argument("--relation", choices=("SAME_EVENT_CANDIDATE", "SAME_EPISODE_CANDIDATE", "SYNDICATED_REPORT_CANDIDATE"), required=True)
    child.add_argument("--reason", required=True)
    child.add_argument("--reviewer", required=True)
    child.add_argument("--proposal-id")
    child = sub.add_parser("forward-episodes", help="Read-only as-of mapping from human reviews")
    child.add_argument("--config", default="configs/forward.yaml")
    child.add_argument("--through")
    child = sub.add_parser("forward-quality", help="Read-only forward dataset and operational diagnostics")
    child.add_argument("--config", default="configs/forward.yaml")
    child.add_argument("--window-seconds", type=int, default=86400)
    child = sub.add_parser("reset-source-circuit", help="Record operator review and allow a source retry; no fetch")
    child.add_argument("--config", default="configs/forward.yaml")
    child.add_argument("--source", required=True)
    child.add_argument("--reason", required=True)
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
    child = sub.add_parser("paper-replay", help="Local paper execution of explicit intents; no broker connection")
    child.add_argument("--manifest", type=Path, required=True)
    child.add_argument("--instrument", required=True)
    child.add_argument("--limits", type=Path, required=True)
    child.add_argument("--out", type=Path, required=True)
    child = sub.add_parser("paper-scenario", help="Run or resume an explicit synthetic paper scenario, never real orders")
    child.add_argument("--file", type=Path, required=True)
    child.add_argument("--out", type=Path, required=True)
    child.add_argument("--resume", action="store_true")
    for command in ("ibkr-preflight", "ibkr-capture"):
        child = sub.add_parser(command, help="IBKR paper read-only diagnostics; never sends orders")
        child.add_argument("--config", type=Path, default=Path("configs/ibkr-paper.json"))
        if command == "ibkr-capture":
            child.add_argument("--connect", action="store_true", required=True, help="Explicitly open the configured paper API socket")
            child.add_argument("--out", type=Path, required=True)
    child = sub.add_parser("dataset")
    child.add_argument("--manifest", type=Path, required=True)
    child.add_argument("--market", type=Path)
    child.add_argument("--out", type=Path, required=True)
    child.add_argument("--policy", type=Path, required=True)
    child.add_argument("--support", type=Path)
    for command in ("macro-collect", "macro-import", "macro-recover"):
        child = sub.add_parser(command, help="Isolated EIA/CFTC numeric observations; no model or orders")
        child.add_argument("--out", type=Path, required=True)
        if command != "macro-recover":
            child.add_argument("--source", choices=("eia", "cftc"), required=True)
        if command == "macro-import":
            child.add_argument("--file", type=Path, required=True)
        if command == "macro-recover":
            child.add_argument("--delivery", choices=("http", "local_import"), default="http")
    child = sub.add_parser("macro-research", help="Point-in-time inventory hypothesis and price-only baseline; no orders")
    child.add_argument("--journal", type=Path, required=True)
    child.add_argument("--at", required=True)
    child.add_argument("--manifest", type=Path)
    child.add_argument("--rules", type=Path, default=Path("configs/inventory-research.json"))
    child.add_argument("--out", type=Path)
    child = sub.add_parser("macro-dataset", help="Grouped inventory research and hypothetical MCL outcomes; no orders")
    child.add_argument("--journal", type=Path, required=True)
    child.add_argument("--through", required=True)
    child.add_argument("--manifest", type=Path)
    child.add_argument("--rules", type=Path, default=Path("configs/inventory-research.json"))
    child.add_argument("--costs", type=Path, help="OutcomePolicy JSON; defaults are illustrative, not broker-verified")
    child.add_argument("--out", type=Path, required=True)
    child = sub.add_parser("macro-evaluate", help="Chronological release-level strategy screen; no promotion or orders")
    child.add_argument("--dataset", type=Path, required=True)
    child.add_argument("--holdout-start", required=True)
    child.add_argument("--horizon", type=int, default=300)
    child.add_argument("--strategy", default="inventory_continuation_v2")
    child.add_argument("--extra-round-trip-cost", default="2.00")
    child.add_argument("--out", type=Path)
    for command in ("macro-worker", "macro-health"):
        child = sub.add_parser(command, help="Release-aware macro collection or read-only health; no trading")
        child.add_argument("--root", type=Path, required=True)
        child.add_argument("--state", type=Path, required=True)
        child.add_argument("--config", type=Path, default=Path("configs/macro-scheduler.json"))
        child.add_argument("--contact-file", type=Path)
        if command == "macro-worker":
            mode = child.add_mutually_exclusive_group(required=True)
            mode.add_argument("--once", action="store_true")
            mode.add_argument("--serve", action="store_true")
            mode.add_argument("--run-seconds", type=int)
    for command in ("eia-detail-collect", "eia-detail-import", "eia-detail-recover"):
        child = sub.add_parser(command, help="Isolated Cushing/refinery observations; no strategy or orders")
        child.add_argument("--out", type=Path, required=True)
        if command == "eia-detail-collect":
            child.add_argument("--contact-file", type=Path)
        if command == "eia-detail-import":
            child.add_argument("--file", type=Path, required=True)
        if command == "eia-detail-recover":
            child.add_argument("--delivery", choices=("http", "local_import"), default="http")
    child = sub.add_parser("eia-detail-health", help="Read-only freshness and release checks; no raw-body scan")
    child.add_argument("--root", type=Path, required=True)
    child.add_argument("--config", type=Path, default=Path("configs/macro-scheduler.json"))
    child = sub.add_parser("eia-detail-report", help="Read-only as-of Cushing/refinery evidence")
    child.add_argument("--journal", type=Path, required=True)
    child.add_argument("--at", required=True)
    child.add_argument("--out", type=Path)
    child = sub.add_parser("bsee-report", help="Read-only shut-in estimates; no inferred restarts")
    child.add_argument("--config", default="configs/bsee-report.yaml")
    child.add_argument("--at", required=True)
    child.add_argument("--out", type=Path)
    child = sub.add_parser("weather-report", help="As-of NHC regional research screening; no outage or price inference")
    child.add_argument("--config", default="configs/weather-forward.yaml")
    child.add_argument("--regions", type=Path, default=Path("configs/oil-weather-regions.json"))
    child.add_argument("--at", required=True)
    child.add_argument("--out", type=Path)
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
        if args.command in {"macro-collect", "macro-import", "macro-recover"}:
            import os
            from .macro import MacroRecorder, MAX_BYTES
            delivery = "local_import" if args.command == "macro-import" else getattr(args, "delivery", "http")
            worker = MacroRecorder(args.out, delivery=delivery)
            if args.command == "macro-collect":
                result = worker.collect(args.source, contact=os.environ.get("OILBOT_SOURCE_CONTACT"))
            else:
                with component_lock(worker.root, "macro"):
                    if args.command == "macro-recover":
                        result = {"recovered": worker.recover()}
                    else:
                        if args.file.stat().st_size > MAX_BYTES:
                            raise ValueError("macro import exceeds size limit")
                        result = worker.ingest(args.source, args.file.read_bytes())
            print(json.dumps(result, indent=2))
            return 2 if result.get("status") in {"FAILED", "BLOCKED"} else 0
        elif args.command in {"macro-worker", "macro-health"}:
            import os
            from .macro_scheduler import MacroWorker, health, load_contact, load_schedule
            policy = load_schedule(args.config)
            contact = load_contact(args.contact_file) if args.contact_file else os.environ.get("OILBOT_SOURCE_CONTACT")
            if args.command == "macro-health":
                result = health(args.root, args.state, policy, contact_configured=bool(contact))
                print(json.dumps(result, indent=2))
                return 0 if result["status"] == "HEALTHY" else 2
            worker = MacroWorker(args.root, args.state, policy, contact=contact)
            if args.once:
                result = worker.run_once()
                print(json.dumps(result, indent=2))
                return 0 if result["health"]["status"] == "HEALTHY" else 2
            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            worker.run(stop, seconds=args.run_seconds, emit=lambda report: print(json.dumps(report), flush=True))
            result = {"status": "STOPPED", "trade_authorized": False}
        elif args.command == "eia-detail-health":
            from .eia_detail import detail_health
            from .macro_scheduler import load_schedule
            result = detail_health(args.root, load_schedule(args.config))
            print(json.dumps(result, indent=2))
            return 2 if result["issues"] else 0
        elif args.command in {"eia-detail-collect", "eia-detail-import", "eia-detail-recover"}:
            import os
            from .eia_detail import DetailRecorder
            from .macro import MAX_BYTES
            from .macro_scheduler import load_contact
            delivery = "local_import" if args.command == "eia-detail-import" else getattr(args, "delivery", "http")
            worker = DetailRecorder(args.out, delivery=delivery)
            if args.command == "eia-detail-collect":
                contact = load_contact(args.contact_file) if args.contact_file else os.environ.get("OILBOT_SOURCE_CONTACT")
                result = worker.collect("eia", contact=contact)
            else:
                with component_lock(worker.root, "macro"):
                    if args.command == "eia-detail-recover":
                        recovered = worker.recover()
                        result = {"recovered": recovered, "status": "FAILED" if any(r["status"] == "FAILED" for r in recovered) else "OK"}
                    else:
                        if args.file.stat().st_size > MAX_BYTES:
                            raise ValueError("EIA detail import exceeds size limit")
                        result = worker.ingest("eia", args.file.read_bytes())
            print(json.dumps(result, indent=2))
            return 2 if result.get("status") in {"FAILED", "BLOCKED"} else 0
        elif args.command in {"eia-detail-report", "bsee-report"}:
            if args.command == "eia-detail-report":
                from .eia_detail import detail_report
                result = detail_report(args.journal, at=args.at)
                degraded = bool(result["issues"])
            else:
                from .bsee import bsee_report
                result = bsee_report(load_config(args.config), at=args.at)
                degraded = any(r["issues"] for r in result["reports"])
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                with args.out.open("x") as stream:
                    json.dump(result, stream, indent=2)
            print(json.dumps(result, indent=2))
            return 2 if degraded else 0
        elif args.command == "weather-report":
            from .weather import load_regions, weather_report
            result = weather_report(load_config(args.config), load_regions(args.regions), at=args.at)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                with args.out.open("x") as stream:
                    json.dump(result, stream, indent=2)
        elif args.command == "macro-research":
            from .clock import epoch_ns
            from .inventory_strategy import load_inventory_rules, research_macro
            market = load_manifest(args.manifest)[2] if args.manifest else None
            if epoch_ns(args.at) > epoch_ns(utc_now()):
                raise ValueError("cannot evaluate future decisions")
            result = research_macro(args.journal, at=args.at, market=market,
                rules=load_inventory_rules(json.loads(args.rules.read_text())))
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                with args.out.open("x") as stream:
                    json.dump(result, stream, indent=2)
        elif args.command == "macro-dataset":
            from .inventory_strategy import load_inventory_rules
            from .macro_dataset import build_macro_dataset
            from .outcomes import OutcomePolicy
            market = load_manifest(args.manifest)[2] if args.manifest else None
            result = build_macro_dataset(args.journal, args.out, through=args.through, market=market,
                rules=load_inventory_rules(json.loads(args.rules.read_text())),
                policy=OutcomePolicy(**json.loads(args.costs.read_text())) if args.costs else OutcomePolicy(),
                manifest=args.manifest)
        elif args.command == "macro-evaluate":
            from .macro_evaluation import evaluate_macro_dataset
            result = evaluate_macro_dataset(args.dataset, holdout_start=args.holdout_start,
                horizon_seconds=args.horizon, strategy=args.strategy,
                extra_round_trip_cost_usd=args.extra_round_trip_cost)
            if args.out:
                if args.dataset.resolve() in args.out.resolve().parents:
                    raise ValueError("evaluation output must be outside the frozen dataset")
                args.out.parent.mkdir(parents=True, exist_ok=True)
                with args.out.open("x") as stream:
                    json.dump(result, stream, indent=2)
        elif args.command in {"ibkr-preflight", "ibkr-capture"}:
            import os
            from .ibkr import load_ibkr_config, ibkr_preflight, capture_ibkr
            config = load_ibkr_config(args.config)
            if args.command == "ibkr-preflight":
                result = ibkr_preflight(config)
            else:
                stop = threading.Event()
                signal.signal(signal.SIGTERM, lambda *_: stop.set())
                signal.signal(signal.SIGINT, lambda *_: stop.set())
                result = capture_ibkr(config, args.out, account=os.environ.get(config.account_env), stop=stop)
            print(json.dumps(result, indent=2))
            return 0 if not result.get("blockers", result.get("connection_blockers")) else 2
        elif args.command == "operational-queue":
            from .operational import read_queue
            config = load_config(args.config)
            result = read_queue(config.db("forward"), after_seq=args.after_seq, limit=args.limit, include_baseline=args.include_baseline)
        elif args.command == "forward-quality":
            from .forward_quality import forward_quality
            result = forward_quality(load_config(args.config), window_seconds=args.window_seconds)
        elif args.command == "reset-source-circuit":
            from .operations import reset_source_circuit
            config = load_config(args.config)
            if args.source not in {s["id"] for s in config.sources}:
                raise ValueError("unknown source")
            result = {"transition_id": reset_source_circuit(Journal(config.db("news")), args.source, args.reason)}
        elif args.command == "propose-forward-link":
            from .forward_review import propose_link
            result = {"candidate_link_id": propose_link(Journal(load_config(args.config).db("forward")),
                args.left, args.right, args.relation, args.reason, args.reviewer, proposal_id=args.proposal_id)}
        elif args.command == "review-forward-link":
            from .forward_review import review_link
            result = {"review_id": review_link(Journal(load_config(args.config).db("forward")), args.candidate,
                args.decision, args.reason, args.reviewer, supersedes_review_id=args.supersedes_review, review_id=args.review_id)}
        elif args.command == "forward-episodes":
            from .forward_review import episode_map
            result = episode_map(load_config(args.config).db("forward"), through=args.through)
        elif args.command == "forward-candidates":
            from .linking import read_candidates
            result = read_candidates(load_config(args.config).db("forward"), after_seq=args.after_seq, limit=args.limit)
        elif args.command in {"qualify-sources", "collection-health"}:
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
        elif args.command == "paper-replay":
            from .paper import PaperLimits, paper_replay
            result = paper_replay(args.manifest, args.out, args.instrument, PaperLimits(**json.loads(args.limits.read_text())))
        elif args.command == "paper-scenario":
            from .paper_scenario import run_scenario
            result = run_scenario(args.file, args.out, resume=args.resume)
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
