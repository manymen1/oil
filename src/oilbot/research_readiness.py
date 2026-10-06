"""Read-only readiness and verified SQLite recovery exercises. No activation."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

from .clock import utc_now
from .databento import file_hash
from .market import atomic_json
from .schema import canonical, digest


def journal_identity(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError("journal integrity check failed")
        hasher = hashlib.sha256()
        count = 0
        for row in db.execute("SELECT seq,id,kind,available_at,payload,recorded_at FROM records ORDER BY seq"):
            hasher.update((canonical(list(row)) + "\n").encode())
            count += 1
        tables = {}
        for name in ("cursors", "budget", "parse_work"):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone():
                # These are fixed trusted table names, never a user SQL identifier.
                tables[name] = digest([list(r) for r in db.execute("SELECT * FROM " + name + " ORDER BY 1")])
        return {"integrity": integrity, "records": count, "records_sha256": hasher.hexdigest(),
            "state_table_hashes": tables, "schema_version": db.execute("PRAGMA user_version").fetchone()[0]}


def backup_research(journals, destination, *, restore_test=True):
    """Consistent per-database backups; never claims cross-database atomicity."""
    target = Path(destination).resolve()
    if not journals or target.exists():
        raise ValueError("journals and a new backup destination required")
    import re
    for name, path in journals.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,50}", name):
            raise ValueError("simple unique journal names required")
        source = Path(path).resolve()
        if not source.is_file() or target == source.parent or source.parent in target.parents:
            raise ValueError("backup output must be outside existing journal roots")
    target.mkdir(parents=True)
    files = {}
    for name, path in journals.items():
        copied = target / (name + ".sqlite3")
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(copied)) as output:
                source.backup(output)
        files[copied.name] = {"sha256": file_hash(copied), "identity": journal_identity(copied)}
    manifest = {"schema": "research-backup-v1", "created_at": utc_now(), "files": files,
        "consistency": "individual_sqlite_snapshots", "trade_authorized": False,
        "limitations": ["This recovery exercise is local; independent off-host retention is still required.",
            "Concurrent source journals are not one atomic cross-journal research snapshot.",
            "Private environment files and broker credentials are not included."]}
    atomic_json(target / "manifest.json", manifest)
    if restore_test:
        result = restore_research(target / "manifest.json", target / "recovery-exercise")
        atomic_json(target / "recovery-check.json", result)
        manifest["recovery_exercise"] = result
    return manifest


def restore_research(manifest_path, destination):
    source = Path(manifest_path).resolve()
    manifest = json.loads(source.read_text())
    if manifest.get("schema") != "research-backup-v1" or manifest.get("trade_authorized") is not False:
        raise ValueError("read-only research backup required")
    target = Path(destination).resolve()
    if target.exists():
        raise ValueError("restore destination already exists")
    for name, expected in manifest["files"].items():
        path = (source.parent / name).resolve()
        if path.parent != source.parent or not name.endswith(".sqlite3") or file_hash(path) != expected["sha256"] or journal_identity(path) != expected["identity"]:
            raise ValueError("backup checksum or journal identity mismatch")
    target.mkdir(parents=True)
    verified = {}
    for name, expected in manifest["files"].items():
        copied = target / name
        shutil.copyfile(source.parent / name, copied)
        verified[name] = file_hash(copied) == expected["sha256"] and journal_identity(copied) == expected["identity"]
    if not all(verified.values()):
        raise ValueError("restored journal verification failed")
    return {"schema": "research-recovery-check-v1", "checked_at": utc_now(), "result": "VERIFIED",
        "verified_journals": verified, "restore_root": str(target), "trade_authorized": False}


def research_readiness(*, macro_root, macro_state, schedule_path, curve_config_path, news_config=None):
    from .macro_scheduler import health, load_schedule
    from .market_curve import curve_preflight, load_curve_config
    macro = health(macro_root, macro_state, load_schedule(schedule_path))
    curve = curve_preflight(load_curve_config(curve_config_path))
    result = {"schema": "research-readiness-v1", "checked_at": utc_now(), "macro": macro, "market": curve,
        "trade_authorized": False, "broker_execution": "disabled", "economic_validation": "unavailable"}
    blockers = list(macro["issues"]) + list(curve["connection_blockers"])
    if news_config is not None:
        from .cli import status
        from .config import load_config
        result["news"] = status(load_config(news_config))
    result.update(state="BLOCKED" if blockers else "OBSERVATION_READY", blockers=sorted(set(blockers)))
    return result
