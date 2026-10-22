"""K0-013 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

Offline in-memory fixtures only; no Worker startup hook or publisher is registered.
UNKNOWN means an unresolved outcome, not a new database enum. COMPLETED and
SAFE_TO_RETRY below are adapter results: persisted states remain Step.COMPLETED /
Intent.VERIFIED and Step.RETRY_WAIT. Missing targets never prove non-execution.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jarvis.intent import IntentRepository, OperationIntent
from jarvis.repository import (
    DraftOnlyError,
    GuardRejectedError,
    RepositoryError,
    StaleEpochError,
    TaskRepository,
)
from jarvis.worker import Worker

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


@dataclass(frozen=True)
class ReconcileResult:
    """Candidate facade labels, not approved Intent/Attempt lifecycle values."""

    operation_id: str
    status: Literal["COMPLETED", "SAFE_TO_RETRY", "NEEDS_VERIFICATION", "BLOCKED"]
    observation: str


class Reconciler:
    """Orchestrate K0-011's readback, CAS, epoch fences and independent verification.

    The supplied repository must belong to the same acquired Worker/connection.
    Its existing domain adapter remains fail-closed; this class injects NO grants.
    root must be a stable, trusted, exclusively controlled fixture root. All file
    probes are inherited fd-relative NOFOLLOW reads, never Path.exists shortcuts.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        repository: TaskRepository,
        root: Path,
        draft_fixture: bool = False,
    ) -> None:
        if draft_fixture is not True:
            raise DraftOnlyError("explicit offline draft_fixture=True required")
        if repository.connection is not connection:
            raise RepositoryError("Reconciler connection must match its repository")
        self.connection = connection
        self.repository = repository
        self.intents = IntentRepository(repository)
        self.root = Path(root)
        self._assert_current()

    def _assert_current(self) -> None:
        self.repository._validate_connection(self.connection)  # pyright: ignore[reportPrivateUsage]
        if self.connection.in_transaction:
            raise RepositoryError("caller transaction is not allowed")
        self.repository.worker_lock.assert_held()
        if self.connection.execute(
            "SELECT worker_epoch FROM system_control WHERE singleton_id=1"
        ).fetchone() != (self.repository.worker_epoch,):
            raise StaleEpochError("recovery worker epoch is no longer current")

    def unknown_intents(self) -> tuple[OperationIntent, ...]:
        """Inventory unknown effects, excluding already assessed RETRY_WAIT rows.

        IntentRepository also returns NEEDS_RECONCILIATION Intents whose Step has
        moved to RETRY_WAIT. They are not unresolved outcomes; eligibility must
        nevertheless recheck their files and retry guards on every recovery scan.
        VERIFYING remains visible until independent verification actually passes.
        """
        self._assert_current()
        rows = self.connection.execute(
            "SELECT i.operation_id FROM operation_intents i "
            "JOIN task_steps s ON s.id=i.step_id "
            "WHERE i.state IN ('PUBLISHING','NEEDS_RECONCILIATION') "
            "AND s.state IN ('EXECUTING','NEEDS_RECONCILIATION','VERIFYING') "
            "ORDER BY i.created_at_ms,i.operation_id"
        ).fetchall()
        result = tuple(self.intents.get_intent(row[0]) for row in rows)
        self._assert_current()
        return result

    def reconcile(self, intent_id: str, *, now_ms: int) -> ReconcileResult:
        """Observe, persist a decision, then verify; never execute or retry an effect.

        ACTIVE Attempts need separate recovery first. A matching file only becomes
        COMPLETED after complete_verified re-reads it and all verification/effects
        guards pass. If verification is unavailable, keep VERIFYING, not success.
        """
        self._assert_current()
        intent = self.intents.get_intent(intent_id)
        step = self.intents._step(intent)  # pyright: ignore[reportPrivateUsage]
        if step["state"] == "NEEDS_RECONCILIATION":
            report = self.intents.reconcile(
                intent_id, root=self.root, expected_version=step["version"], now_ms=now_ms
            )
            if report.decision != "VERIFY":
                return ReconcileResult(
                    intent_id,
                    "SAFE_TO_RETRY" if report.decision == "RETRY_SAFE" else "BLOCKED",
                    report.observation,
                )
            step = self.intents._step(intent)  # pyright: ignore[reportPrivateUsage]
        elif step["state"] != "VERIFYING":
            raise GuardRejectedError("Intent must first enter reconciliation or verification")
        try:
            self.intents.complete_verified(
                intent_id, root=self.root, expected_version=step["version"], now_ms=now_ms
            )
        except GuardRejectedError:
            self._assert_current()
            observation = self.intents._observe(intent, self.root)  # pyright: ignore[reportPrivateUsage]
            return ReconcileResult(intent_id, "NEEDS_VERIFICATION", observation)
        return ReconcileResult(intent_id, "COMPLETED", "EXPECTED_EFFECT_PRESENT")


class RecoveryManager:
    """Explicit post-startup recovery; return an advisory inventory, not a grant.

    Sir's safety policy permits a new Worker to take over old tasks, but ownership
    alone never proves that detached old publishers have stopped. Old ACTIVE
    Attempts require K0-011's old_worker_effects_settled proof before closing them
    UNKNOWN. Their original epoch, key and immutable Intent remain unchanged.
    Each Intent transition/audit is independently fenced and committed, so another
    crash may leave a partially scanned inventory that can safely be scanned again.
    """

    def __init__(self, worker: Worker, reconciler: Reconciler) -> None:
        self.worker = worker
        self.reconciler = reconciler
        self.repository = reconciler.repository
        self._assert_bound()

    def _assert_bound(self) -> None:
        self.worker.assert_current_epoch()
        self.reconciler._assert_current()  # pyright: ignore[reportPrivateUsage]
        binding = self.worker.execute(lambda repository: repository)
        if (
            self.worker.connection is not self.reconciler.connection
            or self.worker.epoch != self.repository.worker_epoch
            or binding.worker_lock is not self.repository.worker_lock
        ):
            raise StaleEpochError("RecoveryManager requires the current Worker's repository")

    def _recover_active(self, task_id: str, now_ms: int) -> bool:
        rows = self.reconciler.connection.execute(
            "SELECT a.id,s.version FROM attempts a JOIN task_steps s ON s.id=a.step_id "
            "WHERE s.task_id=? AND (a.state='ACTIVE' OR a.finished_at_ms IS NULL) "
            "ORDER BY a.started_at_ms,a.id",
            (task_id,),
        ).fetchall()
        for attempt_id, version in rows:
            try:
                self.reconciler.intents.recover_unknown_attempt(
                    attempt_id, expected_version=version, now_ms=now_ms
                )
            except GuardRejectedError:
                return False  # current/future epoch or missing old-effect-settled proof
        return True

    def _eligible(self, task_id: str) -> dict[str, Any] | None:
        ledger = self.reconciler.intents
        # Recheck controls and every resolved effect under the SQL epoch barrier.
        # A returned snapshot is NOT permission to skip the later start/publish guards.
        with self.repository._write():  # pyright: ignore[reportPrivateUsage]
            task = self.repository.get_task(task_id)
            if task is None or task["state"] not in {"READY", "RUNNING"}:
                return None
            try:
                self.repository._check_guard("can_start_work", task)  # pyright: ignore[reportPrivateUsage]
                self.repository._check_guard("no_active_attempt", task)  # pyright: ignore[reportPrivateUsage]
                self.repository._check_guard("active_plan_bound", task)  # pyright: ignore[reportPrivateUsage]
                historical = self.reconciler.connection.execute(
                    "SELECT s.state,i.state FROM operation_intents i "
                    "JOIN task_steps s ON s.id=i.step_id WHERE s.task_id=? AND s.plan_id!=?",
                    (task_id, task["active_plan_id"]),
                ).fetchall()
                if any(pair != ("COMPLETED", "VERIFIED") for pair in historical):
                    return None  # A replacement plan cannot hide an older unsettled effect.
                rows = self.reconciler.connection.execute(
                    "SELECT s.state,i.operation_id,i.state FROM task_steps s "
                    "LEFT JOIN operation_intents i ON i.step_id=s.id "
                    "WHERE s.task_id=? AND s.plan_id=? ORDER BY s.ordinal,s.id",
                    (task_id, task["active_plan_id"]),
                ).fetchall()
                for state, operation_id, intent_state in rows:
                    if state not in {"PENDING", "CLAIMED", "PREPARING", "RETRY_WAIT", "COMPLETED"}:
                        return None
                    if operation_id is None:
                        if state not in {"PENDING", "CLAIMED"}:
                            return None
                        continue
                    intent = ledger.get_intent(operation_id)
                    if state == "COMPLETED":
                        if intent_state != "VERIFIED":
                            return None
                        observed = ledger._observe(intent, self.reconciler.root)  # pyright: ignore[reportPrivateUsage]
                        if observed != "EXPECTED_EFFECT_PRESENT":
                            return None
                    elif (state, intent_state) in {
                        ("PREPARING", "PREPARED"),
                        ("RETRY_WAIT", "NEEDS_RECONCILIATION"),
                    }:
                        ledger._retry_safe(intent, self.reconciler.root)  # pyright: ignore[reportPrivateUsage]
                    else:
                        return None
            except GuardRejectedError:
                return None
        return task

    def recover_tasks(self, *, now_ms: int) -> tuple[dict[str, Any], ...]:
        """Scan all unfinished Tasks; assess RUNNING as the draft IN_PROGRESS state.

        Blocked/canceled/stopped tasks are never scheduled. No Task state, Attempt
        epoch, operation key, prepared file or external target is overwritten here.
        No automatic Attempt start is performed, including for SAFE_TO_RETRY.
        """
        self._assert_bound()
        candidates: list[dict[str, Any]] = []
        for task in self.worker.recover_tasks():
            self._assert_bound()
            settled = self._recover_active(task["id"], now_ms)
            if settled:
                for intent in self.reconciler.unknown_intents():
                    if intent.task_id == task["id"]:
                        self.reconciler.reconcile(intent.operation_id, now_ms=now_ms)
            candidate = self._eligible(task["id"]) if settled else None
            self.repository.append_event(
                task["id"],
                "draft.recovery.assessed",
                {
                    "task_id": task["id"],
                    "worker_epoch": self.repository.worker_epoch,
                    "eligible": candidate is not None,
                    "active_attempts_settled": settled,
                    "advisory_only": True,
                },
                now_ms=now_ms,
            )
            if candidate is not None:
                candidates.append(candidate)
        self._assert_bound()
        return tuple(candidates)
