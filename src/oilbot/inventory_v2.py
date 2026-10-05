"""Cost-aware inventory continuation hypothesis; not an execution strategy.

Observed price movement is a filter, never a forecast of remaining profit.
The original v1 decision is retained as an explicit ablation baseline.
"""
from dataclasses import asdict, dataclass, fields
from decimal import Decimal, InvalidOperation

from .clock import epoch_ns, iso_ns
from .features import midpoint
from .inventory_strategy import InventoryRules, inventory_decision
from .schema import digest


@dataclass(frozen=True)
class InventoryRulesV2(InventoryRules):
    version: str = "inventory-continuation-v2-draft"
    persistence_seconds: int = 30
    min_confirmation_ticks: int = 2
    min_persistence_ticks: int = 1
    min_confirmation_cost_multiple: str = "1.5"
    fee_per_contract_side: str = "1.00"
    slippage_ticks_per_side: int = 1

    def baseline_rules(self):
        values = {f.name: getattr(self, f.name) for f in fields(InventoryRules)}
        values["version"] = InventoryRules().version
        return InventoryRules(**values)

    def validate(self):
        if self.version != "inventory-continuation-v2-draft":
            raise ValueError("unsupported continuation policy")
        self.baseline_rules().validate()
        for key in ("persistence_seconds", "min_confirmation_ticks", "min_persistence_ticks", "slippage_ticks_per_side"):
            value = getattr(self, key)
            if type(value) is not int or value < (0 if key == "slippage_ticks_per_side" else 1):
                raise ValueError("invalid continuation policy: " + key)
        if self.persistence_seconds >= self.confirmation_seconds:
            raise ValueError("persistence must be shorter than confirmation")
        for key in ("min_confirmation_cost_multiple", "fee_per_contract_side"):
            value = getattr(self, key)
            if not isinstance(value, str):
                raise ValueError("decimal-string continuation assumption required")
            try:
                number = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError("invalid continuation decimal") from exc
            if not number.is_finite() or number < (1 if key == "min_confirmation_cost_multiple" else 0):
                raise ValueError("invalid continuation assumption: " + key)


def continuation_features(view, received_at, at, rules):
    rules.validate()
    roles = view.roles(at, roll_days=7)
    prior_roles = view.roles(iso_ns(epoch_ns(received_at) - 1), roll_days=7)
    recent = iso_ns(epoch_ns(at) - rules.persistence_seconds * 10**9)
    anchors = {"receipt": received_at, "persistence_start": recent, "decision": at}
    instruments = {}
    for role, instrument in roles.items():
        definition = view.definition(instrument, at) if instrument else None
        quotes = {key: view.quote(instrument, time) for key, time in anchors.items()}
        instruments[role] = {
            "instrument_id": instrument,
            "definition": asdict(definition) if definition else None,
            "quotes": {key: asdict(q) if q else None for key, q in quotes.items()},
            "mids": {key: midpoint(q) for key, q in quotes.items()},
            "gap": view.gap_between(instrument, received_at, at),
        }
    return {"schema": "inventory-continuation-features-v2", "anchors": anchors,
            "roles_before_receipt": prior_roles, "instruments": instruments}


def continuation_decision(eia, cot, features, *, at, rules):
    rules.validate()
    base = inventory_decision(eia, cot, features, at=at, rules=rules.baseline_rules())
    result = {**base, "schema": "inventory-research-decision-v2", "policy": rules.version,
              "rules_hash": digest(asdict(rules)), "reason_codes": list(base["reason_codes"]),
              "baselines": dict(base["baselines"]), "continuation_metrics": {},
              "cost_assumptions": {"fee_per_contract_side": rules.fee_per_contract_side,
                                   "slippage_ticks_per_side": rules.slippage_ticks_per_side,
                                   "verified_broker_costs": False}}
    reasons = result["reason_codes"]
    side = base["hypothesis_direction"]
    context = features.get("continuation_v2") if features else None
    metrics = result["continuation_metrics"]
    if not context or not eia:
        reasons.append("MISSING_CONTINUATION_CONTEXT")
    else:
        expected = {"receipt": eia["payload"]["received_at"], "decision": at,
                    "persistence_start": iso_ns(epoch_ns(at) - rules.persistence_seconds * 10**9)}
        if (context["schema"] != "inventory-continuation-features-v2"
                or any(epoch_ns(context["anchors"][k]) != epoch_ns(v) for k, v in expected.items())):
            raise ValueError("continuation feature time mismatch")
        usable = {}
        for role in ("CL1", "CL2", "MCL1"):
            value = context["instruments"][role]
            definition, quotes = value["definition"], value["quotes"]
            if value["instrument_id"] != features["roles"][role] or value["instrument_id"] != context["roles_before_receipt"][role]:
                reasons.append("CONTINUATION_CONTRACT_CHANGED:" + role)
            if not definition or value["gap"] or any(q is None for q in quotes.values()):
                reasons.append("INCOMPLETE_CONTINUATION_MARKET:" + role)
                continue
            if (definition["product"] != ("MCL" if role == "MCL1" else "CL")
                    or definition["exchange"] != "NYMEX" or definition["currency"] != "USD"
                    or Decimal(definition["tick_size"]) != Decimal(".01")
                    or Decimal(definition["multiplier"]) != (100 if role == "MCL1" else 1000)):
                raise ValueError("unexpected continuation contract specification")
            from .schema import InstrumentDefinition, QuoteEvent
            d = InstrumentDefinition(**definition)
            d.validate()
            tick = Decimal(d.tick_size)
            for anchor, q in quotes.items():
                event = QuoteEvent(**q)
                event.validate(d)
                if (q["instrument_id"] != value["instrument_id"] or q["flags"] & (4 | 8 | 32)
                        or q["data_mode"] not in {"fixture", "historical", "realtime"}
                        or any(not 0 <= (epoch_ns(expected[anchor]) - epoch_ns(q[k])) / 1e9 <= 2
                               for k in ("available_at", "bid_at", "ask_at"))):
                    raise ValueError("unusable continuation anchor")
                if value["mids"][anchor] != midpoint(event):
                    raise ValueError("continuation midpoint mismatch")
            q = quotes["decision"]
            if q != features["snapshots"][role]["quote"]:
                raise ValueError("continuation decision snapshot mismatch")
            spread = (Decimal(q["ask"]) - Decimal(q["bid"])) / tick
            if spread > rules.max_spread_ticks or min(q["bid_size"], q["ask_size"]) < rules.min_depth:
                reasons.append("CONTINUATION_LIQUIDITY:" + role)
            mids = {k: Decimal(v) for k, v in value["mids"].items()}
            usable[role] = mids
            move = side * (mids["decision"] - mids["receipt"]) / tick
            recent_move = side * (mids["decision"] - mids["persistence_start"]) / tick
            metrics[role] = {"signed_confirmation_ticks": str(move), "signed_persistence_ticks": str(recent_move)}
            if role in {"CL1", "MCL1"}:
                if move < rules.min_confirmation_ticks:
                    reasons.append("WEAK_CONTINUATION_CONFIRMATION:" + role)
                if recent_move < rules.min_persistence_ticks:
                    reasons.append("CONTINUATION_FADED:" + role)
            if role == "MCL1":
                # Estimated round trip: spread + both-side slippage + both-side fees.
                # This is a decision-time cost screen, not an expected-return model.
                cost = spread + 2 * rules.slippage_ticks_per_side + 2 * Decimal(rules.fee_per_contract_side) / (tick * Decimal(d.multiplier))
                metrics[role]["estimated_round_trip_cost_ticks"] = str(cost)
                metrics[role]["estimated_round_trip_cost_usd"] = str(cost * tick * Decimal(d.multiplier))
                if move < cost * Decimal(rules.min_confirmation_cost_multiple):
                    reasons.append("CONFIRMATION_SMALL_RELATIVE_TO_COST")
        if "CL1" in usable and "CL2" in usable:
            curve_change = side * ((usable["CL1"]["decision"] - usable["CL2"]["decision"])
                                  - (usable["CL1"]["receipt"] - usable["CL2"]["receipt"]))
            metrics["signed_calendar_spread_change"] = str(curve_change)
            if curve_change < 0:
                reasons.append("CALENDAR_SPREAD_CONTRADICTS")
    result["action"] = "ABSTAIN" if reasons else "RESEARCH_CANDIDATE"
    result["baselines"]["inventory_continuation_v2"] = side if not reasons else 0
    result["limitations"] = [*base["limitations"],
        "Observed confirmation is not expected future profit or proof of causality.",
        "Cost inputs are illustrative, not verified IBKR fees or fill guarantees.",
        "Persistence and calendar-spread filters are unvalidated hypotheses."]
    return result
