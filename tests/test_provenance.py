from dataclasses import asdict, replace
import json

import pytest

from oilbot.clock import utc_now, stamp
from oilbot.provenance import (SourceProfile, StructuredNewsAdapter, attribution_candidates,
                               claim_transitions, load_profiles, record_claim)
from oilbot.schema import NewsItem
from oilbot.store import Journal


def profile(source, kind="regional_news", scopes=(), *, group=None, verified=False):
    return SourceProfile(source, source.upper(), kind, "test", list(scopes), group, bool(group),
                         qualification="verified" if verified else "pending",
                         verification_basis="synthetic fixture evidence" if group or verified else None)


PROFILES = {p.source: p for p in [profile("fars"), profile("al_arabiya"),
    profile("irgc", "official", ["own_statements"]), profile("centcom", "official", ["own_statements"]),
    profile("ukmto", "official", ["maritime_incidents"], verified=True),
    profile("aramco", "operator", ["own_operations"], verified=True),
    profile("media_a", group="a"), profile("media_b", group="b")]}


def claim(identity, publisher, origin, *, value="missile", key="damage_mechanism", basis="attributed",
          scope="own_statements", when=0, polarity="asserted", episode="episode1"):
    return {"id": identity, "kind": "claim_revision", "available_at": f"2026-09-18T12:00:{when:02d}Z",
            "payload": {"publisher": publisher, "claim_origin": origin, "origin_claim_id": "original-claim",
                "profiles": {k: asdict(v) for k, v in PROFILES.items()}, "basis": basis,
                "claim_key": key, "value": value, "polarity": polarity, "authority_scope": scope,
                "event_type": "EXPORT_TERMINAL_CLOSED" if value == "suspended" else "TANKER_ATTACK",
                "episode_id": episode, "source_status": "update", "story_id": identity,
                "story_revision_id": "story-" + identity, "received_at": f"2026-09-18T12:00:{when:02d}Z"}}


def test_syndicated_official_claims_do_not_become_independent_confirmations():
    rows = [claim("a", "fars", "irgc"), claim("b", "al_arabiya", "irgc", when=1)]
    transitions = claim_transitions(rows)
    assert len(transitions) == 1
    assert transitions[0]["confirmation_level"] == "OFFICIAL_CLAIM"
    assert transitions[0]["independent_groups"] == []


def test_competing_official_accounts_remain_contested():
    rows = [claim("a", "fars", "irgc", value="mine"), claim("b", "centcom", "centcom", basis="first_party", when=1)]
    transitions = claim_transitions(rows)
    assert transitions[-1]["confirmation_level"] == "CONTESTED"
    assert transitions[-1]["contradictions"][0]["asserted"] == ["mine", "missile"]


def test_scoped_primary_confirmation_progression():
    rows = [claim("a", "fars", None, scope="", value="hit"),
            claim("b", "fars", "irgc", when=1, value="hit"),
            claim("c", "ukmto", "ukmto", when=2, value="hit", scope="maritime_incidents", basis="first_party"),
            claim("d", "aramco", "aramco", when=3, value="suspended", key="ras_tanura.operational_status.at-2026-09-18", scope="own_operations", basis="first_party")]
    transitions = claim_transitions(rows)
    assert [r["confirmation_level"] for r in transitions] == ["UNVERIFIED_REPORT", "OFFICIAL_CLAIM", "MARITIME_AUTHORITY_CORROBORATION", "PHYSICAL_DISRUPTION_CONFIRMED"]
    # A media quote from an operator does not substitute for an authenticated operator publication.
    quoted = claim("e", "fars", "aramco", value="suspended", key="operational_status", scope="own_operations")
    assert claim_transitions([quoted])[0]["confirmation_level"] == "OFFICIAL_CLAIM"


def test_independence_requires_original_reporting_on_same_proposition():
    a = claim("a", "media_a", "media_a", basis="direct_observation", scope="")
    b = claim("b", "media_b", "media_b", when=1, basis="direct_observation", scope="")
    assert claim_transitions([a, b])[-1]["confirmation_level"] == "MULTIPLE_MEDIA_CORROBORATION"
    b["payload"]["claim_key"] = "other_vessel"
    assert claim_transitions([a, b])[-1]["confirmation_level"] != "MULTIPLE_MEDIA_CORROBORATION"


def test_deletion_removes_evidence_and_old_review_does_not_resurrect_it():
    a = claim("a", "aramco", "aramco", value="suspended", key="operational_status", scope="own_operations", basis="first_party")
    deletion = {"id": "new-story", "kind": "story_revision", "available_at": "2026-09-18T12:00:01Z",
                "payload": {"story_id": "a", "status": "deleted", "source_id": "aramco"}}
    stale = {**a, "id": "late-review", "available_at": "2026-09-18T12:00:02Z"}
    states = claim_transitions([a, deletion, stale])
    assert len(states) == 2 and states[-1]["confirmation_level"] == "UNVERIFIED_REPORT"


def test_social_metadata_survives_without_creating_authority():
    row = {"id": "post1", "url": "https://example.test/post1", "title": "Screenshot",
           "text": "CENTCOM says vessel was hit", "author": "reposter", "reposter": "reposter",
           "original_url": "https://example.test/original", "media_sha256": "a" * 64, "status": "deleted"}
    item = StructuredNewsAdapter().parse(json.dumps({"items": [row]}).encode(), "https://example.test", "application/json")[0]
    assert item.author == "reposter" and item.status == "deleted" and item.origin is None
    with pytest.raises(ValueError, match="unrecognized"):
        StructuredNewsAdapter().parse(json.dumps({"items": [{**row, "confirmed": True}]}).encode(), "", "")


def test_attribution_requires_literal_speech_not_name_mentions():
    assert not attribution_candidates("IRGC vessel near port", PROFILES)
    candidates = attribution_candidates("IRGC says two tankers were hit", PROFILES)
    assert candidates[0]["claim_origin"] == "irgc" and candidates[0]["status"] == "ATTRIBUTION_CANDIDATE"


def test_claim_is_reviewed_immutable_and_literal(tmp_path):
    news, analysis = Journal(tmp_path / "news.db"), Journal(tmp_path / "analysis.db")
    source = {"id": "fars", "role": "journalism", "rights": {"model_processing": "pending"}}
    clock = stamp()
    oid = news.capture(source, {"body": b"IRGC says two tankers were hit", "received": clock})
    story = news.accept_items(source, oid, [NewsItem("story1", "https://example.test/1", "IRGC says", "IRGC says two tankers were hit")], {})[0]
    annotation = {"story_revision_id": story, "episode_id": "incident1", "claim_key": "tanker_damage",
                  "event_type": "TANKER_ATTACK", "value": "two_hit", "polarity": "asserted",
                  "claim_origin": "irgc", "origin_claim_id": None, "basis": "attributed",
                  "authority_scope": "own_statements", "start": 0, "end": 30,
                  "quote": "IRGC says two tankers were hit", "review_reason": "synthetic literal attribution"}
    rid = record_claim(news, analysis, PROFILES, annotation)
    assert record_claim(news, analysis, PROFILES, annotation) == rid
    assert analysis.get(rid)["payload"]["publisher"] == "fars"
    with pytest.raises(ValueError, match="literal"):
        record_claim(news, analysis, PROFILES, {**annotation, "quote": "confirmed fact"})
    with pytest.raises(ValueError, match="scope"):
        record_claim(news, analysis, PROFILES, {**annotation, "authority_scope": "own_operations"})


def test_source_catalog_does_not_enable_unqualified_access():
    profiles = load_profiles("configs/source-profiles.json")
    assert len(profiles) == 39
    assert not any(p.enabled for p in profiles.values())
    assert not any(p.independence_verified for p in profiles.values())
    assert {"irna", "mehr", "tasnim", "fars", "spa", "wam", "ona", "qna", "kuna", "bna", "centcom"} <= profiles.keys()
