"""K0-002 candidate probe: controlled two-connection contention, then post-commit uniqueness.

Candidate evidence only: approves nothing (ADR-0001 PROPOSED, INV-010 NOT_IMPLEMENTED, no DOC-R or
TRACE change, no product code touched); raw platform probe like ``test_sqlite_busy_capabilities``.

The name says race; the run does not race and is no test of simultaneous insertion. One process, the
main thread, gives connection 1 the write lock and only then asks connection 2 for it, in both
directions (``winner_index``): the winner is decided by the order of the calls below, never by
SQLite, so no sleep, thread or child process is involved. Each connection applies and reads back its
own WAL, ``synchronous=FULL``, ``foreign_keys=ON`` and ``busy_timeout=0`` before any lock exists.
Schema, the parent Task and two Plans of that Task at different ``plan_version`` values are
committed first, so no foreign key is pending and a version collision is never confused with the
rule under test.

The loser's single ``BEGIN IMMEDIATE`` - not its INSERT, which never runs here - is refused with
exactly ``SQLITE_BUSY`` ("database is locked"): it starts no transaction, so nothing is left to roll
back. After the winner commits, the loser's first INSERT, in an explicit new transaction and at an
unused version, is refused with exactly ``SQLITE_CONSTRAINT_UNIQUE`` and the exact message
``UNIQUE constraint failed: task_plans.task_id``: that pins the partial index, not
``UNIQUE(task_id, plan_version)``. The loser rolls that back, then inserts the same id and version
with a non-ACTIVE placeholder state, which is retained, leaving the predicate
``WHERE state = 'ACTIVE'`` of Master Plan v1.1.1 section 10.1 - extracted verbatim below - as the
only explanation offered. Throwaway file in a ``TemporaryDirectory`` on the repository volume, with
``st_dev`` asserted, both connections closed by ``ExitStack`` and removal verified. Not claimed:
simultaneous insertion, cross-process contention, retries (FI-023), reader snapshots, WAL
checkpointing, power-loss durability, migrations or the product database.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from collections.abc import Generator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_PATH = (
    REPO_ROOT
    / "docs/planning"
    / "JARVIS 独立智能体系统工程开发总规划-JARVIS_Engineering_Master_Plan_v1.1.1.md"
)
SECTION_10_1 = "#### 10.1 K0 核心数据表"
SECTION_10_2 = "#### 10.2 数据库实现规则"
#: The three published statements this probe executes, by their prefixes in section 10.1.
WANTED_STATEMENTS: Final = (
    "CREATE TABLE tasks",
    "CREATE TABLE task_plans",
    "CREATE UNIQUE INDEX ux_active_plan_per_task",
)
BEGIN_IMMEDIATE: Final = "BEGIN IMMEDIATE"  # the statement the loser is refused on here
ACTIVE: Final = "ACTIVE"
#: Placeholder only: no plan-state enum is published, so this literal merely differs from ACTIVE.
NON_ACTIVE_PLAN_STATE: Final = "NOT_ACTIVE_PLAN_STATE_PLACEHOLDER"
DIGEST: Final = "a" * 64
UNIQUE_MESSAGE: Final = "UNIQUE constraint failed: task_plans.task_id"  # the partial index column
TASK_INSERT: Final = (
    "INSERT INTO tasks (id, request_key, request_digest, task_kind, state, created_at_ms,"
    " updated_at_ms) VALUES (?, ?, ?, 'inventory.generate', 'CREATED', 1, 1)"
)
PLAN_INSERT: Final = (
    "INSERT INTO task_plans (id, task_id, plan_version, state, plan_digest, created_at_ms)"
    " VALUES (?, ?, ?, ?, ?, 1)"
)


def _published_statements() -> tuple[str, ...]:
    """Section 10.1's two catalog tables plus its ACTIVE-plan index, extracted verbatim."""
    text = PLAN_PATH.read_text(encoding="utf-8")
    start = text.index(SECTION_10_1)
    section = text[start : text.index(SECTION_10_2, start)]
    blocks = re.findall(r"```sql\n(.*?)```", section, flags=re.DOTALL)
    split = [f"{raw.strip()};" for block in blocks for raw in block.split(";") if raw.strip()]
    needed = tuple(statement for statement in split if statement.startswith(WANTED_STATEMENTS))
    assert len(needed) == 3, f"section 10.1 no longer publishes the three statements: {needed!r}"
    return needed


@contextmanager
def probe_workspace() -> Generator[Path, None, None]:
    """Throwaway directory on the target volume; removal is verified on every exit path."""
    directory = tempfile.TemporaryDirectory(dir=REPO_ROOT, prefix=".k0-002-plan-race-probe-")
    root = Path(directory.name)
    try:
        assert os.stat(root).st_dev == os.stat(REPO_ROOT).st_dev, "probe dir left the target volume"
        yield root
    finally:  # a leak assertion here can mask the original error; a leak matters more
        directory.cleanup()
        assert not root.exists(), f"probe workspace leaked: {root}"


def _pragma(connection: sqlite3.Connection, name: str, value: str | int | None = None) -> object:
    """Apply a PRAGMA when a value is given, then read it back: nothing is assumed to apply."""
    if value is not None:
        connection.execute(f"PRAGMA {name} = {value}")
    row = connection.execute(f"PRAGMA {name}").fetchone()
    assert row is not None, f"PRAGMA {name} returned no row"
    return row[0]


def _open(db_path: Path, stack: ExitStack) -> sqlite3.Connection:
    """timeout=0 drops the driver's 5 s wait; isolation_level=None leaves BEGIN/COMMIT to us."""
    return stack.enter_context(closing(sqlite3.connect(db_path, timeout=0, isolation_level=None)))


def _count(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None, f"the {table} row count returned no row"
    return int(row[0])


#: Both directions: either connection may be the one the main thread gives the write lock to first.
@pytest.mark.parametrize("winner_index", (0, 1), ids=("first_wins", "second_wins"))
def test_candidate_contention_then_post_commit_uniqueness(winner_index: int) -> None:
    with probe_workspace() as root, ExitStack() as stack:
        db_path = root / "candidate_active_plan_race.db"
        connections = (_open(db_path, stack), _open(db_path, stack))
        assert _pragma(connections[0], "journal_mode", "wal") == "wal"  # before any lock exists
        for connection in connections:  # each connection applies and reads back its own values
            assert _pragma(connection, "journal_mode") == "wal"
            assert _pragma(connection, "synchronous", "FULL") == 2
            assert _pragma(connection, "foreign_keys", "ON") == 1
            assert _pragma(connection, "busy_timeout", 0) == 0
        winner, loser = connections[winner_index], connections[1 - winner_index]

        winner.execute(BEGIN_IMMEDIATE)  # schema, parent Task and two versions, one commit
        for statement in _published_statements():
            winner.execute(statement)
        winner.execute(TASK_INSERT, ("t1", "request-key-1", DIGEST))
        winner.execute(PLAN_INSERT, ("p-seed-1", "t1", 1, NON_ACTIVE_PLAN_STATE, DIGEST))
        winner.execute(PLAN_INSERT, ("p-seed-2", "t1", 2, NON_ACTIVE_PLAN_STATE, DIGEST))
        winner.execute("COMMIT")
        assert winner.in_transaction is False, "the setup transaction is still open"
        assert _count(loser, "tasks") == 1, "the committed parent Task is not visible"
        assert _count(loser, "task_plans") == 2, "two versions of one Task are not both retained"

        winner.execute(BEGIN_IMMEDIATE)  # the one write lock, taken before the loser asks for it
        winner.execute(PLAN_INSERT, ("p-winner", "t1", 3, ACTIVE, DIGEST))
        assert winner.in_transaction is True, "the winner did not start a transaction"
        assert _count(loser, "task_plans") == 2, "the winner's uncommitted Plan is visible"

        with pytest.raises(sqlite3.OperationalError) as refused:  # one attempt: no retry here
            loser.execute(BEGIN_IMMEDIATE)  # the refusal is on BEGIN, never on the INSERT
        assert refused.value.sqlite_errorname == "SQLITE_BUSY", refused.value
        assert refused.value.sqlite_errorcode == sqlite3.SQLITE_BUSY, refused.value
        assert "database is locked" in str(refused.value), refused.value
        assert refused.value.sqlite_errorname != "SQLITE_CONSTRAINT_UNIQUE", refused.value
        assert loser.in_transaction is False, "a refused BEGIN left a transaction open"
        assert _count(loser, "task_plans") == 2, "the refusal wrote a row or was retried"
        winner.execute("COMMIT")  # the lock is released here; nothing sleeps to wait for it
        assert winner.in_transaction is False, "COMMIT left the winner's transaction open"

        loser.execute(BEGIN_IMMEDIATE)  # only now: the loser's own first INSERT, post-commit
        with pytest.raises(sqlite3.IntegrityError) as duplicate:
            loser.execute(PLAN_INSERT, ("p-loser", "t1", 4, ACTIVE, DIGEST))
        assert duplicate.value.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE", duplicate.value
        assert duplicate.value.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE, duplicate.value
        assert str(duplicate.value) == UNIQUE_MESSAGE, duplicate.value  # the partial index column
        assert loser.in_transaction is True, "the rejected statement closed the transaction"
        assert _count(loser, "task_plans") == 3, "the rejected ACTIVE row landed anyway"
        loser.execute("ROLLBACK")  # explicit here: this transaction did exist
        assert loser.in_transaction is False, "ROLLBACK left the loser's transaction open"

        loser.execute(BEGIN_IMMEDIATE)  # same id, same version 4, only the state differs now
        loser.execute(PLAN_INSERT, ("p-loser", "t1", 4, NON_ACTIVE_PLAN_STATE, DIGEST))
        loser.execute("COMMIT")
        rows = winner.execute("SELECT id, state FROM task_plans ORDER BY plan_version").fetchall()
    assert rows == [
        ("p-seed-1", NON_ACTIVE_PLAN_STATE),
        ("p-seed-2", NON_ACTIVE_PLAN_STATE),
        ("p-winner", ACTIVE),
        ("p-loser", NON_ACTIVE_PLAN_STATE),
    ]
    for connection in connections:  # the ExitStack closed both before the probe dir went away
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
