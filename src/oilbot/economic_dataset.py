"""Forward operational assessments -> research rows, never strategy decisions.

Keeps every assessment revision and excluded subject. Uses original assessment
availability, not report/export time. The legacy strategy training format is
deliberately separate: this contract cannot silently authorize or train orders.
"""
from dataclasses import asdict
import json
from pathlib import Path

from .clock import epoch_ns, instant, utc_now
from .databento import file_hash
from .economic import VERSION, build_transition
from .economic_review import events, history, mapping_at, outside_snapshot
from .features import MarketView, RECEIPT_VERSION
from .market import atomic_json, read_archive
from .outcomes import OutcomePolicy, forward_outcomes
from .schema import canonical, digest


def build_economic_dataset(archive, assessments, destination, *, market_root=None, through=None,
                           policy=OutcomePolicy(), roll_days=5, admissions=None):
    policy.validate()
    at = through or utc_now()
    if instant(at) > instant(utc_now()):
        raise ValueError("cannot export future assessments")
    target = outside_snapshot(archive, destination)
    if target.exists():
        raise ValueError("output already exists")
    rows = history(archive, assessments, through=at)
    from .assessment_queue import snapshot_resolutions
    admission_rows = snapshot_resolutions(archive, admissions, through=at) if admissions is not None else []
    captured = {eid: r for eid,r in events(archive).items() if instant(r["available_at"]) <= instant(at)}
    if market_root is None:
        market = json.loads((archive.path.parent / "market.json").read_text())
    else:
        records, gaps = read_archive(Path(market_root))
        market = {"records": records, "gaps": gaps}
    # Outcome labels must not use records unavailable at the dataset cutoff.
    market = {"records": [r for r in market["records"] if instant(r.get("available_at") or r["payload"]["available_at"]) <= instant(at)],
              "gaps": [g for g in market["gaps"] if not g.get("available_at") or instant(g["available_at"]) <= instant(at)]}
    view = MarketView(market["records"], market["gaps"])
    result = []
    latest = {r["payload"]["event_id"]: r["id"] for r in rows}
    for assessment in rows:
        a = assessment["payload"]
        event = captured.get(a["event_id"])
        story = archive.stories.get(a["story_revision_id"])
        if not event or not story or digest(event) != assessment["event_hash"] or digest(story) != assessment["story_hash"]:
            raise ValueError("assessment input hash mismatch")
        if assessment["bridge_version"] != VERSION:
            raise ValueError("assessment contract migration required; original timestamps must not be relabeled")
        transition = assessment["transition_at_review"]
        p = transition["payload"]
        if (transition["id"] != digest(p) or p["assessment_id"] != assessment["id"]
                or p["decision_at"] != assessment["available_at"] or p["schema"] != VERSION):
            raise ValueError("original transition binding mismatch")
        rebuilt = build_transition(event, story, assessment, mapping_at(archive, assessment["available_at"]),
            evaluated_at=assessment["available_at"], evidence_stories=archive.stories)
        if rebuilt != transition:
            raise ValueError("original transition reconstruction mismatch")
        features = view.features(p["received_at"], p["decision_at"], roll_days=roll_days,
                                 max_age_seconds=policy.max_quote_age_seconds)
        features["receipt_response"] = view.receipt_response(p["received_at"], p["classifier_available_at"],
            p["decision_at"], roll_days=roll_days, max_age_seconds=policy.max_quote_age_seconds)
        features["features_hash"] = digest({k: v for k,v in features.items() if k != "features_hash"})
        outcomes = forward_outcomes(view, features["roles"], p["decision_at"], policy)
        # A still-fresh quote must never fabricate completion of a future exit.
        for horizon, outcome in outcomes["horizons"].items():
            mature = epoch_ns(p["decision_at"]) + int(horizon) * 10**9 + policy.delay_ms * 10**6 <= epoch_ns(at)
            outcome["label_state"] = "MATURED" if mature else "PENDING_HORIZON"
            if not mature:
                outcome["CL_return"] = None
                outcome["instruments"] = {role: {"mid_return": None,
                    **{side: {"status": "PENDING_HORIZON", "net_pnl": None} for side in ("long", "short")}}
                    for role in ("CL1", "MCL1")}
        result.append({"id": transition["id"], **p, "features": features, "outcomes": outcomes,
            "assessment_payload_hash": digest(a), "latest_assessment_at_export": latest[a["event_id"]] == assessment["id"],
            "action": "ABSTAIN", "trade_authorized": False,
            "reason_codes": ["RESEARCH_ONLY", "NO_VALIDATED_EXECUTION_POLICY"],
            "supersedes_assessment_id": a.get("supersedes_assessment_id")})
    eligible = {row["event_id"] for row in result if row["latest_assessment_at_export"] and row["research_eligible"]}
    subjects = [{"event_id": eid, "candidate_kind": e["kind"], "story_revision_id": e["payload"]["story_revision_id"],
                 "state": "ASSESSED" if eid in eligible else "ABSTAINED" if eid in latest else "PENDING", "latest_assessment_id": latest.get(eid)}
                for eid,e in sorted(captured.items())]
    target.mkdir(parents=True)
    for name, items in (("transitions.jsonl", result), ("subjects.jsonl", subjects), ("admissions.jsonl", admission_rows)):
        with (target / name).open("x") as stream:
            for row in items:
                stream.write(canonical(row) + "\n")
    modes = sorted({e.data_mode for items in view.events.values() for e in items})
    metadata = {"schema": "operational-research-dataset-v2", "contract": VERSION, "through": at,
        "receipt_feature_version": RECEIPT_VERSION,
        "admission_resolutions": len(admission_rows), "admission_history_hash": digest(admission_rows),
        "source_manifest_sha256": file_hash(archive.path), "assessment_history_hash": digest(rows),
        "market_hash": digest(market), "market_modes": modes, "rows": len(result), "subjects": len(subjects),
        "pending_subjects": len(captured.keys() - latest.keys()), "outcome_policy": asdict(policy),
        "files": {n: file_hash(target / n) for n in ("transitions.jsonl", "subjects.jsonl", "admissions.jsonl")},
        "dataset_role": "engineering_fixture" if "fixture" in modes else "unpriced_research" if not modes else "development_research",
        "trade_authorized": False, "promotion": False,
        "limitations": ["All assessment revisions are retained, including abstentions and superseded judgments.",
                        "Availability is original review time; this is not hypothetical historical execution at receipt.",
                        "Assistant judgments are not independent labels; singleton episodes are not independent samples.",
                        "Existing CL/MCL feature roles are research schema, not a selected broker or execution instrument.",
                        "No strategy fitting or execution authorization; no qualified market data means missing outcomes."]}
    atomic_json(target / "dataset.json", metadata)
    return metadata
