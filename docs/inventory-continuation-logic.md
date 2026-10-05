# Inventory continuation: strategy logic and falsification

Implemented 5 October 2026. This is an opt-in research hypothesis, not a
profitable or broker-enabled trading system. The original v1 configuration stays
unchanged, and no running collector or paper/broker service adopts v2 implicitly.

## The hypothesis

After a newly observed EIA weekly release, a sufficiently large crude-stock change
with product-stock agreement may identify a direction worth testing. Require
persistent price confirmation in both CL and the executable MCL contract, and
reject contradictory movement in the CL front/next-month spread. Evaluate whether
any continuation **after** the decision beats execution costs and simpler rules.

This deliberately does not call a draw bullish in all circumstances. EIA explains
that inventory behavior depends on expected future supply/demand and seasonal
patterns, not just the sign of the weekly change:
[EIA inventory/price relationships](https://www.eia.gov/finance/markets/products/balance.php).
The current rule is a conditional continuation experiment, not an inventory-
surprise model. Consensus estimates and seasonal forecast vintages are absent.

## Decision rules

All rules use only observations available by the decision time. Default settings
are frozen in `configs/inventory-continuation-v2.json`; they are draft engineering
choices, not fitted or statistically validated thresholds.

| Check | Draft rule | Reason to abstain |
| --- | --- | --- |
| Release provenance | New consecutive weekly HTTP observation; neither first snapshot nor correction; prior successful poll within 180 seconds | An old/current endpoint cannot recreate release-time availability |
| Fundamental direction | Crude change at least 2 million barrels; combined gasoline/distillate change agrees with the opposite stock-change direction | Conflicting or small inventory evidence |
| Decision time | Default 60 seconds after receipt, and no later than 300 seconds | Late signals and reaction already completed |
| Market quality | Fresh, usable CL1/CL2/MCL1 quotes; unchanged contracts; no recorded gap; spread at most 3 ticks, displayed depth at least 1 | Unreliable or unexecutable observations |
| Initial confirmation | CL1 and MCL1 each move at least 2 ticks in the hypothesized direction since receipt | CL does not substitute for MCL executable confirmation |
| Persistence | Both CL1 and MCL1 advance at least 1 further tick over the last 30 seconds | One jump followed by a flat/fading price |
| Curve agreement | Signed change in CL1 minus CL2 spread is nonnegative | The second month moves more strongly against the near-term hypothesis |
| Cost screen | Observed MCL move is at least 1.5 times estimated round-trip costs | Price evidence is small relative to execution friction |
| No chasing | Receipt-to-decision CL return at most 0.5%; five-minute realized volatility at most 1% | Excessive reaction or volatility under the inherited draft limits |

For builds the hypothesis is short and all signed-move checks reverse; for draws
it is long. The curve check uses the **change** in the spread, not a requirement
that its absolute level be in backwardation or contango. CFTC positioning remains
lagged context, not an additional independent confirmation vote.

The hourly table 9 Cushing/refinery collector is not currently a release-time
confirmation input. Its different availability must not be silently aligned
with the faster table 1 feed. Weather watches, attacks, sanctions headlines and
proposed reopenings likewise do not automatically become trade directions.

## Costs are a hurdle, not an edge estimate

Per-contract estimated round-trip cost in ticks is:

```text
current MCL spread ticks
+ 2 × assumed slippage ticks per side
+ 2 × assumed fee per side / MCL tick value
```

[CME's contract specification](https://www.cmegroup.com/education/articles-and-reports/micro-wti-crude-oil-futures-faq)
defines MCL as 100 barrels with a $0.01 price tick, or $1 per tick. The code checks
the explicit instrument definition; CL uses a different multiplier and its
prices are never used as MCL fills.

For a 2-tick spread, assumed 1-tick slippage per side and assumed $1 fee per side,
the screen estimates $6 round-trip cost and requires at least a 9-tick observed
move. The already-observed 9 ticks are **not** expected remaining profit. A trade
can pass every filter and still lose. Actual entry/exit spreads, gaps and fees
can be worse; these settings are not verified IBKR pricing or fill guarantees.

## Interfaces

```bash
# Point-in-time decision. Omit --manifest to test explicit missing-data abstention.
.venv/bin/python -m oilbot macro-research \
  --journal data/macro/20260929-official-v1/macro.sqlite3 \
  --at DECISION_TIME \
  --rules configs/inventory-continuation-v2.json \
  --manifest QUALIFIED_MARKET_MANIFEST

# Freeze research rows and hypothetical fixed-horizon MCL outcomes.
.venv/bin/python -m oilbot macro-dataset \
  --journal data/macro/20260929-official-v1/macro.sqlite3 \
  --through DATA_CUTOFF \
  --rules configs/inventory-continuation-v2.json \
  --manifest QUALIFIED_MARKET_MANIFEST \
  --out NEW_DATASET_DIRECTORY

# Evaluate a predeclared holdout and horizon; report must be outside the dataset.
.venv/bin/python -m oilbot macro-evaluate \
  --dataset DATASET_DIRECTORY \
  --holdout-start PREDECLARED_HOLDOUT_START \
  --horizon 300 \
  --extra-round-trip-cost 2.00 \
  --out NEW_REPORT_PATH
```

Replace uppercase placeholders with actual paths/timestamps. The dataset command
uses the original v1 rules unless `--rules` explicitly selects v2. If overriding
`--costs`, its fee/slippage assumptions must match the v2 decision policy.

The v2 decision preserves these comparison choices on the same eligible releases:
no trade, inventory direction alone, price-only 60-second direction, original
inventory-plus-price rule, and v2. Persistence, curve disagreement and cost-screen
failures set only the v2 direction to zero; they do not discard inconvenient
outcomes from the comparison population. Missing market data and common liquidity
exclusions remove the release from every comparison and remain counted.

## How to test whether the logic adds value

`macro-evaluate` verifies file/row hashes and cost-policy consistency, then groups
revisions by release. It refuses multiple comparable rows for one release. Split
assignment is chronological by the group's first receipt, with outcome windows
that cross the holdout boundary purged. Overlapping evaluated trade windows are
rejected because this evaluator is not a portfolio simulator.

It reports net PnL, trades, wins/losses, average trade/release PnL, profit factor
and chronological drawdown for every baseline. Missing labels never become zero
PnL. A no-trade decision on a fully comparable release is correctly zero. Profit
factor is null when there are no losses, not a fabricated infinity.

Default sample screens require 52 training release groups, 26 holdout release
groups and 10 holdout trades. They also require positive holdout PnL, positive
PnL after an additional $2 round-trip stress per traded contract, and greater
holdout total PnL than the inventory-only, price-only and original v1 rules.
These sample thresholds are draft policy; they are not statistical significance.
The training partition is diagnostic only: this tool fits no parameters.

Choose rules, horizon, costs and split **before inspecting the holdout**. Repeatedly
trying filters or horizons against the same holdout contaminates it. The tool
binds settings to the report but cannot prove the user never inspected the data.
Even `RESEARCH_SCREEN_PASSED` leaves economic validation unavailable, promotion
false and trading disabled. Fixture results validate software only.

## Remaining path to a tradable system

1. Capture qualified, synchronized CL1/CL2/MCL1 prices alongside enough genuine
   weekly releases. Existing current snapshots are not historical surprises.
2. Test whether v2 improves on v1 and price-only out of sample. Reject it if it
   fails; more filters are not automatically better.
3. Where lawful data access permits, add pre-release consensus or a separately
   trained seasonal inventory expectation model with strictly prior vintages.
4. Validate an explicit stop/target/time-exit policy, risk budget, daily loss
   limits and concurrent exposure in the existing paper engine. Current labels
   model fixed-horizon exits, not stops; none of their drawdowns is a loss cap.
5. Run prospective paper decisions with measured latency and realistic broker
   costs before any gated broker-order integration. Neither a positive holdout
   nor paper profits guarantee future profitability.

Tests exercise long/short symmetry, faded moves, MCL disagreement, curve
contradictions, fees, gaps, future-data invariance, inconsistent inputs, grouping,
holdout purging, incomplete outcomes, cost stress and disabled promotion.

## Current local evidence

The 5 October smoke run used the actual macro journal through 08:19:05 UTC,
containing five macro observations and two EIA release groups. No qualified
market manifest was supplied because that data gate remains unresolved. Both
EIA rows were excluded from comparisons. The decision was `ABSTAIN`; the
evaluation was `INSUFFICIENT_EVIDENCE`, with PnL and drawdown null rather than a
fabricated zero-return result.

Artifacts are under `data/research/`: `inventory-v2-20261005-decision.json`,
`inventory-v2-20261005-dataset/`, and
`inventory-v2-20261005-evaluation-final.json`. This is a software/data-readiness
smoke test, not a preregistered economic holdout experiment. No broker connection,
orders, strategy activation or running-service changes were made.
