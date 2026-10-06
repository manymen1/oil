from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import threading
import time

import pytest

from oilbot.clock import epoch_ns, utc_now
from oilbot.features import MarketView
from oilbot.market_curve import (CurveConfig, CurveState, append_canonical, capture_curve,
    contract_definition, curve_health, export_curve, load_curve_snapshot, read_curve, select_curve)
from oilbot.store import Journal


def details(product="CL", month="202611", con_id=1):
    now = datetime.now(timezone.utc)
    return {"conId": con_id, "symbol": product, "secType": "FUT", "exchange": "NYMEX", "currency": "USD",
        "multiplier": "1000" if product == "CL" else "100", "minTick": "0.01",
        "contractMonth": month, "localSymbol": product + month, "tradingClass": product,
        "lastTradeDateOrContractMonth": (now + timedelta(days=30 if month == "202611" else 60)).strftime("%Y%m%d"),
        "timeZoneId": "US/Central", "lastTradeTime": "13:30:00",
        "tradingHours": (now - timedelta(days=1)).strftime("%Y%m%d") + ":0000-" + (now + timedelta(days=2)).strftime("%Y%m%d") + ":1600"}


def contracts():
    return [details(), details(month="202612", con_id=2), details("MCL", con_id=3)]


def test_curve_selection_is_contract_specific_and_matches_front_month():
    chosen, rejected = select_curve(contracts(), at=utc_now())
    assert [chosen[role]["conId"] for role in ("CL1", "CL2", "MCL1")] == [1, 2, 3]
    assert chosen["CL1"]["definition"]["month"] == chosen["MCL1"]["definition"]["month"]
    assert not rejected
    near = details(con_id=4, month="202610")
    near["lastTradeDateOrContractMonth"] = (datetime.now(timezone.utc) + timedelta(days=6)).strftime("%Y%m%d")
    chosen, _ = select_curve([near] + contracts(), at=utc_now())
    assert chosen["CL1"]["conId"] == 1


@pytest.mark.parametrize("change", [{"multiplier": "100"}, {"minTick": "0.02"}, {"timeZoneId": "CST"},
    {"tradingHours": "20261006:1700-1600"}, {"tradingHours": ""}, {"lastTradeDateOrContractMonth": "202611"},
    {"contractMonth": "202613"}, {"conId": True}, {"currency": "EUR"}])
def test_unusable_definitions_fail_closed(change):
    with pytest.raises(ValueError):
        contract_definition({**details(), **change}, at=utc_now())


def test_timezone_overnight_and_dst_are_explicit():
    value = details()
    value["tradingHours"] = "20261005:1700-20261006:1600;20261007:CLOSED"
    d, _ = contract_definition(value, at="2026-10-06T00:00:00Z")
    assert d.sessions == [{"open": "2026-10-05T22:00:00+00:00", "close": "2026-10-06T21:00:00+00:00"}]
    value["tradingHours"] = "20261101:0130-20261101:0300"
    with pytest.raises(ValueError, match="ambiguous"):
        contract_definition(value, at=utc_now())
    value["tradingHours"] = "20260308:0230-20260308:0400"
    with pytest.raises(ValueError, match="nonexistent"):
        contract_definition(value, at=utc_now())


def test_ambiguous_month_and_missing_micro_are_rejected():
    with pytest.raises(ValueError, match="unambiguous"):
        select_curve(contracts() + [details(con_id=9)], at=utc_now())
    with pytest.raises(ValueError, match="matching"):
        select_curve(contracts()[:2] + [details("MCL", month="202612", con_id=3)], at=utc_now())


class FakeTransport:
    sdk_version = "synthetic-test-sdk"

    def __init__(self, config, account, stop, fault=None):
        self.stop, self.fault = stop, fault
        self.overflow = threading.Event()
        self.events = []

    def start(self):
        self.events += [("ready", {}), ("accounts", {"exact_match": self.fault != "account", "count": 1})]

    def discover(self):
        self.events += [("contract", {"reqId": 1001 if d["symbol"] == "CL" else 1002, "details": d}) for d in contracts()]
        self.events += [("contracts_end", {"reqId": 1001}), ("contracts_end", {"reqId": 1002}),
            ("positions_end", {}), ("orders_end", {})]

    def subscribe(self, selected):
        for req_id in (2001, 2002, 2003):
            self.events.append(("bidask", {"reqId": req_id, "source_time": int(time.time()),
                "bid": "72.00", "ask": "72.02", "bid_size": "20", "ask_size": "20",
                "bid_past_low": False, "ask_past_high": False}))
        if self.fault == "entitlement":
            self.events.append(("error", {"code": 354, "reqId": 2003}))
        if self.fault == "disconnect":
            self.events.append(("closed", {}))
        if self.fault == "overflow":
            self.overflow.set()

    def get(self, timeout):
        if not self.events:
            self.stop.set()
            return None
        kind, payload = self.events.pop(0)
        return {"kind": kind, "payload": payload, "at": utc_now(), "monotonic_ns": time.monotonic_ns()}

    def close(self):
        if self.fault == "tail":
            self.events.append(("error", {"code": 354, "reqId": 2003}))


def capture(root, fault=None):
    stop = threading.Event()
    factory = lambda config, account: FakeTransport(config, account, stop, fault)
    return capture_curve(CurveConfig(paper_session_confirmed=True, capture_seconds=1), root,
        account="DU123456", stop=stop, transport_factory=factory)


def test_capture_commits_raw_before_canonical_and_never_qualifies(tmp_path):
    result = capture(tmp_path)
    assert not result["blockers"] and result["quotes"] == {"CL1": 1, "CL2": 1, "MCL1": 1}
    assert not result["market_data_qualified"] and not result["trade_authorized"]
    rows = Journal(tmp_path / "market.sqlite3").records()
    by_id = {r["id"]: r for r in rows}
    for row in rows:
        if row["kind"] == "curve_record":
            assert by_id[row["payload"]["source_record_id"]]["seq"] < row["seq"]
    market = read_curve(tmp_path / "market.sqlite3", through=utc_now())
    quotes = [r["payload"] for r in market["records"] if r["kind"] == "market"]
    assert all(q["sequence_scope"] == "local_callback_instrument" for q in quotes)
    assert all(q["local_available_ns"] >= q["local_receive_ns"] for q in quotes)
    assert all(q["exchange_event_ns"] is None and q["provider_receive_ns"] is None for q in quotes)
    assert "DU123456" not in json.dumps(rows)
    assert curve_health(tmp_path / "market.sqlite3")["state"] == "DEGRADED"


@pytest.mark.parametrize("fault,reason", [("account", "PAPER_ACCOUNT_MISMATCH"),
    ("entitlement", "IBKR_ERROR:354"), ("disconnect", "BROKER_STREAM_DISCONNECTED"),
    ("overflow", "CALLBACK_QUEUE_OVERFLOW"), ("tail", "BROKER_ERROR_DURING_SHUTDOWN")])
def test_stream_errors_and_shutdown_tail_are_durable(tmp_path, fault, reason):
    result = capture(tmp_path, fault)
    assert reason in result["blockers"]
    assert read_curve(tmp_path / "market.sqlite3", through=utc_now())["gaps"]


def test_blocked_capture_never_constructs_transport_or_storage(tmp_path):
    def unexpected(*args):
        raise AssertionError("must not connect")
    result = capture_curve(CurveConfig(), tmp_path / "absent", transport_factory=unexpected)
    assert not result["connection_attempted"] and not (tmp_path / "absent").exists()


def test_reconnect_keeps_sequences_and_marks_downtime(tmp_path):
    capture(tmp_path)
    capture(tmp_path)
    market = read_curve(tmp_path / "market.sqlite3", through=utc_now())
    assert "RECONNECT_UNOBSERVED" in {g["reason"] for g in market["gaps"]}
    for role in ("IBKR:1", "IBKR:2", "IBKR:3"):
        assert [r["payload"]["sequence"] for r in market["records"] if r["kind"] == "market" and r["payload"]["instrument_id"] == role] == [1, 2]


def test_unclean_restart_is_not_hidden(tmp_path):
    capture(tmp_path)
    journal = Journal(tmp_path / "market.sqlite3")
    with journal.transaction() as db:
        journal.set_cursor(db, "curve_session", {"state": "RUNNING", "at": utc_now(), "id": "synthetic-crash"})
    capture(tmp_path)
    assert "UNCLEAN_RESTART" in {g["reason"] for g in read_curve(journal.path, through=utc_now())["gaps"]}


@pytest.mark.parametrize("change", [{"bid": "73"}, {"ask_size": "0"}, {"bid_size": "1.5"},
    {"bid_past_low": True}, {"source_time": 1}, {"source_time": True}])
def test_rejected_quote_retains_raw_and_creates_gap(tmp_path, change):
    journal = Journal(tmp_path / "market.sqlite3")
    now = utc_now()
    session = journal.append("curve_session_start", {}, available_at=now)
    state = CurveState(CurveConfig(), journal, session)
    state.selected, _ = select_curve(contracts(), at=now)
    event = {"kind": "bidask", "at": utc_now(), "monotonic_ns": time.monotonic_ns(), "payload": {
        "reqId": 2001, "source_time": int(time.time()), "bid": "72.00", "ask": "72.02",
        "bid_size": "20", "ask_size": "20", "bid_past_low": False, "ask_past_high": False, **change}}
    raw = journal.append("curve_raw", {"session_id": session, "event": event}, available_at=event["at"])
    state.accept(event, raw, available_at=utc_now())
    assert state.rejected["CL1"] == 1 and journal.get(raw)
    assert journal.records("curve_record")[-1]["payload"]["payload"]["reason"] == "REJECTED_QUOTE"


def test_outage_interval_vetoes_windows_inside_it():
    view = MarketView([], [{"available_at": "2026-10-06T10:00:00Z", "start_at": "2026-10-06T10:00:00Z", "end_at": "2026-10-06T12:00:00Z"}])
    assert view.gap_between("any", "2026-10-06T10:30:00Z", "2026-10-06T11:00:00Z")
    assert not view.gap_between("any", "2026-10-06T12:01:00Z", "2026-10-06T12:02:00Z")


def test_later_outage_report_does_not_rewrite_earlier_features():
    view = MarketView([], [{"available_at": "2026-10-06T12:00:00Z", "start_at": "2026-10-06T10:00:00Z", "end_at": "2026-10-06T12:00:00Z"}])
    assert not view.gap_between("any", "2026-10-06T10:30:00Z", "2026-10-06T11:00:00Z")
    assert view.gap_between("any", "2026-10-06T10:30:00Z", "2026-10-06T12:01:00Z")


def test_snapshot_roundtrip_is_checked_and_never_qualified(tmp_path):
    capture(tmp_path)
    out = tmp_path.parent / (tmp_path.name + "-snapshot")
    exported = export_curve(tmp_path / "market.sqlite3", out, through=utc_now())
    manifest, market = load_curve_snapshot(out / "manifest.json")
    assert exported == manifest and not manifest["market_data_qualified"]
    assert len(market["records"]) == 6
    (out / "market.json").write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        load_curve_snapshot(out / "manifest.json")


def test_read_health_does_not_create_a_missing_journal(tmp_path):
    import sqlite3
    with pytest.raises(sqlite3.Error):
        curve_health(tmp_path / "absent.sqlite3")
    assert not (tmp_path / "absent.sqlite3").exists()
