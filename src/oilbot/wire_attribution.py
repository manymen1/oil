"""Literal wire credits and citations are distinct from original actor claims."""
import re

VERSION = "wire-provenance-v2"
WIRES = ("Reuters", "Bloomberg", "Associated Press", "AFP")


def wire_provenance(text, author=None):
    evidence, credited, cited = [], set(), set()
    for name in WIRES:
        wire = name.lower().replace(" ", "_")
        # Only a leading credit on a text line or the entire author field.
        # "a statement by Reuters" is a citation, not an article byline.
        pattern = rf"^[ \t]*(?:\([ \t]*{name}[ \t]*\)|by[ \t]+{name}(?=[ \t]*(?:$|[—–:-])))"
        for match in re.finditer(pattern, text, re.I | re.M):
            credited.add(wire)
            evidence.append({"wire": wire, "basis": "LEADING_WIRE_CREDIT", "field": "text",
                             "start": match.start(), "end": match.end(), "quote": match.group()})
        if author:
            match = re.fullmatch(rf"\s*(?:by\s+)?{name}\s*", author, re.I)
            if match:
                credited.add(wire)
                evidence.append({"wire": wire, "basis": "AUTHOR_FIELD", "field": "author",
                                 "start": match.start(), "end": match.end(), "quote": match.group()})
        for match in re.finditer(rf"\baccording to\s+{name}\b", text, re.I):
            cited.add(wire)
            evidence.append({"wire": wire, "basis": "SOURCE_CITATION", "field": "text",
                             "start": match.start(), "end": match.end(), "quote": match.group()})
    return {"origin": next(iter(credited)) if len(credited) == 1 else None,
            "source_attributions": tuple(sorted(cited)), "wire_evidence": tuple(evidence),
            "wire_provenance_version": VERSION}
