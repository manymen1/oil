# Project assessment and automated oil-trading development plan

Assessment date: 29 September 2026, Asia/Ulaanbaatar. Read-only operational check
at 28 September 2026 16:37 UTC / 29 September 00:37 local. Scope: the current
standalone `/home/tstuv/oiltrading/oil` checkout and its saved deployment.

Subsequent implementation: see [the recovery and first-milestone notes](implementation-2026-09-29.md).
The [next milestone](assessment-admission.md) adds automated queue admission and
receipt-relative response features, with explicit remaining gates.
The [paper execution milestone](paper-execution.md) subsequently adds a local
single-contract execution core; live strategy and broker gates remain unmet.
The findings below retain their original assessment timestamp rather than being
silently rewritten after deployment recovery.

The project is an auditable oil-news collection system with useful offline
research components. It is not yet an integrated trading-research system or an
execution bot. Turning it into a technically automated bot is feasible; whether
the collected information supports a profitable strategy remains an unanswered
empirical question. The next milestone should be a reliable, end-to-end forward
research loop, followed by a frozen paper campaign if the evidence supports it.

This plan was grounded in current source inspection, live service/journal metadata,
the saved assistant economic review, and a fresh full test run: **406 passed in
61.26 seconds**. `git diff --check` passed. Tests establish engineering behavior,
not source completeness, financial performance or production readiness. This
assessment did not restart services, modify runtime code, connect a broker, buy
data or activate model processing.

**What exists today**

| Component | Current evidence | Assessment |
| --- | --- | --- |
| News ingestion | Raw receipts, immutable revisions, recovery indexes, source circuits, timestamp/provenance fields in `sources.py` and `store.py` | Useful foundation; six-source pilot coverage remains narrow |
| Forward classification | Headline rules, first-party attribution, lifecycle history and episode-link review | Implemented; interpretation accuracy and novelty remain unvalidated |
| Operational text queue | `operational.py` scans captured title/feed text with literal spans and source-role priority | Implemented triage; no automatic assessment worker or durable resolution workflow |
| Economic review | `economic.py` plus `economic_review.py`, revision-bound assessments, append-only corrections, as-of reports | Implemented offline; requires captured fast events and excludes assistant reviews from eligibility |
| Market schema/archive | Explicit quote, trade, status, contract and timestamp records; checksummed archive | Reusable engineering foundation |
| Market ingestion | Fixture adapter plus offline Databento DBN/CSV import | No qualified live recorder; configuration rejects live providers |
| Features | CL1/CL2/MCL1 roles, decision-relative returns, spread, volatility, curve, trade imbalance | Partial; receipt-relative repricing and classification latency need explicit treatment |
| Outcomes | Multiple horizons, entry/exit bid/ask, latency, slippage, fees, displayed size and partial exits | Hypothetical execution labels, not an order-management system |
| Research | Episode/time partitions, descriptive event studies and a support fitter | Operates on the older incident/cluster path; disconnected from the forward economic bridge |
| Strategy | Deterministic abstention gates and `RESEARCH_CANDIDATE` output | Draft thresholds, zero authorized contracts, no established edge |
| Paper OMS, portfolio risk, broker | No corresponding implementation in this checkout | Must be built and tested separately |
| Deployment | Frozen code/config copy, checksums, supervisor, transient user service | Useful isolation; does not yet provide reliable unattended collection |

Code pointers: [source capture](../src/oilbot/sources.py),
[operational queue](../src/oilbot/operational.py),
[economic bridge](../src/oilbot/economic.py),
[dataset construction](../src/oilbot/dataset.py),
[legacy episode transitions](../src/oilbot/clusters.py),
[market features](../src/oilbot/features.py),
[execution labels](../src/oilbot/outcomes.py),
[strategy gates](../src/oilbot/strategy.py).

The current working tree contains substantial uncommitted development work.
The operational deployment binds the copied bytes to checksums rather than
pretending they are all represented by Git commit `af51a09`. Preserve those
deployment checksums and the earlier soak SHA `111d572` when consolidating work.
Do not infer that a paper implementation from another repository exists here.

**Most important findings**

1. **Continuous capture is currently broken.** The operational service is absent
   from loaded user units and reports inactive/dead; no collector workers were
   found. Last worker heartbeats were around 28 September 10:01:52 UTC, over
   6.5 hours before inspection. The current host boot began 29 September 00:35:16
   local. This is consistent with a transient service not surviving a host/session
   restart, but the inspected logs do not establish the precise shutdown cause.
   No normal `runtime_stop` rows were present in this run. Record the interval as
   a coverage gap; do not label it successful continuous operation.

2. **There are two separate integration gaps.** The new text queue emits
   `operational_review_candidate`; the economic review API only accepts
   `fast_event`. A useful report found only in feed text cannot currently pass
   directly into that bridge. Separately, `dataset.transition_rows()` calls
   `cluster_transitions()`, which consumes `incident_revision` and
   `cluster_assignment`, not `forward_economic_transition`. Connecting the pieces
   requires a versioned data contract and integration tests, not a renamed field.

3. **The current review rule conflicts with unattended operation.**
   `economic.py` rejects every non-human review through
   `ASSISTANT_REVIEW_NOT_INDEPENDENT`. The queue has no consumer that produces
   qualified automatic assessments. An automated bot needs a separately validated
   machine-assessment policy. Preserve truthful reviewer/model provenance; never
   label assistant work as human or simply remove the guard without replacing its
   purpose with measured eligibility rules.

4. **Positive economic examples are missing from the reviewed evidence.** The
   [completed development review](economic-validation-2026-09-28.md) covers 39
   revisions: 18 baseline and 21 nonbaseline. None supports a before-and-after
   physical crude operating transition. This does not imply that no such events
   occurred in the world. The new run contains 159 story revisions, seven queued
   baseline candidates, zero forward queue candidates and zero fast events as of
   the read-only check. Most of that run was initial capture, not new information.

5. **The project does not yet measure its central economic question:** whether
   price movement remains after this system actually receives, interprets and
   acts on a report. There is no qualified synchronized live market archive. The
   feature gate uses approximately receipt-to-decision movement; its other windows
   end at decision time, which is different from measuring repricing before receipt.

6. **Existing research statistics need strengthening before they can govern risk.**
   Support is a median of episode-average signed midpoint returns, not estimated
   mean net expectancy with uncertainty. Training excludes outcomes without a
   fully closed hypothetical position. That conditioning requires explicit
   missingness/fill diagnostics and sensitivity analysis. The exact context key
   combines many fields while demanding 20 matching episodes, creating likely
   sparsity. The configured thresholds and fees are draft assumptions.

**Recommended sequence and completion gates**

Work on event quality, runtime reliability and market-data qualification in
parallel. Start prospective market recording once its provider/entitlements are
qualified; do not wait for a large set of rare positive news examples. Historical
data can help develop the system, but cannot recreate the missing local receipt
timeline of an unrecorded prospective experiment.

| Phase | Deliverable | Exit evidence |
| --- | --- | --- |
| 0. Stable deployment | Versioned release, persistent collection service, independent health checks and gap ledger | Reboot, process-kill and network-loss recovery demonstrated; checksums, backlog and source freshness observable |
| 1. Unified event contract | Queue-to-assessment-to-transition integration, including reports without fast-event matches | Real/synthetic cases traverse one causal path; later evidence never changes an earlier decision |
| 2. Automated evidence assessment | Rights-gated extraction, deterministic validators, novelty and episode-state handling | Frozen benchmark and prospective error/latency reports; uncertainty consistently abstains |
| 3. Qualified market recording | CL1, CL2 and MCL1 contract-specific quotes/trades/status with receipt provenance | Measured continuity, recovery, timestamp quality, contract mapping and data-mode verification |
| 4. Response research | Receipt-relative features and executable outcome dataset tied to economic transitions | Reproducible event-response tables, complete denominators, episode/time-separated evaluation |
| 5. Frozen signal policy | Versioned TRADE/ABSTAIN decisions with calibrated residual-return and cost estimates | Evidence of net benefit versus baselines survives uncertainty and independent episodes |
| 6. Paper OMS and risk | Persistent orders, positions, exits, reconciliation and fault handling | Deterministic replay and prospective paper campaign; execution/accounting faults reconciled |
| 7. Broker integration | Adapter behind the same OMS/risk interfaces | Paper-account reconnect/reconciliation tests and explicit live configuration readiness |
| 8. Limited live evaluation | Separately approved, tightly bounded live campaign | Observed fills/costs/risk match tested assumptions before any scaling |

Passing a phase's engineering tests does not waive its data or economic gate.
Broker simulator work can be developed before the strategy qualifies, but live
order submission remains contingent on later evidence and explicit authority.

**Phase 0: make the experiment durable**

Recover and document the current stopped run first. Preserve the last good
heartbeat, boot/session identity, missing interval, source circuits and immutable
records. Choose either a properly supervised WSL deployment with understood
Windows lifecycle behavior or an always-on Linux host. A persistent user unit
alone cannot keep WSL collecting while Windows is off.

Build a supervisor unit whose code/config paths are explicit and validated, with
boot/restart behavior tested on the chosen host. The existing service template
uses `%h/oil`, which does not match this checkout's location. Add independent
heartbeat and source-freshness checks, disk-space and storage-growth limits,
bounded retention procedures, backup checksums and an off-host restore exercise.
Monitor policy/review expiry as well as process liveness. A running process with
stale sources or an ever-growing queue is not healthy collection.

Use a proposed seven-day operational canary after fault tests. Seven days is an
engineering observation window, not proof of strategy quality or sufficient event
support. Extend it when gaps, incidents or outages remain unexplained. Add CI or
an equivalent reproducible release check for the maintained test suite, schema
contracts and snapshot replay. No production deployment should depend on which
editable installation happens to be on the developer's PATH.

**Phases 1–2: turn collected text into auditable automated assessments**

Define a canonical economic-transition record shared by forward collection and
research. Include a transition ID; episode/incident IDs; source and claimant;
asset identity/type; evidence class; assertion; state before/after; disruption or
restoration family; mechanism; novelty; lifecycle state; evidence spans; all input
revision IDs; assessment/model/policy version; receipt, extraction-completion and
decision timestamps. Keep hypothesis direction distinct from order authorization.

Normalize existing naming differences, including `disruption` versus
`physical_disruption`, uppercase versus lowercase states, `->` versus `_to_`, and
`hypothesis_direction` versus numeric strategy direction. Preserve old schemas and
add explicit adapters rather than rewriting historical rows.

Allow a captured story/queue entry to receive a new operational assessment even
when no headline classifier event exists. Its availability begins when the
assessment finishes. Do not fabricate an event at the old news-receipt time.
Persist queue resolutions, supersession, retries, failed assessments and exclusions.
Provide an append-only PENDING → ASSESSED/ABSTAINED/FAILED lifecycle with an as-of
view so the same report is not repeatedly treated as new.

Maintain prior operating states across separately dated reports from an episode.
The current bridge requires prior-state support inside the same bound story,
which is conservative but excludes legitimate multi-report transitions. Extend
it to cite earlier source revisions that were actually available then. Freeze
as-of grouping, retain conflicting links, distinguish syndication from independent
evidence, and never convert silence in a rotating feed into restoration/withdrawal.

Use the existing structured extractor as a starting point, after source-specific
processing permission and latency/cost qualification. Keep model output limited
to evidence extraction; deterministic code owns eligibility, prices, quantity and
risk. Add source identity, literal-span, assertion, operational-state, commodity,
asset-ownership and lifecycle checks. Planned reopening, repair completion and
military statements alone do not establish resumed crude flow. Refinery demand
effects must not be mixed into crude-supply-loss labels by default.

Define automatic eligibility as a measured policy, separate from whether a label
was independently audited. Track model provenance truthfully. Ambiguous reports
can abstain without requiring the user to label every item. Build a benchmark
covering actual disruption, partial/full restoration, negatives, proposals,
denials, historical reports and corrections. Develop from documented examples,
freeze a disjoint evaluation set, and check claim/episode dependence. Multiple
models agreeing is a diagnostic, not independent ground truth. Any external
adjudication/audit arrangement is a separate resource decision; synthetic cases
test behavior but cannot substitute for real positive evidence.

Measure false eligible transitions, missed captured transitions, unsupported
attribution/state assignments, abstention rate, queue age and end-to-end latency.
Choose tolerances before the evaluation and in relation to the strategy's loss
budget. Do not invent a universal sufficient number of examples or silently
relax rules because the data is sparse.

Source work should prioritize supported operator and port/maritime operational
channels, then incident detail and correction capture. Aramco currently provides
the only enabled operator channel; broad media and military/sanctions listings
cannot substitute for operational reports. ADNOC/Fujairah qualification and UKMTO
access remain open. The pilot's enabled flags do not clear the broader automated
trading/model-use scope; `source-qualification.json` still records pending checks.
Use approved delivery channels and handle image PDFs only under a qualified text
extraction process. Measure unique useful claims and completeness, not feed count.

**Phase 3: record the market alongside events**

Select the market-data provider separately from the eventual execution broker.
IBKR is mentioned as a future provider in project documentation, but no working
account entitlements or adapter are present. Databento is an offline import path,
not an already connected live feed. Confirm account eligibility, permitted data
use/retention, instrument coverage, timestamp semantics, feed granularity, cost
and operating budget before selecting either or another provider.

IBKR's [current API subscription documentation](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/introduction)
states that most API market-data access requires subscriptions. Its
[paper-account page](https://www.interactivebrokers.com/en/trading/papertrader-delayed-data.php)
lists delayed US futures data when unsubscribed. A paper account is therefore
not evidence of a usable real-time research feed. Databento documents a separate
[live API](https://databento.com/docs/api-reference-live?live=raw); that capability
still requires a qualified implementation and access for this project.

Record explicitly identified CL front/second-month contracts and the corresponding
MCL contract. Roles such as CL1 are dated mappings, not untracked continuous-price
symbols. Preserve exchange-event time, provider receipt when available, local
receipt, contract definition, bid/ask and sizes, trades, sequence scope and quality
flags. Leave unsupported fields such as aggressor unknown. Never use CL prices as
MCL executable fills. Brent can follow if the hypothesis needs seaborne-crude basis
information and its own feed/contract qualification is completed.

Implement disconnect/reconnect recovery, deduplication, sequence-gap semantics,
sessions/holidays, roll/expiry handling, clock uncertainty, archive validation and
explicit delayed-versus-realtime status. Record data and clock gaps even when
there is no news. Separate schema support, engineering capture readiness and
economic eligibility; do not flip the existing hardcoded unqualified result to
true merely because packets arrive.

**Phases 4–5: test residual response before building a trading model**

Add explicit pre-receipt windows of 300, 120, 60, 30, 10 and 5 seconds; then
receipt-to-classification and receipt-to-decision windows. Keep existing
decision-relative returns as separate features. All features must use only inputs
available at decision time and return unavailable for invalid/stale/gapped data.
Expose coverage/missingness and input IDs with every feature row. Features should
be stable if future market observations are appended to a replay.

Connect canonical transitions to `MarketView` and the outcome engine. Retain the
existing 1, 2, 5, 10, 30, 60, 120, 300, 900, 1,800, 3,600 and 14,400 second
horizons as descriptive outputs. Predeclare a primary endpoint (the existing
protocol proposes five minutes); do not select the winning horizon afterward.
Measure actual extraction/decision latency and stress realistic slower paths.

Reuse bid/ask, fees, slippage and size-aware execution labels, then validate their
assumptions. BBO displayed size is not a fill guarantee. Report entry failures,
partial exits, unavailable labels and quote gaps with denominators; do not select
only successful closes and present their results as unconditional strategy
expectancy. Separate gross midpoint movement, hypothetical net PnL and actual
future paper/broker fills. Prefer dollars/ticks per contract for risk; percentage
returns need special treatment near zero or negative futures prices.

First produce event-response tables for physical disruption and restoration
separately: before-receipt move, after-decision response, latency, spread/depth,
net outcome, fill availability, source/evidence class and episode count. Compare
no-trade, event direction, price momentum and combined event/market policies.
Keep sanctions, OPEC/EIA, ceasefires and generic military escalation as separate
research families. Inventory surprise would additionally require a timestamped
pre-release expectation, which the current system does not capture.

Use chronological, episode-disjoint partitions with purge windows covering
outcome horizons and consider dependence across a broader crisis. Estimate net
mean expectancy, uncertainty, downside, concentration and sensitivity to costs,
missed fills and latency. A median continuation estimate alone is insufficient.
Only consider hierarchical/shrinkage models after simple descriptive baselines;
fit pooling and parameters on development data alone. Do not solve sparse exact
contexts by cutting the minimum episode count to a few headlines.

If the evidence supports a policy, freeze features, model, support data,
thresholds, horizons, costs, latency, exits and risk rules. A signal contract must
record TRADE or ABSTAIN, reason codes, policy/support hashes, decision time,
instrument, side, expiry, maximum entry price and bounded requested quantity.
Risk approval is separate. Every decision must be journaled before outcomes exist.
If residual response does not survive costs or uncertainty, keep the system as
an observation/research product and reconsider the hypothesis; adding a broker
does not address that failure.

**Phase 6: build paper execution and portfolio risk**

Implement an order-management system (OMS) with durable client/order IDs and
submitted, acknowledged, working, partially filled, filled, cancelled and rejected
states. Add order expiry, cancellation races, duplicate/out-of-order events,
partial fills, stops/targets/time exits, position and cash ledgers, fees,
realized/unrealized PnL and recovery after process failure. Enforce idempotent
intent handling and reconcile external state after uncertainty; do not promise
exactly-once network execution.

Use the same broker-neutral interfaces for local simulation and later broker
execution. Keep a central risk engine between signal and order submission:
position/episode limits, contract and notional bounds, daily/episode loss limits,
cooldown, expiry restrictions and data/source/clock health gates. Include an
operator kill switch that disables entry, cancels working orders and supports an
explicitly defined flattening policy. Recognize that stops and flatten requests
may not fill promptly in a gap or halt. Account for correlated positions across
contracts and headlines from one episode.

Run fault tests for crashes, network loss, stale data, market halts, margin
changes, bad clocks, duplicate fills and rejected orders. Then run a frozen
prospective paper campaign long enough to contain independent useful episodes;
calendar duration alone is not sufficient. Separate software/order-state
correctness from economic performance. Evaluate all trade and abstain decisions,
costs, drawdowns, failed fills and episode concentration. Broker paper fills are
an additional integration check, not proof of realizable live execution.

**Phases 7–8: broker and limited live operation**

After selecting a broker, implement connection/session management, account and
contract discovery, orders, cancellations, fills and position reconciliation.
Keep the strategy unaware of broker API details. Test reconnects, ambiguous
submissions, ID recovery and broker-side protections in the paper environment.
Credentials should be separate from source/model workers and audit logs.

MCL is a candidate for a smaller initial experiment, subject to account,
liquidity and risk-budget choices. CME specifies a
[100-barrel Micro WTI contract](https://www.cmegroup.com/education/articles-and-reports/micro-wti-crude-oil-futures-faq);
its smaller size does not establish suitability or remove gap risk. Use current
exchange/broker contract specifications, margin, fees and expiry rules at
qualification time rather than stale constants or published example margins.

A live campaign requires explicit approval of broker/account, instrument,
maximum exposure, loss budget and operational controls. Start tightly bounded,
compare observed execution with paper assumptions and stop on unreconciled state
or material mismatch. Scaling is conditional on real fill quality and subsequent
evidence, never an automatic consequence of a favorable backtest.

**Immediate implementation backlog**

| Priority | Work item | Concrete acceptance test |
| --- | --- | --- |
| P0 | Record stopped-run gap; implement persistent host-appropriate service and health checks | Reboot test resumes capture with a recorded gap and matching frozen config; no repeated source-circuit bypass |
| P0 | Consolidate tested working changes and release metadata | Clean reviewed release can recreate the deployed bytes; previous experiment identities remain available |
| P0 | Define canonical event/assessment contract and adapters | Body-only candidate becomes a newly dated assessment and research transition without fabricating a historical fast event |
| P1 | Add queue resolution and automated assessment consumer | Retry/restart/correction scenarios produce one current resolution with complete immutable history |
| P1 | Add prior-state/episode evidence across revisions | Earlier valid state can be cited; future corroboration is rejected; reprints do not add independent support |
| P1 | Resolve source and market-provider qualification decisions | Written channel/use/timestamp/entitlement evidence and explicit budget; no activation inferred from HTTP 200 |
| P1 | Implement prospective market recorder | Contract mapping, timestamps, source flags and reconnect gaps survive verified replay |
| P1 | Extend receipt-relative features and bridge-to-dataset integration | Altering future quotes cannot alter saved decision-time features or decisions |
| P2 | Produce first episode-level response report | All sampled/excluded/missing/fill denominators reconcile; no return estimate without data |
| P2 | Add portfolio paper OMS/risk test harness | Crash/reconnect/duplicate/cancel/partial-fill tests reconcile orders, positions and cash |
| Conditional | Freeze predictive policy and prospective paper campaign | Independent evidence supports net expectancy under declared cost/latency assumptions |
| Conditional | Broker adapter and live readiness | Reconciled paper integration, approved limits and separate live authorization |

The next development milestone is complete when the project can prospectively
capture an operational report and synchronized prices, automatically produce a
dated evidence-backed assessment or abstention, reconstruct episode state,
calculate decision-time features, attach later executable outcomes, and reproduce
the result from immutable inputs while surviving ordinary host/network failures.
This milestone does not require or authorize a live order.

**Decisions still needed before dependent work**

The implementation can progress on reliability, schemas, tests and adapter
interfaces now. Paid market-data activation needs a provider, budget and access;
expanded source/model use needs qualified scope; deployment needs a host and
uptime policy; execution later needs a broker/account, instrument and monetary
risk limits. IBKR/MCL remain candidates in the plan, not silently selected account
or trading commitments. Evidence collection time is event-driven and may exceed
engineering implementation time substantially; no profitability or delivery date
for live trading can be inferred from the present dataset.
