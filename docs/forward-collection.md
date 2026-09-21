# Forward collection: news first, IBKR next

Historical-event reconstruction is deferred. Do not trade while building this
dataset. Existing research/demo commands remain in isolated storage; they are
not the active forward workflow.

## Run

```bash
.venv/bin/python -m oilbot preflight --config configs/forward.yaml
.venv/bin/python scripts/supervisor.py --config configs/forward.yaml --duration 45
.venv/bin/python -m oilbot collection-health --config configs/forward.yaml
.venv/bin/python -m oilbot status --config configs/forward.yaml
```

Remove the duration for continuous foreground operation; Ctrl-C stops children.
No daemon, scheduled task, email subscription, purchase, model call or broker
connection is installed or initiated. Sleep/restart gaps are recorded. Polling
is at least 60 seconds, so this is not a sub-second news service.

Storage is `data/forward`, separate from old experiments. The supervisor creates
the persistent experiment start boundary before launching collectors. If running
workers separately, initialize `record --component forward --once --config
configs/forward.yaml` before starting news. The forward worker checks for new
revisions every second and commits at most 200 per batch, independently of news
network requests. Pending batches catch up on subsequent iterations.

## Current source slice

- Al Jazeera and Iran International: publisher RSS content only.
- gCaptain and Aramco: publisher RSS content only.
- CENTCOM: public-release listing titles/links only, not full articles.
- OFAC: recent-actions listing titles/links only, not full notices or attachments.

These six sources are enabled for personal local capture, not certified as
complete or low-latency feeds. Public retrieval does not establish model,
redistribution or commercial-use rights. All model processing stays pending and
the forward pipeline rejects the model-analysis worker. Retention remains manual
review with no redistribution functionality. Sources fail independently; health
and backoff are visible and no access challenge is bypassed.

On 2026-09-21 the runtime returned HTTP 200 and parsed 25 items from
[Al Jazeera RSS](https://www.aljazeera.com/xml/rss/all.xml) and 50 from
[Iran International RSS](https://www.iranintl.com/en/feed). The
[CENTCOM public-release index](https://www.centcom.mil/MEDIA/PUBLIC-RELEASES/)
and [OFAC recent-actions index](https://ofac.treasury.gov/recent-actions) were also
reachable. These are one-time checks, not availability guarantees. Treasury
[retired OFAC RSS on January 31, 2025](https://content.govdelivery.com/accounts/USTREAS/bulletins/3c36623);
the collector uses the public listing instead.

Still deferred: Al Arabiya, IRNA, Reuters, Defense, OPEC, UKMTO live delivery,
ADNOC/Fujairah forward-detail handling, and additional state agencies. The legacy
observation configuration is unchanged. UKMTO's delayed mirror archive is not a
substitute for forward alerts. Do not activate all catalog entries or make
Reuters availability a startup requirement.

## Receipt and event semantics

Raw response bytes commit before parsing. Each story retains source identity,
native ID, URL, title/text, author/original URL when supplied, publisher timestamp,
request-start timestamp, first decoded-body-byte timestamp, complete local receipt,
content hash, revision number, and supersession links. Missing values stay null;
the collector does not fabricate timing or attribution.

`first_byte_at` is an application observation of the first decoded body byte,
not a kernel/network timestamp. Monotonic/host/boot metadata remains in the raw
observation. `local_received_at` is when the whole response was received.
`fast_event.available_at` is later, when classification actually happened.
Never backdate strategy knowledge to publisher time or pretend classification
was instantaneous. Receipt-anchored research and executable timing are separate.

The worker excludes pre-start observations, first source snapshots, old
publications first seen after startup, synthetic/local imports, and inputs
without recorded live HTTP receipt. Every exclusion is recorded. A publisher
without timestamps establishes local first-seen time, not event freshness;
all candidates require review. Updates carry supersession notices, including
corrections/withdrawals; feed disappearance never means withdrawal. Earlier
evidence is not silently rewritten.

The English headline-only classifier covers the initial 21 categories. It returns
literal match spans, attribution candidates, and qualifiers. Bodies remain
archived but are not classified. Coverage is intentionally incomplete; translation
and semantic extraction are not implemented. Mentioning an actor does not make
it the claim origin. `IRGC says ...` can become `OFFICIAL_CLAIM`, which still has
`confirmation: UNVERIFIED`. Negation, uncertainty, obvious historical context,
or ambiguous attribution yields `REVIEW_REQUIRED`.

Candidate clustering uses explicit UKMTO report IDs, supplied original URLs, or
identical normalized headlines within six hours of first receipt. It never groups
solely by geography/type/time. This catches exact syndication, not every paraphrase;
ambiguous stories remain separate candidates. `novelty` means new candidate
cluster, not a proven physical incident or trading signal. Multiple publishers
do not imply independent corroboration. The worker does not automatically award
maritime-authority, operator, or physical confirmation. Existing reviewed claim
tools are the route to stronger assertions.

`forward.sqlite3` contains start records, processing/exclusion receipts,
`fast_event`, `forward_incident_revision`, and supersession notices. Outputs and
cursors commit atomically; restarts do not duplicate events. Rule/asset policy
changes require a new experiment root. Snapshots include the forward journal:

```bash
.venv/bin/python -m oilbot snapshot --config configs/forward.yaml --out data/forward-snapshot-001
```

This produces a checksummed news-only archive, not a completed market dataset.
Do not copy a live SQLite main file without its WAL; use the snapshot command.

## Remaining milestones

1. News foundation implemented: isolated capture, deterministic event candidates,
   conservative clustering, receipt/supersession provenance and snapshots.
2. IBKR live recorder: access is planned, not connected. Establish connectivity,
   subscription and explicit CL front/second and matching MCL contracts. Preserve
   supplied timestamps, sizes and sequences; unavailable fields stay missing.
   Distinguish realtime from delayed data. No order path belongs in this step.
3. Qualify simultaneous capture: freshness, clocks, disconnect/reconnect gaps,
   contracts/rolls, source health and durable writes.
4. Automatic event/transition snapshots and delayed outcomes: receipt and
   classifier-availability anchors; pre-event 60/30/10/5 seconds, receipt, then
   1/2/5/10/30 seconds and 1/2/5/15/30 minutes, 1/4 hours. Missing/stale intervals
   stay unavailable, not zero-return or interpolated evidence. Persist pending
   horizons across restarts. This worker is not implemented yet.
5. Run for weeks/months, review false positives and ambiguous clusters, analyze
   accumulated episodes, freeze a strategy, then separately authorize forward
   paper trading. No profitability inference from fixtures or regex hits.

Until steps 2–4 pass, market reports `WAITING_FOR_QUALIFIED_LIVE_FEED`, automatic
market outcomes are unavailable, and broker execution remains disabled.
`planned_provider: ibkr` is intent only; `provider: fixture` is the existing schema
placeholder. Forward mode rejects `--fixture` to prevent contamination.
