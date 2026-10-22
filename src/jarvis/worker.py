"""K0-007 Worker DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

Explicit offline, in-memory fixtures only, matching TaskRepository's boundary.
The project-scoped flock is held for the whole context; the OS releases it after
process death once ALL copies of its fd are closed (do not fork while owning it).
No CLI, publisher, migration or packaging/ integration is registered here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis.repository import (
    DraftOnlyError,
    RepositoryError,
    StaleEpochError,
    TaskRepository,
    WorkerLock,
)

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False
_LOCK_NAME = ".jarvis-worker.lock"


def _validate_fixture_connection(connection: sqlite3.Connection) -> None:
    """Keep the K0-005 offline boundary; never opt in a file-backed runtime DB."""
    if connection.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL:
        raise RepositoryError("draft requires legacy transaction control (commit/rollback active)")
    if connection.row_factory is not None:
        raise RepositoryError("draft requires the default tuple row_factory")
    if any(row[2] for row in connection.execute("PRAGMA database_list")):
        raise DraftOnlyError("DRAFT only accepts in-memory fixture databases")


class Worker:
    """One fixed project lock plus a monotonically increasing global epoch.

    project_root is an existing trusted bootstrap root, NOT a per-run directory.
    All workers for the same project/database must use that same root. The lock
    inode is never unlinked, truncated or replaced. No PID/lease substitutes for it.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        project_root: Path,
        draft_fixture: bool = False,
    ) -> None:
        if draft_fixture is not True:
            raise DraftOnlyError("explicit offline draft_fixture=True required")
        _validate_fixture_connection(connection)
        self.project_root = Path(project_root).resolve(strict=True)
        if not self.project_root.is_dir():
            raise ValueError("project_root must be an existing directory")
        self.connection = connection
        self.lock_path = self.project_root / _LOCK_NAME
        self._lock = WorkerLock(self.lock_path)
        self._epoch: int | None = None
        self._executing = False

    @property
    def epoch(self) -> int:
        self._lock.assert_held()
        if self._epoch is None:
            raise StaleEpochError("worker has no acquired epoch")
        return self._epoch

    def __enter__(self) -> Worker:
        _validate_fixture_connection(self.connection)
        if self.connection.in_transaction:
            raise RepositoryError("caller transaction is not allowed")
        if self.connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise RepositoryError("foreign_keys must be enabled before writing")
        self._lock.__enter__()  # fcntl.LOCK_EX | fcntl.LOCK_NB; retain fd until exit.
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute(
                    "SELECT worker_epoch FROM system_control WHERE singleton_id = 1"
                ).fetchone()
                if row is None or type(row[0]) is not int or not 0 <= row[0] < 2**63 - 1:
                    raise StaleEpochError("missing, invalid or exhausted global epoch")
                next_epoch = row[0] + 1
                changed = self.connection.execute(
                    "UPDATE system_control SET worker_epoch = ? "
                    "WHERE singleton_id = 1 AND worker_epoch = ?",
                    (next_epoch, row[0]),
                ).rowcount
                if changed != 1:
                    raise StaleEpochError("epoch acquisition did not update exactly one row")
                if self.connection.execute(
                    "SELECT worker_epoch FROM system_control WHERE singleton_id = 1"
                ).fetchone() != (next_epoch,):
                    raise StaleEpochError("acquired epoch changed before commit")
                self._lock.assert_held()
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
            self._epoch = next_epoch
        except BaseException:
            self._lock.__exit__()
            raise
        return self

    def assert_current_epoch(self) -> None:
        """Read-only start check; writes still need the transactional SQL fence."""
        epoch = self.epoch
        if self.connection.execute(
            "SELECT worker_epoch FROM system_control WHERE singleton_id = 1"
        ).fetchone() != (epoch,):
            raise StaleEpochError("worker epoch is no longer current")

    def execute[Result](self, operation: Callable[[TaskRepository], Result]) -> Result:
        """Run one synchronous OFFLINE callback; every write uses repository fences.

        No filesystem/network effect, automatic retry or result inference is added.
        k0_execution_slot_free remains fail-closed: no domain grant is injected.
        Unknown outcomes must be reconciled by the future trusted adapter first.
        A callback is not sandboxed and must not directly write/commit this DB.
        """
        self.assert_current_epoch()
        if self._executing:
            raise RepositoryError("worker execution is not reentrant")
        repository = TaskRepository(
            self.connection,
            worker_lock=self._lock,
            worker_epoch=self.epoch,
            draft_fixture=True,
        )
        self._executing = True
        try:
            result = operation(repository)
            self.assert_current_epoch()
            return result
        finally:
            self._executing = False

    def recover_tasks(self) -> tuple[dict[str, Any], ...]:
        """Discover durable unfinished tasks under the new epoch, without replay.

        This is a recovery INVENTORY, not an Attempt/Intent recovery state machine.
        Historical epochs/versions/cancellation flags and unknown effects are kept.
        No task is marked READY/SUCCEEDED, overwritten, retried or auto-canceled.
        """
        self.assert_current_epoch()
        cursor = self.connection.execute(
            "SELECT * FROM tasks WHERE state NOT IN ('SUCCEEDED', 'FAILED', 'CANCELED') "
            "ORDER BY created_at_ms, id"
        )
        names = tuple(item[0] for item in cursor.description)
        return tuple(dict(zip(names, row, strict=True)) for row in cursor.fetchall())

    def __exit__(self, *_: object) -> None:
        if self._executing:
            raise RepositoryError("cannot release the worker lock during execution")
        try:
            self._lock.__exit__()
        finally:
            self._epoch = None
