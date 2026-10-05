"""BSEE operator-reported shut-in estimates, not measured lost/restored barrels."""
import base64
from contextlib import closing
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .clock import epoch_ns, instant, utc_now
from .macro import MAX_BYTES, number
from .schema import NewsItem, canonical, digest
from .sources import ParseFailure

VERSION = "bsee-shutin-v1"
PREFIX = "/newsroom/latest-news/statements-and-releases/press-releases/"


def valid_report_url(url):
    try:
        u = urlsplit(url)
        return (u.scheme == "https" and u.netloc == "www.bsee.gov" and not u.query and not u.fragment
                and re.fullmatch(re.escape(PREFIX) + r"[a-z0-9]+(?:-[a-z0-9]+)*", u.path) is not None)
    except (TypeError, ValueError):
        return False


def normalized(node):
    return " ".join(node.get_text(" ", strip=True).split())


def parse_bsee(body):
    if len(body) > MAX_BYTES:
        raise ParseFailure("BSEE report size limit")
    soup = BeautifulSoup(body, "html.parser")
    titles = {normalized(h) for h in soup.select("h1")}
    articles = soup.select("article")
    if len(titles) != 1 or len(articles) != 1:
        raise ParseFailure("one BSEE report title and article required")
    title, article = titles.pop(), articles[0]
    text = normalized(article)
    if "BSEE" not in title or not re.search(r"operator reports|operators.*reports", text, re.I):
        raise ParseFailure("operator-reported BSEE evidence required")
    scope = re.findall(r"Gulf of (Mexico|America)", title + " " + text)
    if not scope or len(set(scope)) != 1:
        raise ParseFailure("unambiguous Gulf report scope required")
    facts = {}
    for table in article.select("table"):
        qualified_header = False
        for row in table.select("tr"):
            cells = [normalized(c) for c in row.find_all(["td", "th"], recursive=False)]
            if len(cells) == 3 and cells[1].lower() == "total shut-in" and cells[2] in {"Percentage of GOM Production", "Percentage of GOA Production"}:
                qualified_header = True
                continue
            if not cells or cells[0] not in {"Oil, BOPD Shut-in", "Gas, MMCFD Shut-in"}:
                continue
            if not qualified_header or len(cells) != 3:
                raise ParseFailure("unknown BSEE shut-in table header")
            key = "oil" if cells[0].startswith("Oil") else "gas"
            if key in facts:
                raise ParseFailure("duplicate BSEE shut-in measure")
            try:
                amount, percent = map(number, cells[1:])
            except ValueError as exc:
                raise ParseFailure("invalid BSEE shut-in number") from exc
            if not 0 <= amount <= (10000000 if key == "oil" else 100000) or not 0 <= percent <= 100:
                raise ParseFailure("BSEE shut-in bounds")
            if (amount == 0) != (percent == 0):
                raise ParseFailure("BSEE zero volume/percentage disagreement")
            if key == "oil" and amount != amount.to_integral_value():
                raise ParseFailure("integer BOPD required")
            facts[key] = {"estimated_shut_in": str(amount), "percent_of_reported_production": str(percent),
                "units": "barrels_per_day" if key == "oil" else "million_cubic_feet_per_day",
                "evidence_cells": cells}
    if set(facts) != {"oil", "gas"}:
        raise ParseFailure("oil and gas shut-in table required; absence is not zero")
    for percentage, commodity in re.findall(
            r"approximately ([\d.]+) percent of the (?:current )?(oil|natural gas) production[^.]*?remains shut-in", text):
        key = "oil" if commodity == "oil" else "gas"
        if number(percentage) != number(facts[key]["percent_of_reported_production"]):
            raise ParseFailure("BSEE prose/table percentage disagreement")
    # Only explicitly tagged publication dates are accepted, never image filenames,
    # HTTP modification dates, archive banners or the local capture date.
    dates = set()
    for node in article.select('time[itemprop="datePublished"][datetime]'):
        value = node["datetime"]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ParseFailure("unsupported BSEE publication-date layout")
        try:
            dates.add(date.fromisoformat(value).isoformat())
        except ValueError as exc:
            raise ParseFailure("invalid BSEE report date") from exc
    if len(dates) > 1:
        raise ParseFailure("conflicting BSEE report dates")
    names = set(re.findall(r"(?:Hurricane|Tropical Storm|Tropical Depression) ([A-Z][a-z]+)", title))
    surveys = sorted(set(re.findall(r"as of \d{1,2}:\d{2}(?: [ap]\.?m\.?)? (?:CDT|CST) today", text)))
    return {"schema": VERSION, "title": title, "report_date": next(iter(dates), None),
            "published_at": None, "storm_name": next(iter(names)) if len(names) == 1 else None,
            "scope": "Gulf of " + scope[0], "survey_time_text": surveys,
            "archived": "ARCHIVED" in text and "NOT UPDATED" in text,
            "facts": facts, "measurement": "operator_reported_estimate_not_metered_loss",
            "restored_oil_bpd": None, "confirmed_restoration": False, "price_direction": None,
            "trade_authorized": False}


class BSEEAdapter:
    def __init__(self, url):
        if not valid_report_url(url):
            raise ParseFailure("registered BSEE report URL required")
        self.url = url

    def parse(self, body, url, content_type):
        if url != self.url or "html" not in content_type.lower():
            raise ParseFailure("exact registered BSEE HTML report required")
        value = parse_bsee(body)
        return [NewsItem(url, url, value["title"], canonical(value), published_at=None, origin="bsee")]


def bsee_report(config, *, at):
    cutoff = epoch_ns(at)
    if cutoff > epoch_ns(utc_now()):
        raise ValueError("future BSEE cutoff")
    sources = [s for s in config.sources if s["adapter"] == "bsee_report"]
    if not sources:
        raise ValueError("registered BSEE report source required")
    with closing(sqlite3.connect(config.db("news").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute("SELECT * FROM records ORDER BY seq")
                if epoch_ns(r["available_at"]) <= cutoff]
    parsed = {rid: r for r in rows if r["kind"] == "parse_receipt" for rid in r["payload"]["input_revision_ids"]}
    output = []
    for source in sources:
        if source.get("parser_contract") != VERSION:
            raise ValueError("BSEE parser contract mismatch")
        snapshots, failed, polls = [], [], []
        for row in rows:
            p = row["payload"]
            if row["kind"] != "observation" or p.get("source_id") != source["id"]:
                continue
            if p.get("source_policy") != digest(source):
                raise ValueError("BSEE capture policy mismatch")
            receipt = parsed.get(row["id"])
            if not receipt or p["status"] not in {200, 304}:
                failed.append(row)
                continue
            if epoch_ns(receipt["available_at"]) < epoch_ns(row["available_at"]):
                raise ValueError("BSEE parse precedes capture")
            polls.append(row)
            if p["status"] == 304:
                continue
            body = base64.b64decode(p["body_b64"], validate=True)
            if hashlib.sha256(body).hexdigest() != p["sha256"]:
                raise ValueError("BSEE raw hash mismatch")
            value = json.loads(BSEEAdapter(source["url"]).parse(body, p["url"], p["content_type"])[0].text)
            snapshots.append((row, receipt, value))
        snapshot, receipt, value = snapshots[-1] if snapshots else (None, None, None)
        last_poll = polls[-1] if polls else None
        issues = ["NO_PARSED_BSEE_REPORT"] if not snapshot else []
        if not last_poll or (cutoff-epoch_ns(last_poll["available_at"])) / 1e9 > 86400:
            issues.append("POLL_STALE_OR_MISSING")
        if failed and (not snapshot or failed[-1]["seq"] > snapshot["seq"]):
            issues.append("NEWER_FAILED_OR_UNPARSED_CAPTURE")
        health = [r for r in rows if r["kind"] == "source_health" and r["payload"].get("source_id") == source["id"]]
        if health and health[-1]["payload"]["status"] not in {"OK", "EMPTY", "UNCHANGED", "RECOVERED_UNPARSED_RESPONSE"}:
            issues.append("LATEST_SOURCE_HEALTH_FAILURE")
        if value:
            if value["archived"]:
                issues.append("ARCHIVED_REPORT_NOT_CURRENT")
            if not value["report_date"]:
                issues.append("REPORT_DATE_UNVERIFIED")
            else:
                age = (instant(at).date() - date.fromisoformat(value["report_date"])).days
                if age < 0 or age > 3:
                    issues.append("REPORT_DATE_FUTURE_OR_STALE")
            if snapshot["payload"].get("delivery") != "http":
                issues.append("LOCAL_IMPORT_NOT_PROSPECTIVE")
        output.append({"source_id": source["id"], "source_url": source["url"], "source_policy_hash": digest(source),
            "observation": value, "issues": issues,
            "input_revision_ids": [snapshot["id"], receipt["id"]] if snapshot else [],
            "available_at": receipt["available_at"] if receipt else None,
            "raw_body_sha256": snapshot["payload"]["sha256"] if snapshot else None,
            "status": "REVIEW_REQUIRED" if issues else "REPORTED_ESTIMATE_ONLY"})
    return {"schema": "bsee-shutin-report-v1", "through": at, "reports": output,
            "trade_authorized": False, "price_direction": None, "confirmed_restoration": False,
            "limitations": ["Explicit report URLs only; no automatic discovery or complete coverage claim.",
                "Estimated shut-in rates are not metered cumulative losses or verified price effects.",
                "A declining estimate, absent report, returned rig or future restart plan is not confirmed oil restoration.",
                "No automatic cross-report differencing, episode linking or inferred restoration volumes."]}
