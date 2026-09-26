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
.venv/bin/python -m oilbot forward-quality --config configs/forward.yaml
```

Remove the duration for continuous foreground operation; Ctrl-C stops children.
No daemon, scheduled task, email subscription, purchase, model call or broker
connection is installed or initiated. Sleep/restart gaps are recorded. Polling
is at least 60 seconds, so this is not a sub-second news service.
The supervisor runs `news`, `forward` and `linker`; there is no market/model worker.
Restart backoff resets after a child has run stably for ten minutes.

Storage is `data/forward`, separate from old experiments. The supervisor creates
the persistent experiment start boundary before launching collectors. If running
workers separately, initialize `record --component forward --once --config
configs/forward.yaml` before starting news. The forward worker checks for new
revisions every second and commits at most 200 per batch, independently of news
network requests. Pending batches catch up on subsequent iterations.
The independent linker also checks every second, with its own bounded batches,
cursor and policy. Linker failures do not stop classification. Both write short
transactions to the same forward journal; SQLite still serializes writes.

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
Due network requests run before recovery, with up to eight independent hostname
workers (requests to each hostname remain serialized). Recovery can still delay
the *next* poll by an in-flight parser's timeout. If recovered content predates
the latest observation of that same native story, it is retained in a
`late_story_parse` audit record with `OLDER_OBSERVATION`, not allowed to replace
the current story or create a fresh forward event. Unchanged receipts advance
this watermark too. Raw bytes and the late parsed content remain inspectable.
An unseen native story recovered from before the source's first parsed snapshot
also remains baseline content; delayed parsing cannot make it a fresh event.

`oilbot status --config configs/forward.yaml` shows indexed work counts and
migration progress. Counts describe indexed work only until migration completes.
Status uses SQL aggregates and a bounded heartbeat query instead of loading all
record payloads. Counts still cost a database scan; they are not constant-time.

## Current source slice

### Persistent primary-feed circuit breakers

HTTP 401/403 opens a source circuit immediately. Three consecutive HTTP-200 parse
failures open a parser circuit. Normal network/server errors retain timed backoff;
successful parsing resets the parser streak. Open circuits persist across restarts
and forced polls. Other sources continue normally. These gates apply to primary
feed/listing fetches; legacy detail-page fetching still has its existing failure
handling and is not enabled by the current six-source forward configuration.

Inspect `forward-quality` or `collection-health` for `CIRCUIT_OPEN`. After reviewing
access permission or repairing a parser, explicitly allow another attempt:

```bash
oilbot reset-source-circuit --config configs/forward.yaml --source SOURCE_ID \
  --reason "Describe the access/parser review or fix"
```

This appends an audit transition and clears the circuit, stale HTTP validators,
and failure counters. It does not fetch content or retry failed historical parses.
The next scheduled poll requests full content to exercise the parser again.
A changed source registration invalidates the old policy-bound circuit and logs
`POLICY_CHANGED` when fetched; changes must therefore be intentional and reviewed.
Neither operation authorizes bypassing an access challenge.

### Enabled sources

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
all candidates require review. Updates retain supersession notices and append
`story_event_lineage` plus `forward_evidence_transition` records. New evidence is
`ACTIVE`; an eligible update marks its predecessor `SUPERSEDED`. Explicit source
statuses `correction`, `withdrawal` and `deleted` mark only that story's prior
effective events `CORRECTED`, `WITHDRAWN` or `DELETED`. Intervening non-classified
updates do not sever lineage. Original events and other publishers' evidence
remain unchanged. These are evidence lifecycle states, not truth/confirmation
states. A corrected version is excluded from fresh event generation pending
review. Headline denials still require review; automatic cross-source
`CONTESTED` transitions are not implemented. Feed disappearance is never deletion.

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
`asset_ids` identifies facilities, `location_ids` identifies matched registered
places, and `regions` retains their registry geography. Region normalization
is supported by the matched alias and versioned registry, not a guessed place.
`publisher`, `syndication_origin` and `claim_origin` are separate fields.

Document identity uses explicit UKMTO report IDs, supplied original URLs, or
publisher-scoped native story lineage. Equal normalized headlines never establish
identity: they produce review-only candidates. The legacy `incident_id` field is
a document-identity identifier, not proof that all assertions describe one
physical event. `novelty` means a new document identity, not a trading signal.
Multiple publishers do not imply independent corroboration. No automatic
maritime-authority, operator, or physical confirmation is awarded.

Cross-headline **candidate links** are separate from clustering. The indexed
search compares matching event types within six hours of local receipt, using
claim origin, named vessels/facilities, location and actor mentions. Explicit
conflicting vessel names/IMO IDs, facilities or locations reject a link, as do
explicit event times more than an hour apart. Named-entity matches need shared
context; context-only links need a shared location plus an actor or origin.
Neither is independent corroboration. A separate three-day search emits
`SAME_EPISODE_CANDIDATE` for explicitly configured compatible categories, such as
terminal closure/reopening, with shared entity/context requirements. Same-type
matches use `SAME_EVENT_CANDIDATE`. Different stages are not collapsed into one
event. The time windows are review heuristics, not measured thresholds.

Each relation search examines at most 500 nearby events and emits at most ten links;
compatible types use bounded indexed seeks before combining the candidate pool.
truncation flags expose either limit. Every link records reasons, receipt-time
distance, source-event IDs and `merge_authorized: false`. Incident IDs, novelty
and confirmation are unchanged. A candidate can be wrong; human review remains
necessary. `forward-candidates` reads these records without applying them. Use
the returned `next_after_seq` with `--after-seq` for the next page.
New `fast_event` records contain neither prior-event inputs nor candidate links.
Links live only in separate records, with immutable linker policies and processing
receipts (including zero matches and truncation). Changing the linker version
can derive links over existing immutable events without reclassifying them;
new links are available only at their actual derivation time.

## Human review and as-of episodes

```bash
oilbot review-forward-link --config configs/forward.yaml \
  --candidate CANDIDATE_RECORD_ID --decision SAME_EVENT \
  --reviewer YOUR_NAME --reason "Matching vessel and explicit incident details"
oilbot forward-episodes --config configs/forward.yaml
oilbot forward-episodes --config configs/forward.yaml --through 2026-09-26T12:00:00Z
```

Decisions are `SAME_EVENT`, `SAME_EPISODE`, `SYNDICATED_REPORT`, `UNRELATED` or
`UNCERTAIN`. Each requires a reviewer/reason and gets a local review timestamp.
To revise a decision, supply `--supersedes-review LATEST_REVIEW_ID`; pair-level
validation prevents silently replacing a newer decision, even across linker
versions. Optional `--review-id` supports idempotent retries.

The mapping is derived read-only from immutable reviews as known at the cutoff.
Event identity, episode membership and syndication have separate groups.
Different event types cannot be reviewed as `SAME_EVENT`. An `UNRELATED` decision
inside a transitively joined group quarantines that component into singletons
with an explicit conflict. Evidence lifecycle states are shown separately.
No decision grants confirmation, overwrites an event, or alters receipt times.
Unreviewed events remain singletons in this review-derived view, including events
with shared document identity. This command is an offline full-history derivation,
not part of the collection hot path.

`forward.sqlite3` contains start records, processing/exclusion receipts,
`fast_event`, `forward_incident_revision`, and supersession notices. Outputs and
cursors commit atomically; restarts do not duplicate events. The v3 upgrade adopts
the consumed v1/v2 cursor and preserves the experiment epoch. Rule/registry changes
append effective-dated policy snapshots and apply only to unconsumed revisions;
no new storage root or historic reclassification is required. Events carry
classifier/asset policy hashes and source/asset registry versions. A running
classifier rejects a policy change until restarted. Version constants must be
bumped when extraction or linking logic changes. Old events and their old links
are preserved, including legacy headline-based clustering; v3 does not rewrite
historical mistakes. Legacy evidence without transitions is `LEGACY_UNMODELED`.
Snapshots include the forward journal, policies, lineage, links and reviews:

```bash
.venv/bin/python -m oilbot snapshot --config configs/forward.yaml --out data/forward-snapshot-001
```

This produces a checksummed news-only archive, not a completed market dataset.
Do not copy a live SQLite main file without its WAL; use the snapshot command.

## Remaining milestones

1. Completed integrity core: incremental recovery, independent review-only
   linker, literal slots, evidence lineage, append-only link decisions, derived
   as-of mappings, effective-dated policies and snapshot provenance.
2. First-party identity: bind verified source identity to its authority/asset
   scope so an operator's own operational report need not say "operator says".
   Publication on an official domain alone must not prove a contested claim.
3. Extend revision semantics to carefully reviewed cross-source contradictions
   and denials; do not infer these from publisher identity or feed omission.
4. Expand sources in the order above, testing endpoint/parser behavior and
   preserving source failure isolation. Keep model calls out of the hot path.
5. The combined `forward-quality` command is now available (definitions below).
   Remaining operational work includes source activation/baseline records,
   independent clock synchronization evidence, and long-running soak validation.
6. Validate continuous operation, restarts, disk growth and recoverable backups
   on an always-on host. WSL sleep is a recorded gap, not 24/7 availability.
   No persistent service has been enabled automatically.

The objective is prospective geopolitical intelligence with auditable receipts,
claimants, classifications, incident lineage and corrections. Live market data,
retrospective market attachment, strategy optimization and trading are later
work. Forward mode uses `market.provider: disabled` and rejects `--fixture`.

## Forward dataset-quality report

`oilbot forward-quality --config configs/forward.yaml --window-seconds 86400`
prints a read-only JSON report. Missing databases are reported without creating
them. It never fetches, starts a worker, calls a model, edits evidence or runs a
SQLite checkpoint. SQL removes raw response bodies and article text before
decoding metadata. This remains an offline history scan, not a constant-time
dashboard or hot-path operation; its metadata memory use grows with the journal.

The report includes:

- Lifetime unique native stories, story revisions and fast-event records.
- New native stories and fast events by UTC availability day; events by type.
- Duplicate rate: unchanged story receipts / (unchanged receipts + revisions).
- Revision rate: superseding revisions / all revisions in the window.
- Candidate-link rate: event-policy evaluations emitting links / all evaluations.
  A later linker policy adds evaluations; neither links nor events are incident counts.
- Initial snapshots and other classification exclusions; late recovered parses;
  correction/withdrawal evidence transitions and candidate-search truncations.
- Publication-to-receipt and receipt-to-classification delays, with sample count,
  median, nearest-rank p95 and max; missing, invalid and negative samples remain
  separately visible. Publication lag uses captured live-HTTP story revisions,
  including excluded snapshots, and is not original-event discovery latency.
- Monotonic receive-to-durable-commit milliseconds; source healthy/stale/failing/
  circuit states; indexed recovery work and migration progress; observed runtime gaps.
- Clock samples and current known journal/WAL/log sizes, plus net storage growth
  between recorded samples when at least two exist in the window.

Windows are `(start, evaluated_at]` by record availability; UTC daily counts use
that same timestamp, not publisher timestamps. Missing days are not filled with
zero: the collector may not have run. Lifetime capture counts include baseline
and excluded stories. Empty ratios/latency summaries are null, not invented zeros.
Future-dated records are separately counted and excluded from historical metrics.

Each journal has a consistent read transaction and an exposed maximum sequence.
The report is not an atomic multi-database snapshot: a collector/classifier may
advance between the individual snapshots. Cursor hashes identify the current
scheduling/recovery view. Filesystem sizes are current, approximate samples;
WAL checkpointing can produce negative net growth. Use a checksummed snapshot
when a frozen cross-component research dataset is required.

Workers record clock baselines and periodic samples (roughly once per minute),
plus detected anomalies. UTC is compared to monotonic time only on the same
host/boot; regressions and host/boot changes are explicit. This detects steps or
suspend effects but does **not** measure NTP offset or absolute clock accuracy.
Uncertainty stays null. A slow call inside `work()` is included in heartbeat-gap
checks. Recorded gaps are possible interruptions, not exact downtime estimates.
The news worker samples known storage files roughly every five minutes, without
scanning unrelated directories. Old journals have no invented historical samples.
