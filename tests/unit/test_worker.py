"""K0-007 DRAFT: offline SQLite fixtures and real project-scoped flock only."""

from __future__ import annotations

import importlib.util
import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _connection() -> sqlite3.Connection:
    spec = importlib.util.spec_from_file_location(
        "k0004_worker_fixture", ROOT / "tests/contract/test_sqlite_schema_draft.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = cast("sqlite3.Connection", module._apply_candidate(module._load()))
    connection.execute("INSERT INTO system_control VALUES (1, 0, 0, 7, 0)")
    connection.commit()
    return connection


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    db = _connection()
    try:
        yield db
    finally:
        db.close()


def test_worker_acquires_project_lock_and_increments_epoch(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.worker import DRAFT_STATUS, RUNTIME_ENABLED, Worker

    project = tmp_path / "project"
    project.mkdir()
    with Worker(connection, project_root=project, draft_fixture=True) as worker:
        assert DRAFT_STATUS == "DRAFT_PENDING_DP"
        assert RUNTIME_ENABLED is False
        assert worker.lock_path == project / ".jarvis-worker.lock"
        assert worker.epoch == 8
        worker.assert_current_epoch()
        assert connection.execute("SELECT worker_epoch FROM system_control").fetchone() == (8,)
        assert not connection.in_transaction
        inode = worker.lock_path.stat().st_ino
    assert worker.lock_path.stat().st_ino == inode  # Never unlink the authority inode.
    assert connection.execute("SELECT worker_epoch FROM system_control").fetchone() == (8,)


def test_second_worker_cannot_start_or_bump_epoch(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.worker import Worker

    with Worker(connection, project_root=tmp_path, draft_fixture=True):
        with pytest.raises(BlockingIOError):
            with Worker(connection, project_root=tmp_path, draft_fixture=True):
                pytest.fail("second worker acquired the execution lock")
        assert connection.execute("SELECT worker_epoch FROM system_control").fetchone() == (8,)


def test_new_worker_reacquires_same_inode_and_new_epoch(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.worker import Worker

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as first:
        old_epoch = first.epoch
        inode = first.lock_path.stat().st_ino
    with Worker(connection, project_root=tmp_path, draft_fixture=True) as successor:
        assert successor.epoch == old_epoch + 1
        assert successor.lock_path.stat().st_ino == inode


def _create(repository: object) -> str:
    from jarvis.repository import TaskRepository

    assert isinstance(repository, TaskRepository)
    return str(
        repository.create_task(
            task_id="t1",
            request_key="r1",
            request_digest="a" * 64,
            task_kind="inventory.generate",
            now_ms=10,
        )["id"]
    )


def test_execute_uses_the_fenced_repository(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from jarvis.worker import Worker

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        assert worker.execute(_create) == "t1"
        assert connection.execute("SELECT id FROM tasks").fetchall() == [("t1",)]
        assert connection.execute("SELECT actor_id FROM events").fetchall() == [
            ("fixture-epoch-8",)
        ]


def test_stale_epoch_denies_execution_before_callback(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.repository import StaleEpochError
    from jarvis.worker import Worker

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        connection.execute("UPDATE system_control SET worker_epoch = 9")
        connection.commit()
        with pytest.raises(StaleEpochError):
            worker.execute(_create)
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)


def test_epoch_expiring_inside_commit_rolls_back_task_and_event(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.repository import StaleEpochError
    from jarvis.worker import Worker

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        connection.execute(
            "CREATE TRIGGER expire_worker AFTER INSERT ON events BEGIN "
            "UPDATE system_control SET worker_epoch = worker_epoch + 1; END"
        )
        with pytest.raises(StaleEpochError):
            worker.execute(_create)
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
        assert connection.execute("SELECT worker_epoch FROM system_control").fetchone() == (8,)
        assert connection.in_transaction is False


def test_recovery_inventory_preserves_unknown_results_without_retrying(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.worker import Worker

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        worker.execute(_create)
    # Synthetic interrupted state, NOT proof that a real external effect happened.
    connection.execute("UPDATE tasks SET state = 'NEEDS_RECONCILIATION'")
    connection.commit()
    before = connection.execute("SELECT * FROM tasks").fetchall()
    with Worker(connection, project_root=tmp_path, draft_fixture=True) as successor:
        pending = successor.recover_tasks()
        assert len(pending) == 1
        assert pending[0]["id"] == "t1"
        assert pending[0]["state"] == "NEEDS_RECONCILIATION"
        assert connection.execute("SELECT * FROM tasks").fetchall() == before
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)
        assert successor.epoch == 9


def test_sigkill_releases_flock_and_successor_advances_committed_snapshot(
    tmp_path: Path,
) -> None:
    """Real process death; snapshot replay is NOT persistent DB crash recovery."""
    from jarvis.worker import Worker

    script = """
import json, runpy, signal, sys
from pathlib import Path
from jarvis.worker import Worker
fixture = runpy.run_path(sys.argv[1])
connection = fixture['_connection']()
with Worker(connection, project_root=Path(sys.argv[2]), draft_fixture=True) as worker:
    print(json.dumps({'epoch': worker.epoch, 'dump': list(connection.iterdump())}), flush=True)
    signal.pause()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(Path(__file__).resolve()), str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    snapshot: sqlite3.Connection | None = None
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, "child did not acquire its epoch within timeout"
        observed = json.loads(process.stdout.readline())
        assert observed["epoch"] == 8
        snapshot = sqlite3.connect(":memory:")
        snapshot.executescript("\n".join(observed["dump"]))
        snapshot.execute("PRAGMA foreign_keys = ON")
        inode = (tmp_path / ".jarvis-worker.lock").stat().st_ino
        with pytest.raises(BlockingIOError):
            with Worker(snapshot, project_root=tmp_path, draft_fixture=True):
                pytest.fail("the child must own the actual flock")
        process.kill()  # SIGKILL: no __exit__/finally or explicit flock unlock.
        assert process.wait(timeout=10) == -signal.SIGKILL
        with Worker(snapshot, project_root=tmp_path, draft_fixture=True) as successor:
            assert successor.epoch == observed["epoch"] + 1
            assert successor.lock_path.stat().st_ino == inode
            assert snapshot.execute("SELECT worker_epoch FROM system_control").fetchone() == (9,)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        if snapshot is not None:
            snapshot.close()


@pytest.mark.parametrize("control", ["missing", "exhausted"])
def test_invalid_epoch_rolls_back_and_releases_lock(
    connection: sqlite3.Connection, tmp_path: Path, control: str
) -> None:
    from jarvis.repository import StaleEpochError
    from jarvis.worker import Worker

    if control == "missing":
        connection.execute("DELETE FROM system_control")
    else:
        connection.execute("UPDATE system_control SET worker_epoch = ?", (2**63 - 1,))
    connection.commit()
    with pytest.raises(StaleEpochError):
        with Worker(connection, project_root=tmp_path, draft_fixture=True):
            pytest.fail("invalid epoch must never start")
    assert not connection.in_transaction
    connection.execute("DELETE FROM system_control")
    connection.execute("INSERT INTO system_control VALUES (1, 0, 0, 7, 0)")
    connection.commit()
    with Worker(connection, project_root=tmp_path, draft_fixture=True) as successor:
        assert successor.epoch == 8


def test_epoch_transaction_failure_releases_lock(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.worker import Worker

    connection.execute(
        "CREATE TRIGGER deny_epoch BEFORE UPDATE ON system_control "
        "BEGIN SELECT RAISE(ABORT, 'epoch write failed'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="epoch write failed"):
        with Worker(connection, project_root=tmp_path, draft_fixture=True):
            pytest.fail("failed epoch must never start")
    assert connection.execute("SELECT worker_epoch FROM system_control").fetchone() == (7,)
    assert not connection.in_transaction
    connection.execute("DROP TRIGGER deny_epoch")
    with Worker(connection, project_root=tmp_path, draft_fixture=True) as successor:
        assert successor.epoch == 8


def test_worker_requires_opt_in(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from jarvis.repository import DraftOnlyError
    from jarvis.worker import Worker

    with pytest.raises(DraftOnlyError):
        Worker(connection, project_root=tmp_path)
    assert not (tmp_path / ".jarvis-worker.lock").exists()


def test_persistent_database_not_enabled_by_draft_worker(tmp_path: Path) -> None:
    from jarvis.repository import DraftOnlyError
    from jarvis.worker import Worker

    connection = sqlite3.connect(tmp_path / "fixture.db")
    try:
        with pytest.raises(DraftOnlyError):
            Worker(connection, project_root=tmp_path, draft_fixture=True)
    finally:
        connection.close()
    assert not (tmp_path / ".jarvis-worker.lock").exists()


def test_symlink_project_root_resolves_to_same_execution_lock(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.worker import Worker

    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    with Worker(connection, project_root=project, draft_fixture=True):
        with pytest.raises(BlockingIOError):
            with Worker(connection, project_root=alias, draft_fixture=True):
                pytest.fail("project alias bypassed the one fixed lock")


def test_lock_file_symlink_is_rejected(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from jarvis.worker import Worker

    target = tmp_path / "other"
    target.write_text("untouched")
    (tmp_path / ".jarvis-worker.lock").symlink_to(target)
    with pytest.raises(OSError):
        with Worker(connection, project_root=tmp_path, draft_fixture=True):
            pytest.fail("symlink lock must not start")
    assert target.read_text() == "untouched"
    assert connection.execute("SELECT worker_epoch FROM system_control").fetchone() == (7,)


def test_execute_cannot_reenter_or_release_worker(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.repository import RepositoryError
    from jarvis.worker import Worker

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:

        def callback(_repository: object) -> str:
            with pytest.raises(RepositoryError, match="not reentrant"):
                worker.execute(_create)
            with pytest.raises(RepositoryError, match="during execution"):
                worker.__exit__()
            worker.assert_current_epoch()
            return "still owning lock"

        assert worker.execute(callback) == "still owning lock"
        assert worker.execute(_create) == "t1"


def test_callback_failure_does_not_lose_execution_slot(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.worker import Worker

    def fail(_repository: object) -> str:
        raise ValueError("fixture error")

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        with pytest.raises(ValueError, match="fixture error"):
            worker.execute(fail)
        assert worker.execute(_create) == "t1"


def test_saved_old_repository_cannot_submit_after_successor_acquires_epoch(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.repository import LockNotHeldError
    from jarvis.worker import Worker

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as first:
        old_repository = first.execute(lambda repository: repository)
    with Worker(connection, project_root=tmp_path, draft_fixture=True) as successor:
        assert successor.epoch == 9
        with pytest.raises(LockNotHeldError):
            _create(old_repository)
        assert successor.execute(_create) == "t1"


def test_startup_epoch_changed_by_trigger_is_rolled_back(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.repository import StaleEpochError
    from jarvis.worker import Worker

    connection.execute(
        "CREATE TRIGGER alter_epoch AFTER UPDATE ON system_control "
        "BEGIN UPDATE system_control SET worker_epoch = worker_epoch + 1; END"
    )
    with pytest.raises(StaleEpochError):
        with Worker(connection, project_root=tmp_path, draft_fixture=True):
            pytest.fail("startup must not commit an epoch it does not own")
    assert connection.execute("SELECT worker_epoch FROM system_control").fetchone() == (7,)
    assert not connection.in_transaction
