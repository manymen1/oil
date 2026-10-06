**Oilbot: project potential, profitability assessment and roadmap**

Assessment date: 5 October 2026. Scope: the current standalone checkout at /home/tstuv/oiltrading/oil, including the uncommitted geopolitical research additions. This assessment reviews source code, configurations, saved datasets/evaluations and implementation documents. External contract specifications, published pricing and research were checked against primary sources. Project Python and systemd commands could not be launched in this session; no new test pass, live service-health result, broker connection or market-data entitlement is claimed.

**My recommendation is to continue developing oilbot as a bounded research project.** Prioritize qualified market recording and one frozen economic experiment. The present evidence does not support allocating trading capital, estimating annual returns or expecting monthly income.

The project has useful engineering assets: immutable capture history, causal availability timestamps, corrections, candidate and episode lineage, conservative exclusions, instrument-aware execution labels, baseline comparisons and a durable local paper engine. These reduce the chance of fooling ourselves. They do not establish that a profitable signal exists.

The economic question is precise: after this system receives and processes an event, does a further executable MCL price move remain that exceeds variable execution costs, fixed operating costs and risk? Correctly interpreting a headline or identifying an oil-market mechanism is only an input to that question.

The following assessment distinguishes implemented capabilities from measured readiness.

| Area | Evidence inspected | Assessment |
| --- | --- | --- |
| News recording | README, forward recorder, operational configuration and frozen run configuration | Implemented with raw-before-parse journals, revision history and local receipt/availability clocks. Timely, complete news coverage is unqualified. |
| Official macro data | macro.py, macro scheduler documentation, inventory dataset metadata | EIA table 1 and CFTC capture implemented. Current inventory research export contains two EIA release groups; neither is comparison-eligible. |
| Inventory detail | eia_detail implementation and 5 October collection runbook | Separate hourly Cushing/refinery collector deployed and recorded healthy at 08:06:16 UTC on 5 October. Hourly context cannot be treated as simultaneous fast-release data. |
| Weather/BSEE | oil-price driver/source implementation notes | NHC status and explicit-report BSEE parsers exist. Continuous weather collection, current BSEE discovery and verified asset exposure remain incomplete. |
| Inventory strategy | inventory_strategy.py, inventory_v2.py and macro_evaluation.py | Draft continuation hypothesis with CL/MCL agreement, persistence, curve and cost filters. No measured edge. |
| Geopolitical strategy | uncommitted geopolitical implementation and saved evaluation | Separate risk-premium and physical-transition lanes. Eight saved candidate groups, no qualified market observations or comparable P&L. |
| Economic interpretation | economic.py and assessment_queue.py | Assistant reviews remain ineligible for the physical research bridge. Automatic admission can only abstain or fail; it is not an economic interpreter. |
| Paper execution | paper.py and paper-execution.md | Durable single-contract local simulator with intents, partial fills, exits, risk triggers and reconciliation. No prospective strategy campaign demonstrated. |
| Broker connectivity | ibkr.py, ibkr_transport.py and ibkr-paper.json | Read-only bounded MCL probe implemented. Configuration still leaves paper-session confirmation false. Broker order methods are disabled. |
| Economic validation | latest stored inventory and geopolitical evaluations | Both report INSUFFICIENT_EVIDENCE; expected profit, P&L and drawdown are unavailable. |

The inventory export is through 2026-10-05T08:19:05Z and contains two release groups with zero comparison-eligible rows. Its exclusions include missing qualified market features, unbounded release-detection lag and initial/baseline constraints. Both the training and holdout comparison populations are empty. Null P&L means unmeasured, not zero profit or proven failure.

The geopolitical export uses a snapshot ending 2026-09-28T16:55:00.903314Z, despite a directory name that begins 20260929. It contains seven physical-review candidates and one risk-premium candidate. All eight lack market data and fail publication freshness; seven are initial or reinterpreted captures. It is an old-snapshot software check, not eight current independent trading opportunities.

The 28 September assistant economic screen inspected 39 selected revisions and found no supported before/after physical crude operating transition. The sample was stratified development evidence and had informed classifier development. It does not estimate population accuracy or demonstrate that physical events never occur.

The last documented news health check, at 14:35:09 UTC on 5 October, was DEGRADED. Al Jazeera and CENTCOM circuits were open, and Aramco/OFAC first-party identity reviews had expired. The frozen configuration confirms 4 October expiry timestamps and pending model-processing rights. A successful HTTP response cannot establish complete, timely or independently corroborated news.

The 5 October hardening runbook records 725 passing tests before the current geopolitical additions. Inspected tests cover future-data invariance, cost stress, related-event grouping, missing labels, crashes, ledger consistency and authorization guards. Test quality is useful; this session did not rerun the suite or measure coverage. The CI workflow exists, but an observed successful remote CI run was not verified.

Older roadmap documents are partly superseded. In particular, statements that the local paper engine or IBKR probe do not exist are obsolete. Current code and newer implementation records control this assessment.

The research hypotheses differ in opportunity, difficulty and current evidence.

| Hypothesis | Potential source of value | Main obstacle | Research priority |
| --- | --- | --- | --- |
| EIA continuation | Residual price movement after a repeatable inventory announcement | Weekly changes are not surprises; detection/confirmation delay can consume the response | First experiment |
| Geopolitical risk-premium continuation | A timely novel claim changes perceived supply risk without proving lost barrels | Reprints, ambiguous signs, stale feeds, episode dependence and reversals | Second, after price and news qualification |
| Physical disruption/restoration | Accurate operating-state changes distinguish actual effects from proposals and commentary | Rare positive examples, full-text source gaps and no validated automatic interpreter | Longer-term differentiation |
| Weather and operator reports | Link storm developments to actual offshore, refinery or transport changes | Storm proximity is not an outage; crude supply and refinery demand can imply different effects | Context before a strategy |
| OPEC/sanctions | Changes to future production or effective market access | Announcement, implementation and realized flows differ; no automatic sign | Defer until core studies work |
| CFTC positioning | Slow crowding/regime context | Publication lags reporting date; predictive incremental value is untested | Covariate, not a fast signal |

This ordering is a judgment about testability and implementation fit, not a forecast that the first strategy will be most profitable.

The plausible route for this stack is continuation over minutes or longer after the actual local decision. The recorder polls no faster than one minute, and the draft strategies add 60 seconds of confirmation after receipt. Processing and hypothetical execution add further delay. This makes the first price response a difficult target. The remaining move must be measured at the real latency distribution; it cannot be inferred from a headline's total subsequent return.

A [Bank of Canada study](https://www.bankofcanada.ca/wp-content/uploads/2020/03/swp2020-8.pdf) measures inventory news as actual changes minus pre-release survey expectations and documents oil-price responses in minutes around announcements. Its sample is historical, and it does not establish a profitable strategy for this project's receipt times, MCL instrument or costs. It does support examining surprises rather than equating stock changes with new information.

For example, an actual 3-million-barrel draw is less tight than an expected 6-million-barrel draw. An unconditional draw-means-long policy can give the wrong informational sign. The current code explicitly reports inventory_surprise as null and consensus_available as false. Add a rights-qualified, timestamped consensus vintage or a separately trained, strictly prior seasonal expectation model; a model residual must retain its own provenance and is not market consensus.

CL1 and MCL1 measure closely related WTI exposure. Agreement is a useful consistency check, not two independent pieces of economic confirmation. Persistence, term-spread agreement and cost filters must earn their place through ablation on the same eligible events.

A move already observed before entry is not remaining profit. The v2 screen compares the observed MCL move to 1.5 times estimated costs. Passing that screen can still precede reversal. Its value depends on subsequent net performance, not the size of the confirmation move itself.

**Profitability depends on executable expectancy and opportunity frequency.** MCL is 100 barrels, with a $0.01/barrel tick worth $1 per contract. CL is 1,000 barrels, with a $10 tick. [CME specifications](https://www.cmegroup.com/education/articles-and-reports/micro-wti-crude-oil-futures-faq). A $0.10/barrel movement is therefore $10 on one MCL. Larger contract size also increases losses and exposure; it does not repair an absent signal.

For a one-contract research case:

Net trade expectancy = p × average gross winning P&L − (1 − p) × average gross losing magnitude − average variable round-trip cost.

Monthly operating contribution = trade count × net trade expectancy − fixed monthly costs.

If entry and exit P&L already use executable bid/ask fills, spreads are already included. Do not subtract spread twice. Gross midpoint examples must include spread separately. Operating contribution is before taxes, development labor and any omitted infrastructure or subscription expense; it is not an annual return on account equity.

The v2 default cost screen assumes $1 per contract per side, one tick of adverse slippage per side and the observed spread. A two-tick spread produces a $6 estimated round trip. This is an illustrative policy assumption, not a measured broker fill cost.

IBKR's published lowest-volume MCL broker commission is $0.25 per side; its published NYMEX recovery charge is $0.50 in Tier I, with additional regulatory charges. [Commission schedule](https://www.interactivebrokers.com/en/pricing/commissions-futures.php), [NYMEX charges](https://www.interactivebrokers.com/en/accounts/fees/NYMEX.php). Account entity, plan and applicable charges must be verified. The broker commission alone is not the all-in trade cost. Research should retain base assumptions, adverse spread/slippage scenarios and measured execution costs when available.

Databento's checked pricing page lists a $199/month Standard offering with live data and separate usage-based historical access. [Published pricing](https://databento.com/pricing). The public price does not establish eligibility, dataset/schema entitlement or the allowed automated use for this account. Obtain the actual qualified quote before selecting a provider or spending money.

Consider illustrative fixed costs of $250/month and variable costs of $6 per round trip, trading one MCL:

| Monthly trades | Required mean P&L after variable costs to cover fixed costs | Required gross midpoint expectancy if variable costs average $6 |
| ---: | ---: | ---: |
| 4 | $62.50 | $68.50 |
| 10 | $25.00 | $31.00 |
| 40 | $6.25 | $12.25 |

These are break-even requirements, not forecasts or actual project expenses. An EIA-only strategy has about four to five release opportunities per month before exclusions. At one MCL, high fixed costs are therefore a substantial business constraint.

A second illustration: 55% winners, average gross winner $30, average gross loser $20, and $6 variable cost produces $1.50 expected contribution per trade. Forty such trades contribute $60 before fixed expenses; after $250 fixed costs the result is −$190/month. Under those same assumptions, 52% wins breaks even at the trade level, while 64.5% wins is needed to cover the fixed costs at 40 trades/month.

A different illustration, forty trades averaging $8 after variable costs, gives $70/month after $250 fixed costs. Neither illustration is supported as this project's likely performance. More trades or contracts should not be added simply to make fixed-cost arithmetic attractive; conditional edge, liquidity and risk must support them.

No credible annual return, Sharpe ratio, monthly income forecast, success probability or required trading bankroll can be inferred from the current data. Account equity and current broker margin are also unknown. Margin is a collateral requirement, not a safe operating capital estimate.

These gaps have the greatest effect on the path to profitability:

1. Qualified market observations are absent. Record synchronized contract-specific CL1, CL2 and MCL1 quotes, definitions, sessions, statuses, availability and gaps. The existing MCL-only IBKR probe cannot supply the entire v2 feature set.
2. The capture-to-research-to-paper handoff is incomplete. A research candidate is not a PAPER_INTENT; the local simulator requires explicit instrument, price, quantity and paper-only policy.
3. Physical semantic automation is unqualified. The economic bridge rejects assistant reviews, and admission always includes AUTOMATED_ECONOMIC_POLICY_UNQUALIFIED. Preserve these boundaries while building a separately benchmarked machine-assessment policy.
4. News selection and access limit coverage. Current sources are a small public slice; listing titles and RSS snippets may omit states, capacity and duration. No catalog count proves activated or complete coverage.
5. Decision latency is not economically measured. News publication, local receipt, parse completion, classifier completion, assessment completion and order arrival require separate clocks and observed delay distributions.
6. The evaluators are research screens, not statistical or portfolio certification. They report hypothetical fixed-horizon P&L and chronological drawdown; they do not establish mean expectancy uncertainty, a full concurrent portfolio, broker fills or actual stop behavior.
7. Fixed costs and opportunity frequency may dominate micro-contract returns. A measurable positive trade expectancy can still have negative operating contribution.
8. Operational readiness is unfinished. WSL cannot collect when its host is off. Frozen deployments share a Python environment; off-host recovery, retention, independent alerts and the 2027 calendar renewal remain work.

There is also a schema/continuity issue to resolve deliberately: the local paper engine requires instrument-scoped consecutive quote sequences, while the IBKR probe describes local callback ordering without exchange sequence guarantees. A qualified adapter must preserve what the provider actually supplies. Inventing exchange continuity would conceal gaps.

The older residual strategy requires at least 20 matching historical episodes across a context containing asset type, confirmation, duration, severity, move/volatility bins and spread. Rare events can make these cells sparse. Use diagnostics to assess sparsity and a predeclared simpler or pooled model if justified; do not loosen thresholds opportunistically to produce trades. Its median episode-return support is also different from expected net mean profit.

**Credible validation needs a complete event denominator and comparable baselines.** Maintain the complete denominator: expected releases/stories, captures, parse success, candidates, economically eligible groups, priced groups, abstentions, intended trades, fills, incomplete exits and resolved P&L. Missing outcomes are neither wins nor zero losses.

The primary economic comparison should be net contribution per eligible event and incremental performance over the best simple baseline. Mean per-trade P&L is secondary because a highly selective strategy can show favorable conditional trades while generating too little total value.

Use the existing no-trade, inventory/news-only, price-only and original strategy baselines. Keep evaluation populations and assumed costs comparable. Add a price-only policy with matched timing/filter opportunity where needed, so favorable results cannot be explained solely by selecting liquid trending intervals.

Freeze rules, horizon, cost assumptions, grouping, exclusions and split before a new evaluation. Count releases and economically related episodes, not revisions or reprints. Preserve all experiments and any rejected strategies; repeated holdout inspection contaminates later conclusions.

Report mean net expectancy and uncertainty, paired incremental P&L versus baselines, chronological marked-equity drawdown, drawdown duration, tail loss, adverse excursion, fill and missingness rates, latency sensitivity and cost stress. Block resampling at release/episode or appropriate temporal-group level is a candidate uncertainty method; the method and dependence assumptions must be predeclared and checked.

Evaluate slower-than-assumed decisions, spread widening, adverse slippage, dropped sources, delayed assessments, clock faults, partial exits and unresolved positions. A fixed additional $2 screen is helpful but cannot replace measured distributional stress.

Inspect results by broad predeclared market conditions and long/short side. Do not subdivide a tiny sample into enough regimes to manufacture a winner. If all gains come from one episode, report that concentration plainly and require additional evidence.

The macro screen's defaults are 52 training groups, 26 holdout groups and at least 10 holdout trades. These are draft sample screens, not statistical significance. At one EIA release per week, 78 qualified groups require about 18 months before exclusions, and the holdout alone requires about six months. Ten profitable trades do not prove an edge.

Historical research can accelerate rejection of weak hypotheses if genuine release vintages, original availability and suitable contract data exist. Current macro imports are marked baseline and excluded. Build a separate historical-study contract that labels simulated receipt latency and its limits. Never relabel an import as a historical forward HTTP receipt or pretend to reconstruct missing local clocks. Historical findings still require prospective replication.

**Risk qualification must reflect the actual exit policy and full account exposure.** Current paper defaults include one contract, five daily trades, a $100 daily loss trigger, a 20-tick stop and a 40-tick target. On MCL the stop/target price distances are $20/$40 before friction. These values are engineering settings, not an approved risk budget or a validated exit policy.

Research labels use fixed horizons; the paper engine has stops, targets and maximum holding time. They can produce materially different outcomes on the same path. Choose and validate the actual exit policy, then replay the complete marked portfolio rather than transferring fixed-horizon results directly into stop/target claims.

The local engine reconciles its own ledger but has no real account equity, changing margin, broker execution ingestion or cross-journal aggregate exposure. Shared exposure across macro/news lanes requires one risk budget. Their separate hypothetical P&Ls cannot simply be added.

Broker integration needs durable intents and submission-uncertainty recovery, stable identifiers, partial-fill handling, execution and commission reconciliation, reconnect snapshots, external exposure detection and tested protective/kill behavior. A process restart must not duplicate an order. A kill trigger does not guarantee flattening without executable liquidity.

Any later position sizing should use account equity, adverse stop/slippage/gap scenarios, margin headroom and portfolio loss limits. It must not treat one simulator stop or a broker minimum margin as a guaranteed maximum loss.

**The roadmap advances through evidence gates.** Timings below are planning estimates for engineering effort assuming access and inputs are available. They are not promises or substitutes for sample accumulation.

| Phase | Indicative engineering window | Concrete deliverable | Exit evidence |
| --- | --- | --- | --- |
| 1. Establish a reproducible baseline | Weeks 1–2 | Review and version current work; reconcile dated docs; rerun tests/CI; inspect frozen collectors, source reviews, gaps and calendar; verify backup restore | Reproducible code/config/data identities, observed health, documented exclusions, successful recovery exercise |
| 2. Qualify market recording | Weeks 1–4 | Choose an authorized provider/use case; extend beyond MCL probe; canonical CL1/CL2/MCL1 archive, continuous read-only collection and monitoring | Correct contracts/modes, bid/ask/depth, clocks, sessions, gaps, reconnect and roll behavior; no silent fallback |
| 3. Freeze one experiment | Weeks 2–5 | EIA-v2 versus original/inventory/price-only/no-trade baselines; predeclared decision timing, horizon, grouping, missingness and cost policy | Signed/versioned protocol before prospective evaluation, causal end-to-end engineering replay |
| 4. Evaluate economic contribution | Initial research Weeks 4–10; prospective duration depends on sample | Genuine-vintage historical study where possible; separate locked forward dataset; uncertainty, ablations, timing and cost sensitivity | Positive, stable incremental net value after realistic stress, sufficient effective sample and complete loss/missingness reporting |
| 5. Run prospective local paper | Integration Weeks 6–12; campaign extends for enough opportunities | Frozen policy emits explicit paper-only intents; full exits/risk/marked equity and shared exposure accounting | Forward reproducibility, resolved positions/ledgers, measured delays/cost assumptions, recovery tests and adequate event support |
| 6. Add broker paper execution | Only after prior qualification; several further engineering weeks | Separately authorized paper adapter, durable submission state, fill/commission/position reconciliation and protections | Paper-account fault/reconnect tests, no duplicate orders, complete accounting and measured execution differences |
| 7. Consider bounded live evaluation | Conditional; no present date | Separately approved instrument/account, exposure/loss limits, funding/entitlements and monitored campaign | Live fills and risks agree sufficiently with qualified assumptions before scaling |

A two-to-three-month engineering campaign could deliver a much more complete research/shadow system if prerequisites are available. It cannot, by itself, satisfy an EIA experiment that needs 26 new holdout releases or rare physical episodes. Expect economic qualification to take materially longer; no live date is supported.

Review the calendar before 2027-01-01. Renew source identities only from current evidence and use authorized source-recovery procedures. Source acquisition, broker login and paid subscriptions require their actual user/account prerequisites; this assessment does not activate any of them.

Allocate the next development effort approximately as a provisional priority: 40% market data and integration, 30% economic evaluation, 20% operational reproducibility, and 10% targeted source/semantic improvements. Change that allocation when measured blockers change. Avoid broad feed expansion or model complexity before prices and a baseline study can show whether additional information matters.

The first concrete deliverable should be a verified, continuous CL1/CL2/MCL1 archive aligned with prospective EIA receipts and frozen shadow decisions. Then ask whether the EIA information improves over comparable price-only momentum.

**Research decisions should depend on incremental net value.** Continue a hypothesis when it shows incremental net value with credible uncertainty and survives realistic costs, latency and outlier checks in newly collected evidence. Advance its engineering only to the next appropriate gate.

Simplify or reject it when the best price-only baseline performs as well, required reaction is over before the real decision, realistic friction eliminates the gain, or performance depends on reused holdouts, one event, incomplete exits or favorable missingness.

Inconclusive evidence means more appropriately designed observation, not an automatic profitable/failed verdict. Apply a predeclared research budget and review date so uncertainty does not become endless feature-building.

Even if trading hypotheses fail, the provenance-rich capture archive and evaluation framework retain research utility. No market demand, revenue potential or licensed redistribution model has been validated for a separate commercial data product.

**The next investment should fund evidence about profitability.** Fund the next stage as an experiment with no assumed trading revenue. Complete synchronized market recording, freeze one continuation study, measure incremental net expectancy and operating contribution, and use the results to decide whether broker-order development is worth pursuing. The potential lies in disciplined selection and reliable evidence about residual moves; profitability remains an open empirical question.

The supporting project files are listed below:

- README.md.
- src/oilbot/inventory_strategy.py; src/oilbot/inventory_v2.py; src/oilbot/macro_evaluation.py.
- src/oilbot/geopolitical.py; src/oilbot/geopolitical_evaluation.py.
- src/oilbot/economic.py; src/oilbot/assessment_queue.py.
- src/oilbot/strategy.py; src/oilbot/outcomes.py; src/oilbot/paper.py.
- src/oilbot/ibkr.py; src/oilbot/ibkr_transport.py; configs/ibkr-paper.json.
- data/research/inventory-v2-20261005-dataset/dataset.json.
- data/research/inventory-v2-20261005-evaluation-final.json.
- data/research/geopolitical-20261005-v1/dataset.json.
- data/research/geopolitical-20261005-v1-evaluation.json.
- docs/collection-hardening-2026-10-05.md; docs/inventory-continuation-logic.md.
- docs/geopolitical-news-research.md; docs/economic-validation-2026-09-28.md.
- docs/ibkr-paper.md; docs/paper-execution.md; docs/oil-price-drivers-and-sources.md.
- configs/paper-limits.json; data/runs/20260928T094500Z-operational-v1/config.yaml.
- .github/workflows/tests.yml and inspected research/paper regression tests.

External primary sources checked:

- [CME Micro WTI specifications](https://www.cmegroup.com/education/articles-and-reports/micro-wti-crude-oil-futures-faq). Contract/tick specifications are used; the FAQ's explicitly dated 2021 margins are not treated as current.
- [IBKR futures commissions](https://www.interactivebrokers.com/en/pricing/commissions-futures.php) and [NYMEX recovery charges](https://www.interactivebrokers.com/en/accounts/fees/NYMEX.php). Published pricing is not the account's verified all-in fill cost.
- [Databento pricing](https://databento.com/pricing). Provider selection remains contingent on applicable entitlement, use and total cost.
- [Bank of Canada inventory-news research](https://www.bankofcanada.ca/wp-content/uploads/2020/03/swp2020-8.pdf). Historical empirical mechanism evidence is separate from this project's trading performance.
- [CFTC reporting explanation](https://www.cftc.gov/MarketReports/CommitmentsofTraders/AbouttheCOTReports/cot_about.html). Reporting-date positions are distinct from publication availability.

Prior project memory was used to locate the research and scheduler workflows. Current files supersede older implementation/status notes. This document makes no new live-status claim.
