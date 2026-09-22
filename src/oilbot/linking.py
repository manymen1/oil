"""Literal event slots and bounded, review-only cross-headline associations."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .clock import epoch_ns, instant
from .schema import canonical

LINK_VERSION = "candidate-links-v1"
WINDOW_SECONDS = 6 * 3600
SEARCH_LIMIT = 500
OUTPUT_LIMIT = 10
ACTORS = {"irgc": ["IRGC", "Islamic Revolutionary Guard Corps"],
          "centcom": ["CENTCOM", "US Central Command"], "iran": ["Iran", "Iranian"],
          "opec": ["OPEC"], "ukmto": ["UKMTO"], "aramco": ["Aramco"], "adnoc": ["ADNOC"],
          "ofac": ["OFAC"], "houthis": ["Houthis", "Houthi"], "israel": ["Israel", "Israeli"]}
VESSEL = re.compile(r'''\b(?i:tanker|vessel|ship)\s+(?:(?i:named)\s+)?(?:["“'](?P<quoted>[^"”'\n]{2,60})["”']|(?P<caps>[A-Z][A-Z0-9-]+(?:\s+[A-Z][A-Z0-9-]+){0,3})\b)''')
IMO = re.compile(r"\bIMO\s*[:#]?\s*(\d{7})\b")
EVENT_TIME = re.compile(r"\b(?:at|event time:)\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))", re.I)


def literal_slots(headline, assets):
    evidence, actors, vessels, facilities, locations, times = [], set(), set(), set(), set(), set()

    def add(field, value, match, group=0):
        evidence.append({"field": field, "value": value, "text_field": "title",
                         "start": match.start(group), "end": match.end(group), "quote": match.group(group)})

    for actor, aliases in ACTORS.items():
        for alias in aliases:
            for match in re.finditer(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", headline, re.I):
                actors.add(actor)
                add("actors", actor, match)
    for asset in assets:
        for alias in asset["aliases"]:
            for match in re.finditer(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", headline, re.I):
                locations.add(asset["id"])
                add("location", asset["id"], match)
                if asset.get("type") != "maritime_route":
                    facilities.add(asset["id"])
                    add("asset", asset["id"], match)
    for match in VESSEL.finditer(headline):
        group = "quoted" if match["quoted"] else "caps"
        value = " ".join(match[group].casefold().split())
        # Generic/actor labels must not be invented as vessel identities.
        if value in {"irgc", "us", "iran", "ukmto", "lng", "lpg", "vlcc"}:
            continue
        if group == "caps" and set(value.split()).intersection({"hit", "struck", "attacked", "seized", "was", "is", "in", "at", "near", "says", "reported", "reports"}):
            continue
        vessels.add("name:" + value)
        add("vessel", "name:" + value, match, group)
    for match in IMO.finditer(headline):
        vessels.add("imo:" + match[1])
        add("vessel", "imo:" + match[1], match, 1)
    for match in EVENT_TIME.finditer(headline):
        try:
            value = instant(match[1]).isoformat()
        except ValueError:
            continue
        times.add(value)
        add("event_time_if_explicit", value, match, 1)
    targets = sorted(vessels) or sorted(facilities)
    return {"actor": next(iter(actors)) if len(actors) == 1 else None, "actors": sorted(actors),
            "target": targets[0] if len(targets) == 1 else None,
            "asset": next(iter(facilities)) if len(facilities) == 1 else None,
            "assets": sorted(facilities), "vessels": sorted(vessels),
            "location": next(iter(locations)) if len(locations) == 1 else None,
            "locations": sorted(locations),
            "event_time_if_explicit": next(iter(times)) if len(times) == 1 else None,
            "literal_evidence": evidence}


def initialize_index(db):
    db.execute("""CREATE TABLE IF NOT EXISTS forward_link_index (
        event_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, event_type TEXT NOT NULL,
        received_ns INTEGER NOT NULL, payload TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS forward_link_window ON forward_link_index(event_type,received_ns DESC,event_id)")


def matching_reasons(current, prior):
    reasons = ["SAME_EVENT_TYPE", "CLOSE_RECEIPT_TIME"]
    for key in ("assets", "locations"):
        left, right = set(current[key]), set(prior[key])
        if left and right and not left.intersection(right):
            return []
    # Reject explicit conflicting IMO identities or names, while allowing an
    # IMO-only item and a name-only item to remain uncertain (never equate them).
    for prefix in ("imo:", "name:"):
        left = {v for v in current["vessels"] if v.startswith(prefix)}
        right = {v for v in prior["vessels"] if v.startswith(prefix)}
        if left and right and not left.intersection(right):
            return []
    if current["claim_origin"] and current["claim_origin"] == prior["claim_origin"]:
        reasons.append("SAME_CLAIM_ORIGIN")
    for key, label in (("assets", "SAME_FACILITY"), ("vessels", "SAME_VESSEL"),
                       ("locations", "SAME_LOCATION"), ("actors", "SHARED_ACTOR")):
        if set(current[key]).intersection(prior[key]):
            reasons.append(label)
    if current["event_time_if_explicit"] and prior["event_time_if_explicit"]:
        gap = abs(epoch_ns(current["event_time_if_explicit"]) - epoch_ns(prior["event_time_if_explicit"])) / 1e9
        if gap > 3600:
            return []
        reasons.append("CLOSE_EXPLICIT_EVENT_TIME")
    named = bool({"SAME_FACILITY", "SAME_VESSEL"}.intersection(reasons))
    context = bool({"SAME_CLAIM_ORIGIN", "SHARED_ACTOR"}.intersection(reasons))
    if not ((named and (context or "SAME_LOCATION" in reasons)) or (context and "SAME_LOCATION" in reasons)):
        return []
    return reasons


def candidate_links(db, event, incident_id):
    received = epoch_ns(event["received_at"])
    window = WINDOW_SECONDS * 1_000_000_000
    rows = db.execute("""SELECT event_id,incident_id,received_ns,payload FROM forward_link_index
        WHERE event_type=? AND received_ns BETWEEN ? AND ?
        ORDER BY received_ns DESC,event_id LIMIT ?""",
        (event["event_type"], received - window, received + window, SEARCH_LIMIT + 1)).fetchall()
    candidates = []
    for row in rows[:SEARCH_LIMIT]:
        if row["incident_id"] == incident_id:
            continue
        prior = json.loads(row["payload"])
        reasons = matching_reasons(event, prior)
        if not reasons:
            continue
        candidates.append({"event_id": row["event_id"], "incident_id": row["incident_id"],
            "reasons": reasons, "receipt_distance_seconds": abs(received - row["received_ns"]) / 1e9,
            "strength": "NAMED_ENTITY" if {"SAME_VESSEL", "SAME_FACILITY"}.intersection(reasons) else "CONTEXT_ONLY",
            "review_required": True, "merge_authorized": False, "transform": LINK_VERSION})
    candidates.sort(key=lambda c: (c["strength"] != "NAMED_ENTITY", -len(c["reasons"]), c["receipt_distance_seconds"], c["event_id"]))
    return candidates[:OUTPUT_LIMIT], len(rows) > SEARCH_LIMIT, len(candidates) > OUTPUT_LIMIT


def index_event(db, event_id, event):
    fields = {key: event[key] for key in ("claim_origin", "assets", "vessels", "locations", "actors", "event_time_if_explicit")}
    db.execute("INSERT INTO forward_link_index VALUES(?,?,?,?,?)", (event_id, event["incident_id"],
               event["event_type"], epoch_ns(event["received_at"]), canonical(fields)))


def read_candidates(path, *, after_seq=0, limit=100):
    """Read-only, paginated review queue; does not accept or apply a link."""
    if not 1 <= limit <= 1000 or after_seq < 0:
        raise ValueError("limit must be 1..1000 and after-seq must be nonnegative")
    path = Path(path)
    if not path.exists():
        return {"links": [], "next_after_seq": after_seq, "more": False}
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute("SELECT seq,id,available_at,payload FROM records WHERE kind='candidate_episode_link' AND seq>? ORDER BY seq LIMIT ?",
                                          (after_seq, limit + 1))]
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return {"links": rows[:limit], "next_after_seq": rows[min(limit, len(rows)) - 1]["seq"] if rows else after_seq,
                "more": len(rows) > limit}
    finally:
        db.close()
