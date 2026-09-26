"""Append-only human relation decisions and reconstructable as-of mappings."""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from .clock import instant, utc_now
from .schema import digest

DECISIONS = {"SAME_EVENT", "SAME_EPISODE", "SYNDICATED_REPORT", "UNRELATED", "UNCERTAIN"}


def review_link(output, candidate_link_id, decision, reason, reviewer, *, supersedes_review_id=None, review_id=None):
    if decision not in DECISIONS or not reason.strip() or not reviewer.strip():
        raise ValueError("valid decision, nonempty reason and reviewer required")
    with output.transaction() as db:
        db.execute("CREATE INDEX IF NOT EXISTS forward_review_pair ON records(json_extract(payload,'$.pair_key'),seq) WHERE kind='forward_link_review'")
        candidate = db.execute("SELECT kind,payload FROM records WHERE id=?", (candidate_link_id,)).fetchone()
        if candidate is None or candidate["kind"] != "candidate_episode_link":
            raise ValueError("review requires a candidate_episode_link")
        p = json.loads(candidate["payload"])
        left, right = p["from_event_id"], p["event_id"]
        if left == right:
            raise ValueError("cannot review a self-link")
        events = []
        for event_id in (left, right):
            row = db.execute("SELECT kind,payload FROM records WHERE id=?", (event_id,)).fetchone()
            if row is None or row["kind"] != "fast_event":
                raise ValueError("review endpoints must be captured fast events")
            events.append(json.loads(row["payload"]))
        if decision == "SAME_EVENT" and events[0]["event_type"] != events[1]["event_type"]:
            raise ValueError("different event types require SAME_EPISODE, not SAME_EVENT")
        pair = digest(sorted([left, right]))
        supplied = {"candidate_link_id": candidate_link_id, "left_event_id": left, "right_event_id": right,
                    "decision": decision, "reason": reason.strip(), "reviewer": reviewer.strip(),
                    "supersedes_review_id": supersedes_review_id, "pair_key": pair}
        rid = review_id or str(uuid.uuid4())
        existing = db.execute("SELECT kind,payload FROM records WHERE id=?", (rid,)).fetchone()
        if existing:
            prior = json.loads(existing["payload"])
            if existing["kind"] != "forward_link_review" or any(prior.get(k) != v for k, v in supplied.items()):
                raise ValueError("review ID collision")
            return rid
        latest = db.execute("SELECT id FROM records WHERE kind='forward_link_review' AND json_extract(payload,'$.pair_key')=? ORDER BY seq DESC LIMIT 1", (pair,)).fetchone()
        if (latest[0] if latest else None) != supersedes_review_id:
            raise ValueError("supersedes-review must identify the latest review of this event pair")
        at = utc_now()
        output.append("forward_link_review", {**supplied, "review_id": rid, "reviewed_at": at,
            "confirmation_granted": False, "input_revision_ids": [candidate_link_id, left, right] +
            ([supersedes_review_id] if supersedes_review_id else [])}, available_at=at, record_id=rid, db=db)
        return rid


def episode_map(path, *, through=None):
    """Derive mappings from immutable reviews only, never from mutable indexes.

    SAME_EVENT relates event identity. SAME_EPISODE adds episode membership.
    SYNDICATED_REPORT relates reporting provenance, not physical confirmation.
    An UNRELATED edge inside a transitive positive component quarantines the
    entire component rather than silently overriding the negative review.
    """
    at = instant(through or utc_now()).isoformat(timespec="microseconds")
    path = Path(path)
    if not path.exists():
        return {"as_of": at, "event_groups": [], "episodes": [], "syndication_groups": [], "conflicts": []}
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        events = {r[0] for r in db.execute("SELECT id FROM records WHERE kind='fast_event' AND available_at<=?", (at,))}
        reviews = [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute(
            "SELECT id,payload FROM records WHERE kind='forward_link_review' AND available_at<=? ORDER BY seq", (at,))]
        states = {}
        for r in db.execute("SELECT json_extract(payload,'$.event_id'),json_extract(payload,'$.state') FROM records WHERE kind='forward_evidence_transition' AND available_at<=? ORDER BY seq", (at,)):
            states[r[0]] = r[1]
    finally:
        db.close()
    active = {}
    for review in reviews:
        p = review["payload"]
        active[p["pair_key"]] = review
    conflicts = []

    def groups(kind, decisions):
        parent = {eid: eid for eid in events}
        def root(eid):
            while parent[eid] != eid:
                parent[eid] = parent[parent[eid]]
                eid = parent[eid]
            return eid
        for review in active.values():
            p = review["payload"]
            if p["left_event_id"] not in events or p["right_event_id"] not in events:
                raise ValueError("review has missing as-of event input")
            if p["decision"] in decisions:
                a, b = root(p["left_event_id"]), root(p["right_event_id"])
                parent[max(a, b)] = min(a, b)
        components = {}
        for eid in sorted(events):
            components.setdefault(root(eid), []).append(eid)
        blocked = set()
        for review in active.values():
            p = review["payload"]
            a, b = root(p["left_event_id"]), root(p["right_event_id"])
            if p["decision"] == "UNRELATED" and a == b:
                blocked.add(a)
                conflicts.append({"mapping": kind, "event_ids": components[a], "negative_review_id": review["id"],
                                  "reason": "UNRELATED_WITHIN_POSITIVE_COMPONENT"})
        output = []
        for root_id, members in sorted(components.items()):
            for group in ([[eid] for eid in members] if root_id in blocked else [members]):
                output.append({"id": kind + ":" + digest(group), "event_ids": group,
                               "review_required": root_id in blocked})
        return output

    result = {"as_of": at, "event_groups": groups("event", {"SAME_EVENT"}),
              "episodes": groups("episode", {"SAME_EVENT", "SAME_EPISODE"}),
              "syndication_groups": groups("syndication", {"SYNDICATED_REPORT"}),
              "active_review_ids": sorted(r["id"] for r in active.values()), "conflicts": conflicts,
              "evidence_states": {eid: states.get(eid, "LEGACY_UNMODELED") for eid in sorted(events)},
              "confirmation_granted": False}
    return result
