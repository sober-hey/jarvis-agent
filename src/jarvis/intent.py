"""K0-011 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

Offline in-memory Repository fixtures only. No publisher or automatic retry is
registered. Candidate identities, Attempt terminal labels and event result/error
carriers remain DRAFT_PENDING_DP; no schema/ADR is amended by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from jarvis.repository import CASConflictError, GuardRejectedError, RepositoryError, TaskRepository

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


class DuplicateIntentError(RepositoryError):
    """A stable operation_key or Step already has its original Intent."""


@dataclass(frozen=True)
class Attempt:
    """One actual execution start; result/error are projected from audit events.

    DRAFT_FINISHED is a fixture-only closing label, NOT an approved lifecycle enum.
    started_at/finished_at are integer milliseconds, not wall-clock ordering authority.
    """

    id: str
    step_id: str
    operation_id: str
    attempt_no: int
    worker_epoch: int
    state: str
    started_at: int
    finished_at: int | None
    result: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Reconciliation:
    operation_id: str
    observation: str
    decision: str
    reason_code: str
    evidence: dict[str, Any]
    schema_version: int = 1


def _millis(value: int) -> None:
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("exact nonnegative SQLite integer required")


def _signature(item: os.stat_result) -> tuple[int, int, int, int, int]:
    return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns


def _file_evidence(root: Path, relative: str) -> tuple[str, int | None, str | None]:
    """Read-only, fd-relative NOFOLLOW probe; only a missing final leaf is ABSENT.

    Root must be supplied by a trusted fixture bootstrap, stable and exclusively
    controlled. This is not a production filesystem sandbox/publish arbiter.
    """
    descriptors: list[int] = []
    try:
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        parts = relative.split("/")
        for part in parts[:-1]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=current)
        except FileNotFoundError:
            return "ABSENT", None, None
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink < 1:
            return "UNKNOWN", None, None
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, 1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(fd)
        named = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        if _signature(before) != _signature(after) or _signature(after) != _signature(named):
            return "UNKNOWN", None, None
        return "PRESENT", size, digest.hexdigest()
    except OSError:
        return "UNKNOWN", None, None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class IntentRepository:
    """Offline extension composed with K0-005's transaction/flock/epoch fences.

    It never commits caller transactions or changes tables. Result/error and full
    immutable arguments use existing events.payload_json as an explicitly DRAFT
    carrier (D-13/OPEN-10 remain open). No in-memory cache substitutes for records.
    All external guard callbacks are synthetic fixture seams, fail-closed by default.
    """

    def __init__(self, repository: TaskRepository) -> None:
        self.repository = repository
        repository._validate_connection(repository.connection)  # pyright: ignore[reportPrivateUsage]

    def _step(self, intent: OperationIntent) -> dict[str, Any]:
        cursor = self.repository.connection.execute(
            "SELECT * FROM task_steps WHERE id = ?", (intent.step_id,)
        )
        row = cursor.fetchone()
        if row is None:
            raise GuardRejectedError("Step missing")
        step = dict(zip((column[0] for column in cursor.description), row, strict=True))
        if (step["task_id"], step["logical_effect_id"]) != (
            intent.task_id,
            intent.logical_effect_id,
        ):
            raise GuardRejectedError("original Intent/Step scope mismatch")
        return step

    def _task(self, intent: OperationIntent) -> dict[str, Any]:
        task = self.repository.get_task(intent.task_id)
        if task is None:
            raise GuardRejectedError("parent Task missing")
        return task

    def _external(self, name: str, intent: OperationIntent) -> None:
        self.repository._check_guard(  # pyright: ignore[reportPrivateUsage]
            name, {**self._task(intent), **self._binding(intent)}
        )

    @staticmethod
    def _binding(intent: OperationIntent) -> dict[str, Any]:
        return {**asdict(intent)}

    def _no_active(self, intent: OperationIntent) -> bool:
        return (
            self.repository.connection.execute(
                "SELECT 1 FROM attempts WHERE step_id = ? "
                "AND (state = 'ACTIVE' OR finished_at_ms IS NULL) LIMIT 1",
                (intent.step_id,),
            ).fetchone()
            is None
        )

    def _start_allowed(self, intent: OperationIntent) -> None:
        task = self._task(intent)
        self.repository._check_guard("can_start_work", task)  # pyright: ignore[reportPrivateUsage]
        if task["state"] != "RUNNING":
            raise GuardRejectedError("parent Task must be RUNNING")
        step = self._step(intent)
        if (
            self.repository.connection.execute(
                "SELECT 1 FROM task_plans WHERE id = ? AND task_id = ? AND state = 'ACTIVE'",
                (step["plan_id"], intent.task_id),
            ).fetchone()
            is None
            or task["active_plan_id"] != step["plan_id"]
        ):
            raise GuardRejectedError("Step is not in the active Plan")

    def _event(
        self, intent: OperationIntent, event_type: str, payload: Mapping[str, Any], now_ms: int
    ) -> None:
        self.repository.connection.execute(
            "INSERT INTO events (task_id,step_id,event_type,actor_type,actor_id,payload_json,"
            "created_at_ms) VALUES (?, ?, ?, 'draft_worker', ?, ?, ?)",
            (
                intent.task_id,
                intent.step_id,
                event_type,
                f"fixture-epoch-{self.repository.worker_epoch}",
                json.dumps({**self._binding(intent), **payload}, allow_nan=False, sort_keys=True),
                now_ms,
            ),
        )

    def _intent_transition(self, intent: OperationIntent, source: str, target: str) -> None:
        changed = self.repository.connection.execute(
            "UPDATE operation_intents SET state=? WHERE operation_id=? AND state=?",
            (target, intent.operation_id, source),
        ).rowcount
        if changed != 1:
            raise CASConflictError("Intent source-state CAS failed")
        if self.repository.connection.execute(
            "SELECT state FROM operation_intents WHERE operation_id=?", (intent.operation_id,)
        ).fetchone() != (target,):
            raise CASConflictError("Intent post-write state mismatch")

    def _transition(
        self, intent: OperationIntent, source: str, target: str, version: int, now_ms: int
    ) -> None:
        _millis(version)
        step = self._step(intent)
        if (step["state"], step["version"]) != (source, version):
            raise CASConflictError("expected Step id/source/version did not match")
        guards = STEP_DOMAIN_GUARDS.get((source, target))
        if guards is None:
            raise GuardRejectedError("edge outside implemented draft subset")
        for name in guards:
            # Local evidence checks below cannot be overridden by callback=True.
            self._external(name, intent)
        changed = self.repository.connection.execute(
            "UPDATE task_steps SET state = ?, version = version + 1 "
            "WHERE id = ? AND state = ? AND version = ?",
            (target, intent.step_id, source, version),
        ).rowcount
        if changed != 1:
            raise CASConflictError("Step CAS did not affect exactly one row")
        self.repository.connection.execute(
            "INSERT INTO events (task_id,step_id,event_type,actor_type,actor_id,payload_json,"
            "created_at_ms,from_state,to_state,from_version,to_version) "
            "VALUES (?,?,'draft.step.transition','draft_worker',?,?,?,?,?,?,?)",
            (
                intent.task_id,
                intent.step_id,
                f"fixture-epoch-{self.repository.worker_epoch}",
                json.dumps(self._binding(intent), allow_nan=False, sort_keys=True),
                now_ms,
                source,
                target,
                version,
                version + 1,
            ),
        )
        if (self._step(intent)["state"], self._step(intent)["version"]) != (target, version + 1):
            raise CASConflictError("post-write Step state/version mismatch")
        # The parent method checks relationships after the WHOLE graph mutation,
        # not a transient Step/Intent/Attempt combination within this transaction.

    def create_intent(self, intent: OperationIntent, *, expected_version: int, now_ms: int) -> None:
        """Commit Intent + FileWriteIntent + CLAIMED→PREPARING atomically.

        A separate start_attempt transaction requires this prior commit (S09).
        There is deliberately NO callback that can execute a side effect here.
        """
        _millis(now_ms)
        # Reconstruct, so dataclass replacement/tampering cannot supply arbitrary derived keys.
        checked = OperationIntent(
            **{
                k: v
                for k, v in asdict(intent).items()
                if k not in {"operation_id", "operation_key", "arguments_digest"}
            }
        )
        if checked != intent:
            raise GuardRejectedError("invalid derived Intent identity")
        with self.repository._write():  # pyright: ignore[reportPrivateUsage]
            db = self.repository.connection
            if db.execute(
                "SELECT 1 FROM operation_intents WHERE operation_key = ? OR step_id = ?",
                (intent.operation_key, intent.step_id),
            ).fetchone():
                raise DuplicateIntentError("original operation_key/Step already exists")
            self._start_allowed(intent)
            if not self._no_active(intent):
                raise GuardRejectedError("active Attempt prevents preparation")
            self._transition(intent, "CLAIMED", "PREPARING", expected_version, now_ms)
            db.execute(
                "INSERT INTO operation_intents (operation_id,step_id,operation_key,operation_type,"
                "tool_name,tool_version,arguments_digest,state,created_at_ms) "
                "VALUES (?,?,?,?,'filesystem.publisher',?,?,'PREPARED',?)",
                (
                    intent.operation_id,
                    intent.step_id,
                    intent.operation_key,
                    intent.intended_action,
                    intent.tool_version,
                    intent.arguments_digest,
                    now_ms,
                ),
            )
            db.execute(
                "INSERT INTO file_write_intents VALUES (?,?,?,?,?,?)",
                (
                    intent.operation_id,
                    intent.input_snapshot_sha256,
                    intent.prepared_path,
                    intent.target_path,
                    intent.expected_sha256,
                    intent.expected_size,
                ),
            )
            self._event(intent, "draft.intent.created", {}, now_ms)
            self._external("relationships_consistent", intent)

    def get_intent(self, operation_id: str) -> OperationIntent:
        row = self.repository.connection.execute(
            "SELECT i.step_id,i.operation_key,i.operation_type,i.tool_version,i.arguments_digest,"
            "s.task_id,s.logical_effect_id,f.target_relpath,f.prepared_relpath,f.expected_sha256,"
            "f.expected_size,f.input_snapshot_sha256 FROM operation_intents i "
            "JOIN task_steps s ON s.id=i.step_id JOIN file_write_intents f "
            "ON f.operation_id=i.operation_id WHERE i.operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise GuardRejectedError("committed original Intent/FileWriteIntent missing")
        intent = OperationIntent(
            task_id=row[5],
            logical_effect_id=row[6],
            step_id=row[0],
            target_path=row[7],
            prepared_path=row[8],
            expected_sha256=row[9],
            expected_size=row[10],
            input_snapshot_sha256=row[11],
            intended_action=row[2],
            tool_version=row[3],
        )
        if (intent.operation_id, intent.operation_key, intent.arguments_digest) != (
            operation_id,
            row[1],
            row[4],
        ):
            raise GuardRejectedError("original Intent identity/arguments changed")
        return intent

    def start_attempt(self, operation_id: str, *, expected_version: int, now_ms: int) -> Attempt:
        _millis(now_ms)
        # Read only a PREVIOUSLY committed Intent, never one pending in caller's transaction.
        if self.repository.connection.in_transaction:
            raise RepositoryError("Intent must have committed before Attempt start")
        self.get_intent(operation_id)
        with self.repository._write():  # pyright: ignore[reportPrivateUsage]
            intent = self.get_intent(operation_id)
            self._start_allowed(intent)
            db = self.repository.connection
            if db.execute(
                "SELECT state FROM operation_intents WHERE operation_id=?", (operation_id,)
            ).fetchone() != ("PREPARED",) or not self._no_active(intent):
                raise GuardRejectedError("Intent not PREPARED or Attempt still unresolved")
            attempt_no = db.execute(
                "SELECT COALESCE(MAX(attempt_no),0)+1 FROM attempts WHERE step_id=?",
                (intent.step_id,),
            ).fetchone()[0]
            attempt_id = "attempt-" + _digest(
                "jarvis.k0.attempt.draft.v1", [operation_id, attempt_no]
            )
            db.execute(
                "INSERT INTO attempts VALUES (?,?,?,?,?,'ACTIVE',?,NULL)",
                (
                    attempt_id,
                    intent.step_id,
                    operation_id,
                    attempt_no,
                    self.repository.worker_epoch,
                    now_ms,
                ),
            )
            self._transition(intent, "PREPARING", "EXECUTING", expected_version, now_ms)
            self._intent_transition(intent, "PREPARED", "PUBLISHING")
            self._event(intent, "draft.attempt.started", {"attempt_id": attempt_id}, now_ms)
            self._external("relationships_consistent", intent)
            attempt = self.get_attempt(attempt_id)
        return attempt

    def get_attempt(self, attempt_id: str) -> Attempt:
        row = self.repository.connection.execute(
            "SELECT * FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise GuardRejectedError("Attempt missing")
        records = self.repository.connection.execute(
            "SELECT payload_json FROM events WHERE step_id=? "
            "AND event_type='draft.attempt.finished' "
            "AND json_extract(payload_json,'$.attempt_id')=?",
            (row[1], attempt_id),
        ).fetchall()
        if len(records) > 1 or (row[7] is not None and len(records) != 1):
            raise GuardRejectedError("Attempt result evidence missing/ambiguous")
        payload: dict[str, Any] = json.loads(records[0][0]) if records else {}
        if payload and payload.get("operation_id") != row[2]:
            raise GuardRejectedError("Attempt result has wrong operation binding")
        return Attempt(*row, result=payload.get("result"), error=payload.get("error"))

    def _close_attempt(
        self, intent: OperationIntent, attempt: Attempt, result: str, error: str | None, now_ms: int
    ) -> None:
        if attempt.state != "ACTIVE" or attempt.finished_at is not None:
            raise GuardRejectedError("Attempt already closed")
        if now_ms < attempt.started_at:
            raise ValueError("finished_at must not precede started_at")
        changed = self.repository.connection.execute(
            "UPDATE attempts SET state=?,finished_at_ms=? "
            "WHERE id=? AND state='ACTIVE' AND finished_at_ms IS NULL",
            (result, now_ms, attempt.id),
        ).rowcount
        if changed != 1:
            raise CASConflictError("Attempt finish CAS failed")
        self._event(
            intent,
            "draft.attempt.finished",
            {"attempt_id": attempt.id, "result": result, "error": error},
            now_ms,
        )

    def finish_attempt(
        self, attempt_id: str, *, result: str, error: str | None, expected_version: int, now_ms: int
    ) -> Attempt:
        _millis(now_ms)
        if result not in {"SUCCESS", "ERROR", "UNKNOWN"}:
            raise ValueError("DRAFT result must be SUCCESS, ERROR or UNKNOWN")
        if error is not None:
            _text(error)
        with self.repository._write():  # pyright: ignore[reportPrivateUsage]
            attempt = self.get_attempt(attempt_id)
            if attempt.worker_epoch != self.repository.worker_epoch:
                raise GuardRejectedError("cannot submit an older worker's Attempt result")
            intent = self.get_intent(attempt.operation_id)
            self._close_attempt(intent, attempt, result, error, now_ms)
            target = "VERIFYING" if result == "SUCCESS" else "NEEDS_RECONCILIATION"
            self._transition(intent, "EXECUTING", target, expected_version, now_ms)
            self._intent_transition(intent, "PUBLISHING", "NEEDS_RECONCILIATION")
            finished = self.get_attempt(attempt_id)
            self._external("relationships_consistent", intent)
        return finished

    def recover_unknown_attempt(
        self, attempt_id: str, *, expected_version: int, now_ms: int
    ) -> Attempt:
        """Close ONLY an older epoch as UNKNOWN; never infer its actual result.

        Requires the current repository flock/epoch and a trusted settled-old-worker
        proof. Epoch alone is not proof that a detached external publisher stopped.
        K0-013 still owns production recovery orchestration.
        """
        _millis(now_ms)
        with self.repository._write():  # pyright: ignore[reportPrivateUsage]
            attempt = self.get_attempt(attempt_id)
            if attempt.worker_epoch >= self.repository.worker_epoch:
                raise GuardRejectedError("only a strictly older worker Attempt can be recovered")
            intent = self.get_intent(attempt.operation_id)
            self._external("old_worker_effects_settled", intent)
            self._close_attempt(
                intent,
                attempt,
                "UNKNOWN",
                "worker epoch lost; actual outcome requires reconciliation",
                now_ms,
            )
            self._transition(intent, "EXECUTING", "NEEDS_RECONCILIATION", expected_version, now_ms)
            self._intent_transition(intent, "PUBLISHING", "NEEDS_RECONCILIATION")
            recovered = self.get_attempt(attempt_id)
            self._external("relationships_consistent", intent)
        return recovered

    def unknown_intents(self) -> tuple[OperationIntent, ...]:
        rows = self.repository.connection.execute(
            "SELECT operation_id FROM operation_intents "
            "WHERE state IN ('PUBLISHING','NEEDS_RECONCILIATION') "
            "ORDER BY created_at_ms,operation_id"
        ).fetchall()
        return tuple(self.get_intent(row[0]) for row in rows)

    @staticmethod
    def _observe(intent: OperationIntent, root: Path) -> str:
        state, size, digest = _file_evidence(root, intent.target_path)
        if state == "ABSENT":
            return "TARGET_ABSENT"
        if state != "PRESENT":
            return "INSUFFICIENT_EVIDENCE"
        return (
            "EXPECTED_EFFECT_PRESENT"
            if (size, digest) == (intent.expected_size, intent.expected_sha256)
            else "CONFLICTING_EFFECT_PRESENT"
        )

    def _retry_safe(self, intent: OperationIntent, root: Path) -> None:
        self._start_allowed(intent)
        if not self._no_active(intent):
            raise GuardRejectedError("Attempt not settled")
        if self._observe(intent, root) != "TARGET_ABSENT":
            raise GuardRejectedError("target not proven absent")
        if _file_evidence(root, intent.prepared_path) != (
            "PRESENT",
            intent.expected_size,
            intent.expected_sha256,
        ):
            raise GuardRejectedError("original prepared file not intact")
        for guard in ("effects_settled", "retry_policy_proved", "retry_permitted"):
            self._external(guard, intent)

    def reconcile(
        self, operation_id: str, *, root: Path, expected_version: int, now_ms: int
    ) -> Reconciliation:
        """Observe BEFORE deciding. Never write/delete/overwrite target/prepared files.

        Missing target alone never proves safe retry. Conflicting/unknown evidence
        blocks; matching bytes route to VERIFYING, not straight to COMPLETED.
        """
        _millis(now_ms)
        with self.repository._write():  # pyright: ignore[reportPrivateUsage]
            intent = self.get_intent(operation_id)
            step = self._step(intent)
            if step["state"] != "NEEDS_RECONCILIATION":
                raise GuardRejectedError("Step must enter reconciliation first")
            if not self._no_active(intent):
                raise GuardRejectedError("active/unclosed Attempt requires separate recovery")
            observation = self._observe(intent, root)
            decision, target = "BLOCK", "BLOCKED"
            if observation == "EXPECTED_EFFECT_PRESENT":
                decision, target = "VERIFY", "VERIFYING"
            elif observation == "TARGET_ABSENT":
                try:
                    self._retry_safe(intent, root)
                except GuardRejectedError:
                    pass  # absence without proof is a conservative BLOCK, never retry
                else:
                    decision, target = "RETRY_SAFE", "RETRY_WAIT"
            report = Reconciliation(
                operation_id,
                observation,
                decision,
                "draft-read-only-observation",
                {
                    **self._binding(intent),
                    "observed_at_ms": now_ms,
                    "fixture_root": str(root),
                    "worker_epoch": self.repository.worker_epoch,
                },
            )
            self._event(intent, "draft.intent.reconciled", asdict(report), now_ms)
            self._transition(intent, "NEEDS_RECONCILIATION", target, expected_version, now_ms)
            if target == "BLOCKED":
                self._intent_transition(intent, "NEEDS_RECONCILIATION", "BLOCKED")
            self._external("relationships_consistent", intent)
        return report

    def prepare_retry(
        self, operation_id: str, *, root: Path, expected_version: int, now_ms: int
    ) -> None:
        """Draft schedule round trip, retaining original key/arguments and history."""
        _millis(now_ms)
        with self.repository._write():  # pyright: ignore[reportPrivateUsage]
            intent = self.get_intent(operation_id)
            self._retry_safe(intent, root)  # recheck current files, not just the old decision
            self._transition(intent, "RETRY_WAIT", "PENDING", expected_version, now_ms)
            self._transition(intent, "PENDING", "CLAIMED", expected_version + 1, now_ms)
            self._transition(intent, "CLAIMED", "PREPARING", expected_version + 2, now_ms)
            self._intent_transition(intent, "NEEDS_RECONCILIATION", "PREPARED")
            self._external("relationships_consistent", intent)

    def complete_verified(
        self, operation_id: str, *, root: Path, expected_version: int, now_ms: int
    ) -> None:
        """Independent verification seam + actual byte readback; no existence shortcut."""
        _millis(now_ms)
        with self.repository._write():  # pyright: ignore[reportPrivateUsage]
            intent = self.get_intent(operation_id)
            if self._observe(intent, root) != "EXPECTED_EFFECT_PRESENT" or not self._no_active(
                intent
            ):
                raise GuardRejectedError(
                    "effect not independently verifiable or Attempt still active"
                )
            self._external("verification_passed", intent)
            self._external("verification_evidence", intent)
            self._external("effects_settled", intent)
            self._intent_transition(intent, "NEEDS_RECONCILIATION", "VERIFIED")
            self._transition(intent, "VERIFYING", "COMPLETED", expected_version, now_ms)
            self._event(intent, "draft.intent.verified", {}, now_ms)
            self._external("relationships_consistent", intent)


# Exact domain-guard projection of the implemented Step edges. Transaction,
# row-CAS, graph postcheck and event same-transaction are provided above + K0-005.
# Tests compare this subset to the catalogue; no unimplemented edge is admitted.
STEP_DOMAIN_GUARDS = {
    ("CLAIMED", "PREPARING"): (
        "step_in_active_plan",
        "task_running",
        "can_start_work",
        "preconditions_passed",
    ),
    ("PREPARING", "EXECUTING"): (
        "step_in_active_plan",
        "task_running",
        "can_start_work",
        "preconditions_passed",
        "intent_committed",
        "intent_prepared",
        "prepared_durable",
        "attempt_bound",
        "publish_start_arbitrated",
    ),
    ("EXECUTING", "VERIFYING"): ("result_durable",),
    ("EXECUTING", "NEEDS_RECONCILIATION"): ("reconciliation_needed", "observation_bound"),
    ("NEEDS_RECONCILIATION", "VERIFYING"): ("verify_reconciled",),
    ("NEEDS_RECONCILIATION", "RETRY_WAIT"): ("retry_safe",),
    ("NEEDS_RECONCILIATION", "BLOCKED"): (
        "block_safe",
        "observation_bound",
        "decision_block",
        "block_pair",
    ),
    ("RETRY_WAIT", "PENDING"): ("retry_safe", "retry_schedule_due", "step_in_active_plan"),
    ("PENDING", "CLAIMED"): (
        "step_in_active_plan",
        "task_running",
        "can_start_work",
        "no_active_attempt",
        "k0_execution_slot_free",
    ),
    ("VERIFYING", "COMPLETED"): (
        "verification_passed",
        "verification_evidence",
        "intent_verified",
        "effects_settled",
        "no_active_attempt",
    ),
}


def _digest(namespace: str, payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            {"namespace": namespace, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _text(value: str) -> None:
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError("nonempty UTF-8 text without NUL required")
    value.encode("utf-8", errors="strict")


def _relative_path(value: str) -> None:
    _text(value)
    if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("exact relative POSIX path without dot/empty segments required")


@dataclass(frozen=True)
class OperationIntent:
    """Original immutable effect, shared by all Attempts, not an execution grant.

    target_path/prepared_path map to file_write_intents.*_relpath. Keys match the
    K0-006 candidate hash of (task_id, logical_effect_id), never time/Attempt/epoch.
    Different arguments for that identity must fail, never get a replacement key.
    """

    task_id: str
    logical_effect_id: str
    step_id: str
    target_path: str
    prepared_path: str
    expected_sha256: str
    expected_size: int
    input_snapshot_sha256: str
    intended_action: Literal["file.publish.no_clobber"] = "file.publish.no_clobber"
    tool_version: str = "k0-011-draft-v1"
    operation_key: str = field(init=False)
    operation_id: str = field(init=False)
    arguments_digest: str = field(init=False)

    def __post_init__(self) -> None:
        for value in (self.task_id, self.logical_effect_id, self.step_id, self.tool_version):
            _text(value)
        _relative_path(self.target_path)
        _relative_path(self.prepared_path)
        if self.target_path == self.prepared_path:
            raise ValueError("prepared and target paths must differ")
        for value in (self.expected_sha256, self.input_snapshot_sha256):
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("exact lowercase sha256 required")
        if type(self.expected_size) is not int or not 0 <= self.expected_size < 2**63:
            raise ValueError("expected_size must be a nonnegative SQLite integer")
        if self.intended_action != "file.publish.no_clobber":
            raise ValueError("only file.publish.no_clobber is defined for K0")
        key = _digest("jarvis.k0.operation.draft.v1", [self.task_id, self.logical_effect_id])
        object.__setattr__(self, "operation_key", key)
        object.__setattr__(self, "operation_id", "operation-" + key)
        object.__setattr__(
            self,
            "arguments_digest",
            _digest(
                "jarvis.k0.intent.arguments.draft.v1",
                {
                    "intended_action": self.intended_action,
                    "target_path": self.target_path,
                    "prepared_path": self.prepared_path,
                    "expected_sha256": self.expected_sha256,
                    "expected_size": self.expected_size,
                    "input_snapshot_sha256": self.input_snapshot_sha256,
                    "tool_version": self.tool_version,
                },
            ),
        )
