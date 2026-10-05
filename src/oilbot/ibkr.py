"""Bounded, read-only IBKR paper-session discovery and raw BidAsk capture.

This is deliberately not an order adapter or a qualified market archive. The
callback journal preserves receipt timing without inventing exchange sequences,
trading sessions, latency guarantees, or paper fills.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import threading
import time

from .clock import epoch_ns, instant, utc_now
from .market import atomic_json
from .schema import digest
from .store import Journal


@dataclass(frozen=True)
class IBKRConfig:
    mode: str = "paper_read_only"
    product: str = "MCL"
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 71
    account_env: str = "OILBOT_IBKR_PAPER_ACCOUNT"
    paper_session_confirmed: bool = False
    timeout_seconds: int = 15
    capture_seconds: int = 30
    roll_buffer_days: int = 7
    max_tick_age_seconds: int = 5

    def validate(self):
        if self.mode != "paper_read_only" or self.product != "MCL":
            raise ValueError("only MCL paper_read_only is supported")
        if not isinstance(self.host, str):
            raise ValueError("explicit IPv4 address string required")
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError as exc:
            raise ValueError("explicit loopback or private IB Gateway IP required") from exc
        private_networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8")
        if address.version != 4 or not any(address in ipaddress.ip_network(n) for n in private_networks):
            raise ValueError("public or wildcard broker endpoints are not supported")
        if type(self.port) is not int or self.port not in {4002, 7497}:
            raise ValueError("only default paper ports 4002 or 7497 are supported; ports alone do not prove paper mode")
        for name, low, high in (("client_id", 1, 2147483647), ("timeout_seconds", 1, 60),
                               ("capture_seconds", 1, 3600), ("roll_buffer_days", 7, 60),
                               ("max_tick_age_seconds", 1, 10)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"invalid {name}")
        if type(self.paper_session_confirmed) is not bool:
            raise ValueError("paper_session_confirmed must be boolean")
        if not isinstance(self.account_env, str) or not re.fullmatch(r"OILBOT_[A-Z0-9_]+", self.account_env):
            raise ValueError("use a task-specific OILBOT_ account environment variable")


def load_ibkr_config(path):
    try:
        config = IBKRConfig(**json.loads(Path(path).read_text()))
    except TypeError as exc:
        raise ValueError("invalid IBKR configuration fields") from exc
    config.validate()
    return config


def connection_blockers(config, account):
    config.validate()
    blockers = []
    if not config.paper_session_confirmed:
        blockers.append("PAPER_LOGIN_NOT_CONFIRMED")
    if not isinstance(account, str) or not re.fullmatch(r"DU[0-9]+", account):
        blockers.append("EXPLICIT_PAPER_ACCOUNT_REQUIRED")
    return blockers


def ibkr_preflight(config):
    blockers = connection_blockers(config, os.environ.get(config.account_env))
    if importlib.util.find_spec("ibapi") is None:
        blockers.append("OFFICIAL_IBAPI_SDK_NOT_INSTALLED")
    sdk_version = None
    if "OFFICIAL_IBAPI_SDK_NOT_INSTALLED" not in blockers:
        try:
            from ibapi import get_version_string
            from ibapi.client import EClient  # Verify protobuf/runtime imports too.
            sdk_version = get_version_string()
        except ImportError:
            blockers.append("IBAPI_SDK_IMPORT_FAILED")
    return {"mode": config.mode, "product": config.product, "config": asdict(config),
            "sdk_version": sdk_version,
            "connection_attempted": False, "connection_blockers": blockers,
            "trade_authorized": False, "broker_execution": "disabled",
            "next": "Configure a logged-in paper Gateway/TWS with Read-Only API enabled, then run ibkr-capture.",
            "note": "An account prefix and port are safeguards, not independent proof of paper mode."}


def decimal_value(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid decimal") from exc
    if not result.is_finite() or abs(result) > Decimal("1e12"):
        raise ValueError("nonfinite or unset broker numeric value")
    return result


def select_mcl_contract(rows, at, roll_buffer_days=7):
    """Nearest unique eligible expiry, never a guessed conId or continuous future.

    Dates are used conservatively: the whole reported last-trading date is inside
    the exclusion buffer. This does NOT construct a precise last_trade_at.
    """
    cutoff = instant(at).date() + timedelta(days=roll_buffer_days)
    eligible = {}
    for row in rows:
        if (row.get("symbol"), row.get("secType"), row.get("exchange"), row.get("currency")) != (
                "MCL", "FUT", "NYMEX", "USD"):
            continue
        if decimal_value(row.get("multiplier")) != 100 or decimal_value(row.get("minTick")) != Decimal(".01"):
            raise ValueError("MCL contract specification mismatch")
        cid = row.get("conId")
        if type(cid) is not int or cid <= 0 or not row.get("localSymbol"):
            raise ValueError("exact broker contract identity missing")
        month = row.get("contractMonth", "")
        expiry = row.get("lastTradeDateOrContractMonth", "").split(" ")[0]
        if not re.fullmatch(r"\d{6}", month) or not re.fullmatch(r"\d{8}", expiry):
            raise ValueError("explicit contract month and last trading date required")
        datetime.strptime(month, "%Y%m")
        last_day = datetime.strptime(expiry, "%Y%m%d").date()
        if last_day <= cutoff:
            continue
        if cid in eligible and eligible[cid] != row:
            raise ValueError("conflicting broker contract definitions")
        eligible[cid] = row
    if not eligible:
        raise ValueError("no eligible MCL expiry outside roll buffer")
    ordered = sorted(eligible.values(), key=lambda r: r["lastTradeDateOrContractMonth"][:8])
    if len(ordered) > 1 and ordered[0]["lastTradeDateOrContractMonth"][:8] == ordered[1]["lastTradeDateOrContractMonth"][:8]:
        raise ValueError("ambiguous nearest MCL expiry")
    return ordered[0]


class ProbeState:
    """Single-consumer reducer; all input callbacks are journaled before admission."""

    def __init__(self, config):
        self.config = config
        self.ready = False
        self.account_match = False
        self.ends = set()
        self.contracts = []
        self.selected = None
        self.positions = {}
        self.orders = {}
        self.quote_count = 0
        self.rejected_quotes = 0
        self.last_quote_ns = None
        self.last_source_time = None
        self.last_clock = None
        self.fatal = None

    def accept(self, event):
        at, mono = epoch_ns(event["at"]), event["monotonic_ns"]
        if self.last_clock:
            old_at, old_mono = self.last_clock
            if at < old_at or mono < old_mono or abs((at - old_at) - (mono - old_mono)) > 2_000_000_000:
                raise ValueError("receipt clock regression or step")
        self.last_clock = at, mono
        kind, p = event["kind"], event["payload"]
        if kind == "ready":
            if type(p.get("next_valid_id")) is not int or p["next_valid_id"] < 0:
                raise ValueError("invalid IBKR handshake order ID")
            self.ready = True
        elif kind == "accounts":
            if not p["exact_match"]:
                raise ValueError("configured account not returned by Gateway")
            self.account_match = True
        elif kind == "error":
            # Only explicitly documented informational farm notices are harmless.
            if p["code"] not in {2104, 2106, 2107, 2108, 2158}:
                raise ValueError(f"IBKR_ERROR_{p['code']}")
        elif kind in {"closed", "reader_failed"}:
            raise ValueError("IBKR connection lost")
        elif kind == "contract":
            if p["reqId"] != 1001 or "contracts_end" in self.ends:
                raise ValueError("unexpected contract response")
            self.contracts.append(p["details"])
        elif kind in {"contracts_end", "positions_end", "orders_end"}:
            if kind == "contracts_end" and p["reqId"] != 1001:
                raise ValueError("unexpected contract completion")
            self.ends.add(kind)
        elif kind == "position" and p["target_account"]:
            self.positions[p["conId"]] = str(decimal_value(p["quantity"]))
        elif kind == "order" and p["target_account"]:
            self.orders[(p["clientId"], p["orderId"])] = p
        elif kind == "bidask":
            if not self.selected or p["reqId"] != 2001:
                raise ValueError("unsolicited BidAsk callback")
            try:
                bid, ask = decimal_value(p["bid"]), decimal_value(p["ask"])
                sizes = [decimal_value(p[key]) for key in ("bid_size", "ask_size")]
                source_time = p["source_time"]
                if type(source_time) is not int or source_time <= 0:
                    raise ValueError("invalid provider timestamp")
                age = at / 1e9 - source_time
                if (bid > ask or any(x % Decimal(".01") for x in (bid, ask))
                        or any(x <= 0 or x != x.to_integral_value() for x in sizes)
                        or p["bid_past_low"] or p["ask_past_high"]
                        or not -1 <= age <= self.config.max_tick_age_seconds
                        or (self.last_source_time is not None and source_time < self.last_source_time)):
                    raise ValueError("unusable BidAsk callback")
            except ValueError:
                self.rejected_quotes += 1
                return
            self.last_source_time = source_time
            self.last_quote_ns = at
            self.quote_count += 1

    def report(self, at):
        blockers = []
        if self.last_clock and epoch_ns(at) < self.last_clock[0]:
            blockers.append("FINAL_RECEIPT_CLOCK_REGRESSION")
        if not self.ready or not self.account_match:
            blockers.append("ACCOUNT_HANDSHAKE_INCOMPLETE")
        if self.ends != {"contracts_end", "positions_end", "orders_end"}:
            blockers.append("BROKER_SNAPSHOT_INCOMPLETE")
        if not self.selected:
            blockers.append("CONTRACT_UNRESOLVED")
        if any(decimal_value(q) != 0 for q in self.positions.values()):
            blockers.append("ACCOUNT_NOT_FLAT")
        if self.orders:
            blockers.append("EXISTING_ACCOUNT_ORDERS")
        if not self.quote_count:
            blockers.append("NO_USABLE_BIDASK")
        elif epoch_ns(at) - self.last_quote_ns > self.config.max_tick_age_seconds * 10**9:
            blockers.append("STALE_LAST_BIDASK")
        if self.rejected_quotes:
            blockers.append("REJECTED_BIDASK_CALLBACKS")
        if self.fatal:
            blockers.append(self.fatal)
        return {"schema": "ibkr-read-only-capture-v1", "product": "MCL", "mode": self.config.mode,
                "account_matched": self.account_match, "snapshot_callbacks_complete": sorted(self.ends),
                "nonzero_positions": sum(decimal_value(q) != 0 for q in self.positions.values()),
                "open_orders_observed": len(self.orders),
                "selected_contract": self.selected, "usable_quotes": self.quote_count,
                "rejected_quotes": self.rejected_quotes, "blockers": blockers,
                "capture_checks_passed": not blockers, "trade_authorized": False,
                "broker_execution": "disabled", "market_data_qualified": False,
                "economic_evaluation": "unavailable", "sequence_scope": "local_callback_only",
                "note": "Bounded diagnostic capture, not continuous-feed qualification or OMS reconciliation."}


def capture_ibkr(config, destination, *, account, transport_factory=None, stop=None):
    """Only this explicitly invoked command can open the broker socket."""
    blockers = connection_blockers(config, account)
    if blockers:
        raise ValueError(", ".join(blockers))
    if transport_factory is None:
        from .ibkr_transport import IBKRTransport
        transport_factory = IBKRTransport
    transport = transport_factory(config, account)  # SDK import check before output creation.
    dest = Path(destination).resolve()
    dest.mkdir(parents=True, exist_ok=False)  # Never resume a broker session or overwrite a prior capture.
    state = ProbeState(config)
    store = Journal(dest / "ibkr.sqlite3")
    atomic_json(dest / "config.json", asdict(config))
    store.append("ibkr_session_start", {"config_hash": digest(asdict(config)), "trade_authorized": False,
        "sdk_version": getattr(transport, "sdk_version", "test_transport")})
    stop = stop or threading.Event()
    requested = subscribed = False
    phase_deadline = time.monotonic() + config.timeout_seconds
    try:
        transport.start()
        while True:
            if stop.is_set():
                state.fatal = "INTERRUPTED"
                break
            if transport.overflow.is_set():
                raise ValueError("CALLBACK_QUEUE_OVERFLOW")
            now = time.monotonic()
            if now >= phase_deadline:
                if not subscribed:
                    raise ValueError("BROKER_HANDSHAKE_OR_SNAPSHOT_TIMEOUT")
                break
            event = transport.get(min(.2, phase_deadline - now))
            if event:
                store.append("ibkr_callback", event, available_at=event["at"])
                state.accept(event)
            if state.ready and state.account_match and not requested:
                transport.discover()
                requested = True
                phase_deadline = time.monotonic() + config.timeout_seconds
            if not subscribed and state.ends == {"contracts_end", "positions_end", "orders_end"}:
                state.selected = select_mcl_contract(state.contracts, utc_now(), config.roll_buffer_days)
                store.append("ibkr_contract_selection", {"contract": state.selected, "policy": "nearest-expiry-buffer-v1",
                    "roll_buffer_days": config.roll_buffer_days})
                transport.subscribe(state.selected)
                subscribed = True
                phase_deadline = time.monotonic() + config.capture_seconds
    except (Exception, KeyboardInterrupt) as exc:
        # Error text from sockets/SDKs can contain account identifiers; never print it.
        state.fatal = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        state.fatal = state.fatal.replace(account, "[account]")
    finally:
        try:
            transport.close()
        except Exception:
            state.fatal = "DISCONNECT_FAILED"
    if transport.overflow.is_set():
        state.fatal = "CALLBACK_QUEUE_OVERFLOW"
    # Capture queued callbacks up to the explicit shutdown boundary, including
    # failures just before the deadline. No silent successful tail truncation.
    while (event := transport.get(0)) is not None:
        store.append("ibkr_callback", event, available_at=event["at"])
        try:
            state.accept(event)
        except (ValueError, KeyError, TypeError) as exc:
            state.fatal = str(exc).replace(account, "[account]")
    report = state.report(utc_now())
    store.append("ibkr_session_end", report)
    atomic_json(dest / "report.json", report)
    return {**report, "report": str(dest / "report.json"), "journal": str(store.path)}
