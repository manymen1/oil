"""Economic episodes derived from immutable incidents and dated assignments."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import re

from .clock import epoch_ns
from .schema import digest

CRUDE_ASSETS = {"maritime_route", "export_port", "crude_export_terminal", "oil_processing", "production"}


@dataclass(frozen=True)
class EventCluster:
    episode_id: str
    incident_ids: tuple[str, ...]
    operational_status: str
    available_at: str
    verified: bool


def assign_episode(analysis, *, episode_id, incident_ids, reason):
    if not episode_id or not incident_ids or not reason.strip():
        raise ValueError("episode, incident members and explicit reason required")
    known = {r["payload"]["incident_id"] for r in analysis.records("incident_revision")}
    if not set(incident_ids) <= known:
        raise ValueError("unknown incident")
    refs = [r["id"] for r in analysis.records("incident_revision") if r["payload"]["incident_id"] in incident_ids]
    return analysis.append("cluster_assignment", {"episode_id": episode_id, "incident_ids": sorted(set(incident_ids)),
                           "reason": reason, "input_revision_ids": refs, "transform": "cluster-assignment-v1"})


def duration_bucket(facts):
    values = []
    for fact in facts:
        if fact["field"] != "duration" or fact["assertion"] != "asserted":
            continue
        # Only a literal scalar duration. Ranges, forecasts, indefinite and negations stay unknown.
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(minutes?|hours?|days?)\s*", fact["value"], re.I)
        if match:
            amount = Decimal(match[1]) * (Decimal(1) / 60 if match[2].lower().startswith("minute") else 24 if match[2].lower().startswith("day") else 1)
            values.append(amount)
    if len(set(values)) != 1:
        return "UNKNOWN"
    hours = values[0]
    return "<1H" if hours < 1 else "1-6H" if hours < 6 else "6-24H" if hours < 24 else "1-3D" if hours < 72 else "3D+"


def severity(facts):
    # Nameplate/gross capacity never establishes actual lost supply or severity.
    for f in facts:
        if f["field"] == "reported_quantity" and f["assertion"] == "asserted" and f.get("quantity_kind") == "delivery_disruption" and Decimal(f["value"]) > 0:
            return "partial_export_loss"
    for f in facts:
        if f["field"] == "operational_status" and f["assertion"] == "asserted":
            if f["value"] == "suspended" and re.search(r"\b(?:all exports|all loading|entire terminal)\b", f["quote"], re.I):
                return "full_terminal_suspension"
    return "severity_unknown"


def cluster_transitions(records, assets):
    by_id = {r["id"]: r for r in records}
    asset_map = {a["id"]: a for a in assets}
    assignments, assignment_refs, states, members, latest = {}, {}, {}, {}, {}
    state_refs = {}
    result = []
    # Preserve journal order for equal available_at; never sort ties by random UUID.
    for row in sorted(records, key=lambda r: (epoch_ns(r["available_at"]), r.get("seq", 0))):
        p = row["payload"]
        if row["kind"] == "cluster_assignment":
            affected = {assignments.get(i, "unassigned:" + i) for i in p["incident_ids"]}
            affected.add(p["episode_id"])
            for incident in p["incident_ids"]:
                assignments[incident] = p["episode_id"]
                assignment_refs[incident] = row["id"]
            # An assignment takes effect now; it cannot rewrite past transitions.
            for episode in affected:
                group = [v for k, v in latest.items() if assignments.get(k, "unassigned:" + k) == episode]
                current = max(group, key=lambda r: (epoch_ns(r["available_at"]), r.get("seq", 0))) if group else None
                states[episode] = current["payload"]["operational_status"] if current else "unknown"
                state_refs[episode] = current["id"] if current else None
                members[episode] = {i for i, assigned in assignments.items() if assigned == episode}
            continue
        if row["kind"] != "incident_revision":
            continue
        incident = p["incident_id"]
        episode = assignments.get(incident, "unassigned:" + incident)
        before = states.get(episode, "unknown")
        before_ref = state_refs.get(episode)
        story = next((by_id[ref] for ref in p["input_revision_ids"] if by_id[ref]["kind"] == "story_revision"), None)
        if story is None:
            raise ValueError("incident missing source story")
        s = story["payload"]
        primary = p["evidence_status"] == "primary_operational_report" and s["source_role"] in {"operator", "port_authority", "maritime_authority"}
        after = p["operational_status"]
        if primary or p["evidence_status"] in {"withdrawn", "disputed"}:
            states[episode] = after
            state_refs[episode] = row["id"]
            latest[incident] = row
        else:
            after = before  # An unverified report does not overwrite established operations.
        members.setdefault(episode, set()).add(incident)
        if before == after or before == "unknown" and after in {"operating", "restored"}:
            continue
        family, direction, mechanism = "other", 0, "unknown"
        if before in {"operating", "restored"} and after in {"impaired", "suspended"}:
            family, direction, mechanism = "physical_disruption", 1, "physical_supply_available_down"
        elif before in {"impaired", "suspended", "partly_restored"} and after in {"partly_restored", "restored"}:
            family, direction, mechanism = "restoration", -1, "physical_supply_available_up"
        selected_assets = [asset_map[a] for a in p["asset_ids"] if a in asset_map]
        registries = [by_id[ref]["payload"]["assets"] for ref in p["input_revision_ids"] if by_id[ref]["kind"] == "asset_registry"]
        if registries:
            selected_assets = [a for a in registries[0] if a["id"] in p["asset_ids"]]
        refs = list(p["input_revision_ids"]) + [row["id"]]
        if before_ref:
            refs.append(before_ref)
        if incident in assignment_refs:
            refs.append(assignment_refs[incident])
        extraction = next((by_id[ref] for ref in p["input_revision_ids"] if by_id[ref]["kind"] == "extraction"), None)
        result.append({"event_id": digest(["cluster-transition-v1", row["id"], episode, before, after]),
                       "incident_id": incident, "episode_id": episode, "episode_verified": incident in assignments,
                       "cluster": asdict(EventCluster(episode, tuple(sorted(members[episode])), after, row["available_at"], incident in assignments)),
                       "event_family": family, "event_transition": before + "_to_" + after,
                       "direction": direction, "mechanism": mechanism,
                       "operational_status_before": before, "operational_status_after": after,
                       "assets": p["asset_ids"], "asset_types": [a["type"] for a in selected_assets],
                       "geography": sorted({a["geography"] for a in selected_assets}),
                       "source": s["source_id"], "source_role": s["source_role"],
                       "source_authority": "primary_operator" if primary and s["source_role"] == "operator" else "primary_authority" if primary else "unverified",
                       "confirmation_level": p["evidence_status"], "contradiction_flag": bool(p["contradictions"]),
                       "published_at": s.get("published_at"), "received_at": s["observed_at"],
                       "classified_at": extraction["available_at"] if extraction else row["available_at"], "decision_at": row["available_at"],
                       "novelty": bool(p["novel"]), "initial_snapshot": p.get("initial_snapshot", True),
                       "late": p["late"], "reported_quantities": [f for f in p["facts"] if f["field"] == "reported_quantity"],
                       "estimated_duration_bucket": duration_bucket(p["facts"]), "severity": severity(p["facts"]),
                       "input_revision_ids": sorted(set(refs))})
    return result
