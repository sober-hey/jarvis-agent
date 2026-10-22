"""K0-014 cancellation DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

Explicit offline in-memory fixtures only. No CLI, Worker callback, supervisor or
packaging/ integration is registered. Safe-point evidence uses the repository's
existing fail-closed domain adapter; synthetic grants are NOT runtime authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jarvis.repository import (
    CASConflictError,
    DraftOnlyError,
    GuardRejectedError,
    LockNotHeldError,
    RepositoryError,
    StaleEpochError,
    TaskRepository,
)

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELED"})


@dataclass(frozen=True)
class CancellationResult:
    task_id: str
    status: Literal["CANCELED", "QUEUED", "REJECTED"]
    reason: str


class CancellationManager:
    """Worker-owned cooperative cancellation, not an external request handler.

    The task's durable cancel_requested fields ARE the queue. No in-memory queue
    may lose a request on replacement. The current Worker must call checkpoint at
    safe boundaries. Normal cancellation never signals or closes active Attempts.
    """

    def __init__(self, repository: TaskRepository, *, draft_fixture: bool = False) -> None:
        if draft_fixture is not True:
            raise DraftOnlyError("explicit offline draft_fixture=True required")
        self.repository = repository

    def _assert_current(self) -> None:
        self.repository._validate_connection(self.repository.connection)  # pyright: ignore[reportPrivateUsage]
        self.repository.worker_lock.assert_held()
        if self.repository.connection.in_transaction:
            raise RepositoryError("caller transaction is not allowed")
        if self.repository.connection.execute(
            "SELECT worker_epoch FROM system_control WHERE singleton_id=1"
        ).fetchone() != (self.repository.worker_epoch,):
            raise StaleEpochError("cancellation worker epoch is no longer current")

    def request_cancel(
        self,
        task_id: str,
        *,
        expected_version: int,
        actor: str,
        reason: str,
        now_ms: int,
    ) -> CancellationResult:
        try:
            self._assert_current()
        except LockNotHeldError:
            return CancellationResult(
                task_id, "REJECTED", "worker lock conflict; cancellation denied"
            )
        task = self.repository.get_task(task_id)
        if task is None or task["version"] != expected_version:
            raise CASConflictError("cancel expected id/version did not match")
        if task["state"] in _TERMINAL:
            return CancellationResult(task_id, "REJECTED", "terminal history is immutable")
        self.repository.request_cancel(
            task_id, expected_version=expected_version, actor=actor, reason=reason, now_ms=now_ms
        )
        return self.checkpoint(task_id, now_ms=now_ms)

    def checkpoint(self, task_id: str, *, now_ms: int) -> CancellationResult:
        """Complete only after existing contract guards positively prove safety."""
        try:
            self._assert_current()
        except LockNotHeldError:
            return CancellationResult(
                task_id, "REJECTED", "worker lock conflict; cancellation denied"
            )
        task = self.repository.get_task(task_id)
        if task is None or task["cancel_requested_at_ms"] is None:
            return CancellationResult(task_id, "REJECTED", "no queued cancellation")
        if task["state"] == "CANCELED":
            return CancellationResult(task_id, "CANCELED", "already canceled")
        if task["state"] in _TERMINAL:
            return CancellationResult(task_id, "REJECTED", "terminal history is immutable")
        # Even synthetic domain grants cannot hide an unresolved started effect,
        # including one from a superseded plan. Never erase its recovery inventory.
        unresolved = self.repository.connection.execute(
            "SELECT 1 FROM operation_intents i JOIN task_steps s ON s.id=i.step_id "
            "WHERE s.task_id=? AND (i.state NOT IN ('PREPARED','VERIFIED') "
            "OR s.state IN ('EXECUTING','NEEDS_RECONCILIATION','VERIFYING')) LIMIT 1",
            (task_id,),
        ).fetchone()
        if unresolved:
            return CancellationResult(task_id, "QUEUED", "existing Intent requires reconciliation")
        try:
            self.repository.transition_task_state(
                task_id,
                from_state=task["state"],
                to_state="CANCELED",
                expected_version=task["version"],
                now_ms=now_ms,
            )
        except GuardRejectedError:
            return CancellationResult(task_id, "QUEUED", "waiting for proven safe checkpoint")
        return CancellationResult(task_id, "CANCELED", "safe checkpoint proved")


@dataclass(frozen=True)
class EmergencyStopResult:
    signal_sent: bool
    marker_path: Path
    snapshot_path: Path
    database_stop_persisted: bool
    database_stop_error: str | None


class EmergencyStop:
    """Signal a bootstrap-owned fixture child first, then seal forensic evidence.

    Files are exclusive-created, hashed and chmod-read-only (NOT tamper-proof
    against their owner/root); an incomplete archive has no complete manifest.
    No automatic resume/retry is granted by either marker or snapshot.

    A Popen handle (never a caller-supplied PID) is required. SIGTERM is a request,
    NOT proof of exit: ignored TERM/descendant containment requires a supervisor.
    This offline control repository is owned by the caller; the target child is a
    stand-in executor. It is NOT a cross-process control plane for Worker's private
    in-memory database. Production signaling/identity and responsive IPC stay open.
    Snapshot is a read-only ledger image, not a resumable persistent runtime DB.
    """

    def __init__(
        self,
        repository: TaskRepository,
        *,
        archive_root: Path,
        draft_fixture: bool = False,
    ) -> None:
        if draft_fixture is not True:
            raise DraftOnlyError("explicit offline draft_fixture=True required")
        self.repository = repository
        self.archive_root = Path(archive_root).resolve(strict=True)
        if not self.archive_root.is_dir():
            raise ValueError("archive_root must be an existing trusted directory")

    @staticmethod
    def _seal(path: Path, payload: Any) -> str:
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fchmod(stream.fileno(), 0o400)
            os.fsync(stream.fileno())
        return hashlib.sha256(encoded).hexdigest()

    def stop(
        self,
        process: object,
        *,
        actor: str,
        reason: str,
        now_ms: int,
    ) -> EmergencyStopResult:
        if (
            not isinstance(process, subprocess.Popen)
            or process.pid <= 1
            or process.pid == os.getpid()
        ):
            raise ValueError("a bootstrap-owned child Popen is required, never a raw/self PID")
        if not actor or not reason or type(now_ms) is not int or now_ms < 0:
            raise ValueError("actor, reason and nonnegative integer timestamp required")
        # Never wait for safe points, ledger locks, snapshot IO or domain guards
        # before dispatching TERM. Any later persistence failure must surface.
        sent = process.poll() is None
        if sent:
            process.send_signal(signal.SIGTERM)
        archive = Path(tempfile.mkdtemp(prefix="emergency-stop-", dir=self.archive_root))
        marker = archive / "emergency-stop.json"
        metadata: dict[str, Any] = {
            "status": DRAFT_STATUS,
            "worker_pid": process.pid,
            "worker_epoch": self.repository.worker_epoch,
            "actor": actor,
            "reason": reason,
            "now_ms": now_ms,
            "signal_sent": sent,
            "exit_not_proved": True,
        }
        marker_hash = self._seal(marker, metadata)
        database_stop_persisted = False
        database_stop_error: str | None = None
        try:
            with self.repository._write():  # pyright: ignore[reportPrivateUsage]
                self.repository.connection.execute(
                    "UPDATE system_control SET emergency_stop=1 WHERE singleton_id=1"
                )
            database_stop_persisted = True
        except (RepositoryError, sqlite3.Error) as error:
            # Emergency marker/snapshot never waits for or bypasses lifetime flock.
            # Surface this limitation in the returned result AND sealed evidence.
            database_stop_error = str(error)
        metadata["database_stop_persisted"] = database_stop_persisted
        metadata["database_stop_error"] = database_stop_error
        self.repository._validate_connection(self.repository.connection)  # pyright: ignore[reportPrivateUsage]
        tables: dict[str, Any] = {}
        # Single connection/thread; no concurrent writer can mutate this fixture.
        for (name,) in self.repository.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall():
            escaped = name.replace('"', '""')
            cursor = self.repository.connection.execute(f'SELECT * FROM "{escaped}"')
            columns = [item[0] for item in cursor.description]
            tables[name] = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        snapshot = archive / "snapshot.json"
        snapshot_hash = self._seal(snapshot, {"metadata": metadata, "tables": tables})
        self._seal(
            archive / "manifest.json",
            {"snapshot.json": snapshot_hash, "emergency-stop.json": marker_hash},
        )
        archive.chmod(0o500)
        fd = os.open(archive, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        # Persist the new archive directory entry as well as its contents.
        parent_fd = os.open(self.archive_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return EmergencyStopResult(
            sent, marker, snapshot, database_stop_persisted, database_stop_error
        )
