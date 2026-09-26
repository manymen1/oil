from dataclasses import replace

import pytest

from oilbot.clock import stamp
from oilbot.config import load_config
from oilbot.schema import NewsItem
from oilbot.sources import NewsCollector
from oilbot.store import Journal


@pytest.fixture
def setup(tmp_path):
    config = replace(load_config("configs/forward.yaml"), root=tmp_path)
    source = config.sources[0]
    news = Journal(config.db("news"))
    return news, source, NewsCollector(news, [source], lambda *_: pytest.fail("no network in recovery"))


def capture(news, source, name="one", status=200):
    now = stamp()
    body = f'<rss><channel><item><guid>{name}</guid><title>Tanker attacked</title></item></channel></rss>'.encode()
    return news.capture(source, {"body": body, "url": source["url"], "status": status,
        "content_type": "text/xml", "headers": {}, "started": now, "first_byte": now,
        "received": now, "delivery": "http"})


def state(news, oid):
    with news.connect() as db:
        return dict(db.execute("SELECT * FROM parse_work WHERE observation_id=?", (oid,)).fetchone())


def test_network_runs_before_recovery_and_old_parse_cannot_replace_new(setup, monkeypatch):
    news, source, collector = setup
    old = capture(news, source)
    calls = []
    def fetch(*args):
        calls.append("network")
        now = stamp()
        return {"body": b'<rss><channel><item><guid>one</guid><title>New report</title></item></channel></rss>',
                "url": source["url"], "status": 200, "content_type": "text/xml", "headers": {},
                "started": now, "first_byte": now, "received": now, "delivery": "http"}
    collector.fetcher = fetch
    recover = collector.recover_unparsed
    def recovery():
        calls.append("recovery")
        return recover()
    monkeypatch.setattr(collector, "recover_unparsed", recovery)
    collector.poll_once(force=True)
    assert calls == ["network", "recovery"]
    assert state(news, old)["state"] == "parsed"
    assert [r["payload"]["title"] for r in news.records("story_revision")] == ["New report"]
    late = news.records("late_story_parse")[0]
    assert late["payload"]["title"] == "Tanker attacked"
    assert late["payload"]["exclusion"] == "OLDER_OBSERVATION"
    assert news.cursor("story:" + late["payload"]["story_id"])["revision_id"] == news.records("story_revision")[0]["id"]


def test_duplicate_receipt_advances_recovery_watermark(setup):
    news, source, collector = setup
    first = capture(news, source)
    item = NewsItem("one", source["url"], "Current", "Current")
    news.accept_items(source, first, [item], {})
    pending = capture(news, source)
    latest = capture(news, source)
    news.accept_items(source, latest, [item], {})
    collector.recover_unparsed()
    assert len(news.records("story_revision")) == 1
    assert news.records("late_story_parse")[0]["payload"]["input_revision_ids"][0] == pending


def test_older_unseen_guid_recovered_after_first_snapshot_stays_baseline(setup):
    news, source, collector = setup
    old = capture(news, source, "old")
    new = capture(news, source, "new")
    news.accept_items(source, new, [NewsItem("new", source["url"], "New snapshot", "New snapshot")], {})
    collector.recover_unparsed()
    recovered = news.records("story_revision")[-1]["payload"]
    assert recovered["native_id"] == "old"
    assert recovered["initial_snapshot"]
    assert recovered["input_revision_ids"] == [old]


def test_recovery_never_calls_full_journal_reader(setup, monkeypatch):
    news, source, collector = setup
    oid = capture(news, source)
    monkeypatch.setattr(news, "records", lambda *a, **kw: pytest.fail("full journal scan"))
    assert collector.recover_unparsed()["attempted"] == 1
    assert state(news, oid)["state"] == "parsed"
    for _ in range(3):
        result = collector.recover_unparsed()
        assert result["attempted"] == 0 and result["migration"]["scanned"] == 0


def test_migration_fence_waits_for_later_parse_receipts(setup):
    news, source, collector = setup
    old = capture(news, source)
    news.accept_items(source, old, [NewsItem("one", source["url"], "Old", "Old")], {})
    pending = capture(news, source, "two")
    with news.transaction() as db:
        db.execute("DELETE FROM parse_work")  # Simulate a pre-queue journal.
    result = collector.recover_unparsed(migration_limit=1)
    assert result["attempted"] == 0 and not result["migration"]["complete"]
    while not result["migration"]["complete"]:
        result = collector.recover_unparsed(migration_limit=1)
        assert result["migration"]["scanned"] <= 1
    assert state(news, old)["state"] == "parsed"
    assert state(news, pending)["state"] == "parsed"
    assert len(news.records("story_revision")) == 2  # Old content wasn't replayed as an update.


def test_live_pending_not_starved_during_legacy_migration(setup):
    news, source, collector = setup
    for n in range(10):
        capture(news, source, str(n))
    assert not collector.recover_unparsed(migration_limit=1)["migration"]["complete"]
    live = capture(news, source, "new")
    result = collector.recover_unparsed(migration_limit=1)
    assert result["attempted"] == 1
    assert state(news, live)["state"] == "parsed"


def test_disabled_and_unknown_sources_remain_pending(setup):
    news, source, collector = setup
    oid = capture(news, source)
    assert NewsCollector(news, []).recover_unparsed()["attempted"] == 0
    assert NewsCollector(news, [{**source, "enabled": False}]).recover_unparsed()["attempted"] == 0
    assert state(news, oid)["state"] == "pending"
    assert collector.recover_unparsed()["attempted"] == 1


def test_parse_failure_is_explicit_and_not_retried_forever(setup):
    news, source, collector = setup
    oid = capture(news, source, status=403)
    collector.recover_unparsed()
    assert state(news, oid)["state"] == "failed"
    assert "RECOVERED_HTTP_403" in state(news, oid)["reason"]
    assert collector.recover_unparsed()["attempted"] == 0
    assert news.get(oid)["kind"] == "observation"


def test_success_health_without_receipt_does_not_hide_pending(setup):
    news, source, collector = setup
    oid = capture(news, source)
    news.append("source_health", {"source_id": source["id"], "status": "OK", "input_revision_ids": [oid]})
    assert collector.recover_unparsed()["attempted"] == 1
    assert state(news, oid)["state"] == "parsed"


def test_changed_source_policy_fails_without_reinterpretation(setup):
    news, source, collector = setup
    oid = capture(news, {**source, "url": "https://old.example.test/rss"})
    collector.recover_unparsed()
    assert state(news, oid)["state"] == "failed"
    assert "SOURCE_POLICY_CHANGED" in state(news, oid)["reason"]
    assert not news.records("story_revision")


def test_recovery_batch_limit_and_index_plan(setup):
    news, source, collector = setup
    for n in range(12):
        capture(news, source, str(n))
    result = collector.recover_unparsed(limit=2, time_budget_seconds=100)
    assert result["attempted"] == 2
    with news.connect() as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT observation_id FROM parse_work WHERE state='pending' AND source_id=? AND observation_seq>? ORDER BY observation_seq LIMIT 8", (source["id"], 0)).fetchall()
        assert "parse_work_pending" in " ".join(r["detail"] for r in plan)
        plan = db.execute("EXPLAIN QUERY PLAN SELECT 1 FROM records WHERE kind='story_revision' AND json_extract(payload,'$.source_id')=? LIMIT 1", (source["id"],)).fetchall()
        assert "story_source" in " ".join(r["detail"] for r in plan)


def test_raw_capture_and_pending_work_are_atomic(setup):
    news, source, collector = setup
    with pytest.raises(RuntimeError):
        with news.transaction() as db:
            news.append("observation", {"source_id": source["id"]}, record_id="crashed", db=db)
            raise RuntimeError("crash")
    assert news.get("crashed") is None
    with news.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM parse_work").fetchone()[0] == 0


def test_parse_completion_rolls_back_with_story_transaction(setup, monkeypatch):
    news, source, collector = setup
    oid = capture(news, source)
    original = news.append
    def failing(kind, *args, **kwargs):
        result = original(kind, *args, **kwargs)
        if kind == "parse_receipt":
            raise RuntimeError("crash after parse receipt")
        return result
    monkeypatch.setattr(news, "append", failing)
    with pytest.raises(RuntimeError):
        collector.recover_unparsed()
    assert state(news, oid)["state"] == "pending"
    assert not news.records("story_revision")
    monkeypatch.setattr(news, "append", original)
    assert collector.recover_unparsed()["attempted"] == 1


def test_recorded_failure_not_retried_after_reopening(setup):
    news, source, collector = setup
    oid = capture(news, source, status=403)
    collector.recover_unparsed()
    reopened = NewsCollector(Journal(news.path), [source])
    assert reopened.recover_unparsed()["attempted"] == 0
    assert state(news, oid)["state"] == "failed"


def test_collection_failure_state_and_health_commit_together(setup, monkeypatch):
    news, source, collector = setup
    now = stamp()
    collector.fetcher = lambda *_: {"body": b"denied", "url": source["url"], "status": 403,
        "content_type": "text/html", "headers": {}, "started": now, "first_byte": now,
        "received": now, "delivery": "http"}
    append = news.append
    def crash(kind, *args, **kwargs):
        result = append(kind, *args, **kwargs)
        if kind == "source_health":
            raise RuntimeError("crash during failure audit")
        return result
    monkeypatch.setattr(news, "append", crash)
    with pytest.raises(RuntimeError):
        collector.fetch_source(source)
    observation = news.records("observation")[0]
    assert state(news, observation["id"])["state"] == "pending"
    assert not news.records("source_health")
    monkeypatch.setattr(news, "append", append)
    collector.recover_unparsed()
    assert state(news, observation["id"])["state"] == "failed"
    assert len(news.records("source_health")) == 1
