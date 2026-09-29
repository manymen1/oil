from copy import deepcopy
from datetime import timedelta
import json

import pytest

from oilbot.clock import instant, stamp
from oilbot.economic import build_transition
from oilbot.economic_dataset import build_economic_dataset
from oilbot.economic_review import assess, events, main, mapping_at, queue, report, template
from oilbot.operational import OperationalQueue
from oilbot.replay import export_manifest, load_manifest
from oilbot.schema import NewsItem, digest
from oilbot.story_review import Archive
from test_economic import inputs, T2
from test_forward import setup, capture, warm


@pytest.fixture
def body_case(setup, tmp_path):
    cfg, news, output, fast = setup
    worker = OperationalQueue(news, output, cfg.raw["assets"], cfg.sources)
    source = cfg.sources[0]
    warm(news, source)
    now = stamp()
    title = "Operations update"
    text = "Operator Alpha reports previously operating crude loading is now suspended."
    oid = news.capture(source, {"url": source["url"], "status": 200, "content_type": "text/xml", "headers": {},
        "started": now, "first_byte": now, "received": now, "body": text.encode(), "delivery": "http", "synthetic": False})
    rid = news.accept_items(source, oid, [NewsItem("body", source["url"], title, text)], {})[0]
    fast.run_once()
    worker.run_once()
    assert not output.records("fast_event")
    archive = Archive(export_manifest(cfg, tmp_path / "snapshot"))
    eid = next(iter(events(archive)))
    value = template(archive, eid)["assessment"]
    value.update(reviewer="synthetic-human", reason="Synthetic body-only operational statement.",
        episode_reviewed=True, novel=True, stage="disruption", state_before="OPERATING", state_after="SUSPENDED",
        assertion="asserted", confirmation_level="OPERATOR_REPORT", mechanism="supply", claim_origin="Operator Alpha",
        evidence=[dict(text_field="text", start=0, end=len(text), quote=text,
            supports=["stage", "assertion", "state_before", "state_after", "confirmation_level", "mechanism", "claim_origin"])])
    return archive, value, tmp_path / "assessments.db"


def test_body_only_roundtrip_no_synthetic_fast_event(body_case, tmp_path):
    archive, value, path = body_case
    assert queue(archive)["events"][0]["candidate_kind"] == "operational_review_candidate"
    assert report(archive, path)["queue_states"] == {value["event_id"]: "PENDING"}
    saved = assess(archive, path, value)
    p = saved["transition_at_review"]["payload"]
    assert p["research_eligible"] and p["event_family"] == "physical_disruption"
    assert p["candidate_kind"] == "operational_review_candidate"
    assert report(archive, path)["queue_states"] == {value["event_id"]: "ASSESSED"}
    out = tmp_path / "dataset"
    metadata = build_economic_dataset(archive, path, out, through=saved["available_at"])
    rows = [json.loads(line) for line in (out / "transitions.jsonl").read_text().splitlines()]
    assert metadata["rows"] == metadata["subjects"] == 1 and metadata["pending_subjects"] == 0
    assert metadata["dataset_role"] == "unpriced_research" and not metadata["promotion"]
    assert rows[0]["decision_at"] == rows[0]["review_available_at"] == saved["available_at"]
    assert rows[0]["action"] == "ABSTAIN" and not rows[0]["trade_authorized"]
    assert rows[0]["features"]["CL_price_at_decision"] is None
    assert rows[0]["outcomes"]["horizons"]["1"]["label_state"] == "PENDING_HORIZON"
    load_manifest(archive.path)
    with pytest.raises(ValueError, match="already exists"):
        build_economic_dataset(archive, path, out)


def test_assistant_abstention_and_supersession_remain_visible(body_case, tmp_path):
    archive, value, path = body_case
    first = assess(archive, path, {**value, "assessment_id": "first", "reviewer_type": "assistant", "authorization": "synthetic"})
    second = assess(archive, path, {**value, "assessment_id": "second", "supersedes_assessment_id": "first", "novel": False})
    assert report(archive, path)["queue_states"][value["event_id"]] == "ABSTAINED"
    build_economic_dataset(archive, path, tmp_path / "dataset")
    rows = [json.loads(line) for line in (tmp_path / "dataset/transitions.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert [r["latest_assessment_at_export"] for r in rows] == [False, True]
    assert rows[0]["decision_at"] == first["available_at"] and rows[1]["decision_at"] == second["available_at"]
    assert all(not r["research_eligible"] for r in rows)
    meta = build_economic_dataset(archive, path, tmp_path / "past", through=first["available_at"])
    assert meta["rows"] == 1


def test_pending_census_and_cli_export(body_case, tmp_path, capsys):
    archive, value, path = body_case
    main(["--manifest", str(archive.path), "dataset", "--assessments", str(path), "--out", str(tmp_path / "empty")])
    meta = json.loads(capsys.readouterr().out)
    assert meta["pending_subjects"] == 1 and meta["rows"] == 0
    assert not path.exists()


def test_legacy_dataset_refuses_to_silently_drop_forward_records(body_case):
    from oilbot.dataset import transition_rows
    from oilbot.features import MarketView
    archive, _, _ = body_case
    with pytest.raises(ValueError, match="dated assessments"):
        transition_rows(list(archive.records.values()), [], MarketView([]))


def test_changed_saved_transition_is_not_exportable(body_case, tmp_path, monkeypatch):
    archive, value, path = body_case
    saved = assess(archive, path, value)
    saved["transition_at_review"]["payload"]["novelty"] = False
    saved["transition_at_review"]["id"] = digest(saved["transition_at_review"]["payload"])
    monkeypatch.setattr("oilbot.economic_dataset.history", lambda *a, **k: [saved])
    with pytest.raises(ValueError, match="reconstruction mismatch"):
        build_economic_dataset(archive, path, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_operational_capture_exclusions_survive_bridge(body_case):
    archive, value, path = body_case
    event = deepcopy(events(archive)[value["event_id"]])
    event["payload"].update(forward_capture_candidate=False, exclusions=["NOT_LIVE_HTTP"])
    story = archive.stories[value["story_revision_id"]]
    now = stamp()["utc"]
    result = build_transition(event, story, {"id": "a", "available_at": now, "payload": value}, mapping_at(archive, now), evaluated_at=now)
    assert "CAPTURE_EXCLUDED" in result["payload"]["abstention_reasons"]
    event["payload"]["story_hash"] = "wrong"
    with pytest.raises(ValueError, match="hash mismatch"):
        build_transition(event, story, {"id": "a", "available_at": now, "payload": value}, mapping_at(archive, now), evaluated_at=now)


def test_queued_revision_with_fast_event_is_not_double_counted(setup, tmp_path):
    cfg, news, output, fast = setup
    worker = OperationalQueue(news, output, cfg.raw["assets"], cfg.sources)
    warm(news, cfg.sources[0])
    capture(news, cfg.sources[0], "Operator Alpha reports crude production suspended")
    fast.run_once()
    worker.run_once()
    archive = Archive(export_manifest(cfg, tmp_path / "snapshot"))
    assert output.records("fast_event") and output.records("operational_review_candidate")
    assert len(events(archive)) == 1
    assert next(iter(events(archive).values()))["kind"] == "fast_event"


def test_later_classifier_does_not_replace_an_earlier_queue_subject(setup, tmp_path):
    cfg, news, output, fast = setup
    worker = OperationalQueue(news, output, cfg.raw["assets"], cfg.sources)
    warm(news, cfg.sources[0])
    capture(news, cfg.sources[0], "Operator Alpha reports crude production suspended")
    worker.run_once()
    fast.run_once()
    archive = Archive(export_manifest(cfg, tmp_path / "snapshot"))
    assert len(events(archive)) == 1
    subject = next(iter(events(archive).values()))
    assert subject["kind"] == "operational_review_candidate"
    assert subject["id"] in mapping_at(archive, subject["available_at"])["evidence_states"]


def test_later_revision_quarantines_operational_subject(body_case):
    archive, value, path = body_case
    story = archive.stories[value["story_revision_id"]]
    later = deepcopy(story)
    later["id"] = "later"
    later["available_at"] = (instant(story["available_at"]) + timedelta(seconds=10)).isoformat()
    archive.stories["later"] = later
    before = mapping_at(archive, story["available_at"])
    after = mapping_at(archive, later["available_at"])
    assert value["event_id"] not in before["evidence_states"]  # Not queued yet.
    assert after["evidence_states"][value["event_id"]] == "SUPERSEDED"


def test_prior_state_literal_story_is_causal_and_hash_bound():
    event, story, assessment, mapping = inputs()
    prior = deepcopy(story)
    prior.update(id="prior", available_at="2026-09-27T00:00:00Z")
    prior["payload"]["text"] = "Terminal is operating normally."
    span = assessment["payload"]["evidence"][0]
    span["supports"].remove("state_before")
    text = prior["payload"]["text"]
    assessment["payload"]["evidence"].append(dict(story_revision_id="prior", text_field="text", start=0,
        end=len(text), quote=text, supports=["state_before"]))
    result = build_transition(event, story, assessment, mapping, evaluated_at=T2, evidence_stories={"prior": prior})
    assert result["payload"]["prior_story_hashes"] == {"prior": digest(prior)}
    assert "prior" in result["payload"]["input_revision_ids"]
    prior["available_at"] = T2
    with pytest.raises(ValueError, match="before receipt"):
        build_transition(event, story, assessment, mapping, evaluated_at=T2, evidence_stories={"prior": prior})
    with pytest.raises(ValueError, match="before receipt"):
        build_transition(event, story, assessment, mapping, evaluated_at=T2)
