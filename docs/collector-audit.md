# Offline collector-quality audit pack

Run from the repository root with the project environment:

```bash
.venv/bin/python scripts/collector_audit.py --config configs/forward.yaml --out data/audits/NEW-NAME
.venv/bin/python -m oilbot replay --manifest data/audits/NEW-NAME/restore-check/manifest.json
```

The destination must not exist. All four journals must already exist. This
news-only exporter refuses an existing nonempty quote archive instead of silently
omitting it. It makes SQLite online backups using read-only source connections;
it never initializes source journals, polls, resets circuits or starts workers.
Partial failed exports are retained for diagnosis, without a completion checksum file.

The pack contains:

- `snapshot/`: SQLite backups, integrity checks and a standard checksummed replay manifest.
- `restore-check/`: a separate-directory copy validated by the normal causal replay reader.
- `quality.json`: seven-day metadata diagnostics computed from the fixed backups.
- `audit.json`: journal fences, recovery/index reconciliation, latest heartbeat,
  original-filesystem size sample and limits of the restore check.
- `coverage-sample.json`: deterministic revision-grain samples by source and
  historical matched/unmatched/excluded/unprocessed disposition, with population
  denominators and blank human labels. Default: three revisions per nonempty stratum.
- `audit_script.py`: the exact exporter used, plus `checksums.json` binding pack files.

The offline headline probe uses the current regex classifier only. It does not
run first-party attribution, override exclusions, emit historical events, call a
model, or supply ground truth. Human labels must remain distinct from these probes.
There is no automatic precision/recall claim: sample strata need weighting,
matched strata can be empty, and baseline exclusions are not forward misses.
For benchmarking, retain labels as a separately dated, versioned sidecar; do not
edit checksum-bound source records or pretend labels were known at receipt.

This is an offline full-history scan, not hot-path collection. Replay validation
loads archived records (including raw payloads) into memory; size the audit host
for the archive. Diagnostics and review samples do not export raw response bodies
outside the backed-up journals.
Backups are individually consistent, not atomic across journals; missing/future
dependencies fail causal validation. Source IDs and timestamp ordering are checked
by the existing replay contract, not a claim of semantic truth.

Response-interval statistics measure gaps between received responses, not exact
poll starts, publisher delays or downtime. Publication-to-receipt statistics include
baseline/old stories and must not be marketed as live-news latency. Historical
processing versions remain visible; current config is not evidence it ran then.

The same-host restore checks bytes and replay dependencies. It does not establish
off-host disaster recovery, forced-crash recovery, or continuous collection.
No storage samples means unknown growth. Complete a separately authorized bounded
soak and human labeling before describing the collector as operationally qualified.
