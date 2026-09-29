# Economic review — 28 September 2026

The user delegated the review to the assistant. The assistant inspected the exact
captured text of all 30 pilot revisions and all nine revisions from the early-ended
soak's existing development sample. This pass is an economic screen of those
39 revisions, with separately dated assistant judgments and literal evidence.

No reviewed revision supports the required before-and-after physical crude
operating state. This is a statement about captured evidence, not a claim that
no disruption or restoration occurred in the world.

| Economic exclusion | Reviewed revisions |
| --- | ---: |
| No stated crude operational change | 23 |
| Sanctions/administrative action without physical operating evidence | 5 |
| Attack without sufficient operational evidence | 4 |
| Proposed restoration, not evidenced resumed operations | 3 |
| Historical or corporate context | 3 |
| Oil-sector commentary without operating states | 1 |

The 39 revisions comprise 18 baseline captures and 21 nonbaseline captures.
Baseline rows remain excluded from forward opportunities. These are stratified
development samples with different sampling weights; the raw counts are not
population prevalence estimates, independent episode counts or accuracy scores.
The nine-story sample had already informed classifier development before this
pass and is not an untouched holdout.

The three Hormuz items discuss conditional, proposed or rejected reopening plans.
They do not show resumed transit. They may describe one diplomatic episode and
must not be treated as three independent economic events. Military strikes and
merchant-shipping warnings likewise do not establish a named crude asset's prior
operating state, impairment, lost flow or subsequent restoration. Corporate
remarks and sanctions listing titles cannot supply those missing facts.

Both verified source snapshots contain zero captured `fast_event` records. The
pilot contains 247 story revisions and the cumulative early-stop snapshot contains
475; these populations overlap. Consequently the transition-review queues are
empty, and this pass creates zero bridge assessments. Story-level exclusions are
saved as separate screens rather than inventing historical event IDs. The empty
bridge reports document this limit. Full snapshot event counts do not mean that
every story in either population was semantically reviewed.

Evidence is saved under
[`data/reviews/20260928-economic-validation/`](../data/reviews/20260928-economic-validation/):

- `decisions.json`: the explicit per-revision economic judgments and reasons.
- `economic-screens.json`: exact spans, revision/snapshot hashes, original receipt
  and availability times, new review time, assistant provenance and exclusions.
- `pilot-queue.json`, `early_stop-queue.json` and corresponding bridge reports:
  actual captured event coverage, without creating synthetic candidates.
- `authorization.json`, `summary.json`, `checksums.json`, and `run_review.py`:
  authorization, denominators, input preservation results and reproducible export.

Original snapshots were checksum/causality verified before and after processing,
with byte preservation checked. Original annotations, classifier output, soak
records and collector gaps remain intact. The local review script makes no
network, model-service, collector, source or broker calls. Its outputs refuse
overwrite; corrections require a separate dated artifact.

The assistant review is complete for this selected evidence. Positive real-data
validation of disruption and restoration remains unestablished because these
samples provide no supported operational transition. The useful next evidence
is a captured operator or maritime-authority operational report identifying the
asset, earlier state and actual change, with enough context to resolve novelty
and episode membership. Prospective capture must retain original receipt times;
later retrieval or retrospective labels cannot become earlier decision inputs.
No trading edge, independent human validation or promotion gate is claimed.
