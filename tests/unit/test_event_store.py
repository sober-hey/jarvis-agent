"""K3-002 offline durable event store draft tests."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from jarvis.event_schema import Event, EventPriority
from jarvis.event_store import EventStore

Database = tuple[sqlite3.Connection, EventStore]


def _modules() -> Any:
    assert importlib.util.find_spec("jarvis.event_store") is not None, "event store is missing"
    from jarvis.event_schema import Event, EventFilter, EventType
    from jarvis.event_store import AppendOnlyError, EventStore, EventStoreError

    return Event, EventFilter, EventType, EventStore, EventStoreError, AppendOnlyError


def _event(index: int = 1, **changes: Any) -> Event:
    Event, _, EventType, *_ = _modules()
    values: dict[str, Any] = {
        "id": f"event-{index}",
        "type": EventType.OBSERVATION,
        "priority": EventPriority.NORMAL,
        "session_id": "timeline-test",
        "source": "unit-test",
        "payload": {"text": "Sir", "values": [index, True, None]},
        "created_at": 1000 + index,
    }
    return Event(**(values | changes))


@pytest.fixture
def database(tmp_path: Path) -> Generator[Database]:
    _, _, _, EventStore, *_ = _modules()
    connection = sqlite3.connect(tmp_path / "events.sqlite3")
    try:
        yield connection, EventStore(connection)
    finally:
        connection.close()


def test_append_is_durable_and_get_returns_the_event(database: Database) -> None:
    connection, store = database
    event = _event()
    assert store.append(event) == event.id
    assert store.get(event.id) == event
    assert not connection.in_transaction
    assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
    assert connection.execute("PRAGMA fullfsync").fetchone() == (1,)
    assert connection.execute("PRAGMA checkpoint_fullfsync").fetchone() == (1,)


def test_reopen_and_checkpoint_preserve_data(database: Database) -> None:
    connection, store = database
    event = _event()
    store.append(event)
    store.checkpoint()
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    connection.close()
    _, _, _, EventStore, *_ = _modules()
    reopened = sqlite3.connect(path)
    try:
        assert EventStore(reopened).get(event.id) == event
        assert reopened.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        reopened.close()


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_append_only_api(database: Database, operation: str) -> None:
    _, store = database
    event = _event()
    store.append(event)
    *_, AppendOnlyError = _modules()
    with pytest.raises(AppendOnlyError, match="append-only"):
        getattr(store, operation)(event if operation == "update" else event.id)
    assert store.get(event.id) == event


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE durable_events SET source = 'forged'",
        "DELETE FROM durable_events",
        "INSERT OR REPLACE INTO durable_events SELECT * FROM durable_events",
        "REPLACE INTO durable_events SELECT * FROM durable_events",
    ],
)
def test_append_only_enforced_by_database(database: Database, statement: str) -> None:
    connection, store = database
    event = _event()
    store.append(event)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(statement)
    connection.rollback()
    assert store.get(event.id) == event


def test_duplicate_id_cannot_overwrite_and_store_remains_usable(database: Database) -> None:
    _, store = database
    event = _event()
    store.append(event)
    with pytest.raises(sqlite3.IntegrityError):
        store.append(_event(source="replacement"))
    assert store.get(event.id) == event
    store.append(_event(2))
    assert len(store.query()) == 2


def test_replay_is_inclusive_append_order_not_timestamp_order(database: Database) -> None:
    _, store = database
    events = [_event(index, created_at=4000 - index) for index in range(1, 305)]
    for event in events:
        store.append(event)
    assert list(store.replay()) == events
    assert list(store.replay(events[1].id)) == events[1:]
    assert list(store.replay(events[-1].id)) == events[-1:]
    with pytest.raises(KeyError):
        list(store.replay("missing"))


def test_replay_has_fixed_high_watermark_and_does_not_pin_checkpoint(database: Database) -> None:
    _, store = database
    store.append(_event())
    iterator = store.replay()
    store.append(_event(2))
    store.checkpoint()
    assert list(iterator) == [_event()]


def test_query_filter_is_parameterized_inclusive_and_ordered(database: Database) -> None:
    _, store = database
    _, EventFilter, EventType, *_ = _modules()
    events = [
        _event(1, source="source' OR 1=1 --"),
        _event(2, type=EventType.ACTION),
        _event(3, type=EventType.ACTION),
    ]
    for event in events:
        store.append(event)
    assert store.query(EventFilter(source_filter=(events[0].source,))) == events[:1]
    assert store.query(EventFilter(type_filter=(EventType.ACTION,))) == events[1:]
    assert store.query(EventFilter(time_range=(1002, 1003))) == events[1:]
    assert store.query(EventFilter(source_filter=("unknown",))) == []


def test_payloads_are_detached_from_callers_and_reads(database: Database) -> None:
    _, store = database
    payload = {"nested": ["before"]}
    event = _event(payload=payload)
    store.append(event)
    payload["nested"].append("later")
    result = store.get(event.id)
    result.payload["nested"].append("mutated-read")
    assert store.get(event.id).payload == {"nested": ["before"]}


def test_missing_get_raises_key_error(database: Database) -> None:
    _, store = database
    with pytest.raises(KeyError):
        store.get("missing")


def test_concurrent_append_on_independent_connections(database: Database) -> None:
    connection, store = database
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    _, _, _, EventStore, *_ = _modules()

    def append_batch(worker: int) -> list[str]:
        writer = sqlite3.connect(path, timeout=10)
        try:
            writer_store = EventStore(writer)
            return [writer_store.append(_event(worker * 20 + i)) for i in range(20)]
        finally:
            writer.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        batches = list(executor.map(append_batch, range(4)))
    expected = {event_id for batch in batches for event_id in batch}
    assert len(expected) == 80
    assert {event.id for event in store.replay()} == expected
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_concurrent_append_on_shared_store(tmp_path: Path) -> None:
    _, _, _, EventStore, *_ = _modules()
    connection = sqlite3.connect(tmp_path / "shared.sqlite3", check_same_thread=False)
    try:
        store = EventStore(connection)

        def append_one(index: int) -> str:
            return store.append(_event(index))

        with ThreadPoolExecutor(max_workers=4) as executor:
            ids = list(executor.map(append_one, range(40)))
        assert len(set(ids)) == len(store.query()) == 40
    finally:
        connection.close()


def test_crash_recovers_committed_wal_and_discards_uncommitted_rows(tmp_path: Path) -> None:
    _, _, _, EventStore, *_ = _modules()
    path = tmp_path / "crash.sqlite3"
    script = """
import os, sqlite3, sys
from jarvis.event_schema import Event, EventType, EventPriority
from jarvis.event_store import EventStore
connection = sqlite3.connect(sys.argv[1])
store = EventStore(connection)
connection.execute('PRAGMA wal_autocheckpoint=0')
store.append(Event(id='committed', type=EventType.OBSERVATION, priority=EventPriority.NORMAL,
                  session_id='timeline', source='crash-test', payload={'ok': True}, created_at=1))
connection.execute('BEGIN IMMEDIATE')
connection.execute('INSERT INTO durable_events '
                   '(id, type, priority, session_id, source, payload, created_at, tags) '
                   'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                   ('uncommitted', 'OBSERVATION', 2, 'timeline', 'crash-test', '{}', 2, '[]'))
os._exit(73)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 73, result.stderr
    assert Path(str(path) + "-wal").stat().st_size > 0
    connection = sqlite3.connect(path)
    try:
        store = EventStore(connection)
        assert [event.id for event in store.replay()] == ["committed"]
        with pytest.raises(KeyError):
            store.get("uncommitted")
        store.checkpoint()
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        connection.close()


def test_caller_transaction_is_not_committed_or_rolled_back(database: Database) -> None:
    connection, store = database
    _, _, _, _, EventStoreError, _ = _modules()
    connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(EventStoreError, match="transaction"):
        store.append(_event())
    with pytest.raises(EventStoreError, match="transaction"):
        store.checkpoint()
    assert connection.in_transaction
    connection.rollback()
    assert store.query() == []


def test_prewrite_fsync_failure_does_not_append(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = database

    def fail_fsync(descriptor: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr("jarvis.event_store.os.fsync", fail_fsync)
    with pytest.raises(OSError, match="fsync failed"):
        store.append(_event())
    assert store.query() == []


@pytest.mark.parametrize(
    "kind", ["memory", "other-schema", "sqlite-prefix-schema", "transaction", "autocommit-false"]
)
def test_unsupported_connections_are_rejected(tmp_path: Path, kind: str) -> None:
    _, _, _, EventStore, EventStoreError, _ = _modules()
    connection = sqlite3.connect(":memory:" if kind == "memory" else tmp_path / "bad.sqlite3")
    try:
        if kind == "other-schema":
            connection.execute("CREATE TABLE product_data (id TEXT)")
        elif kind == "sqlite-prefix-schema":
            connection.execute("CREATE TABLE sqliteXproduct_data (id TEXT)")
        elif kind == "transaction":
            connection.execute("BEGIN")
        elif kind == "autocommit-false":
            connection.autocommit = False
        with pytest.raises(EventStoreError):
            EventStore(connection)
    finally:
        connection.close()


@pytest.mark.parametrize("payload", [{"bad": float("nan")}, {1: "bad"}, {"bad": object()}])
def test_non_json_payload_is_rejected(payload: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        _event(payload=payload)


@pytest.mark.parametrize(
    "selection", ["empty-type", "empty-source", "empty-tags", "tags", "priority", "combined"]
)
def test_full_shared_filter_contract(database: Database, selection: str) -> None:
    from jarvis.event_schema import EventFilter, EventType

    _, store = database
    events = [
        _event(1, tags=("a", "b"), priority=EventPriority.LOW),
        _event(2, tags=("a",), priority=EventPriority.HIGH),
        _event(3, tags=("a", "b"), type=EventType.ACTION, priority=EventPriority.URGENT),
    ]
    for event in events:
        store.append(event)
    cases: dict[str, tuple[EventFilter, list[Event]]] = {
        "empty-type": (EventFilter(type_filter=()), []),
        "empty-source": (EventFilter(source_filter=()), []),
        "empty-tags": (EventFilter(tag_filter=()), events),
        "tags": (EventFilter(tag_filter=("a", "b")), [events[0], events[2]]),
        "priority": (EventFilter(priority_min=EventPriority.HIGH), events[1:]),
        "combined": (
            EventFilter(
                type_filter=(EventType.OBSERVATION, EventType.ACTION),
                source_filter=("unit-test",),
                priority_min=EventPriority.HIGH,
                time_range=(1003, 1003),
                tag_filter=("a", "b"),
            ),
            events[2:],
        ),
    }
    filter, expected = cases[selection]
    assert store.query(filter) == expected


def test_checkpoint_reports_busy_reader_instead_of_claiming_success(database: Database) -> None:
    from jarvis.event_store import EventStoreError

    connection, store = database
    store.append(_event())
    reader = sqlite3.connect(connection.execute("PRAGMA database_list").fetchone()[2])
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM durable_events").fetchone()
        store.append(_event(2))
        connection.execute("PRAGMA busy_timeout=1")
        with pytest.raises(EventStoreError, match="busy or incomplete"):
            store.checkpoint()
        reader.rollback()
        store.checkpoint()
        assert len(store.query()) == 2
    finally:
        reader.close()


def test_foreign_key_failure_is_atomic_and_does_not_lose_prior_events(database: Database) -> None:
    _, store = database
    store.append(_event())
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        store.append(_event(2, parent_event_id="absent-parent"))
    assert store.query() == [_event()]
    child = _event(2, parent_event_id="event-1")
    store.append(child)
    assert store.get(child.id) == child


@pytest.mark.parametrize("pragma", ["synchronous=OFF", "fullfsync=OFF", "foreign_keys=OFF"])
def test_downgraded_durability_settings_are_not_silently_used(
    database: Database, pragma: str
) -> None:
    from jarvis.event_store import EventStoreError

    connection, store = database
    connection.execute(f"PRAGMA {pragma}")
    with pytest.raises(EventStoreError, match="setting changed"):
        store.append(_event())
    assert store.query() == []


def test_autocommit_true_is_rejected_before_creating_schema(tmp_path: Path) -> None:
    from jarvis.event_store import EventStoreError

    connection = sqlite3.connect(tmp_path / "autocommit.sqlite3", autocommit=True)
    try:
        with pytest.raises(EventStoreError):
            EventStore(connection)
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        connection.close()


def test_prewrite_fsync_really_precedes_insert(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, store = database
    actions: list[str] = []
    original_fsync = os.fsync

    def trace(statement: str) -> None:
        if statement.startswith("INSERT INTO main.durable_events"):
            actions.append("INSERT")

    def sync(descriptor: int) -> None:
        actions.append("fsync")
        original_fsync(descriptor)

    connection.set_trace_callback(trace)
    monkeypatch.setattr("jarvis.event_store.os.fsync", sync)
    store.append(_event())
    assert "fsync" in actions
    assert "INSERT" in actions
    assert actions.index("fsync") < actions.index("INSERT")
