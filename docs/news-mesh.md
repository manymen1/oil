# Multi-source discovery and original claims

`configs/source-profiles.json` contains 39 discovery profiles across global,
regional, Iranian domestic, Gulf state agencies, US/other official sources,
shipping media and operators. It includes all named outlets in the requested
initial network. These are a catalog, not 39 activated collectors. Endpoints,
delivery permissions, paid access, identity verification and independence must
be qualified before activation. The existing observation sources remain in
`configs/observe.yaml`. No subscription is purchased or assumed.

```bash
oilbot source-profiles
```

There is no global trusted/untrusted score for a publisher. `SourceProfile`
separates source type, geography, scoped authority, delivery, independence group,
verification basis and model-processing policy. Authority scopes describe what
an actor can speak for, not whether a contested statement is true. Independence
is unknown by default. Do not give every regional publisher one independence
group, or treat every outlet name as automatically independent.

## Heterogeneous delivery

The existing RSS/Atom and bounded HTML/PDF collectors remain available. A
`StructuredNewsAdapter` accepts a normalized API/licensed/social envelope with
an `items` array. Each item includes `id`, `url`, `title`, and `text`, with optional
`published_at`, `status`, `author`, `original_url`, `reposter`, and `media_sha256`.
Status is `update`, `correction`, `withdrawal` or `deleted`. Metadata survives
immutable capture and replay; it never proves identity or confirmation by itself.

```bash
oilbot ingest-news --source aramco --file data/captured-envelope.json
```

The source must already exist in observation configuration and item URLs must
belong to its registered hosts. Raw bytes are captured before parsing, with the
actual local import receipt time. Importing an old file does not recreate its
historical local receipt time. Publisher-supplied `confirmed`/`trusted` fields
are rejected. The `FastSourceAdapter` protocol is a boundary for future permitted
push/API feeds. It does not lower website polling intervals or implement an X,
Telegram, Reuters, or Newsquawk subscription client.

## Claims and confirmation

Publisher and original claimant are separate. For example, a Fars story quoting
IRGC is preserved with `publisher=fars`, `claim_origin=irgc`, its literal evidence
span and an optional `origin_claim_id`. A second outlet quoting the same statement
does not produce independent corroboration. Deterministic attribution matching
can suggest exact “X says”/“according to X” spans; suggestions are not confirmed
claims or independent evidence.

The first implementation uses reviewed annotations to keep origin, proposition,
episode membership and contradictory values inspectable:

```json
{
  "story_revision_id": "CAPTURED_STORY_REVISION_ID",
  "episode_id": "hormuz-episode-001",
  "claim_key": "two-tankers.damage.at-2026-09-18T12:00Z",
  "event_type": "TANKER_ATTACK",
  "value": "two_hit",
  "polarity": "asserted",
  "claim_origin": "irgc",
  "origin_claim_id": null,
  "basis": "attributed",
  "authority_scope": "own_statements",
  "start": 0,
  "end": 30,
  "quote": "IRGC says two tankers were hit",
  "review_reason": "Replace this example with the actual reviewed proposition and literal span"
}
```

Replace the example identifiers and text with the actual reviewed source claim.
Text offsets must match the captured text exactly.
Use a narrowly defined `claim_key` (asset, proposition and event-time scope) so
different vessels or different times are not mistaken for a contradiction.

```bash
oilbot claim --file data/reviewed-claim.json
oilbot snapshot --out data/claims-snapshot
oilbot dataset --manifest data/claims-snapshot/manifest.json \
  --market data/market-study --policy configs/research.json --out data/claims-study
```

Annotations are append-only and timestamped when recorded. They cannot annotate
a superseded story revision. Optional `supersedes_claim_ids` explicitly retires
earlier reviewed values of the same episode/proposition; it does not rewrite the
past. Ordinary story edits withdraw previous claims until the new revision is
reviewed, and deletion/withdrawal removes their evidential support. Both sides of
a disagreement remain visible in history.

Confirmation progresses through `UNVERIFIED_REPORT`, `OFFICIAL_CLAIM`,
`MULTIPLE_MEDIA_CORROBORATION`, `MARITIME_AUTHORITY_CORROBORATION`,
`OPERATOR_CONFIRMED`, and `PHYSICAL_DISRUPTION_CONFIRMED` as justified. It can also
regress after a correction or become `CONTESTED`. Multiple-media corroboration
requires documented independent original reporting agreeing on the same
proposition/value. Operator/physical confirmation requires a verified first-party
publication in its authority scope. A media quotation from the operator remains
an attributed claim. A social screenshot/repost is never equivalent to the
authenticated author's own channel.

Claim transitions produce research rows linked to CL/MCL receipt/decision state
and all forward horizons. They do not emit trade signals. Known episode conflicts
also veto operational-strategy candidates. This makes it possible to study
regional discovery versus official confirmation without treating repeated
headlines as repeated economic events.
