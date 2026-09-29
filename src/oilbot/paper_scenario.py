"""Reproducible, restartable synthetic end-to-end paper scenarios.

Explicit synthetic intents test execution plumbing, not forecasting skill.
No model, feed, broker, account credentials or real-time activation exists here.
"""
from dataclasses import asdict
import json
from pathlib import Path

from .clock import epoch_ns
from .market import atomic_json
from .paper import PaperEngine, PaperLimits
from .schema import InstrumentDefinition, MarketEvent, MarketStatusEvent, digest
from .store import Journal, component_lock


def run_scenario(source, destination, *, resume=False):
    data = json.loads(Path(source).read_text())
    if data.get("schema") != "paper-engineering-scenario-v1" or data.get("synthetic") is not True:
        raise ValueError("explicit synthetic engineering scenario required")
    definition = InstrumentDefinition(**data["definition"])
    limits = PaperLimits(**data["limits"])
    definition.validate()
    limits.validate()
    if definition.data_mode != "fixture":
        raise ValueError("scenario requires fixture instrument definition")
    inputs = data["inputs"]
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("nonempty ordered input list required")
    ids, last_at = set(), None
    for row in inputs:
        if set(row) != {"id", "at", "kind", "payload"} or not isinstance(row["id"], str) or not row["id"] or row["id"] in ids:
            raise ValueError("distinct explicit input identities required")
        ids.add(row["id"])
        at = epoch_ns(row["at"])
        if last_at is not None and at < last_at:
            raise ValueError("scenario inputs must be chronological")
        last_at = at
        if row["kind"] not in {"quote", "status", "signal", "clock", "halt", "cancel"}:
            raise ValueError("unknown scenario input")
        if row["kind"] in {"quote", "status"}:
            event = (MarketEvent if row["kind"] == "quote" else MarketStatusEvent)(**row["payload"])
            event.validate(definition)
            if event.data_mode != "fixture" or epoch_ns(event.available_at) != at:
                raise ValueError("fixture event with exact availability required")
    dest = Path(destination).resolve()
    source = Path(source).resolve()
    if source == dest or dest in source.parents:
        raise ValueError("scenario source must be outside output directory")
    if dest.exists() and not resume:
        raise ValueError("output already exists; use explicit --resume for the same scenario")
    if resume and not (dest / "scenario.json").is_file():
        raise ValueError("cannot resume without frozen scenario")
    dest.mkdir(parents=True, exist_ok=True)
    with component_lock(dest, "paper"):
        identity = digest(data)
        scenario_path = dest / "scenario.json"
        if scenario_path.exists():
            if digest(json.loads(scenario_path.read_text())) != identity:
                raise ValueError("scenario identity changed; use a new output directory")
        else:
            atomic_json(scenario_path, data)
        store = Journal(dest / "paper.sqlite3")
        engine = PaperEngine(store, definition, limits)
        with store.transaction() as db:
            binding = db.execute("SELECT value FROM cursors WHERE key='scenario:identity'").fetchone()
            if binding and json.loads(binding[0]) != identity:
                raise ValueError("paper journal scenario binding mismatch")
            store.set_cursor(db, "scenario:identity", identity)
        for row in inputs:
            engine.process(row["kind"], row["payload"], at=row["at"], event_id=row["id"])
        report = {**engine.summary(), "schema": "paper-engineering-report-v1", "scenario_hash": identity,
            "engine_identity": engine.identity, "definition": asdict(definition), "inputs": len(inputs),
            "dataset_role": "engineering_fixture", "synthetic": True,
            "claim": "Execution fault-test evidence only; not a backtest, forecast, or expected profit."}
        atomic_json(dest / "report.json", report)
        return {"report": str(dest / "report.json"), "journal": str(dest / "paper.sqlite3"),
            "mode": "local_paper", "synthetic": True, "reconciled": report["reconciled"],
            "cash_pnl": report["cash_pnl"], "open_position": bool(report["position"]), "trade_authorized": False}
