# Roadmap implementation, 6 October 2026

The first engineering stages of the [project roadmap](project-potential-and-roadmap-2026-10-05.md)
are implemented: readiness inspection, verified local recovery, read-only curve
recording, a frozen prospective EIA shadow study, and paired economic diagnostics.
Broker orders and strategy promotion remain disabled. This is infrastructure for
collecting evidence; it supplies no estimate of future profitability.

| Roadmap stage | Delivered | Remaining exit evidence |
| --- | --- | --- |
| Reproducible baseline | Existing work preserved; local tests; readiness report; hash-checked SQLite backup and restore exercise | Remote CI observation, off-host retention and continuing source/calendar reviews |
| Market recording | Explicit CL1/CL2/MCL1 discovery, raw-first callbacks, canonical journal, restart gaps, snapshots and monitoring | Logged-in paper session, private account configuration, entitlement/retention review, seven-day observation and independent contract/session checks |
| Frozen experiment | Versioned rules, code bundle, calendar, actual decision clocks, one decision per reporting period, late-window abstentions and locked holdout | Qualified prices and prospective sample accumulation |
| Economic contribution | Paired moving-block uncertainty, comparable baseline populations, cost/latency scenarios, event denominator and fixed-cost break-even arithmetic | Adequate independent observations and credible incremental net contribution |
| Local strategy paper / broker paper / live | Existing local simulator retained | Qualified data, validated exit/risk policy and separate broker execution gates; these stages are not activated |

## Observed baseline and recovery

The starting checkout passed **791 tests**; the implemented package passed
**848 tests** in 109.38 seconds. Dependency compatibility, CLI help and
`git diff --check` passed, and an isolated wheel build succeeded. On 6 October at 06:25:34 UTC,
`research-readiness` reported the existing macro collector **HEALTHY**, with no
pending parses. Its market blockers were `PAPER_LOGIN_NOT_CONFIRMED` and
`EXPLICIT_PAPER_ACCOUNT_REQUIRED`. This is a dated observation, not an uptime guarantee.

`data/backups/roadmap-20261006-v1/recovery-check.json` records a successful restore
exercise for the macro, scheduler, news and forward journals. File hashes,
immutable record identities, state-table hashes and SQLite integrity checks
matched. Each database is a consistent SQLite snapshot; the four backups are
not one atomic cross-journal research snapshot. Private environment files are
excluded, and independent off-host retention remains necessary.

Read current readiness with:

```bash
.venv/bin/python -m oilbot research-readiness
```

The 5 October assessment remains a dated analysis. Its statement that tests and
runtime health could not be checked has been superseded by these observations.

## Read-only CL/MCL recording

```bash
.venv/bin/python -m oilbot market-preflight
```

`configs/ibkr-curve.json` defaults to an unconfirmed paper session and a private
account environment-variable name. Configure a logged-in paper Gateway/TWS,
Read-Only API and the account locally before opening a connection. Do not put an
account identifier or credentials in tracked configuration. Port/account naming
alone does not independently establish paper mode.

After those prerequisites are confirmed:

```bash
.venv/bin/python -m oilbot market-watch --connect \
  --config configs/ibkr-curve.json --root data/market/ibkr-curve-v1
.venv/bin/python -m oilbot market-health \
  --journal data/market/ibkr-curve-v1/market.sqlite3
```

The recorder chooses two unambiguous eligible CL months and an MCL contract
matching CL1, using a seven-day roll buffer. It requires explicit broker dates,
specifications and trading sessions. A missing last-trading time uses the
conservative start of the reported trading day and retains that basis for review.
Ambiguous timezone/DST/session formats fail closed. This is not an independently
verified exchange calendar.

Each raw callback commits before normalization. Canonical availability is the
actual local processing time; the raw callback receipt clock is retained
separately. IBKR's coarse source timestamp is not relabeled as an exchange clock.
Sequences describe local instrument callback ordering. Rejected quotes, queue
overflow, disconnects, stale streams, shutdown and unclean restarts remain in the
gap ledger. No claim of exchange-sequence completeness is made.

The API requests three `BidAsk` subscriptions with `numberOfTicks=0` and
`ignoreSize=False`, retaining size updates without historical backfill. The
official [IBKR request documentation](https://www.interactivebrokers.com/docs/tws-api/doc/market-data-live/tick-by-tick-data/request-tick-by-tick-data)
also describes subscription-line limits. The
[callback documentation](https://www.interactivebrokers.com/docs/tws-api/doc/market-data-live/tick-by-tick-data/receive-tick-by-tick-data)
describes the source timestamps. Provider access and sustained throughput still
need observation on this account and host.

`market-snapshot --journal PATH --through UTC --out NEW_DIRECTORY` exports a
checksum-bound canonical archive. Snapshots and health reports always retain
`market_data_qualified: false`; a successful capture cannot approve its own
entitlements, retention rights, clock behavior or economic fitness.

## Frozen EIA engineering pilot

`configs/eia-shadow.json` predeclares an engineering pilot from **7 October to
31 December 2026**, with holdout beginning **18 November**. The primary outcome
is five minutes after the actual completed decision, with one second of assumed
execution latency. Decisions target receipt plus 60 seconds, with at most five
seconds of worker lateness. Late decisions abstain at the actual current time.

The pilot compares v2, original v1, inventory-only, price-only 60-second momentum
and no-trade on a common execution population. Costs match the v2 policy:
$1/contract/side and one adverse tick/side. Additional round-trip stress is
$0/$2/$5; additional latency is 0/1/5 seconds. These remain assumptions rather
than measured broker fill costs. Fixed monthly expenses are deliberately unset.

The policy retains the roadmap's 52-training/26-holdout-group and ten-trade
screens. **This short pilot cannot satisfy those screens**, even with complete
capture. It validates the forward workflow and produces development evidence.
A subsequent economic qualification study needs a newly frozen prospective
protocol, a renewed calendar, qualified prices and materially longer observation.
Do not lower the sample screens or reuse this pilot's inspected holdout to claim
an edge.

```bash
.venv/bin/python -m oilbot shadow-freeze \
  --spec configs/eia-shadow.json --calendar configs/macro-scheduler.json \
  --out data/research/protocols/eia-v2-forward-20261006.json
```

Freezing writes a hash-bound protocol and an adjacent source-code bundle. It
refuses retroactive freezes and existing outputs. The worker checks protocol,
code and bundle identities, uses only point-in-time macro/market inputs, and
records one immutable decision per EIA period. A computation's completion time
becomes the outcome/entry anchor; receipt-time hypothetical decisions are not
substituted. Source corrections known before processing veto the original.

Running without prices is supported: it records unpriced abstentions, with null
outcomes. It never treats missing prices or incomplete exits as zero P&L.

The bounded reviewed calendar expires at the start of 2027. The worker stops on
calendar expiry or the declared experiment end. A future study must use a newly
reviewed calendar and protocol. Frozen source bundles currently share the local
Python environment; dependency isolation and off-host execution remain work.

## Persistent shadow worker

`scripts/install_shadow_service.py` first prepares reviewable worker/health units
in the workspace, then separately installs them without replacing existing units.
It starts no market recorder and supplies no broker credentials.

```bash
.venv/bin/python scripts/install_shadow_service.py \
  --protocol data/research/protocols/eia-v2-forward-20261006.json \
  --macro-journal data/macro/20260929-official-v1/macro.sqlite3 \
  --market-journal data/market/ibkr-curve-v1/market.sqlite3 \
  --root data/shadow/eia-v2-forward-20261006 \
  --out data/deployments/eia-shadow-20261006-v2
.venv/bin/python scripts/install_shadow_service.py \
  --out data/deployments/eia-shadow-20261006-v2 --install --start
```

The units run the frozen source bundle. `oilbot-eia-shadow.service` records
decisions; `oilbot-eia-shadow-health.timer` checks its journal every minute.
Health reports heartbeat age, missing expected decisions, abstentions and late
decisions, without exposing holdout P&L. WSL host downtime is still unobserved
time. Local logs do not provide an independent alert channel.

Deployment `eia-shadow-20261006-v2` is installed and started. Systemd validation
passed; the worker is enabled/active/running, the health timer is enabled and
waiting, and its health oneshot exited successfully. The pilot reports
`WAITING_FOR_START` for 7 October at 00:00 UTC. The market recorder is not running.
The rejected v1 unit files are retained locally; validation stopped that attempt
before installation. The v2 correction removes quoting around `WorkingDirectory`.

These prepare/install commands document the completed deployment; do not rerun
them against the existing destination or units. The installer deliberately refuses
overwrites. For an independently versioned deployment, use new output paths and
review the existing unit ownership first.

For rollback, disable/stop only the new worker and health timer. Keep the
protocol, source bundle and shadow journal for inspection and recovery.

## Export and evaluate

`shadow-dataset` reads the recorded decisions and adds matured hypothetical MCL
execution labels. It does not rerun earlier strategy decisions. The denominator
retains expected release groups, captured groups, capture/parse status counts,
missing decisions, abstentions, unpriced labels and comparison exclusions.

`shadow-evaluate` uses the frozen split, horizon, sample screens, costs, latency
and paired block-bootstrap settings. The general `macro-evaluate` path enforces
the same protocol constraints for a forward dataset. Both keep the holdout
locked until the declared end. Blocks contain four adjacent comparable release
groups, with 2,000 seeded resamples and descriptive 95% percentile intervals;
calendar gaps, missingness and repeated research can undermine these intervals.

Economic reports show net contribution per comparable release, paired incremental
P&L against each baseline, concentration/tail diagnostics and the additional
round-trip cost that would exhaust measured mean trade contribution. Optional
fixed-cost break-even arithmetic does not predict monthly trades or income.
Chronological closed-label drawdown remains distinct from marked portfolio
drawdown. No output authorizes trading or strategy promotion.
