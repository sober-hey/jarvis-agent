"""K3-003 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

Opt-in local SQLite fixture; no production migration, worker or transport is
registered. Timestamps are UTC epoch milliseconds. All mutations participate
in the caller's existing transaction and never begin/commit/rollback it.

Single relay only: poll is read-only, not a claim/lease. Commit enqueue before
polling on a separate connection; send outside the DB transaction, then ack in
an explicit new transaction. A crash before ack leaves the message eligible,
so downstream delivery can repeat (not exactly-once). Pass idempotency_key to
idempotent transports when supported. After five recorded failures the row is
retained as FAILED/dead letter, not deleted or silently treated as delivered.
This draft does not guarantee eventual success or implement dead-letter alerts.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from time import time_ns
from typing import Any, ClassVar, cast

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False
_MAX_INTEGER = 2**63 - 1


class OutboxChannel(StrEnum):
    IMESSAGE = "IMESSAGE"
    EMAIL = "EMAIL"
    WEBHOOK = "WEBHOOK"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


def _text(name: str, value: str, *, nonblank: bool = True) -> None:
    if (
        not isinstance(cast(object, value), str)
        or "\x00" in value
        or (nonblank and not value.strip())
    ):
        raise ValueError(f"{name} must be {'nonblank ' if nonblank else ''}text without NUL")
    value.encode("utf-8", errors="strict")


def _integer(name: str, value: int, minimum: int = 0, maximum: int = _MAX_INTEGER) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an exact integer in [{minimum}, {maximum}]")


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """Immutable snapshot, not execution permission; dates are UTC epoch ms.

    attempts counts recorded transport outcomes (including success), not polls.
    Crashes without acknowledgements cannot be counted as known failures.
    scheduled_at is the next eligible time, updated by retry backoff.
    """

    id: str
    channel: OutboxChannel
    recipient: str
    subject: str
    body: str
    created_at: int
    scheduled_at: int
    idempotency_key: str
    delivered_at: int | None = None
    attempts: int = 0
    last_error: str | None = None
    status: OutboxStatus = OutboxStatus.PENDING

    def __post_init__(self) -> None:
        for name in ("id", "recipient", "idempotency_key"):
            _text(name, getattr(self, name))
        for name in ("subject", "body"):
            _text(name, getattr(self, name), nonblank=False)
        if not isinstance(cast(object, self.channel), OutboxChannel) or not isinstance(
            cast(object, self.status), OutboxStatus
        ):
            raise TypeError("channel/status must be OutboxChannel/OutboxStatus")
        _integer("created_at", self.created_at)
        _integer("scheduled_at", self.scheduled_at, self.created_at)
        _integer("attempts", self.attempts, maximum=5)
        if self.last_error is not None:
            _text("last_error", self.last_error, nonblank=False)
        if self.delivered_at is not None:
            _integer("delivered_at", self.delivered_at, self.created_at)
        if self.status is OutboxStatus.PENDING:
            if self.attempts != 0 or self.delivered_at is not None or self.last_error is not None:
                raise ValueError("PENDING must be fresh, without delivery/error metadata")
        elif self.status is OutboxStatus.DELIVERED:
            if self.attempts < 1 or self.delivered_at is None or self.last_error is not None:
                raise ValueError("DELIVERED requires timestamp/attempts and no error")
        elif self.attempts < 1 or self.delivered_at is not None or self.last_error is None:
            raise ValueError("FAILED requires attempts/error and no delivery timestamp")


_COLUMNS = (
    "id, channel, recipient, subject, body, created_at, scheduled_at, "
    "idempotency_key, delivered_at, attempts, last_error, status"
)


def _fixture(enabled: bool) -> None:
    if enabled is not True:
        raise ValueError("explicit offline draft_fixture=True required")


def _transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise ValueError("caller must begin an explicit transaction")


def create_outbox_schema(connection: sqlite3.Connection, *, draft_fixture: bool = False) -> None:
    """Explicit fixture bootstrap; never called by enqueue, poll or import.

    Creates a new candidate table, refusing an existing table rather than silently
    adopting/migrating an unknown outbox. No executescript (it may commit callers).
    This opt-in helper is not a core-schema migration or production installer.
    """
    _fixture(draft_fixture)
    connection.execute(
        "CREATE TABLE outbox ("
        "id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(id)) > 0), "
        "channel TEXT NOT NULL CHECK (channel IN ('IMESSAGE','EMAIL','WEBHOOK')), "
        "recipient TEXT NOT NULL CHECK (length(trim(recipient)) > 0), "
        "subject TEXT NOT NULL, body TEXT NOT NULL, "
        "created_at INTEGER NOT NULL CHECK (typeof(created_at)='integer' AND created_at >= 0), "
        "scheduled_at INTEGER NOT NULL CHECK (typeof(scheduled_at)='integer' "
        "AND scheduled_at >= created_at), "
        "idempotency_key TEXT NOT NULL UNIQUE CHECK (length(trim(idempotency_key)) > 0), "
        "delivered_at INTEGER CHECK (delivered_at IS NULL OR "
        "(typeof(delivered_at)='integer' AND delivered_at >= created_at)), "
        "attempts INTEGER NOT NULL DEFAULT 0 "
        "CHECK (typeof(attempts)='integer' AND attempts BETWEEN 0 AND 5), last_error TEXT, "
        "status TEXT NOT NULL DEFAULT 'PENDING' "
        "CHECK (status IN ('PENDING','DELIVERED','FAILED')), "
        "CHECK ((status='PENDING' AND attempts=0 AND delivered_at IS NULL AND last_error IS NULL) "
        "OR (status='DELIVERED' AND attempts>0 "
        "AND delivered_at IS NOT NULL AND last_error IS NULL) "
        "OR (status='FAILED' AND attempts>0 AND delivered_at IS NULL AND last_error IS NOT NULL)))"
    )


def _message(row: Sequence[Any]) -> OutboxMessage:
    return OutboxMessage(
        id=row[0],
        channel=OutboxChannel(row[1]),
        recipient=row[2],
        subject=row[3],
        body=row[4],
        created_at=row[5],
        scheduled_at=row[6],
        idempotency_key=row[7],
        delivered_at=row[8],
        attempts=row[9],
        last_error=row[10],
        status=OutboxStatus(row[11]),
    )


class OutboxWriter:
    """Use the exact business connection/transaction, never a second commit."""

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(self, *, draft_fixture: bool = False) -> None:
        _fixture(draft_fixture)

    def enqueue(self, message: OutboxMessage, connection: sqlite3.Connection) -> OutboxMessage:
        """Return original on same-key replay; reject different immutable payload.

        Identity/creation/schedule are first-write-wins: newly generated ids or
        enqueue times must not bypass deduplication or reset retries/delivery.
        """
        _transaction(connection)
        if not isinstance(cast(object, message), OutboxMessage):
            raise TypeError("message must be OutboxMessage")
        message.__post_init__()
        if message.status is not OutboxStatus.PENDING:
            raise ValueError("enqueue requires a fresh PENDING message")
        original = connection.execute(
            f"SELECT {_COLUMNS} FROM outbox WHERE idempotency_key=?", (message.idempotency_key,)
        ).fetchone()
        if original is not None:
            saved = _message(original)
            if (saved.channel, saved.recipient, saved.subject, saved.body) != (
                message.channel,
                message.recipient,
                message.subject,
                message.body,
            ):
                raise ValueError("idempotency_key is already bound to a different payload")
            return saved
        connection.execute(
            f"INSERT INTO outbox ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                message.channel.value,
                message.recipient,
                message.subject,
                message.body,
                message.created_at,
                message.scheduled_at,
                message.idempotency_key,
                message.delivered_at,
                message.attempts,
                message.last_error,
                message.status.value,
            ),
        )
        return message


class OutboxRelay:
    """Read-only due poll + transactional outcome recording, not a transport.

    Explicit caller-run relay, not an automatic background service. No lease or
    multi-relay fencing. FAILED rows below max_attempts reappear when due; exhausted
    rows remain queryable in SQLite for operator reconciliation, never auto-reset.
    max_attempts is at most 5; base_backoff_ms defaults to 1000 (1, 2, 4, 8 seconds).
    Retrying ambiguous transport outcomes is valid only for this at-least-once
    notification contract, not K0 file publishing or general dangerous effects.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(
        self,
        *,
        draft_fixture: bool = False,
        clock: Callable[[], int] | None = None,
        max_attempts: int = 5,
        base_backoff_ms: int = 1000,
    ) -> None:
        _fixture(draft_fixture)
        _integer("max_attempts", max_attempts, 1, 5)
        _integer("base_backoff_ms", base_backoff_ms, 1)
        self._clock = clock if clock is not None else lambda: time_ns() // 1_000_000
        self._max_attempts = max_attempts
        self._base_backoff_ms = base_backoff_ms

    def _now(self) -> int:
        now = self._clock()
        _integer("clock", now)
        return now

    def poll(self, connection: sqlite3.Connection) -> list[OutboxMessage]:
        """No deletion, claiming, attempt increment, or automatic send/commit."""
        if connection.in_transaction:
            raise ValueError("poll requires a committed connection, outside a write transaction")
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM outbox WHERE status IN ('PENDING','FAILED') "
            "AND attempts < ? AND scheduled_at <= ? ORDER BY scheduled_at, created_at, id",
            (self._max_attempts, self._now()),
        ).fetchall()
        return [_message(row) for row in rows]

    def _eligible(self, id: str, connection: sqlite3.Connection, now: int) -> OutboxMessage:
        _transaction(connection)
        _text("id", id)
        row = connection.execute(f"SELECT {_COLUMNS} FROM outbox WHERE id=?", (id,)).fetchone()
        if row is None:
            raise ValueError("outbox message not found")
        message = _message(row)
        if message.status is not OutboxStatus.DELIVERED:
            if message.attempts >= self._max_attempts:
                raise ValueError("outbox attempts exhausted; manual reconciliation required")
            if message.scheduled_at > now:
                raise ValueError("outbox message is not due")
        return message

    @staticmethod
    def _update(
        message: OutboxMessage,
        connection: sqlite3.Connection,
        assignment: str,
        values: tuple[object, ...],
    ) -> None:
        changed = connection.execute(
            f"UPDATE outbox SET {assignment} WHERE id=? AND status=? AND attempts=? "
            "AND scheduled_at=?",
            (*values, message.id, message.status.value, message.attempts, message.scheduled_at),
        ).rowcount
        if changed != 1:
            raise ValueError("outbox outcome state changed concurrently")

    def mark_delivered(self, id: str, connection: sqlite3.Connection) -> None:
        """Record an actual transport acknowledgement; repeated ack is a no-op."""
        now = self._now()
        message = self._eligible(id, connection, now)
        if message.status is OutboxStatus.DELIVERED:
            return
        self._update(
            message,
            connection,
            "status='DELIVERED', delivered_at=?, attempts=attempts+1, last_error=NULL",
            (now,),
        )

    def mark_failed(self, id: str, error: str, connection: sqlite3.Connection) -> None:
        """Retain failure and schedule base * 2**(attempts-1); never drop a row.

        Timestamp addition saturates at SQLite's maximum integer rather than
        overflowing. A late error cannot regress a recorded DELIVERED outcome.
        """
        _text("error", error, nonblank=False)
        now = self._now()
        message = self._eligible(id, connection, now)
        if message.status is OutboxStatus.DELIVERED:
            return
        scheduled = min(_MAX_INTEGER, now + self._base_backoff_ms * 2**message.attempts)
        self._update(
            message,
            connection,
            "status='FAILED', attempts=attempts+1, last_error=?, scheduled_at=?",
            (error, scheduled),
        )
