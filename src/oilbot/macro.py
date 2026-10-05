"""Point-in-time EIA inventory and CFTC positioning observations.

Separate from news interpretation and execution. A reporting period is never
treated as publication time; each revision becomes available only after receipt
and parsing. Current endpoints cannot reconstruct historical first releases.
"""
from __future__ import annotations

import base64
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urljoin, urlsplit

import requests

from .clock import epoch_ns, instant, utc_now
from .schema import digest
from .store import Journal, component_lock

VERSION = "oil-macro-v1"
MAX_BYTES = 2 * 1024 * 1024
SOURCES = {
    "eia": {"url": "https://ir.eia.gov/wpsr/table1.csv", "interval_seconds": 60,
            "owner": "U.S. Energy Information Administration", "series": "WPSR table 1 stocks"},
    "cftc": {"url": "https://publicreporting.cftc.gov/resource/72hh-3qpy.json",
             "interval_seconds": 3600, "owner": "Commodity Futures Trading Commission",
             "series": "Disaggregated futures only / WTI physical / 067651"},
}
CFTC_FIELDS = ("id", "report_date_as_yyyy_mm_dd", "cftc_contract_market_code", "cftc_market_code",
               "market_and_exchange_names", "open_interest_all", "m_money_positions_long_all",
               "m_money_positions_short_all", "m_money_positions_spread")
STOCKS = {"Commercial (Excluding SPR)": "commercial_crude", "Strategic Petroleum Reserve (SPR)": "spr",
          "Total Motor Gasoline": "gasoline", "Distillate Fuel Oil": "distillate"}


def number(value):
    if not isinstance(value, str) or not re.fullmatch(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", value.strip()):
        raise ValueError("missing or invalid numeric observation")
    result = Decimal(value.strip().replace(",", ""))
    if not result.is_finite() or abs(result) > Decimal("1e9"):
        raise ValueError("numeric observation outside bounds")
    return result


def eia_date(value):
    if not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2}", value):
        raise ValueError("unknown EIA date layout")
    month, day, year = map(int, value.split("/"))
    return date(2000 + year, month, day)


def parse_eia(body):
    # The ASCII stock section precedes cp1252 footnotes/flow tables. Do not
    # confuse million-barrel stock changes with thousand-barrel/day flows.
    text = body.decode("utf-8-sig" if body.startswith(b"\xef\xbb\xbf") else "cp1252")
    rows = list(csv.reader(io.StringIO(text), strict=True))
    if not rows or len(rows[0]) != 8 or rows[0][0] != "STUB_1" or rows[0][3:5] != ["Difference", "Percent Change"]:
        raise ValueError("unknown EIA stock header")
    current, previous = eia_date(rows[0][1]), eia_date(rows[0][2])
    if current - previous != timedelta(days=7):
        raise ValueError("EIA stock periods must be consecutive weeks")
    facts = {}
    for row in rows[1:]:
        if row and row[0] == "STUB_1":
            break
        if not row or row[0] not in STOCKS:
            continue
        key = STOCKS[row[0]]
        if key in facts or len(row) != 8:
            raise ValueError("duplicate or malformed EIA stock row")
        level, prior, change = map(number, row[1:4])
        if min(level, prior) < 0 or max(level, prior) > 10000 or abs(level - prior - change) > Decimal(".002"):
            raise ValueError("EIA level/change consistency failure")
        facts[key] = {"level": str(level), "previous_level": str(prior), "change": str(change)}
    if set(facts) != set(STOCKS.values()):
        raise ValueError("required EIA stocks missing")
    return [{"period": current.isoformat(), "previous_period": previous.isoformat(),
             "units": "million_barrels", "facts": facts, "published_at": None}]


def parse_cftc(body):
    rows = json.loads(body)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2:
        raise ValueError("expected one or two bounded CFTC observations")
    result, dates = [], set()
    for row in rows:
        if (row.get("cftc_contract_market_code"), row.get("cftc_market_code")) != ("067651", "NYME"):
            raise ValueError("wrong CFTC market; require NYMEX WTI physical futures only")
        timestamp = row.get("report_date_as_yyyy_mm_dd", "")
        if not re.fullmatch(r"\d{4}-\d\d-\d\dT00:00:00(?:\.000)?", timestamp):
            raise ValueError("unknown CFTC report date layout")
        period = date.fromisoformat(timestamp[:10]).isoformat()
        if period in dates:
            raise ValueError("duplicate CFTC report period")
        dates.add(period)
        oi, long, short, spread = [number(row[k]) for k in
            ("open_interest_all", "m_money_positions_long_all", "m_money_positions_short_all", "m_money_positions_spread")]
        if oi <= 0 or any(v < 0 or v != v.to_integral_value() for v in (oi, long, short, spread)) or max(long + spread, short + spread) > oi:
            raise ValueError("invalid CFTC positions/open interest")
        result.append({"period": period, "units": "contracts", "published_at": None,
            "market_code": "067651", "report_type": "disaggregated_futures_only",
            "facts": {"open_interest": str(oi), "managed_money_long": str(long),
                "managed_money_short": str(short), "managed_money_spread": str(spread),
                "managed_money_net": str(long - short), "net_fraction_open_interest": str((long - short) / oi)}})
    return sorted(result, key=lambda r: r["period"])


PARSERS = {"eia": parse_eia, "cftc": parse_cftc}


class MacroRecorder:
    version = VERSION
    sources = SOURCES
    parsers = PARSERS

    def policy(self, delivery):
        return {"schema": self.version, "sources": self.sources, "delivery": delivery,
                "cftc_fields": CFTC_FIELDS, "model_processing": "not_used", "broker_execution": "disabled"}

    def fetch(self, source, *, session=None, contact=None):
        return fetch_macro(source, session=session, contact=contact)

    def __init__(self, root, *, delivery="http"):
        if delivery not in {"http", "local_import"}:
            raise ValueError("unknown delivery mode")
        self.root = Path(root).resolve()
        self.store = Journal(self.root / "macro.sqlite3")
        self.delivery = delivery
        policy = self.policy(delivery)
        with self.store.transaction() as db:
            foreign = db.execute("SELECT kind FROM records WHERE kind NOT LIKE 'macro_%' LIMIT 1").fetchone()
            if foreign:
                raise ValueError("macro recorder requires a dedicated journal")
            rows = db.execute("SELECT payload FROM records WHERE kind='macro_policy'").fetchall()
            if rows and any(digest(json.loads(r[0])) != digest(policy) for r in rows):
                raise ValueError("macro policy changed; use a separate data root")
            self.store.append("macro_policy", policy, record_id=digest(policy),
                available_at=(self.store.get(digest(policy))["available_at"] if rows else utc_now()), db=db)

    def ingest(self, source, body, *, received_at=None, status=200, metadata=None):
        """Raw bytes commit first. Caller holds the source component lock."""
        if source not in self.sources or len(body) > MAX_BYTES:
            raise ValueError("unknown source or oversized response")
        at = received_at or utc_now()
        capture = {"source": source, "delivery": self.delivery, "status": status,
            "body_base64": base64.b64encode(body).decode(), "body_sha256": hashlib.sha256(body).hexdigest(),
            "metadata": metadata or {}, "received_at": at}
        cid = self.store.append("macro_capture", capture, available_at=at)
        return self.parse_capture(cid, parsed_at=received_at or utc_now())

    def parse_capture(self, capture_id, *, parsed_at=None):
        at = parsed_at or utc_now()
        capture = self.store.get(capture_id)
        if not capture or capture["kind"] != "macro_capture":
            raise ValueError("raw macro capture required")
        p = capture["payload"]
        if epoch_ns(at) < epoch_ns(capture["available_at"]):
            raise ValueError("parse time precedes receipt")
        done = [r for r in self.store.records("macro_parse") if r["payload"]["capture_id"] == capture_id]
        if done:
            return done[0]["payload"]
        source = p["source"]
        try:
            if p["status"] != 200:
                raise ValueError(f"HTTP_{p['status']}")
            body = base64.b64decode(p["body_base64"], validate=True)
            if hashlib.sha256(body).hexdigest() != p["body_sha256"]:
                raise ValueError("raw macro body hash mismatch")
            values = self.parsers[source](body)
            if any(date.fromisoformat(v["period"]) > instant(p["received_at"]).date() for v in values):
                raise ValueError("future reporting period")
        except (ValueError, KeyError, TypeError, UnicodeError, csv.Error) as exc:
            result = {"capture_id": capture_id, "source": source, "status": "FAILED", "reason": str(exc), "revision_ids": []}
            self.store.append("macro_parse", result, available_at=at)
            return result
        old = [r for r in self.store.records("macro_revision") if r["payload"]["source"] == source]
        successful = [r for r in self.store.records("macro_parse") if r["payload"]["source"] == source and r["payload"]["status"] == "OK"]
        prior_capture = self.store.get(successful[-1]["payload"]["capture_id"]) if successful else None
        if prior_capture and epoch_ns(p["received_at"]) < epoch_ns(prior_capture["available_at"]):
            raise ValueError("macro source receipt clock regressed")
        if old and epoch_ns(at) < max(epoch_ns(r["available_at"]) for r in old):
            raise ValueError("macro parsing clock regressed")
        latest_period = max((r["payload"]["observation"]["period"] for r in old), default=None)
        ids = []
        with self.store.transaction() as db:
            for value in values:
                prior = next((r for r in reversed(old) if r["payload"]["observation"]["period"] == value["period"]), None)
                if prior and prior["payload"]["observation"] == value:
                    continue
                baseline = not old or self.delivery != "http" or (latest_period is not None and value["period"] < latest_period)
                payload = {"schema": self.version, "source": source, "observation": value,
                    "received_at": p["received_at"], "available_at": at, "capture_id": capture_id,
                    "capture_body_hash": p["body_sha256"], "input_revision_ids": [capture_id],
                    "initial_snapshot": baseline, "revision": prior is not None,
                    "supersedes_id": prior["id"] if prior else None, "delivery": self.delivery,
                    "previous_successful_receipt_at": prior_capture["available_at"] if prior_capture else None,
                    "prior_latest_period": latest_period, "published_at": None, "trade_authorized": False}
                rid = digest(payload)
                self.store.append("macro_revision", payload, record_id=rid, available_at=at, db=db)
                ids.append(rid)
            result = {"capture_id": capture_id, "source": source, "status": "OK", "revision_ids": ids}
            self.store.append("macro_parse", result, available_at=at, db=db)
        return result

    def recover(self):
        # Inspect IDs only; a frequent scheduler tick must not reload all archived bytes.
        with self.store.connect() as db:
            pending = db.execute("""SELECT id FROM records WHERE kind='macro_capture' AND id NOT IN
                (SELECT json_extract(payload,'$.capture_id') FROM records WHERE kind='macro_parse')
                ORDER BY seq""").fetchall()
        return [self.parse_capture(r["id"]) for r in pending]

    def collect(self, source, *, session=None, contact=None):
        if source not in self.sources or self.delivery != "http":
            raise ValueError("registered HTTP macro source required")
        if source == "eia" and (not contact or not re.fullmatch(r"[^\s<>]+@[^\s<>]+\.[^\s<>]+|https://[^\s<>]+", contact)):
            return {"source": source, "status": "BLOCKED", "reason": "EIA_OWNER_CONTACT_REQUIRED"}
        with component_lock(self.root, "macro"):
            self.recover()
            now = utc_now()
            attempts = [r for r in self.store.records("macro_attempt") if r["payload"]["source"] == source]
            with self.store.connect() as db:
                captures = [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute(
                    """SELECT id,available_at,json_remove(payload,'$.body_base64') AS payload FROM records
                    WHERE kind='macro_capture' AND json_extract(payload,'$.source')=? ORDER BY seq""", (source,))]
            if any(r["payload"]["status"] in {401, 403} for r in captures):
                return {"source": source, "status": "BLOCKED", "reason": "ACCESS_DENIAL_LATCHED"}
            if attempts:
                delta = (epoch_ns(now) - epoch_ns(attempts[-1]["available_at"])) / 1e9
                wait = self.sources[source]["interval_seconds"]
                if captures and captures[-1]["payload"]["status"] == 429:
                    wait = max(wait, 86400)  # conservative, no rapid quota retries
                    retry = captures[-1]["payload"].get("metadata", {}).get("retry_after")
                    if retry:
                        try:
                            retry_seconds = int(retry) if retry.isdigit() else (
                                parsedate_to_datetime(retry) - instant(attempts[-1]["available_at"])).total_seconds()
                            wait = max(wait, retry_seconds)
                        except (ValueError, TypeError):
                            pass
                if delta < wait:
                    return {"source": source, "status": "WAIT", "reason": "POLL_INTERVAL_OR_BACKOFF"}
            self.store.append("macro_attempt", {"source": source}, available_at=now)
            try:
                body, status, metadata = self.fetch(source, session=session, contact=contact)
            except (requests.RequestException, ValueError) as exc:
                result = {"source": source, "status": "FAILED", "reason": type(exc).__name__}
                self.store.append("macro_fetch_error", result)
                return result
            return self.ingest(source, body, status=status, metadata=metadata)


def fetch_macro(source, *, session=None, contact=None, sources=None):
    """Bounded official requests; only EIA's same-host release redirect is followed."""
    registry = SOURCES if sources is None else sources
    url = registry[source]["url"]
    secure_path = "/secure" + urlsplit(url).path
    params = ({"$where": "cftc_contract_market_code='067651'", "$order": "report_date_as_yyyy_mm_dd DESC",
               "$limit": 2, "$select": ",".join(CFTC_FIELDS)} if source == "cftc" else None)
    headers = {"User-Agent": "OilMacroResearch/1.0" + (f" ({contact})" if contact else "")}
    owned = session is None
    session = session or requests.Session()
    if owned:
        session.trust_env = False
    started = time.monotonic()
    hops = []
    try:
        for hop in range(2):
            with session.get(url, params=params, headers=headers, timeout=(5, 15), stream=True, allow_redirects=False) as response:
                hops.append({"status": response.status_code, "host": urlsplit(url).hostname, "path": urlsplit(url).path})
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = urljoin(url, response.headers.get("Location", ""))
                    parsed = urlsplit(target)
                    if source != "eia" or hop or parsed.scheme != "https" or parsed.hostname != "ir.eia.gov" or parsed.path != secure_path or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.fragment:
                        raise ValueError("unqualified macro redirect")
                    url, params = target, None
                    continue
                metadata = {"url": registry[source]["url"], "hops": hops,
                    "content_type": response.headers.get("Content-Type"), "http_date": response.headers.get("Date"),
                    "last_modified": response.headers.get("Last-Modified"), "retry_after": response.headers.get("Retry-After")}
                if response.status_code != 200:
                    # Persist the denial/rate-limit even if its HTML body would
                    # be oversized or stall. Error bodies are not data inputs.
                    return b"", response.status_code, {**metadata, "error_body_omitted": True}
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES or time.monotonic() - started > 30:
                        raise ValueError("macro response byte/time budget exceeded")
                    chunks.append(chunk)
                return b"".join(chunks), response.status_code, metadata
        raise ValueError("macro redirect limit")
    finally:
        if owned:
            session.close()


def read_macro(path, *, through, version=VERSION, parsers=None, _rows=None):
    """Read-only consistent snapshot, verifying revision/raw/parse bindings."""
    at = epoch_ns(through)
    parsers = PARSERS if parsers is None else parsers
    rows = _rows
    if rows is None:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True)
        try:
            db.row_factory = sqlite3.Row
            rows = [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute("SELECT * FROM records ORDER BY seq")]
        finally:
            db.close()
    by_id = {r["id"]: r for r in rows}
    policies = [r for r in rows if r["kind"] == "macro_policy"]
    if len(policies) != 1 or policies[0]["payload"].get("schema") != version or digest(policies[0]["payload"]) != policies[0]["id"]:
        raise ValueError("verified macro policy required")
    parsed = {rid: r for r in rows if r["kind"] == "macro_parse" and r["payload"]["status"] == "OK"
              for rid in r["payload"]["revision_ids"]}
    result = []
    for r in rows:
        if r["kind"] != "macro_revision" or epoch_ns(r["available_at"]) > at:
            continue
        p = r["payload"]
        c = by_id.get(p["capture_id"])
        receipt = parsed.get(r["id"])
        if (not receipt or receipt["payload"]["capture_id"] != p["capture_id"]
                or epoch_ns(receipt["available_at"]) != epoch_ns(r["available_at"])):
            raise ValueError("macro parse receipt binding mismatch")
        if p["schema"] != version or digest(p) != r["id"] or epoch_ns(p["available_at"]) != epoch_ns(r["available_at"]):
            raise ValueError("macro revision identity mismatch")
        if (not c or c["kind"] != "macro_capture" or c["payload"]["source"] != p["source"]
                or c["payload"]["status"] != 200 or c["payload"]["delivery"] != p["delivery"]):
            raise ValueError("macro capture binding mismatch")
        raw = base64.b64decode(c["payload"]["body_base64"], validate=True)
        if (hashlib.sha256(raw).hexdigest() != p["capture_body_hash"] or p["capture_body_hash"] != c["payload"]["body_sha256"]
                or p["observation"] not in parsers[p["source"]](raw)
                or date.fromisoformat(p["observation"]["period"]) > instant(p["received_at"]).date()
                or not epoch_ns(c["available_at"]) == epoch_ns(p["received_at"]) <= epoch_ns(r["available_at"])):
            raise ValueError("macro raw evidence mismatch")
        result.append(r)
    return result
