# Forward geopolitical-news recorder

Historical-event reconstruction is deferred. Do not trade while building this
dataset. Existing research/demo commands remain in isolated storage; they are
not the active forward workflow. GitHub CI and live market data are outside this
milestone. IBKR remains a later option, not a prerequisite for news recording.

## Run

```bash
.venv/bin/python -m oilbot preflight --config configs/forward.yaml
.venv/bin/python scripts/supervisor.py --config configs/forward.yaml --duration 45
.venv/bin/python -m oilbot collection-health --config configs/forward.yaml
.venv/bin/python -m oilbot status --config configs/forward.yaml
.venv/bin/python -m oilbot forward-candidates --config configs/forward.yaml --limit 20
```

Remove the duration for continuous foreground operation; Ctrl-C stops children.
No daemon, scheduled task, email subscription, purchase, model call or broker
connection is installed or initiated. Sleep/restart gaps are recorded. Polling
is at least 60 seconds, so this is not a sub-second news service.
The supervisor runs only `news` and `forward`; there is no idle market worker.

Storage is `data/forward`, separate from old experiments. The supervisor creates
the persistent experiment start boundary before launching collectors. If running
workers separately, initialize `record --component forward --once --config
configs/forward.yaml` before starting news. The forward worker checks for new
revisions every second and commits at most 200 per batch, independently of news
network requests. Pending batches catch up on subsequent iterations.

## Incremental recovery

New raw observations and their `parse_work` entries commit together. Story
revisions, parse receipts and the transition to `parsed` also commit together.
Recovery seeks pending work through a partial SQLite index; it no longer loads
the whole journal or rebuilds a global set of completed observation IDs.
The first-story/source-baseline check also has a dedicated index.

Existing journals are indexed incrementally, at most 500 record metadata rows
per poll. A fixed migration fence prevents an older observation being replayed
before its later parse receipt has been indexed. Live observations after that
fence remain recoverable while migration continues. Once migration finishes,
only the saved cursor and pending-work index are consulted.

Recovery attempts at most eight jobs per pass and stops starting new jobs after
a one-second budget. An already-started parser may finish later (PDF extraction
has its existing ten-second timeout). This is a bound on work started, not a
hard one-second wall-time guarantee. Recovery uses stored bytes, not new network
requests. Failed parses are retained in `failed` state with an audit record;
they are not retried on every poll. They need operator review before a future
targeted retry feature. Unknown/disabled sources remain pending, and changed
source registrations cannot silently reinterpret old payloads.

`oilbot status --config configs/forward.yaml` shows indexed work counts and
migration progress. Counts describe indexed work only until migration completes.

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

Next source order: UKMTO, IRNA, OPEC, Defense releases, Al Arabiya, ADNOC,
Fujairah, then Saudi/Gulf official agencies. Reuters remains optional. These
additions are not activated by this milestone. The legacy
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

Additional deterministic slots retain literal actor mentions, registered
facility/location aliases, vessel names explicitly quoted or uppercase after
"tanker/vessel/ship", and literal IMO identifiers. A timestamp is an event-time
candidate only when explicitly introduced by "at" or "event time:" with a full
timezone-qualified ISO timestamp. No date, timezone, name, actor relationship,
or equivalence between vessel names and IMO identifiers is invented. Multiple
ambiguous values remain in plural evidence lists; singular slots stay null.
`literal_evidence` carries the exact headline span behind each extracted value.

Candidate clustering uses explicit UKMTO report IDs, supplied original URLs, or
identical normalized headlines within six hours of first receipt. It never groups
solely by geography/type/time. This catches exact syndication, not every paraphrase;
ambiguous stories remain separate candidates. `novelty` means new candidate
cluster, not a proven physical incident or trading signal. Multiple publishers
do not imply independent corroboration. The worker does not automatically award
maritime-authority, operator, or physical confirmation. Existing reviewed claim
tools are the route to stronger assertions.

Cross-headline **candidate links** are separate from clustering. The indexed
search compares matching event types within six hours of local receipt, using
claim origin, named vessels/facilities, location and actor mentions. Explicit
conflicting vessel names/IMO IDs, facilities or locations reject a link, as do
explicit event times more than an hour apart. Named-entity matches need shared
context; context-only links need a shared location plus an actor or origin.
Neither is independent corroboration. The time windows are review heuristics,
not measured event-equivalence thresholds.

At most 500 nearby events are examined and ten links emitted per new event;
truncation flags expose either limit. Every link records reasons, receipt-time
distance, source-event IDs and `merge_authorized: false`. Incident IDs, novelty
and confirmation are unchanged. A candidate can be wrong; human review remains
necessary. `forward-candidates` reads these records without applying them. Use
the returned `next_after_seq` with `--after-seq` for the next page.

`forward.sqlite3` contains start records, processing/exclusion receipts,
`fast_event`, `forward_incident_revision`, and supersession notices. Outputs and
cursors commit atomically; restarts do not duplicate events. Rule/asset policy
changes require a new experiment root, except the explicitly supported v1-to-v2
upgrade. That upgrade preserves the experiment start and consumed-news cursor,
records a policy revision, and does not reclassify or retime old events. The
candidate index starts with newly classified v2 events; it does not reconstruct
historical v1 associations. Snapshots include the forward journal and links:

```bash
.venv/bin/python -m oilbot snapshot --config configs/forward.yaml --out data/forward-snapshot-001
```

This produces a checksummed news-only archive, not a completed market dataset.
Do not copy a live SQLite main file without its WAL; use the snapshot command.

## Remaining milestones

1. Completed foundation: incremental recovery, literal event slots, conservative
   cross-headline candidate links, restart-safe journals and snapshot provenance.
2. First-party identity: bind verified source identity to its authority/asset
   scope so an operator's own operational report need not say "operator says".
   Publication on an official domain alone must not prove a contested claim.
3. Stronger revisions: explicit evidence-state transitions for corrections,
   denials, deletions and withdrawals, without withdrawing unrelated evidence.
4. Expand sources in the order above, testing endpoint/parser behavior and
   preserving source failure isolation. Keep model calls out of the hot path.
5. A combined dataset-quality command: stories and new stories/day, events/day
   and by type, healthy/stale sources, publication-to-receipt and
   receipt-to-classification lag, duplicate/link rates, revisions, exclusions
   and runtime gaps. The current commands do not yet provide this full report.
6. Validate continuous operation, restarts, disk growth and recoverable backups
   on an always-on host. WSL sleep is a recorded gap, not 24/7 availability.
   No persistent service has been enabled automatically.

The objective is prospective geopolitical intelligence with auditable receipts,
claimants, classifications, incident lineage and corrections. Live market data,
retrospective market attachment, strategy optimization and trading are later
work. Forward mode uses `market.provider: disabled` and rejects `--fixture`.
