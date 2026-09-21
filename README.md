# oilbot

Standalone oil observation and research application. The runtime captures public source revisions, extracts evidence, tracks incidents, archives market fixtures, and exports immutable replay reports without broker execution or prediction-market dependencies.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Quick start

```bash
oilbot preflight
oilbot record --component news --once
oilbot record --component market --once --fixture tests/fixtures/market.json
oilbot record --component analysis --once
oilbot status
```

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
The 39-source profile catalog is not an activated live news network. Real licensed
market files and reviewed event episodes are still needed for an event study.
Draft settings are in `configs/research.json`; no strategy edge or live execution
has been qualified.

## Repository layout

- `src/oilbot/` — standalone oil runtime package
- `configs/observe.yaml` — observation and source configuration
- `tests/test_oil.py` — focused oil verification suite
- `scripts/supervisor.py` — local supervisor for news/market/analysis workers
- `deploy/oilbot@.service` — systemd user unit for a single host
- `docs/observation.md` — operational notes and guardrails

This project intentionally keeps the runtime focused on public oil intelligence and offline research, with no broker or market-data dependencies in the active package graph.
