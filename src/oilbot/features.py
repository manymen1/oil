"""Causal market features. This module never accesses forward outcome labels."""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from decimal import Decimal
from math import sqrt

from .clock import epoch_ns, iso_ns
from .schema import InstrumentDefinition, market_event, QuoteEvent, TradeEvent, MarketStatusEvent, digest

WINDOWS = (1, 5, 30, 60, 300)


def relative_move(before, after):
    if before is None or after is None or Decimal(before) == 0:
        return None
    return float((Decimal(after) - Decimal(before)) / abs(Decimal(before)))


def midpoint(quote):
    return str((Decimal(quote.bid) + Decimal(quote.ask)) / 2) if quote else None


class MarketView:
    def __init__(self, records, gaps=()):
        self.definitions = defaultdict(list)
        self.events = defaultdict(list)
        self.statuses = defaultdict(list)
        self.quotes = defaultdict(list)
        self.gaps = list(gaps)
        for row in records:
            if row["kind"] == "instrument":
                definition = InstrumentDefinition(**row["payload"])
                definition.validate()
                self.definitions[definition.instrument_id].append(definition)
            elif row["kind"] == "market":
                event = market_event(row["payload"])
                self.events[event.instrument_id].append(event)
                if isinstance(event, QuoteEvent):
                    self.quotes[event.instrument_id].append(event)
                elif isinstance(event, MarketStatusEvent):
                    self.statuses[event.instrument_id].append(event)
            elif row["kind"] == "gap":
                self.gaps.append(row["payload"])
        for groups in (self.definitions, self.events, self.quotes, self.statuses):
            for group in groups.values():
                group.sort(key=lambda item: epoch_ns(item.available_at))
        self.quote_times = {key: [epoch_ns(q.available_at) for q in qs] for key, qs in self.quotes.items()}
        self.status_times = {key: [epoch_ns(q.available_at) for q in qs] for key, qs in self.statuses.items()}
        for instrument, events in self.events.items():
            for event in events:
                definition = self.definition(instrument, event.available_at)
                if definition is None:
                    raise ValueError("market event has no contemporaneous instrument definition")
                event.validate(definition)

    def definition(self, instrument, at):
        rows = [d for d in self.definitions[instrument] if epoch_ns(d.available_at) <= epoch_ns(at)]
        return rows[-1] if rows else None

    def roles(self, at, roll_days=5):
        cutoff = epoch_ns(at) + roll_days * 86400 * 10**9
        known = [self.definition(key, at) for key in self.definitions]
        cl = sorted((d for d in known if d and d.product == "CL" and epoch_ns(d.last_trade_at) > cutoff), key=lambda d: d.month)
        front = cl[0] if cl else None
        mcl = next((d for d in known if d and front and d.product == "MCL" and d.month == front.month
                    and epoch_ns(d.last_trade_at) > cutoff), None)
        return {"CL1": front.instrument_id if front else None,
                "CL2": cl[1].instrument_id if len(cl) > 1 else None,
                "MCL1": mcl.instrument_id if mcl else None}

    def gap_between(self, instrument, start, end):
        lo, hi = epoch_ns(start), epoch_ns(end)
        for gap in self.gaps:
            if gap.get("instrument_id") not in {None, instrument}:
                continue
            at = gap.get("available_at")
            if at is None or lo <= epoch_ns(at) <= hi:
                return True
        for status in self.statuses[instrument]:
            if lo <= epoch_ns(status.available_at) <= hi and status.status != "RESUMED":
                return True
        return False

    def quote(self, instrument, at, max_age_seconds=2):
        if not instrument:
            return None
        cutoff = epoch_ns(at)
        times = self.quote_times.get(instrument, [])
        index = bisect_right(times, cutoff) - 1
        if index < 0:
            return None
        quote = self.quotes[instrument][index]
        definition = self.definition(instrument, quote.available_at)
        if not definition:
            return None
        try:
            quote.validate(definition)
        except ValueError:
            return None
        if quote.flags & (4 | 8 | 32) or quote.data_mode not in {"fixture", "historical", "realtime"}:
            return None
        if quote.bid is None or quote.ask is None or min(quote.bid_size, quote.ask_size) <= 0:
            return None
        if max(cutoff - epoch_ns(t) for t in (quote.bid_at, quote.ask_at)) > max_age_seconds * 10**9:
            return None
        if cutoff >= epoch_ns(definition.last_trade_at) or not any(epoch_ns(s["open"]) <= cutoff < epoch_ns(s["close"]) for s in definition.sessions):
            return None
        status_index = bisect_right(self.status_times.get(instrument, []), cutoff) - 1
        if status_index >= 0:
            status = self.statuses[instrument][status_index]
            if status.status != "RESUMED" or epoch_ns(quote.available_at) < epoch_ns(status.available_at):
                return None
        if self.gap_between(instrument, quote.available_at, at):
            return None
        return quote

    def features(self, received_at, decision_at, *, roll_days=5, max_age_seconds=2):
        if type(roll_days) is not int or roll_days < 0 or max_age_seconds <= 0:
            raise ValueError("invalid feature policy")
        decision_ns, received_ns = epoch_ns(decision_at), epoch_ns(received_at)
        if received_ns > decision_ns:
            raise ValueError("receipt follows decision")
        roles = self.roles(decision_at, roll_days)
        snapshots = {}
        for role, instrument in roles.items():
            q = self.quote(instrument, decision_at, max_age_seconds)
            d = self.definition(instrument, decision_at) if instrument else None
            snapshots[role] = {"instrument_id": instrument, "quote": q.__dict__ if q else None,
                               "mid": midpoint(q), "spread_ticks": d.ticks(q.ask) - d.ticks(q.bid) if q else None}
        front = roles["CL1"]
        before = self.quote(front, iso_ns(received_ns - 1), max_age_seconds)
        receipt = self.quote(front, received_at, max_age_seconds)
        current = self.quote(front, decision_at, max_age_seconds)
        moves = {}
        for seconds in WINDOWS:
            prior_at = iso_ns(decision_ns - seconds * 10**9)
            prior = self.quote(front, prior_at, max_age_seconds)
            moves[str(seconds)] = relative_move(midpoint(prior), midpoint(current)) if not self.gap_between(front, prior_at, decision_at) else None
        samples = [self.quote(front, iso_ns(decision_ns - i * 10**9), max_age_seconds) for i in range(300, -1, -1)]
        returns = [relative_move(midpoint(a), midpoint(b)) for a, b in zip(samples, samples[1:])]
        vol = sqrt(sum(r * r for r in returns)) if all(r is not None for r in returns) and not self.gap_between(front, iso_ns(decision_ns - 300 * 10**9), decision_at) else None
        trades = [e for e in self.events[front] if isinstance(e, TradeEvent)
                  and decision_ns - 30 * 10**9 < epoch_ns(e.available_at) <= decision_ns and not e.flags & (4 | 8 | 32)]
        buys = sum(t.trade_size for t in trades if t.aggressor == "buy")
        sells = sum(t.trade_size for t in trades if t.aggressor == "sell")
        unknown = sum(t.trade_size for t in trades if t.aggressor == "unknown")
        if self.gap_between(front, iso_ns(decision_ns - 30 * 10**9), decision_at):
            imbalance = None
        else:
            imbalance = (buys - sells) / (buys + sells) if buys + sells else None
        cl1, cl2 = snapshots["CL1"]["mid"], snapshots["CL2"]["mid"]
        result = {"received_at": received_at, "decision_at": decision_at, "roles": roles,
                  "snapshots": snapshots, "CL_price_before": midpoint(before),
                  "CL_price_at_receive": midpoint(receipt), "CL_price_at_decision": midpoint(current),
                  "price_move_before_decision": (relative_move(midpoint(before), midpoint(current))
                      if not self.gap_between(front, iso_ns(received_ns - 1), decision_at) else None),
                  "returns_before_decision": moves, "realized_vol_5m": vol,
                  "trade_imbalance_30s": imbalance, "unknown_trade_size_30s": unknown,
                  "calendar_spread": str(Decimal(cl1) - Decimal(cl2)) if cl1 and cl2 else None,
                  "max_quote_age_seconds": max_age_seconds, "roll_days": roll_days,
                  "availability_basis": sorted({s["quote"]["availability_basis"] for s in snapshots.values() if s["quote"]})}
        result["features_hash"] = digest(result)
        return result
