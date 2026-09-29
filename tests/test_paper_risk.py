from dataclasses import asdict, replace
from decimal import Decimal
import json

import pytest

from oilbot.clock import epoch_ns, iso_ns
from oilbot.paper import PaperEngine, PaperLimits
from oilbot.paper_scenario import run_scenario
from oilbot.schema import MarketStatusEvent
from oilbot.store import Journal
from test_paper import engine, quote, signal, opened, at, definition


def events(engine, name):
    return [e for e in engine.summary()["events"] if e["event"] == name]


def test_research_candidate_is_not_order_authorization(engine):
    quote(engine, 0, 1)
    state = signal(engine, action="RESEARCH_CANDIDATE")
    assert state["pending"] is None and state["position"] is None
    assert events(engine, "SIGNAL_REJECTED")[-1]["reason"] == "INELIGIBLE_SIGNAL"


@pytest.mark.parametrize("update,reason", [
    ({"authorization": "live"}, "INELIGIBLE_SIGNAL"),
    ({"instrument_id": "CL-2026-10"}, "INELIGIBLE_SIGNAL"),
    ({"quantity": 2}, "QUANTITY_LIMIT"), ({"quantity": True}, "QUANTITY_LIMIT"),
    ({"limit_price": None}, "EXPLICIT_LIMIT_REQUIRED"), ({"episode_id": ""}, "INELIGIBLE_SIGNAL")])
def test_explicit_intent_guards(engine, update, reason):
    quote(engine, 0, 1)
    p = dict(action="PAPER_INTENT", authorization="paper_only", instrument_id=definition().instrument_id,
             quantity=1, direction=1, episode_id="episode", limit_price="72.03")
    engine.process("signal", {**p, **update}, at=at(0), event_id="intent")
    assert events(engine, "SIGNAL_REJECTED")[-1]["reason"] == reason


def test_cancellation_before_fill_and_after_fill_race(engine):
    quote(engine, 0, 1)
    state = signal(engine)
    order = state["pending"]["order_id"]
    engine.process("cancel", {"order_id": order}, at=at(0), event_id="cancel1")
    assert quote(engine, 1, 2)["position"] is None
    state = signal(engine, 1, "next")
    order = state["pending"]["order_id"]
    quote(engine, 2, 3)
    state = engine.process("cancel", {"order_id": order}, at=at(2), event_id="cancel2")
    assert state["position"]["quantity"] == 1
    assert events(engine, "CANCEL_REJECTED")[-1]["reason"] == "ORDER_NOT_WORKING"
    assert engine.summary()["reconciled"]


def test_episode_and_cooldown_prevent_correlated_reentry(engine):
    opened(engine)
    quote(engine, 2, 3, "72.50", "72.52")
    engine.process("signal", dict(action="PAPER_INTENT", authorization="paper_only", instrument_id=definition().instrument_id,
        quantity=1, direction=1, episode_id="s1", limit_price="72.60"), at=at(2), event_id="reprint")
    assert events(engine, "SIGNAL_REJECTED")[-1]["reason"] == "EPISODE_ALREADY_TRADED"
    signal(engine, 2, "different")
    assert events(engine, "SIGNAL_REJECTED")[-1]["reason"] == "COOLDOWN"


def test_notional_guard_uses_contract_multiplier(engine):
    small = PaperEngine(engine.store, definition(), replace(PaperLimits(), max_notional_usd="1000"))
    quote(small, 0, 1)
    signal(small)
    assert events(small, "SIGNAL_REJECTED")[-1]["reason"] == "NOTIONAL_LIMIT"
    assert small.summary()["pending_order"] is None


@pytest.mark.parametrize("changes", [{"flags": 4}, {"flags": 8}, {"flags": 32}, {"bid_size": 0}])
def test_nonexecutable_quotes_do_not_create_orders(engine, changes):
    quote(engine, 0, 1, **changes)
    signal(engine)
    assert events(engine, "SIGNAL_REJECTED")[-1]["reason"] == "QUOTE_UNAVAILABLE_OR_STALE"


def test_halt_does_not_fabricate_flatten_fill_and_resume_needs_new_quote(engine):
    opened(engine)
    def status(t, value):
        p = asdict(MarketStatusEvent(definition().instrument_id, at(t), value, "fixture", t + 50, "fixture", "synthetic"))
        return engine.process("status", p, at=at(t), event_id="status" + str(t))
    status(2, "HALTED")
    assert engine.summary()["mark_stale"]
    assert quote(engine, 3, 3)["position"]
    assert not events(engine, "EXIT_FILL")
    state = status(4, "RESUMED")
    assert state["position"] and state["quote"] is None
    assert quote(engine, 5, 4)["position"] is None
    signal(engine, 5, "after-resume")
    assert events(engine, "SIGNAL_REJECTED")[-1]["reason"] == "ACCOUNT_HALTED"


def test_corrupted_state_refuses_to_resume(engine):
    opened(engine)
    state = engine.store.cursor("paper:state")
    state["cash_pnl"] = "1000000"
    with engine.store.transaction() as db:
        engine.store.set_cursor(db, "paper:state", state)
    with pytest.raises(ValueError, match="reconciliation"):
        PaperEngine(engine.store, definition(), PaperLimits()).summary()


def test_foreign_journal_and_float_costs_rejected(tmp_path):
    store = Journal(tmp_path / "news.sqlite3")
    store.append("story_revision", {})
    with pytest.raises(ValueError, match="dedicated paper"):
        PaperEngine(store, definition(), PaperLimits())
    with pytest.raises(ValueError, match="decimal string"):
        replace(PaperLimits(), fee_per_side=1.0).validate()


def test_submicrosecond_latency_cannot_fill_early(engine):
    quote(engine, 0, 1)
    p = dict(action="PAPER_INTENT", authorization="paper_only", instrument_id=definition().instrument_id,
             quantity=1, direction=1, episode_id="nano", limit_price="72.03")
    t = iso_ns(epoch_ns(at(0)) + 999)
    engine.process("signal", p, at=t, event_id="nano-intent")
    assert quote(engine, 1, 2)["position"] is None
    assert quote(engine, 2, 3)["position"]


def test_scenario_repeat_is_idempotent_and_changed_scenario_is_rejected(tmp_path):
    from pathlib import Path
    source = Path("tests/fixtures/paper-engineering.json")
    dest = tmp_path / "paper"
    result = run_scenario(source, dest)
    assert result["reconciled"] and not result["open_position"]
    first = json.loads((dest / "report.json").read_text())
    assert sum(o["state"] == "FILLED" for o in first["orders"]) == 4
    assert sum(o["state"] == "CANCELLED" for o in first["orders"]) == 1
    run_scenario(source, dest, resume=True)
    assert json.loads((dest / "report.json").read_text()) == first
    assert len(Journal(dest / "paper.sqlite3").records("paper_input")) == len(json.loads(source.read_text())["inputs"])
    with pytest.raises(ValueError, match="already exists"):
        run_scenario(source, dest)
    changed = json.loads(source.read_text())
    changed["limits"]["contracts"] = 2
    changed["limits"]["max_contracts"] = 2
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="identity changed"):
        run_scenario(path, dest, resume=True)


def test_scenario_crash_then_resume_preserves_exact_accounting(tmp_path, monkeypatch):
    source = "tests/fixtures/paper-engineering.json"
    original = PaperEngine.process
    def interrupted(self, kind, payload, *, at, event_id):
        if event_id == "q3":
            raise OSError("synthetic process failure")
        return original(self, kind, payload, at=at, event_id=event_id)
    with monkeypatch.context() as m:
        m.setattr(PaperEngine, "process", interrupted)
        with pytest.raises(OSError):
            run_scenario(source, tmp_path / "interrupted")
    resumed = run_scenario(source, tmp_path / "interrupted", resume=True)
    clean = run_scenario(source, tmp_path / "clean")
    a = json.loads((tmp_path / "interrupted/report.json").read_text())
    b = json.loads((tmp_path / "clean/report.json").read_text())
    assert a == b and resumed["cash_pnl"] == clean["cash_pnl"]


def test_scenario_refuses_live_data_and_nonchronological_input(tmp_path):
    from pathlib import Path
    original = json.loads(Path("tests/fixtures/paper-engineering.json").read_text())
    for n, data in enumerate([dict(original, synthetic=False), dict(original, inputs=list(reversed(original["inputs"])))]):
        source = tmp_path / f"bad{n}.json"
        source.write_text(json.dumps(data))
        with pytest.raises(ValueError):
            run_scenario(source, tmp_path / f"out{n}")
        assert not (tmp_path / f"out{n}").exists()
