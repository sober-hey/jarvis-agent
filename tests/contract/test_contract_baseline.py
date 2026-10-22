"""K0-002 contract baseline parsing tests for the draft Contract Pack.

Scope: parseability and internal structural consistency of the six YAML
contracts, the six JSON Schemas, and the TaskSpec examples, plus one
cross-document check of the Task transition table against Master Plan
section 7.2.

Deliberately out of scope: transition guard semantics (the guards in the
draft are placeholders), transition counts, draft lifecycle status, and any
claim that the Contract Pack is frozen or verified.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, cast

import pytest
import yaml
from jsonschema import validators

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"
PLAN_PATH = (
    REPO_ROOT
    / "docs/planning"
    / "JARVIS 独立智能体系统工程开发总规划-JARVIS_Engineering_Master_Plan_v1.1.1.md"
)

YAML_CONTRACTS: tuple[str, ...] = (
    "database_constraints.yaml",
    "execution_invariants.yaml",
    "step_states.yaml",
    "step_transitions.yaml",
    "task_states.yaml",
    "task_transitions.yaml",
)

SCHEMA_CONTRACTS: tuple[str, ...] = (
    "file_write_intent.schema.json",
    "operation_intent.schema.json",
    "reconciliation.schema.json",
    "snapshot_manifest.schema.json",
    "task_spec.schema.json",
    "tool_manifest.schema.json",
)

STATE_MACHINE_FILES: tuple[tuple[str, str], ...] = (
    ("task_states.yaml", "task_transitions.yaml"),
    ("step_states.yaml", "step_transitions.yaml"),
)

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

PLAN_TRANSITION_HEADING = "#### 7.2 K0 允许的任务状态转换"
PLAN_TRANSITION_TERMINATOR = "#### 7.3"


class ValidationErrorLike(Protocol):
    """Minimal view of a jsonschema validation error used by these tests."""

    @property
    def validator(self) -> str | None: ...

    @property
    def message(self) -> str: ...

    @property
    def path(self) -> Iterator[str | int]: ...


class ValidatorFacade(Protocol):
    """Minimal view of a jsonschema validator class/instance."""

    __name__: str

    def __init__(self, schema: object) -> None: ...

    @staticmethod
    def check_schema(schema: object) -> None: ...

    def iter_errors(self, instance: object) -> Iterator[ValidationErrorLike]: ...


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"missing contract file: {path}")
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


def _string_list(mapping: dict[str, object], key: str, context: str) -> list[str]:
    items = _as_sequence(mapping.get(key), f"{context}.{key}")
    return [_as_str(item, f"{context}.{key}[]") for item in items]


def _load_json_object(path: Path) -> dict[str, object]:
    raw: object = json.loads(_read_text(path))
    return _as_mapping(raw, path.name)


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    raw: object = yaml.safe_load(_read_text(path))
    return _as_mapping(raw, path.name)


def _load_contract(name: str) -> dict[str, object]:
    return _load_yaml_mapping(CONTRACTS_DIR / name)


def _make_validator(schema: dict[str, object]) -> ValidatorFacade:
    facade = cast("type[ValidatorFacade]", validators.validator_for(schema))
    facade.check_schema(schema)
    return facade(schema)


def _error_pairs(
    validator: ValidatorFacade, instance: object
) -> list[tuple[tuple[str, ...], str | None]]:
    pairs: list[tuple[tuple[str, ...], str | None]] = []
    for error in validator.iter_errors(instance):
        pairs.append((tuple(str(part) for part in error.path), error.validator))
    return pairs


def _transition_entries(name: str) -> list[dict[str, object]]:
    document = _load_contract(name)
    entries = _as_sequence(document.get("transitions"), f"{name}.transitions")
    if not entries:
        raise AssertionError(f"{name}: transitions is empty")
    return [_as_mapping(entry, f"{name}.transitions[]") for entry in entries]


def _contract_task_table() -> dict[str, set[str]]:
    states = _string_list(_load_contract("task_states.yaml"), "states", "task_states.yaml")
    table: dict[str, set[str]] = {state: set() for state in states}
    for entry in _transition_entries("task_transitions.yaml"):
        source = _as_str(entry.get("from"), "task_transitions.yaml.transitions[].from")
        target = _as_str(entry.get("to"), "task_transitions.yaml.transitions[].to")
        table[source].add(target)
    return table


def _plan_task_table() -> dict[str, set[str]]:
    text = _read_text(PLAN_PATH)
    start = text.find(PLAN_TRANSITION_HEADING)
    if start == -1:
        raise AssertionError(f"plan heading not found: {PLAN_TRANSITION_HEADING}")
    end = text.find(PLAN_TRANSITION_TERMINATOR, start)
    if end == -1:
        raise AssertionError(f"plan section terminator not found: {PLAN_TRANSITION_TERMINATOR}")
    table: dict[str, set[str]] = {}
    for line in text[start:end].splitlines():
        match = re.fullmatch(r"\|\s*([A-Z_]+)\s*\|\s*([^|]*?)\s*\|", line.strip())
        if match is None:
            continue
        targets = match.group(2)
        table[match.group(1)] = (
            set()
            if targets == "无"
            else {part.strip() for part in targets.split("、") if part.strip()}
        )
    return table


@pytest.mark.parametrize("name", YAML_CONTRACTS)
def test_all_yaml_contracts_parse(name: str) -> None:
    document = _load_contract(name)
    assert document, f"{name} parsed to an empty mapping"
    assert document.get("schema_version") == 1, f"{name}: unexpected schema_version"


@pytest.mark.parametrize("name", SCHEMA_CONTRACTS)
def test_all_schema_json_parse(name: str) -> None:
    schema = _load_json_object(CONTRACTS_DIR / name)
    assert schema, f"{name} parsed to an empty object"
    assert schema.get("$schema") == DRAFT_2020_12, f"{name}: unexpected dialect"


@pytest.mark.parametrize("name", SCHEMA_CONTRACTS)
def test_schema_meta_validation(name: str) -> None:
    schema = _load_json_object(CONTRACTS_DIR / name)
    facade = cast("type[ValidatorFacade]", validators.validator_for(schema))
    assert facade.__name__ == "Draft202012Validator", (
        f"{name}: dialect does not resolve to draft 2020-12"
    )
    facade.check_schema(schema)


@pytest.mark.parametrize("name", SCHEMA_CONTRACTS)
def test_schema_internal_consistency(name: str) -> None:
    schema = _load_json_object(CONTRACTS_DIR / name)
    properties = _as_mapping(schema.get("properties"), f"{name}.properties")
    required = _string_list(schema, "required", name)
    assert properties, f"{name}: properties is empty"
    assert required, f"{name}: required is empty"
    assert len(required) == len(set(required)), f"{name}: duplicate entries in required"
    missing = sorted(set(required) - set(properties))
    assert not missing, f"{name}: required keys missing from properties: {missing}"


def test_task_spec_examples() -> None:
    schema = _load_json_object(CONTRACTS_DIR / "task_spec.schema.json")
    validator = _make_validator(schema)

    valid = _error_pairs(
        validator, _load_json_object(CONTRACTS_DIR / "examples" / "valid_task.json")
    )
    assert valid == [], f"valid_task.json rejected: {valid}"

    invalid = _error_pairs(
        validator, _load_json_object(CONTRACTS_DIR / "examples" / "invalid_task.json")
    )
    assert invalid, "invalid_task.json was accepted by task_spec.schema.json"
    expected = {
        (("input", "root"), "pattern"),
        (("output", "name"), "const"),
        (("task_kind",), "const"),
    }
    missing = sorted(expected - set(invalid))
    assert not missing, f"invalid_task.json: expected violations not reported: {missing}"


@pytest.mark.parametrize(("states_name", "_transitions_name"), STATE_MACHINE_FILES)
def test_state_collections_are_unique(states_name: str, _transitions_name: str) -> None:
    document = _load_contract(states_name)
    states = _string_list(document, "states", states_name)
    terminal = _string_list(document, "terminal", states_name)
    assert states, f"{states_name}: states is empty"
    assert len(states) == len(set(states)), f"{states_name}: duplicate states"
    assert terminal, f"{states_name}: terminal is empty"
    assert len(terminal) == len(set(terminal)), f"{states_name}: duplicate terminal states"
    assert set(terminal) <= set(states), (
        f"{states_name}: terminal states are not declared in states"
    )


@pytest.mark.parametrize(("states_name", "transitions_name"), STATE_MACHINE_FILES)
def test_transition_state_references_are_defined(states_name: str, transitions_name: str) -> None:
    states_document = _load_contract(states_name)
    transitions_document = _load_contract(transitions_name)
    states = set(_string_list(states_document, "states", states_name))
    assert transitions_document.get("object") == states_document.get("object"), (
        f"{transitions_name} and {states_name} describe different objects"
    )
    for index, entry in enumerate(_transition_entries(transitions_name)):
        context = f"{transitions_name}.transitions[{index}]"
        source = _as_str(entry.get("from"), f"{context}.from")
        target = _as_str(entry.get("to"), f"{context}.to")
        guards = _string_list(entry, "guards", context)
        assert source in states, f"{context}: unknown source state {source!r}"
        assert target in states, f"{context}: unknown target state {target!r}"
        assert guards, f"{context}: guards is empty"


@pytest.mark.parametrize(("states_name", "transitions_name"), STATE_MACHINE_FILES)
def test_terminal_states_have_no_outgoing_edge(states_name: str, transitions_name: str) -> None:
    terminal = set(_string_list(_load_contract(states_name), "terminal", states_name))
    sources = {
        _as_str(entry.get("from"), f"{transitions_name}.transitions[].from")
        for entry in _transition_entries(transitions_name)
    }
    reopened = sorted(sources & terminal)
    assert not reopened, (
        f"{transitions_name}: terminal states have outgoing transitions: {reopened}"
    )


def test_task_table_matches_master_plan_section_7_2() -> None:
    contract = _contract_task_table()
    plan = _plan_task_table()
    assert plan, "no transition rows parsed from plan section 7.2"
    differences = {
        state: (sorted(contract.get(state, set())), sorted(plan.get(state, set())))
        for state in sorted(set(contract) | set(plan))
        if contract.get(state) != plan.get(state)
    }
    assert contract == plan, f"Task table differs from plan section 7.2: {differences}"
