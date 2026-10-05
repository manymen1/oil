"""Freeze an hourly EIA table 9 collector and monitoring units; do not start them."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from oilbot.clock import utc_now
from oilbot.eia_detail import detail_policy
from oilbot.macro_scheduler import load_contact, load_schedule, readonly_rows
from oilbot.schema import digest
from install_forward_service import quoted


def install(config, root, contact_file, destination, unit_dir):
    root, contact, dest, units = [Path(p).resolve() for p in (root, contact_file, destination, unit_dir)]
    load_contact(contact)
    schedule = load_schedule(config)
    policies = [r for r in readonly_rows(root / "macro.sqlite3", summary=True) if r["kind"] == "macro_policy"]
    expected = detail_policy("http")
    if len(policies) != 1 or policies[0]["payload"] != expected or policies[0]["id"] != digest(expected):
        raise ValueError("existing isolated HTTP EIA detail journal required")
    if root == dest or root in dest.parents or dest in root.parents or dest == contact or dest in contact.parents:
        raise ValueError("capture, contact and deployment must remain separate")
    names = ["oilbot-eia-detail.service", "oilbot-eia-detail.timer",
             "oilbot-eia-detail-health.service", "oilbot-eia-detail-health.timer"]
    if dest.exists() or any((units / n).exists() or (units / n).is_symlink() for n in names):
        raise ValueError("deployment or unit exists; explicit migration required")
    python = REPO / ".venv/bin/python"
    if not python.is_file():
        raise ValueError("project Python environment missing")
    command = lambda args: " ".join(quoted(x) for x in args)
    verify = command([python, dest / "src/oilbot/health.py", "--run", dest, "--verify-only"])
    collect = command([python, "-m", "oilbot", "eia-detail-collect", "--out", root, "--contact-file", contact])
    health = command([python, "-m", "oilbot", "eia-detail-health", "--root", root, "--config", dest / "config.yaml"])
    env = f'Environment={quoted("PYTHONPATH=" + str(dest / "src"))}\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n'

    def service(description, execute):
        return f'''[Unit]
Description={description}
After=network.target
[Service]
Type=oneshot
{env}ExecStartPre={verify}
ExecStart={execute}
TimeoutStartSec=120
UMask=0077
NoNewPrivileges=true
'''

    def timer(description, unit, boot, interval):
        return f'''[Unit]
Description={description}
[Timer]
OnBootSec={boot}
OnUnitInactiveSec={interval}
AccuracySec=5
Unit={unit}
[Install]
WantedBy=timers.target
'''

    bodies = {
        names[0]: service("EIA table 9 research capture (no trading)", collect),
        names[1]: timer("Hourly EIA table 9 capture", names[0], 30, 3600),
        names[2]: service("Independent EIA detail freshness and release check", health),
        names[3]: timer("Check EIA detail health every five minutes", names[2], 90, 300),
    }
    dest.mkdir(parents=True)
    shutil.copytree(REPO / "src", dest / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (dest / "config.yaml").write_text(json.dumps(schedule, indent=2) + "\n")
    files = {str(p.relative_to(dest)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted((dest / "src").rglob("*.py"))}
    files["config.yaml"] = hashlib.sha256((dest / "config.yaml").read_bytes()).hexdigest()
    provenance = {"created_at": utc_now(), "source_checkout": str(REPO), "files": files,
        "root": str(root), "python": str(python), "poll_interval_seconds": 3600,
        "units": {str(units / n): hashlib.sha256(body.encode()).hexdigest() for n, body in bodies.items()},
        "limitations": "Shared venv; WSL must remain running. Hourly context, not release-time capture. Local logs only; no trading."}
    (dest / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    units.mkdir(parents=True, exist_ok=True)
    for name, body in bodies.items():
        with (units / name).open("x") as stream:
            stream.write(body)
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "root", "contact-file", "out"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--unit-dir", default=str(Path.home() / ".config/systemd/user"))
    args = parser.parse_args()
    result = install(args.config, args.root, args.contact_file, args.out, args.unit_dir)
    print(json.dumps({k: v for k, v in result.items() if k != "files"}, indent=2))
