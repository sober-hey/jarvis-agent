"""K3-001 offline autonomy event schema; no product/K0 migration."""

from __future__ import annotations

import importlib
import importlib.util
import sqlite3
from collections.abc import Generator
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest


def _module() -> Any:
    assert importlib.util.find_spec("jarvis.event_schema") is not None, "event schema is missing"
    return importlib.import_module("jarvis.event_schema")


def test_ddl_creates_events_and_query_indexes() -> None:
    module = _module()
    assert module.DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert module.RUNTIME_ENABLED is False
    with sqlite3.connect(":memory:") as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        schema = module.EventSchema(connection, draft_fixture=True)
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
        schema.create()
        assert [row[1] for row in connection.execute("PRAGMA table_info(events)")] == [
            "id",
            "type",
            "priority",
            "source",
            "payload",
            "created_at",
            "session_id",
            "task_id",
            "parent_event_id",
            "tags",
        ]
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(events)")}
        assert {
            "idx_events_type",
            "idx_events_priority",
            "idx_events_source",
            "idx_events_created_at",
            "idx_events_session_id",
            "idx_events_task_id",
            "idx_events_parent_event_id",
        } <= indexes
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert not connection.in_transaction
    with sqlite3.connect(":memory:") as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(module.EventSchema.ddl())
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
        assert module.EventSchema.ddl() == module.EventSchema.ddl()


def _event(**changes: Any) -> Any:
    module = _module()
    values = {
        "id": "event-1",
        "type": module.EventType.OBSERVATION,
        "priority": module.EventPriority.NORMAL,
        "source": "sensor:offline",
        "payload": {"observed": "离线夹具", "verified": False},
        "created_at": 1000,
        "session_id": "session-1",
    }
    values.update(changes)
    return module.Event(**values)


@pytest.fixture
def schema() -> Generator[Any, None, None]:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        instance = _module().EventSchema(connection, draft_fixture=True)
        instance.create()
        yield instance
    finally:
        connection.close()


@pytest.mark.parametrize(
    "kind", ["OBSERVATION", "DECISION", "ACTION", "REFLECTION", "NOTIFICATION"]
)
def test_all_event_types_round_trip(schema: Any, kind: str) -> None:
    module = _module()
    event = _event(type=module.EventType(kind), task_id="task:opaque", tags=["一", "二"])
    schema.insert(event)
    assert schema.query() == [event]
    assert schema.query(module.EventFilter(type_filter=(module.EventType(kind),))) == [event]
    assert schema.count() == 1
    assert not schema.connection.in_transaction


@pytest.mark.parametrize("priority", ["LOW", "NORMAL", "HIGH", "URGENT"])
def test_all_priorities_and_inclusive_minimum(schema: Any, priority: str) -> None:
    module = _module()
    for level in module.EventPriority:
        schema.insert(_event(id=level.name, priority=level))
    minimum = module.EventPriority[priority]
    filters = module.EventFilter(priority_min=minimum)
    assert {event.priority for event in schema.query(filters)} == {
        level for level in module.EventPriority if level >= minimum
    }
    assert schema.count(filters) == len(schema.query(filters))


def test_filter_intersection_exact_membership_time_bounds_and_stable_order(schema: Any) -> None:
    module = _module()
    events = [
        _event(id="c", created_at=200, tags=("alpha", "beta"), priority=module.EventPriority.HIGH),
        _event(
            id="b", created_at=200, tags=("alpha", "beta"), priority=module.EventPriority.URGENT
        ),
        _event(id="a", created_at=100, tags=("alpha", "beta"), priority=module.EventPriority.HIGH),
        _event(id="old", created_at=99, tags=("alpha", "beta")),
        _event(id="late", created_at=201, tags=("alpha", "beta")),
        _event(id="low", created_at=150, tags=("alpha", "beta")),
        _event(id="type", type=module.EventType.DECISION, tags=("alpha", "beta")),
        _event(id="source", source="sensor:other", tags=("alpha", "beta")),
        _event(id="case", tags=("Alpha", "beta")),
        _event(id="partial", tags=("alphabet", "beta")),
        _event(id="one-tag", tags=("alpha",)),
    ]
    for event in events:
        schema.insert(event)
    filters = module.EventFilter(
        type_filter=[module.EventType.OBSERVATION, module.EventType.REFLECTION],
        priority_min=module.EventPriority.HIGH,
        source_filter=["sensor:offline"],
        time_range=(100, 200),
        tag_filter=["alpha", "beta"],
    )
    before = schema.connection.total_changes
    assert [event.id for event in schema.query(filters)] == ["a", "b", "c"]
    assert schema.count(filters) == 3
    assert schema.connection.total_changes == before
    assert schema.query(module.EventFilter(type_filter=())) == []
    assert schema.count(module.EventFilter(source_filter=())) == 0
    assert schema.count(module.EventFilter(tag_filter=())) == len(events)
    assert schema.count(module.EventFilter(time_range=(100, 100))) == 1
    assert schema.count(module.EventFilter(type_filter=(module.EventType.DECISION,))) == 1
    assert schema.count(module.EventFilter(tag_filter=("alpha", "beta"))) == 8


def test_filter_binds_hostile_sources_and_tags(schema: Any) -> None:
    module = _module()
    hostile = "' OR 1=1; DROP TABLE events; --"
    event = _event(source=hostile, tags=(hostile, "%_"))
    schema.insert(event)
    schema.insert(_event(id="other"))
    filters = module.EventFilter(source_filter=(hostile,), tag_filter=(hostile, "%_"))
    assert schema.query(filters) == [event]
    assert schema.count(filters) == 1
    assert schema.count(module.EventFilter(source_filter=("%",))) == 0
    assert schema.count() == 2


def test_parent_chain_keeps_all_ids_and_restricts_destructive_cascade(schema: Any) -> None:
    module = _module()
    root = _event(id="observation")
    decision = _event(id="decision", type=module.EventType.DECISION, parent_event_id=root.id)
    action = _event(id="action", type=module.EventType.ACTION, parent_event_id=decision.id)
    for event in (root, decision, action):
        schema.insert(event)
    rows = {event.id: event for event in schema.query()}
    assert rows[rows[action.id].parent_event_id].parent_event_id == root.id
    assert schema.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError):
        schema.connection.execute("DELETE FROM events WHERE id = ?", (root.id,))
    schema.connection.rollback()
    assert schema.count() == 3


def test_missing_parent_and_self_parent_are_rejected_without_partial_write(schema: Any) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema.insert(_event(parent_event_id="missing"))
    assert schema.count() == 0
    assert not schema.connection.in_transaction
    with pytest.raises(ValueError):
        _event(parent_event_id="event-1")


def test_json_payload_round_trip_copies_nested_values_and_tags(schema: Any) -> None:
    payload: dict[str, Any] = {
        "文本": '引号"、换行\n',
        "nested": [True, False, None, 3, 2.5, {"key": ["x"]}],
    }
    tags = ["a", "a\"b'c", "%_"]
    event = _event(payload=payload, tags=tags)
    payload["nested"].append("external mutation")
    tags.append("external mutation")
    assert event.payload["nested"][-1] == {"key": ["x"]}
    assert event.tags == ("a", "a\"b'c", "%_")
    with pytest.raises(FrozenInstanceError):
        event.id = "changed"
    schema.insert(event)
    event.payload["nested"].append("post-insert mutation")
    loaded = schema.query()[0]
    assert loaded.payload["nested"][-1] == {"key": ["x"]}
    loaded.payload["nested"].append("read result mutation")
    assert schema.query()[0].payload["nested"][-1] == {"key": ["x"]}


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"id": "a\x00b"},
        {"type": "ACTION"},
        {"priority": 2},
        {"source": " "},
        {"session_id": ""},
        {"task_id": ""},
        {"parent_event_id": " "},
        {"created_at": True},
        {"created_at": -1},
        {"created_at": 1.5},
        {"created_at": 2**63},
        {"tags": "tag"},
        {"tags": (1,)},
        {"tags": (" ",)},
        {"payload": []},
        {"payload": {1: "lossy key"}},
        {"payload": {"nested": {2: 3}}},
        {"payload": {"tuple": (1, 2)}},
        {"payload": {"nan": float("nan")}},
        {"payload": {"inf": float("inf")}},
        {"payload": {"object": object()}},
    ],
)
def test_event_rejects_invalid_or_lossy_fields(changes: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        _event(**changes)


def test_cyclic_payload_is_rejected() -> None:
    payload: dict[str, Any] = {}
    payload["cycle"] = payload
    with pytest.raises(ValueError):
        _event(payload=payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"type_filter": "ACTION"},
        {"type_filter": ("ACTION",)},
        {"priority_min": 2},
        {"source_filter": "sensor"},
        {"source_filter": ("",)},
        {"time_range": (20, 10)},
        {"time_range": (1,)},
        {"time_range": (False, 10)},
        {"time_range": (0, 2**63)},
        {"tag_filter": "tag"},
        {"tag_filter": ("",)},
    ],
)
def test_filter_rejects_invalid_fields(changes: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        _module().EventFilter(**changes)


def test_filter_copies_collections() -> None:
    module = _module()
    kinds, sources, tags, times = [module.EventType.ACTION], ["tool"], ["tag"], [0, 100]
    filters = module.EventFilter(kinds, module.EventPriority.LOW, sources, times, tags)
    kinds.clear()
    sources.clear()
    tags.clear()
    times.clear()
    assert filters.type_filter == (module.EventType.ACTION,)
    assert filters.source_filter == ("tool",)
    assert filters.tag_filter == ("tag",)
    assert filters.time_range == (0, 100)


def test_duplicate_id_and_mutated_invalid_payload_leave_original_unchanged(schema: Any) -> None:
    original = _event()
    schema.insert(original)
    with pytest.raises(sqlite3.IntegrityError):
        schema.insert(replace(original, source="conflict"))
    assert schema.query() == [original]
    invalid = _event(id="invalid")
    invalid.payload["bad"] = float("nan")
    with pytest.raises(ValueError):
        schema.insert(invalid)
    assert schema.count() == 1


def test_opt_in_connection_modes_and_file_database_are_rejected(tmp_path: Path) -> None:
    module = _module()
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(module.EventSchemaError):
            module.EventSchema(connection)
        with pytest.raises(module.EventSchemaError, match="foreign_keys"):
            module.EventSchema(connection, draft_fixture=True)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        with pytest.raises(module.EventSchemaError):
            module.EventSchema(connection, draft_fixture=True)
        connection.row_factory = None
        connection.execute("ATTACH DATABASE ':memory:' AS other")
        with pytest.raises(module.EventSchemaError):
            module.EventSchema(connection, draft_fixture=True)
    finally:
        connection.close()
    connection = sqlite3.connect(tmp_path / "do-not-migrate.db")
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(module.EventSchemaError):
            module.EventSchema(connection, draft_fixture=True)
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        connection.close()


def test_existing_core_events_table_is_never_adopted() -> None:
    module = _module()
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, payload_json TEXT)")
        instance = module.EventSchema(connection, draft_fixture=True)
        with pytest.raises(module.EventSchemaError):
            instance.create()
        for operation in (lambda: instance.insert(_event()), instance.query, instance.count):
            with pytest.raises(module.EventSchemaError):
                operation()
        assert [row[1] for row in connection.execute("PRAGMA table_info(events)")] == [
            "id",
            "payload_json",
        ]
    finally:
        connection.close()


def test_schema_with_matching_columns_but_missing_constraints_is_rejected() -> None:
    module = _module()
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "CREATE TABLE events (id, type, priority, source, payload, created_at, session_id, "
            "task_id, parent_event_id, tags)"
        )
        schema = module.EventSchema(connection, draft_fixture=True)
        for operation in (lambda: schema.insert(_event()), schema.query, schema.count):
            with pytest.raises(module.EventSchemaError):
                operation()
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
    finally:
        connection.close()


def test_missing_lineage_trigger_is_not_adopted(schema: Any) -> None:
    schema.connection.execute("DROP TRIGGER events_lineage_immutable")
    with pytest.raises(_module().EventSchemaError):
        schema.insert(_event())
    assert schema.connection.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)


def test_caller_transactions_not_committed_and_connection_revalidated(schema: Any) -> None:
    module = _module()
    schema.connection.execute("BEGIN")
    with pytest.raises(module.EventSchemaError):
        schema.insert(_event())
    assert schema.connection.in_transaction
    schema.connection.rollback()
    schema.connection.execute("PRAGMA foreign_keys = OFF")
    for operation in (lambda: schema.insert(_event()), schema.query, schema.count):
        with pytest.raises(module.EventSchemaError):
            operation()


def test_ddl_rejects_malformed_json_enums_tags_and_parent_rewrites(schema: Any) -> None:
    module = _module()
    schema.insert(_event())
    for column, bad in (
        ("type", "OTHER"),
        ("priority", 5),
        ("created_at", -1),
        ("payload", "[]"),
        ("payload", "bad-json"),
        ("tags", "{}"),
        ("tags", "[1]"),
        ("tags", '[" "]'),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            schema.connection.execute(f"UPDATE events SET {column} = ?", (bad,))
        schema.connection.rollback()
    schema.insert(_event(id="child", parent_event_id="event-1"))
    with pytest.raises(sqlite3.IntegrityError):
        schema.connection.execute("UPDATE events SET parent_event_id='child' WHERE id='event-1'")
    schema.connection.rollback()
    assert schema.count(module.EventFilter()) == 2


def test_ddl_creation_failure_rolls_back_all_objects() -> None:
    module = _module()

    class FailIndex(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            if sql.startswith("CREATE INDEX"):
                raise sqlite3.OperationalError("injected DDL failure")
            return super().execute(sql, parameters)

    connection = sqlite3.connect(":memory:", factory=FailIndex)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        schema = module.EventSchema(connection, draft_fixture=True)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            schema.create()
        assert not connection.in_transaction
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        connection.close()
