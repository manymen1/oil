from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json

import pytest

from oilbot.cli import main
from oilbot.clock import epoch_ns, iso_ns
from oilbot.databento import file_hash
from oilbot.inventory_v2 import InventoryRulesV2
from oilbot.macro_dataset import comparisons
from oilbot.macro_evaluation import evaluate_macro_dataset
from oilbot.outcomes import OutcomePolicy
from oilbot.schema import digest


def synthetic_rows():
    """Invented complete-fill labels; deliberately not observed market evidence."""
    rows = []
    rules = asdict(InventoryRulesV2())
    for n, (long_pnl, candidate_side) in enumerate(((10, 1), (-10, 0), (10, 1), (-10, 0))):
        receipt = datetime(2026, 1, 7, 15, 30, tzinfo=timezone.utc) + timedelta(weeks=n)
        at = receipt + timedelta(seconds=60)
        outcomes = {"assumptions": asdict(OutcomePolicy()), "horizons": {"5": {
            "label_state": "MATURED", "instruments": {"MCL1": {
                name: {"status": "CLOSED", "filled": 1, "exited": 1, "net_pnl": str(pnl)}
                for name, pnl in (("long", long_pnl), ("short", -long_pnl))}}}}}
        decision = {"rules_hash": digest(rules), "baselines": {"no_trade": 0,
            "inventory_only": 1, "price_only_60s": -1, "inventory_plus_price": 1,
            "inventory_continuation_v2": candidate_side}}
        rows.append({"release_group": "eia:" + receipt.date().isoformat(),
            "received_at": receipt.isoformat(), "available_at": receipt.isoformat(),
            "decision_at": at.isoformat(), "state": "EVALUATED", "comparison_exclusions": [],
            "initial_snapshot": False, "revision": False, "decision": decision, "outcomes": outcomes,
            "comparisons": comparisons(decision, outcomes, [], 1)})
    return rows


def write_dataset(root, rows):
    root.mkdir(exist_ok=True)
    for row in rows:
        row.pop("id", None)
        row["id"] = digest(row)
    (root / "rows.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (root / "observations.jsonl").write_text("")
    rules = asdict(InventoryRulesV2())
    meta = {"schema": "macro-research-dataset-v1", "through": "2026-02-01T00:00:00Z",
        "rows": len(rows), "release_groups": len({r["release_group"] for r in rows}),
        "rules": rules, "rules_hash": digest(rules), "outcome_policy": asdict(OutcomePolicy()),
        "dataset_role": "engineering_fixture", "market_modes": ["fixture"],
        "trade_authorized": False, "promotion": False,
        "files": {name: file_hash(root / name) for name in ("rows.jsonl", "observations.jsonl")}}
    (root / "dataset.json").write_text(json.dumps(meta))


def evaluate(root, **kwargs):
    return evaluate_macro_dataset(root, holdout_start=kwargs.pop("holdout_start", "2026-01-21T00:00:00Z"),
        horizon_seconds=5, minimum_train_groups=1, minimum_holdout_groups=1,
        minimum_holdout_trades=1, **kwargs)


def test_chronological_common_population_costs_and_disabled_promotion(tmp_path):
    write_dataset(tmp_path, synthetic_rows())
    result = evaluate(tmp_path)
    assert result["included_release_groups"] == {"train": ["eia:2026-01-07", "eia:2026-01-14"],
                                                  "holdout": ["eia:2026-01-21", "eia:2026-01-28"]}
    selected = result["summary"]["holdout"]["inventory_continuation_v2"]
    assert selected["net_pnl_usd"] == "10" and selected["trades"] == 1 and selected["releases"] == 2
    assert selected["profit_factor"] is None
    assert result["holdout_cost_stress"]["inventory_continuation_v2"]["net_pnl_usd"] == "8.00"
    assert result["summary"]["holdout"]["inventory_only"]["max_drawdown_usd"] == "10"
    assert result["result"] == "RESEARCH_SCREEN_PASSED"
    assert result["dataset_role"] == "engineering_fixture"
    assert not result["promotion"] and not result["trade_authorized"] and result["expected_profit"] is None


def test_default_sample_requirements_do_not_pass_tiny_winning_sample(tmp_path):
    write_dataset(tmp_path, synthetic_rows())
    result = evaluate_macro_dataset(tmp_path, holdout_start="2026-01-21T00:00:00Z", horizon_seconds=5)
    assert result["result"] == "INSUFFICIENT_EVIDENCE"
    assert {"INSUFFICIENT_TRAIN_RELEASES", "INSUFFICIENT_HOLDOUT_RELEASES", "INSUFFICIENT_HOLDOUT_TRADES"} <= set(result["reason_codes"])


def test_empty_unpriced_population_is_not_zero_profit(tmp_path):
    rows = synthetic_rows()
    for row in rows:
        row["comparisons"] = row["outcomes"] = None
        row["comparison_exclusions"] = ["MISSING_QUALIFIED_MARKET_FEATURES"]
    write_dataset(tmp_path, rows)
    result = evaluate(tmp_path)
    assert result["summary"]["holdout"]["inventory_continuation_v2"]["net_pnl_usd"] is None
    assert result["summary"]["holdout"]["inventory_continuation_v2"]["max_drawdown_usd"] is None
    assert result["excluded_row_counts"] == {"NO_COMPARISON_LABELS": 4}
    assert result["excluded_reason_counts"] == {"MISSING_QUALIFIED_MARKET_FEATURES": 4}
    assert result["result"] == "INSUFFICIENT_EVIDENCE"


def test_boundary_overlap_is_purged(tmp_path):
    rows = synthetic_rows()
    write_dataset(tmp_path, rows)
    split = iso_ns(epoch_ns(rows[1]["decision_at"]) + 3 * 10**9)
    result = evaluate(tmp_path, holdout_start=split)
    assert result["excluded_row_counts"]["PURGED_BOUNDARY_OVERLAP"] == 1
    assert result["included_release_groups"]["train"] == [rows[0]["release_group"]]


def test_incomplete_outcomes_are_excluded_not_zero(tmp_path):
    rows = synthetic_rows()
    rows[-1]["comparisons"]["5"]["state"] = "INCOMPLETE_MCL_EXECUTION"
    for label in rows[-1]["comparisons"]["5"]["baselines"].values():
        label["net_pnl"] = None
    write_dataset(tmp_path, rows)
    result = evaluate(tmp_path)
    assert result["excluded_row_counts"]["INCOMPLETE_MCL_EXECUTION"] == 1
    assert result["summary"]["holdout"]["no_trade"]["releases"] == 1


def test_stress_can_reject_positive_result(tmp_path):
    write_dataset(tmp_path, synthetic_rows())
    result = evaluate(tmp_path, extra_round_trip_cost_usd="15")
    assert "FAILS_ADDITIONAL_COST_STRESS" in result["reason_codes"]
    assert result["result"] == "REJECTED_BY_RESEARCH_SCREEN"


def test_rejects_no_improvement_and_negative_holdout(tmp_path):
    rows = synthetic_rows()
    for row in rows[2:]:
        row["decision"]["baselines"]["inventory_continuation_v2"] = 1
        row["comparisons"] = comparisons(row["decision"], row["outcomes"], [], 1)
    write_dataset(tmp_path, rows)
    result = evaluate(tmp_path)
    assert "HOLDOUT_NOT_POSITIVE_AFTER_COSTS" in result["reason_codes"]
    assert "NO_INCREMENTAL_PNL_OVER:inventory_plus_price" in result["reason_codes"]


def test_duplicate_release_does_not_inflate_sample(tmp_path):
    rows = synthetic_rows()
    duplicate = deepcopy(rows[-1])
    duplicate["decision_at"] = iso_ns(epoch_ns(duplicate["decision_at"]) + 10**9)
    rows.append(duplicate)
    write_dataset(tmp_path, rows)
    with pytest.raises(ValueError, match="multiple comparable"):
        evaluate(tmp_path)


@pytest.mark.parametrize("mutation", ["future", "pnl", "costs", "rules", "initial", "overlap"])
def test_inconsistent_datasets_fail_closed(tmp_path, mutation):
    rows = synthetic_rows()
    if mutation == "future": rows[-1]["decision_at"] = "2026-02-02T00:00:00Z"
    if mutation == "pnl": rows[-1]["comparisons"]["5"]["baselines"]["inventory_only"]["net_pnl"] = "9999"
    if mutation == "costs": rows[-1]["outcomes"]["assumptions"]["fee_per_contract_side"] = "0"
    if mutation == "rules": rows[-1]["decision"]["rules_hash"] = "changed"
    if mutation == "initial": rows[-1]["initial_snapshot"] = True
    if mutation == "overlap": rows[-1]["decision_at"] = rows[-2]["decision_at"]
    write_dataset(tmp_path, rows)
    with pytest.raises(ValueError):
        evaluate(tmp_path)


def test_file_and_row_hash_validation(tmp_path):
    rows = synthetic_rows()
    write_dataset(tmp_path, rows)
    path = tmp_path / "rows.jsonl"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="file hash"):
        evaluate(tmp_path)
    write_dataset(tmp_path, rows)
    changed = path.read_text().replace('"state": "EVALUATED"', '"state": "CHANGED"')
    path.write_text(changed)
    meta = json.loads((tmp_path / "dataset.json").read_text())
    meta["files"]["rows.jsonl"] = file_hash(path)
    (tmp_path / "dataset.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="research row"):
        evaluate(tmp_path)


def test_cli_saves_new_report_and_refuses_overwrite(tmp_path, capsys):
    source = tmp_path / "dataset"
    write_dataset(source, synthetic_rows())
    target = tmp_path / "report.json"
    args = ["macro-evaluate", "--dataset", str(source), "--holdout-start", "2026-01-21T00:00:00Z",
            "--horizon", "5", "--out", str(target)]
    assert main(args) == 0
    assert json.loads(target.read_text())["result"] == "INSUFFICIENT_EVIDENCE"
    before = target.read_bytes()
    assert main(args) == 2 and before == target.read_bytes()
    assert main(args[:-1] + [str(source / "inside.json")]) == 2
    assert not (source / "inside.json").exists()
