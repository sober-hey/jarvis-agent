"""K3-002 Durable Event Store DRAFT; NOT_FROZEN / NOT_APPROVED.

runtime_enabled=False. Explicit caller-supplied, standalone file-backed SQLite
fixtures only: no K0/product database adoption, packaging/ migration or wiring.
Replay returns untrusted data, never executes events. Durability assumes SQLite's
local-filesystem locking/sync contract; process-crash tests are not power-loss or
Linux/ECS certification. The caller retains connection ownership.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar, cast

from jarvis.event_schema import Event, EventFilter, EventPriority, EventType

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False
_COLUMNS = (
    "id, type, priority, source, payload, created_at, session_id, task_id, parent_event_id, tags"
)


class EventStoreError(RuntimeError):
    """Unsupported database/transaction or incomplete checkpoint."""


class AppendOnlyError(EventStoreError):
    """An existing event may never be updated or removed."""


class EventStore:
    """Append-only audit candidate with a separate monotonically increasing sequence.

    IDs are opaque strings, not clocks. Replay uses commit/append order, not
    created_at or lexical ID order. Each append owns BEGIN IMMEDIATE and commits
    before returning. Duplicate IDs fail, never replace or silently deduplicate.
    Use one connection per thread/process, or one shared store constructed with
    check_same_thread=False. Do not operate on its connection concurrently through
    another object. Busy errors propagate; no hidden retry of unknown commits.

    SQLite FULL synchronizes the WAL commit before acknowledgement. In addition,
    existing database/WAL files are fsynced *before* INSERT, with fullfsync and
    checkpoint_fullfsync enabled for macOS. A pre-write fsync alone would not make
    new writes durable. FULL checkpoint checks its busy/completion status.
    Triggers protect ordinary SQL UPDATE/DELETE/REPLACE, not a malicious owner
    capable of dropping tables/triggers or directly modifying database files.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._lock = threading.RLock()
        with self._lock:
            self._path = self._validate_connection()
            self._require_idle()
            objects = self._objects()
            if objects and objects != self._expected_objects():
                raise EventStoreError("requires an empty or recognized standalone event draft DB")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode != "wal":
                raise EventStoreError("file-backed WAL mode is required")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA fullfsync = ON")
            connection.execute("PRAGMA checkpoint_fullfsync = ON")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
            self._require_durable()
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Recheck after acquiring SQLite's cross-connection writer lock.
                objects = self._objects()
                if not objects:
                    for statement in self._statements():
                        connection.execute(statement)
                elif objects != self._expected_objects():
                    raise EventStoreError("unrecognized standalone event draft schema")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            self._sync_existing()
            self._fsync(self._path.parent)

    def _validate_connection(self) -> Path:
        connection = self.connection
        if (
            connection.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL
            or connection.row_factory is not None
            or connection.text_factory is not str
        ):
            raise EventStoreError("requires explicit transactions, tuple rows and str text")
        databases = connection.execute("PRAGMA database_list").fetchall()
        main = [row for row in databases if row[1] == "main"]
        if (
            len(main) != 1
            or not main[0][2]
            or any(row[1] not in {"main", "temp"} for row in databases)
        ):
            raise EventStoreError("requires a standalone file-backed database")
        if connection.execute("SELECT 1 FROM sqlite_temp_master LIMIT 1").fetchone():
            raise EventStoreError("temporary objects may not shadow event tables")
        return Path(main[0][2])

    def _require_idle(self) -> None:
        if self.connection.in_transaction:
            raise EventStoreError("caller transaction is not allowed")

    def _require_durable(self) -> None:
        self._validate_connection()
        self._require_idle()
        for pragma, expected in (
            ("journal_mode", "wal"),
            ("synchronous", 2),
            ("fullfsync", 1),
            ("checkpoint_fullfsync", 1),
            ("foreign_keys", 1),
        ):
            if self.connection.execute(f"PRAGMA {pragma}").fetchone() != (expected,):
                raise EventStoreError(f"required durability setting changed: {pragma}")

    @staticmethod
    def _statements() -> tuple[str, ...]:
        types = ", ".join(f"'{kind.value}'" for kind in EventType)
        return (
            f"""CREATE TABLE durable_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE CHECK (length(trim(id)) > 0),
                type TEXT NOT NULL CHECK (type IN ({types})),
                priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 4),
                source TEXT NOT NULL CHECK (length(trim(source)) > 0),
                payload TEXT NOT NULL CHECK (CASE WHEN json_valid(payload)
                    THEN json_type(payload) = 'object' ELSE 0 END),
                created_at INTEGER NOT NULL CHECK (created_at >= 0),
                session_id TEXT NOT NULL CHECK (length(trim(session_id)) > 0),
                task_id TEXT,
                parent_event_id TEXT REFERENCES durable_events(id),
                tags TEXT NOT NULL CHECK (CASE WHEN json_valid(tags)
                    THEN json_type(tags) = 'array' ELSE 0 END)
            ) STRICT""",
            "CREATE INDEX durable_events_type ON durable_events (type, sequence)",
            "CREATE INDEX durable_events_source ON durable_events (source, sequence)",
            "CREATE INDEX durable_events_time ON durable_events (created_at, sequence)",
            """CREATE TRIGGER durable_events_no_update BEFORE UPDATE ON durable_events
                BEGIN SELECT RAISE(ABORT, 'event store is append-only'); END""",
            """CREATE TRIGGER durable_events_no_delete BEFORE DELETE ON durable_events
                BEGIN SELECT RAISE(ABORT, 'event store is append-only'); END""",
            """CREATE TRIGGER durable_events_no_replace BEFORE INSERT ON durable_events
                WHEN EXISTS (SELECT 1 FROM durable_events
                             WHERE id = NEW.id OR sequence = NEW.sequence)
                BEGIN SELECT RAISE(ABORT, 'event store is append-only'); END""",
        )

    @classmethod
    def _expected_objects(cls) -> dict[str, str]:
        return {statement.split()[2]: statement for statement in cls._statements()}

    def _objects(self) -> dict[str, str]:
        return dict(
            self.connection.execute(
                "SELECT name, sql FROM main.sqlite_master WHERE name NOT GLOB 'sqlite_*'"
            ).fetchall()
        )

    @staticmethod
    def _fsync(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _sync_existing(self) -> None:
        self._fsync(self._path)
        # While BEGIN IMMEDIATE is held, a writer cannot remove or replace WAL.
        wal = Path(str(self._path) + "-wal")
        try:
            self._fsync(wal)
        except FileNotFoundError:
            pass

    def append(self, event: Event) -> str:
        """Atomically append, fsync existing files before INSERT, return opaque ID.

        A raised commit/I/O error is not a guarantee of non-commit. Reconcile by
        stable event ID with get(); never automatically create a different ID.
        """
        if not isinstance(cast(object, event), Event):
            raise TypeError("expected Event")
        checked = replace(deepcopy(event))
        payload = json.dumps(checked.payload, ensure_ascii=False, allow_nan=False)
        tags = json.dumps(checked.tags, ensure_ascii=False, allow_nan=False)
        with self._lock:
            self._require_durable()
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._sync_existing()
                self.connection.execute(
                    f"INSERT INTO main.durable_events ({_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        checked.id,
                        checked.type.value,
                        int(checked.priority),
                        checked.source,
                        payload,
                        checked.created_at,
                        checked.session_id,
                        checked.task_id,
                        checked.parent_event_id,
                        tags,
                    ),
                )
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
        return checked.id

    @staticmethod
    def _event(row: tuple[Any, ...]) -> Event:
        return Event(
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

    def get(self, event_id: str) -> Event:
        """Return a detached event; unknown IDs raise KeyError."""
        with self._lock:
            row = self.connection.execute(
                f"SELECT {_COLUMNS} FROM main.durable_events WHERE id = ?", (event_id,)
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._event(row)

    def query(self, filter: EventFilter | None = None) -> list[Event]:
        """Return detached matching events in append order; predicates use AND."""
        if filter is None:
            filter = EventFilter()
        if not isinstance(cast(object, filter), EventFilter):
            raise TypeError("expected EventFilter")
        checked = replace(deepcopy(filter))
        clauses: list[str] = []
        parameters: list[object] = []
        for column, values in (("type", checked.type_filter), ("source", checked.source_filter)):
            if values is not None:
                if not values:
                    return []
                clauses.append(f"{column} IN ({', '.join('?' for _ in values)})")
                parameters.extend(values)
        if checked.priority_min is not None:
            clauses.append("priority >= ?")
            parameters.append(int(checked.priority_min))
        if checked.time_range is not None:
            clauses.append("created_at BETWEEN ? AND ?")
            parameters.extend(checked.time_range)
        for tag in checked.tag_filter or ():
            clauses.append("EXISTS (SELECT 1 FROM json_each(durable_events.tags) WHERE value = ?)")
            parameters.append(tag)
        sql = f"SELECT {_COLUMNS} FROM main.durable_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY sequence"
        with self._lock:
            rows = self.connection.execute(sql, parameters).fetchall()
        return [self._event(row) for row in rows]

    def replay(self, since_id: str | None = None) -> Iterator[Event]:
        """Inclusive ID replay through a high-watermark fixed at the call.

        Batches release SQLite read cursors immediately: suspended iterators do
        not pin WAL checkpoints. New appends after this call are for a later
        replay. Unknown cursors fail rather than silently restarting history.
        """
        with self._lock:
            high = self.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM main.durable_events"
            ).fetchone()[0]
            start = 0
            if since_id is not None:
                row = self.connection.execute(
                    "SELECT sequence FROM main.durable_events WHERE id = ?", (since_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(since_id)
                start = row[0] - 1

        def iterate() -> Iterator[Event]:
            cursor = start
            while cursor < high:
                with self._lock:
                    rows = self.connection.execute(
                        f"SELECT sequence, {_COLUMNS} FROM main.durable_events "
                        "WHERE sequence > ? AND sequence <= ? ORDER BY sequence LIMIT 128",
                        (cursor, high),
                    ).fetchall()
                if not rows:
                    return
                for row in rows:
                    cursor = row[0]
                    yield self._event(row[1:])

        return iterate()

    def checkpoint(self) -> None:
        """FULL WAL checkpoint; busy/incomplete results are errors, not success."""
        with self._lock:
            self._require_durable()
            busy, log_frames, completed = self.connection.execute(
                "PRAGMA main.wal_checkpoint(FULL)"
            ).fetchone()
            if busy or log_frames != completed:
                raise EventStoreError("WAL checkpoint is busy or incomplete")
            self._sync_existing()
            self._fsync(self._path.parent)

    def update(self, event: Event) -> None:
        """Explicitly reject mutation; append a new event instead."""
        raise AppendOnlyError("event store is append-only")

    def delete(self, event_id: str) -> None:
        """Explicitly reject deletion; retention policy is outside this draft."""
        raise AppendOnlyError("event store is append-only")
