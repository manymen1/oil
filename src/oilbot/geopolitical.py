"""Snapshot-bound news continuation research. No new claims, models or orders."""
from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal
import json
import math
from pathlib import Path

from .clock import epoch_ns, iso_ns, utc_now
from .databento import file_hash
from .economic import VERSION as ECONOMIC_VERSION, build_transition
from .economic_review import events, history, mapping_at, outside_snapshot
from .features import MarketView
from .inventory_strategy import inventory_features
from .inventory_v2 import InventoryRulesV2, continuation_market_filter
from .macro_dataset import comparisons
from .market import atomic_json
from .outcomes import OutcomePolicy, forward_outcomes
from .schema import canonical, digest

RISK_DIRECTION = {
    "TANKER_ATTACK": 1, "TANKER_SEIZURE": 1, "MILITARY_STRIKE": 1,
    "MISSILE_ATTACK": 1, "DRONE_ATTACK": 1, "CEASEFIRE_BROKEN": 1,
    "NEGOTIATIONS_COLLAPSED": 1, "CEASEFIRE_REACHED": -1, "NEGOTIATIONS_STARTED": -1,
}
PHYSICAL_TYPES = {"SHIPPING_RESTRICTION", "SHIPPING_RESTORED", "PRODUCTION_SUSPENDED",
    "PRODUCTION_RESTORED", "EXPORT_TERMINAL_CLOSED", "EXPORT_TERMINAL_REOPENED",
    "PIPELINE_OUTAGE", "PIPELINE_RESTORED"}


@dataclass(frozen=True)
class GeopoliticalRules:
    version: str = "geopolitical-continuation-v1-draft"
    max_classifier_delay_seconds: int = 10
    max_publication_age_seconds: int = 180
    market_rules: dict = field(default_factory=lambda: asdict(InventoryRulesV2()))

    def validate(self):
        if self.version != "geopolitical-continuation-v1-draft":
            raise ValueError("unsupported geopolitical policy")
        for key in ("max_classifier_delay_seconds", "max_publication_age_seconds"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError("positive news timing limit required")
        self.market().validate()

    def market(self):
        return InventoryRulesV2(**self.market_rules)


def news_decision(event, story, mapping, features, *, at, rules, transition=None, capture_exclusions=()):
    rules.validate()
    m = rules.market()
    e, s = event["payload"], story["payload"]
    now, receipt = epoch_ns(at), epoch_ns(s["local_received_at"])
    if (e["story_revision_id"] != story["id"] or receipt > epoch_ns(story["available_at"])
            or epoch_ns(story["available_at"]) > epoch_ns(event["available_at"])
            or epoch_ns(event["available_at"]) > now or epoch_ns(mapping["as_of"]) != now):
        raise ValueError("noncausal or mismatched news inputs")
    reasons = list(capture_exclusions)
    event_type = e.get("event_type")
    lane = "physical_transition" if transition or event_type in PHYSICAL_TYPES or event["kind"] == "operational_review_candidate" else "risk_premium"
    side = 0
    if s.get("initial_snapshot", True) or s.get("parser_reinterpretation"):
        reasons.append("INITIAL_OR_REINTERPRETED_CAPTURE")
    if s.get("status", "update") != "update" or mapping.get("evidence_states", {}).get(event["id"]) != "ACTIVE":
        reasons.append("INACTIVE_OR_CORRECTED_EVIDENCE")
    group = next((g for g in mapping["episodes"] if event["id"] in g["event_ids"]), None)
    if not group or group.get("review_required"):
        reasons.append("EPISODE_CONFLICT_OR_MISSING")
    publication_age = None
    if s.get("published_at"):
        publication_age = (receipt - epoch_ns(s["published_at"])) / 1e9
        if not 0 <= publication_age <= rules.max_publication_age_seconds:
            reasons.append("OLD_OR_FUTURE_PUBLICATION")
    else:
        reasons.append("MISSING_PUBLICATION_TIME")
    classifier_delay = (epoch_ns(event["available_at"]) - receipt) / 1e9
    if not 0 <= classifier_delay <= rules.max_classifier_delay_seconds:
        reasons.append("SLOW_CAPTURE_TO_CANDIDATE")
    if not m.confirmation_seconds <= (now - receipt) / 1e9 <= m.max_decision_delay_seconds:
        reasons.append("OUTSIDE_NEWS_DECISION_WINDOW")
    if lane == "risk_premium":
        side = RISK_DIRECTION.get(event_type, 0)
        if not side:
            reasons.append("NO_REVIEWED_DIRECTION_POLICY_FOR_EVENT_TYPE")
        if e.get("classifier_version") != "fast-event-v7" or e.get("interpretation_version") != "headline-interpretation-v2":
            reasons.append("UNSUPPORTED_CAPTURED_CLASSIFIER")
        if e.get("assertion") != "asserted" or e.get("qualifier"):
            reasons.append("NONASSERTED_OR_QUALIFIED_CLAIM")
        if e.get("oil_relevance") != "relevant":
            reasons.append("AMBIGUOUS_OIL_RELEVANCE")
        if not e.get("claim_origin"):
            reasons.append("UNKNOWN_CLAIM_ORIGIN")
        if not e.get("novelty"):
            reasons.append("NO_NOVEL_CLAIM")
        evidence = e.get("evidence", {})
        if (evidence.get("field") != "title" or type(evidence.get("start")) is not int
                or type(evidence.get("end")) is not int
                or not 0 <= evidence["start"] < evidence["end"] <= len(s["title"])
                or s["title"][evidence["start"]:evidence["end"]] != evidence.get("quote")):
            raise ValueError("captured event evidence does not match story")
    elif transition is None:
        reasons.append("PHYSICAL_ASSESSMENT_REQUIRED")
    else:
        p = transition["payload"]
        if (transition["id"] != digest(p) or p["schema"] != ECONOMIC_VERSION or p["event_id"] != event["id"]
                or p["story_revision_id"] != story["id"] or epoch_ns(p["decision_at"]) > now):
            raise ValueError("physical transition binding mismatch")
        if not p["research_eligible"]:
            reasons.append("PHYSICAL_REVIEW_INELIGIBLE")
        else:
            side = {"up": 1, "down": -1}.get(p["hypothesis_direction"], 0)
        if not side:
            reasons.append("NO_ELIGIBLE_PHYSICAL_DIRECTION")
    evidence_reasons = list(reasons)
    market_reasons = []
    price_side = 0
    metrics = {}
    if not features:
        market_reasons.append("MISSING_MARKET_DATA")
    else:
        if epoch_ns(features["received_at"]) != receipt or epoch_ns(features["decision_at"]) != now:
            raise ValueError("news market feature time mismatch")
        vol = features.get("realized_vol_5m")
        if vol is None or not math.isfinite(vol) or not 0 <= vol <= m.max_volatility_5m:
            market_reasons.append("MISSING_OR_EXCESSIVE_VOLATILITY")
        move = features["receipt_response"].get("receipt_to_decision_return")
        if move is None or not math.isfinite(move):
            market_reasons.append("MISSING_RECEIPT_PRICE_ANCHOR")
        elif abs(move) > m.max_receipt_move:
            reasons.append("PRICE_ALREADY_REPRICED")
        momentum = features.get("returns_before_decision", {}).get("60")
        if momentum is not None and math.isfinite(momentum):
            price_side = (momentum > 0) - (momentum < 0)
        check = continuation_market_filter(features, received_at=s["local_received_at"], at=at, side=side, rules=m)
        metrics = check["metrics"]
        for reason in check["reason_codes"]:
            if reason == "MISSING_CONTINUATION_CONTEXT" or reason.startswith(("CONTINUATION_CONTRACT_CHANGED:",
                    "INCOMPLETE_CONTINUATION_MARKET:", "CONTINUATION_LIQUIDITY:")):
                market_reasons.append(reason)
            else:
                reasons.append(reason)
    reasons += market_reasons
    return {"schema": "geopolitical-decision-v1", "lane": lane, "event_type": event_type,
        "action": "ABSTAIN" if reasons else "RESEARCH_CANDIDATE", "reason_codes": sorted(set(reasons)),
        "comparison_exclusions": sorted(set(evidence_reasons + market_reasons)),
        "hypothesis_direction": side, "physical_disruption_confirmed": False,
        "physical_review_eligible": bool(transition and transition["payload"]["research_eligible"]),
        "confirmation": "REVIEWED_OPERATIONAL_CLAIM" if transition else "UNVERIFIED_CLAIM",
        "episode_id_asof": group["id"] if group else None,
        "baselines": {"no_trade": 0, "news_direction_only": side, "price_only_60s": price_side,
                      "news_plus_continuation": side if not reasons else 0},
        "publication_to_receipt_seconds": publication_age, "receipt_to_candidate_seconds": classifier_delay,
        "receipt_to_decision_seconds": (now-receipt)/1e9,
        "continuation_metrics": metrics, "features_hash": digest(features) if features else None,
        "rules_hash": digest(asdict(rules)), "trade_authorized": False, "authorized_contracts": 0,
        "expected_profit": None}


def overlap_groups(archive, subjects, through):
    """Conservative evaluation grouping, never confirmation or decision input.

    Even unreviewed links share a split. Equal titles share a split only, not
    factual identity. Future-to-decision links may affect evaluation grouping.
    """
    parent = {eid: eid for eid in subjects}
    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key
    def join(a, b):
        if a in parent and b in parent:
            x, y = root(a), root(b)
            parent[max(x, y)] = min(x, y)
    seen = {}
    for eid, event in subjects.items():
        s = archive.stories[event["payload"]["story_revision_id"]]["payload"]
        keys = [("story", s["story_id"]), ("title", " ".join(s["title"].casefold().split()))]
        if s.get("original_url"):
            keys.append(("original_url", s["original_url"]))
        if event["payload"].get("incident_id"):
            keys.append(("incident", event["payload"]["incident_id"]))
        for key in keys:
            if key in seen: join(eid, seen[key])
            else: seen[key] = eid
    for row in archive.records.values():
        if row["kind"] in {"candidate_episode_link", "forward_link_review"} and epoch_ns(row["available_at"]) <= epoch_ns(through):
            p = row["payload"]
            join(p.get("left_event_id", p.get("from_event_id")), p.get("right_event_id", p.get("event_id")))
    groups = {}
    for eid in parent:
        groups.setdefault(root(eid), []).append(eid)
    return {eid: "news-overlap:" + digest(sorted(members)) for members in groups.values() for eid in members}


def build_geopolitical_dataset(archive, destination, *, through, assessments=None, market=None,
                               rules=GeopoliticalRules(), policy=OutcomePolicy()):
    rules.validate()
    policy.validate()
    if epoch_ns(through) > epoch_ns(utc_now()):
        raise ValueError("future news research cutoff")
    if Decimal(policy.fee_per_contract_side) != Decimal(rules.market().fee_per_contract_side) or policy.slippage_ticks_per_side != rules.market().slippage_ticks_per_side:
        raise ValueError("news decision and outcome costs must match")
    target = outside_snapshot(archive, destination)
    if target.exists():
        raise ValueError("output already exists")
    if assessments is not None and (target == Path(assessments).resolve() or target in Path(assessments).resolve().parents):
        raise ValueError("output overlaps assessment input")
    cutoff = epoch_ns(through)
    subjects = {k: r for k, r in events(archive).items() if epoch_ns(r["available_at"]) <= cutoff}
    # Retain all captured fast events, including multiple patterns on one story.
    subjects.update({k:r for k,r in archive.records.items() if r["kind"] == "fast_event" and epoch_ns(r["available_at"]) <= cutoff})
    reviews = history(archive, assessments, through=through) if assessments is not None else []
    market = market if market is not None else json.loads((archive.path.parent / "market.json").read_text())
    market = {"records": [r for r in market["records"] if epoch_ns(r.get("available_at") or r["payload"]["available_at"]) <= cutoff],
              "gaps": [g for g in market["gaps"] if not g.get("available_at") or epoch_ns(g["available_at"]) <= cutoff]}
    view = MarketView(market["records"], market["gaps"]) if market["records"] else None
    grouping = overlap_groups(archive, subjects, through)
    jobs = [(event, None) for event in subjects.values()]
    for review in reviews:
        eid = review["payload"]["event_id"]
        if eid not in subjects:
            raise ValueError("assessment event missing from snapshot")
        jobs.append((subjects[eid], review))
    rows = []
    for event, review in jobs:
        story = archive.stories[event["payload"]["story_revision_id"]]
        receipt = story["payload"]["local_received_at"]
        decision_ns = max(epoch_ns(event["available_at"]), epoch_ns(receipt) + rules.market().confirmation_seconds * 10**9,
                          epoch_ns(review["available_at"]) if review else 0)
        at = iso_ns(decision_ns)
        row = {"schema": "geopolitical-research-row-v1", "event_id": event["id"], "story_revision_id": story["id"],
            "story_hash": digest(story), "event_hash": digest(event), "publisher": story["payload"]["source_id"],
            "received_at": receipt, "candidate_available_at": event["available_at"], "decision_at": at,
            "assessment_id": review["id"] if review else None, "evaluation_group": grouping[event["id"]],
            "lane": "physical_transition" if review or event["payload"].get("event_type") in PHYSICAL_TYPES
                    or event["kind"] == "operational_review_candidate" else "risk_premium",
            "decision_clock": "counterfactual_max_receipt_plus_confirmation_candidate_and_review_availability",
            "decision": None, "features": None, "outcomes": None, "comparisons": None,
            "trade_authorized": False}
        if decision_ns > cutoff:
            row["state"] = "PENDING_DECISION"
        else:
            mapping = mapping_at(archive, at)
            transition = None
            if review:
                if review["bridge_version"] != ECONOMIC_VERSION or review["event_hash"] != digest(event) or review["story_hash"] != digest(story):
                    raise ValueError("physical assessment input mismatch")
                original = build_transition(event, story, review, mapping_at(archive, review["available_at"]),
                    evaluated_at=review["available_at"], evidence_stories=archive.stories)
                if original != review["transition_at_review"]:
                    raise ValueError("physical assessment reconstruction mismatch")
                transition = original
            features = inventory_features(view, receipt, at, rules.market()) if view else None
            raw = [archive.records.get(rid) for rid in story["payload"]["input_revision_ids"]]
            exclusions = [] if raw and all(r and r["kind"] == "observation" and not r["payload"].get("synthetic")
                and r["payload"].get("delivery") == "http" for r in raw) else ["NONPROSPECTIVE_CAPTURE"]
            if any(s["payload"]["story_id"] == story["payload"]["story_id"]
                   and epoch_ns(story["available_at"]) < epoch_ns(s["available_at"]) <= decision_ns
                   for s in archive.stories.values()):
                exclusions.append("SUPERSEDED_CAPTURED_STORY")
            peer_directions = {RISK_DIRECTION[e["payload"]["event_type"]] for e in subjects.values()
                if e["payload"].get("event_type") in RISK_DIRECTION
                and e["payload"]["story_revision_id"] == story["id"] and epoch_ns(e["available_at"]) <= decision_ns}
            if len(peer_directions) > 1:
                exclusions.append("CONFLICTING_HEADLINE_DIRECTIONS")
            if review and any(r["payload"].get("supersedes_assessment_id") == review["id"]
                              and epoch_ns(r["available_at"]) <= decision_ns for r in reviews):
                exclusions.append("ASSESSMENT_SUPERSEDED_BEFORE_DECISION")
            decision = news_decision(event, story, mapping, features, at=at, rules=rules,
                                     transition=transition, capture_exclusions=exclusions)
            row.update(state="EVALUATED", decision=decision, features=features, mapping_hash=digest(mapping),
                       transition=transition)
            if view:
                outcomes = forward_outcomes(view, features["roles"], at, policy)
                for h, outcome in outcomes["horizons"].items():
                    mature = decision_ns + int(h)*10**9 + policy.delay_ms*10**6 <= cutoff
                    outcome["label_state"] = "MATURED" if mature else "PENDING_HORIZON"
                    if not mature:
                        outcome["CL_return"] = None
                        outcome["instruments"] = {role: {side: {"status":"PENDING_HORIZON", "net_pnl":None}
                            for side in ("long", "short")} for role in ("CL1", "MCL1")}
                row.update(outcomes=outcomes, comparisons=comparisons(decision, outcomes,
                    decision["comparison_exclusions"], policy.contracts))
        row["id"] = digest(row)
        rows.append(row)
    rows.sort(key=lambda r:(epoch_ns(r["decision_at"]),r["id"]))
    target.mkdir(parents=True)
    with (target / "rows.jsonl").open("x") as stream:
        for row in rows: stream.write(canonical(row) + "\n")
    modes = sorted({e.data_mode for values in view.events.values() for e in values}) if view else []
    result = {"schema":"geopolitical-dataset-v1", "through":through, "rows":len(rows),
        "subjects":len(subjects), "evaluation_groups":len(set(grouping.values())),
        "rules":asdict(rules), "rules_hash":digest(asdict(rules)), "outcome_policy":asdict(policy),
        "manifest_sha256":file_hash(archive.path), "assessment_history_hash":digest(reviews), "market_hash":digest(market),
        "lanes":dict(Counter(r["decision"]["lane"] for r in rows if r["decision"])),
        "reason_counts":dict(Counter(x for r in rows if r["decision"] for x in r["decision"]["reason_codes"])),
        "market_modes":modes, "dataset_role":"engineering_fixture" if "fixture" in modes else "development_research" if modes else "unpriced_research",
        "files":{"rows.jsonl":file_hash(target / "rows.jsonl")}, "trade_authorized":False, "promotion":False,
        "limitations":["Risk directions are draft hypotheses, not observed supply losses or validated price forecasts.",
            "Only existing captured classifiers and separately dated operational assessments are used.",
            "Counterfactual decisions are not evidence of actual historical trades or a running strategy.",
            "Overlap groups include unreviewed links and equal headlines only to reduce evaluation leakage; they do not confirm identity.",
            "Unknown cross-group dependence remains; no independent episode-count or profitability claim.",
            "Publication timestamps are publisher-reported; missing, old and future values exclude comparisons.",
            "Future-to-decision links affect evaluation grouping only, never earlier decision inputs."]}
    atomic_json(target / "dataset.json", result)
    return result
