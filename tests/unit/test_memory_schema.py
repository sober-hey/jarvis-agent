"""K2-001 offline memory schema candidate; not a production migration."""

from __future__ import annotations

import importlib
import sqlite3
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest


def _module() -> Any:
    return importlib.import_module("jarvis.memory_schema")


def _entry(**changes: Any) -> Any:
    module = _module()
    values = {
        "id": "memory-1",
        "type": module.MemoryType.SEMANTIC,
        "content": "离线记忆 fixture",
        "source": "event:fixture-1",
        "created_at": 1000,
        "last_accessed_at": 2000,
        "access_count": 2,
        "importance": 4,
        "tags": ("用户偏好", "a\"quoted'标签", "%_"),
        "embedding_id": "vector:1",
    }
    values.update(changes)
    return module.MemoryEntry(**values)


@pytest.fixture
def schema() -> Any:
    module = _module()
    with sqlite3.connect(":memory:") as connection:
        instance = module.MemorySchema(connection, draft_fixture=True)
        instance.create()
        yield instance


def test_entry_is_immutable_with_copied_tags() -> None:
    tags = ["保留"]
    entry = _entry(tags=tags)
    tags.append("not retained")
    assert entry.tags == ("保留",)
    with pytest.raises(FrozenInstanceError):
        entry.content = "changed"


@pytest.mark.parametrize("kind", ["WORKING", "EPISODIC", "SEMANTIC", "PREFERENCE", "PROCEDURAL"])
def test_insert_preserves_each_memory_type(schema: Any, kind: str) -> None:
    entry = _entry(type=_module().MemoryType(kind))
    schema.insert(entry)
    row = schema.connection.execute("SELECT * FROM memories").fetchone()
    assert row[:8] == (entry.id, kind, entry.content, entry.source, 1000, 2000, 2, 4)
    assert row[9] == "vector:1"
    assert schema.by_type(_module().MemoryType(kind)) == [entry]
    assert not schema.connection.in_transaction


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"type": "SEMANTIC"},
        {"content": " "},
        {"source": ""},
        {"created_at": -1},
        {"created_at": True},
        {"created_at": 1.5},
        {"created_at": 2**63},
        {"last_accessed_at": 999},
        {"last_accessed_at": False},
        {"access_count": -1},
        {"access_count": True},
        {"importance": 0},
        {"importance": 6},
        {"importance": True},
        {"importance": 2.0},
        {"tags": "not-array"},
        {"tags": ("",)},
        {"tags": (1,)},
        {"embedding_id": ""},
        {"embedding_id": 3},
    ],
)
def test_entry_rejects_invalid_fields(changes: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        _entry(**changes)


def test_duplicate_insert_never_overwrites_original(schema: Any) -> None:
    entry = _entry()
    schema.insert(entry)
    with pytest.raises(sqlite3.IntegrityError):
        schema.insert(replace(entry, content="conflict"))
    assert schema.connection.execute("SELECT content FROM memories").fetchone() == (entry.content,)


def test_fixture_opt_in_and_persistent_database_are_rejected(tmp_path: Any) -> None:
    module = _module()
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(module.MemorySchemaError):
            module.MemorySchema(connection)
    with sqlite3.connect(tmp_path / "fixture.db") as connection:
        with pytest.raises(module.MemorySchemaError):
            module.MemorySchema(connection, draft_fixture=True)


def test_create_refuses_existing_core_tables() -> None:
    module = _module()
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE tasks (id TEXT)")
        instance = module.MemorySchema(connection, draft_fixture=True)
        with pytest.raises(module.MemorySchemaError):
            instance.create()
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("tasks",)]


def test_insert_rejects_caller_transaction_without_committing_it(schema: Any) -> None:
    schema.connection.execute("BEGIN")
    with pytest.raises(_module().MemorySchemaError):
        schema.insert(_entry())
    assert schema.connection.in_transaction
    schema.connection.rollback()


def test_query_filters_and_stable_order_without_access_writes(schema: Any) -> None:
    module = _module()
    old = _entry(
        id="z",
        type=module.MemoryType.WORKING,
        created_at=100,
        last_accessed_at=100,
        importance=2,
        source="chat:old",
        tags=("cat",),
    )
    second = _entry(id="b", created_at=200, importance=3, tags=("catalog",))
    first = _entry(id="a", created_at=200, importance=4, tags=("cat", "cat", "%_"))
    newest = _entry(id="c", created_at=300, importance=5, source="chat:old", tags=())
    for entry in (old, second, newest, first):
        schema.insert(entry)
    before = list(schema.connection.iterdump())
    assert schema.by_type(module.MemoryType.SEMANTIC) == [first, second, newest]
    assert schema.by_type(module.MemoryType.PROCEDURAL) == []
    assert schema.by_tag("cat") == [old, first]  # Exact match, duplicate tag = one row.
    assert schema.by_tag("%_") == [first]  # No LIKE wildcard semantics.
    assert schema.by_tag("CAT") == []
    assert schema.by_time_range(200, 300) == [first, second, newest]  # Inclusive, created_at.
    assert schema.by_time_range(201, 299) == []
    assert schema.by_importance(3, 4) == [first, second]
    assert schema.by_importance(4) == [first, newest]  # Minimum inclusive; max defaults to 5.
    assert schema.by_source("chat:old") == [old, newest]
    assert schema.by_source("chat:") == []
    assert schema.by_source("'; DROP TABLE memories; --") == []
    assert list(schema.connection.iterdump()) == before


@pytest.mark.parametrize(
    "method, args",
    [
        ("by_type", ("SEMANTIC",)),
        ("by_tag", ("",)),
        ("by_source", ("",)),
        ("by_time_range", (3, 2)),
        ("by_time_range", (-1, 2)),
        ("by_time_range", (True, 2)),
        ("by_importance", (0,)),
        ("by_importance", (6,)),
        ("by_importance", (4, 3)),
        ("by_importance", (2.0,)),
    ],
)
def test_query_rejects_invalid_filters(schema: Any, method: str, args: tuple[Any, ...]) -> None:
    with pytest.raises((TypeError, ValueError)):
        getattr(schema, method)(*args)


@pytest.mark.parametrize("half_lives, expected", [(0, 4.0), (1, 2.0), (2, 1.0), (100, 1.0)])
def test_decay_calculates_half_life_floor_without_mutating_entry(
    half_lives: int,
    expected: float,
) -> None:
    entry = _entry()
    before = replace(entry)
    result = _module().MemorySchema.decayed_importance(
        entry,
        now_ms=entry.last_accessed_at + half_lives * 1000,
        half_life_ms=1000,
    )
    assert result == pytest.approx(expected)
    assert entry == before


def test_decay_uses_last_access_not_created_and_clamps_backward_clock(schema: Any) -> None:
    entry = _entry()
    schema.insert(entry)
    before = list(schema.connection.iterdump())
    assert schema.decayed_importance(entry, now_ms=1500, half_life_ms=1000) == 4.0
    assert schema.decayed_importance(entry, now_ms=2500, half_life_ms=1000) == pytest.approx(
        4 * 2**-0.5
    )
    assert list(schema.connection.iterdump()) == before
    assert schema.by_importance(4) == [entry]  # Stored importance was not changed.


@pytest.mark.parametrize(
    "kwargs",
    [
        {"now_ms": -1},
        {"now_ms": True},
        {"half_life_ms": 0},
        {"half_life_ms": -1},
        {"half_life_ms": True},
        {"half_life_ms": 1.5},
    ],
)
def test_decay_rejects_invalid_parameters(kwargs: dict[str, Any]) -> None:
    args = {"now_ms": 3000, "half_life_ms": 1000} | kwargs
    with pytest.raises((TypeError, ValueError)):
        _module().MemorySchema.decayed_importance(_entry(), **args)


def test_migration_v1_to_v2_preserves_every_original_value() -> None:
    module = _module()
    with sqlite3.connect(":memory:") as connection:
        schema = module.MemorySchema(connection, draft_fixture=True)
        schema.create(version=1)
        for kind in module.MemoryType:
            connection.execute(
                "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind.value,
                    kind.value,
                    "原文\n  preserve",
                    "message:original",
                    0,
                    4000,
                    123,
                    5,
                    '["非 ASCII", "%_", "cat"]',
                ),
            )
        connection.commit()
        original = connection.execute("SELECT * FROM memories ORDER BY id").fetchall()
        connection.execute("PRAGMA user_version = 73")
        assert schema.migrate(1, 2) is True
        migrated = connection.execute("SELECT * FROM memories ORDER BY id").fetchall()
        assert [row[:-1] for row in migrated] == original
        assert all(row[-1] is None for row in migrated)
        assert connection.execute("SELECT version FROM memory_schema_version").fetchone() == (2,)
        assert connection.execute("PRAGMA user_version").fetchone() == (73,)
        assert schema.migrate(1, 2) is False  # Replays do not replace records or embeddings.
        assert schema.by_type(module.MemoryType.EPISODIC)[0].embedding_id is None
        schema.insert(_entry(id="new-v2"))
        assert schema.by_source("event:fixture-1") == [_entry(id="new-v2")]
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_migration_rolls_back_ddl_if_version_write_fails() -> None:
    module = _module()
    with sqlite3.connect(":memory:") as connection:
        schema = module.MemorySchema(connection, draft_fixture=True)
        schema.create(1)
        before = list(connection.iterdump())

        def authorizer(action: int, table: str | None, *_: Any) -> int:
            if action == sqlite3.SQLITE_UPDATE and table == "memory_schema_version":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        with pytest.raises(sqlite3.DatabaseError):
            schema.migrate(1, 2)
        connection.set_authorizer(None)
        assert list(connection.iterdump()) == before
        assert not connection.in_transaction
        assert schema.migrate(1, 2) is True


@pytest.mark.parametrize("versions", [(0, 2), (1, 3), (2, 1), (True, 2)])
def test_unknown_migration_is_rejected_without_writes(
    schema: Any, versions: tuple[int, int]
) -> None:
    before = list(schema.connection.iterdump())
    with pytest.raises((ValueError, _module().MemorySchemaError)):
        schema.migrate(*versions)
    assert list(schema.connection.iterdump()) == before


@pytest.mark.parametrize(
    "column, value",
    [
        ("id", None),
        ("id", ""),
        ("type", "UNKNOWN"),
        ("content", ""),
        ("source", ""),
        ("created_at", -1),
        ("created_at", 1.25),
        ("last_accessed_at", 999),
        ("access_count", -1),
        ("access_count", 1.25),
        ("importance", 0),
        ("importance", 6),
        ("importance", 2.5),
        ("tags", "{}"),
        ("tags", '"scalar"'),
        ("tags", "[2]"),
        ("tags", "[null]"),
        ("tags", '[""]'),
        ("embedding_id", ""),
    ],
)
def test_ddl_rejects_invalid_direct_sql_values(schema: Any, column: str, value: Any) -> None:
    entry = _entry()
    schema.insert(entry)
    before = list(schema.connection.iterdump())
    # Column names are a finite parametrized test list, never caller input.
    with pytest.raises(sqlite3.IntegrityError):
        schema.connection.execute(f"UPDATE memories SET {column} = ?", (value,))
    schema.connection.rollback()
    assert list(schema.connection.iterdump()) == before


def test_invalid_json_is_rejected_and_original_remains(schema: Any) -> None:
    schema.insert(_entry())
    with pytest.raises(sqlite3.DatabaseError):
        schema.connection.execute("UPDATE memories SET tags = ?", ("malformed JSON",))
    schema.connection.rollback()
    assert schema.by_type(_module().MemoryType.SEMANTIC) == [_entry()]


@pytest.mark.parametrize("version", [0, 3, True, 1.0])
def test_create_and_ddl_reject_unknown_versions(version: Any) -> None:
    module = _module()
    with pytest.raises(ValueError):
        module.MemorySchema.ddl(version)
    with sqlite3.connect(":memory:") as connection:
        schema = module.MemorySchema(connection, draft_fixture=True)
        with pytest.raises(ValueError):
            schema.create(version)
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_v1_refuses_v2_apis_until_explicit_migration() -> None:
    module = _module()
    with sqlite3.connect(":memory:") as connection:
        schema = module.MemorySchema(connection, draft_fixture=True)
        schema.create(1)
        before = list(connection.iterdump())
        with pytest.raises(module.MemorySchemaError):
            schema.insert(_entry())
        with pytest.raises(module.MemorySchemaError):
            schema.by_type(module.MemoryType.SEMANTIC)
        assert list(connection.iterdump()) == before


def test_schema_rechecks_attached_and_temporary_database_on_every_operation(schema: Any) -> None:
    module = _module()
    connection = schema.connection
    connection.execute("ATTACH ':memory:' AS other")
    with pytest.raises(module.MemorySchemaError):
        schema.insert(_entry())
    connection.execute("DETACH other")
    connection.execute("CREATE TEMP TABLE memories (id TEXT)")
    with pytest.raises(module.MemorySchemaError):
        schema.by_source("event:fixture-1")
    assert connection.execute("SELECT count(*) FROM main.memories").fetchone() == (0,)


@pytest.mark.parametrize("autocommit", [True, False])
def test_nonlegacy_transaction_modes_are_rejected(autocommit: bool) -> None:
    module = _module()
    with sqlite3.connect(":memory:", autocommit=autocommit) as connection:
        with pytest.raises(module.MemorySchemaError):
            module.MemorySchema(connection, draft_fixture=True)


def test_incompatible_row_and_text_factories_are_rejected() -> None:
    module = _module()
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        with pytest.raises(module.MemorySchemaError):
            module.MemorySchema(connection, draft_fixture=True)
        connection.row_factory = None
        connection.text_factory = bytes
        with pytest.raises(module.MemorySchemaError):
            module.MemorySchema(connection, draft_fixture=True)


def test_creation_rolls_back_all_objects_on_injected_index_failure() -> None:
    module = _module()
    with sqlite3.connect(":memory:") as connection:
        schema = module.MemorySchema(connection, draft_fixture=True)

        def authorizer(action: int, *_: Any) -> int:
            return (
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_INDEX else sqlite3.SQLITE_OK
            )

        connection.set_authorizer(authorizer)
        with pytest.raises(sqlite3.DatabaseError):
            schema.create()
        connection.set_authorizer(None)
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
        assert not connection.in_transaction
        schema.create()


def test_migration_refuses_mismatched_layout_without_marking_v2() -> None:
    module = _module()
    with sqlite3.connect(":memory:") as connection:
        schema = module.MemorySchema(connection, draft_fixture=True)
        schema.create(1)
        connection.execute("ALTER TABLE memories ADD COLUMN unexpected TEXT")
        before = list(connection.iterdump())
        with pytest.raises(module.MemorySchemaError):
            schema.migrate()
        assert list(connection.iterdump()) == before


def test_memory_fixture_never_creates_core_runtime_tables(schema: Any) -> None:
    assert schema.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall() == [("memories",), ("memory_schema_version",)]
    assert _module().RUNTIME_ENABLED is False


def test_generated_ddl_creates_isolated_memory_schema() -> None:
    from importlib.util import find_spec

    assert find_spec("jarvis.memory_schema") is not None, "memory schema draft is missing"
    module = importlib.import_module("jarvis.memory_schema")
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(module.MemorySchema.ddl())
        columns = {row[1] for row in connection.execute("PRAGMA table_info(memories)")}
        assert columns == {
            "id",
            "type",
            "content",
            "source",
            "created_at",
            "last_accessed_at",
            "access_count",
            "importance",
            "tags",
            "embedding_id",
        }
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(memories)")}
        assert {
            "idx_memories_type",
            "idx_memories_source",
            "idx_memories_created_at",
            "idx_memories_importance",
            "idx_memories_last_accessed_at",
        } <= indexes
        assert connection.execute("SELECT version FROM memory_schema_version").fetchone() == (2,)
    assert module.DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert module.RUNTIME_ENABLED is False
