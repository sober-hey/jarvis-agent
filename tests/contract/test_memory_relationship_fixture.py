"""K0-002 candidate fixture: pure in-memory relationship checks over five row kinds.

Contract fixture, not product code: it lives under ``tests/``, no product module
imports it, it ships nothing, opens no file and touches no database, so no IO
happens here and no error code or contract freeze is created.

Published source basis, read by the author as authority and never re-read at run
time; ``yaml`` below is ``contracts/database_constraints.yaml``. Master Plan
v1.1.1 section 10.1 publishes ``task_plans.task_id`` -> ``tasks(id)`` as an
explicit FK (L895-896, ``FOREIGN KEY (task_id) REFERENCES tasks(id)``); L877-879
publishes ``tasks(active_plan_id, id) -> task_plans(id, task_id)`` (nullable
L866; "active_plan_id must belong to same Task", yaml L31); L928-931/L938
publishes ``task_steps(plan_id, task_id) -> task_plans(id, task_id)`` ("Step Plan
must belong to same Task", yaml L26), and ``task_steps`` has no separate FK to
``tasks``, so a Step reaches a Task only through its Plan; L958-961 publishes the
``operation_intents.step_id`` FK with at most one Intent per Step (``step_id TEXT
NOT NULL UNIQUE`` L946, K0 rule L965-969; "K0 one Intent per Step", yaml L28);
L1011-1015 publishes ``step_attempts.step_id`` -> ``task_steps(id)`` and
``step_attempts(operation_id, step_id)`` -> ``operation_intents(id, step_id)``
("Attempt Intent must belong to same Step", yaml L30), while retries of one
logical operation share one Intent row (L360-362).

One call is two phases, in this order:

1. Duplicate-id phase, fail-closed. If any of the five row kinds repeats an
   ``id``, the call returns exactly the ``<kind>.duplicate_id`` labels of the
   kinds that repeat one, and stops: no relationship label is produced, because
   "the row with id X" is then ambiguous and no first or last row may stand in as
   the parent of a reference. Repeats are counted per kind, so one ``id`` string may
   legally appear in two different kinds, and a repeat counts wherever it sits: the
   last row is checked as much as the first two. Within this phase the row order
   cannot matter.

   This phase is a rule about this fixture's own ``id`` column, whose values are
   non-NULL ``str``. It does not claim that a real ``id TEXT PRIMARY KEY`` column
   can never hold two rows a database would call duplicates: on an ordinary rowid
   table SQLite does not make a ``TEXT PRIMARY KEY`` column NOT NULL, and repeated
   NULL ids coexist there (a repeated non-NULL id is still rejected); adding
   STRICT, WITHOUT ROWID or an explicit NOT NULL is what removes the NULL case.
2. Relationship phase, entered only when every id is unique. Each id then maps to
   exactly one row, so no row is shadowed and no rule resolves a reference by row
   order.

Bounded diagnosis: a returned set is the set of rule labels this fixture tests,
not the complete set of constraint violations a real database would raise, and
nothing here reproduces SQLite behaviour. Two short-circuits are deliberate, so
that one negative case fires one label: an Attempt whose Step is unknown reports
``attempt.step_missing`` alone and the Intent half is not evaluated, even though
``step_attempts`` declares two independent FKs and both apply to such a row -- how
many of them one failing statement reports is SQLite's business, and it is not
promised to itemise them; and a Task whose ``active_plan_id`` is NULL is legal,
because yaml L31 is a composite FK and a NULL column is not enforced.

Out of scope: state legality, state machines, INV judgements, recovery, worker
epoch, side effects, SQLite (``sqlite3`` is not imported), migrations, and every
file but this one. Existence is not asserted: "at most one Intent per Step" is an
upper bound, not a promise that a Step has an Intent, a Task a Plan, or an Intent
an Attempt. Labels are fixture-local diagnostics, not formal error codes. Each
relationship negative case below starts from a legal graph and changes one edge, so
that edge is what gets blamed; a single change can still leave a second row's
reference dangling, so a case may report two labels, and the duplicate-id phase is
a separate exception that withholds relationship labels altogether.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Final

PLAN_TASK_MISSING: Final = "plan.task_missing"
TASK_ACTIVE_PLAN_MISSING: Final = "task.active_plan_missing"
TASK_ACTIVE_PLAN_OTHER_TASK: Final = "task.active_plan_other_task"
STEP_PLAN_MISSING: Final = "step.plan_missing"
STEP_PLAN_OTHER_TASK: Final = "step.plan_other_task"
INTENT_STEP_MISSING: Final = "intent.step_missing"
INTENT_STEP_NOT_UNIQUE: Final = "intent.step_not_unique"
ATTEMPT_STEP_MISSING: Final = "attempt.step_missing"
ATTEMPT_INTENT_MISSING: Final = "attempt.intent_missing"
ATTEMPT_INTENT_OTHER_STEP: Final = "attempt.intent_other_step"

# Rows carry only the published columns the rules below read; no state, version,
# epoch or timing column is modelled (table citations are in the module docstring).


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    active_plan_id: str | None = None


@dataclass(frozen=True, slots=True)
class Plan:
    id: str
    task_id: str


@dataclass(frozen=True, slots=True)
class Step:
    id: str
    task_id: str
    plan_id: str


@dataclass(frozen=True, slots=True)
class Intent:
    id: str
    step_id: str


@dataclass(frozen=True, slots=True)
class Attempt:
    id: str
    step_id: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class MemoryGraph:
    tasks: tuple[Task, ...] = ()
    plans: tuple[Plan, ...] = ()
    steps: tuple[Step, ...] = ()
    intents: tuple[Intent, ...] = ()
    attempts: tuple[Attempt, ...] = ()


def _has_repeated_id[R: (Task, Plan, Step, Intent, Attempt)](rows: Iterable[R]) -> bool:
    """Whether this kind repeats an id; repeats are detected per kind, not per graph."""
    seen: set[str] = set()
    for row in rows:
        if row.id in seen:
            return True
        seen.add(row.id)
    return False


def _duplicate_labels(graph: MemoryGraph) -> frozenset[str]:
    """Phase 1: the duplicate-id label of every kind that repeats an id, if any."""
    duplicated = (
        ("task", _has_repeated_id(graph.tasks)),
        ("plan", _has_repeated_id(graph.plans)),
        ("step", _has_repeated_id(graph.steps)),
        ("intent", _has_repeated_id(graph.intents)),
        ("attempt", _has_repeated_id(graph.attempts)),
    )
    return frozenset(kind + ".duplicate_id" for kind, repeats in duplicated if repeats)


def _by_id[R: (Task, Plan, Step, Intent, Attempt)](rows: Iterable[R]) -> dict[str, R]:
    """Phase 2 only: ids are unique here, so this map shadows no row and picks none."""
    return {row.id: row for row in rows}


def check_relationships(graph: MemoryGraph) -> frozenset[str]:
    """Return the labels this fixture found; an empty set means it found none.

    Duplicate ids short-circuit into the fail-closed phase, so any relationship
    label returned always comes from a graph whose ids are all unique.
    """
    duplicates = _duplicate_labels(graph)
    if duplicates:
        return duplicates
    bad: set[str] = set()
    tasks = _by_id(graph.tasks)
    plans = _by_id(graph.plans)
    steps = _by_id(graph.steps)
    intents = _by_id(graph.intents)
    for plan in graph.plans:
        if plan.task_id not in tasks:
            bad.add(PLAN_TASK_MISSING)
    for task in graph.tasks:
        if task.active_plan_id is None:
            continue  # Legal: the composite FK is not enforced for a NULL column.
        plan = plans.get(task.active_plan_id)
        if plan is None:
            bad.add(TASK_ACTIVE_PLAN_MISSING)
        elif plan.task_id != task.id:
            bad.add(TASK_ACTIVE_PLAN_OTHER_TASK)
    for step in graph.steps:
        plan = plans.get(step.plan_id)
        if plan is None:
            bad.add(STEP_PLAN_MISSING)
        elif plan.task_id != step.task_id:
            bad.add(STEP_PLAN_OTHER_TASK)
    seen_steps: set[str] = set()
    for intent in graph.intents:
        if intent.step_id not in steps:
            bad.add(INTENT_STEP_MISSING)
        if intent.step_id in seen_steps:
            bad.add(INTENT_STEP_NOT_UNIQUE)
        seen_steps.add(intent.step_id)
    for attempt in graph.attempts:
        if attempt.step_id not in steps:
            # No Step row means no composite-key comparison to make, so the Intent
            # half is not cascaded: one negative case, one label. This is a bounded
            # short-circuit, not the full set of violations SQLite would raise.
            bad.add(ATTEMPT_STEP_MISSING)
            continue
        intent = intents.get(attempt.operation_id)
        if intent is None:
            bad.add(ATTEMPT_INTENT_MISSING)
        elif intent.step_id != attempt.step_id:
            bad.add(ATTEMPT_INTENT_OTHER_STEP)
    return frozenset(bad)


_ONE = MemoryGraph(
    tasks=(Task("t1", "p1"),),
    plans=(Plan("p1", "t1"),),
    steps=(Step("s1", "t1", "p1"),),
    intents=(Intent("i1", "s1"),),
    attempts=(Attempt("a1", "s1", "i1"),),
)

# Two legal chains: no row may satisfy a reference aimed at the other chain.
_DUAL = MemoryGraph(
    tasks=(Task("t1", "p1"), Task("t2", "p2")),
    plans=(Plan("p1", "t1"), Plan("p2", "t2")),
    steps=(Step("s1", "t1", "p1"), Step("s2", "t2", "p2")),
    intents=(Intent("i1", "s1"), Intent("i2", "s2")),
    attempts=(Attempt("a1", "s1", "i1"), Attempt("a2", "s2", "i2")),
)

# Legal without a Step or Intent: a Task may own Plans nothing has used yet.
_LONE = MemoryGraph(tasks=(Task("t1"),), plans=(Plan("p1", "t1"),))

# Five legal chains and 25 distinct ids: the all-unique contrast, and long enough
# that a repeat can sit past the first two rows of its kind.
_FIVE = MemoryGraph(
    tasks=(
        Task("t1", "p1"),
        Task("t2", "p2"),
        Task("t3", "p3"),
        Task("t4", "p4"),
        Task("t5", "p5"),
    ),
    plans=(
        Plan("p1", "t1"),
        Plan("p2", "t2"),
        Plan("p3", "t3"),
        Plan("p4", "t4"),
        Plan("p5", "t5"),
    ),
    steps=(
        Step("s1", "t1", "p1"),
        Step("s2", "t2", "p2"),
        Step("s3", "t3", "p3"),
        Step("s4", "t4", "p4"),
        Step("s5", "t5", "p5"),
    ),
    intents=(
        Intent("i1", "s1"),
        Intent("i2", "s2"),
        Intent("i3", "s3"),
        Intent("i4", "s4"),
        Intent("i5", "s5"),
    ),
    attempts=(
        Attempt("a1", "s1", "i1"),
        Attempt("a2", "s2", "i2"),
        Attempt("a3", "s3", "i3"),
        Attempt("a4", "s4", "i4"),
        Attempt("a5", "s5", "i5"),
    ),
)


def _reversed(graph: MemoryGraph) -> MemoryGraph:
    """The same rows in the opposite order, so order dependence cannot hide."""
    return MemoryGraph(
        tasks=graph.tasks[::-1],
        plans=graph.plans[::-1],
        steps=graph.steps[::-1],
        intents=graph.intents[::-1],
        attempts=graph.attempts[::-1],
    )


def _assert_order_independent(graph: MemoryGraph, expected: set[str]) -> None:
    """Both row orders must yield the same labels; one call is not enough."""
    assert check_relationships(graph) == expected
    assert check_relationships(_reversed(graph)) == expected


def test_allowed_graphs_produce_no_violation() -> None:
    """Both legal chains, a NULL active_plan_id, and absent Intent/Attempt rows."""
    assert check_relationships(_ONE) == check_relationships(_DUAL) == frozenset()
    assert check_relationships(_LONE) == frozenset()
    assert check_relationships(replace(_ONE, tasks=(Task("t1"),))) == frozenset()
    assert check_relationships(replace(_ONE, attempts=())) == frozenset()
    assert check_relationships(replace(_ONE, intents=(), attempts=())) == frozenset()


def test_attempts_share_one_intent_for_the_same_step() -> None:
    """MP L360-362: a retry reuses the Intent row instead of inserting one."""
    graph = replace(_ONE, attempts=(Attempt("a1", "s1", "i1"), Attempt("a2", "s1", "i1")))
    assert check_relationships(graph) == frozenset()


def test_plan_of_unknown_task_is_rejected() -> None:
    """MP L895-896: task_plans.task_id references tasks(id); a dangling Plan fails."""
    graph = replace(_LONE, plans=(Plan("p1", "t9"),))
    assert check_relationships(graph) == {PLAN_TASK_MISSING}


def test_step_chain_resolves_through_plan_to_task() -> None:
    """A Step reaches its Task through its Plan; a broken link is reported where it is."""
    legal = replace(_LONE, steps=(Step("s1", "t1", "p1"),))
    assert check_relationships(legal) == frozenset()
    dangling = replace(_LONE, plans=(Plan("p1", "t9"),), steps=(Step("s1", "t9", "p1"),))
    assert check_relationships(dangling) == {PLAN_TASK_MISSING}
    mismatched = replace(_LONE, plans=(Plan("p1", "t9"),), steps=(Step("s1", "t1", "p1"),))
    assert check_relationships(mismatched) == {PLAN_TASK_MISSING, STEP_PLAN_OTHER_TASK}


def test_active_plan_of_another_task_is_rejected() -> None:
    graph = replace(_DUAL, tasks=(Task("t1", "p2"), Task("t2", "p2")))
    assert check_relationships(graph) == {TASK_ACTIVE_PLAN_OTHER_TASK}


def test_unknown_active_plan_is_rejected() -> None:
    graph = replace(_ONE, tasks=(Task("t1", "p9"),))
    assert check_relationships(graph) == {TASK_ACTIVE_PLAN_MISSING}


def test_step_plan_of_another_task_is_rejected() -> None:
    graph = replace(_DUAL, steps=(Step("s1", "t1", "p2"), Step("s2", "t2", "p2")))
    assert check_relationships(graph) == {STEP_PLAN_OTHER_TASK}


def test_unknown_step_plan_is_rejected() -> None:
    graph = replace(_ONE, steps=(Step("s1", "t1", "p9"),))
    assert check_relationships(graph) == {STEP_PLAN_MISSING}


def test_intent_on_unknown_step_is_rejected() -> None:
    graph = replace(_ONE, intents=(Intent("i1", "s1"), Intent("i9", "s9")))
    assert check_relationships(graph) == {INTENT_STEP_MISSING}


def test_second_intent_for_one_step_is_rejected() -> None:
    graph = replace(_ONE, intents=(Intent("i1", "s1"), Intent("i9", "s1")))
    assert check_relationships(graph) == {INTENT_STEP_NOT_UNIQUE}


def test_attempt_on_unknown_step_is_rejected() -> None:
    graph = replace(_ONE, attempts=(Attempt("a1", "s9", "i1"),))
    assert check_relationships(graph) == {ATTEMPT_STEP_MISSING}


def test_attempt_with_unknown_intent_is_rejected() -> None:
    graph = replace(_ONE, attempts=(Attempt("a1", "s1", "i9"),))
    assert check_relationships(graph) == {ATTEMPT_INTENT_MISSING}


def test_attempt_intent_of_another_step_is_rejected() -> None:
    graph = replace(_DUAL, attempts=(Attempt("a1", "s1", "i1"), Attempt("a2", "s2", "i1")))
    assert check_relationships(graph) == {ATTEMPT_INTENT_OTHER_STEP}


def test_attempt_missing_step_short_circuits_the_intent_half() -> None:
    """Both halves broken reports the Step half only; each half alone is its own label."""
    both_broken = replace(_ONE, attempts=(Attempt("a1", "s9", "i9"),))
    assert check_relationships(both_broken) == {ATTEMPT_STEP_MISSING}
    assert check_relationships(replace(_ONE, attempts=(Attempt("a1", "s9", "i1"),))) == {
        ATTEMPT_STEP_MISSING
    }
    assert check_relationships(replace(_ONE, attempts=(Attempt("a1", "s1", "i9"),))) == {
        ATTEMPT_INTENT_MISSING
    }


def test_repeated_ids_of_every_kind_are_reported_identically_in_either_order() -> None:
    """Five kinds, each repeating an id across rows that disagree in other columns."""
    cases = (
        (
            "task.duplicate_id",
            replace(_ONE, tasks=(Task("t1", "p1"), Task("t1"))),
        ),
        (
            "plan.duplicate_id",
            replace(_ONE, plans=(Plan("p1", "t1"), Plan("p1", "t9"))),
        ),
        (
            "step.duplicate_id",
            replace(_ONE, steps=(Step("s1", "t1", "p1"), Step("s1", "t2", "p2"))),
        ),
        (
            "intent.duplicate_id",
            replace(_ONE, intents=(Intent("i1", "s1"), Intent("i1", "s2"))),
        ),
        (
            "attempt.duplicate_id",
            replace(_ONE, attempts=(Attempt("a1", "s1", "i1"), Attempt("a1", "s2", "i2"))),
        ),
    )
    for expected, graph in cases:
        _assert_order_independent(graph, {expected})


def test_duplicate_ids_of_several_kinds_are_all_reported() -> None:
    """Every kind that repeats an id is labelled, not just the first one found."""
    graph = replace(
        _DUAL,
        tasks=(Task("t1", "p1"), Task("t1", "p2")),
        intents=(Intent("i1", "s1"), Intent("i1", "s2")),
    )
    _assert_order_independent(graph, {"task.duplicate_id", "intent.duplicate_id"})


def test_repeated_ids_stop_the_check_before_any_relationship_diagnosis() -> None:
    """Fail-closed: one ambiguous id anywhere suppresses every relationship label.

    The graph below also carries a dangling ``active_plan_id``, a Plan naming an
    unknown Task, a Step naming an unknown Plan and an Attempt naming an unknown
    Step and Intent; none of them may be reported while ids are ambiguous.
    """
    graph = MemoryGraph(
        tasks=(Task("t1", "p9"),),
        plans=(Plan("p1", "t9"), Plan("p1", "t1")),
        steps=(Step("s1", "t1", "p9"),),
        intents=(Intent("i1", "s1"), Intent("i1", "s9")),
        attempts=(Attempt("a1", "s9", "i9"), Attempt("a1", "s1", "i1")),
    )
    _assert_order_independent(
        graph, {"plan.duplicate_id", "intent.duplicate_id", "attempt.duplicate_id"}
    )
    assert PLAN_TASK_MISSING not in check_relationships(graph)
    assert TASK_ACTIVE_PLAN_MISSING not in check_relationships(graph)


def test_ambiguous_plan_reference_is_never_resolved_by_row_order() -> None:
    """Task.active_plan_id and Step.plan_id may not pick a first or last Plan row.

    One row would satisfy the references and the other would break them, so a
    first-wins or last-wins index would decide the verdict by row order.
    """
    graph = replace(_ONE, plans=(Plan("p1", "t9"), Plan("p1", "t1")))
    _assert_order_independent(graph, {"plan.duplicate_id"})


def test_ambiguous_intent_reference_is_never_resolved_by_row_order() -> None:
    """Attempt.operation_id may not pick a first or last Intent row either.

    One row carries the Attempt's own Step and the other a different Step, so a
    first-wins index would report a different set for the two orders.
    """
    graph = replace(_ONE, intents=(Intent("i1", "s1"), Intent("i1", "s2")))
    _assert_order_independent(graph, {"intent.duplicate_id"})


def test_a_repeat_after_the_first_two_rows_is_found_in_every_kind() -> None:
    """Row 3 and row 5 share an id in each kind, forwards and backwards.

    Every case keeps the five-chain base and replaces one kind with five rows whose
    last row repeats the third row's id as a variant, so a detector that compares
    only the first two rows, or that lets a first or last row stand in as the
    parent, cannot pass. The displaced id also leaves a dangling reference behind,
    so each exact-set assertion re-checks fail-closed as well.
    """
    assert check_relationships(_FIVE) == frozenset()  # all ids distinct, 25 rows
    cases = (
        (
            "task.duplicate_id",
            (
                Task("t1", "p1"),
                Task("t2", "p2"),
                Task("t3", "p3"),
                Task("t4", "p4"),
                Task("t3", "p9"),
            ),
        ),
        (
            "plan.duplicate_id",
            (
                Plan("p1", "t1"),
                Plan("p2", "t2"),
                Plan("p3", "t3"),
                Plan("p4", "t4"),
                Plan("p3", "t9"),
            ),
        ),
        (
            "step.duplicate_id",
            (
                Step("s1", "t1", "p1"),
                Step("s2", "t2", "p2"),
                Step("s3", "t3", "p3"),
                Step("s4", "t4", "p4"),
                Step("s3", "t9", "p9"),
            ),
        ),
        (
            "intent.duplicate_id",
            (
                Intent("i1", "s1"),
                Intent("i2", "s2"),
                Intent("i3", "s3"),
                Intent("i4", "s4"),
                Intent("i3", "s9"),
            ),
        ),
        (
            "attempt.duplicate_id",
            (
                Attempt("a1", "s1", "i1"),
                Attempt("a2", "s2", "i2"),
                Attempt("a3", "s3", "i3"),
                Attempt("a4", "s4", "i4"),
                Attempt("a3", "s9", "i9"),
            ),
        ),
    )
    for expected, rows in cases:
        field = expected.split(".")[0] + "s"
        _assert_order_independent(replace(_FIVE, **{field: rows}), {expected})


def test_one_id_string_shared_by_several_kinds_is_not_a_duplicate() -> None:
    """Ids are per kind: one string reused in all five kinds is legal, either order.

    No kind repeats an id of its own, so this graph is also the all-unique contrast
    for the five kinds; it is legal as a whole (every reference resolves and every
    composite key matches).
    """
    graph = MemoryGraph(
        tasks=(Task("x", "p1"),),
        plans=(Plan("p1", "x"),),
        steps=(Step("x", "x", "p1"),),
        intents=(Intent("x", "x"),),
        attempts=(Attempt("x", "x", "x"),),
    )
    _assert_order_independent(graph, set())
