# Collection hardening — 5 October 2026

This increment adds read-only status inspection, offline CI and a separately
deployable hourly EIA table 9 collector. It does not qualify a strategy, connect
to IBKR or enable orders. Earlier source canaries remain dated evidence.

## Read-only inspection

`oilbot status --config PATH` no longer initializes missing journals. Its
`missing_journals` list distinguishes missing data from empty record counts;
the command does not recover, migrate or repair storage. Existing databases are
opened in SQLite read-only mode. Each journal is read consistently, but the
summary is not an atomic cross-journal research snapshot.

For the existing operational deployment, inspect its actual frozen configuration:

```bash
.venv/bin/python -m oilbot status \
  --config data/runs/20260928T094500Z-operational-v1/config.yaml
```

`configs/operational-forward.yaml` points at a development root and is not an
alias for that running service. Missing optional analysis storage in a news-only
run does not imply a collection failure. Preflight's readiness fields describe
configuration capabilities; use independent health checks to assess collection.

## Separate EIA detail deployment

The existing table 9 journal is `data/macro-detail/20261002-v1/macro.sqlite3`.
Keep this root: creating another one would lose its access-denial latch and
prospective revision lineage. The original table 1/CFTC macro root is incompatible.

The installer freezes Python sources and the reviewed release calendar, hashes
them, and writes four new units. It refuses existing deployments/units and does
not start anything. It reads the private contact file without copying its value
into source, units or provenance. The contact remains an ignored owner-only file.

```bash
.venv/bin/python scripts/install_eia_detail_service.py \
  --config configs/macro-scheduler.json \
  --root data/macro-detail/20261002-v1 \
  --contact-file data/macro/source-contact.env \
  --out data/deployments/eia-detail-20261005-v1

systemd-analyze --user verify \
  "$HOME/.config/systemd/user/oilbot-eia-detail.service" \
  "$HOME/.config/systemd/user/oilbot-eia-detail.timer" \
  "$HOME/.config/systemd/user/oilbot-eia-detail-health.service" \
  "$HOME/.config/systemd/user/oilbot-eia-detail-health.timer"

systemctl --user daemon-reload
systemctl --user enable --now oilbot-eia-detail.timer oilbot-eia-detail-health.timer
```

Collection runs once after boot and then an hour after each completed attempt.
The recorder's existing hourly minimum, permanent 401/403 latch and conservative
429 backoff still apply. Failed requests are not retried rapidly. No table 1/CFTC
or news unit is overwritten or restarted. There is no backfill for host downtime.

Independent monitoring runs every five minutes:

```bash
.venv/bin/python -m oilbot eia-detail-health \
  --root data/macro-detail/20261002-v1 \
  --config data/deployments/eia-detail-20261005-v1/config.yaml
systemctl --user list-timers 'oilbot-eia-detail*'
journalctl --user -u oilbot-eia-detail.service -u oilbot-eia-detail-health.service
```

Health returns exit 2 when degraded. It checks the isolated HTTP policy, capture
age (three-hour maximum), failed/unparsed captures, pending recovery, latched
denials, failed/unfinished requests, reporting period, reviewed release calendar
and available disk space. The missing-release deadline is 62 minutes after the
scheduled publication to accommodate hourly polling and request completion.
Holiday shifts and DST come from the existing reviewed calendar, which expires
at the start of 2027. Calendar expectations are not actual publication times.

Operational checks read metadata, not archived body payloads into Python. They
do not prove raw evidence integrity or exact release latency; use
`eia-detail-report --journal ... --at ...` for as-of raw-bound evidence validation.
Successful polling is not a worker-heartbeat or uptime guarantee. Timer/service
state must also be inspected, and monitoring cannot report while the host is off.

For rollback, stop/disable only the new timers, then stop an in-progress detail
oneshot if necessary. Retain its journal and frozen deployment for recovery:

```bash
systemctl --user disable --now oilbot-eia-detail.timer oilbot-eia-detail-health.timer
systemctl --user stop oilbot-eia-detail.service oilbot-eia-detail-health.service
```

## Verification and remaining work

Deployment verified on 5 October at 08:06:16 UTC:

- Frozen run: `data/deployments/eia-detail-20261005-v1`; source hash verification
  and `systemd-analyze --user verify` both passed.
- Both new timers are enabled. Collector and health oneshots exited successfully;
  `inactive` between timer runs is normal for these oneshots.
- First new capture: `c3c033c4-7d25-47cc-8dea-69681bb8a908`, parsed successfully,
  reporting period 25 September. It matched the existing revision, so no new
  revision was manufactured. Health changed from stale at startup to `HEALTHY`,
  with zero pending parses. This does not fill the earlier collection gap.
- All 725 repository tests passed locally in 83.95 seconds; the wheel built,
  dependency checks and `git diff --check` passed. Tests use synthetic engineering
  inputs and provide no profitability evidence.

The separate research report at
`data/macro-detail/20261002-v1/research-20261005-deployment.json` preserves the
as-of evidence check. Subsequent freshness must be checked live, not inferred
from this deployment snapshot.

`.github/workflows/tests.yml` installs the base development extra, checks
dependencies, runs tests, builds a wheel and checks CLI startup on Python 3.11
and 3.12. No broker credentials or optional broker SDK are required. Adding a
workflow does not establish a successful GitHub run; that requires pushing it
and observing the run. Local verification is reported separately.

The frozen deployment still shares the project's Python environment. Dedicated
locked environments, tested backup/restore, storage retention and external
alerts remain operational work. Do not delete immutable capture history as an
ad hoc disk-management fix.

Next data gates remain: current BSEE report discovery; reviewed source identity
and processing rights; continuous weather capture; and qualified CL/MCL market
observations alongside releases. IBKR capture still requires an explicitly
confirmed logged-in read-only paper session and a private account configuration.
No account identifier, password or login code belongs in chat or committed files.

Only after those inputs are qualified should frozen, causal strategy comparisons
against no-trade and price-only baselines advance to prospective paper testing.
