"""K0-002 candidate capability probe: ``flock`` exclusion/handoff and SQLite WAL visibility.

Candidate evidence only: approves nothing (ADR-0001 PROPOSED, INV-010 NOT_IMPLEMENTED, no DOC-R or
TRACE change, no product code touched); raw platform probe like ``test_filesystem_capabilities``.

Lock part. Two child processes open the lock file themselves (own fd, own open file description).
Ordering comes from pipes and a signal, never from sleeping: the holder reports ``locked``; the
contender's explicit ``LOCK_EX|LOCK_NB`` attempt is refused and reported as ``busy errno=...`` (the
exclusion handshake, not silence); after ``SIGTERM`` (default disposition, so the kernel closes the
holder's fds) its *new* blocking attempt reports ``acquired``. Each participant records pid, its own
fd, ``st_dev``/``st_ino`` and platform.

Not claimed: other filesystems/mounts (evidence is bound to this volume's ``st_dev``), fairness,
same-process thread mutex, repeated ``flock`` on one fd, the product lock path. Unsupported volumes
and whitelisted errnos skip explicitly; every other errno fails.

WAL part. Throwaway database on the same volume; two connections with ``journal_mode=WAL``,
``synchronous=FULL``, ``busy_timeout=5000``, ``foreign_keys=ON``: one holds ``BEGIN IMMEDIATE`` with
an uncommitted row, an open reader must not see it and keeps that snapshot after ``COMMIT`` returns,
while a *new* transaction must see the committed row.
"""

from __future__ import annotations

import errno
import os
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

import pytest

if os.name != "posix":  # flock has no meaning off POSIX; report that, never a silent PASS
    pytest.skip("the flock probe requires a POSIX platform", allow_module_level=True)

import fcntl  # noqa: E402 - imported only after the platform gate

LOCK_EX, LOCK_NB = fcntl.LOCK_EX, fcntl.LOCK_NB

REPO_ROOT = Path(__file__).resolve().parents[2]
Proc = subprocess.Popen[bytes]
WINDOW_SECONDS = 15.0  # bounded waits for records and for reaping; it never picks a winner
FLOCK_UNSUPPORTED_ERRNOS = frozenset({errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS, errno.ENOLCK})
LOCK_HELD_ERRNOS = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES})
PRAGMAS = (("journal_mode", "wal"), ("synchronous", 2), ("busy_timeout", 5000), ("foreign_keys", 1))
RecordProperty = Callable[[str, object], None]

_WORKER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)  # each participant opens it itself
st = os.fstat(fd)
ident = b"pid=%d fd=%d dev=%d ino=%d platform=%s" % (
    os.getpid(), fd, st.st_dev, st.st_ino, sys.platform.encode())
if sys.argv[2] == "holder":
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(1, b"locked " + ident + b"\\n")
    sys.stdin.readline()  # default SIGTERM disposition until the parent kills us
else:
    sys.stdin.readline()  # gate 1: explicit non-blocking attempt while the holder lives
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.write(1, b"busy errno=%d " % (exc.errno or 0) + ident + b"\\n")
    else:
        os.write(1, b"free " + ident + b"\\n")  # must never happen: exclusion did not hold
        raise SystemExit(4)
    sys.stdin.readline()  # gate 2: a new attempt after the holder is released
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(1, b"acquired " + ident + b"\\n")
"""


@contextmanager
def probe_workspace() -> Generator[Path, None, None]:
    """Throwaway directory on the target volume; removal is verified on every exit path."""
    directory = tempfile.TemporaryDirectory(dir=REPO_ROOT, prefix=".k0-002-lock-probe-")
    root = Path(directory.name)
    try:
        assert os.stat(root).st_dev == os.stat(REPO_ROOT).st_dev, "probe dir left the target volume"
        yield root
    finally:  # a leak assertion here can mask the original error; a leak matters more
        directory.cleanup()
        assert not root.exists(), f"probe workspace leaked: {root}"


def _spawn(lock_path: Path, role: str) -> Proc:
    argv = [sys.executable, "-c", _WORKER, str(lock_path), role]
    pipe = subprocess.PIPE
    return subprocess.Popen(argv, stdin=pipe, stdout=pipe, stderr=subprocess.STDOUT)


def _record(proc: Proc, event: str, window: float) -> dict[str, str]:
    """Read one worker record within ``window``; every call requires the expected event token."""
    assert proc.stdout is not None, "worker stdout pipe missing"
    ready, _, _ = select.select([proc.stdout], [], [], window)
    assert ready, f"no {event} record within {window}s (pid {proc.pid})"
    tokens = os.read(proc.stdout.fileno(), 512).decode("utf-8", "replace").split()
    assert tokens and tokens[0] == event, f"expected {event}, got {tokens!r}"
    fields = {key: value for key, _, value in (token.partition("=") for token in tokens[1:])}
    return {**fields, "event": tokens[0]}


def _try_exclusive(lock_path: Path) -> int | None:
    with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "rb") as handle:
        try:
            fcntl.flock(handle.fileno(), LOCK_EX | LOCK_NB)
        except OSError as exc:
            number = exc.errno or 0
            if number in FLOCK_UNSUPPORTED_ERRNOS:  # no flock here: skip, never a silent PASS
                pytest.skip(f"flock unsupported on this platform/volume: errno {number}")
            return number
        return None  # closing the handle releases the lock again


def _probe_flock_handoff(lock_path: Path) -> dict[str, dict[str, str]]:
    """Holder locks; contender's LOCK_NB is refused; SIGTERM releases; a new attempt wins."""
    workers: list[Proc] = []
    try:
        holder = _spawn(lock_path, "holder")
        workers.append(holder)
        locked = _record(holder, "locked", WINDOW_SECONDS)
        contender = _spawn(lock_path, "contender")
        workers.append(contender)
        assert contender.stdin is not None, "worker stdin pipe missing"
        contender.stdin.write(b"go\n")  # gate 1
        contender.stdin.flush()
        busy = _record(contender, "busy", WINDOW_SECONDS)
        assert int(busy["errno"]) in LOCK_HELD_ERRNOS, f"the second process was not refused: {busy}"
        holder.terminate()  # SIGTERM, default disposition: the kernel drops the holder's flock
        try:
            holder.communicate(timeout=WINDOW_SECONDS)
        except subprocess.TimeoutExpired:
            pytest.fail("the holder survived SIGTERM")
        assert holder.returncode == -signal.SIGTERM, f"holder exit status {holder.returncode}"
        contender.stdin.write(b"go\n")  # gate 2
        contender.stdin.flush()
        acquired = _record(contender, "acquired", WINDOW_SECONDS)
        contender.communicate(timeout=WINDOW_SECONDS)  # bounded wait on the new attempt
        assert contender.returncode == 0, f"contender exit status {contender.returncode}"
        assert locked["pid"] == str(holder.pid), "the locked record is not the holder's"
        assert busy["pid"] == acquired["pid"] == str(contender.pid), "not the contender's records"
        return {"locked": locked, "busy": busy, "acquired": acquired}
    finally:  # reap every worker and close its pipes: nothing may outlive the probe directory
        for worker in workers:
            try:
                if worker.poll() is None:
                    worker.terminate()
                    try:
                        worker.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.wait(timeout=5)
            finally:  # close even if the wait above raises: wait() alone would leak the fds
                for pipe in (worker.stdin, worker.stdout, worker.stderr):
                    if pipe is not None and not pipe.closed:
                        pipe.close()


def test_candidate_flock_handoff_and_startup_failure_reaping(
    record_property: RecordProperty, monkeypatch: pytest.MonkeyPatch
) -> None:
    with probe_workspace() as root:
        lock_path = root / "candidate.lock"
        free = _try_exclusive(lock_path)  # positive control: this probe works on a free lock
        assert free is None, f"the parent could not take an unheld flock: errno {free}"
        fields = _probe_flock_handoff(lock_path)
        observed = lock_path.stat()
        for event in ("locked", "busy", "acquired"):
            record = fields[event]
            assert (int(record["dev"]), int(record["ino"])) == (observed.st_dev, observed.st_ino)
            assert record["platform"] == sys.platform, record
        assert fields["locked"]["pid"] != fields["acquired"]["pid"], "one process reported both"
        record_property("evidence_class", "candidate")
        record_property("lock_events", fields)  # pid, own fd, dev/ino and platform
    spawned: list[Proc] = []
    real_spawn = _spawn

    def flaky_spawn(lock_path: Path, role: str) -> Proc:
        if spawned:  # second call: fail the way an exhausted process table would
            raise OSError(errno.EAGAIN, "injected second-spawn failure")
        spawned.append(worker := real_spawn(lock_path, role))
        return worker

    monkeypatch.setattr(sys.modules[__name__], "_spawn", flaky_spawn)
    with pytest.raises(OSError, match="injected second-spawn failure"), probe_workspace() as root:
        _probe_flock_handoff(root / "candidate.lock")  # the first worker must still be reaped
    assert len(spawned) == 1, "the first participant was never spawned"
    assert spawned[0].returncode is not None, f"pid={spawned[0].pid} was not reaped"


def _wal_connect(db_path: Path) -> sqlite3.Connection:
    """Open a throwaway connection; the caller registers it for closing as soon as it exists."""
    return sqlite3.connect(db_path, isolation_level=None)


def _wal_connections(
    db_path: Path, stack: ExitStack
) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    """Register each connection as it is created, so a failing second one still closes the first."""
    return (
        stack.enter_context(closing(_wal_connect(db_path))),
        stack.enter_context(closing(_wal_connect(db_path))),
    )


def test_candidate_wal_uncommitted_invisible_and_new_transaction_sees_commit(
    record_property: RecordProperty,
) -> None:
    def scalar(connection: sqlite3.Connection, sql: str) -> object:
        row = connection.execute(sql).fetchone()
        assert row is not None, f"{sql} returned no row"
        return row[0]

    rows = "SELECT count(*) FROM probe_rows"
    with probe_workspace() as root, ExitStack() as stack:
        writer, reader = _wal_connections(root / "candidate_wal.db", stack)
        for connection in (writer, reader):  # each connection proves its own pragmas
            for name, value in PRAGMAS:  # foreign_keys = 1 is ON
                connection.execute(f"PRAGMA {name} = {value}")
                assert scalar(connection, f"PRAGMA {name}") == value, f"{name} not applied"
        writer.execute("CREATE TABLE probe_rows (id INTEGER PRIMARY KEY, note TEXT NOT NULL)")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO probe_rows (id, note) VALUES (1, 'uncommitted')")
        reader.execute("BEGIN")
        assert scalar(reader, rows) == 0, "the reader saw an uncommitted row"
        writer.execute("COMMIT")  # returns only once the write is published
        assert scalar(reader, rows) == 0, "the open read snapshot moved under the reader"
        reader.execute("COMMIT")
        reader.execute("BEGIN")  # a new transaction must observe the committed row
        assert scalar(reader, rows) == 1, "a new transaction did not see the committed row"
        reader.execute("COMMIT")
        record_property("evidence_class", "candidate")
        record_property("wal_files_during_probe", sorted(p.name for p in root.iterdir()))


def test_candidate_wal_second_connection_failure_closes_the_first(
    monkeypatch: pytest.MonkeyPatch, record_property: RecordProperty
) -> None:
    opened: list[sqlite3.Connection] = []
    real_connect = _wal_connect

    def flaky_connect(db_path: Path) -> sqlite3.Connection:
        if opened:  # second call: fail the way an unreachable database would
            raise sqlite3.OperationalError("injected second-connect failure")
        opened.append(connection := real_connect(db_path))
        return connection

    monkeypatch.setattr(sys.modules[__name__], "_wal_connect", flaky_connect)
    with (
        pytest.raises(sqlite3.OperationalError, match="injected second-connect failure"),
        probe_workspace() as root,
        ExitStack() as stack,
    ):
        _wal_connections(root / "candidate_wal.db", stack)  # the first connection must be closed
    assert len(opened) == 1, "the first connection was never opened"
    with pytest.raises(sqlite3.ProgrammingError) as closed_error:  # closed, not merely gone
        opened[0].execute("SELECT 1")
    assert "closed" in str(closed_error.value), closed_error.value
    record_property("evidence_class", "candidate")
    record_property("closed_connection_error", str(closed_error.value))
