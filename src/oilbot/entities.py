"""Literal entity extraction; independent of candidate-link policies."""
from __future__ import annotations

import re
from .clock import instant

ENTITY_VERSION = "literal-entities-v1"
ACTORS = {"irgc": ["IRGC", "Islamic Revolutionary Guard Corps"],
          "centcom": ["CENTCOM", "US Central Command"], "iran": ["Iran", "Iranian"],
          "opec": ["OPEC"], "ukmto": ["UKMTO"], "aramco": ["Aramco"], "adnoc": ["ADNOC"],
          "ofac": ["OFAC"], "houthis": ["Houthis", "Houthi"], "israel": ["Israel", "Israeli"]}
VESSEL = re.compile(r'''\b(?i:tanker|vessel|ship)\s+(?:(?i:named)\s+)?(?:["“'](?P<quoted>[^"”'\n]{2,60})["”']|(?P<caps>[A-Z][A-Z0-9-]+(?:\s+[A-Z][A-Z0-9-]+){0,3})\b)''')
IMO = re.compile(r"\bIMO\s*[:#]?\s*(\d{7})\b")
EVENT_TIME = re.compile(r"\b(?:at|event time:)\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))", re.I)


def literal_slots(headline, assets):
    evidence, actors, vessels, facilities, locations, times, regions = [], set(), set(), set(), set(), set(), set()

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
                add("location_ids", asset["id"], match)
                if asset.get("geography"):
                    regions.add(asset["geography"])
                    add("regions", asset["geography"], match)
                if asset.get("type") != "maritime_route":
                    facilities.add(asset["id"])
                    add("asset_ids", asset["id"], match)
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
            "asset_ids": sorted(facilities), "vessels": sorted(vessels),
            "location": next(iter(locations)) if len(locations) == 1 else None,
            "location_ids": sorted(locations), "regions": sorted(regions),
            "event_time_if_explicit": next(iter(times)) if len(times) == 1 else None,
            "literal_evidence": evidence}

