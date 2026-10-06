from copy import deepcopy
import json
from pathlib import Path

import pytest

from oilbot.macro_shadow import (build_shadow_dataset, evaluate_shadow_dataset, freeze_shadow,
    load_shadow_protocol, shadow_health, shadow_rows, shadow_tick)
from oilbot.store import Journal
from test_inventory_strategy import research_case
from test_inventory_v2 import case
from test_macro import EIA
from test_research_v2 import at, record, quote, DEFINITIONS


@pytest.fixture
def protocol(tmp_path):
    spec = json.loads(Path("configs/eia-shadow.json").read_text())
    spec.update(evaluation_start=at(-1), holdout_start=at(120), evaluation_end=at(12 * 86400), horizon_seconds=5)
    schedule = json.loads(Path("configs/macro-scheduler.json").read_text())
    path = tmp_path.parent / (tmp_path.name + "-protocol") / "protocol.json"
    freeze_shadow(spec, schedule, path, clock=lambda: at(-120))
    return path


def state_root(tmp_path):
    return tmp_path.parent / (tmp_path.name + "-state")


def tick(protocol, case, tmp_path, second, **kwargs):
    return shadow_tick(protocol, case[0].store.path, None, state_root(tmp_path), clock=lambda: at(second), _market=case[-1], **kwargs)


def export(protocol, case, tmp_path, suffix="dataset", second=69, market=None):
    root = tmp_path.parent / (tmp_path.name + "-" + suffix)
    metadata = build_shadow_dataset(protocol, state_root(tmp_path) / "shadow.sqlite3", case[0].store.path,
        None, root, through=at(second), _market=market if market is not None else case[-1])
    rows = [json.loads(r) for r in (root / "rows.jsonl").read_text().splitlines()]
    return metadata, rows, root


def test_freeze_is_prospective_immutable_and_bound_to_code(protocol, monkeypatch):
    import oilbot.macro_shadow as module
    p = load_shadow_protocol(protocol)
    assert not p["trade_authorized"] and not p["market_data_qualified"]
    with pytest.raises(ValueError, match="already exists"):
        freeze_shadow(p["spec"], p["calendar"], protocol, clock=lambda: at(-120))
    with pytest.raises(ValueError, match="precede"):
        freeze_shadow(p["spec"], p["calendar"], protocol.parent / "late.json", clock=lambda: at())
    monkeypatch.setattr(module, "code_identity", lambda: {})
    with pytest.raises(ValueError, match="code changed"):
        load_shadow_protocol(protocol)


def test_changed_rules_or_costs_are_rejected(protocol, tmp_path):
    p = json.loads(protocol.read_text())
    p["spec"]["rules"]["confirmation_seconds"] = 65
    protocol.write_text(json.dumps(p))
    with pytest.raises(ValueError, match="changed"):
        load_shadow_protocol(protocol)


def test_worker_waits_and_records_actual_completion_once(protocol, case, tmp_path):
    result = tick(protocol, case, tmp_path, -2)
    assert result["state"] == "WAITING_FOR_START" and not state_root(tmp_path).exists()
    assert tick(protocol, case, tmp_path, 59)["pending_groups"] == 1
    assert len(tick(protocol, case, tmp_path, 61)["decisions_written"]) == 1
    assert not tick(protocol, case, tmp_path, 63)["decisions_written"]
    rows = Journal(state_root(tmp_path) / "shadow.sqlite3").records("shadow_decision")
    assert len(rows) == 1
    decision = rows[0]["payload"]
    assert decision["decision_clock"] == "actual_worker_completion" and decision["decision_at"] == at(61)
    assert decision["feature_at"] == at(61) and decision["decision_lateness_ms"] == 1000
    assert decision["dataset_role"] == "engineering_fixture" and not decision["trade_authorized"]


def test_late_worker_abstains_without_backdating(protocol, case, tmp_path):
    tick(protocol, case, tmp_path, 80)
    row = Journal(state_root(tmp_path) / "shadow.sqlite3").records("shadow_decision")[0]["payload"]
    assert row["decision_at"] == at(80) and row["decision"]["action"] == "ABSTAIN"
    assert "MISSED_FROZEN_DECISION_WINDOW" in row["comparison_exclusions"]
    assert row["decision"]["baselines"]["inventory_continuation_v2"] == 0


def test_computation_latency_counts_against_frozen_window(protocol, case, tmp_path):
    clocks = iter([at(60), at(60), at(66), at(66)])
    shadow_tick(protocol, case[0].store.path, None, state_root(tmp_path), clock=lambda: next(clocks), _market=case[-1])
    row = Journal(state_root(tmp_path) / "shadow.sqlite3").records("shadow_decision")[0]["payload"]
    assert row["feature_at"] == at(60) and row["decision_at"] == at(66)
    assert row["processing_ms"] == 6000 and "MISSED_FROZEN_DECISION_WINDOW" in row["comparison_exclusions"]


def test_correction_before_decision_vetoes_original_and_never_creates_second_trade(protocol, case, tmp_path):
    case[0].ingest("eia", EIA.replace(b"420.000", b"419.000").replace(b"-3.000", b"-4.000"), received_at=at(30))
    tick(protocol, case, tmp_path, 60)
    row = Journal(state_root(tmp_path) / "shadow.sqlite3").records("shadow_decision")[0]["payload"]
    assert "SUPERSEDED_BEFORE_DECISION" in row["comparison_exclusions"]
    assert not tick(protocol, case, tmp_path, 90)["decisions_written"]


def test_missing_market_is_retained_and_not_zero_profit(protocol, case, tmp_path):
    shadow_tick(protocol, case[0].store.path, None, state_root(tmp_path), clock=lambda: at(60))
    metadata, rows, _ = export(protocol, case, tmp_path)
    assert rows[0]["comparisons"] is None and rows[0]["outcomes"] is None
    assert "MISSING_QUALIFIED_MARKET_FEATURES" in rows[0]["comparison_exclusions"]
    assert metadata["denominator"]["unpriced_decisions"] == 1 and not metadata["promotion"]


def test_forward_export_preserves_decisions_and_pending_labels(protocol, case, tmp_path):
    tick(protocol, case, tmp_path, 60)
    meta, rows, root = export(protocol, case, tmp_path, second=62)
    row = rows[0]
    assert row["decision_clock"] == "actual_worker_completion" and row["decision_at"] == at(60)
    assert row["comparisons"]["5"]["state"] == "PENDING_HORIZON"
    assert row["comparisons"]["5"]["baselines"]["no_trade"]["net_pnl"] is None
    assert meta["pipeline"] == "frozen_prospective_shadow" and meta["dataset_role"] == "engineering_fixture"
    assert set(row["latency_scenarios"]) == {"0", "1000", "5000"}
    with pytest.raises(ValueError, match="holdout remains locked"):
        evaluate_shadow_dataset(protocol, root)


def test_future_observations_do_not_rewrite_prior_decision_or_dataset(protocol, case, tmp_path):
    tick(protocol, case, tmp_path, 60)
    before, rows_before, _ = export(protocol, case, tmp_path, "before")
    case[0].ingest("eia", EIA.replace(b"420.000", b"419.000").replace(b"-3.000", b"-4.000"), received_at=at(100))
    market = deepcopy(case[-1])
    market["records"].append(record(quote(DEFINITIONS[2], 100, bid="80.00", ask="80.02")))
    tick(protocol, case, tmp_path, 110)
    after, rows_after, _ = export(protocol, case, tmp_path, "after", market=market)
    assert rows_before == rows_after
    assert before["market_input_hash"] == after["market_input_hash"]
    assert before["shadow_input_hash"] == after["shadow_input_hash"]


def test_output_cannot_write_inside_input_roots(protocol, case, tmp_path):
    with pytest.raises(ValueError, match="outside input"):
        shadow_tick(protocol, case[0].store.path, None, tmp_path / "inside", clock=lambda: at(60))


def test_worker_stops_at_end(protocol, case, tmp_path):
    assert tick(protocol, case, tmp_path, 12 * 86400)["state"] == "EXPERIMENT_ENDED"


def test_changed_journal_protocol_is_rejected(protocol, case, tmp_path):
    tick(protocol, case, tmp_path, 60)
    p = load_shadow_protocol(protocol)
    p["id"] = "wrong"
    with pytest.raises(ValueError, match="protocol mismatch"):
        shadow_rows(state_root(tmp_path) / "shadow.sqlite3", p, through=at(62))


def test_generic_evaluator_cannot_bypass_locked_shadow_holdout(protocol, case, tmp_path):
    from oilbot.macro_evaluation import evaluate_macro_dataset
    tick(protocol, case, tmp_path, 60)
    _, _, root = export(protocol, case, tmp_path)
    with pytest.raises(ValueError, match="holdout remains locked"):
        evaluate_macro_dataset(root, holdout_start=at(120), horizon_seconds=5)


def test_released_evaluation_uses_all_frozen_cost_and_latency_scenarios(protocol, case, tmp_path):
    tick(protocol, case, tmp_path, 60)
    meta, rows, root = export(protocol, case, tmp_path, second=12 * 86400)
    result = evaluate_shadow_dataset(protocol, root)
    assert set(result["cost_scenarios"]) == {"0", "2.00", "5.00"}
    assert set(result["latency_scenarios"]) == {"0", "1000", "5000"}
    assert all(r["settings"]["latency_extra_ms"] == int(delay) for delay, scenarios in result["latency_scenarios"].items() for r in scenarios.values())
    assert not result["trade_authorized"] and result["economic_validation"] == "unavailable"
    assert meta["denominator"]["primary_comparison_state_counts"]


def test_missing_and_stale_health_are_read_only(protocol, case, tmp_path):
    path = state_root(tmp_path) / "shadow.sqlite3"
    assert shadow_health(protocol, path, at=at(-2))["state"] == "WAITING_FOR_START"
    assert shadow_health(protocol, path, at=at(60))["reason_codes"] == ["SHADOW_JOURNAL_UNAVAILABLE"]
    assert not path.exists()
    tick(protocol, case, tmp_path, 60)
    assert shadow_health(protocol, path, at=at(62))["state"] == "OBSERVING"
    assert "SHADOW_HEARTBEAT_STALE" in shadow_health(protocol, path, at=at(200))["reason_codes"]


def test_changed_frozen_source_bundle_is_rejected(protocol):
    p = load_shadow_protocol(protocol)
    source = protocol.parent / p["code_directory"] / "oilbot" / "inventory_v2.py"
    source.write_text(source.read_text() + "\n# changed fixture\n")
    with pytest.raises(ValueError, match="source checksum"):
        load_shadow_protocol(protocol)
