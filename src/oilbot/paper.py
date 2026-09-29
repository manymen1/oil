"""Deterministic local paper execution. Never connects to a broker.

One explicit contract per account; fills require a subsequent fresh quote.
All limits and costs are engineering assumptions, not trading recommendations.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .clock import instant, epoch_ns, iso_ns
from .market import atomic_json
from .schema import InstrumentDefinition, MarketEvent, MarketStatusEvent, digest
from .store import Journal, component_lock


@dataclass(frozen=True)
class PaperLimits:
    contracts: int = 1
    max_contracts: int = 1
    max_trades_per_day: int = 5
    daily_loss_usd: str = "100"
    fee_per_side: str = "1"
    slippage_ticks: int = 1
    max_spread_ticks: int = 5
    stop_ticks: int = 20
    take_profit_ticks: int = 40
    max_hold_seconds: int = 1800
    order_delay_seconds: int = 1
    order_ttl_seconds: int = 10
    max_quote_age_seconds: int = 2
    expiry_buffer_seconds: int = 3600
    max_notional_usd: str = "1000000"
    cooldown_seconds: int = 1

    def validate(self):
        for key, value in asdict(self).items():
            if key in {"daily_loss_usd", "fee_per_side", "max_notional_usd"}:
                if not isinstance(value, str):
                    raise ValueError("decimal string required for paper limit: " + key)
                try:
                    amount = Decimal(value)
                except (InvalidOperation, TypeError) as exc:
                    raise ValueError("invalid paper limit: " + key) from exc
                if not amount.is_finite() or amount < 0 or (key != "fee_per_side" and amount == 0):
                    raise ValueError("invalid paper limit: " + key)
            elif type(value) is not int or value < (0 if key == "slippage_ticks" else 1):
                raise ValueError("invalid paper limit: " + key)
        if self.contracts > self.max_contracts:
            raise ValueError("contracts exceed position limit")
        if self.order_delay_seconds >= self.order_ttl_seconds:
            raise ValueError("order delay must be shorter than order TTL")


class PaperEngine:
    def __init__(self, store: Journal, definition: InstrumentDefinition, limits: PaperLimits):
        definition.validate()
        limits.validate()
        self.store, self.definition, self.limits = store, definition, limits
        with store.connect() as db:
            foreign = db.execute("SELECT kind FROM records WHERE kind NOT IN ('paper_input','paper_event','paper_account') LIMIT 1").fetchone()
        if foreign:
            raise ValueError("dedicated paper journal required")
        self.identity = digest({"definition": asdict(definition), "limits": asdict(limits), "engine": "paper-v2"})

    def _state(self, db=None):
        if db is None:
            with self.store.connect() as conn:
                return self._state(conn)
        row = db.execute("SELECT value FROM cursors WHERE key='paper:state'").fetchone()
        import json
        state = json.loads(row[0]) if row else None
        if state and state["identity"] != self.identity:
            raise ValueError("paper account configuration changed; use a new journal")
        last = db.execute("SELECT payload FROM records WHERE kind='paper_account' ORDER BY seq DESC LIMIT 1").fetchone()
        if (last is not None) != (state is not None) or last and json.loads(last[0])["state"] != state:
            raise ValueError("paper account reconciliation failed")
        return state or {"identity": self.identity, "at": None, "quote": None, "mark_quote": None,
                         "position": None, "pending": None, "cash_pnl": "0",
                         "day": None, "day_start_equity": "0", "trades_today": 0,
                         "loss_halt": False, "manual_halt": False, "close_reason": None,
                         "market_blocked": False, "last_exit_at": None, "episodes": [], "last_quote_time": None}

    def _equity(self, state):
        value = Decimal(state["cash_pnl"])
        position, quote = state["position"], state["mark_quote"]
        if position and quote:
            price = quote["bid"] if position["side"] == 1 else quote["ask"]
            value += (Decimal(price) - Decimal(position["price"])) * position["side"] * position["quantity"] * Decimal(self.definition.multiplier)
        return value

    def _session(self, at):
        return any(instant(s["open"]) <= at < instant(s["close"]) for s in self.definition.sessions)

    def _entry_window(self, at):
        end = at + timedelta(seconds=self.limits.max_hold_seconds + self.limits.order_delay_seconds)
        return (instant(self.definition.available_at) <= at
                and end < instant(self.definition.last_trade_at) - timedelta(seconds=self.limits.expiry_buffer_seconds)
                and any(instant(s["open"]) <= at <= end < instant(s["close"]) for s in self.definition.sessions))

    def _fresh(self, quote, at):
        return (quote and quote["bid"] is not None and quote["ask"] is not None
                and quote["data_mode"] in {"fixture", "historical", "realtime"}
                and not quote.get("flags", 0) & (4 | 8 | 32)
                and min(quote["bid_size"], quote["ask_size"]) > 0
                and quote.get("event_type", "quote") == "quote"
                and all(0 <= (at - instant(quote[key])).total_seconds() <= self.limits.max_quote_age_seconds
                        for key in ("available_at", "bid_at", "ask_at")))

    def process(self, kind: str, payload: dict, *, at: str, event_id: str):
        """Apply an input once, atomically with fills and account state.

        The caller must serialize event time. Identical delivery is a no-op;
        reusing an ID with different data and retrograde time are rejected.
        """
        when = instant(at)
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("nonempty event ID required")
        input_hash = digest([kind, payload, at])
        with self.store.transaction() as db:
            previous = db.execute("SELECT value FROM cursors WHERE key=?", ("paper:event:" + event_id,)).fetchone()
            import json
            prior = json.loads(previous[0]) if previous else None
            if prior:
                if prior != input_hash:
                    raise ValueError("paper event identity collision")
                return self._state(db)
            state = self._state(db)
            if state["at"] and epoch_ns(at) < epoch_ns(state["at"]):
                raise ValueError("paper events must be chronological")
            day = when.date().isoformat()
            if state["day"] != day:
                state.update(day=day, day_start_equity=str(self._equity(state)), trades_today=0, loss_halt=False)
            events = []

            def emit(event, **fields):
                events.append({"event": event, "source_event_id": event_id, **fields})

            def cancel(reason):
                if state["pending"]:
                    emit("ORDER_CANCELLED", reason=reason, order_id=state["pending"]["order_id"], signal_id=state["pending"]["signal_id"])
                    state["pending"] = None

            if kind == "quote":
                quote = MarketEvent(**payload)
                quote.validate(self.definition)
                if epoch_ns(quote.available_at) != epoch_ns(at):
                    raise ValueError("quote time mismatch")
                if quote.sequence_scope != "instrument":
                    raise ValueError("paper requires instrument-scoped quote sequences")
                old = state["quote"]
                if old and quote.sequence <= old["sequence"]:
                    raise ValueError("non-increasing quote sequence")
                if old and quote.sequence != old["sequence"] + 1:
                    state["manual_halt"] = True
                    cancel("QUOTE_SEQUENCE_GAP")
                    emit("HALTED", reason="QUOTE_SEQUENCE_GAP")
                if old and any(asdict(quote).get(k) != old.get(k) for k in ("provider", "subscription", "data_mode", "sequence_scope")):
                    raise ValueError("quote stream identity changed")
                state["quote"] = asdict(quote)
                state["last_quote_time"] = at
            elif kind == "status":
                status = MarketStatusEvent(**payload)
                status.validate(self.definition)
                if epoch_ns(status.available_at) != epoch_ns(at):
                    raise ValueError("status time mismatch")
                state["market_blocked"] = status.status != "RESUMED"
                if state["market_blocked"]:
                    state["manual_halt"] = True
                    state["close_reason"] = "MARKET_" + status.status
                    cancel(state["close_reason"])
                # Never reuse the pre-halt quote after RESUMED.
                state["quote"] = None
                emit("MARKET_STATUS", status=status.status)
            elif kind == "cancel":
                if state["pending"] and payload.get("order_id") == state["pending"]["order_id"]:
                    cancel("REQUESTED_CANCEL")
                else:
                    emit("CANCEL_REJECTED", reason="ORDER_NOT_WORKING", order_id=payload.get("order_id"))
            elif kind == "halt":
                state["manual_halt"] = True
                cancel("MANUAL_HALT")
                state["close_reason"] = "MANUAL_HALT"
                emit("HALTED", reason="MANUAL_HALT")
            elif kind not in {"signal", "clock"}:
                raise ValueError("unknown paper input")

            quote = state["quote"]
            fresh = not state["market_blocked"] and self._fresh(quote, when)
            if state["pending"] and (state["manual_halt"] or state["loss_halt"]):
                cancel("ACCOUNT_HALTED")
            if fresh:
                state["mark_quote"] = quote
            if fresh and self._equity(state) - Decimal(state["day_start_equity"]) <= -Decimal(self.limits.daily_loss_usd):
                state["loss_halt"] = True
                cancel("DAILY_LOSS_LIMIT")
                state["close_reason"] = "DAILY_LOSS_LIMIT"

            position = state["position"]
            if position and fresh:
                mark = self.definition.ticks(quote["bid"] if position["side"] == 1 else quote["ask"])
                change = position["side"] * (mark - self.definition.ticks(position["price"]))
                held = (when - instant(position["opened_at"])).total_seconds()
                if state["manual_halt"]:
                    state["close_reason"] = state["close_reason"] or "MANUAL_HALT"
                elif change <= -self.limits.stop_ticks:
                    state["close_reason"] = state["close_reason"] or "STOP_LOSS"
                elif change >= self.limits.take_profit_ticks:
                    state["close_reason"] = state["close_reason"] or "TAKE_PROFIT"
                elif held >= self.limits.max_hold_seconds:
                    state["close_reason"] = state["close_reason"] or "MAX_HOLD"
                elif when >= instant(self.definition.last_trade_at) - timedelta(seconds=self.limits.expiry_buffer_seconds):
                    state["close_reason"] = state["close_reason"] or "EXPIRY_BUFFER"
                if state["close_reason"] and kind == "quote" and self._session(when) and when < instant(self.definition.last_trade_at):
                    quantity = min(position["quantity"], quote["bid_size"] if position["side"] == 1 else quote["ask_size"])
                    if quantity:
                        exit_id = digest([position["order_id"], "exit", state["close_reason"]])
                        if not position.get("exit_order_id"):
                            position["exit_order_id"] = exit_id
                            emit("ORDER_SUBMITTED", order_id=exit_id, reduce_only=True, quantity=position["quantity"])
                            emit("ORDER_ACKNOWLEDGED", order_id=exit_id)
                        price = (mark - position["side"] * self.limits.slippage_ticks) * Decimal(self.definition.tick_size)
                        pnl = (price - Decimal(position["price"])) * position["side"] * quantity * Decimal(self.definition.multiplier)
                        pnl -= quantity * Decimal(self.limits.fee_per_side)
                        state["cash_pnl"] = str(Decimal(state["cash_pnl"]) + pnl)
                        position["quantity"] -= quantity
                        emit("EXIT_FILL", order_id=position["exit_order_id"], side=-position["side"], quantity=quantity, price=str(price), reason=state["close_reason"], cash_change=str(pnl))
                        emit("ORDER_PARTIALLY_FILLED" if position["quantity"] else "ORDER_FILLED", order_id=position["exit_order_id"])
                        if not position["quantity"]:
                            state["position"] = None
                            state["close_reason"] = None
                            state["last_exit_at"] = at

            pending = state["pending"]
            if pending and epoch_ns(at) >= epoch_ns(pending["expires_at"]):
                emit("ORDER_CANCELLED", reason="TTL_EXPIRED", signal_id=pending["signal_id"], order_id=pending["order_id"])
                state["pending"] = pending = None
            if pending and kind == "quote" and epoch_ns(at) >= epoch_ns(pending["arrives_at"]) and fresh:
                spread = self.definition.ticks(quote["ask"]) - self.definition.ticks(quote["bid"])
                if self._entry_window(when) and spread <= self.limits.max_spread_ticks:
                    side = pending["side"]
                    ticks = self.definition.ticks(quote["ask"] if side == 1 else quote["bid"]) + side * self.limits.slippage_ticks
                    quantity = min(pending["quantity"], quote["ask_size"] if side == 1 else quote["bid_size"])
                    if abs(ticks * Decimal(self.definition.tick_size)) * quantity * Decimal(self.definition.multiplier) > Decimal(self.limits.max_notional_usd):
                        cancel("NOTIONAL_LIMIT_AT_FILL")
                        quantity = 0
                    if quantity and side * (ticks - pending["limit_ticks"]) <= 0:
                        price = ticks * Decimal(self.definition.tick_size)
                        state["position"] = {"side": side, "quantity": quantity, "price": str(price), "opened_at": at,
                                             "signal_id": pending["signal_id"], "order_id": pending["order_id"], "episode_id": pending["episode_id"]}
                        state["cash_pnl"] = str(Decimal(state["cash_pnl"]) - quantity * Decimal(self.limits.fee_per_side))
                        state["trades_today"] += 1
                        state["episodes"].append(pending["episode_id"])
                        state["pending"] = None
                        emit("ENTRY_FILL", signal_id=pending["signal_id"], order_id=pending["order_id"], side=side, quantity=quantity, price=str(price),
                             cash_change=str(-quantity * Decimal(self.limits.fee_per_side)), unfilled_cancelled=pending["quantity"] - quantity)
                        emit("ORDER_PARTIALLY_FILLED" if quantity < pending["quantity"] else "ORDER_FILLED", order_id=pending["order_id"])
                        if quantity < pending["quantity"]:
                            emit("ORDER_CANCELLED", order_id=pending["order_id"], reason="UNFILLED_REMAINDER", quantity=pending["quantity"] - quantity)

            # Fees and exit slippage can cross the loss limit after a fill.
            if fresh and self._equity(state) - Decimal(state["day_start_equity"]) <= -Decimal(self.limits.daily_loss_usd):
                state["loss_halt"] = True
                cancel("DAILY_LOSS_LIMIT")
                if state["position"]:
                    state["close_reason"] = "DAILY_LOSS_LIMIT"

            if kind == "signal":
                side = payload.get("direction")
                reason = None
                if (payload.get("action") != "PAPER_INTENT" or payload.get("authorization") != "paper_only"
                        or payload.get("instrument_id") != self.definition.instrument_id
                        or not isinstance(payload.get("episode_id"), str) or not payload["episode_id"].strip()
                        or type(side) is not int or side not in {-1, 1}):
                    reason = "INELIGIBLE_SIGNAL"
                elif type(payload.get("quantity")) is not int or not 1 <= payload["quantity"] <= min(self.limits.contracts, self.limits.max_contracts):
                    reason = "QUANTITY_LIMIT"
                elif not isinstance(payload.get("limit_price"), str):
                    reason = "EXPLICIT_LIMIT_REQUIRED"
                elif state["manual_halt"] or state["loss_halt"]:
                    reason = "ACCOUNT_HALTED"
                elif state["position"] or state["pending"]:
                    reason = "POSITION_OR_ORDER_EXISTS"
                elif state["trades_today"] >= self.limits.max_trades_per_day:
                    reason = "DAILY_TRADE_LIMIT"
                elif payload["episode_id"] in state["episodes"]:
                    reason = "EPISODE_ALREADY_TRADED"
                elif state["last_exit_at"] and (when - instant(state["last_exit_at"])).total_seconds() < self.limits.cooldown_seconds:
                    reason = "COOLDOWN"
                elif not self._entry_window(when):
                    reason = "SESSION_OR_EXPIRY_RESTRICTED"
                elif not fresh:
                    reason = "QUOTE_UNAVAILABLE_OR_STALE"
                elif self.definition.ticks(quote["ask"]) - self.definition.ticks(quote["bid"]) > self.limits.max_spread_ticks:
                    reason = "SPREAD_LIMIT"
                if reason:
                    emit("SIGNAL_REJECTED", reason=reason)
                else:
                    ticks = self.definition.ticks(quote["ask"] if side == 1 else quote["bid"]) + side * self.limits.slippage_ticks
                    requested_ticks = self.definition.ticks(payload["limit_price"])
                    if max(abs(Decimal(payload["limit_price"])), abs(Decimal(quote["bid"])), abs(Decimal(quote["ask"]))) * payload["quantity"] * Decimal(self.definition.multiplier) > Decimal(self.limits.max_notional_usd):
                        emit("SIGNAL_REJECTED", reason="NOTIONAL_LIMIT")
                    else:
                        # Use the tighter caller cap, never chase away from the arrival quote.
                        ticks = min(ticks, requested_ticks) if side == 1 else max(ticks, requested_ticks)
                        state["pending"] = {"signal_id": event_id, "order_id": digest([self.identity, event_id]),
                                        "episode_id": payload["episode_id"], "quantity": payload["quantity"], "side": side, "limit_ticks": ticks,
                                        "arrives_at": iso_ns(epoch_ns(at) + self.limits.order_delay_seconds * 10**9),
                                        "expires_at": iso_ns(epoch_ns(at) + self.limits.order_ttl_seconds * 10**9)}
                        emit("ORDER_SUBMITTED", **state["pending"])
                        emit("ORDER_ACKNOWLEDGED", order_id=state["pending"]["order_id"])
                        emit("ORDER_WORKING", order_id=state["pending"]["order_id"])

            state["at"] = at
            input_id = self.store.append("paper_input", {"event_id": event_id, "kind": kind, "payload": payload, "identity": self.identity},
                record_id=digest([self.identity, "input", event_id]), available_at=at, db=db)
            for n, event in enumerate(events):
                self.store.append("paper_event", {**event, "input_revision_ids": [input_id]},
                    record_id=digest([self.identity, "event", event_id, n]), available_at=at, db=db)
            self.store.append("paper_account", {"state": state, "input_revision_ids": [input_id]},
                record_id=digest([self.identity, "account", event_id]), available_at=at, db=db)
            self.store.set_cursor(db, "paper:state", state)
            self.store.set_cursor(db, "paper:event:" + event_id, input_hash)
            return state

    def summary(self):
        state = self._state()
        events = [row["payload"] for row in self.store.records("paper_event")]
        ledger_cash = sum((Decimal(e["cash_change"]) for e in events if "cash_change" in e), Decimal(0))
        ledger_quantity = sum(e["side"] * e["quantity"] for e in events if e["event"] in {"ENTRY_FILL", "EXIT_FILL"})
        position_quantity = state["position"]["quantity"] * state["position"]["side"] if state["position"] else 0
        if ledger_cash != Decimal(state["cash_pnl"]) or ledger_quantity != position_quantity:
            raise ValueError("paper fill-ledger reconciliation failed")
        orders = {}
        for event in events:
            oid = event.get("order_id")
            if not oid:
                continue
            if event["event"] == "ORDER_SUBMITTED":
                orders[oid] = {"order_id": oid, "state": "SUBMITTED", "requested_quantity": event["quantity"],
                               "filled_quantity": 0, "reduce_only": event.get("reduce_only", False)}
            elif event["event"] in {"ENTRY_FILL", "EXIT_FILL"}:
                orders[oid]["filled_quantity"] += event["quantity"]
            elif event["event"].startswith("ORDER_") and oid in orders:
                orders[oid]["state"] = event["event"].removeprefix("ORDER_")
                if event.get("reason"):
                    orders[oid]["reason"] = event["reason"]
        if any(o["filled_quantity"] > o["requested_quantity"] for o in orders.values()):
            raise ValueError("paper order quantity reconciliation failed")
        return {"mode": "local_paper", "broker_connected": False, "economic_evaluation": "unavailable",
                "instrument_id": self.definition.instrument_id, "limits": asdict(self.limits),
                "cash_pnl": state["cash_pnl"], "marked_equity": str(self._equity(state)),
                "mark_available_at": state["mark_quote"]["available_at"] if state["mark_quote"] else None,
                "position": state["position"], "pending_order": state["pending"],
                "orders": list(orders.values()),
                "halted": state["loss_halt"] or state["manual_halt"], "events": events,
                "reconciled": True, "trade_authorized": False,
                "mark_stale": state["market_blocked"] or not self._fresh(state["mark_quote"], instant(state["at"])) if state["at"] else True,
                "limitations": "Hypothetical fills and fees; stops can slip. Open exposure remains unresolved when quotes end."}


def paper_replay(manifest_path: Path, destination: Path, instrument_id: str, limits: PaperLimits):
    from .replay import load_manifest
    manifest, reader, market = load_manifest(manifest_path)
    if market["gaps"] or any(row["kind"] == "gap" for row in market["records"]):
        raise ValueError("paper replay requires a market archive without recorded gaps")
    definitions = [InstrumentDefinition(**row["payload"]) for row in market["records"]
                   if row["kind"] == "instrument" and row["payload"]["instrument_id"] == instrument_id]
    if len(definitions) != 1:
        raise ValueError("one unambiguous explicit instrument definition required")
    limits.validate()
    destination = Path(destination).resolve()
    snapshot = Path(manifest_path).resolve().parent
    if destination == snapshot or snapshot in destination.parents:
        raise ValueError("paper output must be outside snapshot")
    # Fresh output ensures results from different snapshots cannot mix.
    destination.mkdir(parents=True, exist_ok=False)
    with component_lock(destination, "paper"):
        engine = PaperEngine(Journal(destination / "paper.sqlite3"), definitions[0], limits)
        timeline = [(row["payload"]["available_at"], 0, row["id"], row["payload"].get("event_type", "quote"), row["payload"])
                    for row in market["records"] if row["kind"] == "market" and row["payload"]["instrument_id"] == instrument_id
                    and row["payload"].get("event_type", "quote") in {"quote", "status"}]
        timeline += [(row["available_at"], 1, row["id"], "signal", row["payload"])
                     for row in reader.records if row["kind"] in {"decision", "paper_intent"}]
        for at, _, event_id, kind, payload in sorted(timeline, key=lambda row: (epoch_ns(row[0]), row[1], row[4].get("sequence", 0), row[2])):
            engine.process(kind, payload, at=at, event_id=event_id)
        report = {**engine.summary(), "source_records_hash": manifest["records_hash"],
                  "source_manifest_hash": digest(manifest), "dataset_role": manifest["dataset_role"]}
        atomic_json(destination / "report.json", report)
        return {"report": str(destination / "report.json"), "journal": str(destination / "paper.sqlite3"),
                "mode": report["mode"], "cash_pnl": report["cash_pnl"], "open_position": bool(report["position"]),
                "economic_evaluation": "unavailable"}
