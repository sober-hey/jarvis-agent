"""K0-002 candidate database contract fixture: execute the published Master Plan DDL.

Scope: pure candidate verification of the SQLite DDL that Master Plan v1.1.1
section 10.1 publishes for the K0 core tables, plus the composite foreign keys
and the two partial unique indexes declared there. Statements are extracted
from the Master Plan text at run time, so no column is retyped or invented.

Out of scope: the product database, migrations, repositories, state machines,
guards, and any edit to ``contracts/``. Every database here is a throwaway
file under ``tmp_path``. This module closes no DOC-R item, claims no contract
freeze, and does not cover cross-connection race behaviour.

Facts recorded, not repaired:

* Section 10.1 names nine core tables but publishes DDL for only seven.
  ``system_control`` and ``artifacts`` appear as field lists without column
  types, so nothing is extracted for them and their columns are not guessed.
* ``tasks.active_plan_id`` and ``task_plans.task_id`` form a circular foreign
  key. SQLite accepts the forward reference at CREATE TABLE time and resolves
  it at DML time, so the published statement order executes unchanged.
* No Task-plan state enum is published, so the historical-plan case uses an
  explicit non-``ACTIVE`` literal. It exercises the partial index predicate
  ``WHERE state = 'ACTIVE'`` only and says nothing about legal plan states.
* The Attempt case uses a second such placeholder the same way: no attempt
  state enum is published either, so the literal only keeps the partial index
  ``ux_active_attempt_per_step`` out of play.

Additional selected published constraints are covered, one parametrized case
each, in ``CONSTRAINT_CASES``: ``tasks.request_key`` UNIQUE,
``UNIQUE(task_id, plan_version)``, ``operation_intents.operation_key`` UNIQUE,
one Intent per Step, ``UNIQUE(plan_id, ordinal)``, ``UNIQUE(step_id,
attempt_no)``, ``CHECK(expected_size >= 0)``, and the four existence foreign
keys on ``task_plans.task_id``, ``operation_intents.step_id``,
``file_write_intents.operation_id`` and ``task_events.task_id``. Each case
rejects exactly one violating row, checks that row did not land, then read-backs
a legal contrast with the other constraints isolated. Not asserted here: the
redundant ``UNIQUE(id, task_id)`` / ``UNIQUE(id, step_id)`` pairs, PRIMARY KEY
and NOT NULL rules, and the single-column Attempt foreign key.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import Final, NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_PATH = (
    REPO_ROOT
    / "docs/planning"
    / "JARVIS 独立智能体系统工程开发总规划-JARVIS_Engineering_Master_Plan_v1.1.1.md"
)

SECTION_10_1 = "#### 10.1 K0 核心数据表"
SECTION_10_2 = "#### 10.2 数据库实现规则"

#: The nine core tables section 10.1 lists, in published order.
NAMED_CORE_TABLES: Final = tuple(
    "tasks task_plans task_steps task_events operation_intents file_write_intents"
    " step_attempts system_control artifacts".split()
)
#: The seven of those nine that section 10.1 actually publishes DDL for.
TABLES_WITH_PUBLISHED_DDL: Final = frozenset(NAMED_CORE_TABLES) - {"system_control", "artifacts"}
PUBLISHED_INDEXES: Final = ("ux_active_plan_per_task", "ux_active_attempt_per_step")

ACTIVE: Final = "ACTIVE"
#: Placeholder only: no plan-state enum is published, so this literal merely
#: differs from ``ACTIVE`` for the partial index predicate.
NON_ACTIVE_PLAN_STATE: Final = "NOT_ACTIVE_PLAN_STATE_PLACEHOLDER"
DIGEST: Final = "a" * 64
FK_ERROR: Final = "SQLITE_CONSTRAINT_FOREIGNKEY"
UNIQUE_ERROR: Final = "SQLITE_CONSTRAINT_UNIQUE"

# Column lists below use subsets of published DDL columns; no extra column is
# guessed.
TASK_COLS: Final = "id, request_key, request_digest, task_kind, state, created_at_ms, updated_at_ms"
TASK_VALUES: Final = "?, ?, ?, 'inventory.generate', 'CREATED', 1, 1"
PLAN_COLS: Final = "id, task_id, plan_version, state, plan_digest, created_at_ms"
PLAN_VALUES: Final = "?, ?, ?, ?, ?, 1"
STEP_COLS: Final = "id, task_id, plan_id, ordinal, state"
STEP_VALUES: Final = "?, ?, ?, ?, 'PENDING'"
INTENT_COLS: Final = (
    "id, step_id, operation_key, operation_type, tool_name, tool_version,"
    " arguments_digest, state, created_at_ms"
)
INTENT_VALUES: Final = (
    "?, ?, ?, 'file.publish.no_clobber', 'filesystem.publisher', '1', ?, 'PREPARED', 1"
)
ATTEMPT_COLS: Final = "id, step_id, operation_id, attempt_no, worker_epoch, state, started_at_ms"
ATTEMPT_VALUES: Final = "?, ?, ?, ?, 1, ?, 1"
EVENT_COLS: Final = (
    "task_id, step_id, event_type, actor_type, actor_id, payload_json, created_at_ms"
)
EVENT_VALUES: Final = "?, ?, ?, 'WORKER', 'w1', '{}', 1"


def _insert(table: str, columns: str, values: str) -> str:
    return f"INSERT INTO {table} ({columns}) VALUES ({values})"


TASK_INSERT: Final = _insert("tasks", TASK_COLS, TASK_VALUES)
PLAN_INSERT: Final = _insert("task_plans", PLAN_COLS, PLAN_VALUES)
STEP_INSERT: Final = _insert("task_steps", STEP_COLS, STEP_VALUES)
INTENT_INSERT: Final = _insert("operation_intents", INTENT_COLS, INTENT_VALUES)
ATTEMPT_INSERT: Final = _insert("step_attempts", ATTEMPT_COLS, ATTEMPT_VALUES)
EVENT_INSERT: Final = _insert("task_events", EVENT_COLS, EVENT_VALUES)

#: case name -> statement whose execution must fail, and its parameters.
CROSS_REFERENCE_CASES: Final = {
    "active_plan_cross_task": ("UPDATE tasks SET active_plan_id = ? WHERE id = 't2'", ("p1",)),
    "step_plan_cross_task": (STEP_INSERT, ("s3", "t2", "p1", 2)),
    "attempt_intent_cross_step": (ATTEMPT_INSERT, ("a2", "s2", "i1", 1, ACTIVE)),
    "event_step_cross_task": (EVENT_INSERT, ("t2", "s1", "STEP_CLAIMED")),
}


def _section_10_1_text() -> str:
    text = PLAN_PATH.read_text(encoding="utf-8")
    start = text.index(SECTION_10_1)
    return text[start : text.index(SECTION_10_2, start)]


def _published_statements() -> tuple[str, ...]:
    """Every fenced SQL statement of section 10.1, extracted verbatim."""
    blocks = re.findall(r"```sql\n(.*?)```", _section_10_1_text(), flags=re.DOTALL)
    return tuple(f"{raw.strip()};" for block in blocks for raw in block.split(";") if raw.strip())


@contextlib.contextmanager
def _connect(db_path: Path) -> Generator[sqlite3.Connection]:
    with contextlib.closing(sqlite3.connect(db_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        yield connection


def _seed_two_task_chains(connection: sqlite3.Connection) -> None:
    """Insert two independent chains: t1/p1/s1/i1 and t2/p2/s2/i2."""
    for suffix in ("1", "2"):
        task_id, plan_id, step_id = f"t{suffix}", f"p{suffix}", f"s{suffix}"
        rows = (
            (TASK_INSERT, (task_id, f"request-key-{suffix}", DIGEST)),
            (PLAN_INSERT, (plan_id, task_id, 1, ACTIVE, DIGEST)),
            (STEP_INSERT, (step_id, task_id, plan_id, 1)),
            (INTENT_INSERT, (f"i{suffix}", step_id, f"operation-key-{suffix}", DIGEST)),
        )
        for statement, parameters in rows:
            connection.execute(statement, parameters)


def _expect_rejected(
    connection: sqlite3.Connection,
    statement: str,
    parameters: tuple[object, ...],
    expected_errorname: str,
) -> None:
    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        connection.execute(statement, parameters)
    assert excinfo.value.sqlite_errorname == expected_errorname


@pytest.fixture
def schema_db(tmp_path: Path) -> Path:
    """The published DDL applied to an empty database, nothing inserted."""
    db_path = tmp_path / "candidate_k0_contract.sqlite3"
    with _connect(db_path) as connection:
        for statement in _published_statements():
            connection.execute(statement)
        connection.commit()
    return db_path


@pytest.fixture
def seeded_db(schema_db: Path) -> Path:
    with _connect(schema_db) as connection:
        _seed_two_task_chains(connection)
        connection.commit()
    return schema_db


def test_published_ddl_executes_and_creates_the_published_objects(schema_db: Path) -> None:
    with _connect(schema_db) as connection:
        objects = connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    tables = [name for kind, name, _ in objects if kind == "table"]
    indexes = {name: sql for kind, name, sql in objects if kind == "index"}
    assert sorted(tables) == sorted(TABLES_WITH_PUBLISHED_DDL)
    assert sorted(indexes) == sorted(PUBLISHED_INDEXES)
    assert all("WHERE state = 'ACTIVE'" in sql for sql in indexes.values())


def test_section_10_1_publishes_ddl_for_seven_of_the_nine_named_tables() -> None:
    section = _section_10_1_text()
    listing = re.search(r"```text\n(.*?)```", section, flags=re.DOTALL)
    assert listing is not None
    assert tuple(listing.group(1).split()) == NAMED_CORE_TABLES
    declared = tuple(
        match.group(1)
        for statement in _published_statements()
        if (match := re.match(r"CREATE TABLE (\w+)", statement))
    )
    # Published DDL order differs from the listing order: task_events is declared last.
    assert sorted(declared) == sorted(TABLES_WITH_PUBLISHED_DDL)
    # system_control and artifacts are named and described, but never declared.
    assert "CREATE TABLE system_control" not in section
    assert "CREATE TABLE artifacts" not in section


def test_corresponding_paths_are_accepted_and_consistent(seeded_db: Path) -> None:
    with _connect(seeded_db) as connection:
        connection.execute("UPDATE tasks SET active_plan_id = 'p1' WHERE id = 't1'")
        connection.execute(
            "INSERT INTO file_write_intents (operation_id, input_snapshot_sha256,"
            " prepared_relpath, target_relpath, expected_sha256, expected_size)"
            " VALUES ('i1', ?, 'prepared/x.txt', 'output/x.txt', ?, 0)",
            (DIGEST, DIGEST),
        )
        connection.execute(ATTEMPT_INSERT, ("a1", "s1", "i1", 1, ACTIVE))
        connection.execute(EVENT_INSERT, ("t1", "s1", "STEP_CLAIMED"))
        # Observed fact, not a contract ruling: step_id is nullable and a composite
        # FK with any NULL column is not enforced (SQLite MATCH SIMPLE).
        connection.execute(EVENT_INSERT, ("t1", None, "TASK_STATE_CHANGED"))
        connection.commit()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("statement", "parameters"), CROSS_REFERENCE_CASES.values(), ids=list(CROSS_REFERENCE_CASES)
)
def test_cross_reference_violations_are_rejected(
    seeded_db: Path, statement: str, parameters: tuple[object, ...]
) -> None:
    with _connect(seeded_db) as connection, pytest.raises(sqlite3.IntegrityError) as excinfo:
        connection.execute(statement, parameters)
    assert excinfo.value.sqlite_errorname == FK_ERROR


def test_second_active_plan_for_one_task_is_rejected(seeded_db: Path) -> None:
    with _connect(seeded_db) as connection:
        _expect_rejected(connection, PLAN_INSERT, ("p1b", "t1", 2, ACTIVE, DIGEST), UNIQUE_ERROR)


def test_non_active_plan_for_one_task_is_retained(seeded_db: Path) -> None:
    with _connect(seeded_db) as connection:
        connection.execute(PLAN_INSERT, ("p1b", "t1", 2, NON_ACTIVE_PLAN_STATE, DIGEST))
        connection.commit()
        plans = connection.execute(
            "SELECT id FROM task_plans WHERE task_id = 't1' ORDER BY plan_version"
        ).fetchall()
    assert plans == [("p1",), ("p1b",)]


def test_second_active_attempt_for_one_step_is_rejected(seeded_db: Path) -> None:
    with _connect(seeded_db) as connection:
        connection.execute(ATTEMPT_INSERT, ("a1", "s1", "i1", 1, ACTIVE))
        _expect_rejected(connection, ATTEMPT_INSERT, ("a2", "s1", "i1", 2, ACTIVE), UNIQUE_ERROR)


# --- Published constraint coverage: one isolated case per rule class -------------

#: Placeholder only: differs from ``ACTIVE`` for the attempt partial-index predicate.
NON_ACTIVE_ATTEMPT_STATE: Final = "NOT_ACTIVE_ATTEMPT_STATE_PLACEHOLDER"
CHECK_ERROR: Final = "SQLITE_CONSTRAINT_CHECK"
#: File-write rows for the CHECK and existence-FK cases: (operation_id, digest, digest, size).
FILE_WRITE_INSERT: Final = _insert(
    "file_write_intents",
    "operation_id, input_snapshot_sha256, prepared_relpath, target_relpath,"
    " expected_sha256, expected_size",
    "?, ?, 'prepared/x.txt', 'output/x.txt', ?, ?",
)
#: A Step that exists but has no Intent yet, so a duplicate operation key is not
#: also the ``step_id`` UNIQUE of a Step that already has one.
FREE_STEP_INSERT: Final = (STEP_INSERT, ("s3", "t1", "p1", 2))


class _ConstraintCase(NamedTuple):
    """One published rule: its rejection, a legal contrast, and the read-back of both."""

    rejected: tuple[str, tuple[object, ...]]
    errorname: str
    contrast: tuple[str, tuple[object, ...]]
    readback: tuple[str, tuple[object, ...]]
    expected: tuple[tuple[object, ...], ...]
    #: Rows that isolate the rule, when the seeded chains do not already isolate it.
    setup: tuple[tuple[str, tuple[object, ...]], ...] = ()


CONSTRAINT_CASES: Final = {
    "tasks_request_key_unique": _ConstraintCase(
        rejected=(TASK_INSERT, ("t1b", "request-key-1", DIGEST)),
        errorname=UNIQUE_ERROR,
        contrast=(TASK_INSERT, ("t1b", "request-key-1b", DIGEST)),
        readback=("SELECT request_key FROM tasks WHERE id = ?", ("t1b",)),
        expected=(("request-key-1b",),),
    ),
    # plan_version 1 collides with p1; the state keeps the partial index out of play.
    "task_plans_task_id_plan_version_unique": _ConstraintCase(
        rejected=(PLAN_INSERT, ("p1b", "t1", 1, NON_ACTIVE_PLAN_STATE, DIGEST)),
        errorname=UNIQUE_ERROR,
        contrast=(PLAN_INSERT, ("p1b", "t1", 2, NON_ACTIVE_PLAN_STATE, DIGEST)),
        readback=("SELECT plan_version FROM task_plans WHERE id = ?", ("p1b",)),
        expected=((2,),),
    ),
    # The free Step keeps duplicate operation_key the only violated rule.
    "operation_intents_operation_key_unique": _ConstraintCase(
        rejected=(INTENT_INSERT, ("i3", "s3", "operation-key-1", DIGEST)),
        errorname=UNIQUE_ERROR,
        contrast=(INTENT_INSERT, ("i3", "s3", "operation-key-3", DIGEST)),
        readback=("SELECT operation_key FROM operation_intents WHERE id = ?", ("i3",)),
        expected=(("operation-key-3",),),
        setup=(FREE_STEP_INSERT,),
    ),
    # A new operation key, so the rejected row is the second Intent of one Step.
    "one_intent_per_step": _ConstraintCase(
        rejected=(INTENT_INSERT, ("i3", "s1", "operation-key-3", DIGEST)),
        errorname=UNIQUE_ERROR,
        contrast=(INTENT_INSERT, ("i3", "s3", "operation-key-3", DIGEST)),
        readback=("SELECT step_id FROM operation_intents WHERE id = ?", ("i3",)),
        expected=(("s3",),),
        setup=(FREE_STEP_INSERT,),
    ),
    "task_steps_plan_id_ordinal_unique": _ConstraintCase(
        rejected=(STEP_INSERT, ("s3", "t1", "p1", 1)),
        errorname=UNIQUE_ERROR,
        contrast=(STEP_INSERT, ("s3", "t1", "p1", 2)),
        readback=("SELECT ordinal FROM task_steps WHERE id = ?", ("s3",)),
        expected=((2,),),
    ),
    # attempt_no 1 collides with a1; the state keeps the partial index out of play.
    "step_attempts_step_id_attempt_no_unique": _ConstraintCase(
        rejected=(ATTEMPT_INSERT, ("a2", "s1", "i1", 1, NON_ACTIVE_ATTEMPT_STATE)),
        errorname=UNIQUE_ERROR,
        contrast=(ATTEMPT_INSERT, ("a2", "s1", "i1", 2, NON_ACTIVE_ATTEMPT_STATE)),
        readback=("SELECT attempt_no FROM step_attempts WHERE id = ?", ("a2",)),
        expected=((2,),),
        setup=((ATTEMPT_INSERT, ("a1", "s1", "i1", 1, ACTIVE)),),
    ),
    # expected_size carries the only CHECK of the published DDL.
    "file_write_intents_expected_size_check": _ConstraintCase(
        rejected=(FILE_WRITE_INSERT, ("i1", DIGEST, DIGEST, -1)),
        errorname=CHECK_ERROR,
        contrast=(FILE_WRITE_INSERT, ("i1", DIGEST, DIGEST, 0)),
        readback=("SELECT expected_size FROM file_write_intents", ()),
        expected=((0,),),
    ),
    "task_plans_task_id_fk": _ConstraintCase(
        rejected=(PLAN_INSERT, ("p9", "t9", 1, NON_ACTIVE_PLAN_STATE, DIGEST)),
        errorname=FK_ERROR,
        contrast=(PLAN_INSERT, ("p9", "t1", 3, NON_ACTIVE_PLAN_STATE, DIGEST)),
        readback=("SELECT task_id FROM task_plans WHERE id = ?", ("p9",)),
        expected=(("t1",),),
    ),
    "operation_intents_step_id_fk": _ConstraintCase(
        rejected=(INTENT_INSERT, ("i9", "s9", "operation-key-9", DIGEST)),
        errorname=FK_ERROR,
        contrast=(INTENT_INSERT, ("i9", "s3", "operation-key-9", DIGEST)),
        readback=("SELECT step_id FROM operation_intents WHERE id = ?", ("i9",)),
        expected=(("s3",),),
        setup=(FREE_STEP_INSERT,),
    ),
    "file_write_intents_operation_id_fk": _ConstraintCase(
        rejected=(FILE_WRITE_INSERT, ("i9", DIGEST, DIGEST, 0)),
        errorname=FK_ERROR,
        contrast=(FILE_WRITE_INSERT, ("i1", DIGEST, DIGEST, 0)),
        readback=("SELECT operation_id, expected_size FROM file_write_intents", ()),
        expected=(("i1", 0),),
    ),
    # step_id NULL: with MATCH SIMPLE the composite FK is skipped, so the rejected
    # row fails on the existence FK (task_id) alone.
    "task_events_task_id_fk": _ConstraintCase(
        rejected=(EVENT_INSERT, ("t9", None, "STEP_CLAIMED")),
        errorname=FK_ERROR,
        contrast=(EVENT_INSERT, ("t1", None, "TASK_STATE_CHANGED")),
        readback=("SELECT task_id, step_id FROM task_events", ()),
        expected=(("t1", None),),
    ),
}


@pytest.mark.parametrize("case", CONSTRAINT_CASES.values(), ids=list(CONSTRAINT_CASES))
def test_published_constraint_violations_are_rejected_and_the_contrast_is_retained(
    seeded_db: Path, case: _ConstraintCase
) -> None:
    """Reject one violating row, prove it did not land, then retain the legal contrast."""
    readback_statement, readback_parameters = case.readback
    with _connect(seeded_db) as connection:
        for statement, parameters in case.setup:
            connection.execute(statement, parameters)
        _expect_rejected(connection, case.rejected[0], case.rejected[1], case.errorname)
        # A rejected statement rolls back alone, so the contrast runs in the same
        # transaction and an empty read-back here means the rejected row is absent.
        assert connection.execute(readback_statement, readback_parameters).fetchall() == []
        connection.execute(case.contrast[0], case.contrast[1])
        connection.commit()
        rows = connection.execute(readback_statement, readback_parameters).fetchall()
    assert rows == list(case.expected)


def test_promoting_the_placeholder_plan_to_active_is_rejected_and_the_plan_rows_stay(
    seeded_db: Path,
) -> None:
    """A candidate UPDATE that promotes the placeholder p1b to ACTIVE is rejected.

    ``ux_active_plan_per_task`` is the partial unique index on ``task_plans(task_id)``
    with the predicate ``state = 'ACTIVE'``, so the rejection identity is that index and
    not the table-level ``UNIQUE(task_id, plan_version)``: version 2 of t1 is free, and
    the observed failure is exactly ``SQLITE_CONSTRAINT_UNIQUE``, error code ``2067``,
    text ``UNIQUE constraint failed: task_plans.task_id``.

    The rejected statement rolls back alone, so the same transaction still reads the
    seeded p1 as ``ACTIVE`` and p1b as the non-``ACTIVE`` placeholder. The explicit
    rollback then leaves both rows unchanged, and a legal no-op update that writes the
    same placeholder back over p1b commits and reads back unchanged as well.

    Scope: the placeholder is a literal that only keeps the partial index out of play,
    because no plan-state enum is published. This case asserts nothing about which plan
    states are legal, and it is not evidence about the product supersede path,
    existence checks, or concurrency.
    """
    plan_rows = (
        "SELECT id, plan_version, state FROM task_plans WHERE task_id = 't1' ORDER BY plan_version"
    )
    unchanged = [("p1", 1, ACTIVE), ("p1b", 2, NON_ACTIVE_PLAN_STATE)]
    with _connect(seeded_db) as connection:
        connection.execute(PLAN_INSERT, ("p1b", "t1", 2, NON_ACTIVE_PLAN_STATE, DIGEST))
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            connection.execute("UPDATE task_plans SET state = ? WHERE id = ?", (ACTIVE, "p1b"))
        assert excinfo.value.sqlite_errorname == UNIQUE_ERROR
        assert excinfo.value.sqlite_errorcode == 2067
        assert str(excinfo.value) == "UNIQUE constraint failed: task_plans.task_id"
        # Same transaction after the rejection: p1 stays the only ACTIVE plan of t1.
        assert connection.execute(plan_rows).fetchall() == unchanged
        connection.rollback()
        assert connection.execute(plan_rows).fetchall() == unchanged
        # Legal no-op: the same non-ACTIVE placeholder goes back over p1b and commits.
        connection.execute(
            "UPDATE task_plans SET state = ? WHERE id = ?", (NON_ACTIVE_PLAN_STATE, "p1b")
        )
        connection.commit()
        assert connection.execute(plan_rows).fetchall() == unchanged


def test_candidate_update_before_insert_order_keeps_one_active_plan_per_task(
    schema_db: Path,
) -> None:
    """Two Tasks each hold one ACTIVE plan; a candidate replacement reorders statements.

    The published schema is empty, so both Tasks and their version-1 ``ACTIVE`` plans are
    inserted here and committed. Both plans carrying version 1 and state ``ACTIVE`` is
    the cross-Task contrast: ``ux_active_plan_per_task`` is keyed on ``task_id``, so it
    bounds each Task separately rather than the table as a whole.

    The candidate replacement is then shown only as statement ordering inside one
    transaction: p1 is lowered to the non-``ACTIVE`` placeholder first, p1b is inserted
    as version-2 ``ACTIVE`` second, and both writes are visible in that transaction.
    Rolling that transaction back undoes the whole pair, not the lowering alone: p1
    reads ``ACTIVE`` again and p1b is absent. Repeating the same pair and committing it
    reads back p1 as the placeholder, p1b as ``ACTIVE``, and p2 of t2 still ``ACTIVE``.

    Scope: candidate SQL statement ordering plus the rollback and the commit of those two
    writes. It does not implement or assert the published switch protocol of section
    22.2 or the plan invariants of section 22.4: historical Intents and effects,
    permission and budget checks, approvals and the ``active_plan_id`` switch all stay
    outside it, and p1b is inserted with no existence or guard check. The two writes
    touch ``task_plans`` only, so ``tasks.active_plan_id`` is left NULL and unchanged -
    this case approves no product plan switch. No concurrency or cross-connection
    behaviour is covered, and the placeholder literal is not claimed to be a legal plan
    state.
    """
    plans_of_t1 = (
        "SELECT id, plan_version, state FROM task_plans WHERE task_id = 't1' ORDER BY plan_version"
    )
    all_plans = "SELECT id, state FROM task_plans ORDER BY id"
    all_pointers = "SELECT id, active_plan_id FROM tasks ORDER BY id"
    replaced = [("p1", 1, NON_ACTIVE_PLAN_STATE), ("p1b", 2, ACTIVE)]
    with _connect(schema_db) as connection:
        for suffix in ("1", "2"):
            connection.execute(TASK_INSERT, (f"t{suffix}", f"request-key-{suffix}", DIGEST))
            connection.execute(PLAN_INSERT, (f"p{suffix}", f"t{suffix}", 1, ACTIVE, DIGEST))
        connection.commit()
        # Cross-Task contrast: one ACTIVE plan per Task, both at version 1.
        assert connection.execute(all_plans).fetchall() == [("p1", ACTIVE), ("p2", ACTIVE)]

    # Both writes in one transaction, then a full rollback: neither write may survive.
    with _connect(schema_db) as connection:
        connection.execute(
            "UPDATE task_plans SET state = ? WHERE id = ?", (NON_ACTIVE_PLAN_STATE, "p1")
        )
        connection.execute(PLAN_INSERT, ("p1b", "t1", 2, ACTIVE, DIGEST))
        # Inside the transaction this connection sees both writes applied.
        assert connection.execute(plans_of_t1).fetchall() == replaced
        connection.rollback()
        assert connection.execute(plans_of_t1).fetchall() == [("p1", 1, ACTIVE)]

    # The same two writes again, this time committed.
    with _connect(schema_db) as connection:
        connection.execute(
            "UPDATE task_plans SET state = ? WHERE id = ?", (NON_ACTIVE_PLAN_STATE, "p1")
        )
        connection.execute(PLAN_INSERT, ("p1b", "t1", 2, ACTIVE, DIGEST))
        connection.commit()
        assert connection.execute(plans_of_t1).fetchall() == replaced
        assert connection.execute(all_plans).fetchall() == [
            ("p1", NON_ACTIVE_PLAN_STATE),
            ("p1b", ACTIVE),
            ("p2", ACTIVE),
        ]
        # Observed: the pair never touches tasks.active_plan_id, so no pointer moved.
        assert connection.execute(all_pointers).fetchall() == [("t1", None), ("t2", None)]


# --- One ACTIVE Attempt per Step: Master Plan v1.1.1 L997-1029 -------------------

#: The partial index ``ux_active_attempt_per_step`` is keyed on ``step_attempts.step_id``,
#: so that column - and not ``UNIQUE(step_id, attempt_no)`` - carries the rejection.
ATTEMPT_UNIQUE_MESSAGE: Final = "UNIQUE constraint failed: step_attempts.step_id"


def test_promoting_the_placeholder_attempt_to_active_is_rejected_and_the_attempt_rows_stay(
    seeded_db: Path,
) -> None:
    """A candidate UPDATE that promotes the placeholder a2 to ``ACTIVE`` is rejected.

    ``ux_active_attempt_per_step`` is the partial unique index on ``step_attempts(step_id)``
    with the predicate ``state = 'ACTIVE'``, so the rejection identity is that index and not
    the table-level ``UNIQUE(step_id, attempt_no)``: attempt number 2 of s1 is free, and the
    observed failure is exactly ``SQLITE_CONSTRAINT_UNIQUE``, error code ``2067``, text
    ``UNIQUE constraint failed: step_attempts.step_id``.

    The rejected statement rolls back alone, so the same transaction still reads a1 as
    ``ACTIVE`` and a2 as the non-``ACTIVE`` placeholder. The explicit rollback then leaves
    both rows unchanged, and a legal no-op update that writes the same placeholder back over
    a2 commits and reads back unchanged as well.

    Scope: the placeholder is a literal that only keeps the partial index out of play, because
    no attempt-state enum is published. This case asserts nothing about which attempt states
    are legal, and it is not evidence about the product recovery path of section 10.1, in which
    an old Attempt must first be moved to an interrupted or pending-review state before a new
    Attempt is created. The Worker Epoch, existence checks, guards and concurrency stay outside
    it, and no ``task_events`` row is written, so the published rule that a state transition
    and its event share one transaction is not covered either.
    """
    attempt_rows = (
        "SELECT id, attempt_no, state FROM step_attempts WHERE step_id = 's1' ORDER BY attempt_no"
    )
    unchanged = [("a1", 1, ACTIVE), ("a2", 2, NON_ACTIVE_ATTEMPT_STATE)]
    with _connect(seeded_db) as connection:
        connection.execute(ATTEMPT_INSERT, ("a1", "s1", "i1", 1, ACTIVE))
        connection.execute(ATTEMPT_INSERT, ("a2", "s1", "i1", 2, NON_ACTIVE_ATTEMPT_STATE))
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            connection.execute("UPDATE step_attempts SET state = ? WHERE id = ?", (ACTIVE, "a2"))
        assert excinfo.value.sqlite_errorname == UNIQUE_ERROR
        assert excinfo.value.sqlite_errorcode == 2067
        assert str(excinfo.value) == ATTEMPT_UNIQUE_MESSAGE
        # Same transaction after the rejection: a1 stays the only ACTIVE attempt of s1.
        assert connection.execute(attempt_rows).fetchall() == unchanged
        connection.rollback()
        assert connection.execute(attempt_rows).fetchall() == unchanged
        # Legal no-op: the same non-ACTIVE placeholder goes back over a2 and commits.
        connection.execute(
            "UPDATE step_attempts SET state = ? WHERE id = ?", (NON_ACTIVE_ATTEMPT_STATE, "a2")
        )
        connection.commit()
        assert connection.execute(attempt_rows).fetchall() == unchanged


def test_candidate_update_before_insert_order_keeps_one_active_attempt_per_step(
    schema_db: Path,
) -> None:
    """Two Steps each hold one ACTIVE attempt; a candidate replacement reorders statements.

    The published schema is empty, so the two independent Task/Plan/Step/Intent chains are
    inserted and committed here by the shared seed helper. Their Steps s1 and s2 keep distinct
    operation keys - ``operation-key-1`` and ``operation-key-2`` - and each attempt references
    the Intent of its own Step, so no ``operation_intents.operation_key`` collision and no
    composite foreign key is pending when the reported writes run.

    Both Steps first hold one ``ACTIVE`` attempt at attempt number 1. Both carrying attempt
    number 1 and state ``ACTIVE`` is the cross-Step contrast: ``ux_active_attempt_per_step``
    is keyed on ``step_id``, so it bounds each Step separately rather than the table as a
    whole.

    The candidate replacement is then shown only as statement ordering inside one transaction:
    a1 is lowered to the non-``ACTIVE`` placeholder first, a1b is inserted second as attempt
    number 2 ``ACTIVE`` of the same Step s1 and the same Intent i1, and both writes are visible
    in that transaction. Rolling that transaction back undoes the whole pair, not the lowering
    alone: a1 reads ``ACTIVE`` again and a1b is absent. Repeating the same pair and committing
    it reads back a1 as the placeholder, a1b as ``ACTIVE``, and a2 of s2 still ``ACTIVE``, with
    no foreign key left pending.

    The fresh attempt number keeps the table-level ``UNIQUE(step_id, attempt_no)`` out of play
    and no second Intent is inserted, so ``operation_intents.operation_key`` UNIQUE is never in
    play either: the only rule the demote-first order exists to satisfy is the partial index,
    and neither write of the pair is rejected here.

    Scope: candidate SQL statement ordering plus the rollback and the commit of those two
    writes. It does not implement or assert the published handling of section 10.1, in which an
    old Attempt must first be moved to an interrupted or pending-review state before a new
    Attempt is created: no such state is written or claimed, and a1b is inserted with no
    existence or guard check. The new attempt reuses the published ``worker_epoch`` literal 1 of
    its predecessor, so nothing here fences epochs or evidences Worker Epoch behaviour. The two
    writes touch ``step_attempts`` only, so no ``task_events`` row is written and the task,
    plan and step rows stay unchanged; no concurrency or cross-connection behaviour is covered,
    and the placeholder literal is not claimed to be a legal attempt state.
    """
    attempts_of_s1 = (
        "SELECT id, attempt_no, state FROM step_attempts WHERE step_id = 's1' ORDER BY attempt_no"
    )
    all_attempts = "SELECT id, step_id, attempt_no, state FROM step_attempts ORDER BY id"
    replaced = [("a1", 1, NON_ACTIVE_ATTEMPT_STATE), ("a1b", 2, ACTIVE)]
    with _connect(schema_db) as connection:
        _seed_two_task_chains(connection)
        connection.execute(ATTEMPT_INSERT, ("a1", "s1", "i1", 1, ACTIVE))
        connection.execute(ATTEMPT_INSERT, ("a2", "s2", "i2", 1, ACTIVE))
        connection.commit()
        # Cross-Step contrast: one ACTIVE attempt per Step, both at attempt number 1.
        assert connection.execute(all_attempts).fetchall() == [
            ("a1", "s1", 1, ACTIVE),
            ("a2", "s2", 1, ACTIVE),
        ]

    # Both writes in one transaction, then a full rollback: neither write may survive.
    with _connect(schema_db) as connection:
        connection.execute(
            "UPDATE step_attempts SET state = ? WHERE id = ?", (NON_ACTIVE_ATTEMPT_STATE, "a1")
        )
        connection.execute(ATTEMPT_INSERT, ("a1b", "s1", "i1", 2, ACTIVE))
        # Inside the transaction this connection sees both writes applied.
        assert connection.execute(attempts_of_s1).fetchall() == replaced
        connection.rollback()
        assert connection.execute(attempts_of_s1).fetchall() == [("a1", 1, ACTIVE)]

    # The same two writes again, this time committed.
    with _connect(schema_db) as connection:
        connection.execute(
            "UPDATE step_attempts SET state = ? WHERE id = ?", (NON_ACTIVE_ATTEMPT_STATE, "a1")
        )
        connection.execute(ATTEMPT_INSERT, ("a1b", "s1", "i1", 2, ACTIVE))
        connection.commit()
        assert connection.execute(attempts_of_s1).fetchall() == replaced
        assert connection.execute(all_attempts).fetchall() == [
            ("a1", "s1", 1, NON_ACTIVE_ATTEMPT_STATE),
            ("a1b", "s1", 2, ACTIVE),
            ("a2", "s2", 1, ACTIVE),
        ]
        # Observed: every attempt still references the Intent of its own Step.
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
