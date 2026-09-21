"""Publisher identity, original claims and auditable confirmation progression.

Claim annotations are explicit reviewed inputs, not truth inferred from outlet names.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from typing import Protocol
from urllib.parse import urlparse

from .clock import epoch_ns
from .schema import NewsItem, digest

EVENT_TYPES = {
    "TANKER_ATTACK", "TANKER_SEIZURE", "SHIPPING_RESTRICTION", "SHIPPING_RESTORED",
    "PRODUCTION_SUSPENDED", "PRODUCTION_RESTORED", "EXPORT_TERMINAL_CLOSED", "EXPORT_TERMINAL_REOPENED",
    "PIPELINE_OUTAGE", "PIPELINE_RESTORED", "SANCTIONS_TIGHTENED", "SANCTIONS_EASED",
    "OPEC_OUTPUT_CUT", "OPEC_OUTPUT_INCREASE", "CEASEFIRE_REACHED", "CEASEFIRE_BROKEN",
    "NEGOTIATIONS_STARTED", "NEGOTIATIONS_COLLAPSED",
}


@dataclass(frozen=True)
class SourceProfile:
    source: str
    name: str
    source_type: str
    geography: str
    authority_scope: list[str] = field(default_factory=list)
    independence_group: str | None = None
    independence_verified: bool = False
    delivery: str = "web"
    homepage: str | None = None
    endpoint: str | None = None
    enabled: bool = False
    qualification: str = "pending"
    verification_basis: str | None = None
    model_processing: str = "pending"
    aliases: list[str] = field(default_factory=list)

    def validate(self):
        if not self.source or not self.name or not self.geography:
            raise ValueError("source identity and geography required")
        if self.source_type not in {"news_agency", "regional_news", "aggregator", "shipping_media", "official", "operator", "social"}:
            raise ValueError("unknown source type")
        if self.delivery not in {"web", "rss", "api", "social", "licensed", "structured"}:
            raise ValueError("unknown delivery")
        if self.independence_verified and (not self.independence_group or not self.verification_basis):
            raise ValueError("independence requires documented verification")
        if self.enabled and (not self.endpoint or self.qualification != "verified" or not self.verification_basis):
            raise ValueError("enabled source requires qualified endpoint and verification basis")
        if self.endpoint and urlparse(self.endpoint).scheme != "https":
            raise ValueError("HTTPS endpoint required")
        if self.model_processing not in {"pending", "permitted", "prohibited"}:
            raise ValueError("invalid model-processing policy")


def load_profiles(path):
    value = json.loads(Path(path).read_text())
    profiles = {}
    for row in value["sources"]:
        profile = SourceProfile(**row)
        profile.validate()
        if profile.source in profiles:
            raise ValueError("duplicate source profile")
        profiles[profile.source] = profile
    return profiles


class FastSourceAdapter(Protocol):
    """Boundary for permitted push/API feeds; does not change website poll limits."""
    def parse(self, payload: bytes, url: str, content_type: str) -> list[NewsItem]: ...


class StructuredNewsAdapter:
    """Normalized licensed/API/social envelopes; all text remains untrusted.

    Profile/transport verification is outside publisher-supplied payloads.
    """
    def parse(self, payload, url, content_type):
        envelope = json.loads(payload)
        if not isinstance(envelope, dict) or set(envelope) != {"items"} or not isinstance(envelope["items"], list):
            raise ValueError("structured feed requires an items envelope")
        result = []
        for row in envelope["items"]:
            if set(row) - {"id", "url", "title", "text", "published_at", "status", "author", "original_url", "reposter", "media_sha256"}:
                raise ValueError("unrecognized structured-feed fields")
            if row.get("status", "update") not in {"update", "correction", "withdrawal", "deleted"}:
                raise ValueError("unknown edit/delete status")
            for key in ("id", "title", "text", "url"):
                if not isinstance(row.get(key), str) or not row[key]:
                    raise ValueError("structured item missing " + key)
            for key in ("url", "original_url"):
                if row.get(key) and urlparse(row[key]).scheme != "https":
                    raise ValueError("invalid original/item URL")
            if row.get("published_at"):
                epoch_ns(row["published_at"])
            media = row.get("media_sha256")
            if media and not re.fullmatch(r"[0-9a-f]{64}", media):
                raise ValueError("invalid media hash")
            result.append(NewsItem(row["id"], row["url"], row["title"], row["text"], row.get("published_at"),
                                  status=row.get("status", "update"), author=row.get("author"),
                                  original_url=row.get("original_url"), reposter=row.get("reposter"), media_sha256=media))
        return result


def attribution_candidates(text, profiles):
    """Literal attribution spans only; mentioning an actor is not attribution."""
    candidates = []
    for source, profile in profiles.items():
        for alias in sorted(set([profile.name, *profile.aliases])):
            pattern = rf"\b(?:{re.escape(alias)}\s+(?:says|said|reports|reported|claims|claimed)|according to\s+{re.escape(alias)})\b"
            for match in re.finditer(pattern, text, re.I):
                candidates.append({"claim_origin": source, "start": match.start(), "end": match.end(),
                                   "quote": match.group(), "status": "ATTRIBUTION_CANDIDATE"})
    return candidates


def record_claim(news, analysis, profiles, annotation):
    """Append a reviewer-supplied proposition linked to immutable source text."""
    required = {"story_revision_id", "episode_id", "claim_key", "event_type", "value", "polarity",
                "claim_origin", "origin_claim_id", "basis", "authority_scope", "start", "end", "quote", "review_reason"}
    if set(annotation) - {"supersedes_claim_ids"} != required or not annotation["review_reason"].strip():
        raise ValueError("complete claim annotation and review reason required")
    story = news.get(annotation["story_revision_id"])
    if story is None or story["kind"] != "story_revision":
        raise ValueError("claim requires a captured story revision")
    p = story["payload"]
    latest_story = news.cursor("story:" + p["story_id"])
    if latest_story and latest_story["revision_id"] != story["id"]:
        raise ValueError("cannot annotate a superseded story revision")
    publisher = profiles[p["source_id"]]
    origin_id = annotation["claim_origin"]
    origin = profiles.get(origin_id)
    if origin_id is not None and origin is None:
        raise ValueError("unknown claim origin profile")
    if annotation["event_type"] not in EVENT_TYPES or annotation["polarity"] not in {"asserted", "denied", "uncertain"}:
        raise ValueError("invalid event category/polarity")
    if annotation["basis"] not in {"attributed", "direct_observation", "first_party", "repost"}:
        raise ValueError("invalid reporting basis")
    if annotation["basis"] in {"first_party", "direct_observation"} and origin_id != publisher.source:
        raise ValueError("direct evidence must identify the publisher as origin")
    start, end = annotation["start"], annotation["end"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(p["text"]) or p["text"][start:end] != annotation["quote"]:
        raise ValueError("claim must have a literal evidence span")
    if not annotation["episode_id"] or not annotation["claim_key"] or not annotation["value"]:
        raise ValueError("episode, proposition and value required")
    if annotation["authority_scope"] and (origin is None or annotation["authority_scope"] not in origin.authority_scope):
        raise ValueError("claim outside declared origin authority scope")
    identity = digest(["claim", annotation])
    old = analysis.cursor("claim:" + identity)
    if old:
        return old
    snapshot = {key: asdict(profile) for key, profile in profiles.items() if key in {publisher.source, origin_id}}
    for rid in annotation.get("supersedes_claim_ids", []):
        old_claim = analysis.get(rid)
        if old_claim is None or old_claim["kind"] != "claim_revision" or old_claim["payload"]["episode_id"] != annotation["episode_id"] or old_claim["payload"]["claim_key"] != annotation["claim_key"]:
            raise ValueError("supersession requires a claim about the same episode/proposition")
    with analysis.transaction() as db:
        rid = analysis.append("claim_revision", {**annotation, "publisher": publisher.source,
                "profiles": snapshot, "received_at": p["observed_at"], "published_at": p.get("published_at"),
                "source_status": p["status"], "story_id": p["story_id"],
                "input_revision_ids": [story["id"], *annotation.get("supersedes_claim_ids", [])], "transform": "reviewed-claim-v1"}, db=db)
        analysis.set_cursor(db, "claim:" + identity, rid)
    return rid


def claim_transitions(records):
    """Rebuild episode evidence at each receipt/review/retraction; no headline voting."""
    active, prior, result = {}, {}, []
    latest_stories = {}
    for row in sorted(records, key=lambda r: (epoch_ns(r["available_at"]), r.get("seq", 0))):
        p = row["payload"]
        episodes = []
        if row["kind"] == "story_revision":
            # Every edit replaces old evidence until its new content is reviewed.
            story_id = p.get("story_id")
            latest_stories[story_id] = row["id"]
            for identity, claim in list(active.items()):
                if claim["payload"]["story_id"] == story_id and claim["payload"]["story_revision_id"] != row["id"]:
                    episodes.append(claim["payload"]["episode_id"])
                    del active[identity]
        elif row["kind"] == "claim_revision":
            if latest_stories.get(p["story_id"], p["story_revision_id"]) != p["story_revision_id"]:
                continue
            for key, claim in list(active.items()):
                if claim["id"] in p.get("supersedes_claim_ids", []):
                    del active[key]
            # Later review can replace a proposition from the same source story.
            key = (p["episode_id"], p["story_id"], p["claim_key"])
            active[key] = row
            episodes = [p["episode_id"]]
        else:
            continue
        for episode in sorted(set(episodes)):
            claims = [c for c in active.values() if c["payload"]["episode_id"] == episode
                      and c["payload"]["source_status"] not in {"withdrawal", "deleted"}]
            summary = _confirmation(claims)
            before = prior.get(episode, {"confirmation": "UNVERIFIED_REPORT", "contradictions": [], "groups": []})
            # New copies remain preserved but don't create economic transitions.
            material = {k: summary[k] for k in ("confirmation", "contradictions", "groups")}
            if episode in prior and material == before:
                continue
            prior[episode] = material
            result.append({"event_id": digest(["claim-transition-v1", episode, row["id"], material]),
                "episode_id": episode, "event_family": "claim_progression", "direction": 0,
                "event_transition": before["confirmation"] + "_to_" + summary["confirmation"],
                "confirmation_level": summary["confirmation"], "contradiction_flag": bool(summary["contradictions"]),
                "contradictions": summary["contradictions"], "independent_groups": summary["groups"],
                "claim_ids": [c["id"] for c in claims], "decision_at": row["available_at"],
                "received_at": p.get("received_at", p.get("observed_at", row["available_at"])),
                "published_at": p.get("published_at"), "source": p.get("publisher", p.get("source_id")),
                "input_revision_ids": [row["id"], *[c["id"] for c in claims]]})
    return result


def _confirmation(claims):
    propositions, groups = {}, set()
    level = "UNVERIFIED_REPORT"
    rank = {"UNVERIFIED_REPORT": 0, "OFFICIAL_CLAIM": 1, "MULTIPLE_MEDIA_CORROBORATION": 2,
            "MARITIME_AUTHORITY_CORROBORATION": 3, "OPERATOR_CONFIRMED": 4, "PHYSICAL_DISRUPTION_CONFIRMED": 5}
    for row in claims:
        p = row["payload"]
        propositions.setdefault(p["claim_key"], []).append(p)
        origin = p["profiles"].get(p["claim_origin"])
        if not origin or p["polarity"] != "asserted" or p["basis"] == "repost":
            continue
        candidate = "UNVERIFIED_REPORT"
        # Authority is scoped. Authenticated first-party publication is required
        # for physical/operator confirmation; a newspaper quoting it stays a claim.
        if origin["source_type"] in {"official", "operator"} and p["authority_scope"]:
            candidate = "OFFICIAL_CLAIM"
        verified_direct = (p["basis"] == "first_party" and p["publisher"] == p["claim_origin"]
                           and origin["qualification"] == "verified" and origin["verification_basis"])
        if verified_direct and p["authority_scope"] == "maritime_incidents":
            candidate = "MARITIME_AUTHORITY_CORROBORATION"
        if verified_direct and origin["source_type"] == "operator" and p["authority_scope"] == "own_operations":
            candidate = "OPERATOR_CONFIRMED"
            if p.get("event_type") in {"PRODUCTION_SUSPENDED", "EXPORT_TERMINAL_CLOSED", "PIPELINE_OUTAGE"} and p["value"] in {"impaired", "suspended"}:
                candidate = "PHYSICAL_DISRUPTION_CONFIRMED"
        if rank[candidate] > rank[level]:
            level = candidate
        if p["basis"] == "direct_observation" and origin["independence_verified"] and origin["independence_group"]:
            groups.add(origin["independence_group"])
    contradictions = []
    for key, rows in propositions.items():
        assertions = {p["value"] for p in rows if p["polarity"] == "asserted"}
        denials = {p["value"] for p in rows if p["polarity"] == "denied"}
        if len(assertions) > 1 or assertions & denials:
            contradictions.append({"claim_key": key, "asserted": sorted(assertions), "denied": sorted(denials)})
    # Agreement must concern the same proposition/value, not unrelated reports in an episode.
    agreement = {}
    for rows in propositions.values():
        for p in rows:
            origin = p["profiles"].get(p["claim_origin"])
            if origin and p["polarity"] == "asserted" and p["basis"] == "direct_observation" and origin["independence_verified"]:
                agreement.setdefault((p["claim_key"], p["value"]), set()).add(origin["independence_group"])
    if any(len(g) >= 2 for g in agreement.values()) and rank[level] < rank["MULTIPLE_MEDIA_CORROBORATION"]:
        level = "MULTIPLE_MEDIA_CORROBORATION"
    return {"confirmation": "CONTESTED" if contradictions else level,
            "contradictions": sorted(contradictions, key=lambda c: c["claim_key"]), "groups": sorted(groups)}
