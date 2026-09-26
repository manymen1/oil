"""Forward-only, local deterministic discovery. Never a truth or trading engine.

Regex hits are review candidates with literal evidence, not verified facts.
Availability is processing time; receipt time is retained separately for research.
"""
from __future__ import annotations

import json
import re

from .clock import instant, utc_now
from .schema import digest
from .store import Journal
from .entities import ENTITY_VERSION, literal_slots

VERSION = "fast-event-v3"
CURSOR_KEY = "forward:seq"
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
            "syndication_origin": story.get("origin"),
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
    def __init__(self, news: Journal, output: Journal, assets: list[dict], *,
                 asset_registry_version="unversioned", source_registry_version="unversioned"):
        self.news, self.output, self.assets = news, output, assets
        policy = {"classifier_version": VERSION, "entity_version": ENTITY_VERSION,
                  "rules": RULES, "attribution_pattern": ATTRIBUTION.pattern,
                  "qualifier_pattern": QUALIFIERS.pattern, "official_origins": sorted(OFFICIAL_ORIGINS),
                  "assets": assets, "asset_registry_version": asset_registry_version,
                  "source_registry_version": source_registry_version}
        self.policy_hash = digest(policy)
        self.metadata = {"classifier_version": VERSION, "classifier_policy_hash": self.policy_hash,
                         "asset_registry_version": asset_registry_version, "asset_registry_hash": digest(assets),
                         "source_registry_version": source_registry_version}
        with output.transaction() as db:
            db.execute("CREATE INDEX IF NOT EXISTS fast_event_story ON records(json_extract(payload,'$.story_revision_id')) WHERE kind='fast_event'")
            db.execute("CREATE INDEX IF NOT EXISTS evidence_event ON records(json_extract(payload,'$.event_id'),seq) WHERE kind='forward_evidence_transition'")
            existing = db.execute("SELECT value FROM cursors WHERE key=?", (CURSOR_KEY,)).fetchone()
            if not existing:
                legacy = db.execute("SELECT value FROM cursors WHERE key IN ('forward:seq:fast-event-v1','forward:seq:fast-event-v2')").fetchall()
                output.set_cursor(db, CURSOR_KEY, max([json.loads(r[0]) for r in legacy], default=0))
            row = db.execute("SELECT value FROM cursors WHERE key='forward:epoch'").fetchone()
            self.epoch = json.loads(row[0]) if row else utc_now()
            if not row:
                output.append("forward_start", {"started_at": self.epoch, "transform": VERSION,
                              "trading": "disabled"}, available_at=self.epoch, db=db)
                output.set_cursor(db, "forward:epoch", self.epoch)
            prior = db.execute("SELECT value FROM cursors WHERE key='forward:policy'").fetchone()
            policy_record = db.execute("SELECT value FROM cursors WHERE key='forward:policy_record'").fetchone()
            if not prior or json.loads(prior[0]) != self.policy_hash or not policy_record:
                effective_at = utc_now()
                self.policy_record_id = output.append("forward_policy_revision", {
                    **self.metadata, "effective_at": effective_at, "policy": policy,
                    "previous_policy_hash": json.loads(prior[0]) if prior else None,
                    "consumed_news_seq": json.loads(db.execute("SELECT value FROM cursors WHERE key=?", (CURSOR_KEY,)).fetchone()[0]),
                    "historical_reclassification": False}, available_at=effective_at, db=db)
                output.set_cursor(db, "forward:policy", self.policy_hash)
                output.set_cursor(db, "forward:policy_record", self.policy_record_id)
            else:
                self.policy_record_id = json.loads(policy_record[0])

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
        if any(r["payload"].get("synthetic") or r["payload"].get("delivery") != "http" for r in observations):
            return "NOT_LIVE_HTTP"
        if instant(receipt) > instant(utc_now()):
            return "FUTURE_RECEIPT_CLOCK"
        if story.get("status") in {"withdrawal", "deleted", "correction"}:
            return "REVISION_REQUIRES_REVIEW"
        return None

    @staticmethod
    def _identity(story, event):
        report = re.search(r"\bUKMTO\s+(?:WARNING|ADVISORY)\s+(\d{1,4}-\d{2})\b", story["title"], re.I)
        if report:
            return ["ukmto_report", report[1]], "EXPLICIT_REPORT_ID"
        if story.get("original_url"):
            return ["original_url", story["original_url"]], "DECLARED_ORIGINAL_URL"
        # Equal prose is never identity. Native IDs are scoped to the publisher.
        return ["story", story["source_id"], story["story_id"]], "NATIVE_STORY_LINEAGE"

    def _prior_events(self, db, story):
        previous = story.get("supersedes_id")
        # New lineage records shortcut chains. Legacy chains are bounded and
        # expose truncation rather than pretending to be complete.
        for _ in range(200):
            if not previous:
                return [], False
            lineage = db.execute("SELECT payload FROM records WHERE id=?",
                                 (digest(["story-event-lineage-v1", previous]),)).fetchone()
            if lineage:
                return json.loads(lineage[0])["effective_event_ids"], False
            rows = db.execute("SELECT id FROM records WHERE kind='fast_event' AND json_extract(payload,'$.story_revision_id')=?",
                              (previous,)).fetchall()
            if rows:
                return [r[0] for r in rows], False
            prior = self.news.get(previous)
            if prior is None or prior["kind"] != "story_revision" or prior["payload"]["story_id"] != story["story_id"]:
                raise ValueError("invalid story revision lineage")
            previous = prior["payload"].get("supersedes_id")
        return [], True

    def _transition(self, db, event_id, state, story, row, at):
        old = db.execute("SELECT id,payload FROM records WHERE kind='forward_evidence_transition' AND json_extract(payload,'$.event_id')=? ORDER BY seq DESC LIMIT 1",
                         (event_id,)).fetchone()
        previous_state = json.loads(old["payload"])["state"] if old else ("LEGACY_UNMODELED" if state != "ACTIVE" else None)
        self.output.append("forward_evidence_transition", {
            "event_id": event_id, "story_revision_id": row["id"], "previous_state": previous_state,
            "supersedes_transition_id": old["id"] if old else None,
            "state": state, "source_status": story.get("status"), "received_at": story.get("local_received_at"),
            "review_required": True, "confirmation": "UNVERIFIED", **self.metadata,
            "input_revision_ids": [event_id, row["id"], self.policy_record_id] + ([old["id"]] if old else [])},
            available_at=at, record_id=digest(["evidence-v1", event_id, row["id"], state]), db=db)

    def run_once(self, limit=200):
        if not 1 <= limit <= 1000:
            raise ValueError("forward batch limit must be 1..1000")
        result = {"processed": 0, "events": 0, "excluded": 0, "pending": False,
                  "market_outcomes": "DEFERRED_NEWS_ONLY", "trading": "disabled"}
        offset = self.output.cursor(CURSOR_KEY, 0)
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
                policy = json.loads(db.execute("SELECT value FROM cursors WHERE key='forward:policy'").fetchone()[0])
                policy_record = json.loads(db.execute("SELECT value FROM cursors WHERE key='forward:policy_record'").fetchone()[0])
                if policy != self.policy_hash or policy_record != self.policy_record_id:
                    raise ValueError("classifier policy changed while worker was running; restart worker")
                current = db.execute("SELECT value FROM cursors WHERE key=?", (CURSOR_KEY,)).fetchone()
                if current and json.loads(current[0]) >= row["seq"]:
                    continue
                prior_events, lineage_truncated = self._prior_events(db, story)
                eligible_revision = exclusion in {None, "REVISION_REQUIRES_REVIEW"}
                if story.get("supersedes_id"):
                    self.output.append("forward_revision_notice", {
                        "story_revision_id": row["id"], "supersedes_story_revision_id": story["supersedes_id"],
                        "source_status": story.get("status"), "review_required": True,
                        "input_revision_ids": [row["id"], story["supersedes_id"]]},
                        available_at=processed_at, db=db)
                if eligible_revision:
                    evidence_state = {"correction": "CORRECTED", "withdrawal": "WITHDRAWN",
                                      "deleted": "DELETED"}.get(story.get("status"), "SUPERSEDED")
                    for event_id in prior_events:
                        self._transition(db, event_id, evidence_state, story, row, processed_at)
                event_ids = []
                for event in events:
                    identity, basis = self._identity(story, event)
                    key = "forward:document:" + digest(identity)
                    previous = db.execute("SELECT value FROM cursors WHERE key=?", (key,)).fetchone()
                    cluster = json.loads(previous[0]) if previous else None
                    novel = cluster is None
                    if cluster is None:
                        cluster = {"incident_id": digest(["document-identity-v1", identity]),
                                   "first_received_at": story["local_received_at"], "publishers": [], "state": None}
                    previous_state = cluster["state"]
                    cluster["publishers"] = sorted(set(cluster["publishers"] + [story["source_id"]]))
                    cluster["state"] = event["state"]
                    event_id = digest([VERSION, row["id"], event["event_type"]])
                    payload = {**event, **self.metadata, "incident_id": cluster["incident_id"], "novelty": novel,
                               "cluster_basis": basis, "transform": VERSION, "headline": story["title"],
                               "received_at": story["local_received_at"], "local_received_at": story["local_received_at"],
                               "available_at": processed_at, "publisher_timestamp": story.get("published_at"),
                               "story_revision_id": row["id"], "story_id": story["story_id"],
                               "input_revision_ids": [row["id"], self.policy_record_id],
                               "supersedes_story_revision_id": story.get("supersedes_id")}
                    self.output.append("fast_event", payload, available_at=processed_at, record_id=event_id, db=db)
                    self._transition(db, event_id, "ACTIVE", story, row, processed_at)
                    self.output.append("forward_incident_revision", {**cluster, "event_id": event_id,
                        "previous_state": previous_state, "state_changed": previous_state != cluster["state"],
                        "input_revision_ids": [event_id], "cluster_basis": basis,
                        "independent_confirmation": "NONE", "review_required": True},
                        available_at=processed_at, record_id=digest([event_id, "incident"]), db=db)
                    self.output.set_cursor(db, key, cluster)
                    event_ids.append(event_id)
                self.output.append("story_event_lineage", {
                    "story_id": story["story_id"], "story_revision_id": row["id"],
                    "supersedes_story_revision_id": story.get("supersedes_id"),
                    "supersedes_fast_event_ids": prior_events if eligible_revision else [],
                    "fast_event_ids": event_ids, "effective_event_ids": event_ids or prior_events,
                    "lineage_search_truncated": lineage_truncated, "exclusion": exclusion,
                    "input_revision_ids": [row["id"], *prior_events, *event_ids]},
                    available_at=processed_at, record_id=digest(["story-event-lineage-v1", row["id"]]), db=db)
                self.output.append("forward_processing", {"input_revision_ids": [row["id"]],
                    **self.metadata, "transform": VERSION, "exclusion": exclusion, "events": len(events)},
                    available_at=processed_at, db=db)
                self.output.set_cursor(db, CURSOR_KEY, row["seq"])
            result["processed"] += 1
            result["events"] += len(events)
            result["excluded"] += bool(exclusion)
        return result
