"""Pure state projections of the K0 machine-readable contracts.

Single source of truth:

- ``contracts/task_states.yaml`` -> :class:`TaskState`, ``TASK_TERMINAL_STATES``
- ``contracts/step_states.yaml`` -> :class:`StepState`, ``STEP_TERMINAL_STATES``
- ``contracts/operation_intent.schema.json`` -> :class:`OperationIntentState`

This module is a projection only: it holds no state, no transition rule and no
transition service; it reads no contract file at run time, so it depends on no
project path; and it imports nothing but :mod:`enum` (Master Plan section 29: the
domain layer does not depend on SQLite).

Drift between this projection and the contracts is meant to fail a test, not to be
resolved by a runtime lookup: ``tests/contract/test_domain_states.py`` parses the
contracts and compares them against these definitions in both directions.

Deliberately out of scope:

- ``Plan`` and ``Attempt`` state sets have no contract source yet (design
  decisions D-01 / D-02 in ``docs/design/DOMAIN_CONTRACT_DESIGN.md``), so no enum is
  projected for them here;
- the Task and Step contracts declare a ``terminal`` list, so those two terminal
  constants are projected below. The Operation Intent schema declares none, so no
  terminal constant exists for :class:`OperationIntentState`; add one here only
  once the contract declares it.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "STEP_TERMINAL_STATES",
    "TASK_TERMINAL_STATES",
    "OperationIntentState",
    "StepState",
    "TaskState",
]


class TaskState(StrEnum):
    """Task states of ``contracts/task_states.yaml`` (K0 set, 10 values)."""

    CREATED = "CREATED"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    RETRY_WAIT = "RETRY_WAIT"
    BLOCKED = "BLOCKED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class StepState(StrEnum):
    """Step states of ``contracts/step_states.yaml`` (K0 set, 11 values)."""

    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    PREPARING = "PREPARING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    RETRY_WAIT = "RETRY_WAIT"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class OperationIntentState(StrEnum):
    """Intent states of the ``state`` enum in ``contracts/operation_intent.schema.json``."""

    PREPARED = "PREPARED"
    PUBLISHING = "PUBLISHING"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


# Terminal task states, projected from the ``terminal`` list of ``task_states.yaml``.
TASK_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELED}
)

# Terminal step states, projected from the ``terminal`` list of ``step_states.yaml``.
STEP_TERMINAL_STATES: frozenset[StepState] = frozenset(
    {StepState.COMPLETED, StepState.FAILED, StepState.CANCELED}
)
