# Forward economic research — development protocol v1

Status: unvalidated hypotheses, not a trading strategy. No live source change,
market subscription, broker connection, model extraction or order submission is
authorized by this document. The WSL soak remains on its pinned code/config.

## The question

After a new, sufficiently supported physical crude-supply or transport disruption
or restoration becomes available to our collector, is there residual repricing
over 30 seconds–30 minutes that survives latency, executable spread, slippage,
fees, false positives and correlation between episodes?

Direction labels are hypotheses, not trades. An attack is not lost supply; a
ceasefire is not resumed shipping. More feeds and more accurate classification
do not establish an edge. Zero qualifying events is a valid research result.

## Forward bridge and episode contract

`oilbot.economic.build_transition(event, story, assessment, mapping,
evaluated_at=...)` is an offline normalization API. It consumes `fast_event` and
its exact captured story revision, an independently dated operational assessment,
and `oilbot.forward_review.episode_map(..., through=evaluated_at)`.
It does not invoke the legacy incident reducer or change captured/classified rows.
Use verified snapshot records, never edit original classifier output to supply
reviewed fields. Persist the returned record to a new diagnostic artifact.

The assessment record has `id`, `available_at`, and `payload` containing:

- Exact `event_id`, `story_revision_id`, `episode_id`; `reviewer`,
  `reviewer_type` (`human`/`assistant`), `reason`, `episode_reviewed`, `novel`.
- `stage`: initial_report, identity, damage, restriction, disruption,
  duration_update, repair, partial_restoration, restoration, contradiction,
  correction or withdrawal.
- `state_before`/`state_after`: UNKNOWN, OPERATING, IMPAIRED, SUSPENDED,
  PARTLY_RESTORED, RESTORED. These are reviewed operational claims, not truth.
- `assertion`: asserted, denied, hypothetical, historical, unclear.
- `confirmation_level`: UNVERIFIED, ACTOR_CLAIM, MARITIME_AUTHORITY_REPORT,
  OPERATOR_REPORT. This is a named evidence class, not a probability. An official
  military claim alone does not establish oil operations or independent evidence.
- `mechanism`: supply, transport, demand, risk_sentiment, unknown.
- Optional literal `claim_origin`, `asset`, `asset_type`, `location`, `severity`,
  `estimated_duration`; missing values remain null. Preserve unknown names in
  `unresolved_entities`, do not manufacture registry matches or capacity estimates.
  Nonempty unresolved entities block research eligibility.
- `evidence`: exact title/text spans `{text_field,start,end,quote,supports}`.
  Every non-unknown interpreted field needs supporting text. Reviewer responsibility:
  a matching substring does not prove it supports the asserted interpretation.

Assessment time cannot precede input availability. The output preserves original
receipt time, classifier availability, review availability and new evaluation time.
It binds the mapping hash, assessment, event, story and active link-review IDs.
Later human grouping must not be backdated to first receipt. As-of lifecycle states
other than ACTIVE block eligibility. Contradictory group mappings block eligibility.
A singleton needs explicit review too. Grouping is never confirmation.

`hypothesis_direction` can be up/down only for an eligible research candidate;
`direction` remains null, `trade_authorized` remains false. Severity/duration are
not inferred from adjectives or estimated barrels. The API is not wired to
`features.py`, `outcomes.py`, `strategy.py` or the running collector yet.
No live economic assessment worker or automatic physical-confirmation resolver
exists. Synthetic tests validate engineering, not economic performance.

## Hypothesis D1: disruption continuation

Eligible changes: OPERATING → IMPAIRED/SUSPENDED, IMPAIRED → SUSPENDED,
or PARTLY_RESTORED → SUSPENDED, at restriction/disruption stage. Require explicit
crude supply/transport mechanism and a named operator or maritime-authority
operational report reviewed against exact text. The earlier operating condition
must be evidenced, not assumed. UNKNOWN → SUSPENDED is logged but excluded from
the initial cohort. A future revision can support earlier-state evidence from
separate prior records; v1 assessments require spans in the bound story.

Exclude unrelated military targets, generic threats, hypothetical/denied/historical
attacks, contested reports, baseline snapshots, repeated syndicated claims,
unsupported duration/capacity estimates and unreviewed episodes. A new headline
does not mean new economic information. Review novelty relative to the episode.

Hypothesis: upward *remaining* return after decision, not total move from an
unobservable first rumor. Primary research horizon: 5 minutes. Secondary descriptive
horizons: 30 seconds, 1, 15 and 30 minutes. No optimal horizon selected afterward.
Invalidation: correction/withdrawal, contradictory operational evidence, resumption,
or invalid/stale/gapped market data. Log invalidation time; never erase the signal.

## Hypothesis R1: restoration reversal

Eligible changes: IMPAIRED/SUSPENDED → PARTLY_RESTORED/RESTORED, or
PARTLY_RESTORED → RESTORED. Require prior impairment and actual resumed
production, loading or transit in the same reviewed episode. Keep partial versus
full restoration separate. Planned repair, permission to reopen, talks and a
ceasefire do not establish resumed operations.

Evidence and abstention gates are the same as D1. Hypothesis: downward residual
return; the same predeclared primary/secondary horizons apply. Invalidate on renewed
interruption, disputed resumption, withdrawal, or unusable market data. Restoration
may fail to lower WTI if already priced, small, offset elsewhere, or predominantly
relevant to seaborne crude. Preserve those null/contrary outcomes.

## Market phase — deferred, not implemented by this package

Use explicit contract IDs/months, not an untracked continuous symbol. CL1/CL2
are research roles resolved as-of with a frozen roll policy; MCL1 is a candidate
execution instrument, not currently enabled. IBKR is the user's intended future
provider; entitlement, timestamp semantics, depth and suitability remain unverified.
Record BBO, sizes, trades and timestamp provenance; do not invent aggressor when
the provider lacks it. Later Brent needs its own qualified data and basis study.

Features must separately measure -60/-30/-10/-5 seconds → receipt and
receipt → decision. Outcomes: decision → +1/+5/+30 seconds, +1/+5/+15/+30
minutes, +1/+4 hours. The primary endpoint remains 5 minutes; secondary testing
must account for multiplicity. Missing, stale, crossed, rolled or gapped quotes
produce unavailable measurements, never zero moves or a fill-forward bridge.
Only decision-time observations enter features; future quotes are outcomes only.

A large pre-receipt move is a potential repricing veto, not proof that all edge
is gone. Pre-register veto thresholds on development data, then test unchanged.
No threshold or expected return is invented without data. Midpoint markout is
descriptive, not executable PnL. Long entry uses ask and exit bid; short entry
uses bid and exit ask after measured latency, with fees, tick rounding, size,
slippage and fill uncertainty. Account for CL/MCL basis and actual MCL costs.

Estimate conditional residual return, with uncertainty, rather than absolute
direction. Hierarchical estimates (physical → terminal → supported terminal →
severity/regime) are a future model, not an excuse for tiny samples. Parent/child
data overlap must be handled; estimate shrinkage using training data only.
Require independent episode counts and report crisis-level dependence. Do not
claim a universal sufficient sample size such as twenty matching headlines.

## Coverage and promotion

Track each episode stage, including unknown identity, operational consequences,
duration, repair, partial/full resumption and corrections. Count distinct underlying
claims as well as publisher receipts. Review denominator = sampled eligible
stories/episodes, stratified by source and original classifier disposition; preserve
N/n weights and baseline exclusions. Stage absence is an observed coverage gap,
not proof no real-world stage occurred. Expanded registries require exact names,
documented aliases/ownership and versioned evidence; unresolved names stay visible.

The 30-story assistant-reviewed pilot is development data only. Freeze an untouched
post-soak sample excluding pilot identities and known linked peers. Unknown episode
dependence remains. Do not read/label/evaluate that holdout with a model without
separate authorization; independent human labels are preferred. No new sample was
created or inspected for this package.

Promotion sequence: independent labels → causal forward/price bridge → frozen
development protocol → episode/time-separated validation → fixed prospective paper
period → only then a separately authorized live-risk decision. Paper evaluation
records every trade/abstention, net and gross PnL, costs, fills, latency, excursion,
drawdown and source/stage/regime breakdowns with denominators. No tuning during
evaluation; no duplicated headline treated as an independent win.

Future abstentions include PRICE_ALREADY_REPRICED, SPREAD_TOO_WIDE,
INSUFFICIENT_DEPTH, VOLATILITY_TOO_HIGH, MARKET_DATA_STALE and
EXPECTED_EDGE_BELOW_COST. None can be evaluated now. The current bridge always
adds MARKET_DATA_UNAVAILABLE and INSUFFICIENT_HISTORICAL_SUPPORT.
Future execution requires separate reconciliation/risk tests: one position,
fixed bounded risk, daily loss limit, episode cooldown, no averaging down and
halts for bad clocks/data, collector outage, spread or broker disconnection.

OPEC/EIA strategies are later work. Inventory surprise requires an immutable,
timestamped pre-release expectation; the inventory change itself is not surprise.
