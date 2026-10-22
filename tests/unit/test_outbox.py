"""K3-003 offline SQLite outbox draft; no real recipients or transports."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path

import pytest

from jarvis import outbox
from jarvis.outbox import (
    OutboxChannel,
    OutboxMessage,
    OutboxStatus,
    OutboxWriter,
    create_outbox_schema,
)


@pytest.fixture
def db() -> Generator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    create_outbox_schema(connection, draft_fixture=True)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def message() -> OutboxMessage:
    return OutboxMessage(
        id="message-1",
        channel=OutboxChannel.EMAIL,
        recipient="fixture@example.invalid",
        subject="fixture",
        body="synthetic content",
        created_at=1000,
        scheduled_at=1000,
        idempotency_key="effect-1",
    )


def _enqueue(db: sqlite3.Connection, message: OutboxMessage) -> OutboxMessage:
    db.execute("BEGIN")
    result = OutboxWriter(draft_fixture=True).enqueue(message, db)
    db.commit()
    return result


def _relay(now: int = 1000) -> outbox.OutboxRelay:
    assert hasattr(outbox, "OutboxRelay"), "relay draft is not implemented"
    return outbox.OutboxRelay(draft_fixture=True, clock=lambda: now)


def test_enqueue_writes_pending_in_caller_transaction(
    db: sqlite3.Connection,
    message: OutboxMessage,
) -> None:
    db.execute("CREATE TABLE business (id TEXT PRIMARY KEY)")
    db.execute("BEGIN")
    db.execute("INSERT INTO business VALUES ('business-1')")
    saved = OutboxWriter(draft_fixture=True).enqueue(message, db)
    assert saved == message
    assert db.in_transaction
    assert db.execute("SELECT status, attempts FROM outbox").fetchone() == ("PENDING", 0)
    db.rollback()
    assert db.execute("SELECT count(*) FROM business").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM outbox").fetchone() == (0,)


def test_poll_returns_due_pending_without_writes(
    db: sqlite3.Connection,
    message: OutboxMessage,
) -> None:
    _enqueue(db, message)
    _enqueue(db, replace(message, id="future", idempotency_key="future", scheduled_at=2000))
    before = db.total_changes
    assert _relay(999).poll(db) == []
    assert _relay().poll(db) == [message]
    assert _relay().poll(db) == [message]  # Crash before ack leaves it eligible.
    assert db.total_changes == before
    assert not db.in_transaction


def test_mark_delivered_updates_state_and_stops_polling(
    db: sqlite3.Connection,
    message: OutboxMessage,
) -> None:
    _enqueue(db, message)
    relay = _relay()
    db.execute("BEGIN")
    relay.mark_delivered(message.id, db)
    assert db.in_transaction
    assert db.execute(
        "SELECT status, delivered_at, attempts, last_error FROM outbox"
    ).fetchone() == ("DELIVERED", 1000, 1, None)
    db.commit()
    assert relay.poll(db) == []
    db.execute("BEGIN")
    relay.mark_delivered(message.id, db)  # Duplicate ack is idempotent.
    relay.mark_failed(message.id, "stale error", db)  # Never regress DELIVERED.
    assert db.execute("SELECT attempts, last_error FROM outbox").fetchone() == (1, None)
    db.commit()


def test_idempotency_key_returns_original_even_with_new_id_or_schedule(
    db: sqlite3.Connection,
    message: OutboxMessage,
) -> None:
    assert _enqueue(db, message) == message
    assert (
        _enqueue(db, replace(message, id="second", created_at=1500, scheduled_at=2000)) == message
    )
    assert db.execute("SELECT count(*) FROM outbox").fetchone() == (1,)


@pytest.mark.parametrize("field", ["channel", "recipient", "subject", "body"])
def test_same_key_different_payload_is_rejected(
    db: sqlite3.Connection,
    message: OutboxMessage,
    field: str,
) -> None:
    _enqueue(db, message)
    changed = replace(message, **{field: OutboxChannel.WEBHOOK if field == "channel" else "other"})
    db.execute("BEGIN")
    with pytest.raises(ValueError, match="different payload"):
        OutboxWriter(draft_fixture=True).enqueue(changed, db)
    db.commit()
    assert _relay().poll(db) == [message]


def test_duplicate_id_with_different_key_is_not_silently_ignored(
    db: sqlite3.Connection,
    message: OutboxMessage,
) -> None:
    _enqueue(db, message)
    db.execute("BEGIN")
    with pytest.raises(sqlite3.IntegrityError):
        OutboxWriter(draft_fixture=True).enqueue(replace(message, idempotency_key="other"), db)
    db.rollback()


def test_exponential_backoff_and_five_attempts_stop_retries(
    db: sqlite3.Connection,
    message: OutboxMessage,
) -> None:
    _enqueue(db, message)
    now = 1000
    for attempt in range(1, 6):
        relay = _relay(now)
        assert len(relay.poll(db)) == 1
        db.execute("BEGIN")
        relay.mark_failed(message.id, f"fixture error {attempt}", db)
        assert db.in_transaction
        db.commit()
        status, attempts, error, scheduled = db.execute(
            "SELECT status, attempts, last_error, scheduled_at FROM outbox"
        ).fetchone()
        assert (status, attempts, error) == ("FAILED", attempt, f"fixture error {attempt}")
        assert relay.poll(db) == []
        if attempt < 5:
            assert scheduled == now + 1000 * 2 ** (attempt - 1)
            assert _relay(scheduled - 1).poll(db) == []
            assert len(_relay(scheduled).poll(db)) == 1
            now = scheduled
    assert _relay(now + 1_000_000).poll(db) == []
    assert db.execute("SELECT count(*) FROM outbox").fetchone() == (1,)  # Dead letter retained.
    db.execute("BEGIN")
    with pytest.raises(ValueError, match="exhausted"):
        _relay(now + 1_000_000).mark_failed(message.id, "sixth", db)
    db.rollback()


def test_success_after_failure_and_duplicate_enqueue_preserve_history(
    db: sqlite3.Connection,
    message: OutboxMessage,
) -> None:
    _enqueue(db, message)
    db.execute("BEGIN")
    _relay().mark_failed(message.id, "temporary", db)
    db.commit()
    duplicate = _enqueue(db, replace(message, id="duplicate"))
    assert duplicate.status is OutboxStatus.FAILED
    assert duplicate.attempts == 1 and duplicate.scheduled_at == 2000
    db.execute("BEGIN")
    _relay(2000).mark_delivered(message.id, db)
    db.commit()
    duplicate = _enqueue(db, replace(message, id="duplicate"))
    assert duplicate.status is OutboxStatus.DELIVERED
    assert duplicate.attempts == 2 and duplicate.last_error is None
    assert duplicate.delivered_at == 2000


def test_mutators_require_caller_transaction(
    db: sqlite3.Connection,
    message: OutboxMessage,
) -> None:
    with pytest.raises(ValueError, match="transaction"):
        OutboxWriter(draft_fixture=True).enqueue(message, db)
    _enqueue(db, message)
    for call in (
        lambda: _relay().mark_delivered(message.id, db),
        lambda: _relay().mark_failed(message.id, "error", db),
    ):
        with pytest.raises(ValueError, match="transaction"):
            call()
    assert _relay().poll(db) == [message]


def test_acknowledgement_can_be_rolled_back(
    db: sqlite3.Connection,
    message: OutboxMessage,
) -> None:
    _enqueue(db, message)
    for call in (
        lambda: _relay().mark_delivered(message.id, db),
        lambda: _relay().mark_failed(message.id, "error", db),
    ):
        db.execute("BEGIN")
        call()
        db.rollback()
        assert _relay().poll(db) == [message]


def test_other_connection_sees_only_committed_messages_and_restart(
    tmp_path: Path,
    message: OutboxMessage,
) -> None:
    path = tmp_path / "outbox.sqlite"
    with sqlite3.connect(path, isolation_level=None) as writer:
        create_outbox_schema(writer, draft_fixture=True)
        with sqlite3.connect(path, isolation_level=None) as reader:
            writer.execute("BEGIN")
            OutboxWriter(draft_fixture=True).enqueue(message, writer)
            assert _relay().poll(reader) == []
            writer.commit()
            assert _relay().poll(reader) == [message]
    with sqlite3.connect(path, isolation_level=None) as reopened:
        reopened.row_factory = sqlite3.Row
        assert _relay().poll(reopened) == [message]
        reopened.execute("BEGIN")
        assert OutboxWriter(draft_fixture=True).enqueue(message, reopened) == message
        reopened.commit()


def test_poll_orders_by_schedule_creation_and_id(
    db: sqlite3.Connection,
    message: OutboxMessage,
) -> None:
    expected = [
        replace(message, id="c", idempotency_key="c", scheduled_at=1001),
        replace(message, id="b", idempotency_key="b"),
        replace(message, id="a", idempotency_key="a"),
    ]
    for item in expected:
        _enqueue(db, item)
    assert [item.id for item in _relay(1001).poll(db)] == ["a", "b", "c"]


def test_early_or_missing_ack_is_rejected(
    db: sqlite3.Connection,
    message: OutboxMessage,
) -> None:
    _enqueue(db, message)
    db.execute("BEGIN")
    for action in (
        lambda: _relay(999).mark_delivered(message.id, db),
        lambda: _relay(999).mark_failed(message.id, "early", db),
        lambda: _relay().mark_delivered("missing", db),
        lambda: _relay().mark_failed("missing", "error", db),
    ):
        with pytest.raises(ValueError):
            action()
    db.rollback()
    assert _relay().poll(db) == [message]


@pytest.mark.parametrize("channel", list(OutboxChannel))
def test_supported_channels_round_trip(
    db: sqlite3.Connection,
    message: OutboxMessage,
    channel: OutboxChannel,
) -> None:
    candidate = replace(message, channel=channel)
    _enqueue(db, candidate)
    assert _relay().poll(db) == [candidate]


@pytest.mark.parametrize(
    "change",
    [
        {"id": ""},
        {"idempotency_key": " "},
        {"recipient": ""},
        {"channel": "SMS"},
        {"status": "unknown"},
        {"created_at": True},
        {"scheduled_at": -1},
        {"scheduled_at": 2**63},
        {"body": "bad\x00body"},
        {"attempts": -1},
        {"delivered_at": 500},
    ],
)
def test_invalid_message_rejected(message: OutboxMessage, change: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(message, **change)


def test_only_fresh_pending_can_be_enqueued(
    db: sqlite3.Connection,
    message: OutboxMessage,
) -> None:
    db.execute("BEGIN")
    with pytest.raises(ValueError):
        OutboxWriter(draft_fixture=True).enqueue(
            replace(message, status=OutboxStatus.FAILED, attempts=1, last_error="error"),
            db,
        )
    db.rollback()


def test_fixture_opt_in_and_no_schema_side_effects() -> None:
    assert outbox.RUNTIME_ENABLED is False
    assert outbox.DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert hasattr(outbox, "OutboxRelay")
    for constructor in (OutboxWriter, outbox.OutboxRelay):
        with pytest.raises(ValueError, match="draft_fixture"):
            constructor()
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(ValueError, match="draft_fixture"):
            create_outbox_schema(connection)
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
        create_outbox_schema(connection, draft_fixture=True)
        with pytest.raises(sqlite3.OperationalError):
            create_outbox_schema(connection, draft_fixture=True)


def test_poll_refuses_uncommitted_messages(
    db: sqlite3.Connection,
    message: OutboxMessage,
) -> None:
    db.execute("BEGIN")
    OutboxWriter(draft_fixture=True).enqueue(message, db)
    with pytest.raises(ValueError, match="committed"):
        _relay().poll(db)
    db.rollback()
    assert _relay().poll(db) == []


def test_schema_enforces_status_and_key_constraints(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO outbox (id, channel, recipient, subject, body, created_at, "
            "scheduled_at, idempotency_key, status) "
            "VALUES ('x', 'SMS', 'r', '', '', 0, 0, '', 'UNKNOWN')"
        )
