from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest

from oilbot.cli import main, preflight, record
from oilbot.clock import instant, stamp
from oilbot.config import load_config
from oilbot.forward import ForwardRecorder, RULES, classify
from oilbot.schema import NewsItem
from oilbot.sources import HTTPFetcher, ListingAdapter, RSSAdapter
from oilbot.store import Journal


@pytest.fixture
def setup(tmp_path):
    cfg = replace(load_config("configs/forward.yaml"), root=tmp_path)
    news, output = Journal(cfg.db("news")), Journal(cfg.db("forward"))
    worker = ForwardRecorder(news, output, cfg.raw["assets"])
    return cfg, news, output, worker


def capture(news, src, headline, native="1", **options):
    now = stamp()
    response = {"url": src["url"], "status": 200, "content_type": "text/xml", "headers": {},
                "started": now, "first_byte": now, "received": now, "body": headline.encode(),
                "delivery": options.pop("delivery", "http"), "synthetic": options.pop("synthetic", False)}
    oid = news.capture(src, response)
    item = NewsItem(native, src["url"], headline, headline, **options)
    ids = news.accept_items(src, oid, [item], {})
    return news.get(ids[0]) if ids else None


def warm(news, src):
    capture(news, src, "Baseline", native="baseline")


def test_receipts_survive_and_publication_never_becomes_availability(setup):
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    warm(news, src)
    row = capture(news, src, "IRGC says tanker hit in Hormuz", published_at=stamp()["utc"])
    assert row["payload"]["request_started_at"]
    assert row["payload"]["first_byte_at"]
    assert row["payload"]["source_type"] == "regional_news"
    assert worker.run_once()["events"] == 1
    event = output.records("fast_event")[0]
    p = event["payload"]
    assert p["received_at"] == row["payload"]["local_received_at"]
    assert instant(event["available_at"]) >= instant(p["received_at"])
    assert p["publisher"] == "al_jazeera" and p["claim_origin"] == "irgc"
    assert p["geography"] == ["hormuz"] and p["confirmation"] == "UNVERIFIED"
    assert p["state"] == "OFFICIAL_CLAIM"
    assert p["review_required"]


def test_multiple_publishers_are_not_independent_confirmations(setup):
    cfg, news, output, worker = setup
    for src in cfg.sources[:3]:
        warm(news, src)
        capture(news, src, "IRGC says tanker hit in Hormuz")
    worker.run_once()
    events = [r["payload"] for r in output.records("fast_event")]
    assert len({e["incident_id"] for e in events}) == 1
    assert [e["novelty"] for e in events] == [True, False, False]
    latest = output.records("forward_incident_revision")[-1]["payload"]
    assert len(latest["publishers"]) == 3
    assert latest["independent_confirmation"] == "NONE"
    assert latest["state"] == "OFFICIAL_CLAIM"


def test_geography_does_not_merge_unrelated_tankers(setup):
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    warm(news, src)
    capture(news, src, "Tanker Alpha hit in Hormuz", native="a")
    capture(news, src, "Tanker Beta hit in Hormuz", native="b")
    worker.run_once()
    assert len({r["payload"]["incident_id"] for r in output.records("fast_event")}) == 2


@pytest.mark.parametrize("title", ["IRGC says tanker not hit in Hormuz", "Could tanker be hit in Hormuz?",
    "No tanker hit in Hormuz", "IRGC denies tanker attacked in Hormuz", "Last year tanker attacked in Hormuz"])
def test_negated_uncertain_historical_headlines_need_review(title):
    events = classify({"title": title, "source_id": "test"}, [])
    assert events and all(e["state"] == "REVIEW_REQUIRED" for e in events)


@pytest.mark.parametrize("options,reason", [
    ({"delivery": "local_import"}, "NOT_LIVE_HTTP"),
    ({"synthetic": True}, "NOT_LIVE_HTTP"),
    ({"published_at": "2000-01-01T00:00:00Z"}, "OLD_PUBLICATION_FIRST_SEEN"),
    ({"status": "withdrawal"}, "REVISION_REQUIRES_REVIEW"),
])
def test_nonforward_inputs_excluded(setup, options, reason):
    cfg, news, output, worker = setup
    warm(news, cfg.sources[0])
    capture(news, cfg.sources[0], "Tanker attacked in Hormuz", **options)
    assert worker.run_once()["events"] == 0
    assert output.records("forward_processing")[-1]["payload"]["exclusion"] == reason


def test_startup_snapshot_excluded_restart_idempotent(setup):
    cfg, news, output, worker = setup
    capture(news, cfg.sources[0], "Tanker attacked in Hormuz")
    worker.run_once()
    assert not output.records("fast_event")
    capture(news, cfg.sources[0], "IRGC says tanker hit in Hormuz", native="new")
    worker.run_once()
    again = ForwardRecorder(news, output, cfg.raw["assets"])
    assert again.epoch == worker.epoch
    assert again.run_once()["processed"] == 0
    assert len(output.records("fast_event")) == 1
    assert len(output.records("forward_start")) == 1


def test_preexisting_journal_not_reconstructed_as_forward(tmp_path):
    cfg = load_config("configs/forward.yaml")
    news, output = Journal(tmp_path / "news"), Journal(tmp_path / "forward")
    warm(news, cfg.sources[0])
    capture(news, cfg.sources[0], "Tanker hit in Hormuz")
    worker = ForwardRecorder(news, output, [])
    assert worker.run_once()["events"] == 0
    assert all(r["payload"]["exclusion"] == "PRE_FORWARD_START" for r in output.records("forward_processing"))


def test_revision_notices_preserve_withdrawal_lineage(setup):
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    warm(news, src)
    original = capture(news, src, "IRGC says tanker hit in Hormuz")
    worker.run_once()
    withdrawn = capture(news, src, "Story withdrawn", status="withdrawal")
    worker.run_once()
    notice = output.records("forward_revision_notice")[0]["payload"]
    assert notice["input_revision_ids"] == [withdrawn["id"], original["id"]]
    assert len(output.records("fast_event")) == 1


def test_report_id_links_updates_but_no_physical_confirmation(setup):
    cfg, news, output, worker = setup
    warm(news, cfg.sources[0])
    capture(news, cfg.sources[0], "UKMTO WARNING 134-26: tanker attacked in Hormuz", native="a")
    capture(news, cfg.sources[0], "UKMTO WARNING 134-26 Update 001: tanker hit in Hormuz", native="b")
    worker.run_once()
    rows = [r["payload"] for r in output.records("forward_incident_revision")]
    assert rows[0]["incident_id"] == rows[1]["incident_id"]
    assert rows[-1]["state"] == "REPORTED"


def test_transaction_rolls_back_outputs_and_cursor(setup, monkeypatch):
    cfg, news, output, worker = setup
    warm(news, cfg.sources[0])
    worker.run_once()
    offset = output.cursor("forward:seq:fast-event-v1")
    capture(news, cfg.sources[0], "IRGC says tanker hit in Hormuz")
    original = output.append
    def fail(kind, *args, **kwargs):
        if kind == "forward_incident_revision":
            raise RuntimeError("simulated crash")
        return original(kind, *args, **kwargs)
    monkeypatch.setattr(output, "append", fail)
    with pytest.raises(RuntimeError):
        worker.run_once()
    assert not output.records("fast_event")
    assert output.cursor("forward:seq:fast-event-v1") == offset
    monkeypatch.setattr(output, "append", original)
    assert worker.run_once()["events"] == 1


def test_processing_batch_bounded(setup):
    cfg, news, output, worker = setup
    warm(news, cfg.sources[0])
    capture(news, cfg.sources[0], "Tanker hit in Hormuz")
    assert worker.run_once(limit=1)["pending"]
    assert worker.run_once(limit=1)["events"] == 1


@pytest.mark.parametrize("kind,html,expected", [
    ("ofac", '<a href="/recent-actions/20260921">Iran designations</a><a href="/recent-actions/general-licenses">Other</a>', 1),
    ("centcom", '<a href="/MEDIA/PUBLIC-RELEASES/Article/1234/test/">Release</a><a href="/ABOUT-US/">About</a>', 1),
])
def test_official_listings_match_only_publications(kind, html, expected):
    items = ListingAdapter(kind).parse(html.encode(), "https://example.test/", "text/html")
    assert len(items) == expected and items[0].published_at is None


def test_rss_author_preserved():
    feed = b'<rss><channel><item><guid>a</guid><title>Test</title><author>Reporter</author></item></channel></rss>'
    assert RSSAdapter().parse(feed, "https://example.test", "text/xml")[0].author == "Reporter"


def test_first_byte_is_read_before_bulk_body():
    class Response:
        is_redirect = False
        headers = {"Content-Type": "text/xml"}
        status_code = 200
        raw = BytesIO(b"abcdef")
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size):
            assert self.raw.tell() == 1
            yield self.raw.read()
    response = Response()
    stream = response.raw
    class Raw:
        def read(self, n, decode_content=False):
            if n != 1:
                assert stream.tell() >= 1
            return stream.read(n)
        def tell(self): return stream.tell()
    response.raw = Raw()
    class Session:
        def get(self, *args, **kwargs): return response
    # iter_content uses .read() without arguments like an ordinary stream.
    def chunks(size):
        assert stream.tell() == 1
        yield stream.read()
    response.iter_content = chunks
    fetcher = HTTPFetcher()
    fetcher.sessions["test"] = Session()
    r = fetcher({"id": "test", "allowed_hosts": ["example.test"]}, "https://example.test", {})
    assert r["body"] == b"abcdef" and r["first_byte_basis"] == "first_decoded_body_byte"
    assert r["started"]["monotonic_ns"] <= r["first_byte"]["monotonic_ns"] <= r["received"]["monotonic_ns"]


def test_forward_cli_never_calls_model(setup, monkeypatch):
    cfg, news, output, worker = setup
    def fail(*args, **kwargs): raise AssertionError("model called")
    monkeypatch.setattr("oilbot.cli.CodexExtractor", fail)
    assert record(cfg, "forward", once=True, fixture=None)["trading"] == "disabled"
    with pytest.raises(ValueError, match="model analysis is disabled"):
        record(cfg, "analysis", once=True, fixture=None)
    assert record(cfg, "market", once=True, fixture=None)["status"] == "WAITING_FOR_QUALIFIED_LIVE_FEED"
    with pytest.raises(ValueError, match="fixtures are not allowed"):
        record(cfg, "market", once=True, fixture=Path("tests/fixtures/market.json"))


def test_all_event_classes_registered():
    from oilbot.provenance import EVENT_TYPES
    assert set(RULES) <= EVENT_TYPES


@pytest.mark.parametrize("event_type,title", [
    ("TANKER_ATTACK", "Tanker attacked in Hormuz"),
    ("TANKER_SEIZURE", "Tanker seized in Hormuz"),
    ("MILITARY_STRIKE", "Military strike in Iran"),
    ("MISSILE_ATTACK", "Missile attack in Iran"),
    ("DRONE_ATTACK", "Drone attack in Iran"),
    ("SHIPPING_RESTRICTION", "Shipping suspended in Hormuz"),
    ("SHIPPING_RESTORED", "Shipping resumed in Hormuz"),
    ("PRODUCTION_SUSPENDED", "Oil production suspended"),
    ("PRODUCTION_RESTORED", "Oil production restored"),
    ("EXPORT_TERMINAL_CLOSED", "Export terminal closed"),
    ("EXPORT_TERMINAL_REOPENED", "Export terminal reopened"),
    ("PIPELINE_OUTAGE", "Pipeline outage reported"),
    ("PIPELINE_RESTORED", "Pipeline restored"),
    ("SANCTIONS_TIGHTENED", "US imposes new sanctions"),
    ("SANCTIONS_EASED", "US lifts sanctions"),
    ("OPEC_OUTPUT_CUT", "OPEC cuts output"),
    ("OPEC_OUTPUT_INCREASE", "OPEC increases output"),
    ("CEASEFIRE_REACHED", "Ceasefire reached"),
    ("CEASEFIRE_BROKEN", "Ceasefire broken"),
    ("NEGOTIATIONS_STARTED", "Negotiations started"),
    ("NEGOTIATIONS_COLLAPSED", "Negotiations collapsed"),
])
def test_event_rule_positive_examples(event_type, title):
    assert event_type in {e["event_type"] for e in classify({"title": title, "source_id": "test"}, [])}


def test_forward_config_is_isolated():
    forward = load_config("configs/forward.yaml")
    assert forward.root != load_config("configs/observe.yaml").root
    assert forward.raw["pipeline"] == "forward"
    readiness = preflight(forward)
    assert readiness["planned_market_provider"] == "ibkr"
    assert "SOURCE_MODEL_RIGHTS_REQUIRE_QUALIFICATION" not in readiness["blockers"]
    assert not readiness["live_market_ready"]
    assert all(s["rights"]["model_processing"] == "pending" for s in forward.sources)
    assert main(["preflight", "--config", "configs/forward.yaml"]) == 0


def test_snapshot_preserves_forward_provenance(setup, tmp_path):
    from oilbot.replay import export_manifest, load_manifest
    cfg, news, output, worker = setup
    warm(news, cfg.sources[0])
    capture(news, cfg.sources[0], "IRGC says tanker hit in Hormuz")
    worker.run_once()
    manifest, reader, market = load_manifest(export_manifest(cfg, tmp_path / "snapshot"))
    assert "forward.sqlite3" in manifest["files"]
    assert manifest["dataset_role"] == "forward_news_only"
    assert len([r for r in reader.records if r["kind"] == "fast_event"]) == 1


def test_classifier_policy_change_rejected(setup):
    cfg, news, output, worker = setup
    with pytest.raises(ValueError, match="policy changed"):
        ForwardRecorder(news, output, [])


def test_gzip_chunked_transport_does_not_mix_read_paths():
    import gzip
    from http.client import HTTPResponse
    import requests
    from urllib3.response import HTTPResponse as UrllibResponse
    body = b"<rss>Some feed content</rss>" * 1000
    compressed = gzip.compress(body)
    wire = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Encoding: gzip\r\n\r\n"
            + f"{len(compressed):x}\r\n".encode() + compressed + b"\r\n0\r\n\r\n")
    class Socket:
        def makefile(self, *args): return BytesIO(wire)
    http = HTTPResponse(Socket())
    http.begin()
    response = requests.Response()
    response.status_code = 200
    response.headers = {"Transfer-Encoding": "chunked", "Content-Encoding": "gzip", "Content-Type": "text/xml"}
    response.raw = UrllibResponse(body=http, headers=response.headers,
                                  original_response=http, preload_content=False)
    class Session:
        def get(self, *args, **kwargs): return response
    fetcher = HTTPFetcher()
    fetcher.sessions["test"] = Session()
    r = fetcher({"id": "test", "allowed_hosts": ["example.test"]}, "https://example.test", {})
    assert r["body"] == body
