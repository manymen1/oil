"""Fixed chronological holdout screen of hypothetical release-level results.

No optimization, model fitting, random revision split or execution authorization.
Even a positive screen is insufficient to establish a deployable trading edge.
"""
from collections import Counter, defaultdict
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from .clock import epoch_ns
from .databento import file_hash
from .inventory_strategy import load_inventory_rules
from .macro_dataset import comparisons
from .outcomes import HORIZONS, OutcomePolicy
from .schema import digest
from .research_stats import paired_uncertainty, contribution_diagnostics, stressed_samples


def summarize(samples, names, stress, contracts):
    result = {}
    for name in names:
        values = [(side, value - (stress * contracts if side else 0))
                  for row in samples for side, value in [row["values"][name]]]
        pnl = [value for _, value in values]
        trades = [value for side, value in values if side]
        gains = sum((v for v in trades if v > 0), Decimal(0))
        losses = -sum((v for v in trades if v < 0), Decimal(0))
        equity = peak = drawdown = Decimal(0)
        for value in pnl:
            equity += value
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        result[name] = {"releases": len(values), "trades": len(trades),
            "net_pnl_usd": str(sum(pnl, Decimal(0))) if pnl else None,
            "mean_pnl_per_release_usd": str(sum(pnl, Decimal(0)) / len(pnl)) if pnl else None,
            "mean_pnl_per_trade_usd": str(sum(trades, Decimal(0)) / len(trades)) if trades else None,
            "wins": sum(v > 0 for v in trades), "losses": sum(v < 0 for v in trades),
            "breakeven": sum(v == 0 for v in trades), "max_drawdown_usd": str(drawdown) if pnl else None,
            "profit_factor": str(gains / losses) if losses else None}
    return result


def _collect_samples(groups, *, delay, policy, names, rules_hash, horizon_seconds, cutoff, split):
    excluded, excluded_reasons, samples = Counter(), Counter(), {"train": [], "holdout": []}
    for group, revisions in groups.items():
        selected = []
        for row in revisions:
            labels = row.get("latency_scenarios", {}).get(str(delay)) if delay else row
            comparison = ((labels or {}).get("comparisons") or {}).get(str(horizon_seconds))
            if not comparison or comparison["state"] != "COMPARABLE":
                excluded[comparison["state"] if comparison else
                         "NO_COMPARISON_LABELS" if row["state"] == "EVALUATED" else row["state"]] += 1
                excluded_reasons.update(row["comparison_exclusions"])
                continue
            if row["comparison_exclusions"] or row["initial_snapshot"] or row["revision"]:
                raise ValueError("ineligible release marked comparable")
            decision, outcomes = row["decision"], labels["outcomes"]
            if decision["rules_hash"] != rules_hash or outcomes["assumptions"] != asdict(policy):
                raise ValueError("row policy mismatch")
            if set(decision["baselines"]) != set(names):
                raise ValueError("inconsistent baseline population")
            rebuilt = comparisons(decision, outcomes, [], policy.contracts)[str(horizon_seconds)]
            if rebuilt != comparison:
                raise ValueError("comparison does not match execution labels")
            end = epoch_ns(row["decision_at"]) + horizon_seconds * 10**9 + policy.delay_ms * 10**6
            if end > cutoff or epoch_ns(row["decision_at"]) < epoch_ns(row["available_at"]):
                raise ValueError("immature or noncausal comparison")
            values = {}
            for name in names:
                label = comparison["baselines"][name]
                side, pnl = label["direction"], Decimal(label["net_pnl"])
                if type(side) is not int or side not in {-1, 0, 1} or not pnl.is_finite() or (side == 0 and pnl != 0):
                    raise ValueError("invalid comparison value")
                values[name] = (side, pnl)
            selected.append({"release_group": group, "decision_at": row["decision_at"],
                             "end_ns": end, "values": values})
        if len(selected) > 1:
            raise ValueError("multiple comparable revisions in one release group")
        if not selected:
            continue
        sample = selected[0]
        first_receipt = min(epoch_ns(r["received_at"]) for r in revisions)
        if first_receipt < split and sample["end_ns"] >= split:
            excluded["PURGED_BOUNDARY_OVERLAP"] += 1
            continue
        samples["train" if first_receipt < split else "holdout"].append(sample)
    for values in samples.values():
        values.sort(key=lambda r: (epoch_ns(r["decision_at"]), r["release_group"]))
        if any(a["end_ns"] >= epoch_ns(b["decision_at"]) for a, b in zip(values, values[1:])):
            raise ValueError("overlapping release outcomes require a portfolio simulator")
    return samples, excluded, excluded_reasons


def evaluate_macro_dataset(directory, *, holdout_start, horizon_seconds=300,
                           strategy="inventory_continuation_v2", minimum_train_groups=52,
                           minimum_holdout_groups=26, minimum_holdout_trades=10,
                           extra_round_trip_cost_usd="2.00", bootstrap_block_groups=4,
                           bootstrap_draws=2000, bootstrap_seed=1729, fixed_monthly_cost_usd=None,
                           latency_extra_ms=0):
    if type(horizon_seconds) is not int or horizon_seconds not in HORIZONS:
        raise ValueError("supported preselected horizon required")
    for value in (minimum_train_groups, minimum_holdout_groups, minimum_holdout_trades):
        if type(value) is not int or value < 1:
            raise ValueError("positive minimum sample counts required")
    try:
        stress = Decimal(extra_round_trip_cost_usd)
    except InvalidOperation as exc:
        raise ValueError("invalid cost stress decimal") from exc
    if not stress.is_finite() or stress < 0:
        raise ValueError("finite nonnegative cost stress required")
    if type(latency_extra_ms) is not int or not 0 <= latency_extra_ms <= 60000:
        raise ValueError("additional latency must be 0..60000 milliseconds")
    root = Path(directory).resolve()
    metadata = json.loads((root / "dataset.json").read_text())
    if metadata["schema"] != "macro-research-dataset-v1" or metadata["trade_authorized"] or metadata["promotion"]:
        raise ValueError("research dataset required")
    for name in ("rows.jsonl", "observations.jsonl"):
        if file_hash(root / name) != metadata["files"][name]:
            raise ValueError("dataset file hash mismatch")
    rules = load_inventory_rules(metadata["rules"])
    if digest(metadata["rules"]) != metadata["rules_hash"]:
        raise ValueError("dataset rules hash mismatch")
    policy = OutcomePolicy(**metadata["outcome_policy"])
    policy.validate()
    if metadata.get("pipeline") == "frozen_prospective_shadow":
        from .macro_shadow import verify_shadow_protocol
        protocol = verify_shadow_protocol(metadata["protocol"])
        spec, method = protocol["spec"], protocol["spec"]["statistical_method"]
        if metadata.get("protocol_id") != protocol["id"] or epoch_ns(metadata["through"]) < epoch_ns(spec["evaluation_end"]):
            raise ValueError("holdout remains locked until the predeclared evaluation end")
        requested = (epoch_ns(holdout_start), horizon_seconds, strategy, minimum_train_groups, minimum_holdout_groups,
            minimum_holdout_trades, bootstrap_block_groups, bootstrap_draws, bootstrap_seed, fixed_monthly_cost_usd)
        frozen = (epoch_ns(spec["holdout_start"]), spec["horizon_seconds"], "inventory_continuation_v2", method["minimum_train_groups"],
            method["minimum_holdout_groups"], method["minimum_holdout_trades"], method["block_groups"], method["draws"], method["seed"], spec["fixed_monthly_cost_usd"])
        if (requested != frozen or stress not in {Decimal(v) for v in spec["extra_round_trip_costs_usd"]}
                or latency_extra_ms not in spec["latency_stress_ms"] or metadata["rules"] != spec["rules"]
                or metadata["outcome_policy"] != spec["outcome_policy"]):
            raise ValueError("evaluation settings differ from frozen shadow protocol")
    elif latency_extra_ms:
        raise ValueError("latency scenarios require a frozen prospective dataset")
    base_policy = policy
    policy = OutcomePolicy(**{**asdict(policy), "delay_ms": policy.delay_ms + latency_extra_ms})
    if rules.version == "inventory-continuation-v2-draft" and (
            Decimal(rules.fee_per_contract_side) != Decimal(policy.fee_per_contract_side)
            or rules.slippage_ticks_per_side != policy.slippage_ticks_per_side):
        raise ValueError("decision and outcome cost mismatch")
    names = ["no_trade", "inventory_only", "price_only_60s", "inventory_plus_price"]
    if rules.version == "inventory-continuation-v2-draft":
        names.append("inventory_continuation_v2")
    if strategy not in names or strategy == "no_trade":
        raise ValueError("strategy absent from dataset")
    cutoff, split = epoch_ns(metadata["through"]), epoch_ns(holdout_start)
    if split >= cutoff:
        raise ValueError("holdout must start before dataset cutoff")
    rows = [json.loads(line) for line in (root / "rows.jsonl").read_text().splitlines() if line]
    if len(rows) != metadata["rows"]:
        raise ValueError("dataset row count mismatch")
    groups = defaultdict(list)
    seen = set()
    for row in rows:
        if row["id"] in seen or digest({k: v for k, v in row.items() if k != "id"}) != row["id"]:
            raise ValueError("duplicate or changed research row")
        seen.add(row["id"])
        if epoch_ns(row["received_at"]) > cutoff or epoch_ns(row["available_at"]) > cutoff:
            raise ValueError("future dataset observation")
        groups[row["release_group"]].append(row)
    if len(groups) != metadata["release_groups"]:
        raise ValueError("release group count mismatch")
    delays = (spec["latency_stress_ms"] if metadata.get("pipeline") == "frozen_prospective_shadow" else [0])
    populations = {}
    for delay in delays:
        scenario_policy = OutcomePolicy(**{**asdict(base_policy), "delay_ms": base_policy.delay_ms + delay})
        populations[delay] = _collect_samples(groups, delay=delay, policy=scenario_policy, names=names,
            rules_hash=metadata["rules_hash"], horizon_seconds=horizon_seconds, cutoff=cutoff, split=split)
    samples, excluded, excluded_reasons = populations[latency_extra_ms]
    common_groups = {part: set.intersection(*(
        {r["release_group"] for r in population[0][part]} for population in populations.values()))
        for part in ("train", "holdout")}
    coverage = {str(delay): {"included_release_groups": {part: [r["release_group"] for r in values]
        for part, values in population[0].items()}, "excluded_row_counts": dict(population[1]),
        "excluded_reason_counts": dict(population[2])} for delay, population in populations.items()}
    dropped = {part: [r["release_group"] for r in values if r["release_group"] not in common_groups[part]]
               for part, values in samples.items()}
    excluded["NOT_COMPARABLE_AT_ALL_DECLARED_LATENCIES"] += sum(len(v) for v in dropped.values())
    if not excluded["NOT_COMPARABLE_AT_ALL_DECLARED_LATENCIES"]:
        del excluded["NOT_COMPARABLE_AT_ALL_DECLARED_LATENCIES"]
    samples = {part: [r for r in values if r["release_group"] in common_groups[part]]
               for part, values in samples.items()}
    summaries = {part: summarize(values, names, Decimal(0), policy.contracts) for part, values in samples.items()}
    stressed = summarize(samples["holdout"], names, stress, policy.contracts)
    stressed_holdout = stressed_samples(samples["holdout"], stress, policy.contracts)
    selected = summaries["holdout"][strategy]
    reasons = []
    if len(samples["train"]) < minimum_train_groups:
        reasons.append("INSUFFICIENT_TRAIN_RELEASES")
    if len(samples["holdout"]) < minimum_holdout_groups:
        reasons.append("INSUFFICIENT_HOLDOUT_RELEASES")
    if selected["trades"] < minimum_holdout_trades:
        reasons.append("INSUFFICIENT_HOLDOUT_TRADES")
    sufficient = not reasons
    if sufficient:
        if Decimal(selected["net_pnl_usd"]) <= 0:
            reasons.append("HOLDOUT_NOT_POSITIVE_AFTER_COSTS")
        if Decimal(stressed[strategy]["net_pnl_usd"]) <= 0:
            reasons.append("FAILS_ADDITIONAL_COST_STRESS")
        for baseline in ("inventory_only", "price_only_60s", "inventory_plus_price"):
            if baseline != strategy and Decimal(selected["net_pnl_usd"]) <= Decimal(summaries["holdout"][baseline]["net_pnl_usd"]):
                reasons.append("NO_INCREMENTAL_PNL_OVER:" + baseline)
    settings = {"holdout_start": holdout_start, "horizon_seconds": horizon_seconds, "strategy": strategy,
        "minimum_train_groups": minimum_train_groups, "minimum_holdout_groups": minimum_holdout_groups,
        "minimum_holdout_trades": minimum_holdout_trades, "extra_round_trip_cost_usd": str(stress),
        "bootstrap_block_groups": bootstrap_block_groups, "bootstrap_draws": bootstrap_draws,
        "bootstrap_seed": bootstrap_seed, "fixed_monthly_cost_usd": fixed_monthly_cost_usd,
        "latency_extra_ms": latency_extra_ms}
    return {"schema": "macro-holdout-screen-v1", "settings": settings, "settings_hash": digest(settings),
        "dataset_hash": digest(metadata), "dataset_role": metadata["dataset_role"],
        "summary": summaries, "holdout_cost_stress": stressed,
        "holdout_uncertainty": paired_uncertainty(samples["holdout"], names, strategy,
            block_groups=bootstrap_block_groups, draws=bootstrap_draws, seed=bootstrap_seed),
        "holdout_cost_stress_uncertainty": paired_uncertainty(stressed_holdout, names, strategy,
            block_groups=bootstrap_block_groups, draws=bootstrap_draws, seed=bootstrap_seed),
        "contribution_diagnostics": contribution_diagnostics(samples["holdout"], names,
            contracts=policy.contracts, fixed_monthly_cost_usd=fixed_monthly_cost_usd),
        "cost_stress_contribution_diagnostics": contribution_diagnostics(stressed_holdout, names,
            contracts=policy.contracts, fixed_monthly_cost_usd=fixed_monthly_cost_usd),
        "latency_population": {"basis": "intersection_of_all_declared_latency_scenarios",
            "declared_extra_ms": delays, "scenario_coverage": coverage, "dropped_from_requested_scenario": dropped,
            "limitations": ["Complete-case latency comparisons do not resolve missing-outcome selection bias.",
                "Scenario-specific coverage is retained; excluded releases have no imputed PnL."]},
        "denominator": {"captured_release_groups": len(groups), "research_rows": len(rows),
            "comparable_train_groups": len(samples["train"]), "comparable_holdout_groups": len(samples["holdout"]),
            "unscored_groups": len(groups) - sum(len(v) for v in samples.values()),
            "expected_release_count": None, "expected_release_calendar": "not_available_in_counterfactual_dataset"},
        "included_release_groups": {part: [r["release_group"] for r in values] for part, values in samples.items()},
        "excluded_row_counts": dict(excluded), "excluded_reason_counts": dict(excluded_reasons), "reason_codes": reasons,
        "result": "INSUFFICIENT_EVIDENCE" if not sufficient else "REJECTED_BY_RESEARCH_SCREEN" if reasons else "RESEARCH_SCREEN_PASSED",
        "trade_authorized": False, "promotion": False, "expected_profit": None,
        "economic_validation": "unavailable",
        "deployment_blockers": ["MARKET_PROVENANCE_NOT_QUALIFIED_BY_THIS_SCREEN",
            "HOLDOUT_NONREUSE_NOT_ATTESTED", "PROSPECTIVE_PAPER_AND_RISK_REVIEW_REQUIRED"],
        "limitations": ["Hypothetical fixed-horizon executions, not actual trades or stop-loss simulation.",
            "No fitting occurs here. Choose rules, horizon and split before inspecting holdout results.",
            "This tool cannot verify that the holdout was never inspected during strategy development.",
            "Thresholds are draft research policy, not statistical proof or guaranteed profitability.",
            "Positive screens still require qualified market provenance, prospective paper testing and risk review.",
            "Missing/excluded releases are reported, never imputed as zero PnL.",
            "Each comparable release has both-side complete MCL fills under identical assumed costs.",
            "Fixture and development datasets cannot establish a production edge."]}
