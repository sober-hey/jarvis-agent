"""Offline SQLite design probes, not a migration runner or approval of DP/ADR.

Only in-memory databases are created. Render the declarative candidate once and
exercise SQLite itself: accepting YAML or regex-matching SQL is not SQL evidence.
The aliases in this draft do NOT rename any deployed/product tables.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
DRAFT_PATH = CONTRACTS / "draft/sqlite_schema.yaml"
REQUIRED_TABLES = {
    "tasks",
    "task_steps",
    "task_plans",
    "operation_intents",
    "attempts",
    "system_control",
    "events",
    "schema_version",
    "file_write_intents",
}
Document = dict[str, Any]


def _load(path: Path = DRAFT_PATH) -> Document:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast("Document", value)


def _identifier(value: str) -> str:
    assert re.fullmatch(r"[a-z_][a-z_0-9]*", value), value
    return f'"{value}"'


def _table_sql(name: str, table: Document) -> str:
    clauses: list[str] = []
    for column, definition in table["columns"].items():
        column_type = definition["type"]
        assert column_type in {"TEXT", "INTEGER"}
        parts = [_identifier(column), column_type]
        if definition.get("primary_key"):
            parts.append("PRIMARY KEY")
        if definition.get("autoincrement"):
            parts.append("AUTOINCREMENT")
        if definition["not_null"]:
            parts.append("NOT NULL")
        if definition.get("unique"):
            parts.append("UNIQUE")
        if "default_sql" in definition:
            parts.extend(("DEFAULT", definition["default_sql"]))
        if "check" in definition:
            parts.append(f"CHECK ({definition['check']})")
        clauses.append(" ".join(parts))
    for columns in table.get("unique", []):
        clauses.append("UNIQUE (" + ", ".join(map(_identifier, columns)) + ")")
    for foreign_key in table.get("foreign_keys", []):
        local = ", ".join(map(_identifier, foreign_key["columns"]))
        remote = ", ".join(map(_identifier, foreign_key["references"]["columns"]))
        parent = _identifier(foreign_key["references"]["table"])
        assert foreign_key["on_delete"] == foreign_key["on_update"] == "NO ACTION"
        clauses.append(
            f"FOREIGN KEY ({local}) REFERENCES {parent} ({remote}) "
            "ON DELETE NO ACTION ON UPDATE NO ACTION"
        )
    clauses.extend(f"CHECK ({condition})" for condition in table.get("checks", []))
    return f"CREATE TABLE {_identifier(name)} (" + ", ".join(clauses) + ");"


def _assert_migration_chain(document: Document) -> None:
    migrations = document["migrations"]
    versions = [row["version"] for row in migrations]
    assert versions and all(type(version) is int for version in versions)
    assert versions == list(range(1, len(versions) + 1)), "non-increasing migration versions"
    assert document["migration_policy"]["candidate_latest_version"] == versions[-1]
    tables: list[str] = []
    indexes: list[str] = []
    for previous, migration in enumerate(migrations):
        assert migration["previous_version"] == (previous or None)
        assert migration["status"] == "DRAFT_PENDING_DP"
        assert migration["approval_status"] == "NOT_APPROVED"
        tables.extend(migration["create_tables"])
        indexes.extend(migration["create_indexes"])
    assert len(tables) == len(set(tables)) and set(tables) == set(document["tables"])
    assert len(indexes) == len(set(indexes)) and set(indexes) == set(document["indexes"])


def _apply_candidate(document: Document) -> sqlite3.Connection:
    """Test-only renderer; never imported by the product or run against a file DB."""
    _assert_migration_chain(document)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        for migration in document["migrations"]:
            connection.execute("BEGIN IMMEDIATE")
            for name in migration["create_tables"]:
                connection.execute(_table_sql(name, document["tables"][name]))
            for name in migration["create_indexes"]:
                connection.execute(document["indexes"][name]["sql"])
            # Probe data/checksum, not a hash or installation record of a real migration.
            connection.execute(
                "INSERT INTO schema_version "
                "(version, previous_version, migration_name, checksum_sha256, applied_at_ms) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    migration["version"],
                    migration["previous_version"],
                    migration["name"],
                    "a" * 64,
                    0,
                ),
            )
            connection.commit()
        return connection
    except BaseException:
        connection.close()
        raise


@pytest.fixture
def draft() -> Document:
    return _load()


@pytest.fixture
def database(draft: Document) -> Iterator[sqlite3.Connection]:
    with closing(_apply_candidate(draft)) as connection:
        yield connection


def _insert(connection: sqlite3.Connection, table: str, **values: Any) -> None:
    columns = ", ".join(map(_identifier, values))
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {_identifier(table)} ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


def _seed(connection: sqlite3.Connection) -> None:
    for suffix in ("1", "2"):
        _insert(
            connection,
            "tasks",
            id="t" + suffix,
            request_key="r" + suffix,
            request_digest="a" * 64,
            task_kind="inventory.generate",
            state="CREATED",
            version=0,
            created_at_ms=0,
            updated_at_ms=0,
        )
        _insert_plan(connection, "p" + suffix, "t" + suffix)
        _insert(
            connection,
            "task_steps",
            id="s" + suffix,
            task_id="t" + suffix,
            plan_id="p" + suffix,
            ordinal=1,
            state="PENDING",
            required=1,
            version=0,
        )
        _insert(
            connection,
            "operation_intents",
            operation_id="i" + suffix,
            step_id="s" + suffix,
            operation_key="key" + suffix,
            operation_type="file.publish.no_clobber",
            tool_name="filesystem.publisher",
            tool_version="fixture",
            arguments_digest="a" * 64,
            state="PREPARED",
            created_at_ms=0,
        )
        _insert(
            connection,
            "file_write_intents",
            operation_id="i" + suffix,
            input_snapshot_sha256="a" * 64,
            prepared_relpath="prepared/" + suffix,
            target_relpath="output/" + suffix,
            expected_sha256="a" * 64,
            expected_size=0,
        )
    connection.commit()


def _insert_plan(
    connection: sqlite3.Connection,
    identifier: str,
    task: str,
    version: int = 1,
    state: str = "ACTIVE",
) -> None:
    _insert(
        connection,
        "task_plans",
        id=identifier,
        task_id=task,
        plan_version=version,
        state=state,
        plan_digest="a" * 64,
        created_at_ms=0,
    )


def _insert_attempt(
    connection: sqlite3.Connection,
    identifier: str,
    step: str = "s1",
    operation: str = "i1",
    number: int = 1,
    state: str = "ACTIVE",
) -> None:
    _insert(
        connection,
        "attempts",
        id=identifier,
        step_id=step,
        operation_id=operation,
        attempt_no=number,
        worker_epoch=0,
        state=state,
        started_at_ms=0,
    )


def test_schema_is_offline_draft_not_approval(draft: Document) -> None:
    assert draft["status"] == "DRAFT"
    assert draft["approval_status"] == "NOT_APPROVED"
    assert draft["runtime_enabled"] is False
    assert draft["contract_pack_status"] == "NOT_FROZEN"
    assert set(draft["tables"]) == REQUIRED_TABLES
    for table in draft["tables"].values():
        assert table["status"] == "DRAFT_PENDING_DP"
        assert table["unresolved_dependencies"]
    assert draft["naming"]["is_deployed_rename"] is False


def test_all_tables_have_explicit_non_null_primary_key(
    draft: Document, database: sqlite3.Connection
) -> None:
    for name, table in draft["tables"].items():
        declared = {key for key, value in table["columns"].items() if value.get("primary_key")}
        assert len(declared) == 1, name
        actual = database.execute(f"PRAGMA table_info({_identifier(name)})").fetchall()
        assert {row[1] for row in actual if row[5]} == declared
        assert all(value["not_null"] for key, value in table["columns"].items() if key in declared)
        assert all(row[3] for row in actual if row[5]), name


def test_foreign_keys_reference_existing_columns_and_unique_parent_keys(
    draft: Document, database: sqlite3.Connection
) -> None:
    tables = draft["tables"]
    for name, table in tables.items():
        actual = database.execute(f"PRAGMA foreign_key_list({_identifier(name)})").fetchall()
        expected: set[tuple[str, str, str]] = set()
        for foreign_key in table.get("foreign_keys", []):
            reference = foreign_key["references"]
            assert reference["table"] in tables
            parent = tables[reference["table"]]
            local, remote = foreign_key["columns"], reference["columns"]
            assert len(local) == len(remote) > 0
            assert set(local) <= set(table["columns"])
            assert set(remote) <= set(parent["columns"])
            unique_keys = list(parent.get("unique", [])) + [
                [column]
                for column, definition in parent["columns"].items()
                if definition.get("primary_key") or definition.get("unique")
            ]
            assert remote in unique_keys
            expected.update(
                (reference["table"], left, right) for left, right in zip(local, remote, strict=True)
            )
        assert {(row[2], row[3], row[4]) for row in actual} == expected
    _seed(database)  # DML also detects SQLite's deferred parent-key mismatch errors.
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("name", "table", "column"),
    [
        ("ux_active_plan_per_task", "task_plans", "task_id"),
        ("ux_active_attempt_per_step", "attempts", "step_id"),
    ],
)
def test_partial_unique_indexes_are_real_sqlite_indexes(
    draft: Document, database: sqlite3.Connection, name: str, table: str, column: str
) -> None:
    index = draft["indexes"][name]
    assert index["unique"] is True and index["where"] == "state = 'ACTIVE'"
    assert index["table"] == table and index["columns"] == [column]
    assert re.fullmatch(
        rf"CREATE UNIQUE INDEX {name}\s+ON {table}\({column}\)\s+WHERE state = 'ACTIVE';",
        index["sql"].strip(),
    )
    info = database.execute(f"PRAGMA index_list({_identifier(table)})").fetchall()
    assert [(row[2], row[4]) for row in info if row[1] == name] == [(1, 1)]
    assert [row[2] for row in database.execute(f"PRAGMA index_info({_identifier(name)})")] == [
        column
    ]


@pytest.mark.parametrize("kind", ["plan", "attempt"])
def test_partial_unique_constraints_reject_only_second_active(
    database: sqlite3.Connection, kind: str
) -> None:
    _seed(database)
    if kind == "plan":
        _insert_plan(database, "history", "t1", 2, "NON_ACTIVE_FIXTURE_NOT_APPROVED")
        with pytest.raises(sqlite3.IntegrityError) as error:
            _insert_plan(database, "conflict", "t1", 3)
        assert str(error.value) == "UNIQUE constraint failed: task_plans.task_id"
    else:
        _insert_attempt(database, "active")
        _insert_attempt(database, "history", number=2, state="NON_ACTIVE_FIXTURE_NOT_APPROVED")
        with pytest.raises(sqlite3.IntegrityError) as error:
            _insert_attempt(database, "conflict", number=3)
        assert str(error.value) == "UNIQUE constraint failed: attempts.step_id"
    assert error.value.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"


def test_migration_versions_are_contiguous_and_read_back(
    draft: Document, database: sqlite3.Connection
) -> None:
    _assert_migration_chain(draft)
    assert database.execute(
        "SELECT version, previous_version FROM schema_version ORDER BY version"
    ).fetchall() == [(row["version"], row["previous_version"]) for row in draft["migrations"]]
    assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.parametrize("versions", [[2, 1], [1, 1], [1, 3]])
def test_migration_chain_rejects_reorder_duplicates_and_gaps(
    draft: Document, versions: list[int]
) -> None:
    changed = copy.deepcopy(draft)
    for row, version in zip(changed["migrations"], versions, strict=True):
        row["version"] = version
    with pytest.raises(AssertionError, match="non-increasing"):
        _assert_migration_chain(changed)


@pytest.mark.parametrize("kind", ["task", "step"])
def test_state_checks_match_domain_schema_and_transition_endpoints(
    draft: Document, database: sqlite3.Connection, kind: str
) -> None:
    table_name = "tasks" if kind == "task" else "task_steps"
    table = draft["tables"][table_name]
    state_document = _load(CONTRACTS / f"{kind}_states.yaml")
    transitions = _load(CONTRACTS / f"{kind}_transitions.yaml")["transitions"]
    endpoints = {edge[key] for edge in transitions for key in ("from", "to")}
    assert set(table["state_values"]) == set(state_document["states"]) == endpoints
    if kind == "task":
        graph = json.loads((CONTRACTS / "draft/execution_graph.schema.json").read_text())
        domain_states = graph["$defs"]["task"]["properties"]["state"]["enum"]
    else:
        domain = json.loads((CONTRACTS / "draft/step.schema.json").read_text())
        domain_states = domain["properties"]["state"]["enum"]
    assert set(domain_states) == set(table["state_values"])
    _seed(database)
    for state in table["state_values"]:
        database.execute(f"UPDATE {_identifier(table_name)} SET state = ?", (state,))
    with pytest.raises(sqlite3.IntegrityError) as error:
        database.execute(f"UPDATE {_identifier(table_name)} SET state = 'INVALID'")
    assert error.value.sqlite_errorname == "SQLITE_CONSTRAINT_CHECK"


def test_all_guard_mappings_preserve_source_status_and_dependencies(draft: Document) -> None:
    guards = _load(CONTRACTS / "guards.yaml")["guards"]
    mappings = draft["guard_mapping"]
    assert set(mappings) == set(guards)
    for name, guard in guards.items():
        mapping = mappings[name]
        assert mapping["source_status"] == guard["source_status"]
        assert mapping["unresolved_dependencies"] == guard["unresolved_dependencies"]
        assert mapping["guard_dependencies"] == guard["guard_dependencies"]
        assert mapping["enforcement"] in {
            "SQLITE_CONSTRAINT_AND_APPLICATION",
            "SQLITE_TRANSACTION_AND_APPLICATION",
            "APPLICATION_ONLY",
            "SQLITE_CARRIER_PENDING_DP",
            "COMPOSITE",
        }
        assert mapping["requirements"]
        assert set(mapping["tables"]) <= set(draft["tables"])
        assert set(mapping["indexes"]) <= set(draft["indexes"])
        if mapping["enforcement"] == "APPLICATION_ONLY":
            assert not mapping["tables"] and not mapping["indexes"]
        if mapping["enforcement"] == "SQLITE_CARRIER_PENDING_DP":
            assert mapping["tables"] and "DRAFT_PENDING_DP" in mapping["pending_carrier"]
    for name in ("one_row_cas", "fenced_commit", "event_same_transaction"):
        assert mappings[name]["enforcement"] == "SQLITE_TRANSACTION_AND_APPLICATION"
    assert mappings["event_same_transaction"]["source_status"] == "SOURCE_MISSING"


@pytest.mark.parametrize(
    "case",
    [
        "active_plan_cross_task",
        "step_plan_cross_task",
        "attempt_intent_cross_step",
        "event_step_cross_task",
    ],
)
def test_composite_foreign_keys_reject_cross_object_links(
    database: sqlite3.Connection, case: str
) -> None:
    _seed(database)
    with pytest.raises(sqlite3.IntegrityError) as error:
        if case == "active_plan_cross_task":
            database.execute("UPDATE tasks SET active_plan_id = 'p2' WHERE id = 't1'")
        elif case == "step_plan_cross_task":
            database.execute("UPDATE task_steps SET plan_id = 'p2', ordinal = 2 WHERE id = 's1'")
        elif case == "attempt_intent_cross_step":
            _insert_attempt(database, "bad", operation="i2")
        else:
            _insert(
                database,
                "events",
                task_id="t1",
                step_id="s2",
                event_type="fixture.not_a_contract_event_type",
                actor_type="fixture",
                actor_id="fixture",
                payload_json="{}",
                created_at_ms=0,
            )
    assert error.value.sqlite_errorname == "SQLITE_CONSTRAINT_FOREIGNKEY"
    database.execute("UPDATE tasks SET active_plan_id = 'p1' WHERE id = 't1'")
    _insert_attempt(database, "valid", operation="i1")
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []


def test_singleton_is_at_most_one_not_an_automatic_epoch_grant(
    database: sqlite3.Connection,
) -> None:
    assert database.execute("SELECT * FROM system_control").fetchall() == []
    values = dict(emergency_stop=1, backup_mode=0, worker_epoch=0, updated_at_ms=0)
    _insert(database, "system_control", singleton_id=1, **values)
    with pytest.raises(sqlite3.IntegrityError) as error:
        _insert(database, "system_control", singleton_id=2, **values)
    assert error.value.sqlite_errorname == "SQLITE_CONSTRAINT_CHECK"
    with pytest.raises(sqlite3.IntegrityError):
        _insert(database, "system_control", singleton_id=1, **values)


@pytest.mark.parametrize(
    ("table", "column", "invalid"),
    [
        ("tasks", "version", -1),
        ("task_steps", "required", 2),
        ("task_plans", "plan_digest", "g" * 64),
    ],
)
def test_candidate_representation_checks_reject_bad_values(
    database: sqlite3.Connection, table: str, column: str, invalid: Any
) -> None:
    _seed(database)
    with pytest.raises(sqlite3.IntegrityError) as error:
        database.execute(f"UPDATE {_identifier(table)} SET {_identifier(column)} = ?", (invalid,))
    assert error.value.sqlite_errorname == "SQLITE_CONSTRAINT_CHECK"


def test_plan_attempt_lifecycle_enums_are_intentionally_not_invented(draft: Document) -> None:
    for name in ("task_plans", "attempts"):
        table = draft["tables"][name]
        assert "state_values" not in table
        assert table["columns"]["state"]["check"] == "length(state) > 0"
        assert table["lifecycle_status"] == "DRAFT_PENDING_DP"
    # Document wrapper versions are not silently turned into new persistent row fields.
    for name in ("tasks", "task_plans", "task_steps", "attempts"):
        assert "schema_version" not in draft["tables"][name]["columns"]
    for name in ("task_plans", "attempts", "operation_intents"):
        assert "version" not in draft["tables"][name]["columns"]


def test_all_domain_structural_fields_project_without_losing_nullability(draft: Document) -> None:
    graph = json.loads((CONTRACTS / "draft/execution_graph.schema.json").read_text())
    names = {
        "task": "tasks",
        "plan": "task_plans",
        "step": "task_steps",
        "attempt": "attempts",
        "intent": "operation_intents",
        "file_write_intent": "file_write_intents",
    }
    for name, table_name in names.items():
        domain = graph["$defs"][name]
        columns = draft["tables"][table_name]["columns"]
        for key in set(domain["required"]) - {"schema_version"}:
            assert key in columns, (name, key)
            raw_type = domain["properties"][key].get("type", "string")
            types = cast("list[str]", raw_type) if isinstance(raw_type, list) else [str(raw_type)]
            assert columns[key]["not_null"] is ("null" not in types), (name, key)
            expected_type = "INTEGER" if {"integer", "boolean"} & set(types) else "TEXT"
            assert columns[key]["type"] == expected_type, (name, key)


def test_all_index_metadata_matches_sqlite(draft: Document, database: sqlite3.Connection) -> None:
    for name, index in draft["indexes"].items():
        table = index["table"]
        assert set(index["columns"]) <= set(draft["tables"][table]["columns"])
        rows = database.execute(f"PRAGMA index_list({_identifier(table)})").fetchall()
        assert [(row[2], row[4]) for row in rows if row[1] == name] == [
            (int(index["unique"]), int(index["where"] is not None))
        ]
        assert [
            row[2] for row in database.execute(f"PRAGMA index_info({_identifier(name)})")
        ] == index["columns"]


@pytest.mark.parametrize(
    ("version", "previous", "expected"),
    [
        (3, None, "SQLITE_CONSTRAINT_CHECK"),
        (4, 2, "SQLITE_CONSTRAINT_CHECK"),
        (4, 3, "SQLITE_CONSTRAINT_FOREIGNKEY"),
        (2, 1, "SQLITE_CONSTRAINT_PRIMARYKEY"),
    ],
)
def test_migration_history_constraints_reject_invalid_predecessors(
    database: sqlite3.Connection, version: int, previous: int | None, expected: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError) as error:
        _insert(
            database,
            "schema_version",
            version=version,
            previous_version=previous,
            migration_name="negative_probe",
            checksum_sha256="a" * 64,
            applied_at_ms=0,
        )
    assert error.value.sqlite_errorname == expected
    assert database.execute("SELECT version FROM schema_version ORDER BY version").fetchall() == [
        (1,),
        (2,),
    ]


def test_index_migration_failure_rolls_back_ddl_and_version_record(draft: Document) -> None:
    """A real statement error rolls back this version, not earlier committed versions."""
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for name in draft["migrations"][0]["create_tables"]:
            connection.execute(_table_sql(name, draft["tables"][name]))
        _insert(
            connection,
            "schema_version",
            version=1,
            previous_version=None,
            migration_name="0001_candidate_core_tables",
            checksum_sha256="a" * 64,
            applied_at_ms=0,
        )
        connection.commit()
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                for index in draft["indexes"].values():
                    connection.execute(index["sql"])
                _insert(
                    connection,
                    "schema_version",
                    version=2,
                    previous_version=1,
                    migration_name="0002_candidate_active_and_lookup_indexes",
                    checksum_sha256="a" * 64,
                    applied_at_ms=0,
                )
                connection.execute("CREATE INDEX bad_probe ON tasks(no_such_column)")
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [(1,)]
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert set(draft["tables"]) <= names
        assert not set(draft["indexes"]) & names
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
