from copy import deepcopy
import json

import pytest

from oilbot.cli import main
from oilbot.databento import file_hash
from oilbot.geopolitical import build_geopolitical_dataset
from oilbot.geopolitical_evaluation import evaluate_geopolitical
from oilbot.macro_dataset import comparisons
from oilbot.schema import digest
from test_geopolitical import news_case
from test_research_v2 import at


@pytest.fixture
def dataset(news_case,tmp_path):
    root = tmp_path / "priced-news"
    build_geopolitical_dataset(news_case[5],root,through=at(370),market=news_case[-1])
    return root


def evaluate(root, **kwargs):
    return evaluate_geopolitical(root,holdout_start=kwargs.pop("holdout_start",at(-1)),**kwargs)


def rewrite(root, rows):
    for row in rows:
        row.pop("id",None)
        row["id"] = digest(row)
    (root / "rows.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    meta = json.loads((root / "dataset.json").read_text())
    meta["files"]["rows.jsonl"] = file_hash(root / "rows.jsonl")
    meta["rows"] = len(rows)
    meta["evaluation_groups"] = len({r["evaluation_group"] for r in rows})
    (root / "dataset.json").write_text(json.dumps(meta))


def test_real_mcl_labels_and_no_economic_promotion(dataset):
    report = evaluate(dataset)
    risk = report["lanes"]["risk_premium"]["holdout"]
    assert risk["baselines"]["news_plus_continuation"]["net_pnl_usd"] == "-6.00"
    assert risk["cost_stress"]["news_plus_continuation"]["net_pnl_usd"] == "-8.00"
    assert risk["baselines"]["no_trade"]["net_pnl_usd"] == "0"
    assert report["lanes"]["physical_transition"]["holdout"]["baselines"]["news_plus_continuation"]["net_pnl_usd"] is None
    assert report["result"] == "DESCRIPTIVE_RESEARCH_ONLY"
    assert not report["promotion"] and not report["trade_authorized"]


def test_group_crossing_split_is_purged(dataset):
    report = evaluate(dataset,holdout_start=at(30))
    assert report["result"] == "INSUFFICIENT_EVIDENCE"
    assert report["excluded_counts"]["GROUP_SPANS_HOLDOUT_BOUNDARY"] == 1


def test_no_winner_selection_among_related_updates(dataset):
    rows = [json.loads(x) for x in (dataset / "rows.jsonl").read_text().splitlines()]
    second = deepcopy(rows[0])
    second["decision_at"] = at(65)
    second["event_id"] = "later-event"
    # Make the first decision abstain on a valid, fully labelled comparison.
    rows[0]["decision"]["baselines"]["news_plus_continuation"] = 0
    rows[0]["comparisons"] = comparisons(rows[0]["decision"],rows[0]["outcomes"],[],1)
    rewrite(dataset,[*rows,second])
    report = evaluate(dataset)
    assert report["excluded_counts"]["REPEATED_GROUP_MEMBER"] == 1
    assert report["lanes"]["risk_premium"]["holdout"]["baselines"]["news_plus_continuation"]["trades"] == 0


def test_missing_first_label_cannot_be_replaced_by_later_label(dataset):
    rows = [json.loads(x) for x in (dataset / "rows.jsonl").read_text().splitlines()]
    second = deepcopy(rows[0]); second["decision_at"] = at(65); second["event_id"] = "later"
    rows[0]["comparisons"] = None
    rewrite(dataset,[*rows,second])
    report = evaluate(dataset)
    assert report["result"] == "INSUFFICIENT_EVIDENCE"
    assert report["excluded_counts"]["NO_COMPARISON_LABELS"] == 1


def test_independent_groups_with_overlapping_windows_are_not_pooled(dataset):
    row = json.loads((dataset / "rows.jsonl").read_text())
    second = deepcopy(row); second["evaluation_group"] = "other"; second["event_id"] = "other-event"
    second["decision_at"] = at(61)
    rewrite(dataset,[row,second])
    report = evaluate(dataset)
    assert report["excluded_counts"]["OVERLAPPING_GROUP_WINDOW:risk_premium"] == 1
    assert len(report["lanes"]["risk_premium"]["holdout"]["groups"]) == 1


@pytest.mark.parametrize("change",["row_hash","comparison","cost","future"])
def test_inconsistent_outputs_fail_closed(dataset,change):
    row = json.loads((dataset / "rows.jsonl").read_text())
    if change == "comparison": row["comparisons"]["300"]["baselines"]["news_plus_continuation"]["net_pnl"] = "1000"
    if change == "cost": row["outcomes"]["assumptions"]["fee_per_contract_side"] = "0"
    if change == "future": row["decision_at"] = at(100)
    rewrite(dataset,[row])
    if change == "row_hash":
        text = (dataset / "rows.jsonl").read_text().replace('"lane": "risk_premium"','"lane": "changed"')
        (dataset / "rows.jsonl").write_text(text)
    with pytest.raises(ValueError): evaluate(dataset)


def test_cli_unpriced_data_is_missing_not_zero(news_case,tmp_path,capsys):
    out = tmp_path / "cli-data"
    assert main(["geopolitical-dataset","--manifest",str(news_case[5].path),"--through",at(370),"--out",str(out)]) == 0
    report = tmp_path / "evaluation.json"
    args = ["geopolitical-evaluate","--dataset",str(out),"--holdout-start",at(-1),"--out",str(report)]
    assert main(args) == 0
    value = json.loads(report.read_text())
    assert value["result"] == "INSUFFICIENT_EVIDENCE"
    assert value["lanes"]["risk_premium"]["holdout"]["baselines"]["news_direction_only"]["net_pnl_usd"] is None
    assert main(args) == 2
