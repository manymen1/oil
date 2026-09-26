from dataclasses import replace
from datetime import timedelta
import json

import pytest

from oilbot.clock import instant, stamp, utc_now
from oilbot.config import load_config
from oilbot.forward_quality import forward_quality
from oilbot.linking import LinkerWorker
from oilbot.operations import clock_sample, reset_source_circuit, sample_storage
from oilbot.sources import NewsCollector
from oilbot.store import Journal
from test_forward import setup, capture, warm


def response(source, status=200, body=b"<rss><channel/></rss>"):
    now = stamp()
    return {"body": body, "url": source["url"], "status": status, "content_type": "text/xml",
            "headers": {}, "started": now, "first_byte": now, "received": now, "delivery": "http"}


def test_quality_metrics_have_explicit_denominators_and_do_not_mutate(setup, monkeypatch):
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    warm(news, src)
    capture(news, src, "IRGC says tanker ALPHA hit in Hormuz", native="a")
    capture(news, src, "IRGC says tanker ALPHA hit in Hormuz", native="a")  # Unchanged receipt.
    capture(news, src, "IRGC says tanker ALPHA attacked in Hormuz", native="b")
    worker.run_once()
    LinkerWorker(news, output).run_once()
    capture(news, src, "Withdrawn", native="a", status="withdrawal")
    worker.run_once()
    before = (news.records(), output.records())
    # Report JSON decoding must never encounter captured response bodies.
    loads = json.loads
    def metadata_only(value, *a, **kw):
        assert "body_b64" not in value
        return loads(value, *a, **kw)
    with monkeypatch.context() as m:
        m.setattr(json, "loads", metadata_only)
        report = forward_quality(cfg)
    assert (news.records(), output.records()) == before
    assert report["totals"] == {"stories_captured": 3, "story_revisions": 4, "fast_events": 2}
    w = report["window"]
    assert sum(w["new_stories_by_utc_day"].values()) == 3
    assert sum(w["fast_events_by_utc_day"].values()) == 2
    assert w["revision_rate"] == {"numerator": 1, "denominator": 4, "fraction": .25}
    assert w["duplicate_receipt_rate"] == {"numerator": 1, "denominator": 5, "fraction": .2}
    assert w["candidate_link_rate"] == {"numerator": 1, "denominator": 2, "fraction": .5}
    assert w["excluded_initial_snapshots"] == 1
    assert w["publication_to_receipt"]["missing"] == 4
    assert w["receipt_to_classification"]["samples"] == 2
    assert w["evidence_transitions"]["WITHDRAWN"] == 1
    assert report["clock"]["absolute_accuracy"] == "NOT_MEASURED"


def test_missing_storage_is_unknown_not_created(tmp_path):
    cfg = replace(load_config("configs/forward.yaml"), root=tmp_path / "absent")
    report = forward_quality(cfg)
    assert not cfg.root.exists()
    assert all(v is None for v in report["journal_max_seq"].values())
    assert report["window"]["revision_rate"]["fraction"] is None
    assert report["window"]["receipt_to_classification"]["median"] is None
    assert report["storage"]["window_growth"] is None
    assert report["source_states"] == {"NEVER_SUCCEEDED": 6}
    for value in (0, -1, True):
        with pytest.raises(ValueError):
            forward_quality(cfg, window_seconds=value)


def test_bad_publication_times_not_clamped_into_valid_samples(setup):
    cfg, news, output, worker = setup
    row = capture(news, cfg.sources[0], "Baseline")
    for published in ("bad timestamp", (instant(utc_now()) + timedelta(days=1)).isoformat()):
        news.append("story_revision", {**row["payload"], "published_at": published})
    lag = forward_quality(cfg)["window"]["publication_to_receipt"]
    assert lag["samples"] == 0 and lag["median"] is None
    assert (lag["missing"], lag["invalid"], lag["negative"]) == (1, 1, 1)


def test_window_uses_availability_and_no_zero_fill_for_unknown_days(setup):
    cfg, news, output, worker = setup
    warm(news, cfg.sources[0])
    now = (instant(utc_now()) + timedelta(days=2)).isoformat()
    report = forward_quality(cfg, now=now, window_seconds=60)
    assert report["totals"]["story_revisions"] == 1
    assert report["window"]["new_stories_by_utc_day"] == {}
    assert report["source_states"].get("STALE") == 1


@pytest.mark.parametrize("status,attempts", [(401, 1), (403, 1), (200, 3)])
def test_circuits_persist_and_require_explicit_reset(setup, status, attempts):
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    calls = []
    def fetch(*_):
        calls.append(1)
        return response(src, status, body=b"<not-xml")
    collector = NewsCollector(news, [src], fetch)
    for n in range(attempts):
        assert collector.poll_once(force=True)["errors"] == 1
        assert bool(news.cursor("source:" + src["id"]).get("circuit")) == (n == attempts - 1)
    reopened = NewsCollector(Journal(news.path), [src], fetch)
    reopened.poll_once(force=True)
    reopened.fetch_source(src)
    assert len(calls) == attempts
    assert len(news.records("source_circuit_transition")) == 1
    assert forward_quality(cfg)["sources"][0]["state"] == "CIRCUIT_OPEN"
    with pytest.raises(ValueError):
        reset_source_circuit(news, src["id"], " ")
    reset_source_circuit(news, src["id"], "Parser/access policy reviewed")
    assert len(calls) == attempts  # Reset never makes a request.
    cursor = news.cursor("source:" + src["id"])
    assert not any(k in cursor for k in ("etag", "last_modified", "circuit", "next_poll"))
    reopened.poll_once(force=True)
    assert len(calls) == attempts + 1


def test_parse_streak_resets_on_success_and_http_errors_do_not_open_it(setup):
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    collector = NewsCollector(news, [src], lambda *_: response(src, body=b"broken"))
    for _ in range(2):
        collector.poll_once(force=True)
    collector.fetcher = lambda *_: response(src)
    collector.poll_once(force=True)
    assert news.cursor("source:" + src["id"])["parse_failures"] == 0
    collector.fetcher = lambda *_: response(src, 503)
    for _ in range(4):
        collector.poll_once(force=True)
    assert not news.cursor("source:" + src["id"]).get("circuit")


def test_registration_change_records_circuit_transition(setup):
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    NewsCollector(news, [src], lambda *_: response(src, 403)).poll_once(force=True)
    updated = {**src, "url": src["url"] + "?revised=1"}
    NewsCollector(news, [updated], lambda *_: response(updated)).poll_once(force=True)
    assert [r["payload"]["state"] for r in news.records("source_circuit_transition")] == ["OPEN", "POLICY_CHANGED"]
    assert not news.cursor("source:" + src["id"]).get("circuit")


def test_clock_samples_do_not_claim_absolute_accuracy():
    previous = {"utc": "2026-09-26T00:00:00Z", "monotonic_ns": 10**9, "boot_id": "b", "host_id": "h"}
    current = {**previous, "utc": "2026-09-26T00:00:10Z", "monotonic_ns": 11 * 10**9}
    assert clock_sample("news", None, current)["state"] == "BASELINE"
    assert clock_sample("news", previous, current)["state"] == "NO_STEP_DETECTED"
    assert clock_sample("news", previous, {**current, "monotonic_ns": 0})["state"] == "MONOTONIC_REGRESSION"
    assert clock_sample("news", previous, {**current, "boot_id": "new"})["state"] == "HOST_OR_BOOT_CHANGED"
    anomaly = clock_sample("news", previous, {**current, "utc": "2026-09-25T23:59:00Z"})
    assert anomaly["state"] == "WALL_MONOTONIC_DIVERGENCE"
    assert anomaly["clock_uncertainty_ms"] is None and anomaly["absolute_accuracy"] == "NOT_MEASURED"


def test_storage_samples_are_bounded_and_growth_is_net(setup):
    cfg, news, output, worker = setup
    runtime = Journal(cfg.db("runtime"))
    sample_storage(runtime, cfg)
    sample_storage(runtime, cfg)
    assert len(runtime.records("storage_sample")) == 1
    sample_storage(runtime, cfg, interval_seconds=0)
    report = forward_quality(cfg)
    assert report["storage"]["total_bytes"] > 0
    assert report["storage"]["window_growth"]["elapsed_seconds"] > 0


def test_quality_cli_default_is_read_only(setup, monkeypatch, capsys):
    import oilbot.cli as cli
    cfg, news, output, worker = setup
    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    before = output.records()
    assert cli.main(["forward-quality"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "forward-quality-v1"
    assert output.records() == before


def test_slow_work_and_clock_step_inside_work_are_recorded(setup, monkeypatch):
    import oilbot.cli as cli
    cfg, news, output, worker = setup
    start = stamp()
    finish = {**start, "utc": (instant(start["utc"]) + timedelta(seconds=200)).isoformat(),
              "monotonic_ns": start["monotonic_ns"] + 2 * 10**9}
    stamps = iter([start, start, finish, finish])
    monkeypatch.setattr(cli, "stamp", lambda: next(stamps))
    cli.record(cfg, "linker", once=True, fixture=None)
    runtime = Journal(cfg.db("runtime"))
    assert runtime.records("runtime_gap")[0]["payload"]["reason"] == "WORK_OR_HEARTBEAT_DELAY"
    assert runtime.records("clock_health")[-1]["payload"]["state"] == "WALL_MONOTONIC_DIVERGENCE"


def test_circuit_transition_and_cursor_are_atomic(setup, monkeypatch):
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    append = news.append
    def fail(kind, *args, **kwargs):
        result = append(kind, *args, **kwargs)
        if kind == "source_circuit_transition":
            raise RuntimeError("interrupted")
        return result
    monkeypatch.setattr(news, "append", fail)
    with pytest.raises(RuntimeError):
        NewsCollector(news, [src], lambda *_: response(src, 403)).fetch_source(src)
    assert not news.records("source_circuit_transition")
    assert not news.cursor("source:" + src["id"], {}).get("circuit")
    assert len(news.records("observation")) == 1  # Raw capture survives.
