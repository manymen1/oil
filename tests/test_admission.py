from copy import deepcopy
from dataclasses import replace
import sqlite3

import pytest

from oilbot.assessment_queue import AdmissionWorker, report
from oilbot.config import load_config
from oilbot.operational import OperationalQueue
from oilbot.store import Journal
from oilbot.forward import ForwardRecorder
from test_forward import warm, capture


@pytest.fixture
def case(tmp_path):
    cfg = replace(load_config("configs/operational-forward.yaml"), root=tmp_path / "capture")
    news, forward = Journal(cfg.db("news")), Journal(cfg.db("forward"))
    ForwardRecorder(news, forward, cfg.raw["assets"])
    queue = OperationalQueue(news, forward, cfg.raw["assets"], cfg.sources)
    return cfg, news, forward, queue, tmp_path / "sidecar/admission.sqlite3"


def test_rights_pending_abstains_without_model_or_economic_label(case):
    cfg, news, forward, queue, path = case
    warm(news, cfg.sources[0])
    capture(news, cfg.sources[0], "Crude exports suspended")
    queue.run_once()
    before = {p: p.read_bytes() for p in cfg.root.glob("*.sqlite3")}
    worker = AdmissionWorker(cfg, path)
    assert worker.run_once()["states"] == {"ABSTAINED": 1, "FAILED": 0}
    p = report(path)["resolutions"][0]["payload"]
    assert "SOURCE_MODEL_RIGHTS_NOT_PERMITTED" in p["reason_codes"]
    assert "AUTOMATED_ECONOMIC_POLICY_UNQUALIFIED" in p["reason_codes"]
    assert not any(p[k] for k in ("model_invoked", "research_eligible", "trade_authorized", "economic_assessment_created"))
    assert before == {p: p.read_bytes() for p in before}
    assert AdmissionWorker(cfg, path).run_once()["processed"] == 0
    assert report(path)["resolved_candidates"] == 1


def test_baseline_and_correction_keep_immutable_resolution_chain(case):
    cfg, news, forward, queue, path = case
    capture(news, cfg.sources[0], "Oil exports suspended", native="a")
    queue.run_once()
    worker = AdmissionWorker(cfg, path)
    worker.run_once()
    capture(news, cfg.sources[0], "Statement withdrawn", native="a", status="withdrawal")
    queue.run_once()
    worker.run_once()
    rows = report(path)["resolutions"]
    assert "CAPTURE_EXCLUDED" in rows[0]["payload"]["reason_codes"]
    assert "LIFECYCLE_REQUIRES_REVIEW" in rows[1]["payload"]["reason_codes"]
    assert rows[1]["payload"]["supersedes_resolution_id"] == rows[0]["id"]
    assert report(path)["current_stories"] == 1
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM records")


def test_transaction_failure_keeps_cursor_and_retries_once(case, monkeypatch):
    cfg, news, forward, queue, path = case
    capture(news, cfg.sources[0], "Crude loading stopped")
    queue.run_once()
    worker = AdmissionWorker(cfg, path)
    original = worker.output.set_cursor
    monkeypatch.setattr(worker.output, "set_cursor", lambda *a: (_ for _ in ()).throw(RuntimeError("disk")))
    with pytest.raises(RuntimeError):
        worker.run_once()
    assert report(path)["resolved_candidates"] == 0
    assert worker.output.cursor("admission:progress") is None
    monkeypatch.setattr(worker.output, "set_cursor", original)
    assert worker.run_once()["processed"] == 1
    assert worker.run_once()["processed"] == 0


def test_future_candidate_waits_and_invalid_binding_fails_durably(case, monkeypatch):
    cfg, news, forward, queue, path = case
    forward.append("operational_review_candidate", {"story_revision_id": "missing", "story_id": "future"}, available_at="2099-01-01T00:00:00Z")
    worker = AdmissionWorker(cfg, path)
    assert worker.run_once()["processed"] == 0
    monkeypatch.setattr("oilbot.assessment_queue.utc_now", lambda: "2099-01-01T00:01:00Z")
    assert worker.run_once()["states"]["FAILED"] == 1
    assert report(path)["resolutions"][0]["payload"]["reason_codes"] == ["CAPTURED_STORY_MISSING"]
    monkeypatch.setattr("oilbot.assessment_queue.utc_now", lambda: "2099-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="clock regressed"):
        worker.run_once()


def test_changed_rights_require_new_policy_even_if_permitted(case):
    cfg, news, forward, queue, path = case
    AdmissionWorker(cfg, path)
    raw = deepcopy(cfg.raw)
    raw["sources"][0]["rights"]["model_processing"] = "permitted"
    with pytest.raises(ValueError, match="different admission policy"):
        AdmissionWorker(replace(cfg, raw=raw), path)
    worker = AdmissionWorker(replace(cfg, raw=raw), path.parent / "new.sqlite3")
    warm(news, cfg.sources[0])
    capture(news, cfg.sources[0], "Oil exports suspended")
    queue.run_once()
    worker.run_once()
    assert report(worker.destination)["economic_assessments"] == 0


def test_path_and_batch_guards(case, tmp_path):
    cfg, news, forward, queue, path = case
    with pytest.raises(ValueError, match="outside"):
        AdmissionWorker(cfg, cfg.root / "bad.sqlite3")
    alias = tmp_path / "alias.sqlite3"
    alias.hardlink_to(cfg.db("news"))
    with pytest.raises(ValueError, match="alias"):
        AdmissionWorker(cfg, alias)
    worker = AdmissionWorker(cfg, path)
    for n in (0, -1, 1001, True):
        with pytest.raises(ValueError, match="bounded"):
            worker.run_once(n)


def test_bounded_pagination_and_cursor_rewind_detection(case):
    cfg, news, forward, queue, path = case
    for i in range(3):
        capture(news, cfg.sources[0], "Oil exports suspended", native=str(i))
    queue.run_once()
    worker = AdmissionWorker(cfg, path)
    assert worker.run_once(2)["processed"] == 2
    assert AdmissionWorker(cfg, path).run_once(2)["processed"] == 1
    with worker.output.transaction() as db:
        worker.output.set_cursor(db, "admission:progress", {"seq": 999999, "candidate_hash": "missing"})
    with pytest.raises(ValueError, match="history changed"):
        worker.run_once()


def test_installer_freezes_code_and_preserves_capture_root(case, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    import yaml
    from oilbot.health import verify
    cfg, news, forward, queue, path = case
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("install_admission_test", scripts / "install_assessment_service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "config.yaml"
    source.write_text(yaml.safe_dump({**cfg.raw, "storage": {**cfg.raw["storage"], "root": str(cfg.root)}}))
    result = module.install(source, tmp_path / "deployment", tmp_path / "units")
    assert result["capture_root"] == str(cfg.root)
    assert verify(tmp_path / "deployment") == []
    assert load_config(tmp_path / "deployment/config.yaml").root == cfg.root
    body = Path(result["unit"]).read_text()
    assert "--verify-only" in body and "oilbot.assessment_queue" in body
    assert not (tmp_path / "deployment/state").exists()
    with pytest.raises(ValueError, match="exists"):
        module.install(source, tmp_path / "other", tmp_path / "units")


def test_snapshot_export_keeps_admissions_separate_from_economic_labels(case, tmp_path):
    import json
    from oilbot.assessment_queue import snapshot_resolutions
    from oilbot.economic_dataset import build_economic_dataset
    from oilbot.replay import export_manifest
    from oilbot.story_review import Archive
    from oilbot.clock import utc_now
    cfg, news, forward, queue, path = case
    capture(news, cfg.sources[0], "Crude loading halted")
    queue.run_once()
    worker = AdmissionWorker(cfg, path)
    worker.run_once()
    archive = Archive(export_manifest(cfg, tmp_path / "snapshot"))
    at = utc_now()
    rows = snapshot_resolutions(archive, path, through=at)
    assert len(rows) == 1
    assert snapshot_resolutions(archive, path, through="2000-01-01T00:00:00Z") == []
    meta = build_economic_dataset(archive, tmp_path / "no-assessments.db", tmp_path / "dataset", admissions=path)
    assert meta["admission_resolutions"] == 1 and meta["rows"] == 0 and meta["pending_subjects"] == 1
    assert json.loads((tmp_path / "dataset/admissions.jsonl").read_text())["payload"]["state"] == "ABSTAINED"
    candidate = rows[0]["payload"]["candidate_id"]
    archive.records[candidate]["payload"]["story_hash"] = "altered"
    with pytest.raises(ValueError, match="candidate missing or changed"):
        snapshot_resolutions(archive, path, through=at)
