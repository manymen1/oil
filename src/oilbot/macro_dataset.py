"""Receipt-causal macro research rows and hypothetical MCL labels; never orders."""
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from .clock import epoch_ns, iso_ns, utc_now
from .databento import file_hash
from .features import MarketView
from .inventory_strategy import InventoryRules, evaluate_inventory, inventory_features, latest_macro
from .macro import read_macro
from .market import atomic_json
from .outcomes import OutcomePolicy, forward_outcomes
from .schema import canonical, digest


# Signal-specific failures must not remove losing/contrary observations from the
# inventory-only or price-only comparison population.
COMMON_EXCLUSIONS = frozenset({
    "INITIAL_CAPTURE_OR_IMPORT", "REVISION_NOT_NEW_RELEASE",
    "MISSING_CONSECUTIVE_RELEASE_BASELINE", "UNBOUNDED_RELEASE_DETECTION_LAG",
    "OUTSIDE_CONFIRMATION_WINDOW", "STALE_EIA_REPORTING_PERIOD",
    "MISSING_QUALIFIED_MARKET_FEATURES", "CONTRACT_CHANGED_DURING_EVENT",
    "MISSING_OR_EXCESSIVE_VOLATILITY", "MISSING_FRESH_CL1", "MISSING_FRESH_MCL1",
    "UNUSABLE_CL1", "UNUSABLE_MCL1", "SPREAD_OR_DEPTH_CL1", "SPREAD_OR_DEPTH_MCL1",
    "SUPERSEDED_BEFORE_DECISION",
})


def comparisons(decision, outcomes, exclusions, contracts):
    """Common complete-fill population, without treating missing labels as zero."""
    result = {}
    for horizon, outcome in outcomes["horizons"].items():
        execution = outcome["instruments"]["MCL1"]
        complete = all(execution[s].get("status") == "CLOSED"
                       and execution[s].get("filled") == contracts
                       and execution[s].get("exited") == contracts
                       and execution[s].get("net_pnl") is not None for s in ("long", "short"))
        state = ("EXCLUDED_RELEASE" if exclusions else
                 "PENDING_HORIZON" if outcome["label_state"] != "MATURED" else
                 "COMPARABLE" if complete else "INCOMPLETE_MCL_EXECUTION")
        result[horizon] = {"state": state, "execution_role": "MCL1", "baselines": {
            name: {"direction": side, "net_pnl":
                   ("0" if side == 0 else execution["long" if side > 0 else "short"]["net_pnl"])
                   if state == "COMPARABLE" else None}
            for name, side in decision["baselines"].items()}}
    return result


def build_macro_dataset(journal, destination, *, through, market=None,
                        rules=InventoryRules(), policy=OutcomePolicy(), manifest=None):
    rules.validate()
    policy.validate()
    if rules.version == "inventory-continuation-v2-draft" and (
            Decimal(rules.fee_per_contract_side) != Decimal(policy.fee_per_contract_side)
            or rules.slippage_ticks_per_side != policy.slippage_ticks_per_side):
        raise ValueError("v2 decision and outcome cost assumptions must match")
    cutoff = epoch_ns(through)
    if cutoff > epoch_ns(utc_now()):
        raise ValueError("cannot export future macro research")
    journal, target = Path(journal).resolve(), Path(destination).resolve()
    protected = [journal.parent]
    if manifest is not None:
        protected.append(Path(manifest).resolve().parent)
    if any(target == root or root in target.parents for root in protected):
        raise ValueError("output must be outside input roots")
    if target.exists():
        raise ValueError("output already exists")
    observations = read_macro(journal, through=through)
    # Inputs beyond the cutoff must not complete future exits or affect hashes.
    if market is not None:
        market = {"records": [r for r in market["records"]
                              if epoch_ns(r.get("available_at") or r["payload"]["available_at"]) <= cutoff],
                  "gaps": [g for g in market["gaps"]
                           if not g.get("available_at") or epoch_ns(g["available_at"]) <= cutoff]}
    view = MarketView(market["records"], market["gaps"]) if market is not None else None
    rows = []
    for eia in observations:
        p = eia["payload"]
        if p["source"] != "eia":
            continue
        # This is explicitly a counterfactual research clock, NOT evidence that
        # the running collector/strategy actually made a decision at this time.
        decision_ns = max(epoch_ns(eia["available_at"]),
                          epoch_ns(p["received_at"]) + rules.confirmation_seconds * 10**9)
        at = iso_ns(decision_ns)
        row = {"schema": "macro-research-row-v1", "revision_id": eia["id"],
               "release_group": "eia:" + p["observation"]["period"],
               "received_at": p["received_at"], "available_at": eia["available_at"],
               "supersedes_id": p["supersedes_id"], "initial_snapshot": p["initial_snapshot"],
               "revision": p["revision"], "decision_at": at,
               "decision_clock": "counterfactual_receipt_plus_confirmation_or_parse_availability",
               "decision": None, "features": None, "outcomes": None, "comparisons": None,
               "trade_authorized": False, "authorized_contracts": 0}
        if decision_ns > cutoff:
            row.update(state="PENDING_DECISION", comparison_exclusions=["PENDING_DECISION"])
        else:
            known = [r for r in observations if epoch_ns(r["available_at"]) <= decision_ns]
            cot = latest_macro(known, "cftc")
            features = None
            if view is not None:
                features = inventory_features(view, p["received_at"], at, rules)
            decision = evaluate_inventory(eia, cot, features, at=at, rules=rules)
            current = latest_macro(known, "eia")
            if current["id"] != eia["id"]:
                decision["reason_codes"].append("SUPERSEDED_BEFORE_DECISION")
                decision["input_revision_ids"].append(current["id"])
                decision["action"] = "ABSTAIN"
                decision["baselines"]["inventory_plus_price"] = 0
                if "inventory_continuation_v2" in decision["baselines"]:
                    decision["baselines"]["inventory_continuation_v2"] = 0
            exclusions = sorted(set(decision["reason_codes"]) & COMMON_EXCLUSIONS)
            exclusions = sorted(set(exclusions) | {r for r in decision["reason_codes"]
                if r == "MISSING_CONTINUATION_CONTEXT" or r.startswith(("INCOMPLETE_CONTINUATION_MARKET:",
                    "CONTINUATION_CONTRACT_CHANGED:", "CONTINUATION_LIQUIDITY:"))})
            row.update(state="EVALUATED", decision=decision, features=features,
                       comparison_exclusions=exclusions)
            if view is not None:
                outcomes = forward_outcomes(view, features["roles"], at, policy)
                for horizon, outcome in outcomes["horizons"].items():
                    mature = decision_ns + int(horizon) * 10**9 + policy.delay_ms * 10**6 <= cutoff
                    outcome["label_state"] = "MATURED" if mature else "PENDING_HORIZON"
                    if not mature:
                        outcome["CL_return"] = None
                        outcome["instruments"] = {role: {"mid_return": None,
                            **{side: {"status": "PENDING_HORIZON", "net_pnl": None}
                               for side in ("long", "short")}} for role in ("CL1", "MCL1")}
                row.update(outcomes=outcomes, comparisons=comparisons(decision, outcomes, exclusions, policy.contracts))
        row["id"] = digest(row)
        rows.append(row)
    modes = sorted({e.data_mode for items in view.events.values() for e in items}) if view is not None else []
    target.mkdir(parents=True)
    for filename, values in (("rows.jsonl", rows), ("observations.jsonl", observations)):
        with (target / filename).open("x") as stream:
            for value in values:
                stream.write(canonical(value) + "\n")
    metadata = {"schema": "macro-research-dataset-v1", "through": through,
        "rows": len(rows), "release_groups": len({r["release_group"] for r in rows}),
        "pending_decisions": sum(r["state"] == "PENDING_DECISION" for r in rows),
        "comparison_eligible_rows": sum(not r["comparison_exclusions"] for r in rows),
        "rules": asdict(rules), "rules_hash": digest(asdict(rules)), "outcome_policy": asdict(policy),
        "macro_input_hash": digest(observations), "market_input_hash": digest(market) if market is not None else None,
        "source_manifest_sha256": file_hash(Path(manifest)) if manifest is not None else None,
        "market_modes": modes, "execution_role": "MCL1",
        "dataset_role": "engineering_fixture" if "fixture" in modes else "unpriced_research" if not modes else "development_research",
        "files": {name: file_hash(target / name) for name in ("rows.jsonl", "observations.jsonl")},
        "trade_authorized": False, "promotion": False, "expected_profit": None,
        "limitations": ["Counterfactual research decisions, not recorded forward signals or trades.",
            "Inventory changes are not consensus surprises; COT is lagged context.",
            "Baselines, imports and corrections are retained but excluded from comparisons.",
            "Split chronologically by release_group with outcome-window purging, never random revision rows.",
            "Comparison labels require complete MCL fills on both sides; missing labels are not zero.",
            "Fees, latency and slippage are assumptions, not verified IBKR costs or actual fills.",
            "No fitting, profitability claim, statistical validation or execution authorization."]}
    atomic_json(target / "dataset.json", metadata)
    return metadata
