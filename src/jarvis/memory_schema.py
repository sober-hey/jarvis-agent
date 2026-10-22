"""K2-001 DRAFT; NOT_FROZEN, NOT_APPROVED, runtime_enabled=False.

Offline memory layout candidate only: no packaging/ database or runtime wiring.
All timestamps are nonnegative UTC epoch milliseconds (including names without
an ``_ms`` suffix). Version 1 is a synthetic predecessor, not a deployed schema.
Source is an opaque provenance reference, never a verification/trust assertion.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


class MemoryType(StrEnum):
    """Storage categories; content in every category remains untrusted data."""

    WORKING = "WORKING"
    EPISODIC = "EPISODIC"
    SEMANTIC = "SEMANTIC"
    PREFERENCE = "PREFERENCE"
    PROCEDURAL = "PROCEDURAL"


class MemorySchemaError(RuntimeError):
    """Unsupported connection, schema version or draft database operation."""


def _integer(name: str, value: int, minimum: int = 0, maximum: int = 2**63 - 1) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an exact integer in [{minimum}, {maximum}]")


def _text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be nonempty text without NUL")


def _require_type[T](value: object, expected: type[T]) -> T:
    if not isinstance(value, expected):
        raise TypeError(f"expected {expected.__name__}")
    return value


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """Immutable memory candidate; UTC epoch ms and exact, case-sensitive tags.

    ``source`` should identify the original record (e.g. event/message reference),
    not merely a channel name. It is not authenticated here. No inference is
    promoted to a verified fact, and PROCEDURAL content is never executed.
    """

    id: str
    type: MemoryType
    content: str
    source: str
    created_at: int
    last_accessed_at: int
    access_count: int = 0
    importance: int = 3
    tags: tuple[str, ...] = ()
    embedding_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "content", "source"):
            _text(name, getattr(self, name))
        _require_type(self.type, MemoryType)
        _integer("created_at", self.created_at)
        _integer("last_accessed_at", self.last_accessed_at, self.created_at)
        _integer("access_count", self.access_count)
        _integer("importance", self.importance, 1, 5)
        if type(self.tags) not in (tuple, list):
            raise TypeError("tags must be a tuple or list of strings")
        for tag in self.tags:
            _text("tag", tag)
        object.__setattr__(self, "tags", tuple(self.tags))
        if self.embedding_id is not None:
            _text("embedding_id", self.embedding_id)


class MemorySchema:
    """Standalone in-memory draft schema; never changes K0 or PRAGMA user_version.

    Construction does not create/migrate anything. ``create`` and ``insert``
    explicitly own their transactions and reject a caller-owned transaction.
    """

    VERSION = 2

    def __init__(self, connection: sqlite3.Connection, *, draft_fixture: bool = False) -> None:
        if draft_fixture is not True:
            raise MemorySchemaError("explicit offline draft_fixture=True required")
        self.connection = connection
        self._validate_connection()

    def _validate_connection(self) -> None:
        connection = self.connection
        if (
            connection.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL
            or connection.row_factory is not None
            or connection.text_factory is not str
        ):
            raise MemorySchemaError("requires legacy transactions, tuple rows and str text")
        databases = connection.execute("PRAGMA database_list").fetchall()
        if any(row[1] not in {"main", "temp"} or row[2] for row in databases):
            raise MemorySchemaError("DRAFT only accepts standalone in-memory databases")
        if connection.execute("SELECT name FROM sqlite_temp_master LIMIT 1").fetchone():
            raise MemorySchemaError("temporary objects may not shadow memory tables")

    @contextmanager
    def _write(self) -> Generator[None]:
        self._validate_connection()
        if self.connection.in_transaction:
            raise MemorySchemaError("caller transaction is not allowed")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _version(self) -> int:
        self._validate_connection()
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if tables != {"memories", "memory_schema_version"}:
            raise MemorySchemaError("not a standalone memory draft schema")
        rows = self.connection.execute(
            "SELECT singleton_id, version FROM memory_schema_version"
        ).fetchall()
        if rows not in ([(1, 1)], [(1, 2)]):
            raise MemorySchemaError("unknown memory schema version")
        return rows[0][1]

    def create(self, version: int = VERSION) -> None:
        """Create a fixture in an empty database; existing objects are not adopted."""
        statements = self._statements(version)
        with self._write():
            if self.connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                raise MemorySchemaError("creation requires an empty database")
            for statement in statements:
                self.connection.execute(statement)

    def insert(self, entry: MemoryEntry) -> None:
        """Append a v2 memory; conflicting IDs fail rather than replacing history."""
        _require_type(entry, MemoryEntry)
        with self._write():
            if self._version() != self.VERSION:
                raise MemorySchemaError("migrate to v2 before inserting MemoryEntry")
            self.connection.execute(
                "INSERT INTO memories (id, type, content, source, created_at, "
                "last_accessed_at, access_count, importance, tags, embedding_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.id,
                    entry.type.value,
                    entry.content,
                    entry.source,
                    entry.created_at,
                    entry.last_accessed_at,
                    entry.access_count,
                    entry.importance,
                    json.dumps(entry.tags, ensure_ascii=False, allow_nan=False),
                    entry.embedding_id,
                ),
            )

    def migrate(self, from_version: int = 1, to_version: int = 2) -> bool:
        """Atomically add nullable embedding_id to synthetic v1; return False on replay.

        This is NOT K0 schema v1→v2 and never adopts/migrates an existing product
        database. All original columns and JSON bytes remain untouched.
        """
        if type(from_version) is not int or type(to_version) is not int:
            raise ValueError("migration versions must be exact integers")
        if (from_version, to_version) != (1, 2):
            raise ValueError("only memory fixture migration v1 to v2 is supported")
        with self._write():
            current = self._version()
            columns = tuple(
                row[1] for row in self.connection.execute("PRAGMA table_info(memories)")
            )
            expected = (
                "id",
                "type",
                "content",
                "source",
                "created_at",
                "last_accessed_at",
                "access_count",
                "importance",
                "tags",
            )
            if current == 2:
                if columns != (*expected, "embedding_id"):
                    raise MemorySchemaError("v2 layout does not match its version")
                return False
            if columns != expected:
                raise MemorySchemaError("v1 layout does not match its version")
            self.connection.execute(
                "ALTER TABLE memories ADD COLUMN embedding_id TEXT "
                "CHECK (embedding_id IS NULL OR (typeof(embedding_id) = 'text' "
                "AND length(trim(embedding_id)) > 0))"
            )
            changed = self.connection.execute(
                "UPDATE memory_schema_version SET version = 2 "
                "WHERE singleton_id = 1 AND version = 1"
            ).rowcount
            if changed != 1:
                raise MemorySchemaError("migration version update did not match one row")
        return True

    def _query(self, predicate: str, parameters: tuple[str | int, ...]) -> list[MemoryEntry]:
        if self._version() != self.VERSION:
            raise MemorySchemaError("migrate to v2 before querying MemoryEntry")
        # Predicate fragments are private constants; every caller value is bound.
        rows = self.connection.execute(
            "SELECT id, type, content, source, created_at, last_accessed_at, "
            "access_count, importance, tags, embedding_id FROM memories "
            f"WHERE {predicate} ORDER BY created_at, id",
            parameters,
        ).fetchall()
        return [
            MemoryEntry(
                id=row[0],
                type=MemoryType(row[1]),
                content=row[2],
                source=row[3],
                created_at=row[4],
                last_accessed_at=row[5],
                access_count=row[6],
                importance=row[7],
                tags=tuple(json.loads(row[8])),
                embedding_id=row[9],
            )
            for row in rows
        ]

    def by_type(self, memory_type: MemoryType) -> list[MemoryEntry]:
        """Read one category; never refresh access metadata implicitly."""
        _require_type(memory_type, MemoryType)
        return self._query("type = ?", (memory_type.value,))

    def by_tag(self, tag: str) -> list[MemoryEntry]:
        """Exact, case-sensitive JSON array membership, not substring/LIKE search.

        JSON tags have no B-tree membership index in this draft. FTS5/retrieval
        and a normalized tag index are explicitly outside K2-001.
        """
        _text("tag", tag)
        return self._query(
            "EXISTS (SELECT 1 FROM json_each(memories.tags) AS tag "
            "WHERE tag.type = 'text' AND tag.value = ?)",
            (tag,),
        )

    def by_time_range(self, start_ms: int, end_ms: int) -> list[MemoryEntry]:
        """Inclusive range on created_at (UTC epoch milliseconds)."""
        _integer("start_ms", start_ms)
        _integer("end_ms", end_ms, start_ms)
        return self._query("created_at BETWEEN ? AND ?", (start_ms, end_ms))

    def by_importance(self, minimum: int, maximum: int = 5) -> list[MemoryEntry]:
        """Inclusive range over stored integer importance, NOT decayed scores."""
        _integer("minimum", minimum, 1, 5)
        _integer("maximum", maximum, minimum, 5)
        return self._query("importance BETWEEN ? AND ?", (minimum, maximum))

    def by_source(self, source: str) -> list[MemoryEntry]:
        """Exact provenance reference match; not a source authenticity check."""
        _text("source", source)
        return self._query("source = ?", (source,))

    @staticmethod
    def decayed_importance(
        entry: MemoryEntry,
        *,
        now_ms: int,
        half_life_ms: int = 30 * 24 * 60 * 60 * 1000,
    ) -> float:
        """Pure candidate score max(1, importance * 2**(-idle_ms/half_life_ms)).

        Idle time starts at last_accessed_at; backward clocks clamp to zero.
        The default 30-day half-life is a configurable draft, not a retention
        rule. No stored importance/access metadata is changed, and no deletion
        or automatic periodic execution is performed.
        """
        _require_type(entry, MemoryEntry)
        _integer("now_ms", now_ms)
        _integer("half_life_ms", half_life_ms, 1)
        idle_ms = max(0, now_ms - entry.last_accessed_at)
        return max(1.0, entry.importance * 2.0 ** (-idle_ms / half_life_ms))

    @staticmethod
    def _statements(version: int) -> tuple[str, ...]:
        if type(version) is not int or version not in (1, 2):
            raise ValueError("supported memory schema versions are 1 and 2")
        types = ", ".join(f"'{kind.value}'" for kind in MemoryType)
        embedding = (
            ", embedding_id TEXT CHECK (embedding_id IS NULL OR "
            "(typeof(embedding_id) = 'text' AND length(trim(embedding_id)) > 0))"
            if version == 2
            else ""
        )
        table = f"""CREATE TABLE memories (
            id TEXT PRIMARY KEY NOT NULL
                CHECK (typeof(id) = 'text' AND length(trim(id)) > 0),
            type TEXT NOT NULL CHECK (type IN ({types})),
            content TEXT NOT NULL
                CHECK (typeof(content) = 'text' AND length(trim(content)) > 0),
            source TEXT NOT NULL
                CHECK (typeof(source) = 'text' AND length(trim(source)) > 0),
            created_at INTEGER NOT NULL
                CHECK (typeof(created_at) = 'integer' AND created_at >= 0),
            last_accessed_at INTEGER NOT NULL
                CHECK (typeof(last_accessed_at) = 'integer'
                    AND last_accessed_at >= created_at),
            access_count INTEGER NOT NULL DEFAULT 0
                CHECK (typeof(access_count) = 'integer' AND access_count >= 0),
            importance INTEGER NOT NULL DEFAULT 3
                CHECK (typeof(importance) = 'integer' AND importance BETWEEN 1 AND 5),
            tags TEXT NOT NULL DEFAULT '[]'
                CHECK (CASE WHEN json_valid(tags)
                    THEN json_type(tags) = 'array' ELSE 0 END)
            {embedding}
        )"""
        triggers = tuple(
            f"""CREATE TRIGGER memories_tags_{action.lower()}
                BEFORE {action} ON memories
                WHEN EXISTS (SELECT 1 FROM json_each(NEW.tags)
                    WHERE type != 'text' OR length(trim(value)) = 0)
                BEGIN SELECT RAISE(ABORT, 'tags must be nonempty strings'); END"""
            for action in ("INSERT", "UPDATE")
        )
        return (
            "CREATE TABLE memory_schema_version ("
            "singleton_id INTEGER PRIMARY KEY NOT NULL CHECK (singleton_id = 1), "
            "version INTEGER NOT NULL CHECK (typeof(version) = 'integer' AND version IN (1, 2)))",
            table,
            *(
                f"CREATE INDEX idx_memories_{column} ON memories ({column}, id)"
                for column in ("type", "source", "created_at", "importance", "last_accessed_at")
            ),
            *triggers,
            f"INSERT INTO memory_schema_version (singleton_id, version) VALUES (1, {version})",
        )

    @classmethod
    def ddl(cls, version: int = VERSION) -> str:
        """Return deterministic standalone SQL; execution is an explicit caller action."""
        return ";\n".join(cls._statements(version)) + ";\n"
