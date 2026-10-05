"""Draft inventory-change continuation research. Never emits order intents."""
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
import math

from .clock import epoch_ns, instant
from .macro import read_macro
from .schema import digest


@dataclass(frozen=True)
class InventoryRules:
    version: str = "inventory-change-confirmation-v1-draft"
    min_crude_change_mb: str = "2.0"
    confirmation_seconds: int = 60
    max_decision_delay_seconds: int = 300
    max_poll_gap_seconds: int = 180
    max_period_age_days: int = 10
    max_cot_age_days: int = 14
    max_spread_ticks: int = 3
    min_depth: int = 1
    max_receipt_move: float = 0.005
    max_volatility_5m: float = 0.01

    def validate(self):
        if self.version != "inventory-change-confirmation-v1-draft":
            raise ValueError("unsupported inventory research policy")
        if not isinstance(self.min_crude_change_mb, str):
            raise ValueError("decimal-string inventory threshold required")
        try:
            threshold = Decimal(self.min_crude_change_mb)
        except Exception as exc:
            raise ValueError("invalid inventory threshold") from exc
        if not threshold.is_finite() or threshold <= 0:
            raise ValueError("positive finite inventory threshold required")
        for name in ("confirmation_seconds", "max_decision_delay_seconds", "max_poll_gap_seconds", "max_period_age_days",
                     "max_cot_age_days", "max_spread_ticks", "min_depth"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("invalid inventory policy: " + name)
        if self.confirmation_seconds > self.max_decision_delay_seconds:
            raise ValueError("confirmation exceeds decision window")
        for name in ("max_receipt_move", "max_volatility_5m"):
            if type(getattr(self, name)) not in {float, int} or not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError("invalid inventory policy: " + name)


def latest_macro(rows, source):
    eligible = [r for r in rows if r["payload"]["source"] == source]
    return max(eligible, key=lambda r: (r["payload"]["observation"]["period"], r["seq"]), default=None)


def inventory_decision(eia, cot, features, *, at, rules=InventoryRules()):
    rules.validate()
    now = epoch_ns(at)
    reasons, inputs = [], []
    side = 0
    crude = products = None
    if eia is None:
        reasons.append("MISSING_EIA_OBSERVATION")
    else:
        inputs.append(eia["id"])
        p, observation = eia["payload"], eia["payload"]["observation"]
        if p["source"] != "eia" or observation["units"] != "million_barrels":
            raise ValueError("EIA stock observation required")
        if epoch_ns(eia["available_at"]) > now or epoch_ns(p["received_at"]) > now:
            raise ValueError("future inventory observation")
        crude = Decimal(observation["facts"]["commercial_crude"]["change"])
        products = sum(Decimal(observation["facts"][key]["change"]) for key in ("gasoline", "distillate"))
        side = -((crude > 0) - (crude < 0))
        if p["initial_snapshot"] or p["delivery"] != "http":
            reasons.append("INITIAL_CAPTURE_OR_IMPORT")
        if p["revision"]:
            reasons.append("REVISION_NOT_NEW_RELEASE")
        prior_period = p["prior_latest_period"]
        if prior_period is None or (date.fromisoformat(observation["period"]) - date.fromisoformat(prior_period)).days != 7:
            reasons.append("MISSING_CONSECUTIVE_RELEASE_BASELINE")
        previous_receipt = p["previous_successful_receipt_at"]
        gap = None if previous_receipt is None else (epoch_ns(p["received_at"]) - epoch_ns(previous_receipt)) / 1e9
        if gap is None or not 0 <= gap <= rules.max_poll_gap_seconds:
            reasons.append("UNBOUNDED_RELEASE_DETECTION_LAG")
        delay = (now - epoch_ns(p["received_at"])) / 1e9
        if not rules.confirmation_seconds <= delay <= rules.max_decision_delay_seconds:
            reasons.append("OUTSIDE_CONFIRMATION_WINDOW")
        if (instant(at).date() - date.fromisoformat(observation["period"])).days > rules.max_period_age_days:
            reasons.append("STALE_EIA_REPORTING_PERIOD")
        if abs(crude) < Decimal(rules.min_crude_change_mb):
            reasons.append("INVENTORY_CHANGE_BELOW_DRAFT_THRESHOLD")
        if side == 0 or side * products >= 0:
            reasons.append("PRODUCT_STOCKS_DO_NOT_CONFIRM")
    cot_context = None
    if cot is not None:
        p = cot["payload"]
        if p["source"] != "cftc" or epoch_ns(cot["available_at"]) > now:
            raise ValueError("invalid or future CFTC context")
        age = (instant(at).date() - date.fromisoformat(p["observation"]["period"])).days
        if 0 <= age <= rules.max_cot_age_days:
            inputs.append(cot["id"])
            cot_context = {"revision_id": cot["id"], "age_days": age, **p["observation"]["facts"]}
    # COT is a slow descriptive covariate, not another independent buy/sell vote.
    post_move = momentum = None
    if not features or eia is None:
        reasons.append("MISSING_QUALIFIED_MARKET_FEATURES")
    else:
        receipt = eia["payload"]["received_at"]
        if epoch_ns(features["received_at"]) != epoch_ns(receipt) or epoch_ns(features["decision_at"]) != now:
            raise ValueError("inventory feature time mismatch")
        response = features["receipt_response"]
        if epoch_ns(response["anchors"]["receipt"]) != epoch_ns(receipt) or epoch_ns(response["anchors"]["decision"]) != now:
            raise ValueError("receipt-response time mismatch")
        # A roll between receipt and decision must not join different contracts.
        if response["instrument_id"] != features["roles"]["CL1"]:
            reasons.append("CONTRACT_CHANGED_DURING_EVENT")
        post_move = response.get("receipt_to_decision_return")
        if post_move is None or not math.isfinite(post_move) or side * post_move <= 0:
            reasons.append("PRICE_DOES_NOT_CONFIRM")
        elif abs(post_move) > rules.max_receipt_move:
            reasons.append("PRICE_ALREADY_REPRICED")
        vol = features.get("realized_vol_5m")
        if vol is None or not math.isfinite(vol) or not 0 <= vol <= rules.max_volatility_5m:
            reasons.append("MISSING_OR_EXCESSIVE_VOLATILITY")
        for role in ("CL1", "MCL1"):
            snapshot = features["snapshots"].get(role, {})
            q = snapshot.get("quote")
            if not q:
                reasons.append("MISSING_FRESH_" + role)
            elif (q["data_mode"] not in {"historical", "realtime", "fixture"}
                  or now < epoch_ns(q["available_at"])
                  or any(not 0 <= (now - epoch_ns(q[key])) / 1e9 <= 2 for key in ("bid_at", "ask_at"))
                  or q.get("flags", 0) & (4 | 8 | 32)):
                reasons.append("UNUSABLE_" + role)
            elif snapshot["spread_ticks"] > rules.max_spread_ticks or min(q["bid_size"], q["ask_size"]) < rules.min_depth:
                reasons.append("SPREAD_OR_DEPTH_" + role)
        momentum = features.get("returns_before_decision", {}).get("60")
    price_side = 0 if momentum is None or not math.isfinite(momentum) else (momentum > 0) - (momentum < 0)
    return {"schema": "inventory-research-decision-v1", "policy": rules.version, "rules_hash": digest(asdict(rules)),
        "decision_at": at, "action": "ABSTAIN" if reasons else "RESEARCH_CANDIDATE", "reason_codes": reasons,
        "hypothesis_direction": side, "commercial_crude_change_mb": str(crude) if crude is not None else None,
        "gasoline_plus_distillate_change_mb": str(products) if products is not None else None,
        "inventory_surprise": None, "consensus_available": False, "cot_context": cot_context,
        "receipt_to_decision_return": post_move, "input_revision_ids": inputs,
        "features_hash": digest(features) if features else None,
        "baselines": {"no_trade": 0, "inventory_only": side, "price_only_60s": price_side,
                      "inventory_plus_price": side if not reasons else 0},
        "execution_product": "MCL", "authorized_contracts": 0, "trade_authorized": False,
        "expected_profit": None, "economic_evaluation": "unavailable",
        "limitations": ["Draft hypothesis, not validated edge", "Change is not consensus surprise",
                        "Receipt is not publication time", "COT is lagged context, not a release-time signal"]}


def load_inventory_rules(values):
    if values.get("version") == "inventory-continuation-v2-draft":
        from .inventory_v2 import InventoryRulesV2
        rules = InventoryRulesV2(**values)
    else:
        rules = InventoryRules(**values)
    rules.validate()
    return rules


def evaluate_inventory(eia, cot, features, *, at, rules):
    if rules.version == "inventory-continuation-v2-draft":
        from .inventory_v2 import continuation_decision
        return continuation_decision(eia, cot, features, at=at, rules=rules)
    return inventory_decision(eia, cot, features, at=at, rules=rules)


def inventory_features(view, receipt, at, rules):
    features = view.features(receipt, at, roll_days=7)
    features["receipt_response"] = view.receipt_response(receipt, receipt, at, roll_days=7)
    if rules.version == "inventory-continuation-v2-draft":
        from .inventory_v2 import continuation_features
        features["continuation_v2"] = continuation_features(view, receipt, at, rules)
    return features


def research_macro(path, *, at, market=None, rules=InventoryRules()):
    rules.validate()
    rows = read_macro(path, through=at)
    eia, cot = latest_macro(rows, "eia"), latest_macro(rows, "cftc")
    features = None
    if eia and market is not None:
        from .features import MarketView
        view = MarketView(market["records"], market["gaps"])
        receipt = eia["payload"]["received_at"]
        features = inventory_features(view, receipt, at, rules)
    return {"decision": evaluate_inventory(eia, cot, features, at=at, rules=rules),
            "features": features, "rules": asdict(rules), "macro_input_hash": digest(rows),
            "market_input_hash": digest(market) if market is not None else None,
            "observations_available": len(rows), "trade_authorized": False}
