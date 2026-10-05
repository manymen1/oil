import base64
from datetime import timedelta
import hashlib
import json
from pathlib import Path

import pytest

from oilbot.clock import instant, utc_now
from oilbot.macro import MacroRecorder, parse_eia, parse_cftc, fetch_macro, read_macro

EIA = Path("tests/fixtures/eia-stocks-synthetic.csv").read_bytes()
AT = "2026-09-23T14:30:00+00:00"


def cot(period="2026-09-22", **changes):
    return {"id": "synthetic", "report_date_as_yyyy_mm_dd": period + "T00:00:00.000",
        "cftc_contract_market_code": "067651", "cftc_market_code": "NYME",
        "open_interest_all": "1000", "m_money_positions_long_all": "300",
        "m_money_positions_short_all": "100", "m_money_positions_spread": "200", **changes}


def test_eia_units_and_stock_section_only():
    value = parse_eia(EIA)[0]
    assert value["units"] == "million_barrels" and value["period"] == "2026-09-18"
    assert value["facts"]["commercial_crude"]["change"] == "-3.000"
    assert value["published_at"] is None


@pytest.mark.parametrize("before,after", [(b"STUB_1", b"BROKEN"), (b"9/11/26", b"9/10/26"),
    (b'"-3.000"', b'"-30.000"'), (b'"420.000"', b'"NaN"'),
    (b'"420.000"', b'"-420.000"'), (b"Distillate Fuel Oil", b"Missing category")])
def test_eia_schema_and_number_guards(before, after):
    with pytest.raises(ValueError):
        parse_eia(EIA.replace(before, after))


def test_eia_duplicate_and_rounding():
    with pytest.raises(ValueError, match="duplicate"):
        parse_eia(EIA.replace(b'"Strategic', EIA.splitlines()[1] + b'\n"Strategic', 1))
    assert parse_eia(EIA.replace(b'"-3.000"', b'"-3.001"'))


def test_cftc_positions_not_publication_date():
    rows = parse_cftc(json.dumps([cot(), cot("2026-09-15")]).encode())
    assert [r["period"] for r in rows] == ["2026-09-15", "2026-09-22"]
    assert rows[0]["facts"]["managed_money_net"] == "200"
    assert rows[0]["facts"]["net_fraction_open_interest"] == "0.2"
    assert rows[0]["published_at"] is None


@pytest.mark.parametrize("changes", [{"cftc_contract_market_code": "067411"}, {"cftc_market_code": "ICE"},
    {"open_interest_all": "0"}, {"m_money_positions_long_all": "NaN"},
    {"m_money_positions_short_all": "-1"}, {"m_money_positions_spread": "900"},
    {"m_money_positions_long_all": "1.5"}, {"report_date_as_yyyy_mm_dd": "2026-09-22T12:00:00.000"}])
def test_cftc_rejects_wrong_market_or_bad_data(changes):
    with pytest.raises(ValueError):
        parse_cftc(json.dumps([cot(**changes)]).encode())


def test_cftc_duplicate_or_empty():
    for value in [[], {}, [cot(), cot()], [cot(), cot(), cot()]]:
        with pytest.raises(ValueError):
            parse_cftc(json.dumps(value).encode())


def test_raw_first_parse_failure_and_no_future_period(tmp_path):
    w = MacroRecorder(tmp_path)
    result = w.ingest("eia", b"broken", received_at=AT)
    assert result["status"] == "FAILED"
    assert base64.b64decode(w.store.get(result["capture_id"])["payload"]["body_base64"]) == b"broken"
    result = w.ingest("eia", EIA, received_at="2026-09-01T00:00:00Z")
    assert result["reason"] == "future reporting period"


def test_baseline_unchanged_new_period_and_correction(tmp_path):
    w = MacroRecorder(tmp_path)
    first = w.ingest("eia", EIA, received_at=AT)
    assert w.store.get(first["revision_ids"][0])["payload"]["initial_snapshot"]
    unchanged = w.ingest("eia", EIA, received_at="2026-09-30T14:29:00Z")
    assert unchanged["revision_ids"] == []
    next_week = EIA.replace(b"9/18/26", b"9/25/26").replace(b"9/11/26", b"9/18/26")
    new = w.ingest("eia", next_week, received_at="2026-09-30T14:30:00Z")
    p = w.store.get(new["revision_ids"][0])["payload"]
    assert not p["initial_snapshot"] and not p["revision"]
    assert p["prior_latest_period"] == "2026-09-18"
    assert instant(p["previous_successful_receipt_at"]) == instant("2026-09-30T14:29:00Z")
    revised = next_week.replace(b"420.000", b"419.000").replace(b"-3.000", b"-4.000")
    correction = w.ingest("eia", revised, received_at="2026-09-30T14:31:00Z")
    p = w.store.get(correction["revision_ids"][0])["payload"]
    assert p["revision"] and p["supersedes_id"] == new["revision_ids"][0]
    assert len(read_macro(w.store.path, through="2026-09-30T14:30:30Z")) == 2
    assert len(read_macro(w.store.path, through="2026-09-30T14:31:00Z")) == 3


def test_imports_cannot_become_prospective_or_mix_with_http(tmp_path):
    w = MacroRecorder(tmp_path, delivery="local_import")
    one = w.ingest("eia", EIA, received_at=AT)
    p = w.store.get(one["revision_ids"][0])["payload"]
    assert p["initial_snapshot"] and p["delivery"] == "local_import"
    with pytest.raises(ValueError, match="policy"):
        MacroRecorder(tmp_path)
    with pytest.raises(ValueError):
        w.collect("cftc")


def test_crash_recovery_does_not_backdate_parsed_availability(tmp_path):
    w = MacroRecorder(tmp_path)
    cid = w.store.append("macro_capture", {"source": "eia", "delivery": "http", "status": 200,
        "body_base64": base64.b64encode(EIA).decode(), "body_sha256": hashlib.sha256(EIA).hexdigest(),
        "received_at": AT}, available_at=AT)
    parsed_at = "2026-09-23T14:40:00Z"
    result = w.parse_capture(cid, parsed_at=parsed_at)
    assert read_macro(w.store.path, through="2026-09-23T14:35:00Z") == []
    assert len(read_macro(w.store.path, through=parsed_at)) == 1
    assert w.parse_capture(cid, parsed_at=parsed_at) == result
    assert w.recover() == []


class Response:
    def __init__(self, body=b"", status=200, headers=None):
        self.body, self.status_code, self.headers = body, status, headers or {}

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size): yield self.body


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_fetch_official_redirect_only_and_no_signed_url_leak():
    s = Session(Response(status=302, headers={"Location": "/secure/wpsr/table1.csv?Signature=secret"}), Response(EIA))
    body, status, meta = fetch_macro("eia", session=s, contact="bot@example.com")
    assert body == EIA and status == 200 and len(s.calls) == 2
    assert "secret" not in json.dumps(meta)
    assert all(not args["allow_redirects"] for _, args in s.calls)
    for location in ("https://evil.example/secure/wpsr/table1.csv", "/unknown", "http://ir.eia.gov/secure/wpsr/table1.csv"):
        with pytest.raises(ValueError, match="redirect"):
            fetch_macro("eia", session=Session(Response(status=302, headers={"Location": location})))


def test_contact_throttle_and_denial_latch(tmp_path):
    w = MacroRecorder(tmp_path)
    session = Session(Response(status=403))
    assert w.collect("eia", session=session)["reason"] == "EIA_OWNER_CONTACT_REQUIRED"
    assert not session.calls
    result = w.collect("eia", session=session, contact="bot@example.com")
    assert result["status"] == "FAILED"
    assert w.collect("eia", session=session, contact="bot@example.com")["reason"] == "ACCESS_DENIAL_LATCHED"
    c = Session(Response(json.dumps([cot("2026-09-01")]).encode()))
    assert w.collect("cftc", session=c)["status"] == "OK"
    assert w.collect("cftc", session=c)["status"] == "WAIT"
    assert len(c.calls) == 1


def test_missing_read_only_journal_does_not_create(tmp_path):
    with pytest.raises(Exception):
        read_macro(tmp_path / "missing.sqlite3", through=AT)
    assert not (tmp_path / "missing.sqlite3").exists()
