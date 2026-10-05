from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json

import pytest

from oilbot.databento import file_hash
from oilbot.macro_dataset import build_macro_dataset
from oilbot.outcomes import OutcomePolicy
from test_inventory_strategy import research_case
from test_macro import EIA, cot
from test_research_v2 import DEFINITIONS, at, quote, record


def export(case, target, through=at(62), market=None, **kwargs):
    recorder, *_ = case
    metadata = build_macro_dataset(recorder.store.path, target, through=through,
                                   market=market, **kwargs)
    rows = [json.loads(line) for line in (target / "rows.jsonl").read_text().splitlines()]
    return metadata, rows


def market_with_exits(case):
    market = deepcopy(case[-1])
    for second in (61, 62, 63, 65):
        for d in DEFINITIONS:
            market["records"].append(record(quote(d, second, bid="72.15", ask="72.17")))
    return market


def test_unpriced_export_retains_baselines_and_hashes(research_case, tmp_path):
    metadata, rows = export(research_case, tmp_path.parent / (tmp_path.name + "-export"))
    assert metadata["rows"] == 2 and metadata["release_groups"] == 2
    assert metadata["dataset_role"] == "unpriced_research"
    assert metadata["comparison_eligible_rows"] == 0
    assert not metadata["trade_authorized"] and not metadata["promotion"]
    assert all(r["decision"]["action"] == "ABSTAIN" and r["outcomes"] is None for r in rows)
    target = tmp_path.parent / (tmp_path.name + "-export")
    assert metadata["files"] == {name: file_hash(target / name) for name in metadata["files"]}


def test_pending_decision_and_horizon_are_not_zero_pnl(research_case, tmp_path):
    _, rows = export(research_case, tmp_path.parent / (tmp_path.name + "-pending"), through=at(59))
    assert rows[-1]["state"] == "PENDING_DECISION" and rows[-1]["decision"] is None
    _, rows = export(research_case, tmp_path.parent / (tmp_path.name + "-mature"), market=market_with_exits(research_case))
    row = rows[-1]
    assert row["decision"]["action"] == "RESEARCH_CANDIDATE"
    assert row["comparisons"]["1"]["state"] == "COMPARABLE"
    # At t=62 a quote is fresh for the t=63 exit, but that horizon has not elapsed.
    assert row["outcomes"]["horizons"]["2"]["label_state"] == "PENDING_HORIZON"
    assert row["comparisons"]["2"]["baselines"]["no_trade"]["net_pnl"] is None
    assert row["outcomes"]["horizons"]["2"]["instruments"]["MCL1"]["long"]["net_pnl"] is None


def test_costs_and_pnl_use_mcl_not_cl(research_case, tmp_path):
    policy = OutcomePolicy(fee_per_contract_side="1.25", slippage_ticks_per_side=2)
    _, rows = export(research_case, tmp_path.parent / (tmp_path.name + "-costs"),
                     market=market_with_exits(research_case), policy=policy)
    row = rows[-1]
    outcomes = row["outcomes"]["horizons"]["1"]["instruments"]
    mcl, cl = outcomes["MCL1"]["long"], outcomes["CL1"]["long"]
    assert Decimal(mcl["net_pnl"]) == Decimal("-8.50")  # six adverse ticks * $1 plus $2.50 fees
    assert Decimal(cl["net_pnl"]) == Decimal("-62.50")
    assert row["comparisons"]["1"]["baselines"]["inventory_only"]["net_pnl"] == mcl["net_pnl"]
    assert row["comparisons"]["1"]["baselines"]["no_trade"]["net_pnl"] == "0"


def test_causal_cot_future_quotes_and_corrections_do_not_change_prior_export(research_case, tmp_path):
    recorder, *_ = research_case
    first_meta, first = export(research_case, tmp_path.parent / (tmp_path.name + "-before"), market=market_with_exits(research_case))
    recorder.ingest("cftc", json.dumps([cot("2026-09-15", m_money_positions_long_all="500")]).encode(), received_at=at(61))
    recorder.ingest("eia", EIA.replace(b"420.000", b"419.000").replace(b"-3.000", b"-4.000"), received_at=at(100))
    market = market_with_exits(research_case)
    market["records"].append(record(quote(DEFINITIONS[0], 200, bid="80.00", ask="80.02")))
    after_meta, after = export(research_case, tmp_path.parent / (tmp_path.name + "-after"), market=market)
    assert first == after  # t=61 COT correction cannot enter t=60 decision
    assert first_meta["market_input_hash"] == after_meta["market_input_hash"]
    assert first_meta["macro_input_hash"] != after_meta["macro_input_hash"]
    _, rows = export(research_case, tmp_path.parent / (tmp_path.name + "-correction"), through=at(160), market=market)
    assert rows[-1]["release_group"] == rows[-2]["release_group"]
    assert rows[-1]["supersedes_id"] == rows[-2]["revision_id"]
    assert "REVISION_NOT_NEW_RELEASE" in rows[-1]["comparison_exclusions"]


def test_correction_during_confirmation_invalidates_original(research_case, tmp_path):
    recorder, *_ = research_case
    recorder.ingest("eia", EIA.replace(b"420.000", b"419.000").replace(b"-3.000", b"-4.000"), received_at=at(30))
    _, rows = export(research_case, tmp_path.parent / (tmp_path.name + "-superseded"), market=market_with_exits(research_case))
    assert "SUPERSEDED_BEFORE_DECISION" in rows[1]["comparison_exclusions"]
    assert rows[1]["decision"]["action"] == "ABSTAIN"
    assert rows[1]["comparisons"]["1"]["state"] == "EXCLUDED_RELEASE"


def test_partial_or_zero_fills_never_count_as_comparable(research_case, tmp_path):
    market = market_with_exits(research_case)
    # Larger quantity than available depth: raw labels retain partial fills,
    # but a common fully executable comparison is unavailable.
    _, rows = export(research_case, tmp_path.parent / (tmp_path.name + "-partial"),
                     market=market, policy=OutcomePolicy(contracts=21))
    assert rows[-1]["comparisons"]["1"]["state"] == "INCOMPLETE_MCL_EXECUTION"
    assert rows[-1]["comparisons"]["1"]["baselines"]["inventory_only"]["net_pnl"] is None
    for r in market["records"]:
        if r["kind"] == "market" and r["payload"]["available_at"] >= at(61):
            r["payload"].update(ask_size=0, bid_size=0)
    _, rows = export(research_case, tmp_path.parent / (tmp_path.name + "-zero"), market=market)
    assert rows[-1]["comparisons"]["1"]["state"] == "INCOMPLETE_MCL_EXECUTION"


def test_signal_failure_kept_in_common_comparison(research_case, tmp_path):
    from oilbot.inventory_strategy import InventoryRules
    _, rows = export(research_case, tmp_path.parent / (tmp_path.name + "-threshold"),
        market=market_with_exits(research_case), rules=replace(InventoryRules(), min_crude_change_mb="4"))
    row = rows[-1]
    assert row["decision"]["action"] == "ABSTAIN" and not row["comparison_exclusions"]
    assert row["comparisons"]["1"]["baselines"]["inventory_plus_price"]["net_pnl"] == "0"
    assert Decimal(row["comparisons"]["1"]["baselines"]["inventory_only"]["net_pnl"]) < 0


def test_output_protection_and_future_cutoff(research_case, tmp_path):
    recorder, *_ = research_case
    with pytest.raises(ValueError, match="outside input"):
        build_macro_dataset(recorder.store.path, tmp_path / "export", through=at(62))
    target = tmp_path.parent / (tmp_path.name + "-unique")
    export(research_case, target)
    with pytest.raises(ValueError, match="already exists"):
        export(research_case, target)
    with pytest.raises(ValueError, match="future"):
        export(research_case, target, through="2099-01-01T00:00:00Z")


def test_recovered_parse_is_not_backdated(research_case, tmp_path, monkeypatch):
    from oilbot.macro import MacroRecorder
    recorder, *_ = research_case
    original = MacroRecorder.parse_capture
    monkeypatch.setattr(MacroRecorder, "parse_capture", lambda *a, **k: None)
    recorder.ingest("eia", EIA.replace(b"420.000", b"419.000").replace(b"-3.000", b"-4.000"), received_at=at(30))
    capture = recorder.store.records("macro_capture")[-1]
    monkeypatch.setattr(MacroRecorder, "parse_capture", original)
    recorder.parse_capture(capture["id"], parsed_at=at(400))
    _, rows = export(research_case, tmp_path.parent / (tmp_path.name + "-recovered"), through=at(400))
    assert rows[-1]["decision_at"] == at(400)
    assert "OUTSIDE_CONFIRMATION_WINDOW" in rows[-1]["comparison_exclusions"]
    assert rows[1]["decision_at"] == at(60)
    assert "SUPERSEDED_BEFORE_DECISION" not in rows[1]["comparison_exclusions"]


def test_missing_exit_is_not_scored_as_no_trade(research_case, tmp_path):
    _, rows = export(research_case, tmp_path.parent / (tmp_path.name + "-missing-exit"),
                     through=at(70), market=research_case[-1])
    assert rows[-1]["comparisons"]["5"]["state"] == "INCOMPLETE_MCL_EXECUTION"
    assert rows[-1]["comparisons"]["5"]["baselines"]["no_trade"]["net_pnl"] is None


def test_cli_export_without_market(research_case, tmp_path, capsys):
    from oilbot.cli import main
    recorder, *_ = research_case
    target = tmp_path.parent / (tmp_path.name + "-cli")
    assert main(["macro-dataset", "--journal", str(recorder.store.path), "--through", at(62), "--out", str(target)]) == 0
    metadata = json.loads(capsys.readouterr().out)
    assert metadata["rows"] == 2 and metadata["comparison_eligible_rows"] == 0
