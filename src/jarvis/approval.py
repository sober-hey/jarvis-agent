"""K1-007 DRAFT; NOT_APPROVED / NOT_FROZEN, runtime_enabled=False.

Standalone, explicitly opted-in SQLite fixtures only; not wired into Repository,
Intent, Worker, CLI or packaging/. Records are advisory, NOT execution grants.
TaskRepository's flock/epoch/graph transaction cannot be bypassed by this module.

Notification and identity verification are injected trusted-bootstrap seams. The
imessage callback must return True only after checking its transport receipt;
there is deliberately no guessed recipient, osascript, credential or live send.
A channel name, model output or CLI approver string is not a trusted identity.
ADR-013's independent identity boundary and atomic one-use execution consumption
remain unimplemented; configuring a fixture verifier does NOT approve that ADR.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


class ApprovalError(RuntimeError):
    """Approval request rejected; never permission to perform an effect."""


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ApprovalStatus(StrEnum):
    APPROVED = "APPROVED"
    RECORDED = "RECORDED"
    PENDING = "PENDING"


def _text(value: str) -> None:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError("nonempty text without NUL required")
    value.encode("utf-8", errors="strict")


def _timestamp(value: int) -> None:
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("timestamp must be an exact nonnegative SQLite integer")


def _canonical(arguments: Mapping[str, Any]) -> str:
    def check(value: Any) -> None:
        if isinstance(value, dict):
            mapping = cast(dict[object, Any], value)
            if any(type(key) is not str for key in mapping):
                raise ValueError("canonical argument keys must be strings")
            for item in mapping.values():
                check(item)
        elif isinstance(value, (list, tuple)):
            for item in cast(list[Any] | tuple[Any, ...], value):
                check(item)

    copied = dict(arguments)
    check(copied)
    return json.dumps(copied, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ApprovalPolicy:
    """Sparse policy from trusted configuration, never from model arguments.

    No-clobber local publication is LOW, general publication/cancellation MEDIUM;
    deletion, external calls, production and unknown operations are HIGH.
    Mandatory safety floors cannot be weakened via configuration.
    """

    _DEFAULTS = {
        "file.publish.no_clobber": RiskLevel.LOW,
        "file.publish": RiskLevel.MEDIUM,
        "task.cancel": RiskLevel.MEDIUM,
        "operation.cancel": RiskLevel.MEDIUM,
        "file.delete": RiskLevel.HIGH,
        "external.call": RiskLevel.HIGH,
        "production.change": RiskLevel.HIGH,
    }
    _HIGH = frozenset({"file.delete", "external.call", "production.change"})

    def __init__(self, rules: Mapping[str, RiskLevel] | None = None) -> None:
        self._rules = dict(self._DEFAULTS)
        for operation, risk in (rules or {}).items():
            _text(operation)
            if type(risk) is not RiskLevel:
                raise ValueError("policy values must be RiskLevel")
            if operation in self._HIGH and risk is not RiskLevel.HIGH:
                raise ValueError(f"{operation} requires HIGH risk")
            self._rules[operation] = risk

    def classify(
        self, operation_type: str, arguments: Mapping[str, Any] | None = None
    ) -> RiskLevel:
        _text(operation_type)
        if arguments and arguments.get("environment") == "production":
            return RiskLevel.HIGH
        return self._rules.get(operation_type, RiskLevel.HIGH)


@dataclass(frozen=True)
class ApprovalRecord:
    operation_id: str
    risk_level: RiskLevel
    approved_by: str | None
    timestamp: int
    reason: str
    status: ApprovalStatus

    @property
    def may_proceed(self) -> bool:
        """Advisory decision only; does not arbitrate or execute an Intent."""
        return self.status is not ApprovalStatus.PENDING


@dataclass(frozen=True)
class ApprovalRequest:
    operation_id: str
    operation_type: str
    canonical_arguments: str
    binding_digest: str
    risk_level: RiskLevel
    created_at: int
    expires_at: int


@dataclass(frozen=True)
class ApprovalNotification:
    """Structured intent, not hard-coded human-facing wording or an approver."""

    operation_id: str
    binding_digest: str
    risk_level: RiskLevel
    expires_at: int
    channel: str = "imessage"


IMessageNotifier = Callable[[ApprovalNotification], bool]
ConfirmationVerifier = Callable[[ApprovalRequest, object], str | None]


class ApprovalStore:
    """Own a separate file-backed fixture database; never migrate the core DB.

    BEGIN/COMMIT/ROLLBACK use explicit SQL even with autocommit=True. Requests,
    append-only audit records and notification outbox are committed together.
    Paths must be selected by a trusted fixture owner; this is not a filesystem
    sandbox, protected audit log, production migration or identity boundary.
    """

    def __init__(self, path: Path, *, draft_fixture: bool = False) -> None:
        if draft_fixture is not True:
            raise ApprovalError("explicit offline draft_fixture=True required")
        self.connection = sqlite3.connect(path, autocommit=True)
        try:
            with self._write():
                expected = {
                    "draft_approval_requests",
                    "draft_approval_records",
                    "draft_approval_notifications",
                    "draft_approval_history",
                }
                actual = {
                    row[0]
                    for row in self.connection.execute(
                        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                    )
                }
                if actual and actual != expected:
                    raise ApprovalError("standalone approval fixture database required")
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS draft_approval_requests ("
                    "operation_id TEXT PRIMARY KEY NOT NULL, operation_type TEXT NOT NULL, "
                    "canonical_arguments TEXT NOT NULL, binding_digest TEXT NOT NULL, "
                    "risk_level TEXT NOT NULL CHECK (risk_level IN ('LOW','MEDIUM','HIGH')), "
                    "created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)"
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS draft_approval_records ("
                    "id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, "
                    "risk_level TEXT NOT NULL CHECK (risk_level IN ('LOW','MEDIUM','HIGH')), "
                    "approved_by TEXT, timestamp INTEGER NOT NULL, reason TEXT NOT NULL, "
                    "status TEXT NOT NULL CHECK (status IN ('APPROVED','RECORDED','PENDING')))"
                )
                self.connection.execute(
                    "CREATE INDEX IF NOT EXISTS draft_approval_history "
                    "ON draft_approval_records(operation_id, id)"
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS draft_approval_notifications ("
                    "operation_id TEXT PRIMARY KEY NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                    "sent INTEGER NOT NULL DEFAULT 0, last_error TEXT)"
                )
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> ApprovalStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.connection.close()

    @contextmanager
    def _write(self) -> Generator[None]:
        if self.connection.in_transaction:
            raise ApprovalError("caller transaction is not allowed")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def request(self, operation_id: str) -> ApprovalRequest:
        row = self.connection.execute(
            "SELECT operation_id, operation_type, canonical_arguments, binding_digest, "
            "risk_level, created_at, expires_at FROM draft_approval_requests WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise ApprovalError("approval request missing")
        return ApprovalRequest(row[0], row[1], row[2], row[3], RiskLevel(row[4]), row[5], row[6])

    def history(self, operation_id: str | None = None) -> list[ApprovalRecord]:
        sql = (
            "SELECT operation_id, risk_level, approved_by, timestamp, reason, status "
            "FROM draft_approval_records"
        )
        rows = self.connection.execute(
            sql + (" WHERE operation_id=?" if operation_id is not None else "") + " ORDER BY id",
            (operation_id,) if operation_id is not None else (),
        )
        return [
            ApprovalRecord(
                row[0], RiskLevel(row[1]), row[2], row[3], row[4], ApprovalStatus(row[5])
            )
            for row in rows
        ]

    def _append(self, record: ApprovalRecord) -> None:
        self.connection.execute(
            "INSERT INTO draft_approval_records "
            "(operation_id,risk_level,approved_by,timestamp,reason,status) VALUES (?,?,?,?,?,?)",
            (
                record.operation_id,
                record.risk_level,
                record.approved_by,
                record.timestamp,
                record.reason,
                record.status,
            ),
        )

    def register(self, request: ApprovalRequest, record: ApprovalRecord) -> ApprovalRecord:
        """Internal fixture persistence; not exposed to model tools or a CLI."""
        with self._write():
            existing = self.history(request.operation_id)
            if existing:
                original = self.request(request.operation_id)
                if (
                    original.operation_type,
                    original.canonical_arguments,
                    original.risk_level,
                ) != (request.operation_type, request.canonical_arguments, request.risk_level):
                    raise ApprovalError("operation binding changed")
                return existing[-1]
            self.connection.execute(
                "INSERT INTO draft_approval_requests VALUES (?,?,?,?,?,?,?)",
                (
                    request.operation_id,
                    request.operation_type,
                    request.canonical_arguments,
                    request.binding_digest,
                    request.risk_level,
                    request.created_at,
                    request.expires_at,
                ),
            )
            self._append(record)
            if record.status is ApprovalStatus.PENDING:
                self.connection.execute(
                    "INSERT INTO draft_approval_notifications(operation_id) VALUES (?)",
                    (request.operation_id,),
                )
        return record

    def record_notification_result(
        self, operation_id: str, delivered: bool, error: str | None
    ) -> None:
        """Fixture persistence only; the transport owns receipt verification."""
        with self._write():
            self.connection.execute(
                "UPDATE draft_approval_notifications SET attempts=attempts+1,"
                "sent=?,last_error=? WHERE operation_id=? AND sent=0",
                (int(delivered), error, operation_id),
            )

    def record_confirmation(self, record: ApprovalRecord) -> None:
        """Internal fixture persistence, NOT a trusted approval endpoint."""
        with self._write():
            history = self.history(record.operation_id)
            if not history or history[-1].status is not ApprovalStatus.PENDING:
                raise ApprovalError("only a pending request may be confirmed")
            self._append(record)
            # Stop queued prompts once resolved; do not fabricate a sent receipt.
            self.connection.execute(
                "DELETE FROM draft_approval_notifications WHERE operation_id=? AND sent=0",
                (record.operation_id,),
            )

    def pending_notifications(self) -> list[ApprovalNotification]:
        rows = self.connection.execute(
            "SELECT r.operation_id,r.binding_digest,r.risk_level,r.expires_at "
            "FROM draft_approval_notifications n JOIN draft_approval_requests r "
            "ON n.operation_id=r.operation_id WHERE n.sent=0 ORDER BY r.created_at,r.operation_id"
        )
        return [ApprovalNotification(row[0], row[1], RiskLevel(row[2]), row[3]) for row in rows]


class ApprovalGate:
    """Draft preflight gate; HIGH stays sealed despite notification failure.

    Retry the durable outbox on evaluation/replay or retry_notifications(). Each
    pass attempts each selected message once, with no sleeping/unbounded retry.
    No executor callback exists: operation failure/retry and unknown-effect
    reconciliation remain the Intent/Worker's job, not an approval bypass.
    """

    def __init__(
        self,
        store: ApprovalStore,
        *,
        policy: ApprovalPolicy | None = None,
        imessage_notifier: IMessageNotifier | None = None,
        confirmation_verifier: ConfirmationVerifier | None = None,
        validity_ms: int = 300_000,
    ) -> None:
        _timestamp(validity_ms)
        if validity_ms == 0:
            raise ValueError("validity_ms must be positive")
        self.store = store
        self.policy = policy if policy is not None else ApprovalPolicy()
        self.imessage_notifier = imessage_notifier
        self._confirmation_verifier = confirmation_verifier
        self._validity_ms = validity_ms

    def evaluate(
        self,
        operation_id: str,
        operation_type: str,
        arguments: Mapping[str, Any],
        *,
        timestamp: int,
    ) -> ApprovalRecord:
        _text(operation_id)
        _timestamp(timestamp)
        _timestamp(timestamp + self._validity_ms)
        canonical = _canonical(arguments)
        # Classify the same detached payload that is sealed, not a mutable caller map.
        risk = self.policy.classify(operation_type, json.loads(canonical))
        binding = hashlib.sha256(
            _canonical(
                {
                    "operation_id": operation_id,
                    "operation_type": operation_type,
                    "canonical_arguments": canonical,
                    "risk_level": risk.value,
                    "expires_at": timestamp + self._validity_ms,
                }
            ).encode("utf-8")
        ).hexdigest()
        status, approver, reason = {
            RiskLevel.LOW: (ApprovalStatus.APPROVED, "policy:auto", "low_risk_auto_approved"),
            RiskLevel.MEDIUM: (ApprovalStatus.RECORDED, None, "medium_risk_consultation_recorded"),
            RiskLevel.HIGH: (ApprovalStatus.PENDING, None, "high_risk_human_confirmation_required"),
        }[risk]
        record = self.store.register(
            ApprovalRequest(
                operation_id,
                operation_type,
                canonical,
                binding,
                risk,
                timestamp,
                timestamp + self._validity_ms,
            ),
            ApprovalRecord(operation_id, risk, approver, timestamp, reason, status),
        )
        self.retry_notifications(operation_id=operation_id)
        return record

    def retry_notifications(self, *, operation_id: str | None = None) -> int:
        if self.imessage_notifier is None:
            return 0
        sent = 0
        for notification in self.store.pending_notifications():
            if operation_id is not None and notification.operation_id != operation_id:
                continue
            error = None
            try:
                delivered = self.imessage_notifier(notification) is True
                if not delivered:
                    error = "delivery_unconfirmed"
            except Exception as failure:
                delivered = False
                error = type(failure).__name__
            self.store.record_notification_result(notification.operation_id, delivered, error)
            sent += int(delivered)
        return sent

    def confirm(self, operation_id: str, *, proof: object, timestamp: int) -> ApprovalRecord:
        """Fixture-only verification seam, not a callable model/CLI approval tool."""
        _timestamp(timestamp)
        if self._confirmation_verifier is None:
            raise ApprovalError("trusted confirmation verifier is not configured")
        request = self.store.request(operation_id)
        if timestamp < request.created_at or timestamp >= request.expires_at:
            raise ApprovalError("approval expired or confirmation predates request")
        identity = self._confirmation_verifier(request, proof)
        if type(identity) is not str or not identity.strip() or identity.startswith("policy:"):
            raise ApprovalError("trusted human identity verification failed")
        _text(identity)
        record = ApprovalRecord(
            operation_id,
            request.risk_level,
            identity,
            timestamp,
            "trusted_fixture_confirmation",
            ApprovalStatus.APPROVED,
        )
        self.store.record_confirmation(record)
        return record
