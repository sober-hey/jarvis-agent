"""JARVIS domain layer: contract projections only, no persistence, no execution.

Everything in this package is derived from the machine-readable contracts under
``contracts/``; the contracts remain the single source of truth and the projections
are kept honest by consistency tests. Following Master Plan section 29, ``domain/``
must not depend on SQLite, and it must not execute anything.
"""

from __future__ import annotations

from jarvis.domain.states import (
    STEP_TERMINAL_STATES,
    TASK_TERMINAL_STATES,
    OperationIntentState,
    StepState,
    TaskState,
)

__all__ = [
    "STEP_TERMINAL_STATES",
    "TASK_TERMINAL_STATES",
    "OperationIntentState",
    "StepState",
    "TaskState",
]
