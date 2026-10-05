import importlib.util
from pathlib import Path
import sys

import pytest

from oilbot.cli import main
from oilbot.eia_detail import DetailRecorder, detail_health
from oilbot.health import verify
from oilbot.macro import MacroRecorder
from oilbot.macro_scheduler import load_schedule
from test_eia_detail import fixture, Session, Response

AT = "2026-10-05T12:00:00Z"


@pytest.fixture
def clock(monkeypatch):
    current = [AT]
    for module in ("oilbot.macro", "oilbot.store", "oilbot.eia_detail"):
        monkeypatch.setattr(module + ".utc_now", lambda: current[0])
    return current


@pytest.fixture
def schedule():
    return load_schedule("configs/macro-scheduler.json")


def test_health_missing_root_read_only(tmp_path, schedule, clock):
    root = tmp_path / "missing"
    assert detail_health(root, schedule)["status"] == "DEGRADED"
    assert main(["eia-detail-health", "--root", str(root)]) == 2
    assert not root.exists()


def test_health_success_staleness_no_raw_report(tmp_path, schedule, clock, monkeypatch):
    w = DetailRecorder(tmp_path)
    w.ingest("eia", fixture())
    monkeypatch.setattr("oilbot.eia_detail.detail_report", lambda *a, **k: pytest.fail("full raw scan"))
    before = w.store.path.read_bytes()
    result = detail_health(tmp_path, schedule)
    assert result["status"] == "HEALTHY" and result["latest_period"] == "2026-09-25"
    assert not result["trade_authorized"] and result["pending_parses"] == 0
    assert before == w.store.path.read_bytes()
    clock[0] = "2026-10-05T15:00:01Z"
    assert "CAPTURE_STALE_OR_MISSING" in detail_health(tmp_path, schedule)["issues"]


@pytest.mark.parametrize("at,missing", [("2026-10-07T15:31:59Z", False), ("2026-10-07T15:32:00Z", True)])
def test_expected_release_respects_hourly_capture_grace(tmp_path, schedule, clock, at, missing):
    clock[0] = at
    DetailRecorder(tmp_path).ingest("eia", fixture())
    result = detail_health(tmp_path, schedule)
    assert ("EXPECTED_RELEASE_MISSING" in result["issues"]) is missing
    assert schedule["sources"]["eia"]["publication_grace_seconds"] == 300


def test_expired_calendar_and_regressed_clock(tmp_path, schedule, clock):
    w = DetailRecorder(tmp_path)
    w.ingest("eia", fixture())
    assert "CAPTURE_STALE_OR_MISSING" in detail_health(tmp_path, schedule, at="2026-10-05T11:00:00Z")["issues"]
    clock[0] = "2027-01-01T12:00:00Z"
    assert "CALENDAR_EXPIRED_OR_NOT_STARTED" in detail_health(tmp_path, schedule)["issues"]


def test_denial_remains_degraded_even_with_later_success(tmp_path, schedule, clock):
    w = DetailRecorder(tmp_path)
    w.ingest("eia", b"denied", status=403)
    w.ingest("eia", fixture())
    assert "ACCESS_DENIAL_LATCHED" in detail_health(tmp_path, schedule)["issues"]
    session = Session(Response(fixture()))
    assert w.collect("eia", contact="test@example.com", session=session)["reason"] == "ACCESS_DENIAL_LATCHED"
    assert not session.calls


def test_failed_parse_and_unfinished_attempt(tmp_path, schedule, clock):
    w = DetailRecorder(tmp_path)
    w.ingest("eia", b"broken")
    assert "LATEST_CAPTURE_FAILED_OR_UNPARSED" in detail_health(tmp_path, schedule)["issues"]
    w.store.append("macro_attempt", {"source": "eia"})
    clock[0] = "2026-10-05T12:02:01Z"
    assert "UNFINISHED_OR_FAILED_ATTEMPT" in detail_health(tmp_path, schedule)["issues"]


@pytest.mark.parametrize("kind", ["original", "import"])
def test_foreign_journals_fail_closed(tmp_path, schedule, clock, kind):
    if kind == "original":
        MacroRecorder(tmp_path)
    else:
        DetailRecorder(tmp_path, delivery="local_import").ingest("eia", fixture())
    assert "HEALTH_CHECK_ERROR:ValueError" in detail_health(tmp_path, schedule)["issues"]


def test_low_disk_and_fetch_failure(tmp_path, schedule, clock, monkeypatch):
    from collections import namedtuple
    w = DetailRecorder(tmp_path)
    w.ingest("eia", fixture())
    w.store.append("macro_fetch_error", {"source": "eia", "reason": "Timeout"})
    usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr("oilbot.eia_detail.shutil.disk_usage", lambda p: usage(1000, 999, 1))
    assert {"LOW_DISK_SPACE", "LATEST_FETCH_FAILED"} <= set(detail_health(tmp_path, schedule)["issues"])


@pytest.fixture
def installer(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("detail_installer", scripts / "install_eia_detail_service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "repo"
    (repo / "src/oilbot").mkdir(parents=True)
    (repo / "src/oilbot/__init__.py").write_text("# frozen test source\n")
    (repo / ".venv/bin").mkdir(parents=True)
    (repo / ".venv/bin/python").symlink_to(sys.executable)
    monkeypatch.setattr(module, "REPO", repo)
    return module


def test_installer_freeze_isolation_privacy_and_no_overwrite(tmp_path, installer, clock):
    root, dest, units = (tmp_path / n for n in ("capture", "deployment", "units"))
    w = DetailRecorder(root)
    w.ingest("eia", fixture())
    contact = tmp_path / "contact.env"
    contact.write_text("OILBOT_SOURCE_CONTACT=test@example.com\n")
    contact.chmod(0o600)
    before = w.store.path.read_bytes()
    args = ("configs/macro-scheduler.json", root, contact, dest, units)
    result = installer.install(*args)
    assert verify(dest) == []
    assert result["poll_interval_seconds"] == 3600
    assert before == w.store.path.read_bytes()
    assert len(list(units.iterdir())) == 4
    assert "OnUnitInactiveSec=3600" in (units / "oilbot-eia-detail.timer").read_text()
    for path in [*dest.rglob("*.json"), *units.iterdir()]:
        assert "test@example.com" not in path.read_text()
    with pytest.raises(ValueError, match="exists"):
        installer.install(*args)
    (dest / "src/oilbot/__init__.py").write_text("# changed\n")
    assert verify(dest) == ["src/oilbot/__init__.py"]


def test_installer_rejects_macro_root_before_writing(tmp_path, installer, clock):
    root, dest = tmp_path / "capture", tmp_path / "deployment"
    MacroRecorder(root)
    contact = tmp_path / "contact.env"
    contact.write_text("OILBOT_SOURCE_CONTACT=test@example.com\n")
    contact.chmod(0o600)
    with pytest.raises(ValueError, match="isolated HTTP"):
        installer.install("configs/macro-scheduler.json", root, contact, dest, tmp_path / "units")
    assert not dest.exists()
