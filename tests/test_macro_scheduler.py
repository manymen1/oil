from copy import deepcopy
import json
from pathlib import Path
import threading

import pytest

from oilbot.clock import instant
from oilbot.macro import MacroRecorder
from oilbot.macro_scheduler import MacroWorker, calendar, health, load_contact, load_schedule, validate_schedule
from oilbot.store import component_lock
from test_macro import EIA, cot


@pytest.fixture
def policy():
    return load_schedule("configs/macro-scheduler.json")


@pytest.fixture
def clock(monkeypatch):
    current = ["2026-09-30T14:20:00Z"]
    for module in ("oilbot.macro_scheduler", "oilbot.macro", "oilbot.store"):
        monkeypatch.setattr(module + ".utc_now", lambda: current[0])
    return current


@pytest.fixture
def worker(tmp_path, policy, clock, monkeypatch):
    calls = []
    def fetch(source, **kwargs):
        calls.append(source)
        return (EIA if source == "eia" else json.dumps([cot()]).encode()), 200, {}
    monkeypatch.setattr("oilbot.macro.fetch_macro", fetch)
    instance = MacroWorker(tmp_path / "capture", tmp_path / "state", policy, contact="test@example.com")
    return instance, calls


@pytest.mark.parametrize("source,at,mode,next_release", [
    ("eia", "2026-09-30T14:19:59Z", "BACKGROUND", "2026-09-30T14:30:00+00:00"),
    ("eia", "2026-09-30T14:20:00Z", "RELEASE_WINDOW", "2026-09-30T14:30:00+00:00"),
    ("eia", "2026-10-14T14:30:00Z", "BACKGROUND", "2026-10-15T16:00:00+00:00"),
    ("eia", "2026-10-15T15:59:00Z", "RELEASE_WINDOW", "2026-10-15T16:00:00+00:00"),
    ("eia", "2026-11-12T16:55:00Z", "RELEASE_WINDOW", "2026-11-12T17:00:00+00:00"),
    ("cftc", "2026-11-13T20:30:00Z", "BACKGROUND", "2026-11-16T20:30:00+00:00"),
    ("cftc", "2026-11-16T20:30:00Z", "RELEASE_WINDOW", "2026-11-20T20:30:00+00:00"),
])
def test_release_windows_holidays_and_dst(policy, source, at, mode, next_release):
    plan = calendar(policy, source, at)
    assert plan["mode"] == mode and plan["next_release"]["release_at"] == next_release


def test_holiday_report_period_uses_original_week(policy):
    plan = calendar(policy, "cftc", "2026-11-17T02:00:00Z")
    assert plan["expected_release"]["period"] == "2026-11-10"
    plan = calendar(policy, "eia", "2026-11-12T17:10:00Z")
    assert plan["expected_release"]["period"] == "2026-11-06"
    assert not calendar(policy, "eia", "2027-01-01T05:00:00Z")["valid"]


@pytest.mark.parametrize("key,value", [("window_poll_seconds", 1), ("window_poll_seconds", True),
    ("background_poll_seconds", -1), ("window_after_seconds", 0)])
def test_schedule_rejects_unbounded_or_excessive_polling(policy, key, value):
    policy["sources"]["eia"][key] = value
    with pytest.raises(ValueError):
        validate_schedule(policy)


def test_restart_dedupes_polls_and_unchanged_observations(worker, clock):
    w, calls = worker
    result = w.run_once()
    assert result["health"]["status"] == "HEALTHY" and calls == ["eia", "cftc"]
    clock[0] = "2026-09-30T14:20:15Z"
    restarted = MacroWorker(w.root, w.state, w.policy, contact=w.contact)
    assert restarted.run_once()["results"]["eia"]["status"] == "WAIT"
    assert len(calls) == 2
    clock[0] = "2026-09-30T14:21:00Z"
    restarted.run_once()
    assert calls == ["eia", "cftc", "eia"]
    assert len(w.recorder.store.records("macro_revision")) == 2


def test_historical_http_success_does_not_hide_missing_release(worker, clock):
    w, _ = worker
    w.run_once()
    clock[0] = "2026-09-30T14:36:00Z"
    result = w.run_once()
    assert "eia:EXPECTED_RELEASE_MISSING" in result["health"]["issues"]
    assert "eia:SUCCESSFUL_POLL_STALE" not in result["health"]["issues"]


def test_alerts_only_on_issue_changes_and_recovery(worker, clock, monkeypatch):
    w, _ = worker
    w.run_once()
    clock[0] = "2026-09-30T14:36:00Z"
    assert w.run_once()["health"]["state_changed"]
    clock[0] = "2026-09-30T14:36:15Z"
    assert not w.run_once()["health"]["state_changed"]
    assert len(w.store.records("scheduler_alert")) == 1
    next_week = EIA.replace(b"9/18/26", b"9/25/26").replace(b"9/11/26", b"9/18/26")
    monkeypatch.setattr("oilbot.macro.fetch_macro", lambda *a, **k: (next_week, 200, {}))
    clock[0] = "2026-09-30T14:37:00Z"
    result = w.run_once()
    assert result["health"]["status"] == "HEALTHY" and result["health"]["state_changed"]
    assert len(w.store.records("scheduler_alert")) == 2


def test_dead_worker_health_is_readonly(worker, clock):
    w, _ = worker
    w.run_once()
    before = w.store.records(), w.recorder.store.records()
    result = health(w.root, w.state, w.policy, at="2026-09-30T14:24:00Z")
    assert "WORKER_HEARTBEAT_STALE" in result["issues"]
    assert "eia:SUCCESSFUL_POLL_STALE" not in result["issues"]  # exactly interval + grace
    assert before == (w.store.records(), w.recorder.store.records())


def test_health_missing_paths_not_created(tmp_path, policy):
    report = health(tmp_path / "missing", tmp_path / "also-missing", policy)
    assert report["status"] == "DEGRADED" and not list(tmp_path.iterdir())


def test_state_policy_identity_and_writer_lock(worker):
    w, calls = worker
    altered = deepcopy(w.policy)
    altered["tick_seconds"] += 1
    with pytest.raises(ValueError, match="different scheduler"):
        MacroWorker(w.root, w.state, altered)
    with component_lock(w.state, "scheduler"):
        with pytest.raises(RuntimeError, match="active writer"):
            w.run_once()
    assert not calls


def test_expired_calendar_does_not_fetch(worker, clock):
    w, calls = worker
    clock[0] = "2027-01-02T12:00:00Z"
    result = w.run_once()
    assert not calls and all(r["status"] == "BLOCKED" for r in result["results"].values())
    assert "RELEASE_CALENDAR_EXPIRED_OR_NOT_STARTED" in result["health"]["issues"]


@pytest.mark.parametrize("status", [403, 429])
def test_denial_and_rate_limit_persist_across_restart(worker, clock, monkeypatch, status):
    w, calls = worker
    def denied(source, **kwargs):
        calls.append(source)
        return b"", status, {"retry_after": "172800"}
    monkeypatch.setattr("oilbot.macro.fetch_macro", denied)
    first = w.run_once()
    assert first["health"]["status"] == "DEGRADED"
    clock[0] = "2026-09-30T14:21:00Z"
    new = MacroWorker(w.root, w.state, w.policy, contact=w.contact)
    new.run_once()
    assert calls == ["eia", "cftc"]  # no denied endpoint retries


def test_recover_raw_before_next_poll_and_keep_parse_time(worker, clock, monkeypatch):
    w, calls = worker
    w.run_once()
    original = MacroRecorder.parse_capture
    monkeypatch.setattr(MacroRecorder, "parse_capture", lambda *a, **k: None)
    clock[0] = "2026-09-30T14:20:10Z"
    revised = EIA.replace(b"420.000", b"419.000").replace(b"-3.000", b"-4.000")
    w.recorder.ingest("eia", revised)
    monkeypatch.setattr(MacroRecorder, "parse_capture", original)
    clock[0] = "2026-09-30T14:20:20Z"
    new = MacroWorker(w.root, w.state, w.policy, contact=w.contact)
    new.run_once()
    assert len(calls) == 2
    revision = w.recorder.store.records("macro_revision")[-1]
    assert instant(revision["available_at"]) == instant(clock[0])
    assert instant(revision["payload"]["received_at"]) < instant(revision["available_at"])


def test_crashed_cycle_reported_then_recovers(worker, clock):
    w, _ = worker
    w.run_once()
    clock[0] = "2026-09-30T14:20:10Z"
    w.store.append("scheduler_start", {})
    assert "WORKER_CYCLE_INCOMPLETE" in health(w.root, w.state, w.policy, at=clock[0])["issues"]
    assert "WORKER_CYCLE_INCOMPLETE" not in w.run_once()["health"]["issues"]


def test_clock_regression_fails_without_fetch(worker, clock):
    w, calls = worker
    w.run_once()
    clock[0] = "2026-09-30T14:19:00Z"
    with pytest.raises(ValueError, match="clock regressed"):
        w.run_once()
    assert len(calls) == 2


def test_stop_before_run_and_invalid_duration(worker):
    w, calls = worker
    stop = threading.Event()
    stop.set()
    w.run(stop)
    assert not calls
    for duration in (0, -1, True, 86401):
        with pytest.raises(ValueError):
            w.run(stop, seconds=duration)


def test_missing_contact_does_not_block_cftc(worker):
    w, calls = worker
    w.contact = None
    result = w.run_once()
    assert calls == ["cftc"]
    assert result["results"]["eia"]["reason"] == "EIA_OWNER_CONTACT_REQUIRED"
    assert "EIA_OWNER_CONTACT_REQUIRED" in result["health"]["issues"]


def test_contact_not_sent_to_cftc(worker, monkeypatch):
    w, _ = worker
    contacts = {}
    def fetch(source, **kwargs):
        contacts[source] = kwargs.get("contact")
        return (EIA if source == "eia" else json.dumps([cot()]).encode()), 200, {}
    monkeypatch.setattr("oilbot.macro.fetch_macro", fetch)
    w.run_once()
    assert contacts == {"eia": w.contact, "cftc": None}
    assert w.contact not in json.dumps(w.store.records())


def test_low_disk_blocks_network(worker, monkeypatch):
    from types import SimpleNamespace
    w, calls = worker
    monkeypatch.setattr("oilbot.macro_scheduler.shutil.disk_usage", lambda _: SimpleNamespace(free=1))
    result = w.run_once()
    assert not calls and "LOW_DISK_SPACE" in result["health"]["issues"]


def test_network_failure_isolated_and_not_retried_each_tick(worker, monkeypatch, clock):
    import requests
    w, calls = worker
    def fetch(source, **kwargs):
        calls.append(source)
        if source == "eia":
            raise requests.Timeout("private URL must not enter journal")
        return json.dumps([cot()]).encode(), 200, {}
    monkeypatch.setattr("oilbot.macro.fetch_macro", fetch)
    result = w.run_once()
    assert result["results"]["eia"]["status"] == "FAILED"
    assert result["results"]["cftc"]["status"] == "OK"
    clock[0] = "2026-09-30T14:20:15Z"
    w.run_once()
    assert calls == ["eia", "cftc"]
    assert "private URL" not in json.dumps(w.store.records())


def test_bounded_run_does_not_start_another_cycle_after_deadline(worker, monkeypatch):
    w, calls = worker
    monotonic = iter([0, 0, 2, 2])
    monkeypatch.setattr("oilbot.macro_scheduler.time.monotonic", lambda: next(monotonic))
    w.run(threading.Event(), seconds=1)
    assert calls == ["eia", "cftc"]


def test_private_contact_file_never_evaluates_shell(tmp_path):
    path = tmp_path / "contact.env"
    path.write_text("export OILBOT_SOURCE_CONTACT='test@example.com'\n")
    path.chmod(0o600)
    assert load_contact(path) == "test@example.com"
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        load_contact(path)
    path.chmod(0o600)
    path.write_text("export OILBOT_SOURCE_CONTACT='test@example.com'; touch /tmp/should-never-execute")
    with pytest.raises(ValueError):
        load_contact(path)
    link = tmp_path / "link.env"
    link.symlink_to(path)
    with pytest.raises(OSError):
        load_contact(link)


def test_cli_health_missing_state_exits_degraded(tmp_path, capsys):
    from oilbot.cli import main
    assert main(["macro-health", "--root", str(tmp_path / "absent"), "--state", str(tmp_path / "state")]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "DEGRADED"


def test_installer_frozen_units_no_email_no_overwrite(worker, tmp_path, monkeypatch):
    from oilbot.health import verify
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    from install_macro_service import install
    w, _ = worker
    contact = tmp_path / "contact.env"
    contact.write_text("export OILBOT_SOURCE_CONTACT='private@example.com'\n")
    contact.chmod(0o600)
    dest, units = tmp_path / "deployment", tmp_path / "units"
    install("configs/macro-scheduler.json", w.root, w.state, contact, dest, units)
    assert verify(dest) == []
    for path in units.iterdir():
        assert "private@example.com" not in path.read_text()
    assert "macro-worker" in (units / "oilbot-macro.service").read_text()
    assert "macro-health" in (units / "oilbot-macro-health.service").read_text()
    with pytest.raises(ValueError, match="exists"):
        install("configs/macro-scheduler.json", w.root, w.state, contact, dest, units)
