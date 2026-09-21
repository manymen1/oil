"""Explicit residual-response gates, using only decision-time inputs."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import math

from .clock import epoch_ns
from .clusters import CRUDE_ASSETS
from .schema import digest


@dataclass(frozen=True)
class StrategyRules:
    version: str = "oil-residual-v2-draft"
    execution_role: str = "MCL1"
    max_spread_ticks: int = 2
    min_depth: int = 5
    max_volatility_5m: float = 0.01
    max_move_before_decision: float = 0.005
    min_historical_episodes: int = 20
    safety_buffer: float = 0.001

    def validate(self):
        if not self.version or self.execution_role not in {"CL1", "MCL1"}:
            raise ValueError("invalid strategy version/instrument")
        for name in ("max_spread_ticks", "min_depth", "min_historical_episodes"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("invalid strategy gate: " + name)
        for name in ("max_volatility_5m", "max_move_before_decision", "safety_buffer"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError("invalid strategy gate: " + name)


def context_key(event, features, execution_role="MCL1"):
    move = features.get("price_move_before_decision")
    vol = features.get("realized_vol_5m")
    return {"asset_types": sorted(event.get("asset_types", [])),
            "confirmation": event.get("confirmation_level"), "duration": event.get("estimated_duration_bucket"),
            "severity": event.get("severity"),
            "move_bin": None if move is None else math.floor(event.get("direction", 0) * move / 0.001),
            "volatility_bin": None if vol is None else math.floor(vol / 0.002),
            "spread_ticks": features.get("snapshots", {}).get(execution_role, {}).get("spread_ticks")}


def strategy_v2(event, features, *, support=None, expected_cost=None, rules=StrategyRules()):
    rules.validate()
    reasons = []
    if not event.get("novelty"):
        reasons.append("NO_NOVEL_INFORMATION")
    if event.get("initial_snapshot", True):
        reasons.append("INITIAL_CAPTURE_BACKFILL")
    if event.get("late"):
        reasons.append("ANALYSIS_DEADLINE_EXCEEDED")
    if event.get("source_authority") not in {"primary_operator", "primary_authority"}:
        reasons.append("WEAK_SOURCE")
    if event.get("contradiction_flag"):
        reasons.append("CONTRADICTORY_EVIDENCE")
    if not set(event.get("asset_types", [])) & CRUDE_ASSETS:
        reasons.extend(["NO_PHYSICAL_MECHANISM", "OUTSIDE_CRUDE_DISRUPTION_SCOPE"])
    if event.get("event_family") not in {"physical_disruption", "restoration"} or event.get("direction") not in {-1, 1}:
        reasons.append("NO_MEANINGFUL_TRANSITION")
    if not event.get("episode_verified"):
        reasons.append("UNADJUDICATED_EPISODE")
    if features.get("decision_at") != event.get("decision_at") or features.get("received_at") != event.get("received_at"):
        reasons.append("FEATURE_TIME_MISMATCH")
    snapshots = features.get("snapshots", {})
    if any(not snapshots.get(role, {}).get("quote") for role in ("CL1", "CL2", "MCL1")):
        reasons.append("STALE_OR_MISSING_MARKET_DATA")
    target = snapshots.get(rules.execution_role, {})
    if target.get("quote"):
        if target["spread_ticks"] > rules.max_spread_ticks:
            reasons.append("SPREAD_TOO_WIDE")
        q = target["quote"]
        if min(q["bid_size"], q["ask_size"]) < rules.min_depth:
            reasons.append("INSUFFICIENT_DEPTH")
    vol = features.get("realized_vol_5m")
    if vol is None or not math.isfinite(vol):
        reasons.append("MISSING_VOLATILITY")
    elif vol > rules.max_volatility_5m:
        reasons.append("VOLATILITY_TOO_HIGH")
    move = features.get("price_move_before_decision")
    if move is None or not math.isfinite(move):
        reasons.append("MISSING_PRE_EVENT_ANCHOR")
    elif event.get("direction", 0) * move > rules.max_move_before_decision:
        reasons.append("PRICE_ALREADY_REPRICED")
    if expected_cost is None or not math.isfinite(expected_cost) or expected_cost < 0:
        reasons.append("MISSING_COST_ASSUMPTIONS")
    residual = None
    support_ok = bool(support)
    if support:
        # Support is trained on disjoint completed episodes, available before this decision.
        support_ok = (support.get("target") == "decision_to_horizon_signed_return"
                      and support.get("event_family") == event["event_family"]
                      and support.get("event_transition") == event["event_transition"]
                      and support.get("execution_role") == rules.execution_role
                      and support.get("context") == context_key(event, features, rules.execution_role)
                      and support.get("feature_policy_hash") == features.get("feature_policy_hash")
                      and len(set(support.get("episode_ids", []))) >= rules.min_historical_episodes
                      and len(set(support.get("incident_ids", support.get("episode_ids", [])))) >= rules.min_historical_episodes
                      and event["episode_id"] not in support.get("episode_ids", [])
                      and event.get("incident_id") not in support.get("incident_ids", [])
                      and epoch_ns(support["available_at"]) < epoch_ns(event["decision_at"])
                      and epoch_ns(support["outcomes_through"]) < epoch_ns(support["available_at"]))
        if support_ok:
            residual = support["median_residual"]
            if not math.isfinite(residual):
                support_ok = False
                residual = None
    if not support_ok:
        reasons.append("INSUFFICIENT_HISTORICAL_SUPPORT")
    # Support already targets continuation after decision: do not subtract the observed move twice.
    net = residual - expected_cost - rules.safety_buffer if residual is not None and expected_cost is not None else None
    if net is not None and net <= 0:
        reasons.append("RESIDUAL_BELOW_COST_AND_BUFFER")
    return {"policy": rules.version, "rules_hash": digest(asdict(rules)),
            "action": "ABSTAIN" if reasons else "RESEARCH_CANDIDATE",
            "direction": event.get("direction", 0), "reason_codes": reasons,
            "expected_residual": residual, "expected_cost": expected_cost, "net_expected_residual": net,
            "horizon_seconds": support.get("horizon_seconds") if support_ok else None,
            "execution_role": rules.execution_role, "authorized_contracts": 0,
            "support_hash": digest(support) if support else None, "features_hash": features.get("features_hash")}
