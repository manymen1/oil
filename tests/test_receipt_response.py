from dataclasses import replace

import pytest

from oilbot.features import MarketView, RECEIPT_WINDOWS
from test_research_v2 import at, record, quote, DEFINITIONS, market_rows


def test_pre_receipt_excludes_receipt_jump_and_splits_processing_latency():
    rows = [record(d) for d in DEFINITIONS]
    for t, bid in [(-300, "69.00"), (-1, "70.00"), (0, "71.00"), (1, "72.00"), (2, "73.00")]:
        rows.append(record(quote(seconds=t, bid=bid, ask=str(float(bid) + .02))))
    f = MarketView(rows).receipt_response(at(), at(1), at(2))
    assert set(f["returns_before_receipt"]) == {str(n) for n in RECEIPT_WINDOWS}
    assert f["returns_before_receipt"]["300"] == pytest.approx(1 / 69.01)
    assert f["receipt_step_return"] == pytest.approx(1 / 70.01)
    assert f["receipt_to_candidate_return"] == pytest.approx(1 / 71.01)
    assert f["candidate_to_decision_return"] == pytest.approx(1 / 72.01)
    assert f["latency_ms"] == {"receipt_to_candidate": 1000, "candidate_to_decision": 1000, "receipt_to_decision": 2000}


def test_future_quotes_and_definitions_cannot_change_receipt_features():
    rows = market_rows()
    before = MarketView(rows).receipt_response(at(-2), at(-1), at())
    rows += [record(quote(seconds=.000000001, bid="99.00", ask="99.02")),
             record(replace(DEFINITIONS[0], available_at=at(1), vendor_id="updated-vendor-id"))]
    assert before == MarketView(rows).receipt_response(at(-2), at(-1), at())


def test_gaps_missing_prices_and_unknown_contract_remain_missing():
    empty = MarketView([]).receipt_response(at(), at(1), at(2))
    assert empty["instrument_id"] is None
    assert all(v is None for v in empty["returns_before_receipt"].values())
    f = MarketView(market_rows(), [{"available_at": at(-1), "reason": "gap"}]).receipt_response(at(-2), at(-1), at())
    assert f["receipt_to_candidate_return"] is None and f["candidate_to_decision_return"] is None
    later_definition = record(replace(DEFINITIONS[0], available_at=at()))
    assert MarketView([later_definition, record(quote())]).receipt_response(at(), at(), at())["instrument_id"] is None


def test_timestamp_order_and_feature_policy_guards():
    view = MarketView([])
    with pytest.raises(ValueError, match="causally ordered"):
        view.receipt_response(at(), at(-1), at(1))
    with pytest.raises(ValueError, match="causally ordered"):
        view.receipt_response(at(), at(2), at(1))
    with pytest.raises(ValueError, match="feature policy"):
        view.receipt_response(at(), at(), at(), roll_days=-1)
