"""Conservative literal context, not event truth or inferred physical losses."""
import re

VERSION = "headline-interpretation-v2"
PATTERNS = {
    "denied": r"\b(?:no|not|never|denies|denied|deny|denial|false|untrue)\b",
    "hypothetical": r"\b(?:may|might|could|would|if|possible|potential|risk|fears?|threatens?|plans?|considering|expected|proposals?|propos(?:e|es|ed|ing)|roadmaps?)\b|\?",
    "historical": r"\b(?:last year|last month|anniversary|in (?:19|20)\d{2})\b",
    "unclear": r"\b(?:unconfirmed|rumou?r|reportedly)\b",
}
OIL = r"\b(?:oil|crude|petroleum|WTI|Brent|refiner(?:y|ies)|OPEC|oilfield|Hormuz|tanker)\b"
UNRELATED = r"\b(?:Toyota|automaker|automobile|car factory|North Korean hackers|cybercriminals|swimming|football)\b"
TRANSPORT = r"\b(?:tanker|shipping|transit|strait|Hormuz|pipeline|terminal|exports)\b"
SUPPLY = r"\b(?:production|output|oilfield|refiner(?:y|ies))\b"


def policy():
    return {"version": VERSION, "assertion_patterns": PATTERNS, "oil_pattern": OIL,
            "unrelated_pattern": UNRELATED, "transport_pattern": TRANSPORT,
            "supply_pattern": SUPPLY, "scope": "whole-headline conservative; compound scope is not resolved"}


def interpret(title, slots):
    matches = [(label, re.search(pattern, title, re.I)) for label, pattern in PATTERNS.items()]
    present = [(label, hit) for label, hit in matches if hit]
    assertion = present[0][0] if len(present) == 1 else "unclear" if present else "asserted"
    # Several kinds of modality in one headline are not assigned to individual clauses.
    oil, unrelated = re.search(OIL, title, re.I), re.search(UNRELATED, title, re.I)
    if unrelated:
        relevance = "uncertain" if oil or slots["location_ids"] else "irrelevant"
    else:
        relevance = "relevant" if oil or slots["location_ids"] else "uncertain"
    mechanism = "unknown"
    if relevance == "relevant":
        mechanism = "transport" if re.search(TRANSPORT, title, re.I) else "supply" if re.search(SUPPLY, title, re.I) else "unknown"
    evidence = [{"field": "assertion", "value": label, "text_field": "title", "start": hit.start(),
                 "end": hit.end(), "quote": hit.group()} for label, hit in present]
    for field, hit, value in (("oil_relevance", oil, relevance), ("oil_relevance", unrelated, relevance)):
        if hit:
            evidence.append({"field": field, "value": value, "text_field": "title", "start": hit.start(),
                             "end": hit.end(), "quote": hit.group()})
    return {"assertion": assertion, "oil_relevance": relevance, "oil_mechanism": mechanism,
            "asserted_oil_event_candidate": assertion == "asserted" and relevance == "relevant",
            "interpretation_evidence": evidence, "interpretation_version": VERSION}
