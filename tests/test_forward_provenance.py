from dataclasses import replace

import pytest

from oilbot.forward import classify
from oilbot.forward_quality import forward_quality
from oilbot.forward_review import propose_link, review_link, episode_map
from oilbot.linking import LinkerWorker, read_candidates
from oilbot.replay import export_manifest, load_manifest
from oilbot.sources import RSSAdapter, NewsCollector
from oilbot.wire_attribution import wire_provenance
from test_forward import setup, capture, warm
from test_forward_quality import response


@pytest.mark.parametrize("text,origin,cited", [
    ("According to Reuters, a tanker was hit", None, ("reuters",)),
    ("A report by Reuters says a tanker was hit", None, ()),
    ("By Reuters standards this is unusual", None, ()),
    ("A news item mentions (Reuters) in prose", None, ()),
    ("(Reuters) A tanker was hit", "reuters", ()),
    ("By Reuters\nTanker hit", "reuters", ()),
    ("By Bloomberg: Tanker hit", "bloomberg", ()),
    ("(Reuters) According to AFP, a tanker was hit", "reuters", ("afp",)),
    ("(Reuters) First\n(Bloomberg) Second", None, ()),
])
def test_wire_credits_are_not_citations(text, origin, cited):
    result = wire_provenance(text)
    assert result["origin"] == origin
    assert result["source_attributions"] == cited
    for span in result["wire_evidence"]:
        assert span["quote"] == text[span["start"]:span["end"]]


def test_author_credit_and_body_citation_remain_separate():
    xml = b'''<rss><channel><item><guid>one</guid><title>Tanker ALPHA hit in Hormuz</title>
      <author>Reuters</author><description>According to AFP, officials said...</description></item></channel></rss>'''
    item = RSSAdapter().parse(xml, "https://example.test/feed", "text/xml")[0]
    assert item.origin == "reuters" and item.source_attributions == ("afp",)
    for span in item.wire_evidence:
        text = getattr(item, span["field"])
        assert text[span["start"]:span["end"]] == span["quote"]
    from oilbot.schema import to_dict
    event = classify({**to_dict(item), "source_id": "publisher"}, [])[0]
    assert event["syndication_origin"] == "reuters"
    assert event["source_attributions"] == ("afp",)
    assert event["claim_origin"] is None and event["confirmation"] == "UNVERIFIED"
    legacy = classify({"title": item.title, "source_id": "publisher", "origin": "reuters"}, [])[0]
    assert legacy["syndication_origin"] is None and legacy["legacy_origin_unverified"] == "reuters"


def test_parser_upgrade_does_not_turn_old_words_into_new_event(setup, monkeypatch):
    import oilbot.store as store
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    warm(news, src)
    xml = b'''<rss><channel><item><guid>a</guid><title>Tanker ALPHA hit in Hormuz</title>
      <description>According to Reuters, an incident occurred.</description></item></channel></rss>'''
    current = RSSAdapter().parse(xml, src["url"], "text/xml")[0]
    legacy = replace(current, origin="reuters", source_attributions=(), wire_evidence=(), wire_provenance_version=None)
    with monkeypatch.context() as m:
        m.setattr(store, "PARSER_VERSION", "source-v1")
        oid = news.capture(src, response(src, body=xml))
        news.accept_items(src, oid, [legacy], {})
    worker.run_once()
    original = output.records("fast_event")
    assert len(original) == 1
    oid = news.capture(src, response(src, body=xml))
    news.accept_items(src, oid, [current], {})
    assert news.records("story_revision")[-1]["payload"]["parser_reinterpretation"]
    assert worker.run_once()["events"] == 0
    assert output.records("fast_event") == original
    assert output.records("forward_processing")[-1]["payload"]["exclusion"] == "PARSER_REINTERPRETATION"
    assert episode_map(output.path)["evidence_states"][original[0]["id"]] == "ACTIVE"
    # A genuinely changed headline under the new parser is a normal source update.
    oid = news.capture(src, response(src, body=xml))
    news.accept_items(src, oid, [replace(current, title="IRGC says tanker ALPHA hit in Hormuz")], {})
    assert not news.records("story_revision")[-1]["payload"]["parser_reinterpretation"]
    assert worker.run_once()["events"] == 1


def test_recovery_quarantines_unknown_parser_version(setup, monkeypatch):
    import oilbot.store as store
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    with monkeypatch.context() as m:
        m.setattr(store, "PARSER_VERSION", "source-v1")
        oid = news.capture(src, response(src))
    NewsCollector(news, [src]).recover_unparsed()
    with news.connect() as db:
        row = db.execute("SELECT state,reason FROM parse_work WHERE observation_id=?", (oid,)).fetchone()
    assert row["state"] == "failed" and "PARSER_VERSION_CHANGED" in row["reason"]
    assert news.get(oid) is not None and not news.records("story_revision")


def test_manual_proposal_surpasses_linker_but_does_not_merge(setup, tmp_path):
    cfg, news, output, worker = setup
    src = cfg.sources[0]
    warm(news, src)
    capture(news, src, "Tanker ALPHA hit in Hormuz", native="a")
    capture(news, src, "Tanker BETA hit in Hormuz", native="b")
    worker.run_once()
    assert LinkerWorker(news, output).run_once()["candidate_links"] == 0
    events = output.records("fast_event")
    left, right = [r["id"] for r in events]
    reason = "Two vessels in the same reviewed operational episode"
    pid = propose_link(output, left, right, "SAME_EPISODE_CANDIDATE", reason, "tester", proposal_id="manual-one")
    assert propose_link(output, left, right, "SAME_EPISODE_CANDIDATE", reason, "tester", proposal_id="manual-one") == pid
    assert len(episode_map(output.path)["episodes"]) == 2
    proposed_at = output.get(pid)["available_at"]
    assert read_candidates(output.path)["links"][0]["payload"]["proposal_origin"] == "HUMAN"
    assert forward_quality(cfg)["window"]["manual_candidate_links"] == 1
    assert forward_quality(cfg)["window"]["candidate_link_rate"]["numerator"] == 0
    review = review_link(output, pid, "SAME_EPISODE", reason, "tester")
    assert len(episode_map(output.path)["episodes"]) == 1
    assert len(episode_map(output.path, through=proposed_at)["episodes"]) == 2
    review_link(output, pid, "UNRELATED", "New evidence", "tester", supersedes_review_id=review)
    assert len(episode_map(output.path)["episodes"]) == 2
    assert output.records("fast_event") == events
    _, reader, _ = load_manifest(export_manifest(cfg, tmp_path / "manual-snapshot"))
    assert any(r["id"] == pid for r in reader.records)
    with pytest.raises(ValueError, match="collision"):
        propose_link(output, left, right, "SAME_EVENT_CANDIDATE", reason, "tester", proposal_id=pid)
    for a, b in [(left, left), (left, "missing")]:
        with pytest.raises(ValueError):
            propose_link(output, a, b, "SAME_EPISODE_CANDIDATE", reason, "tester")


def test_manual_proposal_cli_does_not_review_implicitly(setup, monkeypatch, capsys):
    import json
    import oilbot.cli as cli
    cfg, news, output, worker = setup
    warm(news, cfg.sources[0])
    for n in range(2):
        capture(news, cfg.sources[0], "Tanker hit in Hormuz", native=str(n))
    worker.run_once()
    events = output.records("fast_event")
    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    assert cli.main(["propose-forward-link", "--left", events[0]["id"], "--right", events[1]["id"],
                     "--relation", "SAME_EVENT_CANDIDATE", "--reviewer", "tester", "--reason", "Matching details"]) == 0
    assert json.loads(capsys.readouterr().out)["candidate_link_id"]
    assert not output.records("forward_link_review")
