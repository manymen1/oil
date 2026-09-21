# Source qualification and collection health

These commands inspect existing evidence. They do not fetch URLs, initialize
missing journals, call models, purchase data, enable sources, or change policy.
An existing `enabled: true` is reported as a setting, not endorsed as qualified.

## Initial shortlist

`configs/source-qualification.json` proposes eight candidates:

| Coverage | Candidates | Configuration |
| --- | --- | --- |
| Operators | Aramco, ADNOC | Existing observation registrations |
| Port and maritime authority | Fujairah, UKMTO | Existing registrations; UKMTO disabled |
| Shipping reporting | gCaptain | Existing registration |
| Iranian domestic | IRNA | Catalog only |
| Gulf regional | Al Arabiya | Catalog only |
| Iran-focused regional | Iran International | Catalog only |

This shortlist is not a claim of current access, identity verification,
permission, independence, or a running eight-source network. No new feeds are enabled.

## Qualification report

```bash
oilbot qualify-sources
oilbot qualify-sources --source aramco --out data/reports/aramco-qualification.json
```

The default combines the shortlist and observation registrations. `--source`
can be repeated for any registered/catalog source. `--profiles` and `--reviews`
accept alternative files. `--evidence-max-age-seconds` defaults to 86400; changing
it is a policy choice, not new evidence. Output files are never overwritten.

Capture checks require:

- A registered HTTPS endpoint satisfying allowed-host, port and credential policy.
- A profile and unexpired review bound to exact source/profile hashes.
- Reviewed identity, capture permission, timestamp semantics, revision handling,
  and original-claim attribution, with inspectable evidence references.
- A recent successful endpoint response and no latest collection failure.
- A recent nonempty parsed sample linked to an archived HTTP 200 response.
  Detail articles and duplicate receipts can establish parser evidence. Empty
  feeds and 304s alone cannot; fixtures and local imports never qualify live access.

Source hashes include adapter, endpoint, allowed hosts, polling, enabled flag
and rights. Changing the registration/profile requires a new review and matching
collection evidence. Reviewed evidence does not automatically update profiles
or authorize first-party confirmation in the claim engine.

The `reviews` array starts empty. Use report-provided hashes when adding a
review. Replace the following placeholders with actual reviewed evidence:

```json
{
  "source": "aramco",
  "source_policy_hash": "HASH_FROM_REPORT",
  "profile_hash": "HASH_FROM_REPORT",
  "reviewed_at": "2026-09-21T12:00:00Z",
  "expires_at": "2026-10-21T12:00:00Z",
  "reviewer": "REVIEWER_IDENTIFIER",
  "checks": {
    "identity": {"status": "pending", "evidence": ""},
    "capture": {"status": "pending", "evidence": ""},
    "model_processing": {"status": "pending", "evidence": ""},
    "timestamps": {"status": "pending", "evidence": ""},
    "revisions": {"status": "pending", "evidence": ""},
    "attribution": {"status": "pending", "evidence": ""}
  }
}
```

Use `verified` for identity/timestamp/revision/attribution, `permitted` for
capture/model use, or `pending`/`prohibited`. Positive and prohibited decisions
require evidence references: a reviewed policy URL, agreement reference, or
dated review document. Dates above illustrate syntax, not a chosen expiry.
Reports do not independently establish the legal meaning of those references.

Model checks also require explicit `permitted` settings in both observation
configuration and the profile. Unknown independence remains unknown: it does
not block capture, but outlet counts cannot establish independent corroboration.

Qualification is advisory and fail-closed in its report, not a new activation
controller. The collector still obeys observation configuration. Activation is
an explicit reviewed change; keep dated reviews and reports for the audit trail.

## Collection health

```bash
oilbot collection-health --window-seconds 86400 --out data/reports/news-health.json
```

States are `HEALTHY`, `DEGRADED`, `STALE`, `FAILING`, `NEVER_SUCCEEDED`, and
`DISABLED`. Staleness is three configured poll intervals since the last parsed
successful response. Backoff is separate and never excuses staleness. A 304 can
show transport health without proving parser compatibility or new content.

Reports include response/success/status evidence IDs, consecutive failed health
events, next poll/backoff, observed response intervals, response and revision counts, detail failures,
corrections/withdrawals, missing/future/invalid publication times, and
capture-to-parse delay (count, median, p95, max). Publication-to-receipt delay is
descriptive, not original-event discovery latency. Quiet publishing is not a
collection failure; counts are journal events, not measured uptime percentages.

The window limits counters, not last-success lookup. Synthetic/local imports
are separate and cannot make a collector healthy. Unparsed responses remain
visible. First real content remains backfill after failed/empty/304 startup
polls. New collector records preserve requested URLs and policy-bound health
metadata, including network failures. Older unbound evidence cannot qualify a
changed configuration.

SQLite reads use a read-only transaction including committed WAL data. Reports
do not migrate or mutate records/cursors. Commands currently load the pilot
journal into memory; large archives need a bounded reporting index first.

## Next operational step

Review identity and permitted usage, register chosen endpoints, and obtain
bounded transport/parser evidence before activating additional collectors.
Then start the news-only shakedown. These reports are prerequisites, not evidence
that the pilot has run or that a trading edge exists.
