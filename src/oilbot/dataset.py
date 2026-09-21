"""Reproducible transition rows with separate causal features and forward labels."""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
from statistics import median, mean

from .clock import epoch_ns, iso_ns
from .clusters import cluster_transitions
from .databento import file_hash
from .features import MarketView
from .market import atomic_json, read_archive
from .outcomes import forward_outcomes, OutcomePolicy, HORIZONS
from .replay import load_manifest
from .schema import digest, canonical
from .strategy import StrategyRules, strategy_v2, context_key
from .provenance import claim_transitions


def estimated_cost(view, features, policy, role):
    target = features["snapshots"][role]
    if target["quote"] is None or Decimal(target["mid"]) == 0:
        return None
    definition = view.definition(target["instrument_id"], features["decision_at"])
    # Round-trip spread (one spread total) + slippage on each side + both fees.
    price_cost = (Decimal(target["quote"]["ask"]) - Decimal(target["quote"]["bid"])
                  + 2 * policy.slippage_ticks_per_side * Decimal(definition.tick_size)
                  + 2 * Decimal(policy.fee_per_contract_side) / Decimal(definition.multiplier))
    return float(price_cost / abs(Decimal(target["mid"])))


def transition_rows(records, assets, view, *, rules=StrategyRules(), policy=OutcomePolicy(), supports=(), roll_days=5):
    rules.validate()
    policy.validate()
    result = []
    feature_policy_hash = digest({"transform": "features-v1", "roll_days": roll_days, "outcomes": asdict(policy)})
    claim_states = claim_transitions(records)
    for event in cluster_transitions(records, assets):
        known_claims = [c for c in claim_states if c["episode_id"] == event["episode_id"]
                        and epoch_ns(c["decision_at"]) <= epoch_ns(event["decision_at"])]
        if known_claims:
            latest_claim = known_claims[-1]
            event["claim_confirmation"] = latest_claim["confirmation_level"]
            event["contradiction_flag"] |= latest_claim["contradiction_flag"]
            event["input_revision_ids"] = sorted(set(event["input_revision_ids"] + latest_claim["input_revision_ids"]))
        features = view.features(event["received_at"], event["decision_at"],
                                 roll_days=roll_days, max_age_seconds=policy.max_quote_age_seconds)
        features["feature_policy_hash"] = feature_policy_hash
        features["features_hash"] = digest({k: v for k, v in features.items() if k != "features_hash"})
        candidates = [s for s in supports if s.get("event_family") == event["event_family"]
                      and s.get("event_transition") == event["event_transition"]
                      and s.get("context") == context_key(event, features, rules.execution_role)
                      and s.get("execution_role") == rules.execution_role
                      and epoch_ns(s["available_at"]) < epoch_ns(event["decision_at"])]
        # Horizon is explicit in each support artifact; ambiguous horizon choices are rejected.
        if len({s["horizon_seconds"] for s in candidates}) > 1:
            raise ValueError("support artifact must select one prospective horizon per context")
        support = max(candidates, key=lambda s: epoch_ns(s["available_at"])) if candidates else None
        decision = strategy_v2(event, features, support=support,
                               expected_cost=estimated_cost(view, features, policy, rules.execution_role), rules=rules)
        # Labels are evaluated only after the decision has been constructed.
        outcomes = forward_outcomes(view, features["roles"], event["decision_at"], policy)
        result.append({**event, "available_at": event["decision_at"], "features": features,
                       "decision": decision, "outcomes": outcomes})
    return result


def build_dataset(manifest_path, destination, *, market_root=None, rules=StrategyRules(), policy=OutcomePolicy(), supports=(), roll_days=5):
    manifest, reader, market = load_manifest(Path(manifest_path))
    if market_root is not None:
        records, gaps = read_archive(Path(market_root))
        market = {"records": records, "gaps": gaps}
    view = MarketView(market["records"], market["gaps"])
    rows = transition_rows(reader.records, manifest["config"]["assets"], view, rules=rules, policy=policy,
                           supports=supports, roll_days=roll_days)
    claims = []
    for transition in claim_transitions(reader.records):
        features = view.features(transition["received_at"], transition["decision_at"],
                                 roll_days=roll_days, max_age_seconds=policy.max_quote_age_seconds)
        claims.append({**transition, "features": features,
                       "outcomes": forward_outcomes(view, features["roles"], transition["decision_at"], policy),
                       "action": "ABSTAIN", "reason_codes": ["CLAIM_PROGRESSION_RESEARCH_ONLY"]})
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    with (destination / "events.jsonl").open("x") as stream:
        for row in rows:
            stream.write(canonical(row) + "\n")
    with (destination / "claims.jsonl").open("x") as stream:
        for row in claims:
            stream.write(canonical(row) + "\n")
    modes = sorted({e.data_mode for events in view.events.values() for e in events})
    metadata = {"schema": "oil-transition-dataset-v1", "rows": len(rows), "claim_transitions": len(claims),
                "source_manifest_sha256": file_hash(manifest_path), "market_hash": digest(market),
                "rules": asdict(rules), "outcome_policy": asdict(policy), "support_hash": digest(list(supports)),
                "roll_days": roll_days, "horizons_seconds": list(HORIZONS), "market_modes": modes,
                "dataset_role": "engineering_fixture" if not modes or "fixture" in modes else "historical_research",
                "files": {name: file_hash(destination / name) for name in ("events.jsonl", "claims.jsonl")},
                "episode_assignment": "dated assignments only; unassigned episodes cannot train support",
                "promotion": False}
    atomic_json(destination / "dataset.json", metadata)
    return metadata


def read_dataset(path):
    path = Path(path)
    metadata = json.loads((path / "dataset.json").read_text())
    for name, expected in metadata["files"].items():
        if name not in {"events.jsonl", "claims.jsonl"} or file_hash(path / name) != expected:
            raise ValueError("dataset checksum mismatch")
    return metadata, [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]


def fit_support(rows, *, available_at, horizon, execution_role="MCL1"):
    """Equal episode weight, explicit horizon, completed development outcomes only."""
    if horizon not in HORIZONS or execution_role not in {"CL1", "MCL1"}:
        raise ValueError("unsupported support horizon/role")
    groups = {}
    for row in rows:
        if not row["episode_verified"]:
            raise ValueError("all training episodes must be adjudicated")
        through_ns = epoch_ns(row["decision_at"]) + horizon * 10**9 + row["outcomes"]["assumptions"]["delay_ms"] * 10**6
        if through_ns >= epoch_ns(available_at):
            raise ValueError("training outcome is not complete before support availability")
        if row["event_family"] not in {"physical_disruption", "restoration"}:
            continue
        if row["initial_snapshot"] or row["late"] or row["contradiction_flag"] or not row["novelty"]:
            continue
        outcome = row["outcomes"]["horizons"][str(horizon)]["instruments"][execution_role]
        side = "long" if row["direction"] == 1 else "short"
        if outcome["mid_return"] is None or outcome[side]["status"] != "CLOSED":
            continue
        context = context_key(row, row["features"], execution_role)
        key = digest([row["event_family"], row["event_transition"], context, row["features"]["feature_policy_hash"]])
        group = groups.setdefault(key, {"row": row, "context": context, "episodes": {}, "incidents": set(), "through": 0, "inputs": []})
        group["incidents"].add(row["incident_id"])
        group["episodes"].setdefault(row["episode_id"], []).append(row["direction"] * outcome["mid_return"])
        group["through"] = max(group["through"], through_ns)
        group["inputs"].append(digest(row))
    return [{"target": "decision_to_horizon_signed_return", "event_family": g["row"]["event_family"],
             "event_transition": g["row"]["event_transition"], "context": g["context"], "execution_role": execution_role,
             "feature_policy_hash": g["row"]["features"]["feature_policy_hash"],
             "horizon_seconds": horizon, "available_at": available_at, "outcomes_through": iso_ns(g["through"]),
             "episode_ids": sorted(g["episodes"]), "median_residual": median(mean(v) for v in g["episodes"].values()),
             "incident_ids": sorted(g["incidents"]),
             "training_rows_hash": digest(sorted(g["inputs"])), "promotion": False} for g in groups.values()]
