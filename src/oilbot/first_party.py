"""Scoped publication attribution, never independent factual confirmation."""
from copy import deepcopy
import re
from urllib.parse import urlparse

from .clock import instant
from .schema import digest

VERSION = "first-party-v1"
SCOPES = {
    "own_operations": {"PRODUCTION_SUSPENDED", "PRODUCTION_RESTORED", "EXPORT_TERMINAL_CLOSED",
                       "EXPORT_TERMINAL_REOPENED", "PIPELINE_OUTAGE", "PIPELINE_RESTORED"},
    "own_sanctions_actions": {"SANCTIONS_TIGHTENED", "SANCTIONS_EASED"},
    "own_military_statements": {"MILITARY_STRIKE", "MISSILE_ATTACK", "DRONE_ATTACK"},
}
SUBJECTS = {
    "own_sanctions_actions": r"^(?:OFAC|Office of Foreign Assets Control)\s+(?P<action>imposes?|tightens?|expands?|lifts?|eases?|removes?)\b",
    "own_military_statements": r"^(?:CENTCOM|USCENTCOM|U\.?S\.? Central Command)\s+(?P<action>conducts?|conducted|launches?|launched|carries out|carried out)\b",
}
REPORTED_SPEECH = re.compile(r"\b(?:according to|says|said|reports?|reported|claims?|claimed|confirms?|confirmed|quoting|quoted)\b|[\"“”]", re.I)


def safe_url(url, hosts, prefixes=None):
    try:
        parsed = urlparse(url or "")
        if (parsed.scheme != "https" or parsed.hostname not in hosts or parsed.port not in (None, 443)
                or parsed.username or parsed.password or "%" in parsed.path or "\\" in parsed.path
                or any(part in {".", ".."} for part in parsed.path.split("/"))):
            return False
        return prefixes is None or any(parsed.path.startswith(prefix) for prefix in prefixes)
    except ValueError:
        return False


def validate_reviews(reviews, sources, assets):
    if not isinstance(reviews, (list, tuple)):
        raise ValueError("first-party reviews must be a list")
    registry = {s["id"]: s for s in sources}
    asset_ids = {a["id"] for a in assets}
    seen = set()
    required = {"source_id", "claim_origin", "status", "source_policy_hash", "reviewed_at", "expires_at",
                "reviewer", "evidence", "scope", "item_path_prefixes", "asset_ids", "asset_owner"}
    for review in reviews:
        if not isinstance(review, dict) or set(review) != required or review["source_id"] not in registry or review["source_id"] in seen:
            raise ValueError("one complete first-party review per registered source required")
        seen.add(review["source_id"])
        if review["claim_origin"] != review["source_id"] or review["status"] not in {"verified", "pending", "revoked"}:
            raise ValueError("first-party identity must match publisher; valid review status required")
        if not isinstance(review["reviewer"], str) or not review["reviewer"].strip() or not isinstance(review["evidence"], list) or not review["evidence"] or any(not isinstance(e, str) or not e.strip() for e in review["evidence"]):
            raise ValueError("first-party review requires reviewer and evidence")
        if not re.fullmatch(r"[0-9a-f]{64}", review["source_policy_hash"]):
            raise ValueError("first-party review requires a source-policy hash")
        if instant(review["expires_at"]) <= instant(review["reviewed_at"]):
            raise ValueError("first-party expiry must follow review time")
        if review["scope"] not in SCOPES:
            raise ValueError("unsupported first-party authority scope")
        prefixes = review["item_path_prefixes"]
        if not isinstance(prefixes, list) or not prefixes or any(not isinstance(p, str) or not p.startswith("/") or not p.endswith("/") or p == "/" or any(c in p for c in ("%", "..", "?", "#", "\\")) for p in prefixes):
            raise ValueError("first-party item paths must be explicit directory prefixes")
        if not isinstance(review["asset_ids"], list) or any(not isinstance(a, str) for a in review["asset_ids"]):
            raise ValueError("first-party asset IDs must be a list")
        source = registry[review["source_id"]]
        if review["scope"] == "own_operations":
            if source["role"] != "operator" or not review["asset_ids"] or not set(review["asset_ids"]) <= asset_ids or review["asset_owner"] != source["owner"]:
                raise ValueError("own-operations scope requires operator and registered owned assets")
        elif source["role"] != "official" or review["asset_ids"] or review["asset_owner"] is not None:
            raise ValueError("official scope requires an official source and no asset ownership claims")
        if review["scope"] == "own_sanctions_actions" and source["id"] != "ofac":
            raise ValueError("sanctions scope currently supports OFAC only")
        if review["scope"] == "own_military_statements" and source["id"] != "centcom":
            raise ValueError("military scope currently supports CENTCOM only")


class FirstPartyResolver:
    def __init__(self, sources=(), reviews=(), assets=()):
        validate_reviews(reviews, sources, assets)
        self.sources = {s["id"]: deepcopy(s) for s in sources}
        self.reviews = {r["source_id"]: deepcopy(r) for r in reviews}
        self.assets = {a["id"]: deepcopy(a) for a in assets}

    def apply(self, event, story, observations, *, at, event_count=1):
        review = self.reviews.get(story["source_id"])
        assessment = {"policy_version": VERSION, "status": "ABSTAINED", "reason": None}
        event["first_party_assessment"] = assessment
        def reject(reason):
            assessment["reason"] = reason
        if event["attribution_evidence"]:
            return reject("EXPLICIT_ATTRIBUTION_TAKES_PRECEDENCE")
        if event_count != 1:
            return reject("MULTIPLE_EVENT_HEADLINE")
        if review is None:
            return reject("NO_REVIEW")
        assessment["review_hash"] = digest(review)
        if review["status"] != "verified":
            return reject("REVIEW_NOT_VERIFIED")
        source = self.sources[story["source_id"]]
        if not source["enabled"] or digest(source) != review["source_policy_hash"]:
            return reject("SOURCE_REGISTRATION_MISMATCH")
        receipt = story.get("local_received_at")
        if not receipt or not (instant(review["reviewed_at"]) <= instant(receipt) <= instant(at) < instant(review["expires_at"])):
            return reject("REVIEW_NOT_VALID_AT_RECEIPT_AND_PROCESSING")
        if not safe_url(story.get("url"), source["allowed_hosts"], review["item_path_prefixes"]):
            return reject("ITEM_OUTSIDE_REVIEWED_CHANNEL")
        if not observations:
            return reject("MISSING_TRANSPORT_EVIDENCE")
        for row in observations:
            p = row["payload"] if row and row["kind"] == "observation" else {}
            if (p.get("source_id") != source["id"] or p.get("source_policy") != review["source_policy_hash"]
                    or p.get("status") != 200 or p.get("delivery") != "http" or p.get("synthetic")
                    or p.get("requested_url", p.get("url")) != source["url"]
                    or p.get("url") != source["url"]
                    or not safe_url(p.get("url"), source["allowed_hosts"])):
                return reject("TRANSPORT_REGISTRATION_MISMATCH")
        if any(story.get(k) for k in ("origin", "source_attributions", "wire_evidence", "original_url", "reposter")):
            return reject("THIRD_PARTY_PROVENANCE")
        if REPORTED_SPEECH.search(story["title"]):
            return reject("REPORTED_SPEECH_OR_QUOTE")
        if event["qualifier"]:
            return reject("QUALIFIED_OR_UNCERTAIN_HEADLINE")
        scope = review["scope"]
        if event["event_type"] not in SCOPES[scope]:
            return reject("EVENT_OUTSIDE_AUTHORITY_SCOPE")
        if scope == "own_operations":
            asset_ids = event["asset_ids"]
            if not asset_ids or not set(asset_ids) <= set(review["asset_ids"]):
                return reject("ASSET_OUTSIDE_AUTHORITY_SCOPE")
            if any(self.assets[aid].get("owner") != review["asset_owner"] for aid in asset_ids):
                return reject("ASSET_OWNERSHIP_MISMATCH")
            if set(event["actors"]) - {review["claim_origin"]} or re.search(r"[;|]|\b(?:and|but|while)\b", story["title"], re.I):
                return reject("AMBIGUOUS_OPERATION_SUBJECT")
            evidence = [e for e in event["literal_evidence"] if e["field"] == "asset_ids"]
            # Proximity alone is not ownership: "Shell production halted near
            # Ras Tanura" must not become an Aramco-origin operation claim.
            hit = event["evidence"]
            prefix = story["title"][:hit["start"]].strip().casefold()
            permitted_prefixes = {"", review["claim_origin"].casefold(), review["asset_owner"].casefold()}
            permitted_prefixes.update(alias.casefold() for aid in asset_ids for alias in self.assets[aid]["aliases"])
            if len(asset_ids) != 1 or prefix not in permitted_prefixes:
                return reject("AMBIGUOUS_OPERATION_SUBJECT")
            if not any(e["start"] < hit["end"] or story["title"][hit["end"]:e["start"]].strip().casefold() == "at" for e in evidence):
                return reject("AMBIGUOUS_OPERATION_SUBJECT")
        else:
            match = re.search(SUBJECTS[scope], story["title"], re.I)
            if not match:
                return reject("MISSING_FIRST_PARTY_ACTION_SUBJECT")
            hit = event["evidence"]
            if re.search(r"[;|:]|\b(?:and|but|while|after|before|as)\b", story["title"], re.I):
                return reject("AMBIGUOUS_OFFICIAL_ACTION_SUBJECT")
            if scope == "own_sanctions_actions":
                linked = hit["start"] == match.start("action") and re.fullmatch(
                    r"\s+(?:(?:new|additional|oil|oil-related|Iran|Iranian)\s+)*sanctions",
                    story["title"][match.end():hit["end"]], re.I)
            else:
                linked = hit["start"] >= match.end() and re.fullmatch(
                    r"\s+(?:an?\s+)?", story["title"][match.end():hit["start"]], re.I)
            if not linked:
                return reject("AMBIGUOUS_OFFICIAL_ACTION_SUBJECT")
            evidence = [{"text_field": "title", "start": match.start(), "end": match.end(), "quote": match.group()}]
        event.update(claim_origin=review["claim_origin"], claim_origin_basis="FIRST_PARTY_PUBLICATION",
                     state="OFFICIAL_CLAIM", first_party_evidence={"review_hash": digest(review),
                     "scope": scope, "subject_evidence": evidence, "observation_ids": [r["id"] for r in observations]})
        assessment.update(status="APPLIED", reason="VERIFIED_CHANNEL_AND_SCOPED_SUBJECT")
