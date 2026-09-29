from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest
import yaml

from oilbot.health import inspect, save_check, verify
from oilbot.store import Journal


@pytest.fixture
def run(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    config = {"pipeline": "forward", "broker_execution": "disabled", "operational_queue": True,
        "storage": {"root": str(run / "capture")}, "sources": [{"id": "one", "enabled": True, "poll_seconds": 60}]}
    path = run / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    (run / "provenance.json").write_text(json.dumps({"files": {"config.yaml": hashlib.sha256(path.read_bytes()).hexdigest()}}))
    runtime = Journal(run / "capture/runtime.sqlite3")
    for component in ("news", "forward", "linker", "operational"):
        runtime.append("heartbeat", {"component": component}, available_at="2026-09-28T00:00:00Z")
    news = Journal(run / "capture/news.sqlite3")
    news.append("source_health", {"source_id": "one", "status": "OK"}, available_at="2026-09-28T00:00:00Z")
    with news.transaction() as db:
        news.set_cursor(db, "source:one", {"failures": 0, "circuit": None})
    return run


def check(run, at="2026-09-28T00:00:30+00:00", **kwargs):
    return inspect(run, "oilbot-test.service", now=datetime.fromisoformat(at),
                   service={"ActiveState": "active", "SubState": "running"}, minimum_free_bytes=0, **kwargs)


def test_health_is_read_only_and_records_stale_and_clock_faults(run):
    before = {p: p.read_bytes() for p in (run / "capture").glob("*.sqlite3")}
    assert check(run)["status"] == "HEALTHY"
    stale = check(run, "2026-09-28T01:00:00+00:00")
    assert {"WORKER_STALE:news", "SOURCE_STALE:one"} <= set(stale["issues"])
    assert "WORKER_STALE:forward" in check(run, "2026-09-27T00:00:00+00:00")["issues"]
    assert before == {p: p.read_bytes() for p in before}


def test_hash_mismatch_and_missing_journal_fail_closed(run):
    assert verify(run) == []
    (run / "config.yaml").write_text((run / "config.yaml").read_text() + "# changed\n")
    assert "FROZEN_FILES_CHANGED" in check(run)["issues"]
    (run / "capture/news.sqlite3").rename(run / "capture/old-news.sqlite3")
    assert check(run)["status"] == "DEGRADED"
    assert not (run / "capture/news.sqlite3").exists()


def test_append_only_monitor_preserves_gap_and_recovery(run, tmp_path):
    journal = tmp_path / "monitor.sqlite3"
    first = save_check(journal, check(run, "2026-09-28T01:00:00+00:00"))
    healthy = {**check(run), "checked_at": "2026-09-28T02:00:00+00:00"}
    second = save_check(journal, healthy)
    assert first["status"] == "DEGRADED" and second["state_changed"]
    assert second["monitor_gap_seconds"] == 3600
    with sqlite3.connect(journal) as db:
        assert db.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM checks")
    with pytest.raises(ValueError, match="regressed"):
        save_check(journal, check(run))
    with pytest.raises(ValueError, match="another deployment"):
        save_check(journal, {**healthy, "unit": "different"})


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_never_overwrites_units_or_changes_frozen_run(tmp_path):
    run = tmp_path / "frozen"
    load_script("prepare_forward_run").prepare("configs/operational-forward.yaml", run)
    install = load_script("install_forward_service").install
    deployment, units = tmp_path / "deploy", tmp_path / "units"
    result = install(run, deployment, units)
    assert verify(run) == []
    assert len(result["units"]) == 3
    service = (units / "oilbot-operational.service").read_text()
    assert "--verify-only" in service and "Restart=on-failure" in service
    assert str(run / "code/scripts/supervisor.py") in service
    assert "oilbot-operational-health.service" in (units / "oilbot-operational-health.timer").read_text()
    with pytest.raises(ValueError, match="exists"):
        install(run, tmp_path / "other", units)
