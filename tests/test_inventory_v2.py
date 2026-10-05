from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import json

import pytest

from oilbot.features import MarketView
from oilbot.inventory_strategy import evaluate_inventory, inventory_features, load_inventory_rules, research_macro
from oilbot.inventory_v2 import InventoryRulesV2
from oilbot.macro_dataset import build_macro_dataset
from oilbot.outcomes import OutcomePolicy
from test_inventory_strategy import research_case
from test_research_v2 import DEFINITIONS, at, quote, record


@pytest.fixture
def case(research_case):
    recorder, eia, cot, _, _ = research_case
    rows = [record(d) for d in DEFINITIONS]
    for second in range(-310, 70):
        for d in DEFINITIONS:
            ticks = max(0, min(second, 60) // (10 if d == DEFINITIONS[1] else 5))
            bid = Decimal("72.00") + Decimal(".01") * ticks
            rows.append(record(quote(d, second, bid=str(bid), ask=str(bid + Decimal(".02")))))
    market = {"records": rows, "gaps": []}
    features = inventory_features(MarketView(rows), at(), at(60), InventoryRulesV2())
    return recorder, eia, cot, features, market


def decide(case, **kwargs):
    _, eia, cot, features, _ = case
    return evaluate_inventory(eia, cot, features, at=at(60), rules=kwargs.get("rules", InventoryRulesV2()))


def test_cost_aware_continuation_preserves_baselines(case):
    result = decide(case)
    assert result["action"] == "RESEARCH_CANDIDATE"
    assert result["baselines"]["inventory_plus_price"] == result["baselines"]["inventory_continuation_v2"] == 1
    assert Decimal(result["continuation_metrics"]["MCL1"]["estimated_round_trip_cost_usd"]) == 6
    assert not result["trade_authorized"] and result["authorized_contracts"] == 0
    assert result["expected_profit"] is None and result["inventory_surprise"] is None


def test_old_single_jump_candidate_is_rejected(research_case):
    recorder, _, _, _, market = research_case
    value = research_macro(recorder.store.path, at=at(60), market=market, rules=InventoryRulesV2())["decision"]
    assert value["baselines"]["inventory_plus_price"] == 1
    assert value["baselines"]["inventory_continuation_v2"] == 0
    assert {"CONTINUATION_FADED:CL1", "CONTINUATION_FADED:MCL1", "CONFIRMATION_SMALL_RELATIVE_TO_COST"} <= set(value["reason_codes"])


def test_mcl_must_confirm_not_just_cl(case):
    recorder, _, _, _, market = case
    market["records"] = [r for r in market["records"] if r["kind"] == "instrument" or r["payload"]["instrument_id"] != DEFINITIONS[2].instrument_id]
    market["records"] += [record(quote(DEFINITIONS[2], s)) for s in range(-310, 70)]
    result = research_macro(recorder.store.path, at=at(60), market=market, rules=InventoryRulesV2())["decision"]
    assert "WEAK_CONTINUATION_CONFIRMATION:MCL1" in result["reason_codes"]
    assert result["action"] == "ABSTAIN"


def test_curve_contradiction_and_cost_stress(case):
    recorder, _, _, _, market = case
    market["records"].append(record(quote(DEFINITIONS[1], 60, bid="72.30", ask="72.32")))
    result = research_macro(recorder.store.path, at=at(60), market=market, rules=InventoryRulesV2())["decision"]
    assert "CALENDAR_SPREAD_CONTRADICTS" in result["reason_codes"]
    result = decide(case, rules=replace(InventoryRulesV2(), fee_per_contract_side="10.00"))
    assert "CONFIRMATION_SMALL_RELATIVE_TO_COST" in result["reason_codes"]


def test_short_direction_is_symmetric(case):
    recorder, eia, cot, _, market = case
    for key in ("commercial_crude", "gasoline", "distillate"):
        value = eia["payload"]["observation"]["facts"][key]
        value["change"] = str(-Decimal(value["change"]))
    for row in market["records"]:
        if row["kind"] == "market":
            p = row["payload"]
            p["bid"], p["ask"] = str(Decimal("144.02") - Decimal(p["ask"])), str(Decimal("144.02") - Decimal(p["bid"]))
    features = inventory_features(MarketView(market["records"]), at(), at(60), InventoryRulesV2())
    result = evaluate_inventory(eia, cot, features, at=at(60), rules=InventoryRulesV2())
    assert result["action"] == "RESEARCH_CANDIDATE" and result["baselines"]["inventory_continuation_v2"] == -1


def test_missing_curve_gap_and_roll_abstain(case):
    recorder, eia, cot, features, market = case
    features["continuation_v2"]["instruments"]["CL2"]["gap"] = True
    assert "INCOMPLETE_CONTINUATION_MARKET:CL2" in decide(case)["reason_codes"]
    features["continuation_v2"]["roles_before_receipt"]["MCL1"] = "other"
    assert "CONTINUATION_CONTRACT_CHANGED:MCL1" in decide(case)["reason_codes"]


def test_future_quotes_do_not_rewrite_v2_features_or_decision(case):
    recorder, _, _, features, market = case
    before = research_macro(recorder.store.path, at=at(60), market=market, rules=InventoryRulesV2())
    market["records"].append(record(quote(DEFINITIONS[2], 100, bid="90.00", ask="90.02")))
    after = research_macro(recorder.store.path, at=at(60), market=market, rules=InventoryRulesV2())
    assert before["decision"] == after["decision"] and before["features"] == after["features"]


@pytest.mark.parametrize("change", ["timestamp", "midpoint", "contract", "quote"])
def test_inconsistent_features_fail_closed(case, change):
    c = case[3]["continuation_v2"]
    if change == "timestamp": c["anchors"]["decision"] = at(61)
    if change == "midpoint": c["instruments"]["CL1"]["mids"]["receipt"] = "100.00"
    if change == "contract": c["instruments"]["MCL1"]["definition"]["multiplier"] = "1000"
    if change == "quote": c["instruments"]["MCL1"]["quotes"]["receipt"]["available_at"] = at(10)
    with pytest.raises(ValueError):
        decide(case)


@pytest.mark.parametrize("changes", [{"persistence_seconds": 60}, {"min_confirmation_ticks": True},
    {"slippage_ticks_per_side": -1}, {"fee_per_contract_side": "NaN"},
    {"min_confirmation_cost_multiple": "0.5"}, {"fee_per_contract_side": 1.0}, {"fee_per_contract_side": "abc"}])
def test_invalid_policy(changes):
    with pytest.raises(ValueError):
        replace(InventoryRulesV2(), **changes).validate()


def test_cli_rules_and_v2_dataset(case, tmp_path, capsys):
    from oilbot.cli import main
    recorder, _, _, _, market = case
    rules = load_inventory_rules(json.loads(Path("configs/inventory-continuation-v2.json").read_text()))
    root = tmp_path.parent / (tmp_path.name + "-v2-export")
    meta = build_macro_dataset(recorder.store.path, root, through=at(69), market=market, rules=rules)
    rows = [json.loads(x) for x in (root / "rows.jsonl").read_text().splitlines()]
    assert rows[-1]["comparisons"]["5"]["baselines"]["inventory_continuation_v2"]["net_pnl"] == "-6.00"
    assert meta["rules"]["version"] == rules.version
    assert main(["macro-research", "--journal", str(recorder.store.path), "--at", at(60),
        "--rules", "configs/inventory-continuation-v2.json"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"]["action"] == "ABSTAIN"
    with pytest.raises(ValueError, match="cost assumptions"):
        build_macro_dataset(recorder.store.path, root / "mismatch", through=at(69), market=market,
            rules=rules, policy=OutcomePolicy(fee_per_contract_side="5.00"))
