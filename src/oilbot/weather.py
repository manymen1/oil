"""NHC weather observations and geographic research screening, never oil outages."""
import base64
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from .clock import epoch_ns, iso_ns, utc_now
from .schema import NewsItem, canonical, digest
from .sources import ParseFailure

VERSION = "nhc-status-v1"
ENDPOINT = "https://www.nhc.noaa.gov/CurrentStorms.json"


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ParseFailure("duplicate JSON field")
        value[key] = item
    return value


def parse_storms(body):
    if len(body) > 2 * 1024 * 1024:
        raise ParseFailure("NHC payload exceeds 2 MiB parser budget")
    try:
        data = json.loads(body, object_pairs_hook=unique_object)
        if not isinstance(data, dict) or not isinstance(data.get("activeStorms"), list) or len(data["activeStorms"]) > 30:
            raise ParseFailure("bounded activeStorms array required")
        result, seen = [], set()
        for row in data["activeStorms"]:
            identity = row["id"]
            if not isinstance(identity, str) or not re.fullmatch(r"(?:al|ep|cp)\d{2}\d{4}", identity) or identity in seen:
                raise ParseFailure("invalid or duplicate storm identity")
            seen.add(identity)
            name, classification = row["name"], row["classification"]
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
                raise ParseFailure("invalid storm name")
            if not isinstance(classification, str) or not re.fullmatch(r"[A-Z]{1,4}", classification):
                raise ParseFailure("invalid classification code")
            lat, lon = row["latitudeNumeric"], row["longitudeNumeric"]
            if any(type(x) not in {int, float} or not math.isfinite(x) for x in (lat, lon)) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ParseFailure("invalid storm coordinates")
            for key, value, hemispheres in (("latitude", lat, "NS"), ("longitude", lon, "EW")):
                match = re.fullmatch(r"(\d+(?:\.\d+)?)([" + hemispheres + "])", row[key])
                if not match or abs(float(match[1]) * (-1 if match[2] in "SW" else 1) - value) > .05:
                    raise ParseFailure("coordinate representations disagree")
            advisory = row["publicAdvisory"]
            number, issued, link = advisory["advNum"], advisory["issuance"], advisory["url"]
            if not isinstance(number, str) or not re.fullmatch(r"\d{1,4}[a-zA-Z]?", number):
                raise ParseFailure("invalid advisory number")
            for value in (issued, row["lastUpdate"]):
                epoch_ns(value)
            if advisory.get("fileUpdateTime") is not None:
                epoch_ns(advisory["fileUpdateTime"])
            parsed = urlsplit(link)
            if (parsed.scheme != "https" or parsed.hostname != "www.nhc.noaa.gov" or parsed.port not in {None, 443}
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or not re.fullmatch(r"/text/[A-Za-z0-9]+\.shtml", parsed.path)):
                raise ParseFailure("unqualified advisory URL")
            # Preserve the provider's numbers, without claiming unverified units
            # or deriving storm categories/price scores from them.
            for key in ("intensity", "pressure"):
                if not isinstance(row[key], str) or not re.fullmatch(r"\d{1,4}(?:\.\d+)?", row[key]):
                    raise ParseFailure("invalid source numeric value")
            result.append({"schema": VERSION, "storm_id": identity, "name": name,
                "classification_code": classification, "latitude": lat, "longitude": lon,
                "advisory_number": number, "advisory_issued_at": iso_ns(epoch_ns(issued)),
                "last_update_at": iso_ns(epoch_ns(row["lastUpdate"])), "file_update_at": advisory.get("fileUpdateTime"),
                "advisory_url": link, "source_intensity": row["intensity"], "source_pressure": row["pressure"],
                "intensity_units": "unverified", "pressure_units": "unverified"})
        return sorted(result, key=lambda s: s["storm_id"])
    except (KeyError, TypeError, ValueError, AttributeError, UnicodeError) as exc:
        if isinstance(exc, ParseFailure):
            raise
        raise ParseFailure("invalid NHC status document: " + type(exc).__name__) from exc


class NHCAdapter:
    def parse(self, payload, url, content_type):
        if url != ENDPOINT or "json" not in content_type.lower():
            raise ParseFailure("registered NHC JSON endpoint required")
        items = []
        for storm in parse_storms(payload):
            # Mutable file-update metadata does not create semantic duplicates.
            normalized = {k: v for k,v in storm.items() if k != "file_update_at"}
            native = ":".join((storm["storm_id"], storm["advisory_issued_at"]))
            items.append(NewsItem(native, storm["advisory_url"],
                f'NHC weather observation: {storm["name"]} ({storm["storm_id"]}), advisory {storm["advisory_number"]}',
                canonical(normalized), published_at=None, origin="nhc"))
        return items


def load_regions(path):
    policy = json.loads(Path(path).read_text())
    if policy["schema"] != "oil-weather-screen-v1" or policy["purpose"] != "heuristic_geographic_screen_only":
        raise ValueError("research-only weather screen required")
    for key in ("max_poll_age_seconds", "max_advisory_age_seconds"):
        if type(policy[key]) is not int or policy[key] <= 0:
            raise ValueError("positive freshness limit required")
    ids = set()
    for region in policy["regions"]:
        if region["id"] in ids:
            raise ValueError("duplicate region")
        ids.add(region["id"])
        south, north, west, east = [region[k] for k in ("south", "north", "west", "east")]
        if any(type(v) not in {int, float} or not math.isfinite(v) for v in (south, north, west, east)) or not -90 <= south < north <= 90 or not -180 <= west < east <= 180:
            raise ValueError("invalid regional bounding box")
    return policy


def weather_report(config, regions, *, at):
    cutoff = epoch_ns(at)
    if cutoff > epoch_ns(utc_now()):
        raise ValueError("future weather report cutoff")
    sources = [s for s in config.sources if s["adapter"] == "nhc_json"]
    if len(sources) != 1 or sources[0].get("parser_contract") != VERSION:
        raise ValueError("one version-bound NHC source required")
    source = sources[0]
    with closing(sqlite3.connect(config.db("news").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute("SELECT * FROM records ORDER BY seq")
                if epoch_ns(r["available_at"]) <= cutoff]
    parsed = {rid: r for r in rows if r["kind"] == "parse_receipt" for rid in r["payload"]["input_revision_ids"]}
    snapshots, successful, failed, seen_storms = [], [], [], set()
    for row in rows:
        p = row["payload"]
        if row["kind"] != "observation" or p.get("source_id") != source["id"]:
            continue
        if p.get("source_policy") != digest(source):
            raise ValueError("weather capture source policy mismatch")
        if row["id"] not in parsed or p["status"] not in {200, 304}:
            failed.append(row)
            continue
        if epoch_ns(parsed[row["id"]]["available_at"]) < epoch_ns(row["available_at"]):
            raise ValueError("weather parse precedes receipt")
        successful.append(row)
        if p["status"] == 304:
            continue
        raw = base64.b64decode(p["body_b64"], validate=True)
        if hashlib.sha256(raw).hexdigest() != p["sha256"]:
            raise ValueError("weather raw hash mismatch")
        # Reparse only as-of available captures under the bound parser contract.
        NHCAdapter().parse(raw, p["url"], p["content_type"])
        storms = parse_storms(raw)
        seen_storms.update(s["storm_id"] for s in storms)
        snapshots.append((row, storms))
    issues, output = [], []
    snapshot, storms = snapshots[-1] if snapshots else (None, [])
    last_poll = successful[-1] if successful else None
    if not snapshot:
        issues.append("NO_PARSED_NHC_SNAPSHOT")
    if not last_poll or (cutoff - epoch_ns(last_poll["available_at"])) / 1e9 > regions["max_poll_age_seconds"]:
        issues.append("SOURCE_POLL_STALE_OR_MISSING")
    if failed and (not last_poll or failed[-1]["seq"] > last_poll["seq"]):
        issues.append("NEWER_FAILED_OR_UNPARSED_CAPTURE")
    source_health = [r for r in rows if r["kind"] == "source_health" and r["payload"].get("source_id") == source["id"]]
    if source_health and source_health[-1]["payload"]["status"] not in {"OK", "EMPTY", "UNCHANGED", "RECOVERED_UNPARSED_RESPONSE"}:
        issues.append("LATEST_SOURCE_HEALTH_FAILURE")
    if snapshot and snapshot["payload"].get("delivery") != "http":
        issues.append("LOCAL_IMPORT_NOT_PROSPECTIVE")
    for storm in storms:
        matches = [r["id"] for r in regions["regions"] if r["south"] <= storm["latitude"] <= r["north"]
                   and r["west"] <= storm["longitude"] <= r["east"]]
        age = (cutoff - epoch_ns(storm["last_update_at"])) / 1e9
        reasons = list(issues)
        if age > regions["max_advisory_age_seconds"]:
            reasons.append("ADVISORY_STALE")
        if age < 0:
            reasons.append("NOMINAL_ADVISORY_TIME_IN_FUTURE")
        output.append({**storm, "region_matches": matches, "advisory_age_seconds": age,
            "screen": "REVIEW_DATA_QUALITY" if reasons else "REGIONAL_WEATHER_WATCH" if matches else "OUTSIDE_SCREENED_REGIONS",
            "reason_codes": reasons, "release_group": storm["storm_id"],
            "input_revision_ids": [snapshot["id"], parsed[snapshot["id"]]["id"]],
            "available_at": parsed[snapshot["id"]]["available_at"],
            "confirmed_disruption": False, "affected_oil_bpd": None, "price_direction": None, "trade_authorized": False})
    # Feed rotation/removal is not an all-clear, a cancellation or a restoration.
    absent = sorted(seen_storms - {s["storm_id"] for s in storms})
    return {"schema": "oil-weather-report-v1", "through": at, "source_policy_hash": digest(source),
        "screen_policy": regions, "screen_policy_hash": digest(regions), "issues": issues, "storms": output,
        "snapshot_id": snapshot["id"] if snapshot else None,
        "snapshot_role": ("initial_capture" if len(snapshots) == 1 else "later_capture") if snapshot else None,
        "raw_body_sha256": snapshot["payload"]["sha256"] if snapshot else None,
        "last_successful_poll_id": last_poll["id"] if last_poll else None,
        "not_in_latest_feed": absent, "absence_meaning": "unknown_not_restoration",
        "trade_authorized": False, "price_direction": None, "economic_evaluation": "unavailable",
        "limitations": ["Derived research screen, not an official NOAA/NWS product or safety warning.",
            "Current storm centers only; no forecast tracks, wind footprint, surge or asset intersection.",
            "Outside a screening box does not mean no oil exposure; no mapped facility capacity.",
            "Nominal advisory issuance is not verified publication time; availability uses local capture/parse.",
            "No confirmed outage, shut-in volume, recovery, price impact or execution authorization."]}
