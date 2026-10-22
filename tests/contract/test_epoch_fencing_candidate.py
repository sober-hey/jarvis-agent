"""DP-5b A1/A2 candidate SQL evidence, using temporary in-memory SQLite only.

These fields are temporary SQL carriers, not a new formal schema. SQLiteProbe
is a candidate fixture, not a production Repository/Worker. This does not prove
INV-009, complete product fencing, concurrency, or power-loss durability; v2
review's F-NEW-EPOCH-COVERAGE remains outside this local evidence.
"""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

import pytest

TARGET_UPDATE = """
UPDATE attempts SET result = :result, version = version + 1
WHERE id = :id AND version = :expected
  AND :my_epoch = (
      SELECT worker_epoch FROM system_control WHERE singleton_id = 1
  )
"""
A2_BARRIER = """
UPDATE system_control SET updated_at_ms = :now
WHERE singleton_id = 1 AND worker_epoch = :my_epoch
"""


@dataclass
class SQLiteProbe:
    """Explicit candidate SQL carrier; historical row epoch is not authority."""

    db: sqlite3.Connection

    def update(self, my_epoch: int, expected: int, result: str) -> int:
        return self.db.execute(
            TARGET_UPDATE,
            {"id": 1, "expected": expected, "my_epoch": my_epoch, "result": result},
        ).rowcount

    def attempt(self) -> tuple[int, int, str]:
        return cast(
            tuple[int, int, str],
            self.db.execute(
                "SELECT version, worker_epoch, result FROM attempts WHERE id = 1"
            ).fetchone(),
        )


def _probe_connection() -> Iterator[SQLiteProbe]:
    db = sqlite3.connect(":memory:")
    try:
        db.executescript("""
            CREATE TABLE system_control (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                worker_epoch INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE attempts (
                id INTEGER PRIMARY KEY, version INTEGER NOT NULL,
                worker_epoch INTEGER NOT NULL, result TEXT NOT NULL
            );
            CREATE TABLE events (id INTEGER PRIMARY KEY, evidence TEXT NOT NULL);
            INSERT INTO system_control VALUES (1, 7, 0);
            INSERT INTO attempts VALUES (1, 3, 7, 'unresolved');
        """)
        yield SQLiteProbe(db)
    finally:
        db.close()


@pytest.fixture
def probe() -> Iterator[SQLiteProbe]:
    yield from _probe_connection()


def test_initialization_failure_closes_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingConnection(sqlite3.Connection):
        def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
            raise sqlite3.OperationalError("injected initialization failure")

    db = sqlite3.connect(":memory:", factory=FailingConnection)

    def connect(database: str) -> sqlite3.Connection:
        assert database == ":memory:"
        return db

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected initialization failure"):
            next(_probe_connection())
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            db.execute("SELECT 1")
    finally:
        db.close()


def test_current_epoch_updates_result_and_increments_version(probe: SQLiteProbe) -> None:
    with probe.db:
        assert probe.update(7, 3, "observed") == 1
    assert probe.attempt() == (4, 7, "observed")


def test_old_worker_cannot_update_its_own_old_attempt(probe: SQLiteProbe) -> None:
    with probe.db:
        assert (
            probe.db.execute(
                "UPDATE system_control SET worker_epoch = worker_epoch + 1 WHERE singleton_id = 1"
            ).rowcount
            == 1
        )
        assert probe.db.execute("SELECT worker_epoch FROM system_control").fetchone() == (8,)
        # The old worker still matches its row's historical epoch, but has no authority.
        assert probe.attempt() == (3, 7, "unresolved")
        assert probe.update(7, 3, "old-worker-result") == 0
    assert probe.attempt() == (3, 7, "unresolved")


def test_current_worker_can_record_old_attempt_reconciliation(probe: SQLiteProbe) -> None:
    with probe.db:
        probe.db.execute("UPDATE system_control SET worker_epoch = 8 WHERE singleton_id = 1")
        assert probe.update(8, 3, "reconciliation-candidate") == 1
    assert probe.attempt() == (4, 7, "reconciliation-candidate")


def test_missing_singleton_fails_closed(probe: SQLiteProbe) -> None:
    with probe.db:
        probe.db.execute("DELETE FROM system_control")
        assert probe.update(7, 3, "must-not-land") == 0
    assert probe.attempt() == (3, 7, "unresolved")


def test_stale_version_fails_closed_even_for_current_epoch(probe: SQLiteProbe) -> None:
    with probe.db:
        assert probe.update(7, 2, "must-not-land") == 0
    assert probe.attempt() == (3, 7, "unresolved")


@pytest.mark.parametrize(
    ("my_epoch", "expected", "missing", "succeeds"),
    [(6, 3, False, False), (7, 3, True, False), (7, 2, False, False), (7, 3, False, True)],
)
def test_a2_and_target_cas_roll_back_staged_writes(
    probe: SQLiteProbe, my_epoch: int, expected: int, missing: bool, succeeds: bool
) -> None:
    """Stage writes before the barrier to make rollback observable, not vacuous.

    This adversarial fixture ordering is not a proposed production write protocol.
    A2 supplements A1 and does not replace the target's version CAS.
    """
    if missing:
        with probe.db:
            probe.db.execute("DELETE FROM system_control")

    def transaction() -> None:
        with probe.db:
            probe.db.execute("INSERT INTO events VALUES (1, 'candidate-evidence')")
            probe.db.execute("INSERT INTO attempts VALUES (2, 0, 7, 'staged-candidate')")
            if probe.db.execute(A2_BARRIER, {"now": 10, "my_epoch": my_epoch}).rowcount != 1:
                raise RuntimeError("VERSION_CONFLICT: A2 candidate barrier")
            if probe.update(my_epoch, expected, "reconciliation-candidate") != 1:
                raise RuntimeError("VERSION_CONFLICT: target candidate CAS")

    if succeeds:
        transaction()
    else:
        with pytest.raises(RuntimeError, match="VERSION_CONFLICT"):
            transaction()
    assert probe.db.execute("SELECT COUNT(*) FROM events").fetchone() == (int(succeeds),)
    assert probe.db.execute("SELECT COUNT(*) FROM attempts").fetchone() == (1 + int(succeeds),)
    assert probe.attempt() == (
        (4, 7, "reconciliation-candidate") if succeeds else (3, 7, "unresolved")
    )
    assert probe.db.execute("SELECT updated_at_ms FROM system_control").fetchone() == (
        None if missing else (10 if succeeds else 0,)
    )
