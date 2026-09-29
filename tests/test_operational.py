from dataclasses import replace
import json

import pytest

from oilbot.cli import record
from oilbot.config import load_config
from oilbot.operational import OperationalQueue, read_queue, signals
from oilbot.replay import export_manifest, load_manifest
from oilbot.store import Journal
from test_forward import setup, warm, capture


def worker(setup):
    cfg, news, output, _ = setup
    return OperationalQueue(news, output, cfg.raw["assets"], cfg.sources)


def test_body_only_operator_report_keeps_exact_spans_and_no_confirmation(setup):
    cfg, news, output, _ = setup
    q = worker(setup)
    source = next(s for s in cfg.sources if s["id"] == "aramco")
    warm(news, source)
    # Capture a title plus a different feed body using the actual ingestion API.
    from oilbot.schema import NewsItem
    from oilbot.clock import stamp
    now = stamp()
    text = "Operations update\nCrude production resumed after suspension at Ras Tanura."
    oid = news.capture(source, {"url":source["url"], "status":200, "content_type":"text/xml", "headers":{},
        "started":now, "first_byte":now, "received":now, "body":text.encode(), "delivery":"http", "synthetic":False})
    rid = news.accept_items(source, oid, [NewsItem("body", source["url"], "Operations update", text)], {})[0]
    assert q.run_once()["queued"] == 1
    rows = read_queue(output.path)["candidates"]
    assert len(rows) == 1
    p = rows[0]["payload"]
    assert p["story_revision_id"] == rid and p["priority"] == "primary_operations"
    assert not p["research_eligible"] and not p["trade_authorized"] and p["confirmation"] == "UNVERIFIED"
    assert p["model_processing"] == "pending"
    for span in p["evidence"]:
        assert news.get(rid)["payload"][span["text_field"]][span["start"]:span["end"]] == span["quote"]
    assert not output.records("fast_event")


def test_baselines_proposals_and_restarts(setup):
    cfg, news, output, _ = setup
    q = worker(setup)
    capture(news, cfg.sources[0], "Oil exports suspended", native="baseline")
    capture(news, cfg.sources[0], "Plan would reopen Hormuz if talks succeed", native="proposal")
    assert q.run_once()["queued"] == 2
    rows = read_queue(output.path)["candidates"]
    assert len(rows) == 1
    assert any(e["supports"] == "qualified_wording" for e in rows[0]["payload"]["evidence"])
    assert not rows[0]["payload"]["research_eligible"]
    assert len(read_queue(output.path, include_baseline=True)["candidates"]) == 2
    assert worker(setup).run_once() == {"processed":0,"queued":0,"policy_hash":q.policy_hash}
    assert len(output.records("operational_review_policy")) == 1


def test_revision_and_withdrawal_stay_in_queue_without_repeating_keywords(setup, tmp_path):
    cfg, news, output, _ = setup
    q = worker(setup)
    warm(news, cfg.sources[0])
    first = capture(news, cfg.sources[0], "Crude exports suspended", native="x")
    q.run_once()
    capture(news, cfg.sources[0], "Story withdrawn", native="x", status="withdrawal")
    assert q.run_once()["queued"] == 1
    rows = read_queue(output.path)["candidates"]
    assert len(rows) == 2 and rows[-1]["payload"]["previous_candidate_id"] == rows[0]["id"]
    assert rows[-1]["payload"]["priority"] == "lifecycle"
    assert rows[-1]["payload"]["evidence"] == []
    assert rows[0]["payload"]["story_revision_id"] == first["id"]
    manifest = export_manifest(cfg, tmp_path / "snapshot")
    load_manifest(manifest)  # New policy/candidate dependencies replay causally.


def test_transaction_failure_does_not_drop_or_duplicate_queue_work(setup, monkeypatch):
    cfg, news, output, _ = setup
    q = worker(setup)
    warm(news, cfg.sources[0])
    capture(news, cfg.sources[0], "Oil production halted")
    original = output.set_cursor
    monkeypatch.setattr(output, "set_cursor", lambda *a: (_ for _ in ()).throw(RuntimeError("fail")))
    with pytest.raises(RuntimeError):
        q.run_once()
    assert output.records("operational_review_candidate") == []
    monkeypatch.setattr(output, "set_cursor", original)
    assert q.run_once()["queued"] == 1
    assert q.run_once()["queued"] == 0


def test_read_only_pagination_and_unrelated_text(setup, tmp_path):
    cfg, news, output, _ = setup
    q = worker(setup)
    warm(news, cfg.sources[0])
    for i in range(3):
        capture(news, cfg.sources[0], "Crude loading resumed", native=str(i))
    capture(news, cfg.sources[0], "Swimming competition stopped", native="irrelevant")
    q.run_once(limit=2)
    q.run_once(limit=2)
    q.run_once(limit=2)
    first = read_queue(output.path, limit=2)
    second = read_queue(output.path, after_seq=first["next_after_seq"], limit=2)
    assert len(first["candidates"]) == 2 and len(second["candidates"]) == 1
    assert read_queue(tmp_path / "missing") == {"candidates":[], "next_after_seq":0}
    assert not (tmp_path / "missing").exists()


def test_future_story_waits_then_recovers(setup, monkeypatch):
    cfg, news, output, _ = setup
    q = worker(setup)
    news.append("story_revision", {"source_id":cfg.sources[0]["id"], "story_id":"future", "title":"Oil exports halted",
        "text":"Oil exports halted", "local_received_at":"2099-01-01T00:00:00Z", "initial_snapshot":False}, available_at="2099-01-01T00:00:00Z")
    assert q.run_once()["processed"] == 0
    monkeypatch.setattr("oilbot.operational.utc_now", lambda: "2099-01-01T00:00:01Z")
    assert q.run_once()["queued"] == 1
    assert "NOT_LIVE_HTTP" in output.records("operational_review_candidate")[0]["payload"]["exclusions"]


def test_operational_config_and_component_are_network_free(tmp_path):
    cfg = replace(load_config("configs/operational-forward.yaml"), root=tmp_path)
    assert cfg.raw["operational_queue"]
    assert not next(s for s in cfg.sources if s["id"] == "adnoc")["enabled"]
    result = record(cfg, "operational", once=True, fixture=None)
    assert result["queued"] == 0
    assert Journal(cfg.db("news")).records("observation") == []


@pytest.mark.parametrize("text", ["Crude exports have resumed", "Suspension of crude loading", "No pipeline outage reported", "Repairs at Ras Tanura terminal", "Proposal for reopening Hormuz"])
def test_screen_catches_reversed_and_qualified_wording(text):
    assert signals({"title":text,"text":text}, load_config("configs/operational-forward.yaml").raw["assets"])
