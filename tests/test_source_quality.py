from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest
import yaml

from oilbot.cli import main
from oilbot.clock import epoch_ns, iso_ns
from oilbot.config import load_config
from oilbot.provenance import load_profiles
from oilbot.schema import digest
from oilbot.source_quality import (collection_health, qualify_sources, load_qualification,
                                   read_evidence, REVIEW_FIELDS)
from oilbot.store import Journal


NOW = "2026-09-21T12:00:00Z"


def at(offset):
    return iso_ns(epoch_ns(NOW) + int(offset * 1e9))


@pytest.fixture
def config(tmp_path):
    raw = yaml.safe_load(Path("configs/observe.yaml").read_text())
    raw["storage"]["root"] = str(tmp_path / "runtime")
    raw["sources"] = [raw["sources"][0]]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return load_config(path)


@pytest.fixture
def profiles():
    return load_profiles("configs/source-profiles.json")


def poll(news, source, offset=-10, *, status=200, parsed=True, item=True,
         health="OK", local=False, synthetic=False, policy=None, parse_delay=.1):
    oid = news.append("observation", {"source_id": source["id"], "source_policy": policy or digest(source),
        "url": source["url"], "requested_url": source["url"], "status": status,
        "delivery": "local_import" if local else "http", "synthetic": synthetic}, available_at=at(offset))
    ids = []
    if parsed:
        if item:
            ids.append(news.append("story_revision", {"source_id": source["id"], "story_id": oid,
                "observed_at": at(offset), "published_at": at(offset - 5), "status": "update",
                "input_revision_ids": [oid]}, available_at=at(offset + parse_delay / 2)))
        news.append("parse_receipt", {"input_revision_ids": [oid], "revision_ids": ids}, available_at=at(offset + parse_delay))
    news.append("source_health", {"source_id": source["id"], "source_policy": policy or digest(source),
        "status": health, "scope": "collection", "input_revision_ids": [oid]}, available_at=at(offset + .5))
    return oid


def review(source, profile):
    return {"source": source["id"], "source_policy_hash": digest(source), "profile_hash": digest(asdict(profile)),
            "reviewed_at": at(-3600), "expires_at": at(3600), "reviewer": "synthetic reviewer",
            "checks": {key: {"status": "permitted" if key in {"capture", "model_processing"} else "verified",
                             "evidence": "synthetic test evidence, not a real permission grant"} for key in REVIEW_FIELDS}}


def qualification(config, profiles, reviews=None):
    return qualify_sources(config, profiles, ["aramco"], reviews or {}, now=NOW)["sources"][0]


def test_reports_do_not_create_storage_or_activate_sources(config, profiles):
    before = config.path.read_bytes()
    health = collection_health(config, now=NOW)
    assert health["storage"] == "MISSING" and health["sources"][0]["state"] == "NEVER_SUCCEEDED"
    q = qualification(config, profiles)
    assert q["status"] == "BLOCKED" and not q["model_checks_passed"]
    assert not config.root.exists() and config.path.read_bytes() == before


def test_healthy_collection_is_not_rights_or_identity_qualification(config, profiles):
    news = Journal(config.db("news"))
    poll(news, config.sources[0])
    health = collection_health(config, now=NOW)["sources"][0]
    assert health["state"] == "HEALTHY"
    assert health["window"]["capture_to_parse_ms"]["median"] == pytest.approx(100)
    assert health["window"]["publication_to_receipt_seconds"]["median"] == 5
    q = qualification(config, profiles)
    assert q["checks"]["parsed_nonempty_sample"]
    assert "CAPTURE_REQUIRED" in q["blockers"] and "IDENTITY_REQUIRED" in q["blockers"]


@pytest.mark.parametrize("local,synthetic", [(True, False), (False, True)])
def test_local_imports_and_fixtures_never_establish_live_health(config, profiles, local, synthetic):
    poll(Journal(config.db("news")), config.sources[0], local=local, synthetic=synthetic)
    health = collection_health(config, now=NOW)["sources"][0]
    assert health["state"] == "NEVER_SUCCEEDED"
    assert health["window"]["transport_responses"] == 0
    assert health["window"]["local_or_fixture_revisions"] == 1
    assert not qualification(config, profiles)["checks"]["recent_endpoint_response"]


def test_stale_success_backoff_and_network_failure_are_visible(config, profiles):
    news, src = Journal(config.db("news")), config.sources[0]
    poll(news, src, offset=-1000)
    assert collection_health(config, now=NOW)["sources"][0]["state"] == "STALE"
    news.append("source_health", {"source_id": "aramco", "scope": "collection", "source_policy": digest(src),
        "status": "ConnectionError", "input_revision_ids": []}, available_at=at(-5))
    with news.transaction() as db:
        news.set_cursor(db, "source:aramco", {"failures": 1, "next_poll": at(90)})
    row = collection_health(config, now=NOW)["sources"][0]
    assert row["state"] == "FAILING" and row["backoff_active"]
    assert row["consecutive_failed_health_events"] == 1
    assert "LATEST_COLLECTION_OK_REQUIRED" in qualification(config, profiles)["blockers"]


def test_304_does_not_establish_parser_and_parse_failure_is_not_healthy(config, profiles):
    news, src = Journal(config.db("news")), config.sources[0]
    poll(news, src, status=304, item=False, health="UNCHANGED")
    assert not qualification(config, profiles)["checks"]["parsed_nonempty_sample"]
    poll(news, src, offset=-4, parsed=False, health="invalid RSS/Atom")
    row = collection_health(config, now=NOW)["sources"][0]
    assert row["state"] == "FAILING" and "UNPARSED_RESPONSE" in row["warnings"]
    assert not qualification(config, profiles)["checks"]["latest_response_parsed"]


def test_current_review_and_real_parser_evidence_pass_capture_only(config, profiles):
    news, src = Journal(config.db("news")), config.sources[0]
    poll(news, src)
    reviews = {"aramco": review(src, profiles["aramco"])}
    q = qualification(config, profiles, reviews)
    assert q["capture_checks_passed"] and not q["model_checks_passed"]
    assert q["independence_group"] is None  # No outlet-name independence assumption.
    src["rights"]["model_processing"] = "permitted"
    profiles["aramco"] = replace(profiles["aramco"], model_processing="permitted")
    reviews = {"aramco": review(src, profiles["aramco"])}
    poll(news, src, offset=-3)
    assert qualification(config, profiles, reviews)["model_checks_passed"]


@pytest.mark.parametrize("change", ["endpoint", "adapter", "expired", "future", "profile"])
def test_changed_policies_or_invalid_review_dates_fail_closed(config, profiles, change):
    src = config.sources[0]
    poll(Journal(config.db("news")), src)
    r = review(src, profiles["aramco"])
    if change == "endpoint":
        src["url"] += "&updated=yes"
    elif change == "adapter":
        src["adapter"] = "structured"
    elif change == "expired":
        r["expires_at"] = at(-1)
    elif change == "future":
        r["reviewed_at"] = at(1)
    else:
        profiles["aramco"] = replace(profiles["aramco"], geography="changed")
    assert not qualification(config, profiles, {"aramco": r})["capture_checks_passed"]


def test_duplicate_receipts_remain_usable_parser_evidence(config, profiles):
    news, src = Journal(config.db("news")), config.sources[0]
    poll(news, src, offset=-172800)
    old_story = news.records("story_revision")[0]["id"]
    oid = poll(news, src, item=False)
    news.append("story_receipt", {"source_id": src["id"], "input_revision_ids": [oid, old_story]}, available_at=at(-9))
    assert qualification(config, profiles)["checks"]["parsed_nonempty_sample"]


def test_detail_failures_degrade_collection(config):
    news, src = Journal(config.db("news")), config.sources[0]
    poll(news, src, health="OK_DETAIL_INCOMPLETE")
    news.append("source_health", {"source_id": src["id"], "scope": "detail", "status": "DETAIL_FAILED",
        "input_revision_ids": []}, available_at=at(-9))
    row = collection_health(config, now=NOW)["sources"][0]
    assert row["state"] == "DEGRADED" and row["window"]["detail_failures"] == 1


def test_window_does_not_hide_staleness_and_disabled_sources_are_distinct(config):
    poll(Journal(config.db("news")), config.sources[0], offset=-1000)
    row = collection_health(config, now=NOW, window_seconds=60)["sources"][0]
    assert row["state"] == "STALE" and row["window"]["transport_responses"] == 0
    config.sources[0]["enabled"] = False
    assert collection_health(config, now=NOW)["sources"][0]["state"] == "DISABLED"


def test_review_evidence_and_schema_validation(config, profiles, tmp_path):
    path = tmp_path / "reviews.json"
    r = review(config.sources[0], profiles["aramco"])
    value = {"schema": "source-qualification-v1", "candidates": ["aramco"], "reviews": [r]}
    path.write_text(json.dumps(value))
    assert load_qualification(path)[1]["aramco"] == r
    r["checks"]["capture"]["evidence"] = ""
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="evidence"):
        load_qualification(path)


def test_read_only_snapshot_preserves_records_and_cursors(config):
    news = Journal(config.db("news"))
    poll(news, config.sources[0])
    before = read_evidence(news.path)
    collection_health(config, now=NOW)
    assert read_evidence(news.path) == before


def test_missing_invalid_future_timestamps_and_response_cadence(config):
    news, src = Journal(config.db("news")), config.sources[0]
    first = poll(news, src, offset=-100)
    poll(news, src, offset=-40)
    for i, published in enumerate((None, "not-a-time", at(10))):
        news.append("story_revision", {"source_id": src["id"], "published_at": published,
            "observed_at": at(-100), "status": "update", "input_revision_ids": [first]}, available_at=at(-99 + i))
    window = collection_health(config, now=NOW)["sources"][0]["window"]
    assert window["missing_publication_times"] == 1
    assert window["invalid_publication_times"] == 1
    assert window["future_publication_times"] == 1
    assert window["response_interval_seconds"]["median"] == 60


def test_current_journal_corruption_is_not_reported_as_no_data(config, capsys):
    config.root.mkdir()
    config.db("news").write_bytes(b"broken sqlite")
    assert main(["collection-health", "--config", str(config.path)]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_cli_reports_are_offline_and_refuse_overwrite(config, tmp_path, capsys, monkeypatch):
    import requests
    monkeypatch.setattr(requests.Session, "get", lambda *a, **k: pytest.fail("report must be offline"))
    out = tmp_path / "health.json"
    assert main(["collection-health", "--config", str(config.path), "--out", str(out)]) == 0
    original = out.read_bytes()
    assert main(["collection-health", "--config", str(config.path), "--out", str(out)]) == 2
    assert out.read_bytes() == original
    assert main(["qualify-sources", "--config", str(config.path), "--source", "irna"]) == 0
    assert not config.root.exists()
    capsys.readouterr()
