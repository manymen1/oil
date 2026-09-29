# Automated admission and receipt-response milestone

Implemented 29 September 2026, after the [initial recovery work](implementation-2026-09-29.md).
This advances the queue-consumer and response-feature items in the
[automated oil-trading plan](automated-oil-trading-plan-2026-09-29.md).

## What now runs automatically

`oilbot-assessment-admission.service` is enabled as a persistent user service.
It reads the existing operational queue, checks revision bindings and admission
conditions, and stores newly dated resolutions in its own append-only journal.
It polls every 30 seconds, processes at most 100 candidates per batch by default,
and saves its resolution and cursor in one transaction. A retry or restart does
not duplicate decisions. Corrections preserve the earlier resolution and link
the new one through `supersedes_resolution_id`.

Deployment: `data/deployments/20260929-admission-v1/`.
State: `state/admission.sqlite3` within that directory.
Capture remains the original `data/runs/20260928T094500Z-operational-v1/` run.
The collector's frozen code, configuration, source circuits and epochs were not
changed. Startup verifies the new worker's frozen source and configuration hashes.
The new worker was started at **03:24:45 UTC / 11:24:45 Ulaanbaatar**.

This version is an **admission worker, not a qualified economic interpreter**.
It can produce only `ABSTAINED` or `FAILED`; it cannot call a model, assign an
eligible economic assessment or send an order. Reasons include excluded capture,
unpermitted model-processing rights, lifecycle review requirements and the absence
of a validated automatic economic policy. Even permitted processing rights alone
would not qualify a strategy or permit an economic interpretation.

Human economic review remains independently pending. A machine abstention must
never be substituted for a completed human review or a negative economic label.
This worker consumes only `operational_review_candidate` records; unmatched
stories and fast-only events remain separate coverage obligations. Malformed
input, I/O or clock failures stop the pass without advancing past uncommitted
work; the service retries with bounded restart attempts. Missing stories and
invalid revision bindings are recorded as `FAILED` candidates.

The first pass consumed **seven existing baseline candidates**, recorded seven
abstentions and zero failures, and created **zero economic assessments**. A
controlled service restart preserved the seven decisions and resumed at the same
candidate cursor. The existing collector health check remained healthy.

```bash
systemctl --user status oilbot-assessment-admission.service
journalctl --user -u oilbot-assessment-admission.service -n 10
PYTHONPATH=src .venv/bin/python -m oilbot.assessment_queue --config data/runs/20260928T094500Z-operational-v1/config.yaml --out data/deployments/20260929-admission-v1/state/admission.sqlite3 --report
```

The report requires an initialized sidecar; querying before the worker's first
startup can fail with a missing-file error without creating or changing capture.
The service depends on WSL running. It has systemd restart handling and its own
durable heartbeats, but is not yet included in the separate collector health
timer and has no off-host alert destination.

`scripts/install_assessment_service.py` prepares a new frozen copy and installs,
but does not start, its user unit. It refuses existing deployment/unit paths.
Policy/source changes require an explicit new deployment and sidecar; old
abstentions are not silently rewritten under new permissions.

## Research integration

`economic_review dataset` accepts optional `--admissions PATH`. It validates
candidate/story hashes, source policy and capture epochs against the verified
snapshot and writes a separate checksummed `admissions.jsonl` stream. These rows
are **not** inserted into `transitions.jsonl` or counted as economic assessments.
If visible admission inputs are newer than the selected snapshot, export rejects
the mismatch; use a newer verified snapshot or an appropriate as-of cutoff.

The saved run at `data/reviews/20260929-admission-milestone/` contains a verified
149,235-record snapshot and a v2 research export: ten economic-review subjects,
seven admission resolutions, zero economic transition rows, and no market prices.
The empty transition file is intentional, not manufactured evidence of returns.

## Receipt-relative response features

`MarketView.receipt_response()` adds the `receipt-response-v1` feature contract.
It separates:

- Returns over 300, 120, 60, 30, 10, 5 and 1 seconds ending just before receipt.
- The price step at receipt, excluding it from the pre-receipt windows.
- Receipt-to-candidate, candidate-to-decision and receipt-to-decision movement.
- The corresponding observed processing latencies in milliseconds.

Contract selection uses definitions known just before receipt, so a later roll
cannot splice two contracts into one return. Missing/stale quotes, market gaps
and missing instrument definitions leave returns null. Quotes after decision
cannot affect the feature record. The feature contains midpoint observations,
not executable P&L or proof of causation.

The new feature is included in `operational-research-dataset-v2` rows. Existing
legacy strategy feature semantics and support artifacts were not silently
redefined. The default research execution-cost assumptions remain unqualified.

## Remaining plan gates

| Area | Current status |
| --- | --- |
| Durable capture | Running; off-host restore/alerts and full fault/canary gates remain |
| Queue admission | Running and restart-tested; no automatic semantic interpreter |
| Economic review | Causal offline workflow; cross-story episode/novelty automation remains |
| Market response features | Implemented and regression-tested; no qualified live prices |
| Automatic semantic assessment | Requires processing qualification, policy, benchmark and validation |
| Live market recording | Requires provider/access/budget choices and a qualified adapter |
| Paper OMS and portfolio risk | Not implemented in this checkout |
| Broker/live execution | Not implemented or authorized |

The entire roadmap is not complete. Current tests validate engineering behavior;
neither synthetic cases nor baseline abstentions establish a tradable edge.

Validation: full suite **433 passed in 45.45 seconds**; the final focused
admission, receipt-response and pipeline checks also passed. `git diff --check`
passed, the installed systemd unit passed syntax verification, and both live
services were running after the controlled admission-worker restart.
