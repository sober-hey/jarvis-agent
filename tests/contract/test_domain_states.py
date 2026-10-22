"""Consistency tests: machine-readable contracts vs. the Python state projection.

Authority: ``contracts/task_states.yaml``, ``contracts/step_states.yaml`` and
``contracts/operation_intent.schema.json`` are the single source of truth.
``jarvis.domain.states`` is a projection of them; these tests compare the two sides
in both directions, so drift on either side (missing or extra states, missing or
extra terminal states) fails.

Deliberately out of scope: transition rules and guard semantics, any transition
service, and Plan / Attempt state sets (they have no contract source yet, so nothing
here asserts anything about them).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import cast

import pytest
import yaml

from jarvis.domain.states import (
    STEP_TERMINAL_STATES,
    TASK_TERMINAL_STATES,
    OperationIntentState,
    StepState,
    TaskState,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"

TASK_STATES_CONTRACT = "task_states.yaml"
STEP_STATES_CONTRACT = "step_states.yaml"
OPERATION_INTENT_SCHEMA = "operation_intent.schema.json"

STATE_ENUMS: tuple[type[StrEnum], ...] = (TaskState, StepState, OperationIntentState)


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"missing file: {path}")
    return path.read_text(encoding="utf-8")


def _as_mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"{context}: expected a mapping, got {type(value).__name__}")
    return cast("dict[str, object]", value)


def _as_sequence(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise AssertionError(f"{context}: expected a list, got {type(value).__name__}")
    return cast("list[object]", value)


def _as_str(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise AssertionError(f"{context}: expected a string, got {type(value).__name__}")
    return value


def _string_list(document: dict[str, object], key: str, context: str) -> list[str]:
    items = _as_sequence(document.get(key), f"{context}.{key}")
    return [_as_str(item, f"{context}.{key}[]") for item in items]


def _contract_document(name: str) -> dict[str, object]:
    raw: object = yaml.safe_load(_read_text(CONTRACTS_DIR / name))
    return _as_mapping(raw, name)


def _operation_intent_enum_values() -> list[str]:
    context = OPERATION_INTENT_SCHEMA
    raw: object = json.loads(_read_text(CONTRACTS_DIR / context))
    document = _as_mapping(raw, context)
    properties = _as_mapping(document.get("properties"), f"{context}.properties")
    state = _as_mapping(properties.get("state"), f"{context}.properties.state")
    return _string_list(state, "enum", f"{context}.properties.state")


def _members(state_enum: type[StrEnum]) -> tuple[StrEnum, ...]:
    # Enum classes are iterable through their metaclass; the cast tells that to type checkers.
    return tuple(cast("Iterable[StrEnum]", state_enum))


def _values(state_enum: type[StrEnum]) -> tuple[str, ...]:
    return tuple(member.value for member in _members(state_enum))


def _assert_states_match_contract(
    state_enum: type[StrEnum], contract_values: list[str], context: str
) -> None:
    projected = _values(state_enum)
    assert set(projected) == set(contract_values), (
        f"{state_enum.__name__} differs from {context}: "
        f"missing={sorted(set(contract_values) - set(projected))} "
        f"extra={sorted(set(projected) - set(contract_values))}"
    )


def _assert_terminal_matches_contract(
    state_enum: type[StrEnum], contract_name: str, projected: set[str]
) -> None:
    declared = set(_string_list(_contract_document(contract_name), "terminal", contract_name))
    states = set(_values(state_enum))
    assert declared, f"{contract_name}: no terminal states declared"
    assert declared <= states, f"{contract_name}: terminal states outside the state set"
    assert projected == declared, (
        f"{state_enum.__name__} terminal projection differs from {contract_name}: "
        f"missing={sorted(declared - projected)} extra={sorted(projected - declared)}"
    )


def test_task_state_matches_task_states_contract() -> None:
    context = TASK_STATES_CONTRACT
    _assert_states_match_contract(
        TaskState, _string_list(_contract_document(context), "states", context), context
    )


def test_step_state_matches_step_states_contract() -> None:
    context = STEP_STATES_CONTRACT
    _assert_states_match_contract(
        StepState, _string_list(_contract_document(context), "states", context), context
    )


def test_operation_intent_state_matches_schema_enum() -> None:
    _assert_states_match_contract(
        OperationIntentState, _operation_intent_enum_values(), OPERATION_INTENT_SCHEMA
    )


def test_task_terminal_projection_matches_contract() -> None:
    _assert_terminal_matches_contract(
        TaskState, TASK_STATES_CONTRACT, {state.value for state in TASK_TERMINAL_STATES}
    )


def test_step_terminal_projection_matches_contract() -> None:
    _assert_terminal_matches_contract(
        StepState, STEP_STATES_CONTRACT, {state.value for state in STEP_TERMINAL_STATES}
    )


def test_state_enums_have_no_alias_and_no_duplicate_value() -> None:
    for state_enum in STATE_ENUMS:
        values = _values(state_enum)
        assert len(values) == len(set(values)), f"{state_enum.__name__}: duplicate member values"
        declared = cast("Mapping[str, StrEnum]", state_enum.__members__)
        assert len(_members(state_enum)) == len(declared), (
            f"{state_enum.__name__}: alias members hide values behind another name"
        )


def test_state_enums_accept_their_own_values() -> None:
    for state_enum in STATE_ENUMS:
        for member in _members(state_enum):
            assert state_enum(member.value) is member


def test_state_enums_reject_values_outside_their_contract_set() -> None:
    unknown = "NOT_A_DECLARED_STATE"
    for state_enum in STATE_ENUMS:
        with pytest.raises(ValueError):
            state_enum(unknown)
    # A value declared by another contract's enum is still invalid for this enum.
    with pytest.raises(ValueError):
        TaskState(str(StepState.COMPLETED))


def test_state_enums_are_pure_str_enums() -> None:
    for state_enum in STATE_ENUMS:
        assert issubclass(state_enum, StrEnum)
    assert json.dumps(TaskState.READY) == '"READY"'
    assert f"{StepState.PENDING}" == "PENDING"
