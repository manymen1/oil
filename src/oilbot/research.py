from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal
from random import Random

from .clock import instant, utc_now
from .schema import InstrumentDefinition, MarketEvent, digest
from .store import Journal


POLICIES = ("no_trade", "event_time", "matched_price", "simple_news", "news_plus_market", "continuous_price")


@dataclass(frozen=True)
class ExecutionAssumptions:
    delay_seconds: float = 1
    max_quote_age_seconds: float = 2
    fee_per_contract_side: str = "1.00"  # Explicit hypothetical fixture default, not a broker quote.
    extra_slippage_ticks: int = 1
    horizon_seconds: int = 1800

    def validate(self):
        if self.delay_seconds < 0 or self.max_quote_age_seconds <= 0 or self.horizon_seconds <= 0:
            raise ValueError("invalid execution timing")
        if type(self.extra_slippage_ticks) is not int or self.extra_slippage_ticks < 0:
            raise ValueError("invalid slippage")
        fee = Decimal(self.fee_per_contract_side)
        if not fee.is_finite() or fee < 0:
            raise ValueError("invalid fees")


def quote_at(quotes: list[MarketEvent], at: str, definition: InstrumentDefinition, max_age: float) -> MarketEvent | None:
    available = [q for q in quotes if q.event_type == "quote" and q.instrument_id == definition.instrument_id and instant(q.available_at) <= instant(at)]
    if not available:
        return None
    quote = max(available, key=lambda q: (instant(q.available_at), q.sequence))
    quote.validate(definition)
    if quote.data_mode not in {"fixture", "realtime"} or quote.event_type != "quote":
        return None
    if quote.bid is None or quote.ask is None:
        return None
    if max((instant(at) - instant(qat)).total_seconds() for qat in (quote.bid_at, quote.ask_at)) > max_age:
        return None
    return quote


def simulate(definition: InstrumentDefinition, quotes: list[MarketEvent], *, decision_at: str,
             side: int, quantity: int, limit_price: str, assumptions: ExecutionAssumptions) -> dict:
    assumptions.validate()
    definition.validate()
    if type(side) is not int or side not in {-1, 1} or type(quantity) is not int or quantity < 1:
        raise ValueError("signed integer contracts required")
    cap = definition.ticks(limit_price)
    arrival = instant(decision_at) + timedelta(seconds=assumptions.delay_seconds)
    exit_at = instant(decision_at) + timedelta(seconds=assumptions.horizon_seconds + assumptions.delay_seconds)
    result = {"instrument_id": definition.instrument_id, "side": side, "requested": quantity,
              "decision_at": decision_at, "order_arrival_at": arrival.isoformat(), "exit_arrival_at": exit_at.isoformat(),
              "assumptions": asdict(assumptions), "economic_evaluation": "fixture_diagnostic",
              "filled": 0, "entry": None, "exit": None, "net_pnl": None, "remaining_open": 0}
    if arrival >= instant(definition.last_trade_at) or exit_at >= instant(definition.last_trade_at):
        return {**result, "status": "EXPIRY_RESTRICTED"}
    if not any(instant(s["open"]) <= arrival <= exit_at < instant(s["close"]) for s in definition.sessions):
        return {**result, "status": "SESSION_RESTRICTED"}
    entry = quote_at(quotes, arrival.isoformat(), definition, assumptions.max_quote_age_seconds)
    if not entry:
        return {**result, "status": "ENTRY_UNAVAILABLE"}
    price = definition.ticks(entry.ask if side == 1 else entry.bid) + side * assumptions.extra_slippage_ticks
    if side * (price - cap) > 0:
        return {**result, "status": "LIMIT_UNFILLED"}
    filled = min(quantity, entry.ask_size if side == 1 else entry.bid_size)
    if filled == 0:
        return {**result, "status": "NO_DISPLAYED_SIZE"}
    tick, multiplier = Decimal(definition.tick_size), Decimal(definition.multiplier)
    result.update(filled=filled, unfilled_cancelled=quantity - filled, entry=str(price * tick), remaining_open=filled)
    exit_quote = quote_at(quotes, exit_at.isoformat(), definition, assumptions.max_quote_age_seconds)
    if not exit_quote:
        return {**result, "status": "EXIT_UNAVAILABLE"}
    exit_filled = min(filled, exit_quote.bid_size if side == 1 else exit_quote.ask_size)
    exit_price = definition.ticks(exit_quote.bid if side == 1 else exit_quote.ask) - side * assumptions.extra_slippage_ticks
    realized = side * exit_filled * multiplier * tick * (exit_price - price)
    fees = Decimal(assumptions.fee_per_contract_side) * (filled + exit_filled)
    result.update(exit=str(exit_price * tick), exited=exit_filled, remaining_open=filled - exit_filled,
                  explicit_fees=str(fees), realized_contribution=str(realized - fees),
                  status="CLOSED" if exit_filled == filled else "PARTIAL_EXIT")
    if exit_filled == filled:
        result["net_pnl"] = str(realized - fees)
    return result


def baseline_sides(news_side: int, momentum_ticks: int) -> dict[str, int]:
    momentum = (momentum_ticks > 0) - (momentum_ticks < 0)
    return {"no_trade": 0, "event_time": 1, "matched_price": momentum,
            "simple_news": news_side, "news_plus_market": news_side if news_side == momentum else 0,
            "continuous_price": momentum}


def research_candidate(store: Journal, incident: dict) -> str:
    """Persist a conservative v2 decision when no research context is supplied.

    Full cluster/market/support contexts are evaluated by the dataset builder.
    The observation collector cannot promote a bare operational-status change.
    """
    from .strategy import strategy_v2
    key = "candidate:oil-residual-v2:" + incident["id"]
    existing = store.cursor(key)
    if existing:
        return existing
    value = incident["payload"]
    previous = store.get(value["supersedes_id"]) if value["supersedes_id"] else None
    before = previous["payload"]["operational_status"] if previous else "unknown"
    after = value["operational_status"]
    family, side = "other", 0
    if before in {"operating", "restored"} and after in {"impaired", "suspended"}:
        family, side = "physical_disruption", 1
    elif before in {"impaired", "suspended", "partly_restored"} and after in {"partly_restored", "restored"}:
        family, side = "restoration", -1
    event = {"event_family": family, "event_transition": before + "_to_" + after,
             "direction": side, "episode_id": value.get("episode_id"), "episode_verified": False,
             "novelty": value["novel"], "initial_snapshot": value.get("initial_snapshot", True),
             "late": value["late"], "contradiction_flag": bool(value["contradictions"]),
             "asset_types": value.get("asset_types", []), "source_authority": "unverified",
             "decision_at": incident["available_at"], "received_at": incident["available_at"]}
    decision = strategy_v2(event, {"decision_at": event["decision_at"], "received_at": event["received_at"]})
    payload = {**decision, "input_revision_ids": [incident["id"]], "incident_id": value["incident_id"],
               "episode_id": value.get("episode_id"), "family": family,
               "context_status": "CLUSTER_MARKET_AND_HISTORICAL_CONTEXT_REQUIRED"}
    with store.transaction() as db:
        rid = store.append("decision", payload, db=db)
        store.set_cursor(db, key, rid)
    return rid


def freeze_protocol(store: Journal, protocol: dict) -> str:
    required = {"version", "evaluation_start", "evaluation_end", "universe", "instrument_rule", "baseline_rules",
                "costs", "missing_data", "split_rules", "statistical_method", "source_policy", "extraction_policy"}
    if not required <= protocol.keys():
        raise ValueError("incomplete research protocol: " + ", ".join(sorted(required - protocol.keys())))
    if instant(protocol["evaluation_start"]) <= instant(utc_now()):
        raise ValueError("freeze must precede prospective evaluation")
    if instant(protocol["evaluation_end"]) <= instant(protocol["evaluation_start"]):
        raise ValueError("invalid evaluation window")
    if set(protocol["baseline_rules"]) != set(POLICIES):
        raise ValueError("all six baseline rules must be declared")
    prior = store.records("protocol")
    if any(p["payload"]["version"] == protocol["version"] for p in prior):
        raise ValueError("protocol version already frozen")
    return store.append("protocol", protocol, record_id=digest(protocol))


def chronological_split(rows: list[dict], boundaries: list[str], purge_seconds=14400) -> dict:
    if len(boundaries) != 2 or instant(boundaries[0]) >= instant(boundaries[1]):
        raise ValueError("ordered development/validation/test boundaries required")
    result = {"development": [], "validation": [], "test": [], "purged": []}
    episodes = {}
    for row in rows:
        if not row.get("episode_id"):
            raise ValueError("episode adjudication required for evaluation")
        episodes.setdefault(row["episode_id"], []).append(row)
    for group in episodes.values():
        times = [instant(r["available_at"]) for r in group]
        if any(min(times) - timedelta(seconds=purge_seconds) <= instant(b) <= max(times) + timedelta(seconds=purge_seconds) for b in boundaries):
            result["purged"].extend(group)
        else:
            index = sum(min(times) > instant(b) for b in boundaries)
            result[("development", "validation", "test")[index]].extend(group)
    return result


def episode_summary(rows: list[dict]) -> dict:
    """Descriptive uncertainty only; never a standalone economic promotion gate."""
    episodes = {}
    for row in rows:
        if row.get("episode_id") and row.get("net_pnl") is not None:
            episodes.setdefault(row["episode_id"], []).append(float(row["net_pnl"]))
    if len(episodes) < 2:
        return {"status": "INSUFFICIENT_INDEPENDENT_EPISODES", "episodes": len(episodes)}
    groups = list(episodes.values())
    rng = Random(0)
    means = []
    for _ in range(2000):
        sampled = [value for group in rng.choices(groups, k=len(groups)) for value in group]
        means.append(sum(sampled) / len(sampled))
    means.sort()
    return {"status": "DESCRIPTIVE_ONLY", "episodes": len(groups), "interval_95": [means[49], means[1949]],
            "promotion": False, "limitations": "Not a multiplicity-adjusted or sufficiently powered edge claim."}
