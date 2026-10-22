"""K0-017 DRAFT: private local fixtures; no product/production backup claim."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(tmp_path / "source.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
    db.execute("INSERT INTO tasks VALUES ('t1', 'NEEDS_RECONCILIATION')")
    db.commit()
    yield db
    db.close()


def _manager(connection: sqlite3.Connection, tmp_path: Path, *, keep_last: int = 5):
    from jarvis.backup import BackupManager

    snapshots = tmp_path / "source-snapshots"
    snapshots.mkdir(exist_ok=True)
    (snapshots / "nested").mkdir(exist_ok=True)
    (snapshots / "empty").mkdir(exist_ok=True)
    (snapshots / "nested" / "中文.bin").write_bytes(b"\x00snapshot\xff")
    (snapshots / ".hidden").write_bytes(b"hidden")
    return BackupManager(
        connection,
        snapshot_dir=snapshots,
        backup_root=tmp_path / "backups",
        keep_last=keep_last,
        draft_fixture=True,
    )


def test_b1_contains_consistent_wal_database_and_detached_snapshot_tree(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    manager = _manager(connection, tmp_path)
    b1 = manager.create_b1(quiesced=True)
    assert b1.parent == tmp_path / "backups" / "b1"
    assert manager.verify_b1(b1) is True
    with sqlite3.connect(b1 / "database.sqlite3") as copied:
        assert copied.execute("SELECT * FROM tasks").fetchall() == [("t1", "NEEDS_RECONCILIATION")]
        assert copied.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert (b1 / "snapshots/nested/中文.bin").read_bytes() == b"\x00snapshot\xff"
    assert (b1 / "snapshots/empty").is_dir()
    assert (b1 / "snapshots/.hidden").read_bytes() == b"hidden"
    assert not (b1 / "database.sqlite3-wal").exists()
    (tmp_path / "source-snapshots/.hidden").write_bytes(b"changed")
    assert (b1 / "snapshots/.hidden").read_bytes() == b"hidden"
    assert manager.verify_b1(b1) is True
