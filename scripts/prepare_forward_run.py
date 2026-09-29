"""Freeze a forward deployment into a new directory, without starting services.

Copy the actual working source, preserve its Git provenance and file hashes,
and use a fresh data root. Existing source circuits must be reviewed separately
before launching; this utility never resets or copies old capture journals.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from oilbot.clock import utc_now
from oilbot.config import load_config
from oilbot.schema import digest


def prepare(config_path, destination):
    config = load_config(config_path)
    if config.raw.get("pipeline") != "forward" or config.raw["market"]["provider"] != "disabled":
        raise ValueError("news-only forward configuration required")
    dest = Path(destination).resolve()
    if dest.exists():
        raise ValueError("run directory already exists")
    dest.mkdir(parents=True)
    code = dest / "code"
    code.mkdir()
    for name in ("src", "scripts"):
        shutil.copytree(REPO / name, code / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(REPO / "pyproject.toml", code / "pyproject.toml")
    raw = {**config.raw, "storage": {**config.raw["storage"], "root": str(dest / "capture")}}
    frozen_config = dest / "config.yaml"
    frozen_config.write_text(yaml.safe_dump(raw, sort_keys=False))
    load_config(frozen_config)
    files = {str(p.relative_to(dest)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(code.rglob("*")) if p.is_file()}
    files["config.yaml"] = hashlib.sha256(frozen_config.read_bytes()).hexdigest()
    provenance = {"created_at": utc_now(), "config_hash": digest(raw), "source_config": str(config.path),
                  "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
                  "git_status": subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO, text=True),
                  "files": files, "source_checkout": str(REPO),
                  "restart_boundary": "Fresh capture epoch; prior gaps remain in earlier runs.",
                  "command": [str(REPO / ".venv/bin/python"), str(code / "scripts/supervisor.py"), "--config", str(frozen_config)],
                  "environment": {"PYTHONPATH": str(code / "src"), "PYTHONDONTWRITEBYTECODE": "1"}}
    with (dest / "provenance.json").open("x") as stream:
        json.dump(provenance, stream, indent=2)
        stream.write("\n")
    return {"run": str(dest), "config": str(frozen_config), "files_frozen": len(files), "command": provenance["command"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.config, args.out), indent=2))
