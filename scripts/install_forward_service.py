"""Install (but do not start) persistent user units for an existing frozen run.

Refuses to replace any existing unit. Uses the original run and its cursors;
recovery is not a fresh experiment. A WSL user service cannot wake Windows.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from oilbot.config import load_config
from oilbot.health import verify


def quoted(value):
    value = str(value)
    if any(c in value for c in '\n\r\x00'):
        raise ValueError("invalid unit argument")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def install(run, deployment, unit_dir, *, name="oilbot-operational"):
    run, deployment, unit_dir = (Path(p).resolve() for p in (run, deployment, unit_dir))
    if not re.fullmatch(r"oilbot-[a-z0-9-]+", name):
        raise ValueError("invalid service name")
    if verify(run):
        raise ValueError("frozen run hash mismatch")
    cfg = load_config(run / "config.yaml")
    if cfg.raw.get("pipeline") != "forward" or cfg.raw["market"]["provider"] != "disabled":
        raise ValueError("news-only forward run required")
    names = [name + ".service", name + "-health.service", name + "-health.timer"]
    if deployment.exists() or any((unit_dir / n).exists() or (unit_dir / n).is_symlink() for n in names):
        raise ValueError("deployment or unit exists; explicit migration required")
    python = REPO / ".venv/bin/python"
    if not python.is_file():
        raise ValueError("project Python environment missing")
    deployment.mkdir(parents=True)
    shutil.copy2(REPO / "src/oilbot/health.py", deployment / "health.py")
    health = ' '.join(quoted(p) for p in [python, deployment / "health.py", "--run", run, "--unit", name + ".service"])
    collector = ' '.join(quoted(p) for p in [python, run / "code/scripts/supervisor.py", "--config", run / "config.yaml"])
    units = {
        names[0]: f'''[Unit]
Description=Frozen oil observation collector (no trading)
After=network.target
StartLimitIntervalSec=600
StartLimitBurst=5
[Service]
Type=simple
WorkingDirectory={str(run / "code").replace('%', '%%')}
Environment={quoted("PYTHONPATH=" + str(run / "code/src"))}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStartPre={health} --verify-only
ExecStart={collector}
Restart=on-failure
RestartSec=15
TimeoutStopSec=210
UMask=0077
NoNewPrivileges=true
[Install]
WantedBy=default.target
''',
        names[1]: f'''[Unit]
Description=Independent oil collector health check
[Service]
Type=oneshot
ExecStart={health} --journal {quoted(deployment / "health.sqlite3")}
UMask=0077
NoNewPrivileges=true
TimeoutStartSec=45
''',
        names[2]: f'''[Unit]
Description=Check oil collector every minute while the user manager runs
[Timer]
OnBootSec=30
OnUnitActiveSec=60
AccuracySec=5
Unit={names[1]}
[Install]
WantedBy=timers.target
'''}
    unit_dir.mkdir(parents=True, exist_ok=True)
    for filename, body in units.items():
        with (unit_dir / filename).open("x") as stream:
            stream.write(body)
    manifest = {"run": str(run), "units": {str(unit_dir / n): hashlib.sha256(b.encode()).hexdigest() for n,b in units.items()},
        "health_sha256": hashlib.sha256((deployment / "health.py").read_bytes()).hexdigest(),
        "python": str(python), "limitations": "Existing venv dependencies; WSL must be running. No off-host alerting or backup."}
    (deployment / "deployment.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--unit-dir", default=str(Path.home() / ".config/systemd/user"))
    args = parser.parse_args()
    print(json.dumps(install(args.run, args.deployment, args.unit_dir), indent=2))
