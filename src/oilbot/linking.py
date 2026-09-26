"""Independent, versioned, review-only event/episode candidate derivation."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .clock import epoch_ns, utc_now
from .schema import canonical, digest
from .entities import literal_slots  # Compatibility import; classification owns extraction.

LINK_VERSION = "candidate-links-v2"
WINDOW_SECONDS = 6 * 3600
EPISODE_WINDOW_SECONDS = 3 * 86400
SEARCH_LIMIT = 500
OUTPUT_LIMIT = 10
EPISODE_PAIRS = (
    ("TANKER_ATTACK", "SHIPPING_RESTRICTION"), ("TANKER_ATTACK", "SHIPPING_RESTORED"),
    ("SHIPPING_RESTRICTION", "SHIPPING_RESTORED"),
    ("MILITARY_STRIKE", "PRODUCTION_SUSPENDED"), ("MILITARY_STRIKE", "PRODUCTION_RESTORED"),
    ("PRODUCTION_SUSPENDED", "PRODUCTION_RESTORED"),
    ("EXPORT_TERMINAL_CLOSED", "EXPORT_TERMINAL_REOPENED"),
    ("PIPELINE_OUTAGE", "PIPELINE_RESTORED"), ("CEASEFIRE_REACHED", "SHIPPING_RESTORED"),
)


def initialize_index(db):
    # Older v1 index/links remain untouched. Each policy owns its own derivation.
    db.execute("""CREATE TABLE IF NOT EXISTS linker_event_index (
        policy_hash TEXT NOT NULL, event_id TEXT NOT NULL, incident_id TEXT NOT NULL,
        event_type TEXT NOT NULL, received_ns INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(policy_hash,event_id))""")
    db.execute("CREATE INDEX IF NOT EXISTS linker_event_window ON linker_event_index(policy_hash,event_type,received_ns DESC,event_id)")


def normalized_event(event, story):
    return {**event, "headline": event.get("headline", story.get("title", "")),
            "assets": event.get("asset_ids", event.get("assets", [])),
            "locations": event.get("location_ids", event.get("locations", event.get("geography", []))),
            "actors": event.get("actors", []), "vessels": event.get("vessels", []),
            "event_time_if_explicit": event.get("event_time_if_explicit"),
            "claim_origin": event.get("claim_origin")}


def matching_reasons(current, prior, *, episode=False):
    reasons = ["COMPATIBLE_EVENT_TYPES" if episode else "SAME_EVENT_TYPE", "CLOSE_RECEIPT_TIME"]
    for key in ("assets", "locations"):
        left, right = set(current[key]), set(prior[key])
        if left and right and not left.intersection(right):
            return []
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
        if gap > (EPISODE_WINDOW_SECONDS if episode else 3600):
            return []
        reasons.append("CLOSE_EXPLICIT_EVENT_TIME")
    headline = lambda text: re.sub(r"\W+", " ", text.casefold()).strip()
    if not episode and headline(current["headline"]) and headline(current["headline"]) == headline(prior["headline"]):
        reasons.append("EXACT_HEADLINE")
        return reasons
    named = bool({"SAME_FACILITY", "SAME_VESSEL"}.intersection(reasons))
    context = bool({"SAME_CLAIM_ORIGIN", "SHARED_ACTOR"}.intersection(reasons))
    if not ((named and (context or "SAME_LOCATION" in reasons)) or (context and "SAME_LOCATION" in reasons)):
        return []
    return reasons


def candidate_links(db, event, policy_hash, *, episode=False):
    received = epoch_ns(event["received_at"])
    window = (EPISODE_WINDOW_SECONDS if episode else WINDOW_SECONDS) * 1_000_000_000
    types = {event["event_type"]}
    if episode:
        types = {b if a == event["event_type"] else a for a, b in EPISODE_PAIRS if event["event_type"] in (a, b)}
    rows = []
    # A bounded indexed seek per compatible type; no all-history sort/scan.
    for event_type in sorted(types):
        rows.extend(db.execute("""SELECT event_id,incident_id,received_ns,payload FROM linker_event_index
            WHERE policy_hash=? AND event_type=? AND received_ns BETWEEN ? AND ?
            ORDER BY received_ns DESC,event_id LIMIT ?""",
            (policy_hash, event_type, received - window, received + window, SEARCH_LIMIT + 1)).fetchall())
    rows.sort(key=lambda r: (-r["received_ns"], r["event_id"]))
    candidates = []
    for row in rows[:SEARCH_LIMIT]:
        if not episode and row["incident_id"] == event["incident_id"]:
            continue
        prior = json.loads(row["payload"])
        reasons = matching_reasons(event, prior, episode=episode)
        if not reasons:
            continue
        strength = ("EXACT_HEADLINE" if "EXACT_HEADLINE" in reasons else
                    "NAMED_ENTITY" if {"SAME_VESSEL", "SAME_FACILITY"}.intersection(reasons) else "CONTEXT_ONLY")
        candidates.append({"event_id": row["event_id"], "incident_id": row["incident_id"],
            "relation_type": "SAME_EPISODE_CANDIDATE" if episode else "SAME_EVENT_CANDIDATE",
            "reasons": reasons, "receipt_distance_seconds": abs(received - row["received_ns"]) / 1e9,
            "strength": strength, "review_required": True, "merge_authorized": False})
    candidates.sort(key=lambda c: ({"EXACT_HEADLINE": 0, "NAMED_ENTITY": 1, "CONTEXT_ONLY": 2}[c["strength"]],
                                   -len(c["reasons"]), c["receipt_distance_seconds"], c["event_id"]))
    return candidates[:OUTPUT_LIMIT], len(rows) > SEARCH_LIMIT, len(candidates) > OUTPUT_LIMIT


class LinkerWorker:
    def __init__(self, news, output, *, version=LINK_VERSION):
        self.news, self.output, self.version = news, output, version
        policy = {"version": version, "same_event_window": WINDOW_SECONDS, "episode_window": EPISODE_WINDOW_SECONDS,
                  "compatibility": EPISODE_PAIRS, "search_limit": SEARCH_LIMIT, "output_limit": OUTPUT_LIMIT}
        self.policy_hash = digest(policy)
        self.cursor_key = "linker:seq:" + self.policy_hash
        self.policy_id = digest(["linker-policy", self.policy_hash])
        with output.transaction() as db:
            initialize_index(db)
            if not db.execute("SELECT 1 FROM records WHERE id=?", (self.policy_id,)).fetchone():
                at = utc_now()
                output.append("linker_policy_revision", {"policy": policy, "linker_version": version,
                    "linker_policy_hash": self.policy_hash, "effective_at": at, "relinking_existing_events": True},
                    available_at=at, record_id=self.policy_id, db=db)

    def run_once(self, limit=200):
        if not 1 <= limit <= 1000:
            raise ValueError("linker batch limit must be 1..1000")
        offset = self.output.cursor(self.cursor_key, 0)
        with self.output.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM records WHERE kind='fast_event' AND seq>? ORDER BY seq LIMIT ?",
                                               (offset, limit + 1))]
        result = {"processed": 0, "candidate_links": 0, "pending": len(rows) > limit}
        for row in rows[:limit]:
            event = json.loads(row["payload"])
            story = self.news.get(event["story_revision_id"])
            if story is None or story["kind"] != "story_revision":
                raise ValueError("linker event missing source story")
            event = normalized_event(event, story["payload"])
            with self.output.transaction() as db:
                old = db.execute("SELECT value FROM cursors WHERE key=?", (self.cursor_key,)).fetchone()
                if old and json.loads(old[0]) >= row["seq"]:
                    continue
                at, searches = utc_now(), []
                for episode in (False, True):
                    links, search_truncated, links_truncated = candidate_links(db, event, self.policy_hash, episode=episode)
                    relation = "SAME_EPISODE_CANDIDATE" if episode else "SAME_EVENT_CANDIDATE"
                    searches.append({"relation_type": relation, "search_truncated": search_truncated,
                                     "links_truncated": links_truncated, "emitted": len(links)})
                    for link in links:
                        payload = {**link, "from_event_id": row["id"], "from_incident_id": event["incident_id"],
                            "left_event_id": row["id"], "right_event_id": link["event_id"], "state": "PENDING_REVIEW",
                            "linker_version": self.version, "linker_policy_hash": self.policy_hash,
                            "search_truncated": search_truncated, "links_truncated": links_truncated,
                            "input_revision_ids": [row["id"], link["event_id"], self.policy_id]}
                        self.output.append("candidate_episode_link", payload, available_at=at,
                            record_id=digest([self.policy_hash, row["id"], link["event_id"], relation]), db=db)
                    result["candidate_links"] += len(links)
                db.execute("INSERT INTO linker_event_index VALUES(?,?,?,?,?,?)",
                    (self.policy_hash, row["id"], event["incident_id"], event["event_type"],
                     epoch_ns(event["received_at"]), canonical(event)))
                self.output.append("linker_processing", {"event_id": row["id"], "searches": searches,
                    "linker_version": self.version, "linker_policy_hash": self.policy_hash,
                    "input_revision_ids": [row["id"], self.policy_id]}, available_at=at, db=db)
                self.output.set_cursor(db, self.cursor_key, row["seq"])
            result["processed"] += 1
        return result


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
