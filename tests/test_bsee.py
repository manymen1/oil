import json
from pathlib import Path

import pytest
import yaml

from oilbot.bsee import BSEEAdapter, bsee_report, parse_bsee, valid_report_url
from oilbot.cli import main
from oilbot.clock import stamp
from oilbot.config import load_config
from oilbot.sources import NewsCollector, ParseFailure, adapter
from oilbot.store import Journal

AT = "2026-10-01T12:00:00Z"
URL = "https://www.bsee.gov/newsroom/latest-news/statements-and-releases/press-releases/synthetic-report"


def fixture():
    # Invented engineering evidence, not an archived release.
    return b'''<h1>BSEE Monitors Gulf of Mexico Following Hurricane Synthetic</h1><article>
    <time itemprop="datePublished" datetime="2026-10-01"></time>
    <p>From operator reports, estimates are based on expected production as of 11:30 CDT today.</p>
    <p>Production will be brought back online after inspections. Rigs have returned.</p>
    <table><tr><th></th><th>Total shut-in</th><th>Percentage of GOM Production</th></tr>
    <tr><td>Oil, BOPD<br>Shut-in</td><td>100,000</td><td>10.0</td></tr>
    <tr><td>Gas,<br>MMCFD Shut-in</td><td>200.5</td><td>20.0</td></tr></table></article>'''


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    current = [AT]
    for module in ("oilbot.bsee", "oilbot.store", "oilbot.sources"):
        monkeypatch.setattr(module + ".utc_now", lambda: current[0])
    return current


@pytest.fixture
def config(tmp_path):
    raw = yaml.safe_load(Path("configs/bsee-report.yaml").read_text())
    raw["storage"]["root"] = str(tmp_path / "capture")
    raw["sources"][0]["url"] = URL
    path = tmp_path / "bsee.yaml"; path.write_text(yaml.safe_dump(raw))
    return load_config(path)


def capture(config, clock, body, *, parse=True, status=200):
    store = Journal(config.db("news")); now = {**stamp(), "utc": clock[0]}
    oid = store.capture(config.sources[0], {"url": URL, "body": body, "status": status,
        "content_type": "text/html", "headers": {}, "started": now, "received": now, "delivery": "http"})
    if parse:
        items = BSEEAdapter(URL).parse(body, URL, "text/html") if status == 200 else []
        store.accept_items(config.sources[0], oid, items, {})
    return store, oid


def test_units_evidence_and_future_restart_not_confirmed():
    p = parse_bsee(fixture())
    assert p["facts"]["oil"]["estimated_shut_in"] == "100000"
    assert p["facts"]["oil"]["units"] == "barrels_per_day"
    assert p["facts"]["gas"]["units"] == "million_cubic_feet_per_day"
    assert p["facts"]["gas"]["estimated_shut_in"] == "200.5"
    assert p["published_at"] is None and p["report_date"] == "2026-10-01"
    assert not p["confirmed_restoration"] and p["restored_oil_bpd"] is None and not p["trade_authorized"]
    item = BSEEAdapter(URL).parse(fixture(), URL, "text/html")[0]
    assert item.origin == "bsee" and not item.links and item.published_at is None


@pytest.mark.parametrize("before,after", [(b"100,000", b"NaN"), (b"100,000", b"100000.2"),
    (b"100,000", b"-1"), (b"100,000", b"0"), (b"10.0", b"101"), (b"200.5", b"--"),
    (b"BOPD", b"thousand BOPD"), (b"Total shut-in", b"Total output"),
    (b"operator reports", b"commentators"), (b"2026-10-01", b"2026-02-31"),
    (b"Gulf of Mexico", b"Pacific Ocean")])
def test_missing_or_drifted_data_fail_closed(before, after):
    with pytest.raises(ParseFailure):
        parse_bsee(fixture().replace(before, after))


def test_duplicate_and_zero_estimates():
    row = b'<tr><td>Oil, BOPD Shut-in</td><td>100,000</td><td>10.0</td></tr>'
    with pytest.raises(ParseFailure, match="duplicate"):
        parse_bsee(fixture().replace(b"</table>", row + b"</table>"))
    p = parse_bsee(fixture().replace(b"100,000", b"0").replace(b"10.0", b"0"))
    assert p["facts"]["oil"]["estimated_shut_in"] == "0" and not p["confirmed_restoration"]


@pytest.mark.parametrize("url", ["http://www.bsee.gov/newsroom/release", URL + "?x=y", URL + "#fragment",
    URL.replace("www.bsee.gov", "evil.example"), URL.replace("https://", "https://a:b@"), URL + "/../private"])
def test_urls_fail_closed(url):
    assert not valid_report_url(url)
    with pytest.raises(ParseFailure):
        BSEEAdapter(url)


def test_exact_report_and_html_only(config):
    with pytest.raises(ParseFailure):
        adapter(config.sources[0]).parse(fixture(), URL + "-other", "text/html")
    with pytest.raises(ParseFailure):
        adapter(config.sources[0]).parse(fixture(), URL, "application/json")


def test_archive_missing_date_not_inferred_from_capture(config, clock):
    body = fixture().replace(b'<time itemprop="datePublished" datetime="2026-10-01"></time>', b"ARCHIVED content NOT UPDATED")
    capture(config, clock, body)
    p = bsee_report(config, at=AT)["reports"][0]
    assert p["observation"]["report_date"] is None
    assert set(p["issues"]) == {"ARCHIVED_REPORT_NOT_CURRENT", "REPORT_DATE_UNVERIFIED"}
    assert p["status"] == "REVIEW_REQUIRED"


def test_parse_availability_revisions_and_decline_not_restoration(config, clock):
    store, oid = capture(config, clock, fixture(), parse=False)
    clock[0] = "2026-10-01T12:01:00Z"
    store.accept_items(config.sources[0], oid, BSEEAdapter(URL).parse(fixture(), URL, "text/html"), {})
    assert bsee_report(config, at=AT)["reports"][0]["observation"] is None
    store, _ = capture(config, clock, fixture())
    assert len(store.records("story_revision")) == 1
    clock[0] = "2026-10-01T12:02:00Z"
    store, _ = capture(config, clock, fixture().replace(b"100,000", b"90,000").replace(b"10.0", b"9.0"))
    assert len(store.records("story_revision")) == 2
    p = bsee_report(config, at=clock[0])
    assert p["reports"][0]["observation"]["facts"]["oil"]["estimated_shut_in"] == "90000"
    assert not p["confirmed_restoration"]
    assert p["reports"][0]["observation"]["restored_oil_bpd"] is None


def test_failed_capture_and_304_do_not_hide_report_age(config, clock):
    capture(config, clock, fixture())
    clock[0] = "2026-10-01T12:01:00Z"
    capture(config, clock, b"broken", parse=False)
    assert "NEWER_FAILED_OR_UNPARSED_CAPTURE" in bsee_report(config, at=clock[0])["reports"][0]["issues"]
    clock[0] = "2026-10-05T12:00:00Z"
    capture(config, clock, b"", status=304)
    assert "REPORT_DATE_FUTURE_OR_STALE" in bsee_report(config, at=clock[0])["reports"][0]["issues"]


def test_tampered_raw_rejected(config, clock):
    store, oid = capture(config, clock, fixture())
    with store.connect() as db:
        db.execute("DROP TRIGGER immutable_update")
        db.execute("UPDATE records SET payload=json_set(payload,'$.sha256','bad') WHERE id=?", (oid,))
    with pytest.raises(ValueError, match="hash"):
        bsee_report(config, at=AT)


def test_config_model_and_interval_guards(config):
    for key, value in (("poll_seconds", 60), ("parser_contract", "unknown"), ("allowed_hosts", ["evil.example"])):
        raw = yaml.safe_load(config.path.read_text()); raw["sources"][0][key] = value
        config.path.write_text(yaml.safe_dump(raw))
        with pytest.raises(ValueError): load_config(config.path)


def test_readonly_missing_journal_and_cli_report(config, clock, tmp_path):
    assert main(["bsee-report", "--config", str(config.path), "--at", AT]) == 2
    assert not config.db("news").exists()
    capture(config, clock, fixture())
    out = tmp_path / "report.json"
    args = ["bsee-report", "--config", str(config.path), "--at", AT, "--out", str(out)]
    assert main(args) == 0
    assert not json.loads(out.read_text())["trade_authorized"]
    old = out.read_bytes()
    assert main(args) == 2 and out.read_bytes() == old
