import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from oilbot.config import load_config


def test_frozen_run_has_distinct_capture_root_and_verified_source(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/prepare_forward_run.py"
    spec = importlib.util.spec_from_file_location("prepare_run_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run = tmp_path / "new-run"
    result = module.prepare("configs/operational-forward.yaml", run)
    cfg = load_config(result["config"])
    assert cfg.root == run / "capture"
    assert not cfg.root.exists()  # Preparing does not start or fetch anything.
    provenance = json.loads((run / "provenance.json").read_text())
    for name, expected in provenance["files"].items():
        assert hashlib.sha256((run / name).read_bytes()).hexdigest() == expected
    assert "code/src/oilbot/operational.py" in provenance["files"]
    assert provenance["environment"]["PYTHONPATH"] == str(run / "code/src")
    assert cfg.raw["operational_queue"] and cfg.raw["market"]["provider"] == "disabled"
    with pytest.raises(ValueError, match="already exists"):
        module.prepare("configs/operational-forward.yaml", run)
