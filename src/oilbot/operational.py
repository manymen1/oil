"""Deterministic review triage of captured text; never confirmation or a signal."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3

from .clock import instant, utc_now
from .schema import digest

VERSION = "operational-review-v1"
CONTEXT = r"\b(?:crude|oil|petroleum|tanker|pipeline|terminal|refiner(?:y|ies)|production|loading|exports?|shipping|transit)\b"
CHANGE = r"\b(?:suspend(?:s|ed|ing)?|suspension|halt(?:s|ed|ing)?|stop(?:s|ped|ping)?|outage|disrupt(?:s|ed|ion)?|impair(?:ed|ment)?|clos(?:e[ds]?|ing|ure)|resum(?:e[ds]?|ing|ption)|restor(?:e[ds]?|ing|ation)|reopen(?:s|ed|ing)?|restart(?:s|ed|ing)?|repair(?:s|ed|ing)?|restrict(?:s|ed|ion)?|force majeure)\b"
QUALIFIER = r"\b(?:no|not|den(?:y|ies|ied)|unconfirmed|rumou?r|may|might|could|would|if|plans?|propos(?:al|als|e|ed)|roadmaps?|expected|last year|last month)\b"


def signals(story, assets):
    """Recall-oriented co-occurrence, including qualified and historical wording.

    These spans explain queue selection only. They do not assert that the words
    describe the same clause, a real oil asset, or an actual operating change.
    """
    context = CONTEXT
    aliases = [re.escape(alias) for asset in assets for alias in asset["aliases"]]
    if aliases:
        context += "|(?:" + "|".join(aliases) + ")"
    spans = []
    # Captured text normally includes the title; inspect both for sparse adapters.
    fields = ["text"] if story.get("text") and story.get("title", "") in story["text"] else ["title", "text"]
    for field in fields:
        text = story.get(field, "")
        changes = list(re.finditer(CHANGE, text, re.I))
        contexts = list(re.finditer(context, text, re.I))
        if not changes or not contexts:
            continue
        for label, matches in (("operational_wording", changes), ("oil_or_asset_context", contexts),
                               ("qualified_wording", list(re.finditer(QUALIFIER, text, re.I)))):
            for match in matches[:16]:
                spans.append({"text_field": field, "start": match.start(), "end": match.end(),
                              "quote": match.group(), "supports": label})
    return spans


class OperationalQueue:
    def __init__(self, news, output, assets, sources):
        self.news, self.output, self.assets = news, output, assets
        self.sources = {s["id"]: s for s in sources}
        policy = {"version": VERSION, "context": CONTEXT, "change": CHANGE, "qualifier": QUALIFIER,
                  "assets": assets, "sources": sources, "maximum_matches_per_kind_per_field": 16,
                  "meaning": "review triage only; no model, confirmation, event or trading eligibility"}
        self.policy_hash = digest(policy)
        self.cursor_key = "operational:seq:" + self.policy_hash
        with output.transaction() as db:
            db.execute("CREATE INDEX IF NOT EXISTS operational_story ON records(json_extract(payload,'$.story_id'),seq) WHERE kind='operational_review_candidate'")
            epoch = db.execute("SELECT value FROM cursors WHERE key='operational:epoch'").fetchone()
            self.epoch = json.loads(epoch[0]) if epoch else utc_now()
            if not epoch:
                output.set_cursor(db, "operational:epoch", self.epoch)
            rid = "operational-policy:" + self.policy_hash
            previous = db.execute("SELECT id FROM records WHERE id=?", (rid,)).fetchone()
            if not previous:
                output.append("operational_review_policy", {"policy": policy, "policy_hash": self.policy_hash,
                              "epoch": self.epoch, "input_revision_ids": []}, record_id=rid, db=db)
            self.policy_id = rid

    def run_once(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("bounded positive batch size required")
        processed = queued = 0
        with self.output.transaction() as db:
            cursor = db.execute("SELECT value FROM cursors WHERE key=?", (self.cursor_key,)).fetchone()
            seq = json.loads(cursor[0]) if cursor else 0
            with self.news.connect() as source:
                rows = source.execute("SELECT * FROM records WHERE kind='story_revision' AND seq>? ORDER BY seq LIMIT ?", (seq, limit)).fetchall()
            for raw in rows:
                row = {**dict(raw), "payload": json.loads(raw["payload"])}
                at = utc_now()
                if instant(row["available_at"]) > instant(at):
                    break  # Retry later without dropping this or subsequent rows.
                story = row["payload"]
                source = self.sources.get(story["source_id"])
                spans = signals(story, self.assets)
                prior = db.execute("SELECT id FROM records WHERE kind='operational_review_candidate' AND json_extract(payload,'$.story_id')=? ORDER BY seq DESC LIMIT 1", (story["story_id"],)).fetchone()
                lifecycle = story.get("status") in {"correction", "withdrawal", "deleted"}
                if source and source["enabled"] and (spans or prior):
                    exclusions = []
                    receipt = story.get("local_received_at")
                    if not receipt:
                        exclusions.append("MISSING_RECEIPT_TIME")
                    elif instant(receipt) < instant(self.epoch):
                        exclusions.append("PRE_QUEUE_START")
                    elif instant(receipt) > instant(at):
                        exclusions.append("FUTURE_RECEIPT_CLOCK")
                    if story.get("initial_snapshot", True):
                        exclusions.append("INITIAL_SNAPSHOT")
                    if story.get("parser_reinterpretation"):
                        exclusions.append("PARSER_REINTERPRETATION")
                    if story.get("published_at") and story.get("revision") == 1 and instant(story["published_at"]) < instant(self.epoch):
                        exclusions.append("OLD_PUBLICATION_FIRST_SEEN")
                    observations = [self.news.get(rid) for rid in story.get("input_revision_ids", [])]
                    if not observations or any(r is None or r["kind"] != "observation" or r["payload"].get("synthetic") or r["payload"].get("delivery") != "http" for r in observations):
                        exclusions.append("NOT_LIVE_HTTP")
                    role = source["role"]
                    priority = "lifecycle" if lifecycle else "primary_operations" if role in {"operator", "port_authority", "maritime_authority"} else "reported_operations"
                    reasons = (["CAPTURED_OPERATIONAL_WORDING"] if spans else ["FOLLOWUP_TO_QUEUED_STORY"])
                    if lifecycle:
                        reasons.append("LIFECYCLE_REQUIRES_REVIEW")
                    rid = digest([VERSION, self.policy_hash, row["id"]])
                    self.output.append("operational_review_candidate", {
                        "story_revision_id": row["id"], "story_id": story["story_id"], "source_id": story["source_id"],
                        "source_role": role, "priority": priority, "review_state": "PENDING_REVIEW",
                        "received_at": receipt, "story_available_at": row["available_at"], "queued_at": at,
                        "story_hash": digest(row), "story_status": story.get("status", "update"),
                        "initial_snapshot": story.get("initial_snapshot", True), "exclusions": exclusions,
                        "forward_capture_candidate": not exclusions, "selection_reasons": reasons,
                        "evidence": spans, "previous_candidate_id": prior[0] if prior else None,
                        "model_processing": source["rights"]["model_processing"],
                        "policy_hash": self.policy_hash, "version": VERSION, "confirmation": "UNVERIFIED",
                        "research_eligible": False, "trade_authorized": False,
                        "input_revision_ids": [row["id"], self.policy_id] + ([prior[0]] if prior else [])},
                        record_id=rid, available_at=at, db=db)
                    queued += 1
                self.output.set_cursor(db, self.cursor_key, row["seq"])
                processed += 1
        return {"processed": processed, "queued": queued, "policy_hash": self.policy_hash}


def read_queue(path, *, after_seq=0, limit=100, include_baseline=False):
    if type(limit) is not int or not 1 <= limit <= 1000 or after_seq < 0:
        raise ValueError("valid cursor and bounded limit required")
    path = Path(path)
    if not path.exists():
        return {"candidates": [], "next_after_seq": after_seq}
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        extra = "" if include_baseline else " AND json_extract(payload,'$.forward_capture_candidate')=1"
        rows = db.execute("SELECT * FROM records WHERE kind='operational_review_candidate' AND seq>?" + extra + " ORDER BY seq LIMIT ?", (after_seq, limit)).fetchall()
        candidates = [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
    return {"candidates": candidates, "next_after_seq": candidates[-1]["seq"] if candidates else after_seq,
            "include_baseline": include_baseline, "confirmation_granted": False}
