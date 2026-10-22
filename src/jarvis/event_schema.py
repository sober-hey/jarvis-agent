"""K3-001 DRAFT; NOT_FROZEN, NOT_APPROVED, runtime_enabled=False.

Standalone offline autonomy events, NOT K0's execution-audit ``events`` table.
All timestamps/ranges are nonnegative UTC epoch milliseconds. No runtime wiring,
production migration, event bus, deduplication or execution permission is provided.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum
from typing import Any, cast

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


class EventType(StrEnum):
    """Autonomy record categories, never proof that an external effect occurred."""

    OBSERVATION = "OBSERVATION"
    DECISION = "DECISION"
    ACTION = "ACTION"
    REFLECTION = "REFLECTION"
    NOTIFICATION = "NOTIFICATION"


class EventPriority(IntEnum):
    """Ascending stored rank, not a permission or delivery guarantee."""

    LOW = 1
    NORMAL = 2
    HIGH = 3
    URGENT = 4


def _text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be nonempty text without NUL")
    value.encode("utf-8", errors="strict")


def _millis(name: str, value: object) -> None:
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError(f"{name} must be an exact nonnegative SQLite integer (UTC ms)")


def _enum[T](value: object, expected: type[T]) -> T:
    if not isinstance(value, expected):
        raise TypeError(f"expected {expected.__name__}")
    return value


def _strings(name: str, values: tuple[str, ...]) -> tuple[str, ...]:
    if type(values) not in (tuple, list):
        raise TypeError(f"{name} must be a tuple or list of strings")
    for value in values:
        _text(name, value)
    return tuple(values)


def _json_value(value: Any, ancestors: set[int]) -> None:
    """Accept only JSON-native values; reject cycles and lossy key coercions."""
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        value.encode("utf-8", errors="strict")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("payload numbers must be finite")
        return
    if type(value) not in (dict, list):
        raise TypeError("payload must contain only JSON-native values")
    identity = id(value)
    if identity in ancestors:
        raise ValueError("cyclic payload is not JSON")
    ancestors.add(identity)
    try:
        if type(value) is dict:
            for key, item in cast(dict[object, Any], value).items():
                if type(key) is not str:
                    raise TypeError("payload object keys must be strings")
                _json_value(key, ancestors)
                _json_value(item, ancestors)
        else:
            for item in value:
                _json_value(item, ancestors)
    finally:
        ancestors.remove(identity)


def _payload_json(payload: dict[str, Any]) -> str:
    if type(payload) is not dict:
        raise TypeError("payload must be a dict")
    _json_value(payload, set())
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)


@dataclass(frozen=True, slots=True)
class Event:
    """Candidate record, not a verified fact, execution grant or delivery receipt.

    Payload must be a JSON-native dict. It is deep-copied at construction, then
    remains a mutable dict (frozen fields do not make its contents immutable).
    ``insert`` revalidates/snapshots it; query results never alias stored payloads.
    IDs/source/session/task are opaque, unauthenticated references. Causal order
    is represented by parent links, not by potentially regressing wall clocks.
    """

    id: str
    type: EventType
    priority: EventPriority
    source: str
    payload: dict[str, Any]
    created_at: int
    session_id: str
    task_id: str | None = None
    parent_event_id: str | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("id", "source", "session_id"):
            _text(name, getattr(self, name))
        _enum(self.type, EventType)
        _enum(self.priority, EventPriority)
        _millis("created_at", self.created_at)
        for name in ("task_id", "parent_event_id"):
            if (value := getattr(self, name)) is not None:
                _text(name, value)
        if self.parent_event_id == self.id:
            raise ValueError("event cannot be its own parent")
        object.__setattr__(self, "tags", _strings("tags", self.tags))
        object.__setattr__(self, "payload", json.loads(_payload_json(self.payload)))


@dataclass(frozen=True, slots=True)
class EventFilter:
    """AND across fields; OR within type/source and ALL exact case-sensitive tags.

    None disables a field; empty type/source selections match nothing, while an
    empty tag selection is vacuously true. Time bounds are inclusive UTC ms.
    """

    type_filter: tuple[EventType, ...] | None = None
    priority_min: EventPriority | None = None
    source_filter: tuple[str, ...] | None = None
    time_range: tuple[int, int] | None = None
    tag_filter: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.type_filter is not None:
            if type(self.type_filter) not in (tuple, list):
                raise TypeError("type_filter must be a tuple or list of EventType")
            for kind in self.type_filter:
                _enum(kind, EventType)
            object.__setattr__(self, "type_filter", tuple(self.type_filter))
        if self.priority_min is not None:
            _enum(self.priority_min, EventPriority)
        for name in ("source_filter", "tag_filter"):
            if (values := getattr(self, name)) is not None:
                object.__setattr__(self, name, _strings(name, values))
        if self.time_range is not None:
            if type(self.time_range) not in (tuple, list) or len(self.time_range) != 2:
                raise ValueError("time_range must contain exactly two inclusive bounds")
            start, end = self.time_range
            _millis("range start", start)
            _millis("range end", end)
            if end < start:
                raise ValueError("time_range end precedes start")
            object.__setattr__(self, "time_range", (start, end))


class EventSchemaError(RuntimeError):
    """Unsupported connection or non-standalone draft database operation."""


class EventSchema:
    """Explicit in-memory fixture, isolated from K0's incompatible events table.

    Construction is read-only. Writes own their transactions and never commit a
    caller transaction. Caller must enable foreign_keys before construction.
    Parent rows must already exist; deletion is RESTRICTED to preserve lineage.
    """

    def __init__(self, connection: sqlite3.Connection, *, draft_fixture: bool = False) -> None:
        if draft_fixture is not True:
            raise EventSchemaError("explicit offline draft_fixture=True required")
        self.connection = connection
        self._validate_connection()

    def _validate_connection(self) -> None:
        connection = self.connection
        if (
            connection.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL
            or connection.row_factory is not None
            or connection.text_factory is not str
        ):
            raise EventSchemaError("requires legacy transactions, tuple rows and str text")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise EventSchemaError("foreign_keys must be enabled by the caller")
        if any(
            row[1] not in {"main", "temp"} or row[2]
            for row in connection.execute("PRAGMA database_list")
        ):
            raise EventSchemaError("DRAFT only accepts standalone in-memory databases")
        if connection.execute("SELECT 1 FROM sqlite_temp_master LIMIT 1").fetchone():
            raise EventSchemaError("temporary objects may not shadow event tables")

    @contextmanager
    def _write(self) -> Generator[None]:
        self._validate_connection()
        if self.connection.in_transaction:
            raise EventSchemaError("caller transaction is not allowed")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def create(self) -> None:
        """Create only in an empty memory database; never adopt a K0/product DB."""
        with self._write():
            if self.connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                raise EventSchemaError("creation requires an empty database")
            for statement in self._statements():
                self.connection.execute(statement)

    def _validate_schema(self) -> None:
        self._validate_connection()
        # Exact candidate objects, including constraints/triggers, not just names.
        # SQLite retains CREATE SQL without its final semicolon. Whitespace is
        # ignored; this is a draft-layout guard, not protection from a DB owner.
        actual = {
            " ".join(row[0].split())
            for row in self.connection.execute(
                "SELECT sql FROM main.sqlite_master WHERE sql IS NOT NULL"
            )
        }
        expected = {" ".join(statement.split()) for statement in self._statements()}
        if actual != expected:
            raise EventSchemaError("not a standalone autonomy event draft schema")

    def insert(self, event: Event) -> None:
        """Append one record atomically; duplicate IDs fail, never replace history."""
        _enum(event, Event)
        checked = replace(event)  # revalidate/snapshot mutable payload before BEGIN
        with self._write():
            self._validate_schema()
            self.connection.execute(
                "INSERT INTO main.events (id, type, priority, source, payload, created_at, "
                "session_id, task_id, parent_event_id, tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    checked.id,
                    checked.type.value,
                    checked.priority.value,
                    checked.source,
                    _payload_json(checked.payload),
                    checked.created_at,
                    checked.session_id,
                    checked.task_id,
                    checked.parent_event_id,
                    json.dumps(checked.tags, ensure_ascii=False, allow_nan=False),
                ),
            )

    @staticmethod
    def _predicate(filters: EventFilter | None) -> tuple[str, tuple[str | int, ...]]:
        if filters is None:
            filters = EventFilter()
        _enum(filters, EventFilter)
        predicates: list[str] = []
        parameters: list[str | int] = []
        for column, values in (("type", filters.type_filter), ("source", filters.source_filter)):
            if values is not None:
                if not values:
                    predicates.append("0")
                else:
                    placeholders = ", ".join("?" for _ in values)
                    predicates.append(f"e.{column} IN ({placeholders})")
                    parameters.extend(
                        value.value if isinstance(value, EventType) else value for value in values
                    )
        if filters.priority_min is not None:
            predicates.append("e.priority >= ?")
            parameters.append(filters.priority_min.value)
        if filters.time_range is not None:
            predicates.append("e.created_at BETWEEN ? AND ?")
            parameters.extend(filters.time_range)
        for tag in filters.tag_filter or ():
            # Tags are exact JSON string members, not SQL LIKE/substring patterns.
            predicates.append(
                "EXISTS (SELECT 1 FROM json_each(e.tags) AS tag "
                "WHERE tag.type = 'text' AND tag.value = ?)"
            )
            parameters.append(tag)
        return " AND ".join(predicates) or "1", tuple(parameters)

    def query(self, filters: EventFilter | None = None) -> list[Event]:
        """Read without writes, ordered by (created_at, id), not causality/priority."""
        self._validate_schema()
        predicate, parameters = self._predicate(filters)
        rows = self.connection.execute(
            "SELECT e.id, e.type, e.priority, e.source, e.payload, e.created_at, "
            "e.session_id, e.task_id, e.parent_event_id, e.tags FROM main.events AS e "
            f"WHERE {predicate} ORDER BY e.created_at, e.id",
            parameters,
        ).fetchall()
        return [
            Event(
                id=row[0],
                type=EventType(row[1]),
                priority=EventPriority(row[2]),
                source=row[3],
                payload=json.loads(row[4]),
                created_at=row[5],
                session_id=row[6],
                task_id=row[7],
                parent_event_id=row[8],
                tags=tuple(json.loads(row[9])),
            )
            for row in rows
        ]

    def count(self, filters: EventFilter | None = None) -> int:
        """Count with exactly the same predicate as query, without loading payloads."""
        self._validate_schema()
        predicate, parameters = self._predicate(filters)
        return self.connection.execute(
            f"SELECT COUNT(*) FROM main.events AS e WHERE {predicate}", parameters
        ).fetchone()[0]

    @staticmethod
    def _statements() -> tuple[str, ...]:
        types = ", ".join(f"'{kind.value}'" for kind in EventType)
        return (
            f"""CREATE TABLE events (
                id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(id)) > 0),
                type TEXT NOT NULL CHECK (type IN ({types})),
                priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 4),
                source TEXT NOT NULL CHECK (length(trim(source)) > 0),
                payload TEXT NOT NULL CHECK (CASE WHEN json_valid(payload)
                    THEN json_type(payload) = 'object' ELSE 0 END),
                created_at INTEGER NOT NULL
                    CHECK (typeof(created_at) = 'integer' AND created_at >= 0),
                session_id TEXT NOT NULL CHECK (length(trim(session_id)) > 0),
                task_id TEXT CHECK (task_id IS NULL OR length(trim(task_id)) > 0),
                parent_event_id TEXT REFERENCES events(id) ON DELETE RESTRICT ON UPDATE RESTRICT
                    CHECK (parent_event_id IS NULL OR parent_event_id != id),
                tags TEXT NOT NULL DEFAULT '[]' CHECK (CASE WHEN json_valid(tags)
                    THEN json_type(tags) = 'array' ELSE 0 END)
            ) STRICT""",
            *(
                f"""CREATE TRIGGER events_tags_{action.lower()}
                    BEFORE {action} ON events
                    WHEN EXISTS (SELECT 1 FROM json_each(NEW.tags)
                        WHERE type != 'text' OR length(trim(value)) = 0)
                    BEGIN SELECT RAISE(ABORT, 'tags must be nonempty strings'); END"""
                for action in ("INSERT", "UPDATE")
            ),
            """CREATE TRIGGER events_lineage_immutable
                BEFORE UPDATE OF id, parent_event_id ON events
                WHEN NEW.id IS NOT OLD.id OR NEW.parent_event_id IS NOT OLD.parent_event_id
                BEGIN SELECT RAISE(ABORT, 'event lineage is immutable'); END""",
            *(
                f"CREATE INDEX idx_events_{column} ON events ({column}, created_at, id)"
                for column in (
                    "type",
                    "priority",
                    "source",
                    "session_id",
                    "task_id",
                    "parent_event_id",
                )
            ),
            "CREATE INDEX idx_events_created_at ON events (created_at, id)",
        )

    @classmethod
    def ddl(cls) -> str:
        """Deterministic candidate SQL; caller must enable foreign_keys separately."""
        return ";\n".join(cls._statements()) + ";\n"
