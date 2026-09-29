"""Freeze and install a separate admission worker; never change capture deployment."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from oilbot.clock import utc_now
from oilbot.config import load_config
from install_forward_service import quoted


def install(config_path, destination, unit_dir):
    cfg = load_config(config_path)
    if cfg.raw.get("pipeline") != "forward" or not cfg.raw.get("operational_queue"):
        raise ValueError("operational forward capture required")
    if not all(cfg.db(n).is_file() for n in ("news", "forward")):
        raise ValueError("existing capture journals required")
    dest, units = Path(destination).resolve(), Path(unit_dir).resolve()
    unit = units / "oilbot-assessment-admission.service"
    if dest.exists() or dest.is_symlink() or unit.exists() or unit.is_symlink():
        raise ValueError("deployment or service exists; explicit migration required")
    if cfg.root == dest or cfg.root in dest.parents:
        raise ValueError("worker deployment must be outside capture root")
    python = REPO / ".venv/bin/python"
    if not python.is_file():
        raise ValueError("project Python environment missing")
    command = ' '.join(quoted(x) for x in [python, "-m", "oilbot.assessment_queue", "--config", dest / "config.yaml",
                                          "--out", dest / "state/admission.sqlite3"])
    verify = ' '.join(quoted(x) for x in [python, dest / "src/oilbot/health.py", "--run", dest, "--verify-only"])
    body = f'''[Unit]
Description=Oil operational assessment admission (no model or trading)
After=oilbot-operational.service
StartLimitIntervalSec=600
StartLimitBurst=5
[Service]
Type=simple
Environment={quoted("PYTHONPATH=" + str(dest / "src"))}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStartPre={verify}
ExecStart={command}
Restart=on-failure
RestartSec=30
TimeoutStopSec=45
UMask=0077
NoNewPrivileges=true
[Install]
WantedBy=default.target
'''
    dest.mkdir(parents=True)
    shutil.copytree(REPO / "src", dest / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    raw = {**cfg.raw, "storage": {**cfg.raw["storage"], "root": str(cfg.root)}}
    (dest / "config.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    files = {str(p.relative_to(dest)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((dest / "src").rglob("*.py"))}
    files["config.yaml"] = hashlib.sha256((dest / "config.yaml").read_bytes()).hexdigest()
    provenance = {"created_at": utc_now(), "source_config": str(cfg.path), "source_checkout": str(REPO),
        "capture_root": str(cfg.root), "files": files, "unit": str(unit),
        "unit_sha256": hashlib.sha256(body.encode()).hexdigest(), "python": str(python),
        "limitations": "Admission abstentions only; no semantic labels. Shared venv; requires running WSL."}
    (dest / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    units.mkdir(parents=True, exist_ok=True)
    with unit.open("x") as stream:
        stream.write(body)
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--unit-dir", default=str(Path.home() / ".config/systemd/user"))
    args = parser.parse_args()
    result = install(args.config, args.out, args.unit_dir)
    print(json.dumps({k: v for k,v in result.items() if k != "files"}, indent=2))
