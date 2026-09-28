import importlib.util
import json
from pathlib import Path

import pytest

from oilbot.replay import load_manifest
from oilbot.store import Journal
from test_forward import capture, setup, warm

spec = importlib.util.spec_from_file_location("collector_audit", Path(__file__).resolve().parents[1] / "scripts/collector_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def prepare(cfg):
    Journal(cfg.db("analysis"))
    Journal(cfg.db("runtime"))


def test_pack_preserves_source_bytes_and_restores_causal_records(setup, tmp_path, monkeypatch):
    cfg, news, output, worker = setup
    prepare(cfg)
    warm(news, cfg.sources[0])
    capture(news, cfg.sources[0], "Tanker attacked near Hormuz")
    worker.run_once()
    before = {name: audit.checksum(cfg.db(name)) for name in ("news", "forward", "analysis", "runtime")}
    # Audit must never initialize/migrate a live Journal or call a collector.
    monkeypatch.setattr(Journal, "__init__", lambda *a, **kw: pytest.fail("journal initialization"))
    result = audit.build_pack(cfg, tmp_path / "pack")
    assert before == {name: audit.checksum(cfg.db(name)) for name in before}
    assert result["restore"]["verified"]
    assert result["recovery"]["unprocessed_story_revisions"] == 0
    assert result["recovery"]["observations_without_index"] == []
    sample = json.loads((tmp_path / "pack/coverage-sample.json").read_text())
    assert {r["historical_disposition"] for r in sample["sample"]} == {"MATCHED", "EXCLUDED"}
    assert sample["precision"] is None and sample["human_reviewed"] == 0
    for rel, expected in json.loads((tmp_path / "pack/checksums.json").read_text()).items():
        assert audit.checksum(tmp_path / "pack" / rel) == expected
    with pytest.raises(FileExistsError):
        audit.build_pack(cfg, tmp_path / "pack")


def test_missing_journal_not_represented_as_empty(setup, tmp_path):
    cfg, *_ = setup
    with pytest.raises(ValueError, match="four existing journals"):
        audit.build_pack(cfg, tmp_path / "pack")
    assert not cfg.db("analysis").exists()
    assert not (tmp_path / "pack").exists()


def test_unprocessed_story_and_offline_probe_do_not_become_events(setup, tmp_path):
    cfg, news, output, worker = setup
    prepare(cfg)
    warm(news, cfg.sources[0])
    capture(news, cfg.sources[0], "Tanker attacked near Hormuz")
    result = audit.build_pack(cfg, tmp_path / "pack", sample_size=10)
    assert result["recovery"]["unprocessed_story_revisions"] == 2
    sample = json.loads((tmp_path / "pack/coverage-sample.json").read_text())
    assert any(r["offline_headline_probe_types"] == ["TANKER_ATTACK"] for r in sample["sample"])
    assert all(not r["historical_event_types"] for r in sample["sample"])
    assert not output.records("fast_event")


def test_bad_dependencies_prevent_successful_export(setup, tmp_path):
    cfg, news, output, worker = setup
    prepare(cfg)
    output.append("probe", {"input_revision_ids": ["missing-input"]})
    with pytest.raises(ValueError, match="missing replay input"):
        audit.build_pack(cfg, tmp_path / "pack")
    assert not (tmp_path / "pack/checksums.json").exists()


def test_snapshot_tampering_is_detected(setup, tmp_path):
    cfg, *_ = setup
    prepare(cfg)
    audit.build_pack(cfg, tmp_path / "pack")
    (tmp_path / "pack/restore-check/market.json").write_text("{}")
    with pytest.raises(ValueError, match="checksum mismatch"):
        load_manifest(tmp_path / "pack/restore-check/manifest.json")


def test_coverage_sampling_is_repeatable_and_has_denominators(setup):
    cfg, news, output, worker = setup
    warm(news, cfg.sources[0])
    for i in range(8):
        capture(news, cfg.sources[0], "Headline " + str(i), native=str(i))
    records = news.records("story_revision")
    first = audit.coverage(records, cfg.raw["assets"], 3)
    second = audit.coverage(list(reversed(records)), cfg.raw["assets"], 3)
    assert first == second
    assert first["strata"][0]["population_revisions"] == 9
    assert first["strata"][0]["sample_revisions"] == 3
