from decimal import Decimal

import pytest

from oilbot.research_stats import contribution_diagnostics, paired_uncertainty


def samples(count=16):
    return [{"values": {"candidate": (1, Decimal(n % 4 + 2)), "baseline": (1, Decimal(n % 4)),
        "no_trade": (0, Decimal(0))}} for n in range(count)]


def test_paired_resampling_preserves_incremental_difference_and_is_deterministic():
    a = paired_uncertainty(samples(), ["candidate", "baseline", "no_trade"], "candidate", draws=200)
    b = paired_uncertainty(samples(), ["candidate", "baseline", "no_trade"], "candidate", draws=200)
    assert a == b
    assert a["paired_incremental_net_per_release"]["baseline"] == {"mean_usd": "2.0", "interval_95_usd": ["2.0", "2.0"]}
    assert a["mean_net_per_release"]["no_trade"]["interval_95_usd"] == ["0", "0"]


def test_small_or_empty_sample_is_not_statistical_evidence():
    result = paired_uncertainty(samples(2), ["candidate", "baseline"], "candidate", draws=200)
    assert result["state"] == "INSUFFICIENT_GROUPS_FOR_UNCERTAINTY"
    assert result["paired_incremental_net_per_release"]["baseline"]["interval_95_usd"] is None
    empty = paired_uncertainty([], ["candidate", "baseline"], "candidate", draws=200)
    assert empty["mean_net_per_release"]["candidate"]["mean_usd"] is None


def test_fixed_cost_break_even_is_arithmetic_and_spread_is_not_deducted_twice():
    values = [{"values": {"candidate": (1, Decimal("10"))}} for _ in range(4)]
    result = contribution_diagnostics(values, ["candidate"], fixed_monthly_cost_usd="250")
    assert result["policies"]["candidate"]["monthly_trades_required_to_cover_assumed_fixed_costs"] == "25"
    assert result["policies"]["candidate"]["maximum_additional_cost_per_contract_round_trip_usd"] == "10"
    assert result["monthly_profit_forecast"] is None
    assert contribution_diagnostics(values, ["candidate"])["fixed_cost_basis"] == "unconfigured"


@pytest.mark.parametrize("changes", [{"draws": True}, {"draws": 10}, {"block_groups": 0}, {"seed": -1}])
def test_invalid_bootstrap_settings_are_rejected(changes):
    with pytest.raises(ValueError):
        paired_uncertainty([], ["candidate"], "candidate", **changes)
