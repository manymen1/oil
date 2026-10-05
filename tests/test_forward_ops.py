import importlib.util
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from oilbot.cli import record, status
from oilbot.config import load_config
from oilbot.sources import NewsCollector
from oilbot.store import Journal
from test_forward import setup


def test_status_uses_aggregates_not_full_payload_reader(setup, monkeypatch):
    cfg, news, output, worker = setup
    monkeypatch.setattr(Journal, "records", lambda *a, **kw: pytest.fail("full scan"))
    result = status(cfg)
    assert result["journals"]["forward"]["forward_start"] == 1
    assert result["market"]["status"] == "DISABLED_NEWS_ONLY"


def test_status_missing_root_does_not_create_databases(tmp_path, monkeypatch):
    cfg = replace(load_config("configs/forward.yaml"), root=tmp_path / "absent")
    monkeypatch.setattr(Journal, "__init__", lambda *a, **kw: pytest.fail("writable journal opened"))
    result = status(cfg)
    assert set(result["missing_journals"]) == {"news", "analysis", "runtime", "forward"}
    assert result["heartbeats"] == [] and result["attempts_by_day"] == {}
    assert not cfg.root.exists()


def test_status_preserves_budget_cursor_and_heartbeat(setup, monkeypatch):
    cfg, news, output, worker = setup
    analysis, runtime = Journal(cfg.db("analysis")), Journal(cfg.db("runtime"))
    analysis.reserve_attempt(3, at="2026-10-05T01:00:00Z")
    with news.transaction() as db:
        news.set_cursor(db, "recovery:index", {"seq": 12})
    for n in range(4):
        runtime.append("heartbeat", {"n": n})
    before = {p.name: p.read_bytes() for p in cfg.root.glob("*.sqlite3")}
    monkeypatch.setattr(Journal, "__init__", lambda *a, **kw: pytest.fail("writable journal opened"))
    result = status(cfg)
    assert result["attempts_by_day"] == {"2026-10-05": 1}
    assert result["recovery"]["legacy_index"] == {"seq": 12}
    assert [r["payload"]["n"] for r in result["heartbeats"]] == [1, 2, 3]
    assert before == {p.name: p.read_bytes() for p in cfg.root.glob("*.sqlite3")}


def test_linker_cli_component_is_independent_and_network_free(setup):
    cfg, news, output, worker = setup
    record(cfg, "linker", once=True, fixture=None)
    assert output.records("linker_policy_revision")
    assert not news.records("observation")
    assert not output.records("fast_event")


def test_eight_hostnames_can_run_concurrently_but_same_host_serializes(setup, monkeypatch):
    cfg, news, output, worker = setup
    sources = [{**cfg.sources[0], "id": str(n), "url": f"https://host{n}.test/feed"} for n in range(8)]
    # A ninth source shares host 0 and must not overlap its first request.
    sources.append({**sources[0], "id": "9"})
    collector = NewsCollector(news, sources)
    barrier = threading.Barrier(8, timeout=5)
    completed = set()
    lock = threading.Lock()
    def fetch(source):
        if source["id"] != "9":
            barrier.wait()
            with lock:
                completed.add(source["id"])
        else:
            assert "0" in completed
        return {"responses": 1, "revisions": 0, "errors": 0}
    monkeypatch.setattr(collector, "fetch_source", fetch)
    assert collector.poll_once(force=True)["responses"] == 9
    assert len(completed) == 8


def test_supervisor_restart_streak_resets_after_stability():
    path = Path(__file__).resolve().parents[1] / "scripts/supervisor.py"
    spec = importlib.util.spec_from_file_location("oil_supervisor_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.restart_delay(8, 20) == (9, 300)
    assert module.restart_delay(8, 600) == (1, 10)
