"""Conservative chronological descriptive screen, never a promotion gate."""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from .clock import epoch_ns
from .databento import file_hash
from .geopolitical import GeopoliticalRules
from .macro_dataset import comparisons
from .macro_evaluation import summarize
from .outcomes import HORIZONS, OutcomePolicy
from .schema import digest

NAMES = ["no_trade", "news_direction_only", "price_only_60s", "news_plus_continuation"]


def evaluate_geopolitical(directory, *, holdout_start, horizon_seconds=300, extra_round_trip_cost_usd="2.00"):
    if type(horizon_seconds) is not int or horizon_seconds not in HORIZONS:
        raise ValueError("supported fixed horizon required")
    try:
        stress = Decimal(extra_round_trip_cost_usd)
    except InvalidOperation as exc:
        raise ValueError("invalid cost stress") from exc
    if not stress.is_finite() or stress < 0:
        raise ValueError("nonnegative finite cost stress required")
    root = Path(directory).resolve()
    meta = json.loads((root / "dataset.json").read_text())
    if meta["schema"] != "geopolitical-dataset-v1" or meta["trade_authorized"] or meta["promotion"]:
        raise ValueError("geopolitical research dataset required")
    if file_hash(root / "rows.jsonl") != meta["files"]["rows.jsonl"]:
        raise ValueError("geopolitical dataset hash mismatch")
    rules = GeopoliticalRules(**meta["rules"])
    rules.validate()
    if digest(meta["rules"]) != meta["rules_hash"]:
        raise ValueError("geopolitical rules mismatch")
    policy = OutcomePolicy(**meta["outcome_policy"])
    policy.validate()
    if Decimal(policy.fee_per_contract_side) != Decimal(rules.market().fee_per_contract_side) or policy.slippage_ticks_per_side != rules.market().slippage_ticks_per_side:
        raise ValueError("geopolitical cost policy mismatch")
    split, cutoff = epoch_ns(holdout_start), epoch_ns(meta["through"])
    if split >= cutoff:
        raise ValueError("holdout must precede dataset cutoff")
    rows = [json.loads(line) for line in (root / "rows.jsonl").read_text().splitlines() if line]
    if len(rows) != meta["rows"]:
        raise ValueError("geopolitical row count mismatch")
    groups, seen = defaultdict(list), set()
    for row in rows:
        if row["id"] in seen or row["id"] != digest({k:v for k,v in row.items() if k != "id"}):
            raise ValueError("duplicate or changed geopolitical row")
        seen.add(row["id"])
        if row["lane"] not in {"risk_premium", "physical_transition"} or epoch_ns(row["received_at"]) > cutoff:
            raise ValueError("invalid news lane or receipt")
        groups[row["evaluation_group"]].append(row)
    if len(groups) != meta["evaluation_groups"]:
        raise ValueError("geopolitical group count mismatch")
    samples = {lane: {"train":[], "holdout":[]} for lane in ("risk_premium", "physical_transition")}
    excluded = Counter()
    for group, members in groups.items():
        first = min(epoch_ns(r["received_at"]) for r in members)
        last_end = max(epoch_ns(r["decision_at"]) + horizon_seconds*10**9 + policy.delay_ms*10**6 for r in members)
        if first < split <= last_end:
            excluded["GROUP_SPANS_HOLDOUT_BOUNDARY"] += len(members)
            continue
        partition = "train" if first < split else "holdout"
        for lane in samples:
            # Never pick a later member because the first one was a loser or
            # lacked a label. Physical lane starts at its first dated assessment.
            eligible = [r for r in members if r["lane"] == lane
                        and (lane == "risk_premium" or r["assessment_id"] is not None)]
            if not eligible:
                excluded["NO_DATED_PHYSICAL_REVIEW" if lane == "physical_transition" else "NO_RISK_PREMIUM_MEMBER"] += 1
                continue
            eligible.sort(key=lambda r:(epoch_ns(r["decision_at"]),r["event_id"],r["id"]))
            row = eligible[0]
            excluded["REPEATED_GROUP_MEMBER"] += len(eligible)-1
            comparison = (row.get("comparisons") or {}).get(str(horizon_seconds))
            if not comparison or comparison["state"] != "COMPARABLE":
                excluded[comparison["state"] if comparison else "NO_COMPARISON_LABELS"] += 1
                continue
            decision, outcomes = row["decision"], row["outcomes"]
            if (decision["comparison_exclusions"] or decision["rules_hash"] != meta["rules_hash"]
                    or decision["lane"] != lane or set(decision["baselines"]) != set(NAMES)
                    or outcomes["assumptions"] != meta["outcome_policy"]):
                raise ValueError("inconsistent geopolitical comparison policy")
            if comparisons(decision, outcomes, [], policy.contracts)[str(horizon_seconds)] != comparison:
                raise ValueError("geopolitical comparison differs from execution labels")
            end = epoch_ns(row["decision_at"]) + horizon_seconds*10**9 + policy.delay_ms*10**6
            if end > cutoff or epoch_ns(row["decision_at"]) < epoch_ns(row["candidate_available_at"]):
                raise ValueError("immature or noncausal news label")
            values = {}
            for name in NAMES:
                label = comparison["baselines"][name]
                side, pnl = label["direction"], Decimal(label["net_pnl"])
                if type(side) is not int or side not in {-1,0,1} or not pnl.is_finite() or (side == 0 and pnl != 0):
                    raise ValueError("invalid geopolitical comparison value")
                values[name] = (side, pnl)
            samples[lane][partition].append({"group":group, "decision_at":row["decision_at"], "end_ns":end, "values":values})
    report = {}
    for lane, partitions in samples.items():
        report[lane] = {}
        for partition, values in partitions.items():
            values.sort(key=lambda r:(epoch_ns(r["decision_at"]),r["group"]))
            accepted, last_end = [], None
            for value in values:
                if last_end is not None and epoch_ns(value["decision_at"]) <= last_end:
                    excluded["OVERLAPPING_GROUP_WINDOW:" + lane] += 1
                    continue
                accepted.append(value)
                last_end = value["end_ns"]
            report[lane][partition] = {"groups":[r["group"] for r in accepted],
                "baselines":summarize(accepted,NAMES,Decimal(0),policy.contracts),
                "cost_stress":summarize(accepted,NAMES,stress,policy.contracts)}
    count = sum(len(part["groups"]) for lane in report.values() for part in lane.values())
    return {"schema":"geopolitical-evaluation-v1", "dataset_hash":digest(meta), "holdout_start":holdout_start,
        "horizon_seconds":horizon_seconds, "extra_round_trip_cost_usd":str(stress), "lanes":report,
        "dataset_role":meta["dataset_role"], "excluded_counts":dict(excluded),
        "result":"DESCRIPTIVE_RESEARCH_ONLY" if count else "INSUFFICIENT_EVIDENCE",
        "trade_authorized":False, "promotion":False, "expected_profit":None,
        "limitations":["Potentially related claims share evaluation groups, never independent confirmation.",
            "Grouping may miss relationships; independent economic episodes have not been certified.",
            "No parameter fitting, statistical significance, profitability claim or execution authorization.",
            "First decision per group and lane only; no winner selection among repeated updates.",
            "Groups crossing the holdout boundary are purged wholesale; overlapping windows are conservatively omitted.",
            "Lanes are reported separately, not added into a combined portfolio return.",
            "Hypothetical MCL fills and fixed-horizon exits; not actual broker trades or stops."]}
