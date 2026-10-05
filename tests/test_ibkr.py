from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from oilbot.clock import iso_ns, utc_now
from oilbot.ibkr import (IBKRConfig, ProbeState, capture_ibkr, connection_blockers,
                        ibkr_preflight, load_ibkr_config, select_mcl_contract)
from oilbot.ibkr_transport import make_client, IBKRTransport
from oilbot.store import Journal


def contract(**changes):
    return dict({"conId": 123, "symbol": "MCL", "secType": "FUT", "exchange": "NYMEX",
        "currency": "USD", "multiplier": "100", "minTick": "0.01", "localSymbol": "MCLX6",
        "contractMonth": "202611", "lastTradeDateOrContractMonth": "20261019",
        "tradingHours": "", "timeZoneId": "US/Central"}, **changes)


def event(kind, payload=None, *, at=None, mono=None):
    return {"kind": kind, "payload": payload or {}, "at": at or utc_now(),
            "monotonic_ns": mono if mono is not None else time.monotonic_ns()}


def bidask(**changes):
    return dict({"reqId": 2001, "source_time": int(time.time()), "bid": "72.01", "ask": "72.03",
                 "bid_size": "3", "ask_size": "4", "bid_past_low": False, "ask_past_high": False}, **changes)


@pytest.mark.parametrize("changes", [
    {"mode": "live"}, {"product": "CL"}, {"port": 4001}, {"port": 7496}, {"port": True},
    {"host": "8.8.8.8"}, {"host": "0.0.0.0"}, {"host": "224.0.0.1"}, {"host": "example.com"},
    {"host": "::1"}, {"host": 2130706433},
    {"client_id": 0}, {"client_id": True}, {"capture_seconds": 0}, {"capture_seconds": 3601},
    {"roll_buffer_days": 0}, {"paper_session_confirmed": "true"}, {"account_env": "HOME"}])
def test_configuration_guards(changes):
    with pytest.raises(ValueError):
        replace(IBKRConfig(), **changes).validate()


def test_account_selection_and_offline_preflight(monkeypatch):
    c = IBKRConfig()
    assert "PAPER_LOGIN_NOT_CONFIRMED" in connection_blockers(c, "DU123")
    for account in (None, "", "U123", "DU", "DU123 "):
        assert "EXPLICIT_PAPER_ACCOUNT_REQUIRED" in connection_blockers(c, account)
    monkeypatch.setenv(c.account_env, "DU123")
    report = ibkr_preflight(c)
    assert not report["connection_attempted"] and not report["trade_authorized"]
    assert "DU123" not in json.dumps(report)


def test_load_template_and_unknown_fields(tmp_path):
    assert load_ibkr_config("configs/ibkr-paper.json") == IBKRConfig()
    path = tmp_path / "bad.json"
    path.write_text('{"enable_live": true}')
    with pytest.raises(ValueError, match="fields"):
        load_ibkr_config(path)


def test_contract_selection_expiry_buffer_and_duplicates():
    c = contract()
    later = contract(conId=456, contractMonth="202612", lastTradeDateOrContractMonth="20261119")
    near = contract(conId=78, lastTradeDateOrContractMonth="20261006")
    assert select_mcl_contract([later, near, c, c], "2026-09-29T23:59:00Z") == c
    assert select_mcl_contract([c, later], "2026-10-12T00:00:00Z") == later
    with pytest.raises(ValueError, match="ambiguous"):
        select_mcl_contract([c, contract(conId=321)], "2026-09-29T00:00:00Z")
    with pytest.raises(ValueError, match="conflicting"):
        select_mcl_contract([c, contract(localSymbol="changed")], "2026-09-29T00:00:00Z")


@pytest.mark.parametrize("changes", [{"symbol": "CL"}, {"secType": "CONTFUT"}, {"exchange": "SMART"},
    {"multiplier": "1000"}, {"minTick": "0.1"}, {"minTick": "NaN"}, {"conId": 0}, {"conId": True},
    {"localSymbol": ""}, {"contractMonth": "202613"}, {"lastTradeDateOrContractMonth": "202611"}])
def test_reject_wrong_or_incomplete_contract(changes):
    with pytest.raises(ValueError):
        select_mcl_contract([contract(**changes)], "2026-09-29T00:00:00Z")


@pytest.mark.parametrize("changes", [{"bid": "NaN"}, {"ask": "1e308"}, {"bid": "72.04"},
    {"bid": "72.001"}, {"bid_size": "0"}, {"ask_size": "1.5"}, {"bid_past_low": True},
    {"ask_past_high": True}, {"source_time": 1}, {"source_time": 9999999999}])
def test_bad_quotes_remain_raw_not_usable(changes):
    state = ProbeState(IBKRConfig())
    state.selected = contract()
    state.accept(event("bidask", bidask(**changes)))
    assert state.rejected_quotes == 1 and state.quote_count == 0


def test_quote_time_order_and_clock_guards():
    state = ProbeState(IBKRConfig())
    state.selected = contract()
    state.accept(event("bidask", bidask()))
    state.accept(event("bidask", bidask(source_time=int(time.time()) - 1)))
    assert state.quote_count == 1 and state.rejected_quotes == 1
    with pytest.raises(ValueError, match="clock"):
        state.accept(event("ready", at="2000-01-01T00:00:00Z"))
    state = ProbeState(IBKRConfig())
    state.accept(event("ready", {"next_valid_id": 1}, at="2026-09-29T00:00:00Z", mono=0))
    with pytest.raises(ValueError, match="clock"):
        state.accept(event("ready", at="2026-09-29T00:00:10Z", mono=1_000_000_000))


def test_error_account_and_request_guards():
    state = ProbeState(IBKRConfig())
    for code in [2104, 2106, 2107, 2108, 2158]:
        state.accept(event("error", {"code": code}))
    for e in [event("error", {"code": 1100}), event("error", {"code": 354}),
              event("accounts", {"exact_match": False}), event("closed"),
              event("bidask", bidask()), event("contracts_end", {"reqId": 999})]:
        with pytest.raises(ValueError):
            state.accept(e)


class FakeTransport:
    def __init__(self, config, account, fault=None):
        self.events = queue.Queue()
        self.overflow = threading.Event()
        self.fault, self.closed, self.calls = fault, False, []
        self.base_ns = time.time_ns() - 1_000_000_000
        self.callback_seq = 0

    def put(self, kind, payload=None):
        self.callback_seq += 1
        elapsed = self.callback_seq * 1000
        self.events.put(event(kind, payload, at=iso_ns(self.base_ns + elapsed), mono=elapsed))

    def start(self):
        self.calls.append("start")
        self.put("accounts", {"exact_match": self.fault != "account"})
        if self.fault != "handshake":
            self.put("ready", {"next_valid_id": 1})

    def discover(self):
        self.calls.append("discover")
        expiry = datetime.now(timezone.utc) + timedelta(days=30)
        self.put("contract", {"reqId": 1001, "details": contract(
            lastTradeDateOrContractMonth=expiry.strftime("%Y%m%d"), contractMonth=expiry.strftime("%Y%m"))})
        self.put("contracts_end", {"reqId": 1001})
        if self.fault == "position":
            self.put("position", {"target_account": True, "conId": 999, "quantity": "1"})
        if self.fault == "order":
            self.put("order", {"target_account": True, "clientId": 12, "orderId": 33})
        self.put("positions_end")
        if self.fault != "incomplete":
            self.put("orders_end")

    def subscribe(self, selected):
        self.calls.append("subscribe")
        if self.fault != "no_quotes":
            self.put("bidask", bidask(source_time=self.base_ns // 10**9))
        if self.fault == "disconnect":
            self.put("closed")
        if self.fault == "entitlement":
            self.put("error", {"code": 354})
        if self.fault == "overflow":
            self.overflow.set()

    def get(self, timeout):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self.closed = True
        if self.fault == "tail_error":
            self.put("error", {"code": 1100})


def run_capture(tmp_path, fault=None):
    c = replace(IBKRConfig(), paper_session_confirmed=True, capture_seconds=1, timeout_seconds=1)
    transport = FakeTransport(c, "DU123", fault)
    report = capture_ibkr(c, tmp_path / "capture", account="DU123", transport_factory=lambda *_: transport)
    assert transport.closed
    return report, transport


def test_capture_complete_journal_and_no_secret(tmp_path):
    report, transport = run_capture(tmp_path)
    assert report["capture_checks_passed"] and report["usable_quotes"] == 1
    assert not report["trade_authorized"] and not report["market_data_qualified"]
    assert transport.calls == ["start", "discover", "subscribe"]
    rows = list(Journal(report["journal"]).records())
    assert rows[0]["kind"] == "ibkr_session_start" and rows[-1]["kind"] == "ibkr_session_end"
    assert "DU123" not in json.dumps(rows)
    assert any(r["kind"] == "ibkr_contract_selection" for r in rows)
    assert any(r["kind"] == "ibkr_callback" and r["payload"]["kind"] == "bidask" for r in rows)
    with pytest.raises(FileExistsError):
        run_capture(tmp_path)


@pytest.mark.parametrize("fault,blocker", [("account", "configured account"),
    ("handshake", "TIMEOUT"), ("incomplete", "TIMEOUT"), ("position", "ACCOUNT_NOT_FLAT"),
    ("order", "EXISTING_ACCOUNT_ORDERS"), ("no_quotes", "NO_USABLE_BIDASK"),
    ("disconnect", "connection lost"), ("entitlement", "IBKR_ERROR_354"),
    ("overflow", "CALLBACK_QUEUE_OVERFLOW"), ("tail_error", "IBKR_ERROR_1100")])
def test_capture_failure_modes(tmp_path, fault, blocker):
    report, transport = run_capture(tmp_path, fault)
    assert not report["capture_checks_passed"] and not report["trade_authorized"]
    assert blocker in " ".join(report["blockers"])
    if fault in {"account", "handshake"}:
        assert "discover" not in transport.calls


def test_capture_requires_confirmed_account_before_any_io(tmp_path):
    with pytest.raises(ValueError):
        capture_ibkr(IBKRConfig(), tmp_path / "capture", account="DU123",
                     transport_factory=lambda *_: pytest.fail("unexpected transport construction"))
    assert not (tmp_path / "capture").exists()


def test_stop_is_journaled(tmp_path):
    stop = threading.Event()
    stop.set()
    c = replace(IBKRConfig(), paper_session_confirmed=True)
    report = capture_ibkr(c, tmp_path / "capture", account="DU123", stop=stop,
                         transport_factory=FakeTransport)
    assert "INTERRUPTED" in report["blockers"]


def test_sdk_callbacks_and_order_mutations_blocked():
    class Wrapper:
        pass

    class Client:
        def __init__(self, wrapper):
            self.wrapper = wrapper

    events = []
    closing = threading.Event()
    client = make_client(Wrapper, Client, lambda kind, payload: events.append((kind, payload)), "DU123", closing)
    client.managedAccounts("U555,DU123,")
    client.error(1, 354, "secret DU123")
    client.error(1, 1720000000, 1100, "secret DU123", '{"account":"DU123"}')
    client.tickByTickBidAsk(2001, int(time.time()), 72.01, 72.03, 1, 2,
                            SimpleNamespace(bidPastLow=False, askPastHigh=False))
    assert events[0][1]["exact_match"]
    assert events[1][1]["code"] == 354 and events[2][1]["code"] == 1100
    assert "DU123" not in json.dumps(events)
    for method in ("placeOrder", "cancelOrder", "reqGlobalCancel", "exerciseOptions",
                   "placeOrderProtoBuf", "cancelOrderProtoBuf", "reqGlobalCancelProtoBuf", "exerciseOptionsProtoBuf"):
        with pytest.raises(RuntimeError, match="disabled"):
            getattr(client, method)(1, object(), object())
    client.connectionClosed()
    assert events[-1][0] == "closed"
    count = len(events)
    closing.set()
    client.connectionClosed()
    assert len(events) == count


def test_transport_requests_only_read_operations():
    class Recorder:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def call(*args):
                self.calls.append((name, args))
                return name == "isConnected"
            return call

    transport = IBKRTransport.__new__(IBKRTransport)
    transport.Contract = SimpleNamespace
    transport.client = Recorder()
    transport.closing = threading.Event()
    transport.thread = None
    transport.subscribed = False
    transport.discover()
    transport.subscribe(contract())
    transport.close()
    calls = transport.client.calls
    assert [name for name, _ in calls] == ["reqContractDetails", "reqPositions", "reqAllOpenOrders",
        "reqTickByTickData", "isConnected", "cancelTickByTickData", "cancelPositions", "disconnect"]
    assert calls[0][1][1].symbol == "MCL" and not calls[0][1][1].includeExpired
    assert calls[3][1][1].conId == 123 and calls[3][1][2:] == ("BidAsk", 0, False)


def test_cli_preflight_offline_and_missing_confirmation(tmp_path, monkeypatch, capsys):
    from oilbot.cli import main
    monkeypatch.delenv("OILBOT_IBKR_PAPER_ACCOUNT", raising=False)
    assert main(["ibkr-preflight"]) == 2
    assert not json.loads(capsys.readouterr().out)["connection_attempted"]
    with pytest.raises(SystemExit):
        main(["ibkr-capture", "--out", str(tmp_path / "run")])
    assert main(["ibkr-capture", "--connect", "--out", str(tmp_path / "run")]) == 2
    assert not (tmp_path / "run").exists()


def test_official_sdk_protobuf_decode_without_socket():
    pytest.importorskip("ibapi")
    from ibapi.decoder import Decoder
    from ibapi.protobuf.ManagedAccounts_pb2 import ManagedAccounts
    from ibapi.protobuf.NextValidId_pb2 import NextValidId
    from ibapi.protobuf.ContractData_pb2 import ContractData
    from ibapi.protobuf.TickByTickData_pb2 import TickByTickData
    from ibapi.protobuf.ErrorMessage_pb2 import ErrorMessage

    transport = IBKRTransport(IBKRConfig(), "DU123")
    decoder = Decoder(transport.client, 222)
    decoder.processManagedAccountsMsgProtoBuf(ManagedAccounts(accountsList="DU123").SerializeToString())
    decoder.processNextValidIdMsgProtoBuf(NextValidId(orderId=12).SerializeToString())
    cd = ContractData(reqId=1001)
    for key in ("conId", "symbol", "secType", "exchange", "currency", "localSymbol", "multiplier", "lastTradeDateOrContractMonth"):
        setattr(cd.contract, key, 100.0 if key == "multiplier" else contract()[key])
    cd.contractDetails.contractMonth = "202611"
    cd.contractDetails.minTick = "0.01"
    decoder.processContractDataMsgProtoBuf(cd.SerializeToString())
    tick = TickByTickData(reqId=2001, tickType=3)
    tick.historicalTickBidAsk.time = int(time.time())
    tick.historicalTickBidAsk.priceBid = 72.01
    tick.historicalTickBidAsk.priceAsk = 72.03
    tick.historicalTickBidAsk.sizeBid = "1"
    tick.historicalTickBidAsk.sizeAsk = "2"
    decoder.processTickByTickMsgProtoBuf(tick.SerializeToString())
    decoder.processErrorMsgProtoBuf(ErrorMessage(id=2001, errorCode=354, errorMsg="account DU123").SerializeToString())
    events = []
    while (e := transport.get(0)) is not None:
        events.append(e)
    assert [e["kind"] for e in events] == ["accounts", "ready", "contract", "bidask", "error"]
    assert events[2]["payload"]["details"]["conId"] == 123
    assert events[3]["payload"]["bid"] == "72.01"
    assert events[-1]["payload"]["code"] == 354
    assert "DU123" not in json.dumps(events)
    with pytest.raises(RuntimeError, match="disabled"):
        transport.client.placeOrder(1, None, None)
    assert not transport.client.isConnected()
    transport.close()


def test_stale_report_unknown_ready_and_foreign_exposure():
    state = ProbeState(IBKRConfig())
    with pytest.raises(ValueError, match="handshake"):
        state.accept(event("ready", {"next_valid_id": True}))
    state.accept(event("position", {"target_account": False, "conId": 9, "quantity": "50"}))
    state.accept(event("order", {"target_account": False, "clientId": 1, "orderId": 2}))
    assert not state.positions and not state.orders
    state.selected = contract()
    state.accept(event("bidask", bidask()))
    later = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    assert "STALE_LAST_BIDASK" in state.report(later)["blockers"]
    assert "FINAL_RECEIPT_CLOCK_REGRESSION" in state.report("2000-01-01T00:00:00Z")["blockers"]


def test_emitter_overflow_and_shutdown_boundary():
    transport = IBKRTransport.__new__(IBKRTransport)
    transport.closing = threading.Event()
    transport.overflow = threading.Event()
    transport.events = queue.Queue(maxsize=1)
    transport.emit("ready", {})
    transport.emit("ready", {})
    assert transport.overflow.is_set()
    transport.get(0)
    transport.closing.set()
    transport.emit("closed", {})
    assert transport.get(0) is None
