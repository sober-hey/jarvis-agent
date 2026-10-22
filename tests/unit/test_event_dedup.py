"""K3-004 offline deduplication candidate; no runtime integration."""

from __future__ import annotations

import importlib
import importlib.util
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jarvis.event_schema import Event, EventPriority, EventType


def _module() -> Any:
    assert importlib.util.find_spec("jarvis.event_dedup") is not None, "dedup draft is missing"
    return importlib.import_module("jarvis.event_dedup")


def _event(**changes: Any) -> Event:
    original = Event(
        id="event-1",
        type=EventType.OBSERVATION,
        priority=EventPriority.NORMAL,
        source="sensor:offline",
        payload={"value": 1},
        created_at=1000,
        session_id="session-1",
    )
    return replace(original, **changes)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_same_logical_event_is_duplicate_only_after_registration() -> None:
    module = _module()
    dedup = module.EventDeduplicator(clock=Clock())
    event = _event()
    assert not dedup.is_duplicate(event)
    assert not dedup.is_duplicate(event)
    assert dedup.register(event) is None
    assert dedup.is_duplicate(_event(id="replayed", created_at=2000, session_id="new-process"))
    assert module.DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert module.RUNTIME_ENABLED is False
    assert dedup.runtime_enabled is False
    assert module.DeduplicationWindow().window_seconds == 300
    assert module.DeduplicationWindow().max_cache_size == 10000


@pytest.mark.parametrize("elapsed, expected", [(299.999, True), (300, False), (301, False)])
def test_window_is_half_open_and_uses_processing_time(elapsed: float, expected: bool) -> None:
    module = _module()
    clock = Clock()
    dedup = module.EventDeduplicator(clock=clock)
    event = _event(created_at=0)
    dedup.register(event)
    clock.now += elapsed
    assert dedup.is_duplicate(event) is expected


def test_lru_hits_touch_recency_without_extending_ttl() -> None:
    module = _module()
    clock = Clock()
    dedup = module.EventDeduplicator(module.DeduplicationWindow(max_cache_size=2), clock=clock)
    first, second, third = [_event(payload={"value": value}) for value in (1, 2, 3)]
    dedup.register(first)
    dedup.register(second)
    clock.now += 100
    assert dedup.is_duplicate(first)
    dedup.register(third)
    assert not dedup.is_duplicate(second)
    assert dedup.is_duplicate(first)
    clock.now += 200
    assert not dedup.is_duplicate(first)
    assert dedup.is_duplicate(third)


def test_batch_deduplication_preserves_order_and_registers_selected_events() -> None:
    module = _module()
    dedup = module.EventDeduplicator(clock=Clock())
    first = _event()
    second = _event(id="second", payload={"value": 2})
    replay = replace(first, id="replay")
    assert dedup.deduplicate([first, second, replay, second]) == [first, second]
    assert dedup.deduplicate([replay, second]) == []
    assert dedup.deduplicate([]) == []


def test_expired_entries_are_removed_before_evicting_live_lru_entries() -> None:
    module = _module()
    clock = Clock()
    dedup = module.EventDeduplicator(module.DeduplicationWindow(max_cache_size=2), clock=clock)
    first, second, third = [_event(payload={"value": value}) for value in (1, 2, 3)]
    dedup.register(first)
    clock.now += 100
    dedup.register(second)
    assert dedup.is_duplicate(first)
    clock.now += 200
    dedup.register(third)
    assert dedup.is_duplicate(second)
    assert dedup.is_duplicate(third)
    assert not dedup.is_duplicate(first)


def test_persistence_requires_explicit_opt_in_and_standalone_empty_db() -> None:
    module = _module()
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(module.EventDeduplicationError, match="draft_fixture"):
            module.EventDeduplicator(connection=connection)
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
        connection.execute("CREATE TABLE product_data(value TEXT)")
        with pytest.raises(module.EventDeduplicationError, match="standalone"):
            module.EventDeduplicator(connection=connection, draft_fixture=True)
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == [
            ("product_data",)
        ]
    finally:
        connection.close()


def test_sqlite_reopen_retains_live_keys_and_lru_order(tmp_path: Path) -> None:
    module = _module()
    clock = Clock()
    path = tmp_path / "dedup.sqlite"
    window = module.DeduplicationWindow(max_cache_size=2)
    first, second, third = [_event(payload={"value": value}) for value in (1, 2, 3)]
    connection = sqlite3.connect(path)
    try:
        dedup = module.EventDeduplicator(
            window, connection=connection, draft_fixture=True, clock=clock
        )
        dedup.register(first)
        dedup.register(second)
        assert dedup.is_duplicate(first)
        assert not connection.in_transaction
    finally:
        connection.close()
    connection = sqlite3.connect(path)
    try:
        restored = module.EventDeduplicator(
            window, connection=connection, draft_fixture=True, clock=clock
        )
        restored.register(third)
        assert not restored.is_duplicate(second)
        assert restored.is_duplicate(first)
        assert restored.is_duplicate(third)
        clock.now += 300
        assert not restored.is_duplicate(first)
        assert connection.execute("SELECT COUNT(*) FROM event_dedup").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"payload": {"value": 2}},
        {"source": "sensor:different"},
        {"type": EventType.ACTION},
        {"payload": {"value": "1"}},
        {"payload": {"value": True}},
        {"payload": {"value": [1]}},
    ],
)
def test_different_content_is_not_a_duplicate(changes: dict[str, Any]) -> None:
    dedup = _module().EventDeduplicator(clock=Clock())
    dedup.register(_event())
    assert not dedup.is_duplicate(_event(**changes))


def test_canonical_json_is_recursive_and_metadata_is_not_identity() -> None:
    dedup = _module().EventDeduplicator(clock=Clock())
    first = _event(payload={"a": 1, "b": {"一": 2, "二": [False, None]}})
    second = _event(
        id="new-id",
        source=first.source,
        priority=EventPriority.URGENT,
        payload={"b": {"二": [False, None], "一": 2}, "a": 1},
        created_at=9000,
        session_id="new-session",
        task_id="new-task",
        parent_event_id="parent",
        tags=("new-tag",),
    )
    dedup.register(first)
    assert dedup.is_duplicate(second)
    assert not dedup.is_duplicate(_event(payload={"a": 1, "b": {"一": 2, "二": [None, False]}}))


def test_repeated_registration_does_not_extend_ttl() -> None:
    module = _module()
    clock = Clock()
    dedup = module.EventDeduplicator(clock=clock)
    event = _event()
    dedup.register(event)
    clock.now += 299
    dedup.register(event)
    clock.now += 1
    assert not dedup.is_duplicate(event)
    dedup.register(event)
    assert dedup.is_duplicate(event)


def test_a_batch_does_not_emit_same_key_twice_when_larger_than_capacity() -> None:
    module = _module()
    dedup = module.EventDeduplicator(module.DeduplicationWindow(max_cache_size=1), clock=Clock())
    first = _event()
    second = _event(payload={"value": 2})
    assert dedup.deduplicate([first, second, replace(first, id="replayed")]) == [first, second]


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "300", None])
def test_invalid_window_duration_is_rejected(bad: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        _module().DeduplicationWindow(window_seconds=bad)


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "10000", None])
def test_invalid_capacity_is_rejected(bad: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        _module().DeduplicationWindow(max_cache_size=bad)


@pytest.mark.parametrize("bad", [1, {}, False, "window"])
def test_invalid_window_object_is_rejected(bad: Any) -> None:
    with pytest.raises(TypeError):
        _module().EventDeduplicator(bad)


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True, "1000"])
def test_invalid_clock_does_not_register(bad: Any) -> None:
    clock = Clock()
    dedup = _module().EventDeduplicator(clock=clock)
    clock.now = bad
    with pytest.raises((TypeError, ValueError)):
        dedup.register(_event())
    clock.now = 1000
    assert not dedup.is_duplicate(_event())


def test_clock_regression_fails_closed_without_state_mutation() -> None:
    clock = Clock()
    dedup = _module().EventDeduplicator(clock=clock)
    dedup.register(_event())
    clock.now -= 1
    with pytest.raises(ValueError, match="backward"):
        dedup.is_duplicate(_event())
    clock.now += 1
    assert dedup.is_duplicate(_event())


@pytest.mark.parametrize("method", ["is_duplicate", "register"])
def test_non_event_input_is_rejected(method: str) -> None:
    dedup = _module().EventDeduplicator(clock=Clock())
    with pytest.raises(TypeError):
        getattr(dedup, method)({"payload": {"value": 1}})


def test_invalid_batch_is_atomic_and_payload_is_revalidated() -> None:
    dedup = _module().EventDeduplicator(clock=Clock())
    invalid = _event(id="bad")
    invalid.payload["value"] = float("nan")
    with pytest.raises(ValueError):
        dedup.deduplicate([_event(), invalid])
    assert not dedup.is_duplicate(_event())


def test_two_connections_observe_committed_registrations(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "dedup.sqlite"
    one, two = sqlite3.connect(path), sqlite3.connect(path)
    try:
        first = module.EventDeduplicator(connection=one, draft_fixture=True, clock=Clock())
        second = module.EventDeduplicator(connection=two, draft_fixture=True, clock=Clock())
        first.register(_event())
        assert second.is_duplicate(_event())
        other = _event(payload={"value": 2})
        assert second.deduplicate([other]) == [other]
        assert first.deduplicate([other]) == []
        assert not one.in_transaction
        assert not two.in_transaction
    finally:
        one.close()
        two.close()


def test_connection_transaction_is_never_committed_or_rolled_back() -> None:
    module = _module()
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("BEGIN")
        with pytest.raises(module.EventDeduplicationError, match="caller transaction"):
            module.EventDeduplicator(connection=connection, draft_fixture=True)
        assert connection.in_transaction
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
        connection.rollback()
        dedup = module.EventDeduplicator(connection=connection, draft_fixture=True, clock=Clock())
        connection.execute("BEGIN")
        with pytest.raises(module.EventDeduplicationError, match="caller transaction"):
            dedup.register(_event())
        assert connection.in_transaction
        connection.rollback()
        assert not dedup.is_duplicate(_event())
    finally:
        connection.close()


@pytest.mark.parametrize("mode", [True, False])
def test_incompatible_autocommit_mode_is_rejected(mode: bool) -> None:
    module = _module()
    connection = sqlite3.connect(":memory:", autocommit=mode)
    try:
        before = connection.in_transaction
        with pytest.raises(module.EventDeduplicationError, match="legacy"):
            module.EventDeduplicator(connection=connection, draft_fixture=True)
        assert connection.in_transaction is before
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        connection.close()


def test_reopen_with_different_window_is_rejected_without_writes() -> None:
    module = _module()
    connection = sqlite3.connect(":memory:")
    try:
        dedup = module.EventDeduplicator(connection=connection, draft_fixture=True, clock=Clock())
        dedup.register(_event())
        before = connection.total_changes
        with pytest.raises(module.EventDeduplicationError, match="window"):
            module.EventDeduplicator(
                module.DeduplicationWindow(window_seconds=600),
                connection=connection,
                draft_fixture=True,
            )
        assert connection.total_changes == before
        assert dedup.is_duplicate(_event())
    finally:
        connection.close()


def test_sqlite_write_error_rolls_back_eviction_and_registration() -> None:
    module = _module()
    connection = sqlite3.connect(":memory:")
    try:
        dedup = module.EventDeduplicator(connection=connection, draft_fixture=True, clock=Clock())
        dedup.register(_event())
        original = connection.execute("SELECT * FROM event_dedup").fetchall()
        connection.set_authorizer(
            lambda action, table, _column, _database, _trigger: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_INSERT and table == "event_dedup"
                else sqlite3.SQLITE_OK
            )
        )
        with pytest.raises(sqlite3.DatabaseError):
            dedup.register(_event(payload={"value": 2}))
        connection.set_authorizer(None)
        assert not connection.in_transaction
        assert connection.execute("SELECT * FROM event_dedup").fetchall() == original
        assert dedup.is_duplicate(_event())
        assert not dedup.is_duplicate(_event(payload={"value": 2}))
    finally:
        connection.close()


def test_sqlite_contains_only_fixed_hashes_and_timestamps_not_payloads() -> None:
    module = _module()
    connection = sqlite3.connect(":memory:")
    try:
        dedup = module.EventDeduplicator(connection=connection, draft_fixture=True, clock=Clock())
        event = _event(payload={"private": "do-not-persist-this-value"})
        dedup.register(event)
        rows = connection.execute("SELECT * FROM event_dedup").fetchall()
        assert len(rows) == 1
        key, stamp, rank = rows[0]
        assert len(key) == 64 and all(value in "0123456789abcdef" for value in key)
        assert stamp == 1000
        assert rank == 0
        assert "do-not-persist-this-value" not in "\n".join(connection.iterdump())
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
    finally:
        connection.close()


def test_committed_registration_survives_unclean_process_exit(tmp_path: Path) -> None:
    path = tmp_path / "dedup.sqlite"
    script = """
import os, sqlite3, sys
from jarvis.event_dedup import EventDeduplicator
from jarvis.event_schema import Event, EventType, EventPriority
connection = sqlite3.connect(sys.argv[1])
dedup = EventDeduplicator(connection=connection, draft_fixture=True, clock=lambda: 1000)
event = Event('event-1', EventType.OBSERVATION, EventPriority.NORMAL,
              'sensor:offline', {'value': 1}, 1000, 'session-1')
dedup.register(event)
connection.execute('BEGIN IMMEDIATE')
connection.execute('DELETE FROM event_dedup')
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    connection = sqlite3.connect(path)
    try:
        dedup = _module().EventDeduplicator(
            connection=connection, draft_fixture=True, clock=Clock()
        )
        assert dedup.is_duplicate(_event())
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        connection.close()


def test_busy_writer_propagates_without_local_registration(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "dedup.sqlite"
    one = sqlite3.connect(path, timeout=0)
    two = sqlite3.connect(path, timeout=0)
    try:
        dedup = module.EventDeduplicator(connection=one, draft_fixture=True, clock=Clock())
        two.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            dedup.register(_event())
        assert two.in_transaction
        assert not one.in_transaction
        two.rollback()
        assert not dedup.is_duplicate(_event())
    finally:
        one.close()
        two.close()


def test_temporary_and_attached_schema_are_rejected() -> None:
    module = _module()
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TEMP TABLE event_dedup(dedup_key TEXT)")
        with pytest.raises(module.EventDeduplicationError, match="shadow"):
            module.EventDeduplicator(connection=connection, draft_fixture=True)
        assert connection.execute("SELECT name FROM main.sqlite_master").fetchall() == []
        connection.execute("DROP TABLE temp.event_dedup")
        connection.execute("ATTACH DATABASE ':memory:' AS foreign_database")
        with pytest.raises(module.EventDeduplicationError, match="attachments"):
            module.EventDeduplicator(connection=connection, draft_fixture=True)
        assert connection.execute("SELECT name FROM main.sqlite_master").fetchall() == []
    finally:
        connection.close()


def test_failed_commit_does_not_poison_cache() -> None:
    class Connection(sqlite3.Connection):
        fail_commit = False

        def commit(self) -> None:
            if self.fail_commit:
                raise sqlite3.OperationalError("injected commit failure")
            super().commit()

    connection = sqlite3.connect(":memory:", factory=Connection)
    try:
        dedup = _module().EventDeduplicator(
            connection=connection, draft_fixture=True, clock=Clock()
        )
        connection.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            dedup.register(_event())
        assert not connection.in_transaction
        assert connection.execute("SELECT COUNT(*) FROM event_dedup").fetchone() == (0,)
        connection.fail_commit = False
        assert not dedup.is_duplicate(_event())
        dedup.register(_event())
        assert dedup.is_duplicate(_event())
    finally:
        connection.close()


def test_persisted_clock_regression_is_rejected_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "dedup.sqlite"
    connection = sqlite3.connect(path)
    clock = Clock()
    try:
        dedup = _module().EventDeduplicator(connection=connection, draft_fixture=True, clock=clock)
        dedup.register(_event())
    finally:
        connection.close()
    clock.now -= 1
    connection = sqlite3.connect(path)
    try:
        dedup = _module().EventDeduplicator(connection=connection, draft_fixture=True, clock=clock)
        original = connection.execute("SELECT * FROM event_dedup").fetchall()
        with pytest.raises(ValueError, match="backward"):
            dedup.register(_event(payload={"value": 2}))
        assert connection.execute("SELECT * FROM event_dedup").fetchall() == original
        assert not connection.in_transaction
    finally:
        connection.close()
