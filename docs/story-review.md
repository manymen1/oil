# Offline story review and interpretation

Development branch: `codex/story-review`. Do not merge/deploy during the pinned
48-hour v5 soak. This workflow reads a verified snapshot and writes a separate
annotation database, never the live capture or historical classifier journals.

## CLI

From the development checkout, use its source explicitly when sharing an existing
virtualenv (an editable installation may otherwise point at the soak checkout):

```bash
PYTHONPATH=src python -m oilbot.story_review --manifest data/review-pilot/snapshot/manifest.json browse --sample data/review-pilot/coverage-sample.json
PYTHONPATH=src python -m oilbot.story_review --manifest data/review-pilot/snapshot/manifest.json browse --disposition UNMATCHED
PYTHONPATH=src python -m oilbot.story_review --manifest data/review-pilot/snapshot/manifest.json show --revision REVISION_ID
PYTHONPATH=src python -m oilbot.story_review --manifest data/review-pilot/snapshot/manifest.json annotate --annotations data/review-pilot/annotations.sqlite3 --file annotation.json
PYTHONPATH=src python -m oilbot.story_review --manifest data/review-pilot/snapshot/manifest.json history --annotations data/review-pilot/annotations.sqlite3 --through 2026-09-27T23:00:00Z
```

`show` includes captured title/feed text, original receipt/publication fields and
historical processing/events. It does not fetch article bodies or invent missing
publication timestamps. Browse supports source, disposition, offset and limit.
Reading text in the local human CLI does not authorize forwarding it to a model.

Annotation example (synthetic title: `Iran says tanker attacked near Hormuz`):

```json
{
  "story_revision_id": "REVISION_ID",
  "decision": "add_missed",
  "reviewer": "your-name",
  "reviewer_type": "human",
  "oil_relevance": "relevant",
  "event_family": "attack",
  "assertion": "asserted",
  "claimant": "Iran",
  "oil_mechanism": "transport",
  "reason": "The captured statement names a tanker attack and the speaker.",
  "evidence": [
    {"text_field": "title", "start": 0, "end": 37,
     "quote": "Iran says tanker attacked near Hormuz",
     "supports": ["oil_relevance", "event_family", "assertion", "claimant", "oil_mechanism"]}
  ]
}
```

Use `label` for initial labeling of any story (including negative/unmatched or
baseline stories), `add_missed` for a previously unmatched event family,
`accept`/`reject`/`correct` for historical predictions, or `uncertain` to defer a
decision. The verdict does not grant confirmation or alter old predictions.
Corrections to claimant or other labels are complete replacement annotations:
set `supersedes_annotation_id` to the latest review of that exact revision.
Supply `annotation_id` for idempotent retries. Concurrent stale corrections fail.

Allowed labels:

- Relevance: `relevant`, `irrelevant`, `uncertain`.
- Family: `attack`, `disruption`, `restoration`, `sanctions`, `ceasefire`, `other`, `none`.
- Assertion: `asserted`, `denied`, `hypothetical`, `historical`, `unclear`.
- Claimant: literal named originator or JSON `null` (unknown), not automatically publisher/actor.
- Mechanism: `supply`, `transport`, `demand`, `risk_sentiment`, `unknown`.

The minimal annotation has one primary family. Ambiguous multi-family texts need
an uncertainty note or `other`; do not pretend this is a complete claim graph.
Exact spans are checked against captured text, including claimant spelling.
Anchoring validates provenance, not semantic truth. Human adjudication can still
correct assistant mistakes. Timestamp is assigned at insertion, never supplied
as a historical receipt. UPDATE/DELETE triggers protect the sidecar from routine
mutation; privileged filesystem access is not tamper-proof storage.

Assistant annotations require `reviewer_type: assistant` and a recorded
`authorization` reference. This is a trusted local audit field, not proof of
publisher rights. The user authorized **only the existing 30-story offline pilot**;
live model processing remains pending and no source rights were changed. Do not
send a fresh sample to a model under that authorization.

## Versioned diagnostics

The branch classifier is `fast-event-v6`, with explicit assertion/relevance fields.
It covers active/passive tanker-attack wording, ordinary ceasefire word order,
and Hormuz reopening. Denials, uncertain/hypothetical/historical wording remain
review candidates, never asserted confirmations. Recognized unrelated production
and sanctions contexts are flagged irrelevant, not counted as asserted oil-event
candidates. Unknown geography/context stays uncertain rather than being discarded.

```bash
PYTHONPATH=src python -m oilbot.story_review --manifest data/review-pilot/snapshot/manifest.json diagnose --sample data/review-pilot/coverage-sample.json --text-field title --out title-diagnostic.json
PYTHONPATH=src python -m oilbot.story_review --manifest data/review-pilot/snapshot/manifest.json diagnose --sample data/review-pilot/coverage-sample.json --text-field text --out text-diagnostic.json
PYTHONPATH=src python -m oilbot.story_review --manifest data/review-pilot/snapshot/manifest.json coverage --sample data/review-pilot/coverage-sample.json --annotations data/review-pilot/annotations.sqlite3 --diagnostic text-diagnostic.json --out discovery-report.json
PYTHONPATH=src python -m oilbot.story_review --manifest data/review-pilot/snapshot/manifest.json coverage --sample data/review-pilot/coverage-sample.json --annotations data/review-pilot/annotations.sqlite3 --diagnostic text-diagnostic.json --target asserted_oil_event --out assertion-report.json
```

Omit `--diagnostic` to evaluate historical emitted predictions. Diagnostics preserve
original receipt/availability, record a new evaluation time and bind exact rules,
assets, input field and snapshot/sample hashes. Feed-text mode maps spans to `text`;
it is offline only and does not switch the live recorder away from headlines.
The first-party resolver is not run on retrospective diagnostic candidates.

## Denominators and interpretation

Reports are by source, family and reviewer type, with per-row outcomes, original
strata, population/sample counts and inverse sampling weights (`N/n`). They are
descriptive pilot calculations, not performance certification. Sample membership,
source/disposition and denominators are checked against the snapshot. Excluded
and unprocessed revisions never become forward recall opportunities. Baseline
annotations remain available for inspecting wording but outside forward recall.

Two targets prevent relevance from becoming occurrence:

- `candidate_discovery`: find relevant nonhistorical claims, including denials
  and hypotheses, for review. A matched hypothetical claim is not an event occurrence.
- `asserted_oil_event`: match only asserted relevant oil-event candidates. Still
  unverified, not independent confirmation or executable trading information.

Metrics with no denominators or unresolved rows remain null. Family rows may be
multi-label when predictions disagree, so do not sum them as independent events.
Legacy unqualified emitted hits are evaluated as predictions, not facts; original
v1/v2 output is not mislabeled v5. Assistant and human provenance stays separate.
Recall is conditional on captured material, not stories omitted by feeds or outages.

## Holdout after the soak

Freeze this branch policy and this pilot as tuning material. After the fixed soak
deadline, export a new snapshot and draw a deterministic source/disposition-stratified
sample from **new, nonbaseline story identities not in this pilot**. Keep revisions
of a pilot story and adjudicated same-episode reports out of the holdout; unresolved
episode dependence must be disclosed. Freeze IDs, strata and denominators before
labels or diagnostic inspection. Obtain human labels (or separately authorize any
model review), evaluate once, retain uncertainties, and report both input modes.
Do not claim holdout validation until that sample exists and is reviewed.

The `sample` command freezes metadata-only holdout selection, with all nonempty
cohort strata and denominators. It excludes tuning story identities and known
candidate/review-connected peers conservatively, but cannot detect unlinked episodes:

```bash
PYTHONPATH=src python -m oilbot.story_review --manifest FINAL_SNAPSHOT/manifest.json sample --received-after 2026-09-27T08:39:51Z --exclude-sample data/review-pilot/coverage-sample.json --per-stratum 3 --out holdout-sample.json
```

Run this only after the soak on its cumulative snapshot; inspect/label that frozen
sample independently before drawing any conclusion about generalization.
