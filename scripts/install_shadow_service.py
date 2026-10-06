"""Prepare, optionally install/start, the frozen read-only EIA shadow units.

Existing units are never replaced. The market recorder has separate connection
authorization and is not installed or started by this script.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from oilbot.clock import utc_now
from oilbot.macro_shadow import load_shadow_protocol
from oilbot.market import atomic_json


def quoted(value):
    value = str(value)
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("single-line unit value required")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def prepare(protocol_path, macro_path, market_path, root, output):
    protocol_path, macro_path, root, output = (Path(p).resolve() for p in (protocol_path, macro_path, root, output))
    protocol = load_shadow_protocol(protocol_path)
    if not macro_path.is_file() or output.exists():
        raise ValueError("existing macro journal and new unit output directory required")
    protected = [protocol_path.parent, macro_path.parent]
    if market_path is not None:
        market_path = Path(market_path).resolve()
        protected.append(market_path.parent)
    if any(root == p or p in root.parents or output == p or p in output.parents for p in protected):
        raise ValueError("state and units must be outside input roots")
    if root == output or root in output.parents or output in root.parents:
        raise ValueError("separate state and deployment directories required")
    project = Path(__file__).resolve().parent.parent
    if any(character.isspace() for character in str(project)):
        raise ValueError("systemd deployment requires a project path without whitespace")
    working_directory = str(project).replace("%", "%%")
    python = Path(sys.executable).absolute()
    bundle = protocol_path.parent / protocol["code_directory"]
    common = [quoted(python), "-m", "oilbot"]
    worker = common + ["shadow-worker", "--protocol", quoted(protocol_path), "--macro-journal", quoted(macro_path), "--root", quoted(root)]
    if market_path is not None:
        worker += ["--market-journal", quoted(market_path)]
    health = common + ["shadow-health", "--protocol", quoted(protocol_path), "--journal", quoted(root / "shadow.sqlite3")]
    service = "\n".join(["[Unit]", "Description=Oilbot frozen EIA shadow research (no broker orders)", "",
        "[Service]", "Type=simple", "WorkingDirectory=" + working_directory, "Environment=" + quoted("PYTHONPATH=" + str(bundle)),
        "ExecStart=" + " ".join(worker), "Restart=on-failure", "RestartSec=30", "TimeoutStopSec=20", "UMask=0077", "NoNewPrivileges=true", "",
        "[Install]", "WantedBy=default.target", ""])
    check = "\n".join(["[Unit]", "Description=Oilbot read-only EIA shadow health", "", "[Service]", "Type=oneshot",
        "WorkingDirectory=" + working_directory, "Environment=" + quoted("PYTHONPATH=" + str(bundle)),
        "ExecStart=" + " ".join(health), "UMask=0077", "NoNewPrivileges=true", ""])
    timer = "\n".join(["[Unit]", "Description=Check EIA shadow journal every minute", "", "[Timer]",
        "OnBootSec=1min", "OnUnitActiveSec=1min", "AccuracySec=10s", "Unit=oilbot-eia-shadow-health.service", "",
        "[Install]", "WantedBy=timers.target", ""])
    output.mkdir(parents=True)
    names = {"oilbot-eia-shadow.service": service, "oilbot-eia-shadow-health.service": check, "oilbot-eia-shadow-health.timer": timer}
    for name, content in names.items():
        (output / name).write_text(content)
    from oilbot.databento import file_hash
    provenance = {"schema": "eia-shadow-deployment-v1", "prepared_at": utc_now(), "protocol_id": protocol["id"],
        "frozen_code": str(bundle), "state_root": str(root), "python": str(python),
        "unit_hashes": {name: file_hash(output / name) for name in names}, "trade_authorized": False,
        "broker_execution": "disabled", "market_recorder_started": False}
    atomic_json(output / "deployment.json", provenance)
    return provenance


def install(output, *, start=False):
    output = Path(output).resolve()
    provenance = json.loads((output / "deployment.json").read_text())
    from oilbot.databento import file_hash
    units = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
    for name, expected in provenance["unit_hashes"].items():
        if file_hash(output / name) != expected or (units / name).exists():
            raise ValueError("changed prepared unit or existing installed unit; nothing replaced")
    subprocess.run(["systemd-analyze", "--user", "verify", *[str(output / n) for n in provenance["unit_hashes"]]], check=True)
    units.mkdir(parents=True, exist_ok=True)
    Path(provenance["state_root"]).mkdir(parents=True, exist_ok=True)
    for name in provenance["unit_hashes"]:
        shutil.copyfile(output / name, units / name)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if start:
        subprocess.run(["systemctl", "--user", "enable", "--now", "oilbot-eia-shadow.service", "oilbot-eia-shadow-health.timer"], check=True)
    return {**provenance, "installed": True, "started": start}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--macro-journal", type=Path)
    parser.add_argument("--market-journal", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--start", action="store_true")
    args = parser.parse_args(argv)
    if args.start and not args.install:
        parser.error("--start requires --install")
    if args.install:
        if any((args.protocol, args.macro_journal, args.market_journal, args.root)):
            parser.error("install uses the already prepared --out directory")
        result = install(args.out, start=args.start)
    else:
        if not all((args.protocol, args.macro_journal, args.root)):
            parser.error("prepare requires --protocol, --macro-journal and --root")
        result = prepare(args.protocol, args.macro_journal, args.market_journal, args.root, args.out)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
