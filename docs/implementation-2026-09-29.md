# First implementation milestone — 29 September 2026

Implements the initial reliability and research-integration work from the
[development plan](automated-oil-trading-plan-2026-09-29.md). The original plan is
an as-of assessment; this document records the subsequent changes.

## Recovered collection without erasing its gap

The original frozen run remains `data/runs/20260928T094500Z-operational-v1`.
No frozen source/config bytes, source circuits, collection epochs or previous
journal rows were changed. A persistent user service now points to that run:

- `oilbot-operational.service`: frozen supervisor and all four workers.
- `oilbot-operational-health.timer`: independent local checks approximately every
  minute while the user systemd manager runs.
- `oilbot-operational-health.service`: read-only checks of frozen hashes, service
  state, worker heartbeats, source freshness/circuits, free disk and identity-review
  expiry; failures remain observable in systemd and a separate append-only journal.

Deployment metadata and monitoring history are in
`data/deployments/20260929-persistent-v1/`. The installer is
`scripts/install_forward_service.py`; it refuses existing units or a reused
deployment directory. It installs but does not start services. Startup verifies
the original run hashes. `systemd-analyze --user verify` validated the installed
units after correcting the WorkingDirectory syntax before launch.

The service resumed at **28 September 16:48:36 UTC / 29 September 00:48:36 local**.
The first new raw observation was `2026-09-28T16:48:37.606437+00:00`.
Earlier source polls ended around 10:01:02 UTC, and worker heartbeats around
10:01:52 UTC. This is a roughly 6 hour 47 minute interval without observed
collection, not an inferred exact shutdown duration. The native runtime appended
recovery gap records; the monitor also saved the degraded pre-start check.
No missing-time news or prices were backfilled or treated as timely observations.

A controlled SIGTERM of the **forward worker only** at 16:56:01 UTC recovered by
16:56:12 UTC under the supervisor. Its news cursor remained 3278 and both original
09:45:20 UTC epochs were preserved. The news worker stayed running. Evidence is
`data/deployments/20260929-persistent-v1/worker-recovery-check.json`.

Both persistent units are enabled. Existing user lingering was already enabled;
no host power, Windows startup, or user-lingering setting was changed.
This resumes collection when this WSL user manager starts. **It cannot collect
while Windows/WSL is off and has not passed a full Windows/WSL reboot test.**
The health timer is local, not independent of host failure, and sends no external
notifications. Off-host backup/restore, disk-growth/backlog alerting, bounded
retention and the seven-day canary remain outstanding. The virtual environment
is shared with the checkout; dependencies are not yet an immutable runtime image.

Inspect without changing collection:

```bash
systemctl --user status oilbot-operational.service oilbot-operational-health.timer
journalctl --user -u oilbot-operational-health.service -n 10
.venv/bin/python data/deployments/20260929-persistent-v1/health.py --run data/runs/20260928T094500Z-operational-v1
```

## Queue-to-assessment-to-research contract

`forward-economic-v3` / `operational-transition-v1` now accepts a captured
operational candidate directly. It does not invent a `fast_event`, equate keyword
co-occurrence with confirmation, remove capture exclusions, or pretend that an
assistant assessment is an independent human label.

The assessment remains separately dated and append-only. Reports derive current
queue resolution, preserve supersession history, quarantine later story revisions,
and allow literal earlier-story evidence for prior state only when already known
before the current receipt. Cross-story episode identity is still a review
responsibility, not automatically inferred independence.

The new `economic_review dataset` command reconstructs each saved transition at
its original assessment time, attaches decision-time features and separately
calculated outcomes, and retains every subject and assessment revision. Future
unmatured horizons and missing prices cannot become completed returns. Exports
are always research-only and cannot silently enter the legacy strategy fitter.
See [the workflow and contract](economic-transition-review.md).

The live collector continues running its original frozen code. These new offline
research modules run from the current checkout against verified copies; no
mid-experiment runtime code swap was performed.

## Evidence and remaining gates

Final regression run: **420 passed in 43.24 seconds** using
`.venv/bin/python -m pytest -q`. `git diff --check` passed. The independent live
health check at 16:59:09 UTC reported no issues: all four workers were current,
all six enabled sources were healthy, and frozen code/config hashes matched.

The recovery snapshot in `data/reviews/20260929-pipeline-recovery/snapshot/`
verified **7,742 records**. Its research census has **eight subjects**: seven
baseline operational candidates and one fast event. There are **zero saved
economic assessments and zero priced transitions**. The associated queue,
report and dataset metadata are saved alongside the snapshot. This is a verified
local backup/replay check, not an off-host restore exercise or a profitable result.

Synthetic integration tests cover body-only reports, retained exclusions,
candidate deduplication and availability ordering, prior-state evidence,
assistant abstention, superseding assessments, literal/hash integrity, future
horizon censoring and the CLI handoff. Monitoring tests cover stale/future clocks,
missing journals, changed frozen bytes, append-only history and unit overwrite
refusal. Existing safety and research tests remain part of the full-suite check.

Next dependent work is a rights-gated automatic assessment policy and qualified
prospective market recording. No source model-processing rights were changed,
no model was invoked by the collector, no subscription was bought, and no broker
or live order route was enabled. Provider access/budget and later broker/instrument
selection remain explicit decisions. Full phase 0/1 completion also needs the
remaining operational and episode-state checks above; this milestone does not
claim that the entire development plan has passed.
