"""Freeze a macro worker and install separate user units; never start them here."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from oilbot.clock import utc_now
from oilbot.macro_scheduler import load_contact, load_schedule, readonly_rows
from install_forward_service import quoted


def install(config, root, state, contact_file, destination, unit_dir):
    root, state, contact_file, dest, units = [Path(p).resolve() for p in (root, state, contact_file, destination, unit_dir)]
    load_contact(contact_file)
    policy = load_schedule(config)
    rows = readonly_rows(root / "macro.sqlite3", summary=True)
    if not any(r["kind"] == "macro_policy" and r["payload"]["delivery"] == "http" for r in rows):
        raise ValueError("existing HTTP macro capture required")
    if any(a == b or a in b.parents or b in a.parents for a,b in ((root, state), (root, dest), (state, dest))):
        raise ValueError("capture, state and deployment roots must be separate")
    names = ["oilbot-macro.service", "oilbot-macro-health.service", "oilbot-macro-health.timer"]
    if dest.exists() or any((units / n).exists() or (units / n).is_symlink() for n in names):
        raise ValueError("deployment or unit exists; explicit migration required")
    python = REPO / ".venv/bin/python"
    if not python.is_file():
        raise ValueError("project Python environment missing")
    command = lambda args: " ".join(quoted(x) for x in args)
    common = ["--config", dest / "config.yaml", "--root", root, "--state", state, "--contact-file", contact_file]
    worker = command([python, "-m", "oilbot", "macro-worker", *common, "--serve"])
    health = command([python, "-m", "oilbot", "macro-health", *common])
    verify = command([python, dest / "src/oilbot/health.py", "--run", dest, "--verify-only"])
    environment = f'Environment={quoted("PYTHONPATH=" + str(dest / "src"))}\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n'
    bodies = {
        names[0]: f'''[Unit]
Description=Oil macro observations (EIA/CFTC only, no trading)
After=network.target
StartLimitIntervalSec=600
StartLimitBurst=5
[Service]
Type=simple
{environment}ExecStartPre={verify}
ExecStart={worker}
Restart=on-failure
RestartSec=30
TimeoutStopSec=120
UMask=0077
NoNewPrivileges=true
[Install]
WantedBy=default.target
''',
        names[1]: f'''[Unit]
Description=Independent read-only oil macro health check
[Service]
Type=oneshot
{environment}ExecStartPre={verify}
ExecStart={health}
TimeoutStartSec=45
UMask=0077
NoNewPrivileges=true
''',
        names[2]: f'''[Unit]
Description=Check oil macro collection health each minute
[Timer]
OnBootSec=30
OnUnitActiveSec=60
AccuracySec=5
Unit={names[1]}
[Install]
WantedBy=timers.target
'''}
    dest.mkdir(parents=True)
    shutil.copytree(REPO / "src", dest / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (dest / "config.yaml").write_text(json.dumps(policy, indent=2) + "\n")
    files = {str(p.relative_to(dest)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted((dest / "src").rglob("*.py"))}
    files["config.yaml"] = hashlib.sha256((dest / "config.yaml").read_bytes()).hexdigest()
    provenance = {"created_at": utc_now(), "source_checkout": str(REPO), "files": files,
        "root": str(root), "state": str(state), "python": str(python),
        "units": {str(units / n): hashlib.sha256(body.encode()).hexdigest() for n,body in bodies.items()},
        "limitations": "Shared venv; WSL must remain running. Local logs only; no email delivery or broker execution."}
    (dest / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    units.mkdir(parents=True, exist_ok=True)
    for name, body in bodies.items():
        with (units / name).open("x") as stream:
            stream.write(body)
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "root", "state", "contact-file", "out"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--unit-dir", default=str(Path.home() / ".config/systemd/user"))
    args = parser.parse_args()
    result = install(args.config, args.root, args.state, args.contact_file, args.out, args.unit_dir)
    print(json.dumps({k: v for k,v in result.items() if k != "files"}, indent=2))
