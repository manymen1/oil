"""Forward-only, local deterministic discovery. Never a truth or trading engine.

Regex hits are review candidates with literal evidence, not verified facts.
Availability is processing time; receipt time is retained separately for research.
"""
from __future__ import annotations

import json
import re

from .clock import instant, seconds, utc_now
from .schema import digest
from .store import Journal
from .linking import LINK_VERSION, literal_slots, initialize_index, candidate_links, index_event

VERSION = "fast-event-v2"
# Narrow headline patterns intentionally trade recall for inspectability. Bodies
# are retained in the news journal, but historical context is not classified.
RULES = {
    "TANKER_ATTACK": r"\b(?:tanker|vessel|ship)\b.{0,45}\b(?:attacked|struck|hit)\b",
    "TANKER_SEIZURE": r"\b(?:tanker|vessel|ship)\b.{0,45}\b(?:seized|detained)\b",
    "MILITARY_STRIKE": r"\b(?:military strike|airstrike|air strike)\b",
    "MISSILE_ATTACK": r"\bmissile (?:attack|strike)s?\b",
    "DRONE_ATTACK": r"\bdrone (?:attack|strike)s?\b",
    "SHIPPING_RESTRICTION": r"\b(?:shipping|transit|strait)\b.{0,45}\b(?:closed|halted|restricted|suspended)\b",
    "SHIPPING_RESTORED": r"\b(?:shipping|transit|strait)\b.{0,45}\b(?:reopened|resumed|restored)\b",
    "PRODUCTION_SUSPENDED": r"\bproduction\b.{0,30}\b(?:suspended|halted|stopped)\b",
    "PRODUCTION_RESTORED": r"\bproduction\b.{0,30}\b(?:restored|resumed|restarted)\b",
    "EXPORT_TERMINAL_CLOSED": r"\b(?:terminal|loading|exports)\b.{0,30}\b(?:closed|halted|suspended)\b",
    "EXPORT_TERMINAL_REOPENED": r"\b(?:terminal|loading|exports)\b.{0,30}\b(?:reopened|resumed|restored)\b",
    "PIPELINE_OUTAGE": r"\bpipeline\b.{0,30}\b(?:outage|closed|halted|suspended)\b",
    "PIPELINE_RESTORED": r"\bpipeline\b.{0,30}\b(?:reopened|resumed|restored)\b",
    "SANCTIONS_TIGHTENED": r"\b(?:imposes?|tightens?|expands?|new)\b.{0,25}\bsanctions\b",
    "SANCTIONS_EASED": r"\b(?:lifts?|eases?|removes?)\b.{0,25}\bsanctions\b",
    "OPEC_OUTPUT_CUT": r"\bOPEC\+?\b.{0,45}\b(?:cuts?|reduces?)\b.{0,20}\b(?:output|production)\b",
    "OPEC_OUTPUT_INCREASE": r"\bOPEC\+?\b.{0,45}\b(?:raises?|increases?)\b.{0,20}\b(?:output|production)\b",
    "CEASEFIRE_REACHED": r"\bceasefire\b.{0,25}\b(?:agreed|reached|announced)\b",
    "CEASEFIRE_BROKEN": r"\bceasefire\b.{0,25}\b(?:broken|violated|collapsed)\b",
    "NEGOTIATIONS_STARTED": r"\b(?:talks|negotiations)\b.{0,25}\b(?:started|began|resumed)\b",
    "NEGOTIATIONS_COLLAPSED": r"\b(?:talks|negotiations)\b.{0,25}\b(?:collapsed|failed|halted)\b",
}
ATTRIBUTION = re.compile(
    r"\b(?P<actor>IRGC|CENTCOM|UKMTO|OPEC|Iran|Aramco|ADNOC)\s+"
    r"(?:says|said|reports|reported|claims|claimed|confirms|confirmed)\b", re.I)
QUALIFIERS = re.compile(
    r"\b(?:no|not|never|denies|denied|denial|false|untrue|unconfirmed|rumou?r|"
    r"may|might|could|would|if|risk|fears?|threatens?|plans?|considering|expected|"
    r"last year|last month|anniversary)\b|\?", re.I)
OFFICIAL_ORIGINS = {"irgc", "centcom", "ukmto", "opec", "aramco", "adnoc"}


def classify(story: dict, assets: list[dict]) -> list[dict]:
    headline = story["title"]
    attribution = list(ATTRIBUTION.finditer(headline))
    origins = {m["actor"].lower() for m in attribution}
    claim_origin = next(iter(origins)) if len(origins) == 1 else None
    # No implicit identity claim from a publisher, URL, or source role.
    geographies = sorted({a["id"] for a in assets for alias in a["aliases"]
                          if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", headline, re.I)})
    qualifier = QUALIFIERS.search(headline)
    slots = literal_slots(headline, assets)
    result = []
    for event_type, pattern in RULES.items():
        match = re.search(pattern, headline, re.I)
        if match is None:
            continue
        result.append({
            "event_type": event_type, "publisher": story["source_id"],
            "claim_origin": claim_origin, **slots,
            "geography": geographies,
            "state": "REVIEW_REQUIRED" if qualifier or len(origins) > 1 else (
                "OFFICIAL_CLAIM" if claim_origin in OFFICIAL_ORIGINS else "REPORTED"),
            "evidence": {"field": "title", "start": match.start(), "end": match.end(), "quote": match.group()},
            "attribution_evidence": [{"start": m.start(), "end": m.end(), "quote": m.group()} for m in attribution],
            "qualifier": qualifier.group() if qualifier else None,
            "confirmation": "UNVERIFIED", "independent_confirmation": "NONE",
            "review_required": True,
        })
    return result


class ForwardRecorder:
    def __init__(self, news: Journal, output: Journal, assets: list[dict]):
        self.news, self.output, self.assets = news, output, assets
        with output.transaction() as db:
            initialize_index(db)
            policy = digest([VERSION, assets, RULES, ATTRIBUTION.pattern, QUALIFIERS.pattern, LINK_VERSION])
            legacy_policy = digest(["fast-event-v1", assets, RULES, ATTRIBUTION.pattern, QUALIFIERS.pattern])
            prior_policy = db.execute("SELECT value FROM cursors WHERE key='forward:policy'").fetchone()
            if prior_policy and json.loads(prior_policy[0]) != policy:
                if json.loads(prior_policy[0]) != legacy_policy:
                    raise ValueError("forward classifier policy changed; use a new experiment storage root")
                # A known additive upgrade starts at the old high-water mark;
                # no old event is reclassified, retimed or overwritten.
                old_cursor = db.execute("SELECT value FROM cursors WHERE key='forward:seq:fast-event-v1'").fetchone()
                output.set_cursor(db, "forward:seq:" + VERSION, json.loads(old_cursor[0]) if old_cursor else 0)
                output.append("forward_policy_revision", {"from": "fast-event-v1", "to": VERSION,
                    "policy_hash": policy, "candidate_links": LINK_VERSION, "historical_reclassification": False}, db=db)
            output.set_cursor(db, "forward:policy", policy)
            row = db.execute("SELECT value FROM cursors WHERE key='forward:epoch'").fetchone()
            if row:
                self.epoch = json.loads(row[0])
            else:
                self.epoch = utc_now()
                output.append("forward_start", {"started_at": self.epoch, "transform": VERSION,
                              "trading": "disabled"}, available_at=self.epoch, db=db)
                output.set_cursor(db, "forward:epoch", self.epoch)

    def _exclusion(self, row):
        story = row["payload"]
        receipt = story.get("local_received_at")
        if not receipt:
            return "MISSING_RECEIPT_TIME"
        if instant(receipt) < instant(self.epoch):
            return "PRE_FORWARD_START"
        if story.get("initial_snapshot"):
            return "INITIAL_SNAPSHOT"
        if story.get("published_at") and story.get("revision") == 1 and instant(story["published_at"]) < instant(self.epoch):
            return "OLD_PUBLICATION_FIRST_SEEN"
        observations = [self.news.get(rid) for rid in story["input_revision_ids"]]
        if not observations or any(r is None or r["kind"] != "observation" for r in observations):
            return "MISSING_RAW_OBSERVATION"
        if any(r["payload"].get("synthetic") or r["payload"].get("delivery") != "http"
               for r in observations):
            return "NOT_LIVE_HTTP"
        if instant(receipt) > instant(utc_now()):
            return "FUTURE_RECEIPT_CLOCK"
        if story.get("status") in {"withdrawal", "deleted", "correction"}:
            return "REVISION_REQUIRES_REVIEW"
        return None

    @staticmethod
    def _identity(story, event):
        # Only explicit document lineage can connect different headlines. Never
        # merge incidents on a broad region/event-type/time bucket alone.
        report = re.search(r"\bUKMTO\s+(?:WARNING|ADVISORY)\s+(\d{1,4}-\d{2})\b", story["title"], re.I)
        if report:
            return ["ukmto_report", report[1]], "EXPLICIT_REPORT_ID"
        if story.get("original_url"):
            return ["original_url", story["original_url"]], "DECLARED_ORIGINAL_URL"
        normalized = re.sub(r"\W+", " ", story["title"].casefold()).strip()
        return ["headline", normalized, event["claim_origin"], event["event_type"]], "EXACT_HEADLINE_CANDIDATE"

    def run_once(self, limit=200):
        result = {"processed": 0, "events": 0, "excluded": 0, "pending": False,
                  "candidate_links": 0, "market_outcomes": "DEFERRED_NEWS_ONLY", "trading": "disabled"}
        cursor_key = "forward:seq:" + VERSION
        offset = self.output.cursor(cursor_key, 0)
        with self.news.connect() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM records WHERE kind='story_revision' AND seq>? ORDER BY seq LIMIT ?",
                (offset, limit + 1))]
        result["pending"] = len(rows) > limit
        for row in rows[:limit]:
            row["payload"] = json.loads(row["payload"])
            story = row["payload"]
            exclusion = self._exclusion(row)
            events = [] if exclusion else classify(story, self.assets)
            processed_at = utc_now()
            with self.output.transaction() as db:
                # Serialize retries/concurrent callers and commit outputs with
                # the cursor. A crash can never partially consume a revision.
                current = db.execute("SELECT value FROM cursors WHERE key=?", (cursor_key,)).fetchone()
                if current and int(json.loads(current[0])) >= row["seq"]:
                    continue
                if story.get("supersedes_id"):
                    self.output.append("forward_revision_notice", {
                        "story_revision_id": row["id"], "supersedes_story_revision_id": story["supersedes_id"],
                        "source_status": story.get("status"), "review_required": True,
                        "input_revision_ids": [row["id"], story["supersedes_id"]],
                        "instruction": "Earlier event evidence is superseded; do not infer a withdrawal from absence."},
                        available_at=processed_at, db=db)
                for event in events:
                    identity, basis = self._identity(story, event)
                    key = "forward:cluster:" + digest([VERSION, identity])
                    previous = db.execute("SELECT value FROM cursors WHERE key=?", (key,)).fetchone()
                    cluster = json.loads(previous[0]) if previous else None
                    receipt = story["local_received_at"]
                    if cluster and abs(seconds(receipt, cluster["first_received_at"])) > 21600:
                        cluster = None
                    novel = cluster is None
                    if novel:
                        cluster = {"incident_id": digest([VERSION, identity, row["id"]]),
                                   "first_received_at": receipt, "publishers": [], "state": None}
                    previous_state = cluster["state"]
                    if instant(receipt) < instant(cluster["first_received_at"]):
                        cluster["first_received_at"] = receipt
                    cluster["publishers"] = sorted(set(cluster["publishers"] + [story["source_id"]]))
                    # A qualified/denied update cannot silently inherit a stronger
                    # earlier claim; nothing here establishes physical confirmation.
                    cluster["state"] = event["state"]
                    event_id = digest([VERSION, row["id"], event["event_type"]])
                    payload = {**event, "incident_id": cluster["incident_id"], "novelty": novel,
                               "cluster_basis": basis, "transform": VERSION,
                               "received_at": receipt, "local_received_at": receipt,
                               "available_at": processed_at, "publisher_timestamp": story.get("published_at"),
                               "story_revision_id": row["id"], "input_revision_ids": [row["id"]],
                               "supersedes_story_revision_id": story.get("supersedes_id")}
                    links, search_truncated, links_truncated = candidate_links(db, payload, cluster["incident_id"])
                    payload.update(candidate_episode_links=links, candidate_search_truncated=search_truncated,
                                   candidate_links_truncated=links_truncated)
                    payload["input_revision_ids"] += [link["event_id"] for link in links]
                    self.output.append("fast_event", payload, available_at=processed_at, record_id=event_id, db=db)
                    for link in links:
                        self.output.append("candidate_episode_link", {**link, "from_event_id": event_id,
                            "from_incident_id": cluster["incident_id"], "state": "PENDING_REVIEW",
                            "input_revision_ids": [event_id, link["event_id"]]}, available_at=processed_at,
                            record_id=digest([LINK_VERSION, event_id, link["event_id"]]), db=db)
                    index_event(db, event_id, payload)
                    result["candidate_links"] += len(links)
                    self.output.append("forward_incident_revision", {**cluster, "event_id": event_id,
                        "previous_state": previous_state, "state_changed": previous_state != cluster["state"],
                        "input_revision_ids": [event_id], "cluster_basis": basis,
                        "independent_confirmation": "NONE", "review_required": True},
                        available_at=processed_at, record_id=digest([event_id, "incident"]), db=db)
                    self.output.set_cursor(db, key, cluster)
                    result["events"] += 1
                self.output.append("forward_processing", {"input_revision_ids": [row["id"]],
                    "transform": VERSION, "exclusion": exclusion, "events": len(events)},
                    available_at=processed_at, db=db)
                self.output.set_cursor(db, cursor_key, row["seq"])
            result["processed"] += 1
            result["excluded"] += bool(exclusion)
        return result
