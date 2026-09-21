# Transition research and claim provenance

The research pipeline answers what was known at a timestamp, what changed in an
episode, how much CL had moved, and what a delayed marketable entry would have
earned afterward. It does not authorize execution. The supplied thresholds and
fees are draft engineering assumptions; no historical edge has been established.

## Market data

`QuoteEvent`, `TradeEvent`, `InstrumentDefinition`, and `MarketStatusEvent` are
distinct records. Existing `MarketEvent` callers still mean quotes. Trades retain
price, size and aggressor (`buy`, `sell`, or `unknown`); quotes retain BBO sizes and
order counts. Nanosecond exchange-event, provider-receipt and local-receipt fields
are separate. Historical data uses `provider_receive_proxy`: local receipt is
unknown, not backdated to the exchange or vendor timestamp.

The optional Databento importer reads an already-downloaded **GLBX.MDP3 MBP-1 or
trades DBN/DBN.zst file**. It can also read Databento CSV exported with
`pretty_ts=False, pretty_px=False`. Numeric timestamps are epoch nanoseconds;
numeric prices use 1e-9 units. It never makes a paid data request.

```bash
python -m pip install -e '.[dev,databento]'
oilbot import-databento --file data/cl-mcl.mbp-1.dbn.zst \
  --registry data/contracts.json --schema mbp-1 --out data/market-study
```

MBP-1 includes trades, so importing a separate trades copy of the same period
would double-count prints. Use one MBP-1 file for the full dataset; the trades
import mode is for separate trade-only research. Symbol-filtered exchange channel
sequences are preserved, not treated as consecutive per-instrument sequences.
Provider bad-timestamp/book flags create explicit status records and block usage.

The contract registry must be a JSON object with `dataset: "GLBX.MDP3"`, a
nonempty `definition_source` describing the dated reference data, and a
`definitions` array. Each entry has all the fields of `InstrumentDefinition`
(see `tests/fixtures/market.json` for the field layout), with `data_mode:
"historical"` and the actual numeric Databento ID as `vendor_id`. Use the actual
delivery month, expiry timestamp and dated session calendar. Do not substitute
the expiry month for the delivery month or invent vendor IDs. Every imported
instrument must have a mapping known before its first record. Unknown mappings
or malformed data fail the import and leave an explicitly incomplete archive.

The importer records file/registry hashes; retain the original licensed files.
The optional dependency is isolated from the public-source collector.

Primary format references: [Databento MBP-1](https://databento.com/docs/schemas-and-data-formats/mbp-1),
[timestamps, flags and price conventions](https://databento.com/docs/standards-and-conventions),
and the [official Python client](https://github.com/databento/databento-python).

## Episodes and causal features

Explicit dated assignments join multiple incidents into one economic episode:

```bash
oilbot cluster --episode ras-tanura-episode-001 \
  --incident INCIDENT_ID_1 --incident INCIDENT_ID_2 \
  --reason 'Reviewed common operator incident reference'
```

Assignments affect subsequent transitions; they do not rewrite past knowledge.
Unassigned episodes are retained in datasets but cannot train historical support
or enter an evaluated partition. Initial operating state establishes a baseline;
unknown-to-disrupted observations cannot masquerade as operating-to-disrupted.
Repeat reports of the same state do not create new transitions. Authority,
contradictions, literal quantities and duration remain attached to the row.
Gross capacity is never converted into lost production. Indefinite durations
remain unknown. Different explicit state updates can supersede earlier claims;
a disagreement about the same proposition remains contested until reviewed.

At decision time the feature builder selects CL1, CL2 and the matching MCL1
delivery month using dated definitions and a declared days-to-expiry roll policy.
This is an expiry-based front contract, not a volume-based continuous series.
It uses only quotes whose availability is at or before the query. Price before
receipt is strictly before receipt. Returns at 1/5/30/60/300 seconds, CL1–CL2
spread, quote spread/depth, known-aggressor imbalance over 30 seconds, and realized
volatility over 300 one-second returns are recorded. Missing/stale anchors and
unknown trade direction remain missing. Roles stay fixed over an event window.

## Build a reproducible dataset

```bash
oilbot snapshot --out data/source-snapshot
oilbot dataset --manifest data/source-snapshot/manifest.json \
  --market data/market-study --policy configs/research.json --out data/event-dataset
```

The source snapshot is hash-verified before use. `events.jsonl` contains one row
per operational transition, with separate `features`, `decision`, and `outcomes`
objects. `claims.jsonl` contains claim-confirmation transitions linked to market
features and outcomes. Both are checksummed by `dataset.json`, which also binds
the source snapshot, normalized market data, rules, costs and support artifact.
The builder uses in-memory indexes: for large tick histories, import and build
bounded event windows rather than assuming an unlimited-memory full-history job.

Forward labels cover 1, 2, 5, 10, 30, 60, 120, 300, 900, 1800, 3600 and 14400
seconds. Each horizon includes descriptive CL midpoint returns and hypothetical
long/short execution for both CL1 and MCL1. Entry is the fresh BBO available at
decision plus configured delay; exit is at decision plus horizon plus delay.
Ask/bid execution already pays the spread. Additional slippage and explicit fees
are separate. Displayed-size partial fills and missing exits remain unresolved;
they are not labeled as zero P&L. Session/expiry boundaries and recorded gaps
invalidate labels. These fills are assumptions, not proof an order would fill.

## Strategy and evaluation

Only physical-disruption and restoration transitions are candidates. The gates
test novelty, source evidence, crude relevance, meaningful state change, episode
adjudication, fresh synchronized market state, spread, depth, volatility, already
observed price movement, and disjoint historical support after costs and buffer.
Known contradictory claims veto a candidate. The normal observation worker now
abstains when it has only an incident and no market/support context.

Historical support targets **signed return from decision to horizon**. It is
conditioned on transition, asset type, confirmation, literal duration/severity,
signed pre-decision movement bins (0.1 percentage points), volatility bins
(0.2 percentage points), and execution-contract spread. Because this target is
already residual continuation, the engine does not subtract the observed move a
second time. Episode averages receive equal weight before taking the median.
The shipped minimum of 20 episodes is a draft coverage gate, not statistical
proof. Sparse cohorts explicitly abstain.

```bash
oilbot event-study --dataset data/event-dataset \
  --validation-start 2027-01-01T00:00:00Z --test-start 2027-04-01T00:00:00Z \
  --out data/descriptive-study.json
oilbot fit-support --dataset data/event-dataset --horizon 300 \
  --validation-start 2027-01-01T00:00:00Z --test-start 2027-04-01T00:00:00Z \
  --available-at 2027-01-01T00:00:00Z --out data/support.json
```

Dates above illustrate syntax, not a chosen research protocol. `fit-support`
uses only the development partition. All labels must finish before support
availability. Whole episodes crossing a partition boundary plus a four-hour
purge (extended by execution delay) are removed. An incident reassigned between
episodes remains in one fold.
Support cannot include the evaluated episode/incident. A horizon must be chosen
explicitly before applying support; the dataset builder rejects ambiguous
horizon choices. Add `--support data/support.json` to a later dataset build.

Studies compare no trade, simple event direction, event-time price momentum, and
the gated strategy. They are descriptive, preserve missing labels, and do not
net overlapping positions into a portfolio. Parameter/horizon exploration still
requires multiple-testing controls and a prospective study. No real-data study,
rule freeze or forward market recorder is claimed by the fixture tests. The
existing `freeze-protocol` command is for a complete, explicitly reviewed future
protocol once the dataset and study are ready.
