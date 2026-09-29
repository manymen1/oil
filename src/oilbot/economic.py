"""Offline forward-economic bridge. No price estimates, model calls or orders.

Input assessments are separately dated reviewer judgments, not classifier facts.
Callers must load records from a verified snapshot and persist outputs separately.
Exact spans validate provenance, not the truth of the reviewer's interpretation.
"""
from .clock import instant
from .schema import digest

VERSION = "forward-economic-v3"
STATES = {"UNKNOWN", "OPERATING", "IMPAIRED", "SUSPENDED", "PARTLY_RESTORED", "RESTORED"}
STAGES = {"initial_report", "identity", "damage", "restriction", "disruption",
          "duration_update", "repair", "partial_restoration", "restoration",
          "contradiction", "correction", "withdrawal"}
CONFIRMATIONS = {"UNVERIFIED", "ACTOR_CLAIM", "MARITIME_AUTHORITY_REPORT", "OPERATOR_REPORT"}


def build_transition(event, story, assessment, mapping, *, evaluated_at, evidence_stories=None):
    """Normalize one reviewed stage using only inputs available by evaluation.

    assessment is a record {id, available_at, payload}; payload must name the
    exact event/story and the episode and provide a reviewer and literal spans.
    The review can accept a singleton episode explicitly; group membership alone
    never grants operational confirmation or economic novelty.
    """
    now = instant(evaluated_at)
    for row in (event, story, assessment):
        if not row.get("id") or instant(row["available_at"]) > now:
            raise ValueError("missing identity or future input")
    if event.get("kind") not in {"fast_event", "operational_review_candidate"} or story.get("kind") != "story_revision":
        raise ValueError("forward event and captured story revision required")
    e, s, a = event["payload"], story["payload"], assessment["payload"]
    if e["story_revision_id"] != story["id"] or a["event_id"] != event["id"] or a["story_revision_id"] != story["id"]:
        raise ValueError("revision binding mismatch")
    if instant(event["available_at"]) < instant(story["available_at"]):
        raise ValueError("classifier predates captured story availability")
    if instant(assessment["available_at"]) < max(instant(event["available_at"]), instant(story["available_at"])):
        raise ValueError("review predates its inputs")
    if instant(mapping["as_of"]) != now:
        raise ValueError("mapping must be reconstructed at evaluation time")
    groups = [g for g in mapping["episodes"] if event["id"] in g["event_ids"]]
    if len(groups) != 1 or a["episode_id"] != groups[0]["id"]:
        raise ValueError("reviewed episode binding mismatch")
    if any(not isinstance(a.get(k), str) or not a[k].strip() for k in ("reviewer", "reason")):
        raise ValueError("dated reviewer provenance required")
    if a.get("reviewer_type") not in {"human", "assistant"}:
        raise ValueError("reviewer type required")
    if a.get("stage") not in STAGES or a.get("state_before") not in STATES or a.get("state_after") not in STATES:
        raise ValueError("unknown episode stage/state")
    if a.get("confirmation_level") not in CONFIRMATIONS:
        raise ValueError("unknown confirmation assessment")
    if a.get("assertion") not in {"asserted", "denied", "hypothetical", "historical", "unclear"}:
        raise ValueError("assertion required")
    if a.get("mechanism") not in {"supply", "transport", "demand", "risk_sentiment", "unknown"}:
        raise ValueError("mechanism required")
    if type(a.get("novel")) is not bool or type(a.get("episode_reviewed")) is not bool:
        raise ValueError("explicit novelty and episode review required")
    unresolved = a.get("unresolved_entities", [])
    if not isinstance(unresolved, list) or any(not isinstance(x, str) or not x.strip() for x in unresolved):
        raise ValueError("unresolved entities must be a list of nonempty strings")
    evidence = a.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("literal evidence required")
    fields = {"stage", "assertion", "state_before", "state_after", "mechanism",
              "confirmation_level", "claim_origin", "asset", "asset_type", "location",
              "severity", "estimated_duration"}
    for key in ("claim_origin", "asset", "asset_type", "location", "severity", "estimated_duration"):
        if a.get(key) is not None and (not isinstance(a[key], str) or not a[key].strip()):
            raise ValueError(key + " must be nonempty text or null")
    supported = set()
    prior_story_hashes = {}
    for span in evidence:
        base_keys = {"text_field", "start", "end", "quote", "supports"}
        if not isinstance(span, dict) or set(span) not in (base_keys, base_keys | {"story_revision_id"}):
            raise ValueError("evidence requires field, offsets, quote and supported fields")
        evidence_text = s
        if "story_revision_id" in span:
            prior = (evidence_stories or {}).get(span["story_revision_id"])
            if (prior is None or prior.get("kind") != "story_revision" or
                    instant(prior["available_at"]) >= instant(story["available_at"]) or
                    instant(prior["available_at"]) > instant(s["local_received_at"])):
                raise ValueError("prior-state evidence must be a captured story available before receipt")
            if span["supports"] != ["state_before"]:
                raise ValueError("prior story evidence can support only state_before")
            evidence_text = prior["payload"]
            prior_story_hashes[prior["id"]] = digest(prior)
        field = span["text_field"]
        if field not in {"title", "text"}:
            raise ValueError("invalid evidence field")
        lo, hi = span["start"], span["end"]
        if type(lo) is not int or type(hi) is not int or not 0 <= lo < hi <= len(evidence_text[field]) or evidence_text[field][lo:hi] != span["quote"]:
            raise ValueError("evidence must match exact captured text")
        supports = span["supports"]
        if not isinstance(supports, list) or not supports or any(not isinstance(k, str) or k not in fields for k in supports):
            raise ValueError("evidence supports must name reviewed fields")
        if "claim_origin" in supports and a.get("claim_origin") and a["claim_origin"].casefold() not in span["quote"].casefold():
            raise ValueError("claim origin must occur in its cited evidence")
        supported.update(supports)
    required = {"stage", "assertion"}
    for key, unknown in (("state_before", "UNKNOWN"), ("state_after", "UNKNOWN"),
                         ("mechanism", "unknown"), ("confirmation_level", "UNVERIFIED")):
        if a[key] != unknown:
            required.add(key)
    for key in ("claim_origin", "asset", "asset_type", "location", "severity", "estimated_duration"):
        if a.get(key) is not None:
            required.add(key)
    if not required <= supported:
        raise ValueError("missing literal support for reviewed fields")
    received = s["local_received_at"]
    if instant(received) > instant(story["available_at"]):
        raise ValueError("story availability predates receipt")
    before, after = a["state_before"], a["state_after"]
    disruption = (before, after) in {("OPERATING", "IMPAIRED"), ("OPERATING", "SUSPENDED"),
                                    ("IMPAIRED", "SUSPENDED"), ("PARTLY_RESTORED", "SUSPENDED")}
    restoration = before in {"IMPAIRED", "SUSPENDED", "PARTLY_RESTORED"} and after in {"PARTLY_RESTORED", "RESTORED"} and before != after
    reasons = []
    if unresolved:
        reasons.append("UNRESOLVED_ENTITIES")
    if not a["episode_reviewed"] or groups[0].get("review_required"):
        reasons.append("UNREVIEWED_EPISODE")
    if a["reviewer_type"] != "human":
        reasons.append("ASSISTANT_REVIEW_NOT_INDEPENDENT")
    if not a["novel"]:
        reasons.append("NO_NOVEL_INFORMATION")
    if a["assertion"] != "asserted":
        reasons.append("NON_ASSERTED_OPERATIONAL_CHANGE")
    lifecycle = mapping.get("evidence_states", {}).get(event["id"], "UNKNOWN")
    if lifecycle != "ACTIVE" or a["stage"] in {"contradiction", "correction", "withdrawal"}:
        reasons.append("EVIDENCE_REQUIRES_REVIEW")
    if s.get("initial_snapshot", True):
        reasons.append("INITIAL_SNAPSHOT_EXCLUDED")
    if event["kind"] == "operational_review_candidate":
        if e.get("story_hash") != digest(story):
            raise ValueError("operational candidate story hash mismatch")
        if not e.get("forward_capture_candidate") or e.get("exclusions"):
            reasons.append("CAPTURE_EXCLUDED")
    if a["confirmation_level"] not in {"OPERATOR_REPORT", "MARITIME_AUTHORITY_REPORT"}:
        reasons.append("WEAK_OR_AMBIGUOUS_SOURCE")
    if not a.get("claim_origin"):
        reasons.append("UNKNOWN_CLAIM_ORIGIN")
    if a["mechanism"] not in {"supply", "transport"}:
        reasons.append("NO_CRUDE_MECHANISM")
    if not (disruption or restoration):
        reasons.append("NO_ELIGIBLE_STATE_TRANSITION")
    restoration_stage = "partial_restoration" if after == "PARTLY_RESTORED" else "restoration"
    if disruption and a["stage"] not in {"restriction", "disruption"} or restoration and a["stage"] != restoration_stage:
        reasons.append("STAGE_STATE_MISMATCH")
    result = {"schema": VERSION, "episode_id": a["episode_id"], "event_id": event["id"],
              "contract": "operational-transition-v1", "candidate_kind": event["kind"],
              "story_revision_id": story["id"], "reviewer_type": a["reviewer_type"],
              "initial_snapshot": s.get("initial_snapshot", True), "novelty": a["novel"],
              "episode_verified": a["episode_reviewed"] and not groups[0].get("review_required", False),
              "event_family": "physical_disruption" if disruption else "restoration" if restoration else "other",
              "event_transition": before + "->" + after,
              **{k: a.get(k) for k in ("stage", "state_before", "state_after", "assertion", "asset", "asset_type",
                  "location", "claim_origin", "confirmation_level", "severity", "estimated_duration", "mechanism")},
              "unresolved_entities": a.get("unresolved_entities", []), "publisher": s["source_id"],
              "received_at": received, "classifier_available_at": event["available_at"],
              "review_available_at": assessment["available_at"], "decision_at": evaluated_at,
              "evaluated_at": evaluated_at, "evidence_state": lifecycle,
              "hypothesis_direction": ("up" if disruption else "down") if not reasons else None,
              "direction": None, "research_eligible": not reasons,
              "abstention_reasons": reasons + ["MARKET_DATA_UNAVAILABLE", "INSUFFICIENT_HISTORICAL_SUPPORT"],
              "trade_authorized": False, "assessment_id": assessment["id"], "evidence": a["evidence"],
              "mapping_hash": digest(mapping), "prior_story_hashes": prior_story_hashes,
              "input_revision_ids": sorted(set([event["id"], story["id"], assessment["id"]]
                  + mapping.get("active_review_ids", []) + list(prior_story_hashes))),
              "limitations": "Reviewed claims, not verified physical truth. No return estimate or execution decision."}
    return {"id": digest(result), "kind": "forward_economic_transition", "available_at": evaluated_at, "payload": result}
