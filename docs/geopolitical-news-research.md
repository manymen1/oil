# Geopolitical news continuation research

Implemented 5 October 2026 as an offline, snapshot-bound workflow. No news feed,
model processing permission, service, strategy promotion or broker execution is
activated by these commands. This is a testable hypothesis, not a profitable bot.

## Two evidence lanes

| Lane | Inputs | Interpretation |
| --- | --- | --- |
| Risk premium | Already captured `fast-event-v7` / `headline-interpretation-v2` events with literal title evidence, named claimant, asserted and unqualified oil-relevant claim | Study price reaction to a claim; never infer barrels lost, shipping stopped or physical confirmation |
| Physical transition | Separately dated, revision-bound economic assessment, explicit prior/new operating states and supply/transport evidence | Reuse the existing economic eligibility gate; preserve reviewer provenance and actual review availability |

Assistant assessments remain ineligible under the existing economic bridge. A
passing human assessment means research eligibility, not factual certainty or
permission to trade. Tests use synthetic reviewer fixtures, not real human review.
Both lanes always report `physical_disruption_confirmed: false`,
`trade_authorized: false`, and no expected profit.

The draft risk map assigns an upward hypothesis to captured tanker attacks or
seizures, military/missile/drone attacks, broken ceasefires and collapsed
negotiations; ceasefires reached and negotiations started receive a downward
hypothesis. These are unvalidated policy choices, not price forecasts. Sanctions
and OPEC headlines do not get an automatic sign. Operating-state headlines must
pass the physical lane. Opposing signs captured on the same story before the
decision exclude it from comparison.

## Decision clock and market filters

The hypothetical decision occurs at the latest of local news receipt plus 60
seconds, captured candidate availability and (when applicable) assessment
availability. It is explicitly counterfactual: the strategy was not running at
that historical time. Every feature, correction and assessment must be available
by that decision. Future labels cannot enter the decision.

Defaults are versioned in `configs/geopolitical-research.json` and the complete
expanded policy is hashed into each dataset:

- Publisher-reported publication age at receipt: 0–180 seconds; missing, stale
  and future timestamps exclude comparisons. This does not prove publisher speed.
- Receipt-to-candidate delay: at most 10 seconds. Decision: 60–300 seconds after
  receipt. Late manual review cannot be backdated into a timely candidate.
- Explicit CL1, CL2 and MCL1 contracts, stable roles, fresh uncrossed quotes,
  sufficient depth, bounded spreads, no recorded gaps and a usable volatility
  history are required. CL prices never substitute for MCL fills.
- The shared inventory continuation filter requires CL/MCL agreement, persistence
  over the final 30 seconds, no opposing calendar-spread move and enough observed
  movement relative to estimated costs. Excessive repricing also abstains.
- Defaults assume $1 per contract per side and one tick slippage per side, plus
  executable bid/ask spreads. These are illustrative, not verified IBKR fees.

Observed confirmation is not an estimate of remaining profit. Failed persistence,
curve or cost filters make this hypothesis abstain while retaining otherwise
eligible observations for baseline comparison. Evidence and market-quality
failures exclude all baselines on that observation.

Initial snapshots, parser reinterpretations, inactive/corrected evidence and
superseded stories are excluded. A captured correction can veto a decision even
before the classifier processes it. A later assessment creates a separate row;
it does not overwrite the original unreviewed row.

## Evaluation

`geopolitical-evaluate` compares four fixed policies: no trade, news direction
only, price-only 60-second momentum, and news plus continuation. It requires
complete hypothetical MCL execution labels for both directions at the selected
fixed horizon, with spread, delay, fees and slippage. Immature horizons and missing
prices remain null, never fabricated zero returns. Additional round-trip cost
stress defaults to $2.

The chronological train/holdout split uses conservative overlap groups: same
story/revisions, normalized equal titles, original URLs, incident IDs and captured
candidate/review links share a group. Even unreviewed or rejected links may group
observations for evaluation only. This deliberately sacrifices sample count;
it does not establish factual identity or independent corroboration. Later links
known by the dataset cutoff may affect grouping, never earlier decisions.

Use only the first decision per group and lane (first dated assessment in the
physical lane), not the best subsequent update. Missing first labels cannot be
replaced with a later winner. Purge whole groups crossing the holdout boundary
and omit overlapping outcome windows within each lane. Report lanes separately;
their P&L cannot be summed into a portfolio return. Shared summary fields named
`releases` and `mean_pnl_per_release_usd` count selected news groups here, not
macro releases or certified independent economic episodes.

The split is descriptive, not a locked or untouched out-of-sample certification.
There is no tuning, statistical significance gate or automatic promotion. Grouping
can miss dependencies; results may be small and selection-biased. A positive
backtest would still not establish a trading edge. Freeze the policy before a
new prospective experiment with qualified news and instrument-specific prices.

## Commands

Run from the project root. Inputs must be immutable verified manifests. Outputs
must be new paths outside input snapshots; evaluation reports stay outside the
dataset they describe.

```bash
oilbot geopolitical-dataset \
  --manifest /absolute/path/to/news-snapshot/manifest.json \
  --market-manifest /absolute/path/to/market-snapshot/manifest.json \
  --assessments /absolute/path/to/economic-assessments.sqlite3 \
  --through 2026-10-05T12:00:00Z \
  --out data/research/geopolitical-example-v1

oilbot geopolitical-evaluate \
  --dataset data/research/geopolitical-example-v1 \
  --holdout-start 2026-10-01T00:00:00Z \
  --horizon 300 --extra-round-trip-cost 2.00 \
  --out data/research/geopolitical-example-v1-evaluation.json
```

Omit `--assessments` to retain physical candidates as pending review. Omit
`--market-manifest` to use the news snapshot's market file; when empty, research
remains unpriced. Dataset, rules, input provenance and row hashes are recorded;
the evaluator checks file/row integrity and comparison/cost consistency. Neither
command fetches a source, calls a model or connects to IBKR.

## Source gaps checked on 5 October

A read-only health check at 14:35:09 UTC reported the existing collector
`DEGRADED`: Al Jazeera and CENTCOM circuits remain open, and Aramco/OFAC scoped
first-party identity reviews expired on 4 October. Iran International, gCaptain,
OFAC and Aramco had recent transport health records. Successful transport does
not establish timely independent news, processing rights or economic truth.

Inspection of the last saved failing responses found:

- Al Jazeera, 29 September 15:59 UTC: HTTP 403, an access-denied page, no links.
- CENTCOM, 29 September 03:43 UTC: HTTP 200 listing HTML with navigation links
  but no extractable article links. Loosening selectors would not create news.

No circuit reset, access-control bypass, identity-review renewal, polling-rate
increase, new paid subscription or collector redeployment was performed. UKMTO
remains disabled under its existing access restriction. Recover a source only
through an authorized, evidenced qualification workflow; do not treat restored
transport as filling past capture gaps.

## Saved-snapshot smoke check

The verified `data/reviews/20260929-pipeline-recovery/snapshot/manifest.json`
snapshot has a cutoff of **28 September 2026, 16:55:00.903314 UTC** despite its
directory name. It produced eight subjects: seven physical-review candidates and
one risk-premium candidate. All eight lacked market data and failed publication
freshness; seven were initial/reinterpreted captures. No physical assessments
were supplied. Evaluation returned `INSUFFICIENT_EVIDENCE`, with null P&L.

Local artifacts are `data/research/geopolitical-20261005-v1/` and
`data/research/geopolitical-20261005-v1-evaluation.json`. This is an old-snapshot
engineering check, not current feed coverage, an independent-event count or
profitability evidence. Existing frozen deployment checksums still verified.
