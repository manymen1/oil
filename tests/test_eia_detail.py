import base64
import csv
import hashlib
import io
import json

import pytest

from oilbot.cli import main
from oilbot.eia_detail import DetailRecorder, SOURCES, detail_report, parse_detail
from oilbot.macro import MacroRecorder, fetch_macro, read_macro

AT = "2026-10-01T12:00:00Z"


def fixture():
    # Entirely invented engineering observations, including deliberately different averages.
    rows = [["STUB_1", "STUB_2", "9/25/26", "9/18/26", "9/26/25", "9/27/24", "9/25/26", "9/26/25"]]
    for label, us, prior, gulf, gp in [("Crude Oil Inputs", "15000", "16000", "7500", "8000"),
        ("Gross Inputs", "16000", "17000", "8000", "8500"),
        ("Operable Capacity", "20000", "20000", "10000", "10000"),
        ("Percent Utilization", "80.0", "85.0", "80.0", "85.0")]:
        rows += [["Refiner Inputs and Utilization ", label, us, prior, "1", "1", "999", "999"],
                 ["Refiner Inputs and Utilization ", "Gulf Coast (PADD 3)", gulf, gp, "1", "1", "999", "999"]]
    rows.append(["Stocks (Million Barrels) ", "Cushing, Oklahoma", "25.123", "24.100", "20", "30", "--", "--"])
    s = io.StringIO(); csv.writer(s).writerows(rows)
    return s.getvalue().encode()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    current = [AT]
    for module in ("oilbot.macro", "oilbot.store", "oilbot.eia_detail"):
        monkeypatch.setattr(module + ".utc_now", lambda: current[0])
    return current


def test_units_weekly_columns_and_percentage_points():
    p = parse_detail(fixture())[0]
    assert p["period"] == "2026-09-25" and p["published_at"] is None
    f = p["facts"]
    assert len(f) == 9
    assert f["cushing_crude_stocks"]["change"] == "1.023"
    assert f["cushing_crude_stocks"]["units"] == "million_barrels"
    assert f["padd3_crude_inputs"]["level"] == "7500"
    assert f["us_utilization"]["change"] == "-5.0"
    assert f["us_utilization"]["change_units"] == "percentage_points"
    assert f["us_gross_inputs"]["units"] == "thousand_barrels_per_day"


@pytest.mark.parametrize("before,after", [(b"STUB_2", b"renamed"), (b"9/18/26", b"9/17/26"),
    (b"9/26/25", b"9/26/27"), (b"25.123", b"NaN"), (b"25.123", b"--"),
    (b"25.123", b"-1"), (b"15000", b"15000.2"), (b"20000", b"0"),
    (b"80.0", b"180.0"), (b"80.0", b"79.0"), (b"Crude Oil Inputs", b"Other Inputs"),
    (b"Cushing, Oklahoma", b"Other stock")])
def test_schema_missingness_units_and_consistency_fail_closed(before, after):
    with pytest.raises(ValueError):
        parse_detail(fixture().replace(before, after))


def test_duplicates_and_zero_stock():
    with pytest.raises(ValueError, match="duplicate"):
        parse_detail(fixture() + fixture().splitlines()[-1] + b"\n")
    assert parse_detail(fixture().replace(b"25.123", b"0"))[0]["facts"]["cushing_crude_stocks"]["level"] == "0"


def test_new_policy_cannot_mix_with_original_and_original_reader_rejects(tmp_path):
    w = DetailRecorder(tmp_path)
    with pytest.raises(ValueError, match="policy"):
        MacroRecorder(tmp_path)
    w.ingest("eia", fixture())
    with pytest.raises(ValueError, match="policy"):
        read_macro(w.store.path, through=AT)
    assert detail_report(w.store.path, at=AT)["initial_snapshot"] is True


def test_raw_first_duplicate_revision_and_asof(tmp_path, clock):
    w = DetailRecorder(tmp_path)
    first = w.ingest("eia", fixture())
    assert not w.ingest("eia", fixture())["revision_ids"]
    clock[0] = "2026-10-01T12:01:00Z"
    second = w.ingest("eia", fixture().replace(b"25.123", b"26.123"))
    assert w.store.get(second["revision_ids"][0])["payload"]["supersedes_id"] == first["revision_ids"][0]
    assert detail_report(w.store.path, at=AT)["latest_revision"]["id"] == first["revision_ids"][0]
    clock[0] = "2026-10-01T12:02:00Z"
    bad = w.ingest("eia", b"broken")
    assert base64.b64decode(w.store.get(bad["capture_id"])["payload"]["body_base64"]) == b"broken"
    report = detail_report(w.store.path, at=clock[0])
    assert "LATEST_CAPTURE_FAILED_UNPARSED_OR_MISSING" in report["issues"]
    assert not report["trade_authorized"] and report["consensus_surprise"] is None


def test_recovery_availability_and_imports(tmp_path, clock):
    w = DetailRecorder(tmp_path, delivery="local_import")
    raw = fixture()
    w.store.append("macro_capture", {"source": "eia", "delivery": "local_import", "status": 200,
        "body_base64": base64.b64encode(raw).decode(), "body_sha256": hashlib.sha256(raw).hexdigest(),
        "received_at": AT}, available_at=AT)
    clock[0] = "2026-10-01T12:30:00Z"
    assert w.recover()[0]["status"] == "OK"
    assert detail_report(w.store.path, at=AT)["latest_revision"] is None
    assert "LOCAL_IMPORT_NOT_PROSPECTIVE" in detail_report(w.store.path, at=clock[0])["issues"]


class Response:
    def __init__(self, body=b"", status=200, headers=None):
        self.body, self.status_code, self.headers = body, status, headers or {}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size): yield self.body


class Session:
    def __init__(self, *responses): self.responses, self.calls = list(responses), []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_detail_redirect_privacy_and_wrong_table():
    s = Session(Response(status=302, headers={"Location": "/secure/wpsr/table9.csv?Signature=private"}), Response(fixture()))
    _, status, meta = fetch_macro("eia", sources=SOURCES, session=s, contact="bot@example.com")
    assert status == 200 and "private" not in json.dumps(meta) and "bot@example" not in json.dumps(meta)
    assert all(not kw["allow_redirects"] for _,kw in s.calls)
    for target in ("/secure/wpsr/table1.csv", "http://ir.eia.gov/secure/wpsr/table9.csv", "https://other.example/table9.csv"):
        with pytest.raises(ValueError, match="redirect"):
            fetch_macro("eia", sources=SOURCES, session=Session(Response(status=302, headers={"Location": target})))


def test_contact_denial_latch_and_hourly_throttle(tmp_path):
    w = DetailRecorder(tmp_path / "ok")
    s = Session(Response(fixture()))
    assert w.collect("eia", session=s)["status"] == "BLOCKED" and not s.calls
    assert w.collect("eia", session=s, contact="bot@example.com")["status"] == "OK"
    assert w.collect("eia", session=s, contact="bot@example.com")["status"] == "WAIT"
    w = DetailRecorder(tmp_path / "denied")
    s = Session(Response(status=403))
    assert w.collect("eia", session=s, contact="bot@example.com")["status"] == "FAILED"
    assert w.collect("eia", session=s, contact="bot@example.com")["reason"] == "ACCESS_DENIAL_LATCHED"
    assert len(s.calls) == 1


def test_cli_import_report_no_overwrite_and_missing_db(tmp_path, capsys):
    file = tmp_path / "source.csv"; file.write_bytes(fixture())
    root = tmp_path / "capture"
    assert main(["eia-detail-import", "--file", str(file), "--out", str(root)]) == 0
    out = tmp_path / "report.json"
    args = ["eia-detail-report", "--journal", str(root / "macro.sqlite3"), "--at", AT, "--out", str(out)]
    assert main(args) == 2  # imported evidence, not prospective data
    old = out.read_bytes()
    assert main(args) == 2 and out.read_bytes() == old
    assert main(["eia-detail-report", "--journal", str(tmp_path / "missing.sqlite3"), "--at", AT]) == 2
    assert not (tmp_path / "missing.sqlite3").exists()
