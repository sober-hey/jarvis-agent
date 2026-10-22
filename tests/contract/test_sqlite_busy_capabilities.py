"""K0-002 candidate probe: ``busy_timeout`` wait/refusal observations on ``BEGIN IMMEDIATE``.

Candidate evidence only: approves nothing (ADR-0001 PROPOSED, INV-010 NOT_IMPLEMENTED, no DOC-R or
TRACE change, no product code touched); raw platform probe like ``test_lock_wal_capabilities``.

Two independent connections on one throwaway database. ``sqlite3.connect(..., timeout=0)`` removes
the driver's own 5 s default wait, so a wait recorded below is attributed to the ``PRAGMA
busy_timeout`` (MP L814; ``contracts/database_constraints.yaml`` L9) that this probe sets and reads
back per connection - never to a default it did not observe. Each elapsed value is the whole wall
time of one attempt, scheduling and SQLite's own waiting included: candidate observations of this
run, not a calibrated clock. Both connections apply and read back all four declared settings (WAL,
``synchronous=FULL``, ``foreign_keys=ON``, ``busy_timeout``) before the holder locks anything; they
then report the same ``main`` file through ``PRAGMA database_list``; and with the lock still free
the contender passes a positive control, its own ``BEGIN IMMEDIATE``/``ROLLBACK``.

Then the holder takes ``BEGIN IMMEDIATE`` and inserts an uncommitted row, and the contender's one
``BEGIN IMMEDIATE`` is refused with exactly ``SQLITE_BUSY`` (code and name), attributed to that
statement: after >= 4.5 s once the contract value 5000 is in force, while for 0 the refusal was
observed to come back without such a wait on this run. The 5000 case carries no upper bound; only
the lower one is asserted, to reject a refusal that came back without waiting. That same threshold
can fail a 0 run under scheduling delay, and in neither direction does it prove a hard deadline.
Nothing retries the refusal here: ``in_transaction`` stays ``False``, the row count stays 0 and the
pragma reads back unchanged. After the holder's ``ROLLBACK`` the contender makes an explicit new
attempt that succeeds and commits, and the holder reads it back: a legal post-release observation,
not a claim that retrying a busy writer is generally safe.

The database is a throwaway file in a ``TemporaryDirectory`` inside the repository volume
(``st_dev`` asserted, removal verified after ``ExitStack`` closes both connections): never the
product runtime database, which does not exist yet. Nothing here claims behaviour under a hard kill
of a process holding the lock, nor across processes at all.

Not claimed: cross-process contention, WAL writer/reader fairness, FI-023 retry policy, the product
connection wrapper, that a timed-out ``SQLITE_BUSY`` means the other lock was released, or that
``synchronous=FULL`` implies power-loss durability.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from collections.abc import Generator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_BUSY_TIMEOUT_MS = 5000  # MP L814 and contracts/database_constraints.yaml L9
BUSY_TIMEOUT_CASES_MS = (0, CONTRACT_BUSY_TIMEOUT_MS)
BEGIN_IMMEDIATE = "BEGIN IMMEDIATE"  # the one statement this probe attributes SQLITE_BUSY to
ROWS_SQL = "SELECT count(*) FROM probe_rows"
# One loose threshold for both cases: a 5000 run must exceed it, a 0 run must stay under it. It only
# tells "waited" from "came back without waiting" - a discriminator, not a deadline: delay can still
# move either run across it, so neither direction is guaranteed.
BUSY_WAIT_FLOOR_S = 4.5


@contextmanager
def probe_workspace() -> Generator[Path, None, None]:
    """Throwaway directory on the target volume; removal is verified on every exit path."""
    directory = tempfile.TemporaryDirectory(dir=REPO_ROOT, prefix=".k0-002-busy-probe-")
    root = Path(directory.name)
    try:
        assert os.stat(root).st_dev == os.stat(REPO_ROOT).st_dev, "probe dir left the target volume"
        yield root
    finally:  # a leak assertion here can mask the original error; a leak matters more
        directory.cleanup()
        assert not root.exists(), f"probe workspace leaked: {root}"


def _read_pragma(connection: sqlite3.Connection, name: str) -> object:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    assert row is not None, f"PRAGMA {name} returned no row"
    return row[0]


def _apply_pragma(connection: sqlite3.Connection, name: str, value: str | int) -> object:
    """Apply one PRAGMA and read it back: no value here is assumed to have taken effect."""
    connection.execute(f"PRAGMA {name} = {value}")
    return _read_pragma(connection, name)


def _open(db_path: Path, stack: ExitStack) -> sqlite3.Connection:
    """Open one connection and register it for closing as soon as it exists.

    ``timeout=0`` seeds only the busy handler set up at ``connect`` time. The 5000 ms under test is
    installed afterwards by this probe's own ``PRAGMA busy_timeout`` and read back, so the driver's
    5 s connect default can neither stand in for it nor mask it.
    """
    return stack.enter_context(closing(sqlite3.connect(db_path, timeout=0, isolation_level=None)))


def _count(connection: sqlite3.Connection) -> int:
    row = connection.execute(ROWS_SQL).fetchone()
    assert row is not None, "the row count returned no row"
    return int(row[0])


def _main_file(connection: sqlite3.Connection) -> Path:
    """Resolved path of the database this connection really opened, per ``database_list``."""
    rows = connection.execute("PRAGMA database_list").fetchall()
    main = [row for row in rows if row[1] == "main"]
    assert len(main) == 1, f"expected exactly one main database, got {rows!r}"
    return Path(str(main[0][2])).resolve()


@pytest.mark.parametrize("busy_timeout_ms", BUSY_TIMEOUT_CASES_MS)
def test_candidate_busy_timeout_bounds_begin_immediate(busy_timeout_ms: int) -> None:
    opened: list[sqlite3.Connection] = []
    with probe_workspace() as root, ExitStack() as stack:
        db_path = root / "candidate_busy.db"
        holder, contender = _open(db_path, stack), _open(db_path, stack)
        opened.extend((holder, contender))
        assert _apply_pragma(holder, "journal_mode", "wal") == "wal"  # before any lock is taken
        holder.execute("CREATE TABLE probe_rows (id INTEGER PRIMARY KEY, note TEXT NOT NULL)")
        for connection in (holder, contender):  # each connection proves its own values
            assert _read_pragma(connection, "journal_mode") == "wal"
            assert _apply_pragma(connection, "synchronous", "FULL") == 2
            assert _apply_pragma(connection, "foreign_keys", "ON") == 1
            assert _apply_pragma(connection, "busy_timeout", busy_timeout_ms) == busy_timeout_ms
        main_files = {_main_file(holder), _main_file(contender)}
        assert main_files == {db_path.resolve()}, f"not one database file: {main_files}"
        contender.execute(BEGIN_IMMEDIATE)  # positive control: the lock is free before the holder
        contender.execute("ROLLBACK")
        assert contender.in_transaction is False, "the control left a transaction open"
        holder.execute(BEGIN_IMMEDIATE)  # the one lock the two connections now contend for
        holder.execute("INSERT INTO probe_rows (id, note) VALUES (1, 'uncommitted')")
        assert holder.in_transaction is True, "the holder did not start a transaction"
        assert _count(contender) == 0, "the holder's uncommitted row is visible"
        assert _read_pragma(contender, "busy_timeout") == busy_timeout_ms, "pragma not in force"
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError) as refused:
            contender.execute(BEGIN_IMMEDIATE)  # one attempt: nothing here retries it
        elapsed = time.monotonic() - started
        assert refused.value.sqlite_errorcode == sqlite3.SQLITE_BUSY, refused.value
        assert refused.value.sqlite_errorname == "SQLITE_BUSY", refused.value
        assert "database is locked" in str(refused.value), refused.value
        assert contender.in_transaction is False, "a refused BEGIN left a transaction open"
        assert _read_pragma(contender, "busy_timeout") == busy_timeout_ms, "timeout moved mid-run"
        assert _count(contender) == 0, "the refused BEGIN wrote a row or was silently retried"
        if busy_timeout_ms == CONTRACT_BUSY_TIMEOUT_MS:
            assert elapsed >= BUSY_WAIT_FLOOR_S, f"busy_timeout=5000 refused in {elapsed:.3f}s"
        else:
            assert elapsed < BUSY_WAIT_FLOOR_S, f"busy_timeout=0 waited {elapsed:.3f}s"
        holder.execute("ROLLBACK")  # the attempt below is a new one after the release
        assert holder.in_transaction is False, "ROLLBACK left the holder's transaction open"
        contender.execute(BEGIN_IMMEDIATE)
        contender.execute("INSERT INTO probe_rows (id, note) VALUES (2, 'committed')")
        contender.execute("COMMIT")
        assert _count(holder) == 1, "the released lock did not admit the contender's write"
    for connection in opened:  # the ExitStack closed both, before the probe directory went away
        with pytest.raises(sqlite3.ProgrammingError) as closed_error:  # closed, not merely gone
            connection.execute("SELECT 1")
        assert "closed" in str(closed_error.value), closed_error.value
