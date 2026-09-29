# Economic transition review

This offline development workflow implements the next gate in the trading
roadmap: collect dated operational assessments and inspect the bridge's results.
It reads a verified snapshot and stores assessments in a separate append-only
SQLite file. It does not start a collector, change a running soak, call a model,
or connect to market data or a broker.

Use the project environment with `PYTHONPATH=src` so an editable installation
pointing to a different checkout cannot select the wrong code:

```bash
PYTHONPATH=src .venv/bin/python -m oilbot.economic_review --help
PYTHONPATH=src .venv/bin/python -m oilbot.economic_review --manifest SNAPSHOT/manifest.json queue --out REVIEW/queue.json
PYTHONPATH=src .venv/bin/python -m oilbot.economic_review --manifest SNAPSHOT/manifest.json template --event EVENT_ID --out REVIEW/template.json
```

`queue` exports subject IDs, candidate kind, source, original event type when
present, receipt and baseline flag; it does not export story text. Subjects include
operational text candidates without an already-available fast event. The first
queued subject cannot be replaced by a later classifier. Duplicate queue policies
do not create additional subjects for the same revision. It is a candidate census,
not a random sample, holdout or recall denominator. An empty queue is a valid
result. Use the separate [story-review workflow](story-review.md) for unmatched
stories and false negatives. Existing collector gaps remain in the original
snapshot and must be considered before future market research.

`template` includes the exact captured story and event and its snapshot episode
group. It supplies blank/unknown assessment fields; it does not label an event.
Extract the `assessment` object to a new `assessment.json`, then have a reviewer
fill it using the [economic protocol](forward-economic-research.md).

In particular, record:

- The prior and new operating states, and whether the change is novel within
  the episode. A headline describing an attack does not establish suspended oil
  operations; use `UNKNOWN` when prior operating state is absent.
- The named claimant, evidence class, crude supply/transport mechanism and stage.
  Use `partial_restoration` only with `PARTLY_RESTORED`, and `restoration` with
  `RESTORED`. The claimant must appear in its supporting span.
- Literal evidence spans: `text_field` (`title` or `text`), zero-based `start`,
  exclusive `end`, exact `quote`, and a `supports` list naming reviewed fields.
  Every non-unknown interpreted field needs evidence. Literal matching checks
  provenance, not whether the interpretation is correct.
- Reviewer name, `reviewer_type`, rationale, explicit `episode_reviewed` and
  `novel` booleans. Unresolved entity names remain visible and exclude eligibility.

Save and inspect the assessment:

```bash
PYTHONPATH=src .venv/bin/python -m oilbot.economic_review --manifest SNAPSHOT/manifest.json assess --assessments REVIEW/assessments.sqlite3 --file REVIEW/assessment.json
PYTHONPATH=src .venv/bin/python -m oilbot.economic_review --manifest SNAPSHOT/manifest.json history --assessments REVIEW/assessments.sqlite3
PYTHONPATH=src .venv/bin/python -m oilbot.economic_review --manifest SNAPSHOT/manifest.json report --assessments REVIEW/assessments.sqlite3 --out REVIEW/report.json
```

Timestamps are assigned when saving; supplying a historical timestamp is rejected.
Assessments cannot precede the story or classifier. Each saved row binds snapshot,
story and event hashes and preserves the transition computed at review time.
The bridge is `forward-economic-v3`, with contract `operational-transition-v1`.
It accepts captured fast events and operational candidates, retaining the original
kind and rejecting queue exclusions from research eligibility. The disruption
family is `physical_disruption`; states remain explicit uppercase values and
transitions remain `BEFORE->AFTER`. Hypothesis direction is not numeric order
direction. No legacy trading-policy adapter is inferred from these fields.

An evidence span may additionally contain `story_revision_id` for a prior captured
story in the same verified snapshot. Such spans can support only `state_before`;
the prior story must have been available before the current report's receipt.
Its hash and ID become transition inputs. This checks causality and exact text,
not semantic asset/episode identity: the reviewer still has to justify those.

For retries, supply a stable `assessment_id`. Exact retries return the original
record; ID reuse with changed content fails. To correct an assessment, submit a
complete replacement with a new ID and `supersedes_assessment_id` naming the
latest assessment for that event. Old rows remain readable; stale replacements
and clock regressions fail. SQLite triggers reject ordinary updates/deletes.
Use a different assessment database for each snapshot. These protections prevent
accidental rewrites, not tampering by a privileged filesystem user.

`history` and `report` accept `--through TIMESTAMP`. Reports select the latest
assessment available then and rebuild the episode/evidence mapping as of that
time. They retain unreviewed event IDs, assessment revision counts, human versus
assistant counts, invalid assessments, exclusion reasons, eligible transitions
and the number of eligible episode groups. `queue_states` derives `PENDING`,
`ASSESSED`, `ABSTAINED` or `FAILED` from the immutable assessment history and
current validation; it does not rewrite captured queue rows. `FAILED` is an
invalid saved assessment, not a silent dismissal of an unsuccessful submission.
Operational subjects remain singleton episodes pending a future cross-story
episode workflow; later revisions of their story quarantine the earlier subject.
Re-evaluation uses its own decision
time; it cannot make a later review available at original receipt. Lifecycle is
limited to the snapshot's captured history, including when evaluating after its
capture date. Reports do not check subsequent live corrections.

Every output path must be outside the snapshot; JSON exports refuse overwrite.
Sidecars cannot alias captured journals or reuse a database with unrelated tables.
The snapshot is verified before any command runs. No command opens a live capture
journal for writing, changes candidate links, or marks a new sample as a holdout.

## Exporting research rows

```bash
PYTHONPATH=src .venv/bin/python -m oilbot.economic_review --manifest SNAPSHOT/manifest.json dataset --assessments REVIEW/assessments.sqlite3 --out REVIEW/dataset
```

Optionally supply `--market-root` for an existing checksummed market archive and
`--through` for an as-of cutoff. This does not subscribe to or fetch market data.
Optional `--admissions PATH` includes the independently bound machine admission
history in `admissions.jsonl`, never as economic labels. See the
[admission worker and receipt-response contract](assessment-admission.md).
The export contains `subjects.jsonl` (including pending/excluded subjects),
`transitions.jsonl` (every saved assessment revision), and checksummed metadata.
Each transition is reconstructed at its **original review time**, not report time
or publisher time, before attaching causal features and separate forward labels.
Incomplete future horizons stay `PENDING_HORIZON`; absent prices stay absent.
Old bridge-version assessments require an explicit migration/new review; their
original timestamps are never relabeled with a newer contract.

This is a separate research-only format: the legacy `oilbot dataset` path now
rejects forward records instead of silently producing zero rows. The new format
does not feed `fit-support`, does not call the trading strategy, and always exports
`action: ABSTAIN`, `trade_authorized: false`, and `promotion: false`. Draft cost
defaults and CL/MCL research roles are not selected execution settings.

## Completing the human gate

Review positive disruption and partial/full restoration cases, negatives and
uncertain cases, prior-state unknowns, stale/repeated claims, baseline exclusions,
corrections and withdrawals. Reconcile disagreements against original spans and
retain corrections with reasons. Separate development review from the untouched
post-soak holdout described in the existing protocol. Report missing strata and
episode dependence rather than treating a small convenience set as validation.

Assistant assessments require an `authorization` reference and remain excluded
from independent eligibility. Do not label assistant work as human work. This
implementation creates no real labels and does not inspect a new holdout.

`research_eligible` means an assessment satisfies the bridge's declared filters.
It does not prove that the labels are economically correct or independently
validated. Reports therefore retain `HUMAN_SEMANTIC_VALIDATION_REQUIRED`, even
when all captured candidates have assessments. There is no automatic promotion
threshold. Exact human review coverage, disagreements and unresolved errors must
be understood before freezing the protocol and adding market response research.
Market availability and historical support remain unavailable, direction remains
null, and `trade_authorized` remains false.
