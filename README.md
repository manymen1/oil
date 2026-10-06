# oilbot

Standalone oil observation and research application. Current priority: synchronized
CL1/CL2/MCL1 recording and a frozen EIA shadow pilot, alongside **forward news
recording**, local event candidates, and auditable incident lineage.
Development also includes local paper execution and an opt-in read-only IBKR
connection and quote-capture path. Broker order execution and strategy activation
remain disabled; live market data is not yet qualified.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Quick start

```bash
oilbot preflight --config configs/forward.yaml
python scripts/supervisor.py --config configs/forward.yaml --duration 45
oilbot status --config configs/forward.yaml
oilbot collection-health --config configs/forward.yaml
oilbot forward-candidates --config configs/forward.yaml --limit 20
oilbot forward-quality --config configs/forward.yaml
```

The bounded smoke run stops itself. Omit `--duration 45` for a foreground
continuous run; Ctrl-C stops all workers. No service is installed automatically.
The forward supervisor runs `news`, `forward`, and the independent `linker`. It never calls a model,
starts a market worker, or loads market fixtures. Indexed recovery avoids repeated
whole-journal scans. Cross-headline candidate links require review and never
merge incidents or invent confirmation.
Corrections create evidence-state history. Human link decisions are append-only;
`forward-episodes` derives an as-of mapping without rewriting captured events.
`propose-forward-link` lets a reviewer nominate relations the linker missed;
approval is a separate step. Wire citations are distinct from syndication credits.
Scoped [first-party attribution](docs/first-party-attribution.md) can identify a
reviewed publisher as claimant without treating its claim as confirmed.
`forward-quality` reports dataset counts/rates, lag samples, exclusions, source
health, recovery work, clock diagnostics and storage growth without fetching news.
Access denials and repeated primary-feed parse failures open persistent circuits
for operator review; forced polls and restarts do not bypass them.
See [the forward recorder and roadmap](docs/forward-collection.md).
Use the [offline collector audit pack](docs/collector-audit.md) to preserve a
snapshot, verify same-host replay and prepare a stratified coverage-review sample.
The [offline story-review CLI](docs/story-review.md) supports revision-bound labels,
missed-event annotations and separately dated coverage diagnostics without rewriting capture history.
The [forward economic research protocol](docs/forward-economic-research.md) defines
reviewed disruption/restoration transitions and the gates before market or trading work.
Use the [economic transition review workflow](docs/economic-transition-review.md)
to save dated operational assessments and inspect bridge exclusions from a verified snapshot.
The [operational collection workflow](docs/operational-forward.md) adds an opt-in
captured-text assessment queue and isolated, checksummed deployment runs.
The [implementation and recovery notes](docs/implementation-2026-09-29.md) describe
persistent user services, independent health checks, and the queue-to-research
dataset handoff. This is research infrastructure, not an enabled trading bot.
The next [admission and receipt-response milestone](docs/assessment-admission.md)
adds a persistent fail-closed queue consumer and pre-receipt/processing-latency
features without enabling model interpretation or trading.
The [local paper execution core](docs/paper-execution.md) adds restartable synthetic
scenarios, explicit paper intents, order/position ledgers, exits and risk checks.
It does not convert research candidates into orders or connect to a broker.
The [IBKR/MCL paper integration](docs/ibkr-paper.md) adds offline preflight and
explicit read-only contract/account discovery and bounded BidAsk capture. It
does not send broker orders or require broker dependencies for observation work.
The [official macro sources and inventory strategy](docs/macro-sources-and-strategy.md)
add isolated EIA/CFTC numeric observations, immutable release vintages, and an
inventory-change/price-confirmation research policy with comparison baselines.
`macro-dataset` exports grouped release revisions and cost-aware hypothetical
MCL labels, with causal features and explicit incomplete return windows.
`macro-worker` adds dated release-aware polling and `macro-health` independently
checks stale collection, overdue releases and worker heartbeats. A separate
installer freezes its deployment; individual CLI commands never install services.
No strategy output authorizes trading.
The opt-in [inventory continuation logic](docs/inventory-continuation-logic.md)
adds sustained CL/MCL confirmation, calendar-spread agreement and a cost screen,
while retaining the original strategy as a baseline. `macro-evaluate` adds a
release-grouped chronological holdout comparison; it never promotes a strategy
or treats hypothetical results as proof of a profitable trading edge.
The [geopolitical news research workflow](docs/geopolitical-news-research.md)
separates risk-premium claims from reviewed physical transitions, reuses the
cost-aware continuation filter and compares chronological, overlap-grouped news
baselines. It runs offline and never turns a headline into confirmed supply loss
or authorizes an order.
The [oil-price drivers and source coverage](docs/oil-price-drivers-and-sources.md)
maps the research priorities and adds an isolated NOAA/NHC weather adapter and
as-of regional screening report. A storm watch is never a confirmed oil outage
or a buy/sell signal; existing frozen collectors are unchanged.
EIA table 9 and explicit-report BSEE adapters now preserve Cushing/refinery
observations and estimated offshore shut-ins separately. See the
[5 October collection hardening runbook](docs/collection-hardening-2026-10-05.md)
for hourly EIA-detail collection, independent release/freshness checks and CI.
`status` opens existing journals read-only and lists missing journals without
creating them. Use the deployed run's configuration when inspecting a service;
a development configuration can point at a different data root.
The [source development notes](docs/source-development-2026-09-28.md) document
opt-in official-release detail capture and unresolved source-access qualification.

Development lives on `main`. Separate experiments with opt-in configuration,
versioned policies, distinct data roots and immutable snapshots, not long-lived
feature branches. Never update a running experiment's checkout: stop and record
the old run first, then explicitly launch a new run under the new policy/commit.

## Source qualification and collection health

```bash
oilbot qualify-sources
oilbot collection-health
```

These read-only reports do not fetch or activate feeds. The proposed eight-source
shortlist is separate from enabled observation sources. See the
[qualification guide](docs/source-qualification.md) for evidence requirements,
permission reviews, and stale/backoff/parser reporting.

## Offline demo

```bash
oilbot demo --out data/oil-demo
oilbot replay --manifest data/oil-demo/snapshot/manifest.json
```

The demo writes a source/market manifest and JSON/HTML report without calling any remote model or broker service.

## Transition research and news provenance

The research pipeline now supports explicit quote/trade/status records, optional
offline Databento DBN/CSV import, causal CL1/CL2/MCL1 features, dated economic
episodes, forward executable outcomes, and residual-response gates. The news
model separates publishers from claim origins and tracks corroboration,
contradictions, corrections and deletions.

See [the research workflow](docs/research.md) and [the news-source and claim guide](docs/news-mesh.md).
The 39-source profile catalog is not an activated live news network. Historical
event reconstruction is deferred; the existing offline tools remain available
for diagnostics. Actual local news receipts and qualified live market data are
needed for forward outcome research.
Draft settings are in `configs/research.json`; no strategy edge or live execution
has been qualified.

## Repository layout

- `src/oilbot/` — standalone oil runtime package
- `configs/observe.yaml` — observation and source configuration
- `configs/forward.yaml` — isolated personal forward recorder configuration
- `tests/test_oil.py` — focused oil verification suite
- `scripts/supervisor.py` — local supervisor; forward workers by default
- `deploy/oilbot@.service` — systemd user unit for a single host
- `docs/observation.md` — operational notes and guardrails

The base runtime keeps broker SDKs optional. Install the `ibkr` extra only for the
separate read-only IBKR workflow; the news supervisor never starts that adapter.
## Roadmap implementation

The read-only CL1/CL2/MCL1 recorder, frozen forward EIA pilot, actual decision
journal, paired economic diagnostics and verified recovery workflow are described
in [the 6 October implementation runbook](docs/roadmap-implementation-2026-10-06.md).
Market access and qualification remain gated; broker orders are disabled.
