"""Descriptive paired release-block uncertainty, never strategy certification."""
from decimal import Decimal, InvalidOperation
import random


def amount(value):
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("decimal-string cost required") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("finite nonnegative cost required")
    return number


def stressed_samples(samples, extra_round_trip_cost_usd, contracts):
    """Apply the same per-contract cost to each policy's executed trades."""
    stress = amount(extra_round_trip_cost_usd)
    if type(contracts) is not int or contracts < 1:
        raise ValueError("positive integer contract count required")
    return [{**row, "values": {name: (side, pnl - stress * contracts if side else pnl)
            for name, (side, pnl) in row["values"].items()}} for row in samples]


def paired_uncertainty(samples, names, strategy, *, block_groups=4, draws=2000, seed=1729):
    if type(block_groups) is not int or not 1 <= block_groups <= 52:
        raise ValueError("bootstrap block length must be 1..52 releases")
    if type(draws) is not int or not 200 <= draws <= 20000:
        raise ValueError("bootstrap draws must be 200..20000")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("explicit nonnegative bootstrap seed required")
    count = len(samples)
    vectors = {name: [row["values"][name][1] for row in samples] for name in names}
    means = {name: sum(values, Decimal(0)) / count if count else None for name, values in vectors.items()}
    enough = count >= max(8, 2 * block_groups)
    boot = {name: [] for name in names}
    differences = {name: [] for name in names if name != strategy}
    if enough:
        generator = random.Random(seed)
        # Overlapping moving blocks, without wrapping the final release back to
        # the first. All policies share the same sampled indices in each draw.
        for _ in range(draws):
            indices = []
            while len(indices) < count:
                left = generator.randrange(count - block_groups + 1)
                indices.extend(range(left, left + block_groups))
            indices = indices[:count]
            averages = {name: sum((values[i] for i in indices), Decimal(0)) / count
                        for name, values in vectors.items()}
            for name, value in averages.items():
                boot[name].append(value)
            for name in differences:
                differences[name].append(averages[strategy] - averages[name])

    def interval(values):
        if not values:
            return None
        values.sort()
        return [str(values[int((len(values) - 1) * .025)]), str(values[int((len(values) - 1) * .975)])]

    return {"method": "paired_overlapping_moving_release_blocks_percentile_95",
        "block_groups": block_groups, "draws": draws, "seed": seed, "release_groups": count,
        "state": "DESCRIPTIVE_UNCERTAINTY" if enough else "INSUFFICIENT_GROUPS_FOR_UNCERTAINTY",
        "mean_net_per_release": {name: {"mean_usd": str(means[name]) if means[name] is not None else None,
            "interval_95_usd": interval(boot[name])} for name in names},
        "paired_incremental_net_per_release": {name: {
            "mean_usd": str(means[strategy] - means[name]) if count else None,
            "interval_95_usd": interval(differences[name])} for name in differences},
        "limitations": ["Assumes dependence is represented by the preselected release block length.",
            "Percentile intervals are descriptive and do not correct repeated hypothesis or holdout inspection.",
            "Missing and incomplete releases remain outside these intervals; selection bias is unresolved.",
            "Calendar gaps between comparable releases can invalidate the chosen dependence model."]}


def contribution_diagnostics(samples, names, *, contracts=1, fixed_monthly_cost_usd=None):
    fixed = amount(fixed_monthly_cost_usd) if fixed_monthly_cost_usd is not None else None
    result = {}
    for name in names:
        trades = [row["values"][name][1] for row in samples if row["values"][name][0]]
        gains = [value for value in trades if value > 0]
        total = sum(trades, Decimal(0))
        mean = total / len(trades) if trades else None
        result[name] = {"trades": len(trades), "worst_trade_net_usd": str(min(trades)) if trades else None,
            "largest_gain_share": str(max(gains) / sum(gains, Decimal(0))) if gains else None,
            "pnl_without_largest_gain_usd": str(total - max(gains)) if gains else str(total) if trades else None,
            "maximum_additional_cost_per_contract_round_trip_usd": str(mean / contracts) if mean is not None else None,
            "monthly_trades_required_to_cover_assumed_fixed_costs": str(fixed / mean) if fixed is not None and mean is not None and mean > 0 else None}
    return {"fixed_monthly_cost_usd": str(fixed) if fixed is not None else None,
        "fixed_cost_basis": "unconfigured" if fixed is None else "user_supplied_research_assumption",
        "policies": result, "monthly_profit_forecast": None,
        "note": "Net labels already include bid/ask fills, fees and slippage; spread is not subtracted again. Required frequency is arithmetic, not predicted opportunities."}
