"""Durable, read-only CL1/CL2/MCL1 capture. Provider qualification stays separate.

Raw callbacks commit before normalization. Local callback sequences are never
represented as exchange continuity. The restart ledger records unobserved time.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .clock import epoch_ns, instant, iso_ns, utc_now
from .features import MarketView
from .ibkr import IBKRConfig, connection_blockers, decimal_value, ibkr_preflight
from .market import atomic_json
from .schema import InstrumentDefinition, QuoteEvent, digest
from .store import Journal, component_lock

ROLES = {2001: "CL1", 2002: "CL2", 2003: "MCL1"}
INFORMATIONAL_CODES = {2104, 2106, 2107, 2108, 2158}


@dataclass(frozen=True)
class CurveConfig(IBKRConfig):
    client_id: int = 72
    capture_seconds: int = 1800
    max_tick_age_seconds: int = 2
    stream_stale_seconds: int = 30
    reconnect_seconds: int = 30

    def validate(self):
        super().validate()
        for name, low, high in (("stream_stale_seconds", 2, 300), ("reconnect_seconds", 15, 300)):
            if type(getattr(self, name)) is not int or not low <= getattr(self, name) <= high:
                raise ValueError("invalid curve policy: " + name)


def load_curve_config(path):
    try:
        config = CurveConfig(**json.loads(Path(path).read_text()))
    except TypeError as exc:
        raise ValueError("invalid curve configuration fields") from exc
    config.validate()
    return config


def curve_preflight(config):
    result = ibkr_preflight(config)
    result.update(schema="ibkr-curve-preflight-v1", roles=list(ROLES.values()),
                  market_data_qualified=False,
                  next="Confirm paper login, Read-Only API and CL/MCL tick-by-tick entitlement before market-capture --connect.")
    return result


def local_time(text, zone):
    """Reject ambiguous/nonexistent civil times instead of guessing a DST fold."""
    if not isinstance(text, str) or not re.fullmatch(r"\d{8}:\d{4}(?::\d{2})?", text):
        raise ValueError("explicit broker date and time required")
    naive = datetime.strptime(text, "%Y%m%d:%H%M:%S" if text.count(":") == 2 else "%Y%m%d:%H%M")
    first, second = naive.replace(tzinfo=zone, fold=0), naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset() or first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
        raise ValueError("ambiguous or nonexistent broker session time")
    return first.astimezone(timezone.utc).isoformat()


def contract_definition(details, *, at):
    product = details.get("symbol")
    if (product not in {"CL", "MCL"} or details.get("secType") != "FUT"
            or details.get("exchange") != "NYMEX" or details.get("currency") != "USD"):
        raise ValueError("explicit NYMEX WTI future required")
    con_id, month = details.get("conId"), details.get("contractMonth")
    if type(con_id) is not int or con_id <= 0 or not details.get("localSymbol"):
        raise ValueError("explicit broker contract identity required")
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}(0[1-9]|1[0-2])", month):
        raise ValueError("explicit contract month required")
    if decimal_value(details.get("multiplier")) != {"CL": 1000, "MCL": 100}[product] or decimal_value(details.get("minTick")) != decimal_value("0.01"):
        raise ValueError("incorrect WTI contract specifications")
    zone_name = details.get("timeZoneId")
    if zone_name not in {"US/Central", "America/Chicago", "US/Eastern", "America/New_York", "UTC"}:
        raise ValueError("reviewed broker timezone required")
    try:
        zone = ZoneInfo({"US/Central": "America/Chicago", "US/Eastern": "America/New_York"}.get(zone_name, zone_name))
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone data unavailable") from exc
    last = details.get("lastTradeDateOrContractMonth", "")
    if not isinstance(last, str) or not re.fullmatch(r"\d{8}(?: \d{2}:\d{2}:\d{2})?", last):
        raise ValueError("actual last trading date required; expiration/month alone is insufficient")
    last_date = last[:8]
    reported_time = last[9:] if len(last) > 8 else details.get("lastTradeTime", "")
    if reported_time:
        if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", reported_time):
            raise ValueError("invalid last trading time")
        last_at = local_time(last_date + ":" + reported_time[:2] + reported_time[3:5] + ":" + reported_time[6:], zone)
        last_basis = "broker_last_trading_time"
    else:
        # Conservative start of the reported trading day, never an inferred
        # expiration time. Qualification still requires an independent review.
        last_at = local_time(last_date + ":0000", zone)
        last_basis = "conservative_start_of_last_trading_day"
    sessions = []
    for day in details.get("tradingHours", "").split(";"):
        if not day or re.fullmatch(r"\d{8}:CLOSED", day):
            continue
        # Modern TWS includes the date on both endpoints. Older ambiguous
        # overnight shorthand is refused rather than inferred.
        for interval in day.split(","):
            endpoints = interval.split("-")
            if len(endpoints) != 2:
                raise ValueError("invalid broker trading session")
            sessions.append({"open": local_time(endpoints[0], zone), "close": local_time(endpoints[1], zone)})
    sessions.sort(key=lambda s: epoch_ns(s["open"]))
    if any(epoch_ns(a["close"]) > epoch_ns(b["open"]) for a, b in zip(sessions, sessions[1:])):
        raise ValueError("overlapping broker trading sessions")
    definition = InstrumentDefinition(instrument_id=f"IBKR:{con_id}", product=product,
        month=month[:4] + "-" + month[4:], exchange="NYMEX", currency="USD",
        multiplier=str({"CL": 1000, "MCL": 100}[product]), tick_size="0.01",
        last_trade_at=last_at, sessions=sessions, available_at=at,
        vendor_id=str(con_id), broker_id=str(con_id), data_mode="realtime")
    definition.validate()
    return definition, last_basis


def select_curve(rows, *, at, roll_buffer_days=7):
    definitions, details_by_id, rejected = {}, {}, Counter()
    for details in rows:
        try:
            definition, basis = contract_definition(details, at=at)
        except (ValueError, TypeError, KeyError):
            rejected["UNUSABLE_CONTRACT_DETAILS"] += 1
            continue
        identity = definition.instrument_id
        if identity in definitions and definitions[identity] != definition:
            raise ValueError("conflicting broker contract details")
        definitions[identity] = definition
        details_by_id[identity] = {"conId": details["conId"], "definition": asdict(definition), "last_trade_basis": basis}
    cutoff = epoch_ns(at) + roll_buffer_days * 86400 * 10**9
    eligible = [d for d in definitions.values() if epoch_ns(d.last_trade_at) > cutoff
                and any(epoch_ns(s["close"]) > epoch_ns(at) for s in d.sessions)]
    cl = sorted((d for d in eligible if d.product == "CL"), key=lambda d: (d.month, d.instrument_id))
    if len(cl) < 2 or len({d.month for d in cl}) != len(cl):
        raise ValueError("two unambiguous eligible CL months required")
    micro = [d for d in eligible if d.product == "MCL" and d.month == cl[0].month]
    if len(micro) != 1:
        raise ValueError("one MCL matching the selected CL front month required")
    return {role: details_by_id[d.instrument_id] for role, d in zip(("CL1", "CL2", "MCL1"), (cl[0], cl[1], micro[0]))}, dict(rejected)


def append_canonical(journal, kind, payload, source_id):
    envelope = {"schema": "curve-canonical-v1", "kind": kind, "payload": payload, "source_record_id": source_id}
    return journal.append("curve_record", envelope, record_id=digest(["curve_record", envelope]), available_at=payload["available_at"])


class CurveState:
    def __init__(self, config, journal, session_id):
        self.config, self.journal, self.session_id = config, journal, session_id
        self.ready = self.accounts = False
        self.ends, self.contracts, self.selected = set(), [], None
        self.quotes, self.rejected = Counter(), Counter()
        self.last_quotes, self.clock = {}, None

    def gap(self, reason, at, source_id, instrument=None, start=None, end=None):
        return append_canonical(self.journal, "gap", {"available_at": at, "instrument_id": instrument,
            "reason": reason, "start_at": start or at, "end_at": end or at,
            "session_id": self.session_id}, source_id)

    def accept(self, event, raw_id, *, available_at):
        at, mono = epoch_ns(event["at"]), event["monotonic_ns"]
        if type(mono) is not int or mono < 0 or at > epoch_ns(available_at):
            raise ValueError("INVALID_CALLBACK_CLOCK")
        if self.clock:
            wall_delta, mono_delta = at - self.clock[0], mono - self.clock[1]
            if mono_delta < 0 or abs(wall_delta - mono_delta) > 2 * 10**9:
                raise ValueError("CALLBACK_CLOCK_STEP")
        self.clock = at, mono
        kind, p = event["kind"], event["payload"]
        if kind == "ready":
            self.ready = True
        elif kind == "accounts":
            if p.get("exact_match") is not True:
                raise ValueError("PAPER_ACCOUNT_MISMATCH")
            self.accounts = True
        elif kind == "error":
            if p.get("code") not in INFORMATIONAL_CODES:
                raise ValueError("IBKR_ERROR:" + str(p.get("code")))
        elif kind in {"closed", "reader_failed"}:
            raise ValueError("BROKER_STREAM_DISCONNECTED")
        elif kind == "contract":
            if p.get("reqId") not in {1001, 1002}:
                raise ValueError("UNEXPECTED_CONTRACT_REQUEST")
            if p["details"].get("symbol") != ("CL" if p["reqId"] == 1001 else "MCL"):
                raise ValueError("CONTRACT_REQUEST_PRODUCT_MISMATCH")
            self.contracts.append(p["details"])
        elif kind == "contracts_end":
            if p.get("reqId") not in {1001, 1002}:
                raise ValueError("UNEXPECTED_CONTRACT_REQUEST")
            self.ends.add(p["reqId"])
        elif kind in {"positions_end", "orders_end"}:
            self.ends.add(kind)
        elif kind == "position" and p.get("target_account"):
            if decimal_value(p["quantity"]) != 0:
                raise ValueError("PAPER_ACCOUNT_NOT_FLAT")
        elif kind == "order" and p.get("target_account"):
            raise ValueError("PAPER_ACCOUNT_HAS_OPEN_ORDERS")
        elif kind == "bidask":
            role = ROLES.get(p.get("reqId"))
            if not role or not self.selected:
                raise ValueError("UNEXPECTED_QUOTE_SUBSCRIPTION")
            definition = InstrumentDefinition(**self.selected[role]["definition"])
            key = "curve_sequence:" + definition.instrument_id
            # Count all locally observed quotes, including rejects. A restart
            # cannot conceal an invalid quote by reusing an old sequence.
            with self.journal.transaction() as db:
                previous = db.execute("SELECT value FROM cursors WHERE key=?", (key,)).fetchone()
                sequence = (json.loads(previous[0]) if previous else 0) + 1
                self.journal.set_cursor(db, key, sequence)
            try:
                bid, ask = decimal_value(p["bid"]), decimal_value(p["ask"])
                sizes = [decimal_value(p[n]) for n in ("bid_size", "ask_size")]
                source = p["source_time"]
                if type(source) is not int or source <= 0:
                    raise ValueError("invalid source time")
                source_ns = source * 10**9
                if not 0 <= epoch_ns(available_at) - source_ns <= self.config.max_tick_age_seconds * 10**9:
                    raise ValueError("stale or future source quote")
                if self.last_quotes.get(role, (0, 0))[1] > source_ns:
                    raise ValueError("source time regression")
                if min(bid, ask) <= 0 or any(v <= 0 or v != v.to_integral_value() for v in sizes):
                    raise ValueError("nonpositive price or nonintegral depth")
                if p.get("bid_past_low") is not False or p.get("ask_past_high") is not False:
                    raise ValueError("flagged quote")
                source_at = iso_ns(source_ns)
                quote = QuoteEvent(instrument_id=definition.instrument_id, available_at=available_at,
                    bid=str(bid), ask=str(ask), bid_size=int(sizes[0]), ask_size=int(sizes[1]),
                    bid_at=source_at, ask_at=source_at, sequence=sequence,
                    data_mode="realtime", subscription="ibkr-tick-by-tick-BidAsk",
                    source_at=source_at, provider="ibkr", contract_month=definition.month,
                    local_receive_ns=at, local_available_ns=epoch_ns(available_at),
                    availability_basis="local_processing", sequence_scope="local_callback_instrument",
                    source_record_id=raw_id)
                quote.validate(definition)
                if not any(epoch_ns(s["open"]) <= source_ns < epoch_ns(s["close"]) for s in definition.sessions):
                    raise ValueError("quote outside reported trading sessions")
            except (ValueError, KeyError, TypeError):
                self.rejected[role] += 1
                self.gap("REJECTED_QUOTE", available_at, raw_id, definition.instrument_id)
                return
            append_canonical(self.journal, "market", asdict(quote), raw_id)
            self.quotes[role] += 1
            self.last_quotes[role] = epoch_ns(available_at), source_ns


def capture_curve(config, root, *, account=None, stop=None, transport_factory=None):
    config.validate()
    blockers = connection_blockers(config, account)
    if blockers:
        return {"schema": "ibkr-curve-capture-v1", "connection_attempted": False,
            "blockers": blockers, "market_data_qualified": False, "trade_authorized": False}
    if transport_factory is None:
        from .ibkr_transport import IBKRCurveTransport
        transport_factory = IBKRCurveTransport
    stop = stop or threading.Event()
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with component_lock(root, "curve"):
        journal = Journal(root / "market.sqlite3")
        with journal.connect() as db:
            db.execute("CREATE INDEX IF NOT EXISTS curve_available ON records(available_at,seq) WHERE kind='curve_record'")
            db.execute("CREATE INDEX IF NOT EXISTS curve_selection_session ON records(json_extract(payload,'$.session_id'),seq) WHERE kind='curve_selection'")
        previous = journal.cursor("curve_session")
        started = utc_now()
        session_id = journal.append("curve_session_start", {"schema": "ibkr-curve-session-v1",
            "config": asdict(config), "started_at": started, "trade_authorized": False}, available_at=started)
        state = CurveState(config, journal, session_id)
        if previous:
            state.gap("UNCLEAN_RESTART" if previous["state"] == "RUNNING" else "RECONNECT_UNOBSERVED",
                started, session_id, start=previous["at"], end=started)
        with journal.transaction() as db:
            journal.set_cursor(db, "curve_session", {"state": "RUNNING", "at": started, "id": session_id})
        transport, discovered, subscribed = None, False, False
        deadline = time.monotonic() + config.timeout_seconds
        capture_end = None
        try:
            transport = transport_factory(config, account)
            journal.append("curve_sdk", {"session_id": session_id, "sdk_version": transport.sdk_version})
            transport.start()
            while not stop.is_set():
                if transport.overflow.is_set():
                    raise ValueError("CALLBACK_QUEUE_OVERFLOW")
                event = transport.get(timeout=0.1)
                if event is not None:
                    raw_id = journal.append("curve_raw", {"session_id": session_id, "event": event}, available_at=event["at"])
                    state.accept(event, raw_id, available_at=utc_now())
                if state.ready and state.accounts and not discovered:
                    transport.discover()
                    discovered = True
                if not subscribed and state.ends >= {1001, 1002, "positions_end", "orders_end"}:
                    selected_at = utc_now()
                    state.selected, rejected = select_curve(state.contracts, at=selected_at, roll_buffer_days=config.roll_buffer_days)
                    selection_id = journal.append("curve_selection", {"session_id": session_id,
                        "roles": state.selected, "rejected_details": rejected}, available_at=selected_at)
                    for value in state.selected.values():
                        append_canonical(journal, "instrument", value["definition"], selection_id)
                    transport.subscribe(state.selected)
                    subscribed = True
                    deadline = time.monotonic() + config.timeout_seconds
                    capture_end = time.monotonic() + config.capture_seconds
                if subscribed and len(state.last_quotes) == 3:
                    deadline = None
                    now_ns = epoch_ns(utc_now())
                    if any(now_ns - value[0] > config.stream_stale_seconds * 10**9 for value in state.last_quotes.values()):
                        raise ValueError("STREAM_STALE")
                if deadline is not None and time.monotonic() >= deadline:
                    raise ValueError("HANDSHAKE_OR_QUOTES_TIMEOUT")
                if capture_end is not None and time.monotonic() >= capture_end:
                    break
        except (ValueError, RuntimeError, OSError) as exc:
            # Only our fixed diagnostics are retained. External exception text
            # can contain a private account identifier or connection details.
            message = str(exc)
            allowed = {"CALLBACK_QUEUE_OVERFLOW", "CALLBACK_CLOCK_STEP", "INVALID_CALLBACK_CLOCK",
                "PAPER_ACCOUNT_MISMATCH", "PAPER_ACCOUNT_NOT_FLAT", "PAPER_ACCOUNT_HAS_OPEN_ORDERS",
                "BROKER_STREAM_DISCONNECTED", "STREAM_STALE", "HANDSHAKE_OR_QUOTES_TIMEOUT"}
            blockers.append(message if message in allowed or re.fullmatch(r"IBKR_ERROR:\d+", message) else "CURVE_CAPTURE_FAILED")
        finally:
            if transport is not None:
                try:
                    transport.close()
                    while (event := transport.get(timeout=0)) is not None:
                        raw_id = journal.append("curve_raw", {"session_id": session_id, "event": event}, available_at=event["at"])
                        # Tail errors must not disappear behind a normal stop.
                        if event["kind"] in {"error", "reader_failed", "closed"}:
                            try:
                                state.accept(event, raw_id, available_at=utc_now())
                            except ValueError:
                                blockers.append("BROKER_ERROR_DURING_SHUTDOWN")
                    if transport.overflow.is_set():
                        blockers.append("CALLBACK_QUEUE_OVERFLOW")
                except (ValueError, RuntimeError, OSError):
                    blockers.append("SDK_SHUTDOWN_FAILED")
            ended = utc_now()
            for role in ROLES.values():
                if not state.quotes[role]:
                    blockers.append("NO_ACCEPTED_QUOTES:" + role)
            end_id = journal.append("curve_session_end", {"session_id": session_id, "ended_at": ended,
                "blockers": sorted(set(blockers)), "quotes": dict(state.quotes), "rejected_quotes": dict(state.rejected)}, available_at=ended)
            state.gap("CAPTURE_STOPPED" if stop.is_set() else "CAPTURE_FAILED" if blockers else "CAPTURE_ROLLOVER",
                ended, end_id, start=min((iso_ns(v[0]) for v in state.last_quotes.values()), default=started), end=ended)
            with journal.transaction() as db:
                journal.set_cursor(db, "curve_session", {"state": "STOPPED", "at": ended, "id": session_id})
        report = {"schema": "ibkr-curve-capture-v1", "session_id": session_id, "connection_attempted": True,
            "started_at": started, "ended_at": ended, "roles": state.selected,
            "quotes": dict(state.quotes), "rejected_quotes": dict(state.rejected),
            "blockers": sorted(set(blockers)), "market_data_qualified": False,
            "sequence_scope": "local_callback_instrument", "trade_authorized": False,
            "broker_execution": "disabled", "qualification_blockers": ["ENTITLEMENT_AND_RETENTION_REVIEW_REQUIRED",
                "INDEPENDENT_CONTRACT_AND_SESSION_REVIEW_REQUIRED", "SEVEN_DAY_CAPTURE_AND_RECOVERY_REVIEW_REQUIRED"]}
        atomic_json(root / "capture-status.json", report)
        return report


def watch_curve(config, root, *, account=None, stop=None, once=False, transport_factory=None):
    stop = stop or threading.Event()
    if connection_blockers(config, account):
        return capture_curve(config, root, account=account, stop=stop, transport_factory=transport_factory)
    result = None
    while not stop.is_set():
        result = capture_curve(config, root, account=account, stop=stop, transport_factory=transport_factory)
        if once or "PAPER_ACCOUNT_MISMATCH" in result["blockers"]:
            return result
        stop.wait(config.reconnect_seconds)
    return result or {"state": "STOPPED", "trade_authorized": False}


def read_curve(path, *, through, start=None):
    """Read-only, checksum-bound canonical rows. Never initializes an absent DB."""
    target = Path(path).resolve()
    with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        arguments = [instant(through).isoformat(timespec="microseconds")]
        recent = ""
        if start:
            recent = " AND (json_extract(payload,'$.kind')='instrument' OR available_at>=? OR (json_extract(payload,'$.kind')='gap' AND json_extract(payload,'$.payload.end_at') IS NULL))"
            arguments.append(instant(start).isoformat(timespec="microseconds"))
        rows = db.execute("SELECT * FROM records WHERE kind='curve_record' AND available_at<=?" + recent + " ORDER BY seq", arguments).fetchall()
        records, gaps = [], []
        selection_cache = {}
        for row in rows:
            envelope = json.loads(row["payload"])
            if envelope.get("schema") != "curve-canonical-v1" or digest(["curve_record", envelope]) != row["id"]:
                raise ValueError("changed curve record identity")
            p = envelope["payload"]
            if epoch_ns(p["available_at"]) > epoch_ns(through):
                continue
            if epoch_ns(row["available_at"]) != epoch_ns(p["available_at"]):
                raise ValueError("curve availability binding mismatch")
            source = db.execute("SELECT kind,seq,available_at,payload FROM records WHERE id=?", (envelope["source_record_id"],)).fetchone()
            if source is None or source["seq"] >= row["seq"] or epoch_ns(source["available_at"]) > epoch_ns(p["available_at"]):
                raise ValueError("missing or future raw curve input")
            kind = envelope["kind"]
            if kind == "market":
                raw = json.loads(source["payload"])
                if source["kind"] != "curve_raw" or p.get("source_record_id") != envelope["source_record_id"] or raw["event"]["kind"] != "bidask":
                    raise ValueError("quote raw binding mismatch")
                callback = raw["event"]
                original = callback["payload"]
                if (ROLES.get(original.get("reqId")) is None
                        or p["local_receive_ns"] != epoch_ns(callback["at"])
                        or epoch_ns(p["source_at"]) != original["source_time"] * 10**9
                        or any(decimal_value(p[key]) != decimal_value(original[key]) for key in ("bid", "ask", "bid_size", "ask_size"))):
                    raise ValueError("canonical quote differs from raw callback")
                session = raw["session_id"]
                if session not in selection_cache:
                    selection = db.execute("SELECT seq,payload FROM records WHERE kind='curve_selection' AND json_extract(payload,'$.session_id')=? ORDER BY seq DESC LIMIT 1", (session,)).fetchone()
                    selection_cache[session] = (selection[0], json.loads(selection[1])) if selection else None
                selection = selection_cache[session]
                if selection is None or selection[0] >= row["seq"] or selection[1]["roles"][ROLES[original["reqId"]]]["definition"]["instrument_id"] != p["instrument_id"]:
                    raise ValueError("quote contract differs from selected subscription")
            elif kind == "instrument":
                selection = json.loads(source["payload"])
                if source["kind"] != "curve_selection" or not any(value["definition"] == p for value in selection["roles"].values()):
                    raise ValueError("definition selection binding mismatch")
            if start and kind == "market" and epoch_ns(p["available_at"]) < epoch_ns(start):
                continue
            if kind == "gap":
                if not start or epoch_ns(p.get("end_at") or p["available_at"]) >= epoch_ns(start):
                    gaps.append(p)
            elif kind in {"instrument", "market"}:
                records.append({"id": row["id"], "kind": kind, "payload": p})
            else:
                raise ValueError("unknown curve record kind")
    return {"records": records, "gaps": gaps}


def curve_health(path, *, at=None):
    at = at or utc_now()
    market = read_curve(path, through=at, start=iso_ns(epoch_ns(at) - 600 * 10**9))
    view = MarketView(market["records"], market["gaps"])
    roles = view.roles(at, roll_days=7)
    reasons = []
    for role, instrument in roles.items():
        if not instrument:
            reasons.append("MISSING_ROLE:" + role)
        elif view.quote(instrument, at) is None:
            reasons.append("NO_FRESH_QUOTE:" + role)
    return {"schema": "curve-health-v1", "checked_at": at, "state": "DEGRADED" if reasons else "RECORDING",
        "roles": roles, "reason_codes": reasons, "recent_gaps": len(market["gaps"]),
        "market_data_qualified": False, "trade_authorized": False, "broker_execution": "disabled"}


def export_curve(path, destination, *, through):
    if epoch_ns(through) > epoch_ns(utc_now()):
        raise ValueError("cannot export future curve data")
    target, source = Path(destination).resolve(), Path(path).resolve()
    if target.exists() or source.parent == target or source.parent in target.parents:
        raise ValueError("new output outside the market journal root required")
    market = read_curve(source, through=through)
    MarketView(market["records"], market["gaps"])
    target.mkdir(parents=True)
    atomic_json(target / "market.json", market)
    from .databento import file_hash
    manifest = {"schema": "curve-snapshot-v1", "through": through, "records": len(market["records"]),
        "gaps": len(market["gaps"]), "market_hash": digest(market), "files": {"market.json": file_hash(target / "market.json")},
        "dataset_role": "unqualified_provider_capture", "market_data_qualified": False, "trade_authorized": False}
    atomic_json(target / "manifest.json", manifest)
    return manifest


def load_curve_snapshot(path):
    from .databento import file_hash
    target = Path(path).resolve()
    manifest = json.loads(target.read_text())
    if manifest.get("schema") != "curve-snapshot-v1" or manifest.get("market_data_qualified") is not False or manifest.get("trade_authorized") is not False:
        raise ValueError("unqualified read-only curve snapshot required")
    if manifest.get("files") != {"market.json": file_hash(target.parent / "market.json")}:
        raise ValueError("curve snapshot checksum mismatch")
    market = json.loads((target.parent / "market.json").read_text())
    if digest(market) != manifest["market_hash"]:
        raise ValueError("curve reconstruction mismatch")
    MarketView(market["records"], market["gaps"])
    return manifest, market
