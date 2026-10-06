import json

import pytest

from oilbot.research_readiness import backup_research, journal_identity, restore_research
from oilbot.store import Journal


def test_backup_and_restore_preserve_immutable_records_and_cursors(tmp_path):
    source = Journal(tmp_path / "input" / "macro.sqlite3")
    source.append("synthetic", {"value": None})
    with source.transaction() as db:
        source.set_cursor(db, "worker", {"sequence": 1})
    output = tmp_path / "backup"
    result = backup_research({"macro": source.path}, output)
    assert result["recovery_exercise"]["result"] == "VERIFIED"
    assert journal_identity(source.path) == journal_identity(output / "recovery-exercise" / "macro.sqlite3")
    assert not result["trade_authorized"]
    source.append("synthetic", {"later": True})
    assert journal_identity(source.path)["records"] == 2
    assert journal_identity(output / "macro.sqlite3")["records"] == 1
    with pytest.raises(ValueError, match="new backup"):
        backup_research({"macro": source.path}, output)


def test_changed_backup_is_rejected_before_creating_restore(tmp_path):
    source = Journal(tmp_path / "input" / "source.sqlite3")
    source.append("test", {})
    output = tmp_path / "backup"
    backup_research({"source": source.path}, output, restore_test=False)
    (output / "source.sqlite3").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        restore_research(output / "manifest.json", tmp_path / "restore")
    assert not (tmp_path / "restore").exists()


def test_backup_protects_source_roots_and_missing_journals(tmp_path):
    source = Journal(tmp_path / "source.sqlite3")
    with pytest.raises(ValueError, match="outside"):
        backup_research({"source": source.path}, tmp_path / "inside")
    with pytest.raises(ValueError):
        backup_research({"source": tmp_path / "absent.sqlite3"}, tmp_path.parent / "does-not-create")
    assert not (tmp_path / "absent.sqlite3").exists()
