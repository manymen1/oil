"""Integrity boundaries between immutable evidence, linking and human review."""
import pytest

from oilbot.forward_review import episode_map, review_link
from oilbot.linking import LinkerWorker
from oilbot.replay import export_manifest, load_manifest
from test_forward import setup, warm, capture


def linked(setup, count=2):
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    warm(news, src)
    for n, verb in enumerate(["attacked", "hit", "struck"][:count]):
        capture(news, src, f"IRGC says tanker ALPHA {verb} in Hormuz", native=str(n))
    worker.run_once()
    LinkerWorker(news, output).run_once()
    return output.records("candidate_episode_link")


def test_linker_upgrade_does_not_reclassify(setup):
    cfg, news, output, worker = setup
    linked(setup)
    originals = output.records("fast_event")
    assert LinkerWorker(news, output).run_once()["processed"] == 0
    upgraded = LinkerWorker(news, output, version="test-policy-v3")
    assert upgraded.run_once()["candidate_links"] == 1
    assert upgraded.run_once()["processed"] == 0
    assert worker.run_once()["processed"] == 0
    assert output.records("fast_event") == originals
    assert len(output.records("candidate_episode_link")) == 2
    for event in originals:
        p = event["payload"]
        assert "candidate_episode_links" not in p and "linker_version" not in p
        assert not set(p["input_revision_ids"]) & {r["id"] for r in originals}


def test_reviews_supersede_explicitly_and_mapping_is_asof(setup, tmp_path):
    cfg, news, output, worker = setup
    link = linked(setup)[0]
    original = output.records("fast_event")
    before = episode_map(output.path)
    assert len(before["episodes"]) == 2
    first = review_link(output, link["id"], "SAME_EVENT", "matching report", "tester", review_id="review-one")
    assert review_link(output, link["id"], "SAME_EVENT", "matching report", "tester", review_id="review-one") == first
    with pytest.raises(ValueError, match="collision"):
        review_link(output, link["id"], "UNRELATED", "different", "tester", review_id="review-one")
    first_at = output.get(first)["available_at"]
    joined = episode_map(output.path, through=first_at)
    assert len(joined["event_groups"]) == len(joined["episodes"]) == 1
    assert not joined["confirmation_granted"]
    with pytest.raises(ValueError, match="latest review"):
        review_link(output, link["id"], "UNRELATED", "different report", "tester")
    second = review_link(output, link["id"], "UNRELATED", "different report", "tester", supersedes_review_id=first)
    assert episode_map(output.path)["active_review_ids"] == [second]
    assert len(episode_map(output.path)["episodes"]) == 2
    assert episode_map(output.path, through=first_at) == joined
    assert output.records("fast_event") == original
    _, reader, _ = load_manifest(export_manifest(cfg, tmp_path / "review-snapshot"))
    assert len([r for r in reader.records if r["kind"] == "forward_link_review"]) == 2


@pytest.mark.parametrize("decision", ["SYNDICATED_REPORT", "UNCERTAIN", "UNRELATED"])
def test_nonidentity_reviews_do_not_merge_events(setup, decision):
    output = setup[2]
    link = linked(setup)[0]
    review_link(output, link["id"], decision, "test", "tester")
    mapping = episode_map(output.path)
    assert len(mapping["event_groups"]) == len(mapping["episodes"]) == 2
    assert len(mapping["syndication_groups"]) == (1 if decision == "SYNDICATED_REPORT" else 2)


def test_transitive_unrelated_conflict_quarantines_group(setup):
    output = setup[2]
    links = linked(setup, count=3)
    for link, decision in zip(links, ["SAME_EVENT", "SAME_EVENT", "UNRELATED"]):
        review_link(output, link["id"], decision, "test", "tester")
    mapping = episode_map(output.path)
    assert mapping["conflicts"]
    assert len(mapping["episodes"]) == 3
    assert all(group["review_required"] for group in mapping["episodes"])


def test_different_event_types_can_share_episode_only(setup):
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    warm(news, src)
    capture(news, src, "Aramco says Ras Tanura loading suspended", native="a")
    capture(news, src, "Aramco says Ras Tanura loading resumed", native="b")
    worker.run_once()
    assert LinkerWorker(news, output).run_once()["candidate_links"] == 1
    link = output.records("candidate_episode_link")[0]
    assert link["payload"]["relation_type"] == "SAME_EPISODE_CANDIDATE"
    with pytest.raises(ValueError, match="different event types"):
        review_link(output, link["id"], "SAME_EVENT", "same terminal", "tester")
    review_link(output, link["id"], "SAME_EPISODE", "same terminal", "tester")
    mapping = episode_map(output.path)
    assert len(mapping["event_groups"]) == 2 and len(mapping["episodes"]) == 1


@pytest.mark.parametrize("status,state", [("correction", "CORRECTED"), ("withdrawal", "WITHDRAWN"), ("deleted", "DELETED")])
def test_revision_only_changes_its_own_publisher_evidence(setup, status, state):
    cfg, news, output, worker = setup
    for src in cfg.sources[:2]:
        warm(news, src)
        capture(news, src, "IRGC says tanker ALPHA hit in Hormuz")
    worker.run_once()
    original = output.records("fast_event")
    before_at = output.records("forward_evidence_transition")[-1]["available_at"]
    # An intervening non-classified headline must not sever event lineage.
    capture(news, cfg.sources[0], "Update pending")
    worker.run_once()
    capture(news, cfg.sources[0], "The previous report is withdrawn", status=status)
    worker.run_once()
    current = episode_map(output.path)
    assert current["evidence_states"][original[0]["id"]] == state
    assert current["evidence_states"][original[1]["id"]] == "ACTIVE"
    assert set(episode_map(output.path, through=before_at)["evidence_states"].values()) == {"ACTIVE"}
    assert output.records("fast_event") == original
    assert output.records("story_event_lineage")[-1]["payload"]["supersedes_fast_event_ids"] == [original[0]["id"]]
    assert worker.run_once()["processed"] == 0


def test_revision_generates_new_active_evidence_without_rewriting_old(setup):
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    warm(news, src)
    capture(news, src, "Tanker ALPHA hit in Hormuz")
    worker.run_once()
    first = output.records("fast_event")[0]
    capture(news, src, "IRGC says tanker ALPHA hit in Hormuz")
    worker.run_once()
    events = output.records("fast_event")
    assert len(events) == 2 and events[0] == first
    assert events[0]["payload"]["incident_id"] == events[1]["payload"]["incident_id"]
    assert first["id"] not in events[1]["payload"]["input_revision_ids"]
    assert episode_map(output.path)["evidence_states"] == {first["id"]: "DOCUMENT_SUPERSEDED", events[1]["id"]: "ACTIVE"}


@pytest.mark.parametrize("status,state", [("correction", "CORRECTED"), ("withdrawal", "WITHDRAWN"), ("deleted", "DELETED")])
@pytest.mark.parametrize("headline,new_events", [("Further update pending", 0), ("IRGC says tanker ALPHA hit in Hormuz", 1)])
def test_ordinary_update_cannot_weaken_strong_evidence_state(setup, tmp_path, status, state, headline, new_events):
    from oilbot.forward import ForwardRecorder
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    warm(news, src)
    capture(news, src, "Tanker ALPHA hit in Hormuz")
    worker.run_once()
    first = output.records("fast_event")[0]
    active_at = output.records("forward_evidence_transition")[-1]["available_at"]
    capture(news, src, "Previous report revised", status=status)
    worker.run_once()
    strong = output.records("forward_evidence_transition")[-1]
    assert strong["payload"]["state"] == state
    capture(news, src, headline)
    # Restart must use the journal state, not a process-local terminal flag.
    worker = ForwardRecorder(news, output, cfg.raw["assets"])
    assert worker.run_once()["events"] == new_events
    transitions = [r for r in output.records("forward_evidence_transition") if r["payload"]["event_id"] == first["id"]]
    assert transitions[-1] == strong
    assert episode_map(output.path)["evidence_states"][first["id"]] == state
    assert episode_map(output.path, through=active_at)["evidence_states"][first["id"]] == "ACTIVE"
    assert episode_map(output.path, through=strong["available_at"])["evidence_states"][first["id"]] == state
    assert output.get(first["id"]) == first
    if new_events:
        newer = output.records("fast_event")[-1]
        assert newer["id"] != first["id"]
        assert episode_map(output.path)["evidence_states"][newer["id"]] == "ACTIVE"
    # A second non-event update must not erase the original strong state either.
    capture(news, src, "Another update pending")
    worker.run_once()
    assert episode_map(output.path)["evidence_states"][first["id"]] == state
    assert worker.run_once()["processed"] == 0
    manifest = export_manifest(cfg, tmp_path / "strong-state-snapshot")
    load_manifest(manifest)
    expected = episode_map(output.path)
    assert episode_map(manifest.parent / "forward.sqlite3", through=expected["as_of"]) == expected


def test_explicit_withdrawal_still_follows_correction_across_ordinary_update(setup):
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    warm(news, src)
    capture(news, src, "Tanker attacked in Hormuz")
    worker.run_once()
    event = output.records("fast_event")[0]
    for status, title in [("correction", "Corrected report"), ("update", "Update pending"), ("withdrawal", "Withdrawn report")]:
        capture(news, src, title, status=status)
        worker.run_once()
    states = [r["payload"]["state"] for r in output.records("forward_evidence_transition") if r["payload"]["event_id"] == event["id"]]
    assert states == ["ACTIVE", "CORRECTED", "WITHDRAWN"]


def test_evidence_policy_upgrade_preserves_cursor_epoch_and_history(setup, monkeypatch):
    import oilbot.forward as forward
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    with monkeypatch.context() as m:
        m.setattr(forward, "EVIDENCE_POLICY_VERSION", "test-legacy-evidence-policy")
        m.setattr(forward, "STRONG_EVIDENCE_STATES", frozenset())
        legacy = forward.ForwardRecorder(news, output, cfg.raw["assets"])
        warm(news, src)
        capture(news, src, "Tanker hit in Hormuz")
        legacy.run_once()
        capture(news, src, "Withdrawn report", status="withdrawal")
        legacy.run_once()
    old_events = output.records("fast_event")
    old_transitions = output.records("forward_evidence_transition")
    consumed = output.cursor(forward.CURSOR_KEY)
    upgraded = forward.ForwardRecorder(news, output, cfg.raw["assets"])
    assert upgraded.epoch == legacy.epoch
    assert output.cursor(forward.CURSOR_KEY) == consumed
    assert upgraded.run_once()["processed"] == 0
    assert output.records("fast_event") == old_events
    assert output.records("forward_evidence_transition") == old_transitions
    policy = output.records("forward_policy_revision")[-1]["payload"]
    assert policy["evidence_policy_version"] == forward.EVIDENCE_POLICY_VERSION
    assert not policy["historical_reclassification"]
    capture(news, src, "Further update pending")
    upgraded.run_once()
    assert episode_map(output.path)["evidence_states"][old_events[0]["id"]] == "WITHDRAWN"


def test_review_validation_and_linker_transaction_rollback(setup, monkeypatch):
    cfg, news, output, worker = setup
    links = linked(setup)
    with pytest.raises(ValueError):
        review_link(output, links[0]["id"], "SAME_EVENT", "", "tester")
    with pytest.raises(ValueError):
        review_link(output, "missing", "SAME_EVENT", "reason", "tester")
    new_linker = LinkerWorker(news, output, version="rollback-test")
    before = output.records()
    append = output.append
    def fail(kind, *args, **kwargs):
        if kind == "linker_processing":
            raise RuntimeError("interrupted")
        return append(kind, *args, **kwargs)
    monkeypatch.setattr(output, "append", fail)
    with pytest.raises(RuntimeError):
        new_linker.run_once()
    assert output.records() == before
    with output.connect() as db:
        assert db.execute("SELECT count(*) FROM linker_event_index WHERE policy_hash=?", (new_linker.policy_hash,)).fetchone()[0] == 0
    monkeypatch.setattr(output, "append", append)
    assert new_linker.run_once()["candidate_links"] == 1


def test_evidence_snapshot_and_mapping_do_not_depend_on_cursors(setup, tmp_path):
    cfg, news, output, worker = setup
    link = linked(setup)[0]
    review_link(output, link["id"], "SAME_EVENT", "matching vessel", "tester")
    capture(news, cfg.sources[0], "Withdrawn", native="0", status="withdrawal")
    worker.run_once()
    before = episode_map(output.path)
    manifest = export_manifest(cfg, tmp_path / "evidence-snapshot")
    load_manifest(manifest)  # Validate every causal input, including transition lineage.
    with output.transaction() as db:
        db.execute("DELETE FROM cursors")
        db.execute("DROP TABLE linker_event_index")
    assert episode_map(output.path, through=before["as_of"]) == before
    assert episode_map(manifest.parent / "forward.sqlite3", through=before["as_of"]) == before


def test_cli_review_and_readonly_mapping(setup, monkeypatch, capsys):
    import json
    import oilbot.cli as cli
    cfg, news, output, worker = setup
    link = linked(setup)[0]
    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    assert cli.main(["review-forward-link", "--candidate", link["id"], "--decision", "SAME_EVENT",
                     "--reviewer", "tester", "--reason", "matching vessel"]) == 0
    assert json.loads(capsys.readouterr().out)["review_id"]
    before = output.records()
    assert cli.main(["forward-episodes"]) == 0
    assert len(json.loads(capsys.readouterr().out)["episodes"]) == 1
    assert output.records() == before
