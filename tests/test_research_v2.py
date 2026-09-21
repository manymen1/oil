from dataclasses import asdict, replace
from decimal import Decimal
import csv
import json

import pytest

from oilbot.clock import epoch_ns, iso_ns
from oilbot.clusters import cluster_transitions, duration_bucket, severity
from oilbot.databento import DatabentoHistoricalAdapter
from oilbot.dataset import transition_rows, fit_support
from oilbot.features import MarketView
from oilbot.market import record_adapter, read_archive
from oilbot.outcomes import forward_outcomes, executable_outcome, OutcomePolicy, HORIZONS
from oilbot.schema import InstrumentDefinition, QuoteEvent, TradeEvent, MarketStatusEvent, digest, market_event
from oilbot.strategy import strategy_v2, StrategyRules, context_key
from oilbot.study import event_study

BASE = epoch_ns("2026-09-18T12:00:00Z")


def at(seconds=0):
    return iso_ns(BASE + int(seconds * 10**9))


def definition(product="CL", month="2026-10", vendor="1"):
    return InstrumentDefinition(f"{product}-{month}", product, month, "NYMEX", "USD",
                                "1000" if product == "CL" else "100", "0.01", at(86400 * 20),
                                [{"open": at(-3600), "close": at(18000)}], at(-86400), vendor, "historical")


DEFINITIONS = [definition(), definition("CL", "2026-11", "2"), definition("MCL", vendor="3")]


def quote(d=DEFINITIONS[0], seconds=0, bid="72.00", ask="72.02", size=20, **kwargs):
    return QuoteEvent(d.instrument_id, at(seconds), bid, ask, size, size, at(seconds), at(seconds),
                      int((seconds + 1000) * 1000), "historical", "test", **kwargs)


def record(obj):
    kind = "instrument" if isinstance(obj, InstrumentDefinition) else "market"
    return {"kind": kind, "payload": asdict(obj), "id": digest([kind, asdict(obj)])}


def market_rows():
    rows = [record(d) for d in DEFINITIONS]
    for seconds in list(range(-310, 2)) + sorted(set(HORIZONS) | {h + 1 for h in HORIZONS}):
        for d in DEFINITIONS:
            rows.append(record(quote(d, seconds)))
    return rows


ASSETS = [{"id": "ras_tanura", "type": "crude_export_terminal", "geography": "Saudi Arabia"}]


def journal_row(identity, kind, when, payload):
    return {"id": identity, "kind": kind, "available_at": at(when), "recorded_at": at(when), "payload": payload}


def incident_records(statuses=((-300, "operating", "i1"), (0, "suspended", "i1"), (10, "partly_restored", "i2"), (20, "restored", "i2"))):
    rows = [journal_row("registry", "asset_registry", -400, {"assets": ASSETS})]
    for n, (when, status, incident) in enumerate(statuses):
        story = journal_row(f"story{n}", "story_revision", when - 1, {
            "source_id": "aramco", "source_role": "operator", "observed_at": at(when - 2), "published_at": None})
        payload = {"incident_id": incident, "operational_status": status,
                   "input_revision_ids": [story["id"], "registry"], "asset_ids": ["ras_tanura"],
                   "evidence_status": "primary_operational_report", "novel": True, "initial_snapshot": n == 0,
                   "late": False, "contradictions": [], "facts": []}
        rows.extend([story, journal_row(f"incident{n}", "incident_revision", when, payload)])
    rows.append(journal_row("assignment", "cluster_assignment", -100,
        {"episode_id": "episode1", "incident_ids": ["i1", "i2"], "input_revision_ids": ["incident0"]}))
    return rows


def event():
    return cluster_transitions(incident_records(), ASSETS)[0]


def features():
    value = MarketView(market_rows()).features(at(-2), at())
    value["feature_policy_hash"] = "policy"
    return value


def support(e, f):
    return {"target": "decision_to_horizon_signed_return", "event_family": e["event_family"],
            "event_transition": e["event_transition"], "execution_role": "MCL1", "context": context_key(e, f),
            "feature_policy_hash": f["feature_policy_hash"], "episode_ids": [f"past-{n}" for n in range(20)],
            "available_at": at(-10), "outcomes_through": at(-20), "median_residual": 0.005,
            "horizon_seconds": 300}


def test_market_records_are_distinct_and_lossless_at_nanosecond_boundary():
    timestamp = BASE + 999
    assert epoch_ns(iso_ns(timestamp)) == timestamp
    q = replace(quote(), available_at=iso_ns(timestamp), bid_at=iso_ns(timestamp), ask_at=iso_ns(timestamp),
                provider_receive_ns=timestamp, availability_basis="provider_receive_proxy")
    q.validate(DEFINITIONS[0])
    assert market_event(asdict(q)) == q
    t = TradeEvent(DEFINITIONS[0].instrument_id, at(), "-5.00", 3, "unknown", 1, "historical", "test")
    t.validate(DEFINITIONS[0])
    assert not hasattr(t, "bid")
    with pytest.raises(ValueError, match="availability"):
        replace(q, local_receive_ns=timestamp + 1, availability_basis="local_receive").validate(DEFINITIONS[0])
    with pytest.raises(ValueError, match="cannot represent"):
        replace(q, event_type="trade").validate(DEFINITIONS[0])
    view = MarketView([record(DEFINITIONS[0]), record(q)])
    assert view.quote(q.instrument_id, iso_ns(timestamp - 1)) is None
    assert view.quote(q.instrument_id, iso_ns(timestamp)) == q
    assert epoch_ns("2026-09-18 20:00:00.000000999+08:00") == timestamp
    with pytest.raises(ValueError, match="nanosecond"):
        epoch_ns("2026-09-18T12:00:00.1234567891Z")


def test_future_data_does_not_change_features():
    rows = market_rows()
    before = MarketView(rows).features(at(-2), at())
    rows += [record(quote(seconds=0.000000001, bid="100.00", ask="100.02"))]
    after = MarketView(rows).features(at(-2), at())
    assert before == after
    assert before["roles"] == {"CL1": "CL-2026-10", "CL2": "CL-2026-11", "MCL1": "MCL-2026-10"}
    assert before["realized_vol_5m"] == 0
    assert before["trade_imbalance_30s"] is None


def test_missing_stale_halted_and_gapped_quotes_are_not_used():
    d = DEFINITIONS[0]
    rows = [record(d), record(quote(seconds=-3))]
    assert MarketView(rows).quote(d.instrument_id, at()) is None
    rows.append(record(quote()))
    status = MarketStatusEvent(d.instrument_id, at(-1), "HALTED", "test", 9, "historical", "test")
    assert MarketView(rows + [record(status)]).quote(d.instrument_id, at()) is None
    assert MarketView(rows, [{"reason": "OPEN_SEGMENT"}]).quote(d.instrument_id, at()) is None


def test_trade_imbalance_uses_known_aggressor_only():
    rows = market_rows()
    for n, (side, size) in enumerate((("buy", 9), ("sell", 3), ("unknown", 7))):
        rows.append(record(TradeEvent(DEFINITIONS[0].instrument_id, at(-1), "72.01", size, side, n, "historical", "test")))
    f = MarketView(rows).features(at(-2), at())
    assert f["trade_imbalance_30s"] == 0.5 and f["unknown_trade_size_30s"] == 7


def test_many_incidents_form_one_economic_episode_and_no_future_assignment():
    records = incident_records()
    rows = cluster_transitions(records, ASSETS)
    assert [r["event_transition"] for r in rows] == ["operating_to_suspended", "suspended_to_partly_restored", "partly_restored_to_restored"]
    assert {r["episode_id"] for r in rows} == {"episode1"}
    assert "incident1" in rows[1]["input_revision_ids"]
    records[-1]["available_at"] = at(100)
    earlier = cluster_transitions(records, ASSETS)
    assert not earlier[0]["episode_verified"]
    assert earlier[0]["episode_id"].startswith("unassigned:")


def test_duplicate_cluster_state_does_not_make_new_event():
    records = incident_records(((-300, "operating", "i1"), (0, "suspended", "i1"), (1, "suspended", "i2")))
    assert len(cluster_transitions(records, ASSETS)) == 1


def test_assignment_cannot_promote_unverified_state_or_forget_members():
    records = incident_records(((-300, "operating", "i1"), (-200, "suspended", "i2"), (0, "suspended", "i1")))
    weak = next(r for r in records if r["id"] == "incident1")
    weak["payload"]["evidence_status"] = "unverified"
    rows = cluster_transitions(records, ASSETS)
    assert len(rows) == 1 and rows[0]["event_transition"] == "operating_to_suspended"
    assert rows[0]["cluster"]["incident_ids"] == ("i1", "i2")


def test_resumed_status_requires_a_fresh_quote():
    d = DEFINITIONS[0]
    rows = [record(d), record(quote(seconds=-1)),
            record(MarketStatusEvent(d.instrument_id, at(-0.5), "RESET", "test", 1, "historical", "test")),
            record(MarketStatusEvent(d.instrument_id, at(), "RESUMED", "test", 2, "historical", "test"))]
    assert MarketView(rows).quote(d.instrument_id, at()) is None
    assert MarketView(rows + [record(quote(seconds=1))]).quote(d.instrument_id, at(1)) is not None


def test_known_claim_conflicts_veto_but_future_conflicts_do_not():
    from test_provenance import claim
    records = incident_records(((-300, "operating", "i1"), (0, "suspended", "i1")))
    first = claim("a", "fars", "irgc", value="mine")
    second = claim("b", "centcom", "centcom", basis="first_party")
    first["available_at"], second["available_at"] = at(-2), at(-1)
    conflicted = transition_rows(records + [first, second], ASSETS, MarketView(market_rows()))[0]
    assert "CONTRADICTORY_EVIDENCE" in conflicted["decision"]["reason_codes"]
    second["available_at"] = at(1)
    causal = transition_rows(records + [first, second], ASSETS, MarketView(market_rows()))[0]
    assert "CONTRADICTORY_EVIDENCE" not in causal["decision"]["reason_codes"]


def test_literal_duration_and_capacity_safety():
    fact = {"field": "duration", "assertion": "asserted", "value": "3 hours"}
    assert duration_bucket([fact]) == "1-6H"
    for value in ("indefinite", "1-3 days", "up to 6 hours"):
        assert duration_bucket([{**fact, "value": value}]) == "UNKNOWN"
    quantity = {"field": "reported_quantity", "assertion": "asserted", "quantity_kind": "gross_capacity", "value": "600000"}
    assert severity([quantity]) == "severity_unknown"
    assert severity([{**quantity, "quantity_kind": "delivery_disruption"}]) == "partial_export_loss"


def test_strategy_residual_is_not_subtracted_twice():
    e, f = event(), features()
    f["price_move_before_decision"] = 0.004
    s = support(e, f)
    result = strategy_v2(e, f, support=s, expected_cost=0.0004)
    assert result["action"] == "RESEARCH_CANDIDATE"
    assert result["net_expected_residual"] == pytest.approx(0.0036)
    assert result["authorized_contracts"] == 0


@pytest.mark.parametrize("change,reason", [
    ({"episode_ids": ["episode1"] + [str(n) for n in range(20)]}, "INSUFFICIENT_HISTORICAL_SUPPORT"),
    ({"available_at": at(1)}, "INSUFFICIENT_HISTORICAL_SUPPORT"),
    ({"outcomes_through": at()}, "INSUFFICIENT_HISTORICAL_SUPPORT"),
    ({"median_residual": 0.0001}, "RESIDUAL_BELOW_COST_AND_BUFFER"),
    ({"target": "total_event_response"}, "INSUFFICIENT_HISTORICAL_SUPPORT"),
])
def test_support_cannot_leak_or_confuse_total_and_residual(change, reason):
    e, f = event(), features()
    decision = strategy_v2(e, f, support={**support(e, f), **change}, expected_cost=0.0004)
    assert decision["action"] == "ABSTAIN" and reason in decision["reason_codes"]


def test_named_market_gates_and_no_historical_support():
    e, f = event(), features()
    f["price_move_before_decision"] = 0.03
    f["realized_vol_5m"] = 0.1
    f["snapshots"]["MCL1"]["spread_ticks"] = 10
    f["snapshots"]["MCL1"]["quote"]["ask_size"] = 1
    decision = strategy_v2(e, f, expected_cost=0.0004)
    assert set(decision["reason_codes"]) >= {"PRICE_ALREADY_REPRICED", "VOLATILITY_TOO_HIGH", "SPREAD_TOO_WIDE", "INSUFFICIENT_DEPTH", "INSUFFICIENT_HISTORICAL_SUPPORT"}


@pytest.mark.parametrize("side,bid,ask,expected", [(1, "72.40", "72.42", "36.00"), (-1, "71.60", "71.62", "36.00")])
def test_outcome_bid_ask_fees_latency(side, bid, ask, expected):
    d = DEFINITIONS[2]
    rows = [record(d), record(quote(d, 0, "1.00", "1.02")), record(quote(d, 1)), record(quote(d, 6, bid, ask))]
    policy = OutcomePolicy(slippage_ticks_per_side=0)
    result = executable_outcome(MarketView(rows), d.instrument_id, at(), 5, side, policy)
    assert result["net_pnl"] == expected
    assert result["entry_price"] in {"72.02", "72.00"}


def test_partial_and_missing_exits_stay_unresolved():
    d = DEFINITIONS[2]
    rows = [record(d), record(quote(d, 1, "-5.02", "-5.00", 2)), record(quote(d, 6, "-4.00", "-3.98", 1))]
    policy = OutcomePolicy(contracts=3, slippage_ticks_per_side=0)
    outcome = executable_outcome(MarketView(rows), d.instrument_id, at(), 5, 1, policy)
    assert outcome["filled"] == 2 and outcome["remaining_open"] == 1 and outcome["net_pnl"] is None
    assert outcome["realized_contribution"] == "97.00"
    missing = executable_outcome(MarketView(rows[:2]), d.instrument_id, at(), 5, 1, policy)
    assert missing["status"] == "EXIT_UNAVAILABLE" and missing["net_pnl"] is None


def test_dataset_features_are_independent_of_forward_labels():
    records = incident_records(((-300, "operating", "i1"), (0, "suspended", "i1")))
    rows = market_rows()
    initial = transition_rows(records, ASSETS, MarketView(rows))[0]
    changed = rows + [record(quote(DEFINITIONS[2], 6, "80.00", "80.02"))]
    updated = transition_rows(records, ASSETS, MarketView(changed))[0]
    assert initial["features"] == updated["features"] and initial["decision"] == updated["decision"]
    assert initial["outcomes"] != updated["outcomes"]
    assert set(initial["outcomes"]["horizons"]) == set(map(str, HORIZONS))
    with pytest.raises(ValueError, match="not complete"):
        fit_support([initial], available_at=at(3), horizon=5)
    supports = fit_support([initial], available_at=at(100), horizon=5)
    assert len(supports) == 1 and supports[0]["episode_ids"] == ["episode1"]
    assert not supports[0]["promotion"]


def test_episode_split_purges_whole_related_episode():
    rows = transition_rows(incident_records(), ASSETS, MarketView(market_rows()))
    result = event_study(rows, [at(5), at(20000)])
    assert result["partitions"]["purged"]["rows"] == 3
    assert result["partitions"]["development"]["rows"] == 0


def registry(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"dataset": "GLBX.MDP3", "definition_source": "synthetic test fixture",
                               "definitions": [asdict(d) for d in DEFINITIONS]}))
    return path


def raw_row(**kwargs):
    return {"instrument_id": 1, "ts_event": BASE - 500, "ts_recv": BASE + 17,
            "price": 72020000000, "size": 3, "sequence": 10, "flags": 128, "action": "T", "side": "B",
            "bid_px_00": 72000000000, "ask_px_00": 72020000000, "bid_sz_00": 20, "ask_sz_00": 30,
            "bid_ct_00": 4, "ask_ct_00": 6, **kwargs}


def test_databento_csv_preserves_precision_trades_and_channel_sequences(tmp_path):
    path = tmp_path / "input.csv"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=raw_row().keys())
        writer.writeheader()
        writer.writerows([raw_row(), raw_row(ts_recv=BASE + 19, sequence=80, side="A")])
    adapter = DatabentoHistoricalAdapter(path, registry(tmp_path))
    result = record_adapter(adapter, tmp_path / "archive")
    assert result["events"] == 4
    records, gaps = read_archive(tmp_path / "archive")
    assert not gaps and not any(r["kind"] == "gap" for r in records)
    events = [market_event(r["payload"]) for r in records if r["kind"] == "market"]
    assert [e.aggressor for e in events if isinstance(e, TradeEvent)] == ["buy", "sell"]
    assert events[0].provider_receive_ns == BASE + 17 and events[0].exchange_event_ns == BASE - 500
    assert events[0].local_receive_ns is None
    assert events[1].bid_count == 4


def test_databento_native_dbn_uses_actual_sdk(tmp_path):
    dbn = pytest.importorskip("databento_dbn")
    meta = dbn.Metadata(dataset="GLBX.MDP3", start=BASE, stype_in=dbn.SType.RAW_SYMBOL,
                        stype_out=dbn.SType.INSTRUMENT_ID, schema=dbn.Schema.MBP_1)
    msg = dbn.MBP1Msg(publisher_id=1, instrument_id=1, ts_event=BASE, price=72020000000, size=2,
                     action=dbn.Action.TRADE, side=dbn.Side.BID, depth=0, ts_recv=BASE + 99, sequence=17,
                     levels=dbn.BidAskPair(bid_px=72000000000, ask_px=72020000000, bid_sz=20, ask_sz=30))
    path = tmp_path / "ticks.dbn"
    path.write_bytes(meta.encode() + bytes(msg))
    events = list(DatabentoHistoricalAdapter(path, registry(tmp_path)).events())
    assert len(events) == 2 and events[0].trade_price == "72.02"
    assert events[0].provider_receive_ns == BASE + 99
