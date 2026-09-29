# Operational report collection and assessment queue

`configs/operational-forward.yaml` enables a new, separately versioned forward
run. Its source set retains the six existing pilot feeds and listings. The new
`operational` worker scans captured title/feed text for operational-change wording
and crude/asset context. Operator, port-authority and maritime-authority reports
receive `primary_operations` priority; other publishers receive
`reported_operations`. Corrections and withdrawals receive `lifecycle` priority.
Priority indicates reviewer workload order, never source truth or confirmation.

The queue is separate from the headline classifier. It catches wording such as
“suspension of crude loading” and operational details present only in feed text.
It is intentionally a broad co-occurrence screen: qualified/denied/proposed
changes remain reviewable, and keywords in separate clauses may be false positives.
It does not infer prior operating state, resolve physical confirmation, estimate
returns, or create `fast_event` records. Every candidate retains exact matching
spans, original receipt, story availability, current queue time, policy hash,
source-processing rights and story/policy dependencies.

The worker processes bounded batches with a transactional cursor and append-only
records in the forward journal. Retries do not duplicate candidates. Later
revisions of a queued story are routed even when operational keywords disappear.
Baseline, old-publication, pre-start, parser reinterpretation and non-HTTP inputs
stay visible with exclusion reasons; default queue reads omit those rows.
All entries require assessment and have `research_eligible: false` and
`trade_authorized: false`. Nothing in this worker calls a model. Queuing is
automatic; assistant assessment is a separate, explicitly dated review step.

```bash
PYTHONPATH=src .venv/bin/python -m oilbot operational-queue --config RUN/config.yaml
PYTHONPATH=src .venv/bin/python -m oilbot operational-queue --config RUN/config.yaml --include-baseline
PYTHONPATH=src .venv/bin/python -m oilbot operational-queue --config RUN/config.yaml --after-seq 100 --limit 50
```

Pagination is by journal sequence, not priority; consumers can prioritize within
the retrieved rows. The queue is a candidate history, not an order ledger or an
automatic verdict. Saving an assistant screen does not change the original
candidate. Only captured fast events can enter the existing transition-review
API; text-only queue entries need story-level screening and remain distinct from
historical classifier output.

## Source coverage review, 28 September 2026

| Source | Use in this run | Operational evidence gap |
| --- | --- | --- |
| Aramco | Existing operator RSS; primary queue priority | Captured descriptions may be short or corporate rather than operational. First-party attribution remains asset/scope limited. |
| Al Jazeera, Iran International, gCaptain | Existing feeds; reported-operations queue | Secondary reports, truncated descriptions, proposals and repeated claims require assessment. |
| CENTCOM, OFAC | Existing listing capture | Military/administrative actions do not prove lost or restored crude operations. Detail fetching remains off. |
| ADNOC | Registered but disabled | Capture-use scope and detail timestamp semantics unresolved. |
| Fujairah | Registered but disabled | Capture-use scope unresolved; previously observed image-only PDFs need qualified OCR/manual text handling. |
| UKMTO | Not activated | Prior access denial remains unresolved. |

Aramco's current [official news page](https://www.aramco.com/news-media) advertises
the same English RSS endpoint used in this run. That confirms the intended feed
channel, not permission for unrestricted article/model processing or a latency SLA.
The [ADNOC terms](https://www.adnoc.ae/terms-and-conditions) and
[Fujairah official site](https://fujairahport.ae/home/) were revisited during this
review; neither was used to mark the project's outstanding qualification checks
complete. Their pending checks are retained in the new source configuration.
No new paid feed, article-body collection, OCR service or source-rights grant is
assumed. Source breadth remains a documented gap; the implemented improvement
is assessment coverage of text already captured by the pilot.

## Run isolation

Prepare a new directory, then launch its frozen source/config rather than a
mutable development checkout:

```bash
.venv/bin/python scripts/prepare_forward_run.py --config configs/operational-forward.yaml --out data/runs/UNIQUE-RUN
```

`provenance.json` records the current Git commit and dirty status, checksums of
the actual copied source, frozen configuration and launch command. This preserves
uncommitted working changes used by the deployment without committing them or
changing the old run. `capture/` is new; original journals, snapshots, gaps and
the early-ended soak remain untouched. The first poll establishes a new baseline.
Check prior circuits before launch; a fresh directory is not authority to bypass
an access denial. Existing first-party attribution reviews retain their expiry.

The supervisor starts `forward`, `linker`, `operational` and `news`, with bounded
restart backoff and runtime/clock/gap records. A user systemd transient service
can keep it running after this chat. It does not guarantee survival of a Windows
shutdown, WSL termination, sleep, full disk or unavailable network. A launch
record should retain the service name and stop command. New policy changes need
another frozen run; do not edit an active run's copied source/config.
