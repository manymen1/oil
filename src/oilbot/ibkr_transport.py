"""Optional official TWS SDK boundary. No SDK import during offline work."""
from __future__ import annotations

import queue
import threading
import time

from .clock import utc_now


def make_client(EWrapper, EClient, emit, account, closing):
    class ReadOnlyClient(EWrapper, EClient):
        def __init__(self):
            EWrapper.__init__(self)
            EClient.__init__(self, self)

        def nextValidId(self, orderId):
            emit("ready", {"next_valid_id": orderId})

        def managedAccounts(self, accountsList):
            accounts = [a.strip() for a in accountsList.split(",") if a.strip()]
            emit("accounts", {"exact_match": account in accounts, "count": len(accounts)})

        def connectionClosed(self):
            if not closing.is_set():
                emit("closed", {})

        def error(self, reqId, *args):
            # IB API >=10.33 inserts errorTime before errorCode; older SDKs do not.
            if len(args) >= 3 and isinstance(args[1], int):
                error_time, code = args[:2]
            elif len(args) >= 2 and isinstance(args[0], int):
                error_time, code = None, args[0]
            else:
                error_time, code = None, -1
            # Omit messages and advanced-reject JSON, which may expose account IDs.
            emit("error", {"reqId": reqId, "code": code, "error_time": error_time})

        def contractDetails(self, reqId, details):
            c = details.contract
            payload = {k: getattr(c, k, "") for k in ("conId", "symbol", "secType", "exchange", "currency",
                "localSymbol", "tradingClass", "lastTradeDateOrContractMonth")}
            payload["multiplier"] = str(c.multiplier)
            payload.update({k: getattr(details, k, "") for k in ("contractMonth", "realExpirationDate",
                "timeZoneId", "tradingHours", "liquidHours")})
            payload["minTick"] = str(details.minTick)
            emit("contract", {"reqId": reqId, "details": payload})

        def contractDetailsEnd(self, reqId):
            emit("contracts_end", {"reqId": reqId})

        def position(self, accountName, contract, position, avgCost):
            emit("position", {"target_account": accountName == account, "conId": contract.conId,
                              "quantity": str(position)})

        def positionEnd(self):
            emit("positions_end", {})

        def openOrder(self, orderId, contract, order, orderState):
            emit("order", {"target_account": order.account == account, "conId": contract.conId,
                "orderId": orderId, "clientId": order.clientId, "status": orderState.status})

        def openOrderEnd(self):
            emit("orders_end", {})

        def tickByTickBidAsk(self, reqId, time, bidPrice, askPrice, bidSize, askSize, tickAttribBidAsk):
            emit("bidask", {"reqId": reqId, "source_time": time, "bid": str(bidPrice), "ask": str(askPrice),
                "bid_size": str(bidSize), "ask_size": str(askSize),
                "bid_past_low": bool(tickAttribBidAsk.bidPastLow), "ask_past_high": bool(tickAttribBidAsk.askPastHigh)})

        def _no_orders(self, *args, **kwargs):
            raise RuntimeError("broker order operations are disabled in this read-only adapter")

        placeOrder = cancelOrder = reqGlobalCancel = exerciseOptions = _no_orders
        placeOrderProtoBuf = cancelOrderProtoBuf = reqGlobalCancelProtoBuf = exerciseOptionsProtoBuf = _no_orders

    return ReadOnlyClient()


class IBKRTransport:
    def __init__(self, config, account):
        try:
            from ibapi import get_version_string
            from ibapi.client import EClient
            from ibapi.wrapper import EWrapper
            from ibapi.contract import Contract
        except ImportError as exc:
            raise RuntimeError("Install the official IBKR TWS Python SDK; see docs/ibkr-paper.md") from exc
        self.config, self.Contract = config, Contract
        self.sdk_version = get_version_string()
        self.events = queue.Queue(maxsize=10000)
        self.overflow = threading.Event()
        self.closing = threading.Event()
        self.client = make_client(EWrapper, EClient, self.emit, account, self.closing)
        self.thread = None
        self.subscribed = False

    def emit(self, kind, payload):
        if self.closing.is_set():
            return
        try:
            self.events.put_nowait({"kind": kind, "payload": payload,
                "at": utc_now(), "monotonic_ns": time.monotonic_ns()})
        except queue.Full:
            self.overflow.set()

    def start(self):
        # connect() can block in the SDK handshake. Run it off the consumer thread
        # so our deadline still applies; disconnect closes its socket on timeout.
        def reader():
            try:
                self.client.connect(self.config.host, self.config.port, self.config.client_id)
                if not self.closing.is_set():
                    self.client.run()
            except Exception:
                if not self.closing.is_set():
                    self.emit("reader_failed", {})
            finally:
                self.client.disconnect()
        self.thread = threading.Thread(target=reader, name="ibkr-read-only", daemon=True)
        self.thread.start()

    def get(self, timeout):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def discover(self):
        contract = self.Contract()
        contract.symbol, contract.secType, contract.exchange = "MCL", "FUT", "NYMEX"
        contract.currency, contract.multiplier = "USD", "100"
        contract.includeExpired = False
        self.client.reqContractDetails(1001, contract)
        self.client.reqPositions()
        self.client.reqAllOpenOrders()  # Does not bind or cancel other clients' orders.

    def subscribe(self, selected):
        contract = self.Contract()
        contract.conId, contract.exchange = selected["conId"], "NYMEX"
        self.client.reqTickByTickData(2001, contract, "BidAsk", 0, False)
        self.subscribed = True

    def close(self):
        self.closing.set()
        try:
            if self.client.isConnected():
                if self.subscribed:
                    self.client.cancelTickByTickData(2001)
                self.client.cancelPositions()
        finally:
            self.client.disconnect()
            if self.thread:
                self.thread.join(timeout=2)
                if self.thread.is_alive():
                    raise RuntimeError("SDK reader did not stop")
