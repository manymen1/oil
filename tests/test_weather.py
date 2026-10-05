from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from oilbot.clock import instant, stamp
from oilbot.config import load_config
from oilbot.schema import digest
from oilbot.sources import NewsCollector, ParseFailure, adapter
from oilbot.store import Journal
from oilbot.weather import ENDPOINT, NHCAdapter, load_regions, parse_storms, weather_report

AT = "2026-10-01T12:00:00Z"


def storm(**changes):
    # Invented engineering data, not an archived NHC advisory.
    return {"id": "al992026", "name": "Synthetic Test", "classification": "HU", "intensity": "100",
        "pressure": "960", "latitude": "27.0N", "longitude": "90.0W", "latitudeNumeric": 27.0,
        "longitudeNumeric": -90.0, "lastUpdate": "2026-10-01T09:00:00Z",
        "publicAdvisory": {"advNum": "001", "issuance": "2026-10-01T09:00:00Z",
                           "fileUpdateTime": "2026-10-01T08:40:00Z", "url": "https://www.nhc.noaa.gov/text/MIATCPAT1.shtml"},
        **changes}


def body(*storms):
    return json.dumps({"activeStorms": list(storms)}).encode()


@pytest.fixture
def clock(monkeypatch):
    current = [AT]
    for module in ("oilbot.weather", "oilbot.store", "oilbot.sources"):
        monkeypatch.setattr(module + ".utc_now", lambda: current[0])
    return current


@pytest.fixture
def config(tmp_path):
    raw = yaml.safe_load(Path("configs/weather-forward.yaml").read_text())
    raw["storage"]["root"] = str(tmp_path / "capture")
    path = tmp_path / "weather.yaml"
    path.write_text(yaml.safe_dump(raw))
    return load_config(path)


def capture(config, clock, payload, *, parse=True, status=200, delivery="http"):
    journal = Journal(config.db("news"))
    now = {**stamp(), "utc": clock[0]}
    oid = journal.capture(config.sources[0], {"body": payload, "url": ENDPOINT, "status": status,
        "content_type": "application/json", "headers": {}, "received": now, "started": now, "delivery": delivery})
    if parse:
        items = NHCAdapter().parse(payload, ENDPOINT, "application/json") if status == 200 else []
        journal.accept_items(config.sources[0], oid, items, {})
    return journal, oid


def report(config, at=AT):
    return weather_report(config, load_regions("configs/oil-weather-regions.json"), at=at)


def test_parser_preserves_numeric_provenance_without_guessing_units():
    parsed = parse_storms(body(storm()))[0]
    assert parsed["latitude"] == 27 and parsed["longitude"] == -90
    assert parsed["source_intensity"] == "100" and parsed["intensity_units"] == "unverified"
    item = NHCAdapter().parse(body(storm()), ENDPOINT, "application/json")[0]
    assert item.published_at is None and item.origin == "nhc"
    assert json.loads(item.text)["schema"] == "nhc-status-v1"
    assert not item.links  # no recursive detail/GIS requests


@pytest.mark.parametrize("changes", [{"latitudeNumeric": float("nan")}, {"longitudeNumeric": float("inf")},
    {"latitudeNumeric": True}, {"latitudeNumeric": 91}, {"longitudeNumeric": 181},
    {"latitudeNumeric": -27}, {"longitude": "90.0E"}, {"id": "unknown"},
    {"name": ""}, {"classification": "<script>"}, {"pressure": None}, {"intensity": "NaN"},
    {"lastUpdate": "2026-10-01"}, {"publicAdvisory": None}])
def test_schema_drift_and_bad_facts_fail_closed(changes):
    with pytest.raises(ParseFailure):
        parse_storms(body(storm(**changes)))


@pytest.mark.parametrize("url", ["https://evil.example/text/MIATCPAT1.shtml", "http://www.nhc.noaa.gov/text/MIATCPAT1.shtml",
    "https://www.nhc.noaa.gov/text/MIATCPAT1.shtml?redirect=evil", "https://user:password@www.nhc.noaa.gov/text/MIATCPAT1.shtml"])
def test_untrusted_links_rejected(url):
    value = storm()
    value["publicAdvisory"]["url"] = url
    with pytest.raises(ParseFailure):
        parse_storms(body(value))


def test_empty_feed_is_distinct_from_malformed_data():
    assert parse_storms(body()) == []
    for value in (b"{}", b"[]", b'{"activeStorms":[],"activeStorms":[]}', body(storm(), storm()), b"x" * (2 * 1024 * 1024 + 1)):
        with pytest.raises(ParseFailure):
            parse_storms(value)


def test_metadata_only_change_is_not_another_story(config, clock):
    journal, _ = capture(config, clock, body(storm()))
    value = storm()
    value["publicAdvisory"]["fileUpdateTime"] = "2026-10-01T08:45:00Z"
    value["forecastTrack"] = {"zipFile": "https://untrusted.example/not-fetched.zip"}
    clock[0] = "2026-10-01T12:05:00Z"
    capture(config, clock, body(value))
    assert len(journal.records("story_revision")) == 1 and len(journal.records("story_receipt")) == 1


def test_correction_supersedes_original_and_return_to_original_is_new_revision(config, clock):
    journal, _ = capture(config, clock, body(storm()))
    clock[0] = "2026-10-01T12:05:00Z"
    capture(config, clock, body(storm(intensity="105")))
    clock[0] = "2026-10-01T12:10:00Z"
    capture(config, clock, body(storm()))
    rows = journal.records("story_revision")
    assert len(rows) == 3 and rows[-1]["payload"]["supersedes_id"] == rows[-2]["id"]
    assert rows[0]["payload"]["initial_snapshot"]
    assert all(r["payload"]["model_processing"] == "prohibited" for r in rows)


def test_regional_watch_is_not_confirmed_outage_or_direction(config, clock):
    capture(config, clock, body(storm()))
    value = report(config)
    row = value["storms"][0]
    assert row["screen"] == "REGIONAL_WEATHER_WATCH"
    assert row["region_matches"] == ["northern_gulf_research_box"]
    assert not row["confirmed_disruption"] and row["affected_oil_bpd"] is None
    assert row["price_direction"] is None and not value["trade_authorized"]


def test_feed_omission_is_not_recovery_and_older_reports_stay_unchanged(config, clock):
    capture(config, clock, body(storm()))
    before = report(config)
    clock[0] = "2026-10-01T12:05:00Z"
    capture(config, clock, body())
    after = report(config, clock[0])
    assert not after["storms"] and after["not_in_latest_feed"] == ["al992026"]
    assert after["absence_meaning"] == "unknown_not_restoration"
    assert report(config) == before


def test_failed_or_unparsed_newer_snapshot_never_looks_healthy(config, clock):
    capture(config, clock, body(storm()))
    clock[0] = "2026-10-01T12:05:00Z"
    capture(config, clock, b"invalid", parse=False)
    value = report(config, clock[0])
    assert "NEWER_FAILED_OR_UNPARSED_CAPTURE" in value["issues"]
    assert value["storms"][0]["screen"] == "REVIEW_DATA_QUALITY"


def test_recovered_parse_has_later_availability(config, clock):
    journal, oid = capture(config, clock, body(storm()), parse=False)
    assert "NO_PARSED_NHC_SNAPSHOT" in report(config)["issues"]
    clock[0] = "2026-10-01T12:05:00Z"
    journal.accept_items(config.sources[0], oid, adapter(config.sources[0]).parse(body(storm()), ENDPOINT, "application/json"), {})
    assert "NO_PARSED_NHC_SNAPSHOT" in report(config)["issues"]
    assert instant(report(config, clock[0])["storms"][0]["available_at"]) == instant(clock[0])


def test_304_refreshes_transport_not_old_advisory(config, clock):
    capture(config, clock, body(storm()))
    clock[0] = "2026-10-01T23:00:00Z"
    capture(config, clock, b"", status=304)
    value = report(config, clock[0])
    assert "SOURCE_POLL_STALE_OR_MISSING" not in value["issues"]
    assert "ADVISORY_STALE" in value["storms"][0]["reason_codes"]


def test_nominal_future_issuance_not_backdated_as_publication(config, clock):
    value = storm(lastUpdate="2026-10-01T12:30:00Z")
    value["publicAdvisory"]["issuance"] = "2026-10-01T12:30:00Z"
    capture(config, clock, body(value))
    row = report(config)["storms"][0]
    assert "NOMINAL_ADVISORY_TIME_IN_FUTURE" in row["reason_codes"]
    assert instant(row["available_at"]) == instant(AT)


def test_outside_region_is_not_no_exposure(config, clock):
    capture(config, clock, body(storm(latitude="19.4N", longitude="110.4W", latitudeNumeric=19.4, longitudeNumeric=-110.4)))
    value = report(config)
    assert value["storms"][0]["screen"] == "OUTSIDE_SCREENED_REGIONS"
    assert any("does not mean no oil exposure" in s for s in value["limitations"])


def test_capture_hash_binding_and_import_exclusion(config, clock):
    journal, oid = capture(config, clock, body(storm()), delivery="local_import")
    assert "LOCAL_IMPORT_NOT_PROSPECTIVE" in report(config)["issues"]
    # A second immutable observation with a false declared hash is rejected.
    original = journal.get(oid)["payload"]
    clock[0] = "2026-10-01T12:05:00Z"
    bad = journal.append("observation", {**original, "sha256": "0" * 64})
    journal.accept_items(config.sources[0], bad, [], {})
    with pytest.raises(ValueError, match="hash mismatch"):
        report(config, clock[0])


def test_collector_integration_raw_first_and_access_denial(config, clock):
    journal = Journal(config.db("news"))
    def fetch(source, url, cursor):
        now = {**stamp(), "utc": clock[0]}
        return {"body": body(storm()), "url": url, "status": 200, "content_type": "application/json",
                "headers": {}, "received": now, "started": now, "delivery": "http"}
    worker = NewsCollector(journal, config.sources, fetcher=fetch)
    assert worker.poll_once()["revisions"] == 1
    assert journal.records("observation")[0]["seq"] < journal.records("story_revision")[0]["seq"]
    worker.fetcher = lambda *a: {**fetch(*a), "body": b"denied", "status": 403}
    clock[0] = "2026-10-01T12:05:00Z"
    assert worker.poll_once()["errors"] == 1
    clock[0] = "2026-10-01T13:05:00Z"
    assert worker.poll_once()["responses"] == 0
    assert "LATEST_SOURCE_HEALTH_FAILURE" in report(config, clock[0])["issues"]


def test_cli_report_no_overwrite_or_broker_work(config, clock, tmp_path, capsys):
    from oilbot.cli import main
    capture(config, clock, body(storm()))
    args = ["weather-report", "--config", str(config.path), "--at", AT, "--out", str(tmp_path / "report.json")]
    assert main(args) == 0
    assert not json.loads(capsys.readouterr().out)["trade_authorized"]
    assert main(args) == 2


def test_source_and_region_configuration_guards(config, tmp_path):
    raw = deepcopy(config.raw)
    raw["sources"][0]["poll_seconds"] = 60
    config.path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="300-second"):
        load_config(config.path)
    regions = json.loads(Path("configs/oil-weather-regions.json").read_text())
    regions["regions"][0]["north"] = 100
    path = tmp_path / "regions.json"
    path.write_text(json.dumps(regions))
    with pytest.raises(ValueError, match="bounding box"):
        load_regions(path)
