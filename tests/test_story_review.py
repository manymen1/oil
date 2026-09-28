from copy import deepcopy
from pathlib import Path
import json
import sqlite3

import pytest

from oilbot.clock import utc_now
from oilbot.replay import export_manifest, load_manifest
from oilbot.schema import digest
from oilbot.story_review import Archive, annotate, annotation_history, coverage_report, diagnostic, freeze_sample, main
from test_forward import setup, warm, capture


@pytest.fixture
def reviewed(setup, tmp_path):
    cfg, news, output, worker = setup
    warm(news, cfg.sources[0])
    matched = capture(news, cfg.sources[0], "Iran says tanker attacked near Hormuz", native="matched")
    missed = capture(news, cfg.sources[0], "Iran says Hormuz opening proposed", native="missed")
    worker.run_once()
    archive = Archive(export_manifest(cfg, tmp_path / "snapshot"))
    return archive, matched["id"], missed["id"], tmp_path / "reviews.sqlite3"


def label(archive, rid, **updates):
    text = archive.stories[rid]["payload"]["title"]
    return {"story_revision_id": rid, "decision": "label", "reviewer": "test-reviewer", "reviewer_type": "human",
        "oil_relevance": "relevant", "event_family": "attack", "assertion": "asserted", "claimant": None,
        "oil_mechanism": "transport", "reason": "Synthetic test annotation, not real data",
        "evidence": [{"text_field": "title", "start": 0, "end": len(text), "quote": text,
                      "supports": ["oil_relevance", "event_family", "assertion", "oil_mechanism"]}], **updates}


def sample(archive):
    rows, strata = [], {}
    for rid, r in archive.stories.items():
        source, disp = r["payload"]["source_id"], archive.disposition(rid)
        rows.append({"story_revision_id": rid, "source": source, "historical_disposition": disp})
        group = strata.setdefault((source, disp), {"source": source, "historical_disposition": disp,
            "population_revisions": 0, "sample_revisions": 0})
        group["population_revisions"] += 1
        group["sample_revisions"] += 1
    return {"sample": rows, "strata": list(strata.values())}


def test_unmatched_annotation_preserves_capture_and_historical_output(reviewed):
    archive, matched, missed, path = reviewed
    before = {p.name: p.read_bytes() for p in archive.path.parent.iterdir() if p.is_file()}
    row = annotate(archive, path, label(archive, missed, decision="add_missed", event_family="restoration", assertion="hypothetical"))
    assert row["historical_event_ids"] == []
    assert not row["confirmation_granted"] and not row["historical_output_modified"]
    assert row["annotated_at"] >= archive.stories[missed]["available_at"]
    assert before == {p.name: p.read_bytes() for p in archive.path.parent.iterdir() if p.is_file()}
    load_manifest(archive.path)


def test_append_only_supersession_asof_and_idempotence(reviewed):
    archive, matched, _, path = reviewed
    payload = label(archive, matched, annotation_id="first", decision="accept")
    first = annotate(archive, path, payload)
    assert annotate(archive, path, payload) == first
    with pytest.raises(ValueError, match="latest annotation"):
        annotate(archive, path, label(archive, matched))
    second = annotate(archive, path, label(archive, matched, decision="correct", claimant="Iran",
        supersedes_annotation_id=first["annotation_id"], evidence=[
        *payload["evidence"], {"text_field":"title","start":0,"end":9,"quote":"Iran says","supports":["claimant"]}]))
    assert second["supersedes_annotation_id"] == "first"
    assert annotation_history(path, through=first["annotated_at"]) == [first]
    assert len(annotation_history(path)) == 2
    with sqlite3.connect(path) as db:
        for sql in ("DELETE FROM annotations", "UPDATE annotations SET annotated_at='2000'"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(sql)


@pytest.mark.parametrize("update", [
    {"event_family": "invented"}, {"reviewer": ""}, {"assertion": "confirmed"},
    {"annotated_at": "2000-01-01T00:00:00Z"}, {"evidence": []},
    {"story_revision_id": "missing"}, {"reviewer_type":"assistant"},
    {"claimant": "Reuters"},
])
def test_malformed_or_unanchored_annotations_rejected(reviewed, update):
    archive, matched, _, path = reviewed
    with pytest.raises(ValueError):
        annotate(archive, path, label(archive, matched, **update))
    assert not path.exists()


def test_bad_evidence_offsets_and_unknown_fields_rejected(reviewed):
    archive, matched, _, path = reviewed
    v = label(archive, matched)
    v["evidence"][0]["quote"] = "invented"
    with pytest.raises(ValueError, match="exact captured"):
        annotate(archive,path,v)
    v = label(archive, matched)
    v["evidence"][0]["text_field"] = "url"
    with pytest.raises(ValueError):
        annotate(archive,path,v)


def test_sidecar_must_not_alias_capture(reviewed, tmp_path):
    archive, matched, _, _ = reviewed
    target = archive.path.parent / "news.sqlite3"
    with pytest.raises(ValueError, match="outside"):
        annotate(archive,target,label(archive,matched))
    link = tmp_path / "alias.sqlite3"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        annotate(archive,link,label(archive,matched))


def test_assistant_provenance_is_explicit(reviewed):
    archive, _, missed, path = reviewed
    row = annotate(archive,path,label(archive,missed,reviewer_type="assistant",authorization="Synthetic isolated test approval"))
    assert row["reviewer_type"] == "assistant"
    assert row["authorization"]


def test_review_verdicts_require_appropriate_historical_output(reviewed):
    archive, matched, missed, path = reviewed
    for decision in ("accept", "reject", "correct"):
        with pytest.raises(ValueError, match="historical events"):
            annotate(archive,path,label(archive,missed,decision=decision))
    with pytest.raises(ValueError, match="unmatched"):
        annotate(archive,path,label(archive,matched,decision="add_missed"))


def test_sample_population_and_membership_verified(reviewed, tmp_path):
    archive, *_ = reviewed
    value = sample(archive)
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(value))
    assert archive.sample(path) == value
    value["strata"][0]["population_revisions"] += 1
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="denominators"):
        archive.sample(path)


def test_diagnostics_preserve_receipt_and_bind_policy(reviewed):
    archive, *_ = reviewed
    value = diagnostic(archive, sample(archive))
    assert value["policy_hash"] == digest(value["policy"])
    assert value["policy"]["classifier_version"] == "fast-event-v6"
    assert not value["historical_reclassification"]
    for row in value["results"]:
        assert row["original_received_at"] == archive.stories[row["story_revision_id"]]["payload"]["local_received_at"]


def test_coverage_separates_hypothesis_from_occurrence_and_excludes_baselines(reviewed):
    archive, matched, missed, path = reviewed
    annotate(archive,path,label(archive,matched,decision="accept"))
    annotate(archive,path,label(archive,missed,event_family="restoration",assertion="hypothetical",decision="add_missed"))
    labels = annotation_history(path)
    value = coverage_report(archive,sample(archive),labels)
    groups = {g["event_family"]: g for g in value["groups"] if g["reviewer_type"] == "human"}
    assert groups["attack"]["matched"] == 1 and groups["attack"]["weighted_precision"] == 1
    assert groups["restoration"]["missed"] == 1
    assert sum(g["excluded"] for g in value["groups"]) == 1
    occurrence = coverage_report(archive,sample(archive),labels,target="asserted_oil_event")
    group = next(g for g in occurrence["groups"] if g["event_family"] == "restoration")
    assert group["correct_negative"] == 1 and group["weighted_recall"] is None


def test_coverage_no_labels_no_accuracy_and_diagnostic_tamper_detection(reviewed):
    archive, *_ = reviewed
    s = sample(archive)
    value = coverage_report(archive,s,[])
    assert all(g["weighted_precision"] is None and g["weighted_recall"] is None for g in value["groups"])
    d = diagnostic(archive,s)
    d["sample_hash"] = "wrong"
    with pytest.raises(ValueError, match="mismatch"):
        coverage_report(archive,s,[],diagnostic_result=d)


def test_cli_browses_and_shows_unmatched_captured_text(reviewed, capsys):
    archive, _, missed, _ = reviewed
    main(["--manifest",str(archive.path),"browse","--disposition","UNMATCHED"])
    assert json.loads(capsys.readouterr().out)["stories"][0]["story_revision_id"] == missed
    main(["--manifest",str(archive.path),"show","--revision",missed])
    assert json.loads(capsys.readouterr().out)["story"]["text"]


def test_holdout_excludes_prior_story_identities_and_baselines(reviewed, tmp_path):
    archive, matched, missed, _ = reviewed
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps({"sample":[{"story_revision_id":matched}]}))
    result = freeze_sample(archive,per_stratum=3,received_after="2000-01-01T00:00:00Z",exclude_sample=prior)
    assert [r["story_revision_id"] for r in result["sample"]] == [missed]
    path = tmp_path / "holdout.json"
    path.write_text(json.dumps(result))
    assert archive.sample(path) == result
    assert archive.stories[matched]["payload"]["story_id"] in result["cohort"]["excluded_story_ids"]


def test_holdout_excludes_known_candidate_episode_peers(reviewed, tmp_path):
    archive, matched, missed, _ = reviewed
    event = archive.events[matched][0]
    archive.events[missed] = [{"id":"peer"}]
    archive.records["edge"] = {"kind":"candidate_episode_link", "payload":{
        "from_event_id":event["id"],"event_id":"peer"}}
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps({"sample":[{"story_revision_id":matched}]}))
    result = freeze_sample(archive,per_stratum=3,received_after="2000-01-01T00:00:00Z",exclude_sample=prior)
    assert not result["sample"]


def test_diagnostic_text_mode_spans_use_captured_text(reviewed):
    archive, matched, _, _ = reviewed
    d = diagnostic(archive,sample(archive),text_field="text")
    row = next(r for r in d["results"] if r["story_revision_id"] == matched)
    assert row["events"]
    for event in row["events"]:
        span = event["evidence"]
        assert span["field"] == "text"
        text = archive.stories[matched]["payload"]["text"]
        assert text[span["start"]:span["end"]] == span["quote"]


def test_weighted_counts_use_source_disposition_population(reviewed):
    archive, matched, missed, path = reviewed
    annotate(archive,path,label(archive,matched))
    annotate(archive,path,label(archive,missed,event_family="restoration",assertion="hypothetical"))
    s = sample(archive)
    # Add three unsampled revisions to the same unmatched stratum.
    for i in range(3):
        rid = 'extra-' + str(i)
        archive.stories[rid] = deepcopy(archive.stories[missed])
        archive.stories[rid]['id'] = rid
        archive.processing[rid] = deepcopy(archive.processing[missed])
    stratum = next(r for r in s['strata'] if r['historical_disposition'] == 'UNMATCHED')
    stratum['population_revisions'] = 4
    value = coverage_report(archive,s,annotation_history(path))
    group = next(g for g in value['groups'] if g['event_family']=='restoration')
    assert group['missed'] == 1 and group['weighted_missed'] == 4


def test_report_does_not_expose_future_sample_at_earlier_cutoff(reviewed):
    archive, *_ = reviewed
    with pytest.raises(ValueError,match="future-available"):
        coverage_report(archive,sample(archive),[],through="2000-01-01T00:00:00Z")
