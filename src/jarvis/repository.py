"""K0-005 DRAFT repository; NOT_FROZEN, NOT_APPROVED, runtime_enabled=False.

Only explicitly opted-in, in-memory design fixtures are accepted. This module is
not wired to a Worker, publisher, migrations, CLI or any packaging/ database.
Event identity/digest/authority and cancellation arbiter remain DRAFT_PENDING_DP.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


class RepositoryError(RuntimeError):
    """Rejected draft repository operation."""


class DraftOnlyError(RepositoryError):
    """Core runtime and persistent databases are not authorized here."""


class LockNotHeldError(RepositoryError):
    """The submitting process/thread does not hold the worker flock."""


class StaleEpochError(RepositoryError):
    """The submitting epoch is missing or no longer current."""


class CASConflictError(RepositoryError):
    """Expected id/source/version did not match exactly one row."""


class GuardRejectedError(RepositoryError):
    """A required guard was false, unknown or unavailable."""


class CancellationDisposition(StrEnum):
    STOP_NOW = "STOP_NOW"
    FINISH_IN_FLIGHT = "FINISH_IN_FLIGHT"
    RECONCILE_FIRST = "RECONCILE_FIRST"


def cancellation_disposition(
    *,
    publish_started: object,
    interruption_affects_system: object,
    effects_known: object,
) -> CancellationDisposition:
    """Safety policy only; not a publish-start grant or CANCELED completion.

    Inputs must come from a future trusted, operation-bound evidence adapter.
    Unknown results never authorize a new publish, retry or overwrite.
    """
    if any(
        type(value) is not bool
        for value in (publish_started, interruption_affects_system, effects_known)
    ):
        return CancellationDisposition.RECONCILE_FIRST
    if effects_known is not True:
        return CancellationDisposition.RECONCILE_FIRST
    if publish_started is True and interruption_affects_system is True:
        return CancellationDisposition.FINISH_IN_FLIGHT
    return CancellationDisposition.STOP_NOW


DomainGuard = Callable[[str, Mapping[str, Any]], object]


class WorkerLock:
    """Retain an exclusive flock fd for the complete worker lifetime.

    The trusted bootstrap must supply ONE fixed lock path for its database.
    This draft does not select a production path or prove a trusted identity.
    Do not unlink/replace the lock file. An inherited fd is not a child grant.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path).absolute()
        self._fd: int | None = None
        self._owner: tuple[int, int] | None = None

    def __enter__(self) -> WorkerLock:
        if self._fd is not None:
            raise LockNotHeldError("worker lock is not reentrant")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self._owner = (os.getpid(), threading.get_ident())
        return self

    def assert_held(self) -> None:
        if self._fd is None or self._owner != (os.getpid(), threading.get_ident()):
            raise LockNotHeldError("current worker does not hold flock")
        descriptor = os.fstat(self._fd)
        try:
            path = self.path.stat(follow_symlinks=False)
        except OSError as error:
            raise LockNotHeldError("worker lock path is unavailable") from error
        if (descriptor.st_dev, descriptor.st_ino) != (path.st_dev, path.st_ino):
            raise LockNotHeldError("worker lock path was replaced")

    def __exit__(self, *_: object) -> None:
        if self._owner != (os.getpid(), threading.get_ident()) or self._fd is None:
            raise LockNotHeldError("only owning process/thread may release worker lock")
        fd = self._fd
        try:
            self.assert_held()
        finally:
            # Even a replaced path must not leak the held original inode/fd.
            self._fd = None
            self._owner = None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


class TaskRepository:
    """Transactions over the K0-004 schema, exclusively for offline fixtures.

    All draft writes conservatively take the worker branch: there is no ordinary
    caller flag that bypasses flock/epoch as a purported request-only grant.
    A trusted, responsive request-only adapter remains future work.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        worker_lock: WorkerLock,
        worker_epoch: int,
        draft_fixture: bool = False,
        transition_guards: Mapping[tuple[str, str], Sequence[str]] | None = None,
        domain_guard: DomainGuard | None = None,
    ) -> None:
        if draft_fixture is not True:
            raise DraftOnlyError("explicit offline draft_fixture=True required")
        if type(worker_epoch) is not int or worker_epoch < 0:
            raise ValueError("worker_epoch must be an exact nonnegative integer")
        self._validate_connection(connection)
        self.connection = connection
        self.worker_lock = worker_lock
        self.worker_epoch = worker_epoch
        self._transition_guards = dict(TASK_GUARDS)
        if (
            transition_guards is not None
            and {edge: tuple(guards) for edge, guards in transition_guards.items()}
            != self._transition_guards
        ):
            raise GuardRejectedError("fixture guard set differs from projected draft contract")
        self._domain_guard = domain_guard

    @staticmethod
    def _validate_connection(connection: sqlite3.Connection) -> None:
        if connection.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL:
            raise RepositoryError(
                "draft requires legacy transaction control (commit/rollback active)"
            )
        if connection.row_factory is not None:
            raise RepositoryError("draft requires the default tuple row_factory")
        if any(row[2] for row in connection.execute("PRAGMA database_list")):
            raise DraftOnlyError("DRAFT only accepts in-memory fixture databases")

    def _fence(self) -> None:
        self.worker_lock.assert_held()
        cursor = self.connection.execute(
            "UPDATE system_control SET worker_epoch = worker_epoch "
            "WHERE singleton_id = 1 AND worker_epoch = ?",
            (self.worker_epoch,),
        )
        if cursor.rowcount != 1:
            raise StaleEpochError("current global epoch barrier did not match one row")

    @contextmanager
    def _write(self) -> Generator[None]:
        self._validate_connection(self.connection)
        if self.connection.in_transaction:
            raise RepositoryError("caller transaction is not allowed")
        if self.connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RepositoryError("foreign_keys must be enabled before writing")
        self.worker_lock.assert_held()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._fence()
            yield
            self._fence()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        cursor = self.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        return (
            None
            if row is None
            else dict(zip((item[0] for item in cursor.description), row, strict=True))
        )

    def _event(
        self,
        task_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        now_ms: int,
        *,
        from_state: str | None = None,
        to_state: str | None = None,
        from_version: int | None = None,
        to_version: int | None = None,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO events (task_id, event_type, actor_type, actor_id, payload_json, "
            "created_at_ms, from_state, to_state, from_version, to_version) "
            "VALUES (?, ?, 'draft_worker', ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                event_type,
                f"fixture-epoch-{self.worker_epoch}",
                json.dumps(dict(payload), allow_nan=False, sort_keys=True),
                now_ms,
                from_state,
                to_state,
                from_version,
                to_version,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def create_task(
        self,
        *,
        task_id: str,
        request_key: str,
        request_digest: str,
        task_kind: str,
        now_ms: int,
    ) -> dict[str, Any]:
        with self._write():
            prior = self.connection.execute(
                "SELECT id, request_digest, task_kind FROM tasks WHERE request_key = ?",
                (request_key,),
            ).fetchone()
            if prior is not None:
                if prior[1:] != (request_digest, task_kind):
                    raise GuardRejectedError("same request_key has different draft content")
                existing = self.get_task(prior[0])
                assert existing is not None
                return existing
            self.connection.execute(
                "INSERT INTO tasks (id, request_key, request_digest, task_kind, state, "
                "version, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, 'CREATED', 0, ?, ?)",
                (task_id, request_key, request_digest, task_kind, now_ms, now_ms),
            )
            self._event(task_id, "draft.task.created", {}, now_ms)
            task = self.get_task(task_id)
            assert task is not None
        return task

    def append_event(
        self,
        task_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> int:
        """Append ordinary audit only; transition columns are repository-owned."""
        if event_type in {
            "draft.task.transition",
            "draft.task.created",
            "draft.task.cancel_requested",
        }:
            raise GuardRejectedError("reserved repository event type")
        with self._write():
            sequence = self._event(task_id, event_type, payload, now_ms)
        return sequence

    def _check_guard(self, name: str, task: Mapping[str, Any]) -> None:
        if name in _TRANSACTION_GUARDS:
            return  # SQL/event/transaction machinery, never an injected boolean.
        if name == "cancel_absent":
            allowed = task["cancel_requested_at_ms"] is None
        elif name == "cancel_persisted":
            allowed = task["cancel_requested_at_ms"] is not None
        elif name == "emergency_stop_inactive":
            allowed = self.connection.execute(
                "SELECT emergency_stop FROM system_control WHERE singleton_id = 1"
            ).fetchone() == (0,)
        elif name == "no_active_attempt":
            allowed = not self.connection.execute(
                "SELECT 1 FROM attempts a JOIN task_steps s ON s.id = a.step_id "
                "WHERE s.task_id = ? AND (a.finished_at_ms IS NULL OR a.state = 'ACTIVE') LIMIT 1",
                (task["id"],),
            ).fetchone()
        elif name == "active_plan_bound":
            plans = self.connection.execute(
                "SELECT id FROM task_plans WHERE task_id = ? AND state = 'ACTIVE'",
                (task["id"],),
            ).fetchall()
            allowed = plans == [(task["active_plan_id"],)] and task["active_plan_id"] is not None
        elif name == "cancellation_complete":
            for component in (
                "cancel_persisted",
                "new_effects_stopped",
                "safe_checkpoint",
                "effects_settled",
                "no_active_attempt",
            ):
                self._check_guard(component, task)
            return
        elif name == "can_start_work":
            self._check_guard("cancel_absent", task)
            self._check_guard("emergency_stop_inactive", task)
            return
        elif name in {"g3_safety_decision_open", "fail_decision_policy_open"}:
            allowed = False  # unresolved safety decision cannot be fixture-self-approved
        else:
            # Synthetic callback is a fixture seam, NOT a production trusted adapter.
            # Missing/mistyped values deny. No model-supplied grant interface exists.
            allowed = (
                self._domain_guard is not None and self._domain_guard(name, dict(task)) is True
            )
        if allowed is not True:
            raise GuardRejectedError(f"guard denied or unknown: {name}")

    def transition_task_state(
        self,
        task_id: str,
        *,
        from_state: str,
        to_state: str,
        expected_version: int,
        now_ms: int,
    ) -> dict[str, Any]:
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be an exact nonnegative integer")
        guards = self._transition_guards.get((from_state, to_state))
        if (from_state, to_state) not in TASK_EDGES or not guards:
            raise GuardRejectedError("edge is not in the supplied draft Task contract")
        if not _SHARED_EDGE_GUARDS.issubset(guards):
            raise GuardRejectedError("draft edge is missing mandatory transaction guards")
        with self._write():
            task = self.get_task(task_id)
            if task is None or task["state"] != from_state or task["version"] != expected_version:
                raise CASConflictError("expected id/source/version did not match")
            for guard in guards:
                self._check_guard(guard, task)
            # Defense independent of synthetic guard injection.
            if to_state in {"READY", "RUNNING", "SUCCEEDED"}:
                self._check_guard("cancel_absent", task)
            if to_state == "CANCELED":
                self._check_guard("cancel_persisted", task)
                self._check_guard("no_active_attempt", task)
            count = self.connection.execute(
                "UPDATE tasks SET state = ?, version = version + 1, updated_at_ms = ? "
                "WHERE id = ? AND state = ? AND version = ?",
                (to_state, now_ms, task_id, from_state, expected_version),
            ).rowcount
            if count != 1:
                raise CASConflictError("conditional UPDATE did not affect exactly one row")
            self._event(
                task_id,
                "draft.task.transition",
                {},
                now_ms,
                from_state=from_state,
                to_state=to_state,
                from_version=expected_version,
                to_version=expected_version + 1,
            )
            # FK/unique support only part of relationships_consistent; callback
            # must also check its post-write graph, not merely a pre-write snapshot.
            updated = self.get_task(task_id)
            assert updated is not None
            if (updated["state"], updated["version"]) != (to_state, expected_version + 1):
                raise CASConflictError("post-write target state/version did not match")
            self._check_guard("relationships_consistent", updated)
        return updated

    def request_cancel(
        self,
        task_id: str,
        *,
        expected_version: int,
        actor: str,
        reason: str,
        now_ms: int,
    ) -> dict[str, Any]:
        """Persist request, never declare CANCELED or execute/undo any effect.

        This worker-fenced fixture is NOT the future responsive request handler
        (that path must not wait for the executor's lifetime flock).
        """
        if not actor or not reason:
            raise ValueError("cancel actor and reason must be nonempty")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be an exact nonnegative integer")
        with self._write():
            task = self.get_task(task_id)
            if task is None or task["version"] != expected_version:
                raise CASConflictError("cancel expected id/version did not match")
            if task["cancel_requested_at_ms"] is not None:
                return task
            count = self.connection.execute(
                "UPDATE tasks SET cancel_requested_at_ms = ?, cancel_requested_by = ?, "
                "cancel_reason = ?, version = version + 1, updated_at_ms = ? "
                "WHERE id = ? AND version = ? AND cancel_requested_at_ms IS NULL",
                (now_ms, actor, reason, now_ms, task_id, expected_version),
            ).rowcount
            if count != 1:
                raise CASConflictError("cancel CAS did not affect exactly one row")
            self._event(task_id, "draft.task.cancel_requested", {"reason": reason}, now_ms)
            updated = self.get_task(task_id)
            assert updated is not None
        return updated


_SHARED_GUARD_ORDER = (
    "source_state_matches",
    "version_matches",
    "target_state_matches",
    "one_row_cas",
    "event_same_transaction",
    "relationships_consistent",
    "terminal_history_preserved",
    "fenced_commit",
)
_SHARED_EDGE_GUARDS = frozenset(_SHARED_GUARD_ORDER)
_TRANSACTION_GUARDS = _SHARED_EDGE_GUARDS - {"relationships_consistent"}

# Static projection: no YAML parser or project-path dependency at runtime.
# Contract tests compare every guard/edge, including order, in both directions.
_TASK_DOMAIN_GUARDS = {
    ("CREATED", "READY"): ("valid_task_spec", "active_plan_bound"),
    ("CREATED", "FAILED"): ("failure_safe", "no_started_effect"),
    ("CREATED", "CANCELED"): ("cancellation_complete", "no_started_effect"),
    ("READY", "RUNNING"): ("active_plan_bound", "can_start_work", "k0_execution_slot_free"),
    ("READY", "BLOCKED"): ("block_safe",),
    ("READY", "FAILED"): ("failure_safe",),
    ("READY", "CANCELED"): ("cancellation_complete",),
    ("RUNNING", "VERIFYING"): ("result_durable", "active_plan_bound"),
    ("RUNNING", "NEEDS_RECONCILIATION"): ("reconciliation_needed", "observation_bound"),
    ("RUNNING", "RETRY_WAIT"): ("retry_safe", "reconciled_all"),
    ("RUNNING", "BLOCKED"): ("block_safe", "observation_bound", "decision_block", "block_pair"),
    ("RUNNING", "FAILED"): ("failure_safe",),
    ("RUNNING", "CANCELED"): ("cancellation_complete",),
    ("VERIFYING", "SUCCEEDED"): (
        "active_plan_bound",
        "required_steps_completed",
        "all_effects_verified",
        "no_active_attempt",
        "effects_settled",
        "verification_evidence",
        "cancel_absent",
    ),
    ("VERIFYING", "NEEDS_RECONCILIATION"): ("reconciliation_needed", "observation_bound"),
    ("VERIFYING", "RETRY_WAIT"): ("retry_safe", "reconciled_all"),
    ("VERIFYING", "BLOCKED"): ("block_safe", "observation_bound", "decision_block", "block_pair"),
    ("VERIFYING", "FAILED"): ("failure_safe",),
    ("VERIFYING", "CANCELED"): ("cancellation_complete",),
    ("NEEDS_RECONCILIATION", "VERIFYING"): ("verify_reconciled", "reconciled_all"),
    ("NEEDS_RECONCILIATION", "READY"): ("retry_safe", "reconciled_all"),
    ("NEEDS_RECONCILIATION", "BLOCKED"): (
        "block_safe",
        "observation_bound",
        "decision_block",
        "block_pair",
    ),
    ("NEEDS_RECONCILIATION", "FAILED"): (
        "failure_safe",
        "observation_bound",
        "decision_fail",
        "fail_decision_policy_open",
    ),
    ("NEEDS_RECONCILIATION", "CANCELED"): ("cancellation_complete",),
    ("RETRY_WAIT", "READY"): (
        "retry_safe",
        "reconciled_all",
        "retry_schedule_due",
        "active_plan_bound",
    ),
    ("RETRY_WAIT", "BLOCKED"): ("block_safe",),
    ("RETRY_WAIT", "FAILED"): ("failure_safe",),
    ("RETRY_WAIT", "CANCELED"): ("cancellation_complete",),
    ("BLOCKED", "READY"): (
        "unblock_authorized",
        "active_plan_bound",
        "effects_settled",
        "no_active_attempt",
        "cancel_absent",
        "emergency_stop_inactive",
    ),
    ("BLOCKED", "FAILED"): ("failure_safe",),
    ("BLOCKED", "CANCELED"): ("cancellation_complete",),
}
TASK_GUARDS = {edge: _SHARED_GUARD_ORDER + guards for edge, guards in _TASK_DOMAIN_GUARDS.items()}
TASK_EDGES = frozenset(TASK_GUARDS)
