# Official macro sources and inventory strategy research

Implemented 29 September 2026; batch research added 30 September. This is a separate structured-data/research path;
the frozen news/admission services and broker-execution settings are unchanged.
No model receives these data, and no new strategy can emit paper or broker orders.

## Sources now implemented

| Source | Data captured | Research role | Important timing limit |
| --- | --- | --- | --- |
| EIA WPSR table 1 | Commercial crude excluding SPR, SPR, gasoline and distillate stocks: current level, prior level, reported weekly difference | Inventory-change event hypothesis and product confirmation | Week-ending date is not release time; local receipt and later parsing are retained separately |
| CFTC disaggregated futures-only, code 067651 / NYME | WTI physical open interest and managed-money long, short, spreading and net positions | Slow positioning context | Positions are dated before publication; never backdate their availability to the reporting date |

Sources: [EIA weekly report](https://www.eia.gov/petroleum/supply/weekly/),
[table 1 units and rounding notes](https://www.eia.gov/petroleum/supply/weekly/pdf/table1.pdf),
[CFTC disaggregated futures-only report](https://publicreporting.cftc.gov/stories/s/Disaggregated-Futures-Only/ubmb-6exi/),
[CFTC API/report guide](https://publicreporting.cftc.gov/stories/s/COT-Help/p2fg-u73y/).

EIA stock quantities are **million barrels**. The later part of its table uses
thousand barrels per day; that section is deliberately not read by the stock
parser. Reported differences are retained, with a small consistency tolerance for
independent rounding. Missing categories, duplicate rows, invalid numbers or
unknown table layouts fail parsing rather than silently becoming zero.
CFTC values are contracts across the specified WTI market, not MCL contract
positions or execution liquidity. Spreading is kept separate from long-minus-short.

Both official endpoints returned data in one-off development probes. The EIA
canonical URL is `https://ir.eia.gov/wpsr/table1.csv`; it currently issues a
same-host redirect to a signed release URL. Only that exact HTTPS host/path is
followed, with at most one redirect. Signed URL query strings are not retained
in metadata. No access challenge, proxy, alternate denied endpoint or paid API
is used. UKMTO remains out of scope and blocked as previously recorded.

## Capture and provenance

`macro.sqlite3` is an isolated append-only journal containing policy, attempts,
successful raw response bytes, parse receipts and normalized observation
revisions. Successful bytes commit before parsing. HTTP error status/headers are
retained with error bodies explicitly omitted. Every revision binds its raw
capture/hash and parser version. Repeated identical observations do not create
another event. Corrections append a superseding revision; returning to an earlier
value also creates a revision rather than erasing history.

Initial captures and imported files are baselines. A later correction is not a
new release. Different reporting dates are distinct observations; previous-week
stock figures inside the current EIA release stay part of that release vintage.
As-of reads verify identities, parse receipts and raw data and exclude future
revisions. A recovered parse becomes available when recovery finishes, not at the
earlier receipt. HTTP and local imports cannot share a data root.

The collector performs **one bounded poll per command**. Persisted minimum
intervals are 60 seconds for EIA and one hour for CFTC. These are request limits,
not promised detection latency or an automatically installed schedule. HTTP
401/403 latch a denial; 429 backs off at least a day and honors longer Retry-After
values. No reset command bypasses a denial. Each response is limited to 2 MiB,
and CFTC requests only two dates and the needed columns. Connection/read timeouts
and an elapsed-time budget bound retrieval. Failed raw parses remain auditable;
`macro-recover` completes interrupted, previously unparsed captures.

EIA's [automated-retrieval policy](https://www.eia.gov/about/privacy_security_policy.php)
prohibits excessive activity and requests owner contact identification. EIA
collection therefore requires an email or HTTPS contact URL in
`OILBOT_SOURCE_CONTACT`. The owner supplied contact information on 30 September;
it is saved only in ignored `data/macro/source-contact.env`, with owner-only file
permissions. Source that file before collecting; the CLI does not implicitly
load environment files. The address is not embedded in tracked configuration or
documentation. The official
[reuse policy](https://www.eia.gov/about/copyrights_reuse.php) describes public
government data and attribution requirements; it is not blanket permission for
third-party content, logos or unrestricted request volume.

```bash
# One prospective poll; no account, API key, model or broker connection.
PYTHONPATH=src .venv/bin/python -m oilbot macro-collect \
  --source cftc --out data/macro/20260929-official-v1

# Load the locally configured owner contact without printing it.
source data/macro/source-contact.env
PYTHONPATH=src .venv/bin/python -m oilbot macro-collect \
  --source eia --out data/macro/20260929-official-v1

# Optional local file research: separate root, always marked import/baseline.
PYTHONPATH=src .venv/bin/python -m oilbot macro-import \
  --source eia --file tests/fixtures/eia-stocks-synthetic.csv \
  --out data/macro/synthetic-import

# Complete captures interrupted between raw persistence and parsing.
PYTHONPATH=src .venv/bin/python -m oilbot macro-recover \
  --out data/macro/20260929-official-v1
```

The example fixture is invented engineering data, not an EIA historical release.
Use `--delivery local_import` with recovery for an import-only root. A WAIT result
means the poll interval/backoff prevented a request; FAILED/BLOCKED exits with 2.
No existing news registration, model-processing right or deployed policy is
silently expanded by these commands.

## Release-aware worker and independent health checks

Added 30 September 2026. `macro-worker` is a deterministic collector, not an AI
scheduled task or a strategy executor. Its frozen policy lives in
`configs/macro-scheduler.json`. The calendar is valid from September 2026 through
December 2026, with explicit holiday shifts from the
[EIA schedule](https://www.eia.gov/petroleum/supply/weekly/schedule.php) and
[CFTC schedule](https://www.cftc.gov/MarketReports/CommitmentsofTraders/ReleaseSchedule/index.htm),
reviewed on 30 September. America/New_York conversion handles daylight saving;
a delayed Monday CFTC publication retains its original reporting Tuesday.
These schedules are expectations, not evidence of actual publication times.

- EIA polls at most once per minute from ten minutes before until one hour after
  the expected release. Otherwise it polls every six hours.
- CFTC polls at most hourly during the four-hour post-release window, otherwise
  daily. Its positioning context is not a fast release signal.
- Existing persisted attempt limits, 401/403 denial latches and 429 backoff take
  precedence. Restarting the worker never resets them or replays missed polls.
- An expired or not-yet-valid calendar blocks network collection and reports
  degraded health. Updating it requires a reviewed policy and a new worker-state
  directory/frozen deployment; reuse the capture journal to preserve source latches.
- Interrupted raw captures recover even between scheduled polls, with availability
  recorded at recovery. Concurrent ticks cannot acquire the same writer lock.
- Stop signals interrupt waiting; an already-started bounded collection cycle
  may finish before exit. `--run-seconds` bounds starting new cycles, not cancellation
  of an in-flight HTTP request. Systemd allows 120 seconds for graceful stopping.
- Low free space (under 256 MiB) blocks fetching. Disk/DB failures remain failures,
  not evidence of successful capture.

The sidecar `scheduler.sqlite3` records policy identity, cycle starts/completions,
source results, health checks and state-change alerts. It must be outside the
capture root. Repeated identical issues do not create repeated worker alerts.
Recovery produces another transition; it never fills a past coverage gap.
Health distinguishes stale successful polls, overdue reporting periods,
unparsed captures, denial/rate limits, incomplete cycles and a stale worker
heartbeat. Publication grace is five minutes for EIA and four hours for the CFTC
API; these are monitoring thresholds, not vendor delivery guarantees.

`macro-health` reads the capture and sidecar journals without fetching, creating
missing databases, recovering work or resetting source state. Its nonzero exit
status and the independent timer expose a dead collector. Detailed checks appear
in the local systemd journal; worker alerts are emitted only on issue changes.
There is **no email/off-host notification channel**, and the owner-contact email
is sent only to EIA as client identification, not to CFTC or an alert service.

```bash
# Bounded collection tick with the existing private contact file.
PYTHONPATH=src .venv/bin/python -m oilbot macro-worker \
  --root data/macro/20260929-official-v1 \
  --state data/macro-scheduler/20260930-v1 \
  --contact-file data/macro/source-contact.env --once

# Independent read-only check: exit 0 healthy, 2 degraded.
PYTHONPATH=src .venv/bin/python -m oilbot macro-health \
  --root data/macro/20260929-official-v1 \
  --state data/macro-scheduler/20260930-v1 \
  --contact-file data/macro/source-contact.env
```

The contact file accepts only a single `OILBOT_SOURCE_CONTACT` assignment
(optionally prefixed by `export`). It is parsed as data, never executed, must be
owned by the current user, cannot be a symlink, and requires owner-only permissions.
The address is omitted from scheduler state, unit files and frozen provenance.

`scripts/install_macro_service.py` creates a new frozen source/config snapshot,
hash manifest and three separate user units. It refuses existing deployments or
unit names and does not start anything itself:

```bash
.venv/bin/python scripts/install_macro_service.py \
  --config configs/macro-scheduler.json \
  --root data/macro/20260929-official-v1 \
  --state data/macro-scheduler/20260930-v1 \
  --contact-file data/macro/source-contact.env \
  --out data/deployments/macro-20260930-v1
systemctl --user daemon-reload
systemctl --user enable --now oilbot-macro.service oilbot-macro-health.timer
systemctl --user status oilbot-macro.service oilbot-macro-health.timer
```

Both worker and health service verify the frozen files before starting. They use
the existing virtual environment, so dependency pinning is not full runtime
isolation. The health timer checks once a minute. They need the WSL user service
manager running; they cannot wake Windows or collect while the host is suspended.
Stop collection with `systemctl --user stop oilbot-macro.service`; also stop the
health timer if intentionally retiring the deployment. No existing news worker,
broker connection, market-data subscription or trading permission is changed.

## Concrete draft strategy

`inventory-change-confirmation-v1-draft` tests this hypothesis: a substantial
commercial-crude stock draw/build, corroborated by product stocks and a modest
price move after local receipt, may be associated with further continuation.
A draw proposes long; a build proposes short. This is a hypothesis, not an
established causal relationship or forecast.

The initial engineering settings in `configs/inventory-research.json` are:

- Absolute commercial-crude change at least 2 million barrels.
- Gasoline plus distillate inventory change must have the same sign as crude.
  SPR is retained separately and never added to commercial crude's signal.
- Wait at least 60 seconds after receipt; reject decisions later than 300 seconds.
- Require consecutive weekly release coverage and a successful prior poll within
  180 seconds of receipt. A new file discovered after a long downtime is not a
  fresh release-time opportunity. Publication time remains unknown regardless.
- Require aligned CL price movement after receipt, no more than 0.5%, with a
  fixed CL contract throughout the comparison. This is a no-chasing check.
- Require fresh CL and matching MCL quotes, maximum three-tick spread, positive
  displayed depth, and available five-minute volatility below the draft ceiling.
- Retain as-of CFTC net positioning as a descriptive covariate if no older than
  14 reporting-period days. Missing COT does not manufacture a directional vote.

These thresholds have **not been tuned or validated**. No expected return,
confidence probability, order size or profitability estimate is invented.
Inventory change is not **consensus surprise**: `inventory_surprise` stays null
until licensed/authorized expectations with pre-release timestamps are captured.
No historical first-release or consensus history can be reconstructed from a
current endpoint alone.

`macro-research` can use a verified market snapshot through existing `MarketView`
features. It produces `RESEARCH_CANDIDATE` only if the draft gates pass; otherwise
it gives explicit abstention reasons. **Both outputs always authorize zero
contracts** and are incompatible with the paper engine's explicit PAPER_INTENT
boundary. Without a market snapshot the result abstains; IBKR raw callbacks are
not a substitute for a qualified canonical quote archive.

```bash
PYTHONPATH=src .venv/bin/python -m oilbot macro-research \
  --journal data/macro/20260929-official-v1/macro.sqlite3 \
  --at 2026-09-29T08:00:00Z \
  --rules configs/inventory-research.json
# Add --manifest VERIFIED-SNAPSHOT/manifest.json for causal market features.
# Add --out NEW-REPORT.json to save; existing output files are never overwritten.
```

The timestamp is an explicit as-of query, not a request to backdate an observation.
Reports bind rules, macro inputs and market inputs with hashes. Future observations
and quotes cannot change earlier decision features. Baseline directions for
no-trade, inventory-only, price-only 60-second momentum, and inventory-plus-price
are included for later comparison; these are not fill simulations or profit labels.

## Batch release research and hypothetical outcomes

`macro-dataset` evaluates all captured EIA revisions through an explicit cutoff,
not just the latest observation. It writes a **new directory outside its input
roots**, containing `rows.jsonl`, `observations.jsonl` and hash-bound
`dataset.json` metadata. CFTC-only input produces zero release rows honestly;
it cannot substitute for missing inventory releases.

```bash
PYTHONPATH=src .venv/bin/python -m oilbot macro-dataset \
  --journal data/macro/20260929-official-v1/macro.sqlite3 \
  --through 2026-09-30T00:00:00Z \
  --rules configs/inventory-research.json \
  --out data/research/macro-20260930
# Add --manifest VERIFIED-SNAPSHOT/manifest.json for market features and labels.
# Add --costs COST-ASSUMPTIONS.json to override OutcomePolicy defaults.
```

The decision clock is explicitly **counterfactual research time**: the later of
receipt plus the policy confirmation delay and actual parse availability. It is
not evidence that a forward strategy actually ran then. COT and price features
use only information available at that decision. A correction received and
parsed during confirmation invalidates the earlier revision. Corrections,
imports and initial snapshots stay visible but cannot enter comparisons; all
revisions of a reporting period share `release_group`.

With a market snapshot, each row includes hypothetical long/short outcomes at
1, 2, 5, 10, 30, 60, 120, 300, 900, 1800, 3600 and 14400 seconds. Windows that have
not elapsed **including execution delay** remain `PENDING_HORIZON`, even if a
still-fresh quote could otherwise appear to complete them. Missing prices,
market gaps, incomplete exits and partial fills remain explicit.

Comparison labels use **MCL bid/ask and multiplier**, never CL P&L. The default
illustrative assumptions are one contract, one-second execution delay, one
adverse tick per side and $1 fee per contract per side. These are configurable
assumptions, **not verified IBKR costs or an execution recommendation**.

All four baselines use the same source/market eligibility population. A failed
inventory threshold or price confirmation does not discard that release from
the inventory-only/price-only comparison. Each horizon additionally requires
complete MCL fills and exits in both directions. Until then every baseline label,
including no-trade, stays null rather than silently scoring missing data as zero.
Raw CL and MCL diagnostic outcomes remain inspectable separately.

This is a research dataset, not a fitted strategy or a promotion gate. It does
not aggregate returns, select the best horizon, perform train/test splitting or
assert statistical significance. Chronological release-group splitting with
overlapping-window purging and a frozen forward holdout remain required.

## Strategy research priorities and acceptance gates

1. Keep the existing physical-disruption/restoration family separate from this
   scheduled-inventory family. Require validated episode transitions and source
   rights for the former; numerical releases do not repair its label shortages.
2. Accumulate prospective receipt-timed inventory vintages and qualified CL/MCL
   quotes. Expand EIA with Cushing stocks and refinery utilization next, preserving
   units and publication cadence. Do not substitute spot/daily prices for MCL BBO.
3. Add NOAA/NHC official storm advisories and asset-location mapping for a separate
   weather-exposure hypothesis, then qualified OPEC/operator/port releases. An
   advisory or production forecast alone does not prove disrupted oil flows.
   Paid news/consensus feeds need access and budget decisions, not unauthorized
   scraping. These additional connectors are backlog, not implemented sources.
4. Freeze policy before a forward evaluation window. Compare all baseline rules
   on the same eligible releases and market windows, including rejected/missing
   data in coverage counts. Keep revisions and repeated receipts in one release
   group; do not count them as independent trades or random-split them.
5. Evaluate unseen chronological release groups with no overlapping outcome
   windows across splits. Include actual MCL bid/ask, fees, latency, slippage,
   displayed size and incomplete exits. Test cost sensitivity and uncertainty;
   a tiny sample or a positive gross midpoint return does not establish an edge.
6. Only after an independent frozen-policy paper campaign should a separately
   authorized executor consume qualified signals. This work does not relax OMS,
   reconciliation, loss limits or real-money approval gates.

## Initial evidence

The first persisted prospective CFTC poll succeeded and saved two baseline
observations in `data/macro/20260929-official-v1/macro.sqlite3`. No EIA recurring
polling or source service was installed. EIA's parser is fixture-tested and the
endpoint was inspected once; ongoing fetching awaits owner contact. No trading
performance has been evaluated, and the new strategy is research-only.

On 30 September, the batch export at cutoff `2026-09-30T03:07:18Z` saved
`data/research/macro-20260930/dataset.json` and its input/row files. It retained the
two CFTC observations and correctly reported **zero EIA release rows**, no market
inputs and no comparison-eligible releases. It is a smoke test, not economic
evidence. The focused source/strategy/dataset suite passed 60 tests, including
correction timing, recovered-parse availability, cutoff masking, MCL cost math,
partial/zero-fill exclusions and output protection.
The full repository suite passed **599 tests in 62.77 seconds**. Both module and
installed CLI entrypoints expose `macro-dataset`; compilation and
`git diff --check` also passed. These checks establish engineering behavior only.

After the owner supplied contact information on 30 September, one bounded EIA
poll succeeded: capture `e1b98bae-54dd-4391-932d-efff7a1b920a`. Its raw response
and normalized revision are saved in the existing macro journal. This first EIA
observation is a baseline, not a release-time trading signal. No recurring
collection service or broker execution was enabled.

### Scheduler deployment, 30 September 2026

The separately authorized scheduler milestone passed **632 repository tests in
108.14 seconds**, including 58 focused collector/scheduler tests. Compilation,
dependency consistency (`pip check`), whitespace checks and systemd unit
validation passed.

Installed frozen deployment: `data/deployments/macro-20260930-v1`.
Sidecar state: `data/macro-scheduler/20260930-v1/scheduler.sqlite3`.
Enabled and started only `oilbot-macro.service` and
`oilbot-macro-health.timer`. The independent health service completed with exit 0
at `2026-09-30T04:04:09Z`; collector state was **HEALTHY**, with no pending parses
or source issues. The next expected EIA release was 30 September 14:30 UTC, with
the polling window starting at 14:20 UTC (22:20 Ulaanbaatar). This is a planned
window, not a guarantee of publication or proof of uninterrupted future capture.

Existing news/admission services were not restarted. No broker connection,
strategy execution, model invocation, data purchase or email alert channel was
enabled. Keep WSL running for collection; frozen calendar validity ends at the
start of 1 January 2027 in New York and must be reviewed before renewal.
