# Oil-moving news and data: coverage and next implementation priorities

Updated 5 October 2026; dated canary evidence below remains a historical snapshot.
This is a research-source development plan and an
implementation record, not a price forecast, investment recommendation or proof
that a strategy has an edge. The aim is to connect a dated claim to a plausible
oil-market mechanism, then test its incremental information against market data.

## What the system should distinguish

| Driver family | Primary evidence to collect | Current implementation / remaining work |
| --- | --- | --- |
| Physical supply and transport | Named operator/port reports, affected facility or route, prior/new operating state, capacity and duration where actually reported | Existing operator/news observation and operational-review workflow; source rights, detail completeness and semantic validation remain gates |
| Inventories and refinery demand | EIA commercial crude, SPR, products; Cushing stocks, refinery inputs/utilization, imports and exports with units and vintages | Stock collector scheduled; separate table 9 adapter implements Cushing stocks and US/PADD 3 refinery inputs, capacity and utilization. Imports/exports remain backlog |
| Production policy and realized output | Official OPEC decisions, effective dates, target/baseline changes; later reported output to evaluate implementation | OPEC is a discovery-catalog entry, not a new qualified collector; keep announced quotas separate from realized barrels |
| Sanctions and geopolitical restrictions | OFAC actions, affected entities/vessels, exemptions, effective dates, enforcement and operator consequences | OFAC listing/detail adapter exists, but listing access is not complete legal or operational evidence; no new detail activation here |
| Weather and infrastructure | NOAA/NHC storm updates, then forecast footprints and verified facilities; BSEE/operator reports for actual shutdowns and restarts | NHC current-status and explicit-URL BSEE shut-in report parsers implemented. Current BSEE report discovery, continuous weather collection and asset/forecast overlays remain backlog |
| Demand and macro conditions | Dated official activity/fuel-demand indicators, growth forecasts and revisions; separate realized observations from forecasts | New demand/macro series and their release-vintage handling remain backlog |
| Positioning and market confirmation | CFTC positioning as slow context; actual instrument-specific prices, curve structure, spreads and liquidity | CFTC collector exists; qualified CL/MCL quote coverage remains a separate unresolved gate |

Primary references: [EIA petroleum data](https://www.eia.gov/petroleum/data.php/summary)
lists inventories, refinery inputs/utilization and supply/demand measures;
[OPEC's production reporting](https://publications.opec.org/momr/chapter/140/2607)
separates direct communication and secondary-source figures;
[OFAC recent actions](https://ofac.treasury.gov/recent-actions) supplies official
action notices. A [BSEE hurricane operations report](https://www.bsee.gov/newsroom/latest-news/statements-and-releases/press-releases/bsee-monitors-gulf-of-mexico-oil-and-14)
illustrates reported evacuation and estimated shut-in volumes. That historical
example is a schema reference, **not evidence of a current outage**.

Headlines are discovery inputs, not independent confirmations. Preserve original
claimants and syndication relationships; ten copies of one announcement are not
ten independent sources. Proposed reopenings, diplomatic statements, sanctions
announcements and attacks do not become confirmed crude-flow changes without
explicit evidence. Existing UKMTO/CENTCOM access restrictions are not bypassed.

## New implementation: NHC status observations

The official [NHC product-examples page](https://www.nhc.noaa.gov/productexamples/)
links [CurrentStorms.json](https://www.nhc.noaa.gov/CurrentStorms.json). This adapter
reads the current storm IDs, names, classification codes, coordinates, nominal
advisory times and public-advisory references. It does not fetch the linked
advisories, maps, ZIP files, KML or forecast tracks. The public advisory URL is
strictly limited to the registered NHC text path.

Files:

- `src/oilbot/weather.py`: strict parser, generic-news adapter and as-of report.
- `configs/weather-forward.yaml`: separate optional observation root, five-minute
  minimum polling, disabled market provider and prohibited model processing.
- `configs/oil-weather-regions.json`: explicit draft regional screening policy.
- `tests/test_weather.py`: synthetic fixtures and regression checks.

The existing news collector provides raw-before-parse persistence, local receipt
and parse availability, HTTP validators, failure/backoff handling and access-denial
circuits. Same-storm/same-issuance corrections supersede the previous story;
repeated equivalent content does not manufacture a new story. File-update metadata
and unrelated GIS-link changes are not semantic story revisions. Each storm ID
is a research grouping key; advisory updates are not independent market episodes.

Nominal advisory times are retained, but **not promoted to verified publication
timestamps**. `published_at` stays null. An advisory stamped later than the local
capture is flagged for review, not silently backdated. Failed or unparsed newer
captures degrade the report instead of presenting an older valid snapshot as a
healthy current feed. A 304 refreshes polling evidence, not the age of an advisory.

The original numeric intensity and pressure strings are retained with **units
explicitly unverified**. The linked technical-reference PDF returned HTTP 403 in
this environment and was not bypassed; unit conversion, intensity-based scoring
and classification derivation are deliberately absent until the field contract
is independently verified. The geographic screen does not use these numbers.

### What a regional weather watch means

The initial northern-Gulf box (24–31 N, 98–80 W) is an analyst-defined broad
screen, **not a verified facility inventory or official warning boundary**.
A recent storm center within it can produce `REGIONAL_WEATHER_WATCH`.
Stale/problematic input produces `REVIEW_DATA_QUALITY`. A center outside it
produces `OUTSIDE_SCREENED_REGIONS`, which does not establish absence of exposure.

All outputs retain:

```text
confirmed_disruption = false
affected_oil_bpd = null
price_direction = null
trade_authorized = false
```

The weather report does not distinguish offshore supply loss from refinery
crude-demand loss or product-supply disruption; it requires the relevant
operating evidence before making those claims. The [NHC cone definition](https://www.nhc.noaa.gov/aboutcone.shtml)
describes uncertainty in the cyclone center's track, not a complete impact
footprint. Merely putting a facility inside a cone would not establish an outage.
A storm disappearing from the rotating feed likewise does not prove dissipation,
cancellation, resumed operations or an all-clear.

### Commands and isolation

```bash
# One bounded public JSON poll into the separate weather root.
PYTHONPATH=src .venv/bin/python -m oilbot record \
  --config configs/weather-forward.yaml --component news --once

# Explicit as-of report; use a time at/after the desired capture and parsing.
PYTHONPATH=src .venv/bin/python -m oilbot weather-report \
  --config configs/weather-forward.yaml \
  --regions configs/oil-weather-regions.json \
  --at 2026-10-02T12:25:38Z \
  --out data/weather/20261002-nhc-v1/research-initial.json
```

The output file must be new. Reporting is read-only and never creates a missing
capture journal. Both rules and input raw hashes are retained in the report.
No weather service was installed or started in this pass, and no existing frozen
news or macro deployment was changed. A continuous canary requires a separate
frozen deployment and its own reviewed monitoring/retention setup.

The [NWS use policy](https://www.weather.gov/disclaimer) permits lawful use of
its public-domain information, with attribution/endorsement/modification caveats
and exceptions for annotated third-party material. This adapter processes only
the numeric JSON status feed. It does not import map imagery, claim NOAA
endorsement, redistribute raw data or activate external model processing.

## Evidence and next priorities

The bounded 2 October canary returned one successful response, two initial story
revisions and zero collector errors. These are baseline weather observations,
not oil-price signals. The report saved at
`data/weather/20261002-nhc-v1/research-initial.json` binds capture
`5ff625ef-d7d8-41ae-a7c8-aa389acd2a25` and raw SHA-256
`2c7f2e83c01615a60226e81a230ebdd4d8bc7a869ebef5a709e2a3cd020e8373`.
Both centers were outside the draft northern-Gulf box, which is not an all-clear
or a statement about their wider impacts.

Verification: **665 repository tests passed in 73.58 seconds**, including 33 new
weather tests. The focused weather suite was rerun after report-metadata additions.
`git diff --check`, CLI help and the existing frozen macro deployment's hash check
also passed. Synthetic test cases establish engineering behavior, not economic
predictive value.

The existing macro worker's independent check at
`2026-10-02T12:25:38Z` was HEALTHY: latest EIA reporting period 25 September and
CFTC reporting period 22 September, with no pending parses. This snapshot does
not prove uninterrupted prior coverage or profitable information content.

Recommended development order:

1. **Confirm physical effects:** add current-report discovery and operator restart reports to the explicit-URL BSEE parser,
   preserving estimated versus measured quantities and report timestamps. Couple
   them to weather episodes without treating weather itself as an outage.
2. **Deepen inventory context:** operate the implemented Cushing/refinery collector
   with explicit units, period alignment and revisions; qualify release-time
   latency and later add imports/exports. Hourly context is not a fast-release feed.
3. **Improve existing news evidence:** qualify full-body official releases and
   operator/port notices; retain claim origin, effective dates, proposal versus
   implementation, contradictions and correction history.
4. **Add production-policy vintages:** OPEC decisions and later output estimates,
   after access/use qualification; keep targets, voluntary adjustments and actual
   reported production distinct.
5. **Evaluate information value:** join frozen event groups to qualified CL/MCL
   prices, compare against price-only/no-trade baselines out of sample, and include
   missing coverage and realistic execution costs. No fitting or promotion is
   performed by this source-development increment.

## Follow-up implementation

`src/oilbot/eia_detail.py` implements nine table 9 facts in an isolated journal:
Cushing crude stocks plus US and PADD 3 crude inputs, gross inputs, operable
capacity and utilization. Stocks, flow rates and percentage-point changes retain
different units. Week-over-week changes are not consensus surprises. Corrections
supersede prior revisions without rewriting the original receipt history.

`src/oilbot/bsee.py` parses one explicitly registered HTML report at a time,
preserving estimated oil/gas shut-in volumes and percentages. The example URL
is historical and cannot establish a current outage. Unknown publication dates,
archived pages and stale reports remain flagged. It neither discovers current
reports automatically nor infers restoration from report disappearance.

The [collection hardening runbook](collection-hardening-2026-10-05.md) describes
the separate EIA-detail timer, calendar-bounded monitoring and remaining gates.
No weather/BSEE service or broker-order execution is enabled by these adapters.
