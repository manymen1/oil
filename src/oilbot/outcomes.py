"""Forward labels. Never imported by the decision strategy."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from .clock import epoch_ns, iso_ns
from .features import midpoint, relative_move

HORIZONS = (1, 2, 5, 10, 30, 60, 120, 300, 900, 1800, 3600, 14400)


@dataclass(frozen=True)
class OutcomePolicy:
    delay_ms: int = 1000
    max_quote_age_seconds: int = 2
    fee_per_contract_side: str = "1.00"
    slippage_ticks_per_side: int = 1
    contracts: int = 1

    def validate(self):
        for key in ("delay_ms", "slippage_ticks_per_side", "contracts", "max_quote_age_seconds"):
            value = getattr(self, key)
            if type(value) is not int or value < (1 if key in {"contracts", "max_quote_age_seconds"} else 0):
                raise ValueError("invalid outcome policy: " + key)
        fee = Decimal(self.fee_per_contract_side)
        if not fee.is_finite() or fee < 0:
            raise ValueError("invalid fee")


def executable_outcome(view, instrument, decision_at, horizon, side, policy):
    policy.validate()
    if side not in {-1, 1} or horizon <= 0:
        raise ValueError("invalid horizon/side")
    entry_at = iso_ns(epoch_ns(decision_at) + policy.delay_ms * 10**6)
    exit_at = iso_ns(epoch_ns(decision_at) + horizon * 10**9 + policy.delay_ms * 10**6)
    result = {"side": side, "entry_at": entry_at, "exit_at": exit_at,
              "net_pnl": None, "realized_contribution": None, "filled": 0, "remaining_open": 0}
    definition = view.definition(instrument, decision_at)
    if not definition:
        return {**result, "status": "MISSING_INSTRUMENT"}
    if not any(epoch_ns(s["open"]) <= epoch_ns(entry_at) < epoch_ns(exit_at) < epoch_ns(s["close"]) for s in definition.sessions) or epoch_ns(exit_at) >= epoch_ns(definition.last_trade_at):
        return {**result, "status": "SESSION_OR_EXPIRY_RESTRICTED"}
    entry = view.quote(instrument, entry_at, policy.max_quote_age_seconds)
    if not entry:
        return {**result, "status": "ENTRY_UNAVAILABLE"}
    if view.gap_between(instrument, entry_at, exit_at):
        return {**result, "status": "MARKET_GAP"}
    filled = min(policy.contracts, entry.ask_size if side == 1 else entry.bid_size)
    price_in = Decimal(entry.ask if side == 1 else entry.bid) + side * Decimal(definition.tick_size) * policy.slippage_ticks_per_side
    result.update(filled=filled, remaining_open=filled, entry_price=str(price_in), unfilled_cancelled=policy.contracts - filled)
    exit_quote = view.quote(instrument, exit_at, policy.max_quote_age_seconds)
    if not exit_quote:
        return {**result, "status": "EXIT_UNAVAILABLE"}
    exited = min(filled, exit_quote.bid_size if side == 1 else exit_quote.ask_size)
    price_out = Decimal(exit_quote.bid if side == 1 else exit_quote.ask) - side * Decimal(definition.tick_size) * policy.slippage_ticks_per_side
    fees = Decimal(policy.fee_per_contract_side) * (filled + exited)
    pnl = side * exited * Decimal(definition.multiplier) * (price_out - price_in) - fees
    result.update(exit_price=str(price_out), exited=exited, remaining_open=filled - exited,
                  realized_contribution=str(pnl), fees=str(fees),
                  status="CLOSED" if filled == exited else "PARTIAL_EXIT",
                  net_pnl=str(pnl) if filled == exited else None)
    return result


def forward_outcomes(view, roles, decision_at, policy=OutcomePolicy()):
    policy.validate()
    result = {"assumptions": asdict(policy), "horizons": {}, "claim": "hypothetical_marketable_execution"}
    anchor = view.quote(roles.get("CL1"), decision_at, policy.max_quote_age_seconds)
    for h in HORIZONS:
        at = iso_ns(epoch_ns(decision_at) + h * 10**9)
        end = view.quote(roles.get("CL1"), at, policy.max_quote_age_seconds)
        outcome = {"CL_return": relative_move(midpoint(anchor), midpoint(end)), "instruments": {}}
        if view.gap_between(roles.get("CL1"), decision_at, at):
            outcome["CL_return"] = None
        for role in ("CL1", "MCL1"):
            instrument = roles.get(role)
            outcome["instruments"][role] = {name: executable_outcome(view, instrument, decision_at, h, side, policy)
                                                  for name, side in (("long", 1), ("short", -1))}
            first = view.quote(instrument, decision_at, policy.max_quote_age_seconds)
            last = view.quote(instrument, at, policy.max_quote_age_seconds)
            outcome["instruments"][role]["mid_return"] = (relative_move(midpoint(first), midpoint(last))
                if not view.gap_between(instrument, decision_at, at) else None)
        result["horizons"][str(h)] = outcome
    return result
