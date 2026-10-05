from copy import deepcopy
from dataclasses import replace
import json

import pytest

from oilbot.features import MarketView
from oilbot.inventory_strategy import InventoryRules, inventory_decision, latest_macro, research_macro
from oilbot.macro import MacroRecorder, read_macro
from test_macro import EIA, cot
from test_research_v2 import DEFINITIONS, at, quote, record


@pytest.fixture
def research_case(tmp_path):
    recorder = MacroRecorder(tmp_path)
    baseline = EIA.replace(b'"9/11/26"', b'"9/4/26"').replace(b'"9/18/26"', b'"9/11/26"')
    recorder.ingest("eia", baseline, received_at=at(-60))
    recorder.ingest("eia", EIA, received_at=at())
    recorder.ingest("cftc", json.dumps([cot("2026-09-15")]).encode(), received_at=at(-1))
    rows = [record(d) for d in DEFINITIONS]
    for second in range(-310, 61):
        for d in DEFINITIONS:
            rows.append(record(quote(d, second, bid="72.05" if second > 0 else "72.00",
                                     ask="72.07" if second > 0 else "72.02")))
    market = {"records": rows, "gaps": []}
    view = MarketView(rows)
    features = view.features(at(), at(60), roll_days=7)
    features["receipt_response"] = view.receipt_response(at(), at(), at(60), roll_days=7)
    macros = read_macro(recorder.store.path, through=at(60))
    return recorder, latest_macro(macros, "eia"), latest_macro(macros, "cftc"), features, market


def test_candidate_is_research_only_and_changes_are_not_surprises(research_case):
    _, eia, cot_row, features, _ = research_case
    result = inventory_decision(eia, cot_row, features, at=at(60))
    assert result["action"] == "RESEARCH_CANDIDATE" and result["hypothesis_direction"] == 1
    assert result["baselines"] == {"no_trade": 0, "inventory_only": 1, "price_only_60s": 1, "inventory_plus_price": 1}
    assert not result["trade_authorized"] and result["authorized_contracts"] == 0
    assert result["inventory_surprise"] is None and result["expected_profit"] is None
    assert result["cot_context"]["managed_money_net"] == "200"


@pytest.mark.parametrize("key,value,reason", [("initial_snapshot", True, "INITIAL_CAPTURE_OR_IMPORT"),
    ("delivery", "local_import", "INITIAL_CAPTURE_OR_IMPORT"), ("revision", True, "REVISION_NOT_NEW_RELEASE"),
    ("previous_successful_receipt_at", None, "UNBOUNDED_RELEASE_DETECTION_LAG"),
    ("previous_successful_receipt_at", at(-600), "UNBOUNDED_RELEASE_DETECTION_LAG"),
    ("prior_latest_period", "2026-09-04", "MISSING_CONSECUTIVE_RELEASE_BASELINE")])
def test_release_provenance_gates(research_case, key, value, reason):
    _, eia, cot_row, features, _ = research_case
    eia["payload"][key] = value
    result = inventory_decision(eia, cot_row, features, at=at(60))
    assert result["action"] == "ABSTAIN" and reason in result["reason_codes"]


def test_inventory_sign_product_confirmation_and_threshold(research_case):
    _, eia, cot_row, features, _ = research_case
    eia["payload"]["observation"]["facts"]["gasoline"]["change"] = "5"
    assert "PRODUCT_STOCKS_DO_NOT_CONFIRM" in inventory_decision(eia, cot_row, features, at=at(60))["reason_codes"]
    eia["payload"]["observation"]["facts"]["commercial_crude"]["change"] = "0.1"
    result = inventory_decision(eia, cot_row, features, at=at(60))
    assert result["hypothesis_direction"] == -1
    assert "INVENTORY_CHANGE_BELOW_DRAFT_THRESHOLD" in result["reason_codes"]
    assert "PRICE_DOES_NOT_CONFIRM" in result["reason_codes"]


@pytest.mark.parametrize("move,reason", [(None, "PRICE_DOES_NOT_CONFIRM"), (-.001, "PRICE_DOES_NOT_CONFIRM"),
    (.01, "PRICE_ALREADY_REPRICED"), (float("nan"), "PRICE_DOES_NOT_CONFIRM")])
def test_confirmation_and_no_chasing(research_case, move, reason):
    _, eia, cot_row, features, _ = research_case
    features["receipt_response"]["receipt_to_decision_return"] = move
    if move != move:
        # Nonfinite feature artifacts must not serialize into an apparently valid decision.
        with pytest.raises(ValueError):
            inventory_decision(eia, cot_row, features, at=at(60))
    else:
        assert reason in inventory_decision(eia, cot_row, features, at=at(60))["reason_codes"]


def test_market_risk_stale_roll_and_latency_gates(research_case):
    _, eia, cot_row, features, _ = research_case
    features["snapshots"]["MCL1"]["spread_ticks"] = 10
    features["realized_vol_5m"] = None
    features["receipt_response"]["instrument_id"] = "CL-other"
    reasons = inventory_decision(eia, cot_row, features, at=at(60))["reason_codes"]
    assert {"SPREAD_OR_DEPTH_MCL1", "MISSING_OR_EXCESSIVE_VOLATILITY", "CONTRACT_CHANGED_DURING_EVENT"} <= set(reasons)
    reasons = inventory_decision(eia, cot_row, None, at=at(500))["reason_codes"]
    assert "OUTSIDE_CONFIRMATION_WINDOW" in reasons and "MISSING_QUALIFIED_MARKET_FEATURES" in reasons


def test_future_context_or_feature_times_fail(research_case):
    _, eia, cot_row, features, _ = research_case
    cot_row["available_at"] = at(61)
    with pytest.raises(ValueError, match="future"):
        inventory_decision(eia, cot_row, features, at=at(60))
    features["decision_at"] = at(61)
    with pytest.raises(ValueError, match="time mismatch"):
        inventory_decision(eia, None, features, at=at(60))
    eia["available_at"] = at(100)
    with pytest.raises(ValueError, match="future"):
        inventory_decision(eia, None, None, at=at(60))


def test_cot_is_optional_lagged_context_not_confirmation(research_case):
    _, eia, cot_row, features, _ = research_case
    cot_row["payload"]["observation"]["period"] = "2026-01-01"
    result = inventory_decision(eia, cot_row, features, at=at(60))
    assert result["cot_context"] is None and result["action"] == "RESEARCH_CANDIDATE"
    assert cot_row["id"] not in result["input_revision_ids"]


def test_future_revisions_and_quotes_cannot_rewrite_decision(research_case):
    recorder, _, _, _, market = research_case
    before = research_macro(recorder.store.path, at=at(60), market=market)
    recorder.ingest("eia", EIA.replace(b"420.000", b"419.000").replace(b"-3.000", b"-4.000"), received_at=at(100))
    after_market = deepcopy(market)
    after_market["records"].append(record(quote(DEFINITIONS[0], 100, bid="80.00", ask="80.02")))
    after = research_macro(recorder.store.path, at=at(60), market=after_market)
    assert before["decision"] == after["decision"] and before["features"] == after["features"]
    assert before["macro_input_hash"] == after["macro_input_hash"]
    assert before["market_input_hash"] != after["market_input_hash"]
    assert research_macro(recorder.store.path, at=at(100))["decision"]["action"] == "ABSTAIN"


@pytest.mark.parametrize("changes", [{"min_crude_change_mb": "NaN"}, {"min_crude_change_mb": 2},
    {"min_crude_change_mb": "-1"}, {"confirmation_seconds": True}, {"confirmation_seconds": 301},
    {"max_receipt_move": float("nan")}, {"max_volatility_5m": -1}])
def test_policy_validation(changes):
    with pytest.raises(ValueError):
        replace(InventoryRules(), **changes).validate()


def test_cli_missing_market_is_explicit_abstention(research_case, capsys):
    from oilbot.cli import main
    recorder, *_ = research_case
    assert main(["macro-research", "--journal", str(recorder.store.path), "--at", at(60)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["decision"]["action"] == "ABSTAIN" and not result["trade_authorized"]
