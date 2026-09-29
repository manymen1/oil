from copy import deepcopy

import pytest

from oilbot.economic import build_transition

T0, T1, T2 = "2026-09-28T00:00:00Z", "2026-09-28T00:01:00Z", "2026-09-28T00:02:00Z"


def inputs():
    text = "Operator Alpha reports the previously operating crude terminal is now suspended."
    story = dict(id="story", kind="story_revision", available_at=T0, payload=dict(
        title=text, text=text, local_received_at=T0, source_id="publisher", initial_snapshot=False))
    event = dict(id="event", kind="fast_event", available_at=T1, payload=dict(story_revision_id="story"))
    review = dict(id="review", available_at=T2, payload=dict(event_id="event", story_revision_id="story",
        episode_id="episode", reviewer="human1", reviewer_type="human", reason="Read exact operational statement",
        episode_reviewed=True, novel=True, stage="disruption", state_before="OPERATING", state_after="SUSPENDED",
        assertion="asserted", confirmation_level="OPERATOR_REPORT", mechanism="supply", claim_origin="Operator Alpha",
        evidence=[dict(text_field="text", start=0, end=len(text), quote=text,
            supports=["stage", "state_before", "state_after", "assertion", "confirmation_level", "mechanism", "claim_origin"])]))
    mapping = dict(as_of=T2, episodes=[dict(id="episode", event_ids=["event"], review_required=False)],
                   evidence_states={"event": "ACTIVE"}, active_review_ids=[])
    return event, story, review, mapping


def test_bridge_is_research_only_and_preserves_receipt():
    args = inputs()
    original = deepcopy(args)
    output = build_transition(*args, evaluated_at=T2)
    p = output["payload"]
    assert p["research_eligible"] and p["hypothesis_direction"] == "up"
    assert not p["trade_authorized"] and p["direction"] is None
    assert p["received_at"] == T0 and p["decision_at"] == T2
    assert p["severity"] is None and p["estimated_duration"] is None
    assert "MARKET_DATA_UNAVAILABLE" in p["abstention_reasons"]
    assert args == original
    assert build_transition(*args, evaluated_at=T2) == output


@pytest.mark.parametrize("field,value,reason", [
    ("assertion", "denied", "NON_ASSERTED_OPERATIONAL_CHANGE"),
    ("assertion", "hypothetical", "NON_ASSERTED_OPERATIONAL_CHANGE"),
    ("novel", False, "NO_NOVEL_INFORMATION"),
    ("episode_reviewed", False, "UNREVIEWED_EPISODE"),
    ("reviewer_type", "assistant", "ASSISTANT_REVIEW_NOT_INDEPENDENT"),
    ("claim_origin", None, "UNKNOWN_CLAIM_ORIGIN"),
    ("mechanism", "unknown", "NO_CRUDE_MECHANISM"),
    ("confirmation_level", "ACTOR_CLAIM", "WEAK_OR_AMBIGUOUS_SOURCE"),
    ("state_before", "UNKNOWN", "NO_ELIGIBLE_STATE_TRANSITION"),
    ("unresolved_entities", ["terminal identity"], "UNRESOLVED_ENTITIES"),
])
def test_abstention(field, value, reason):
    args = inputs(); args[2]["payload"][field] = value
    p = build_transition(*args, evaluated_at=T2)["payload"]
    assert not p["research_eligible"] and p["hypothesis_direction"] is None
    assert reason in p["abstention_reasons"]


@pytest.mark.parametrize("state", ["WITHDRAWN", "CORRECTED", "DELETED", "CONTESTED", "UNKNOWN"])
def test_lifecycle_cannot_become_a_restoration_signal(state):
    args = inputs(); args[3]["evidence_states"]["event"] = state
    assert not build_transition(*args, evaluated_at=T2)["payload"]["research_eligible"]


def test_causal_and_literal_guards():
    args = inputs()
    with pytest.raises(ValueError, match="future"):
        build_transition(*args, evaluated_at=T1)
    args[2]["payload"]["evidence"][0]["quote"] = "fabricated"
    with pytest.raises(ValueError, match="exact"):
        build_transition(*args, evaluated_at=T2)
    args = inputs(); args[2]["payload"]["story_revision_id"] = "other"
    with pytest.raises(ValueError, match="binding"):
        build_transition(*args, evaluated_at=T2)
    args = inputs(); args[3]["episodes"][0]["review_required"] = True
    assert "UNREVIEWED_EPISODE" in build_transition(*args, evaluated_at=T2)["payload"]["abstention_reasons"]


@pytest.mark.parametrize("value", ["terminal", [None], [""]])
def test_invalid_unresolved_entities(value):
    args = inputs(); args[2]["payload"]["unresolved_entities"] = value
    with pytest.raises(ValueError, match="unresolved entities"):
        build_transition(*args, evaluated_at=T2)


def test_restoration_and_baseline_exclusion():
    args = inputs()
    a = args[2]["payload"]
    text = "Operator Alpha reports crude terminal loading resumed after suspension."
    args[1]["payload"].update(title=text, text=text)
    a.update(stage="restoration", state_before="SUSPENDED", state_after="RESTORED")
    a["evidence"][0].update(quote=text, end=len(text))
    assert build_transition(*args, evaluated_at=T2)["payload"]["hypothesis_direction"] == "down"
    args[1]["payload"]["initial_snapshot"] = True
    assert "INITIAL_SNAPSHOT_EXCLUDED" in build_transition(*args, evaluated_at=T2)["payload"]["abstention_reasons"]


def test_classifier_cannot_predate_story():
    args = inputs()
    args[1]["available_at"] = T2
    with pytest.raises(ValueError, match="classifier predates"):
        build_transition(*args, evaluated_at=T2)


@pytest.mark.parametrize("stage,after,eligible", [
    ("partial_restoration", "PARTLY_RESTORED", True),
    ("restoration", "RESTORED", True),
    ("restoration", "PARTLY_RESTORED", False),
    ("partial_restoration", "RESTORED", False),
])
def test_restoration_stage_must_match_state(stage, after, eligible):
    args = inputs()
    args[2]["payload"].update(state_before="SUSPENDED", state_after=after, stage=stage)
    result = build_transition(*args, evaluated_at=T2)["payload"]
    assert result["research_eligible"] is eligible
    assert ("STAGE_STATE_MISMATCH" in result["abstention_reasons"]) is not eligible


@pytest.mark.parametrize("supports", ["stage", [], ["invented"], [None]])
def test_invalid_evidence_supports(supports):
    args = inputs()
    args[2]["payload"]["evidence"][0]["supports"] = supports
    with pytest.raises(ValueError, match="supports"):
        build_transition(*args, evaluated_at=T2)


def test_named_origin_needs_literal_anchor():
    args = inputs()
    args[2]["payload"]["claim_origin"] = "Someone Else"
    with pytest.raises(ValueError, match="claim origin"):
        build_transition(*args, evaluated_at=T2)
