import json
import sqlite3

import pytest

from oilbot.economic_review import assess, history, main, queue, report, template
from oilbot.forward_review import episode_map
from oilbot.replay import export_manifest, load_manifest
from oilbot.story_review import Archive
from test_forward import setup, warm, capture


@pytest.fixture
def review_case(setup, tmp_path):
    cfg, news, output, worker = setup
    warm(news, cfg.sources[0])
    story = capture(news, cfg.sources[0], "Iran says tanker attacked near Hormuz", native="review-test")
    worker.run_once()
    archive = Archive(export_manifest(cfg, tmp_path / "snapshot"))
    event = archive.events[story["id"]][0]
    value = template(archive, event["id"])["assessment"]
    text = archive.stories[story["id"]]["payload"]["title"]
    value.update(reviewer="synthetic-test-human", reason="Attack claim does not establish an operational disruption.",
                 stage="initial_report", assertion="asserted", claim_origin="Iran", episode_reviewed=True,
                 evidence=[dict(text_field="title", start=0, end=len(text), quote=text,
                                supports=["stage", "assertion", "claim_origin"])])
    return archive, value, tmp_path / "economic.sqlite3"


def test_queue_template_and_report_keep_unknowns_and_denominators(review_case):
    archive, value, path = review_case
    census = queue(archive)
    assert census["captured_events"] == 1
    assert "title" not in census["events"][0]
    draft = template(archive, value["event_id"])["assessment"]
    assert draft["state_before"] == "UNKNOWN" and draft["stage"] is None
    empty = report(archive, path)
    assert empty["reviewed_events"] == 0 and len(empty["unreviewed_event_ids"]) == 1
    assert not path.exists()
    row = assess(archive, path, value)
    result = report(archive, path, through=row["available_at"])
    assert result["captured_events"] == result["reviewed_events"] == 1
    assert result["reviewer_types"] == {"human": 1}
    assert result["unreviewed_event_ids"] == [] and result["invalid_assessments"] == []
    assert result["eligible_transitions"] == result["eligible_episode_groups"] == 0
    assert result["exclusion_reasons"]["NO_ELIGIBLE_STATE_TRANSITION"] == 1
    assert result["validation_status"] == "HUMAN_SEMANTIC_VALIDATION_REQUIRED"
    assert result["transitions"] == [row["transition_at_review"]]
    assert not result["trade_authorized"]


def test_append_only_corrections_idempotence_and_historical_view(review_case, monkeypatch):
    archive, value, path = review_case
    monkeypatch.setattr("oilbot.economic_review.utc_now", lambda: "2099-01-01T00:00:00Z")
    value["assessment_id"] = "first"
    first = assess(archive, path, value)
    assert assess(archive, path, value) == first
    with pytest.raises(ValueError, match="latest assessment"):
        assess(archive, path, {**value, "assessment_id": "stale"})
    monkeypatch.setattr("oilbot.economic_review.utc_now", lambda: "2099-01-01T00:01:00Z")
    second = assess(archive, path, {**value, "assessment_id": "second", "supersedes_assessment_id": "first",
                                   "reason": "Further review; operational state remains unknown."})
    assert history(archive, path, through=first["available_at"]) == [first]
    assert history(archive, path) == [first, second]
    current = report(archive, path)
    assert current["assessment_revisions"] == 2 and current["reviewed_events"] == 1
    assert current["transitions"][0]["payload"]["assessment_id"] == "second"
    with sqlite3.connect(path) as db:
        for statement in ("DELETE FROM economic_assessments", "UPDATE economic_assessments SET id='x'"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(statement)
    monkeypatch.setattr("oilbot.economic_review.utc_now", lambda: "2099-01-01T00:00:01Z")
    with pytest.raises(ValueError, match="clock regressed"):
        assess(archive, path, {**value, "assessment_id": "third", "supersedes_assessment_id": "second"})


@pytest.mark.parametrize("update", [
    {"available_at": "2000-01-01T00:00:00Z"}, {"reviewer_type": "assistant"},
    {"event_id": "missing"}, {"episode_id": "invented"}, {"evidence": []},
    {"claim_origin": "Not In Text"}, {"novel": "yes"},
])
def test_invalid_assessment_does_not_create_sidecar(review_case, update):
    archive, value, path = review_case
    with pytest.raises(ValueError):
        assess(archive, path, {**value, **update})
    assert not path.exists()


def test_assistant_is_distinct_and_never_independent(review_case):
    archive, value, path = review_case
    row = assess(archive, path, {**value, "reviewer_type": "assistant", "authorization": "synthetic test only"})
    assert "ASSISTANT_REVIEW_NOT_INDEPENDENT" in row["transition_at_review"]["payload"]["abstention_reasons"]
    assert report(archive, path)["reviewer_types"] == {"assistant": 1}


def test_eligible_disruption_roundtrip_preserves_causal_times(setup, tmp_path):
    cfg, news, output, worker = setup
    warm(news, cfg.sources[0])
    text = "Operator Alpha reports crude production suspended after operating normally."
    story = capture(news, cfg.sources[0], text, native="synthetic-disruption")
    worker.run_once()
    archive = Archive(export_manifest(cfg, tmp_path / "snapshot"))
    event = archive.events[story["id"]][0]
    value = template(archive, event["id"])["assessment"]
    value.update(reviewer="synthetic-test-human", reason="Synthetic explicit operational change.",
                 stage="disruption", state_before="OPERATING", state_after="SUSPENDED", assertion="asserted",
                 mechanism="supply", confirmation_level="OPERATOR_REPORT", claim_origin="Operator Alpha",
                 episode_reviewed=True, novel=True, evidence=[dict(text_field="title", start=0, end=len(text),
                    quote=text, supports=["stage", "state_before", "state_after", "assertion", "mechanism",
                                          "confirmation_level", "claim_origin"])])
    path = tmp_path / "assessments.db"
    row = assess(archive, path, value)
    result = report(archive, path, through=row["available_at"])
    transition = result["transitions"][0]["payload"]
    assert result["eligible_transitions"] == result["eligible_episode_groups"] == 1
    assert transition["received_at"] == story["payload"]["local_received_at"]
    assert transition["classifier_available_at"] == event["available_at"]
    assert transition["review_available_at"] == transition["decision_at"] == row["available_at"]
    assert {event["id"], story["id"], row["id"]} <= set(transition["input_revision_ids"])
    assert transition["hypothesis_direction"] == "up" and transition["direction"] is None
    assert not transition["trade_authorized"]


def test_snapshot_bytes_and_path_guards(review_case, tmp_path):
    archive, value, path = review_case
    before = {p.name: p.read_bytes() for p in archive.path.parent.iterdir() if p.is_file()}
    for target in (archive.path.parent / "subdir" / "reviews.db", archive.path.parent / "news.sqlite3"):
        with pytest.raises(ValueError, match="outside"):
            assess(archive, target, value)
    alias = tmp_path / "alias.sqlite3"
    alias.hardlink_to(archive.path.parent / "news.sqlite3")
    with pytest.raises(ValueError, match="alias"):
        assess(archive, alias, value)
    assess(archive, path, value)
    report(archive, path)
    load_manifest(archive.path)
    assert before == {p.name: p.read_bytes() for p in archive.path.parent.iterdir() if p.is_file()}


def test_foreign_sidecar_and_cross_snapshot_reuse_rejected(review_case, tmp_path):
    archive, value, path = review_case
    foreign = tmp_path / "foreign.db"
    with sqlite3.connect(foreign) as db:
        db.execute("CREATE TABLE records(id TEXT)")
    with pytest.raises(ValueError, match="not an economic"):
        assess(archive, foreign, value)
    assess(archive, path, value)
    archive.manifest = {**archive.manifest, "records_hash": "different"}
    with pytest.raises(ValueError, match="snapshot mismatch"):
        history(archive, path)
    with pytest.raises(ValueError, match="snapshot mismatch"):
        assess(archive, path, {**value, "assessment_id": "other"})


def test_later_lifecycle_reassessment_retains_original(review_case, monkeypatch):
    archive, value, path = review_case
    first = assess(archive, path, value)
    mapping = episode_map(archive.path.parent / "forward.sqlite3")
    mapping["evidence_states"][value["event_id"]] = "WITHDRAWN"
    monkeypatch.setattr("oilbot.economic_review.mapping_at", lambda archive, at: {**mapping, "as_of": at})
    result = report(archive, path)
    assert result["transitions"][0]["payload"]["evidence_state"] == "WITHDRAWN"
    assert history(archive, path)[0] == first


def test_cli_roundtrip_and_no_overwrite(review_case, tmp_path, capsys):
    archive, value, path = review_case
    base = ["--manifest", str(archive.path)]
    main(base + ["queue"])
    assert json.loads(capsys.readouterr().out)["captured_events"] == 1
    source = tmp_path / "input.json"
    source.write_text(json.dumps(value))
    main(base + ["assess", "--assessments", str(path), "--file", str(source)])
    saved = json.loads(capsys.readouterr().out)
    result_path = tmp_path / "report.json"
    args = base + ["report", "--assessments", str(path), "--through", saved["available_at"], "--out", str(result_path)]
    main(args)
    assert json.loads(result_path.read_text())["reviewed_events"] == 1
    with pytest.raises(ValueError, match="already exists"):
        main(args)
    with pytest.raises(ValueError, match="outside"):
        main(base + ["queue", "--out", str(archive.path.parent / "subdir" / "queue.json")])
