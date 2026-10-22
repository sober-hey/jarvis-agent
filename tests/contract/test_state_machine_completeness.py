"""Structural DRAFT coverage only; never runtime permission or safety approval.

An absent edge is not an explicit denial. Check the full Cartesian product,
including self-pairs and terminal sources, against persisted DENIED records.
"""

from __future__ import annotations

from itertools import product
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
KINDS = ("task", "step")


def _load(name: str) -> dict[str, Any]:
    path = CONTRACTS / name
    assert path.is_file(), f"missing contract: {name}"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{name}: expected mapping"
    return cast("dict[str, Any]", document)


def _assert_complete_pairs(states: list[str], document: dict[str, Any]) -> None:
    rules = [(row["from"], row["to"]) for row in document["transitions"]]
    raw_denied = document.get("denied_transitions")
    assert isinstance(raw_denied, list) and raw_denied, "explicit DENIED records missing"
    denied_rows = cast("list[dict[str, Any]]", raw_denied)
    denied = [(row["from"], row["to"]) for row in denied_rows]
    assert len(rules) == len(set(rules)), "duplicate transition pair"
    assert len(denied) == len(set(denied)), "duplicate DENIED pair"
    assert not set(rules) & set(denied), "rule/DENIED overlap"
    expected = set(product(states, repeat=2))
    actual = set(rules) | set(denied)
    assert actual == expected, (
        f"unclassified pairs={sorted(expected - actual)}; unknown pairs={sorted(actual - expected)}"
    )
    for row in denied_rows:
        assert row["decision"] == "DENIED", "denial must be explicit"
        assert isinstance(row["reason"], str) and row["reason"].strip(), "denial reason missing"
        assert row["approval_status"] == "NOT_APPROVED"


@pytest.mark.parametrize("kind", KINDS)
def test_state_set_equals_full_transition_endpoint_union(kind: str) -> None:
    states_doc = _load(f"{kind}_states.yaml")
    edges_doc = _load(f"{kind}_transitions.yaml")
    states = states_doc["states"]
    terminal = states_doc["terminal"]
    assert states_doc["object"] == edges_doc["object"] == kind.upper()
    assert states_doc["status"] == edges_doc["status"] == "DRAFT"
    assert states and all(isinstance(state, str) and state for state in states)
    assert len(states) == len(set(states)), "duplicate state"
    endpoints = {row[key] for row in edges_doc["transitions"] for key in ("from", "to")}
    assert set(states) == endpoints, (
        f"undefined endpoints={sorted(endpoints - set(states))}; "
        f"unreferenced states={sorted(set(states) - endpoints)}"
    )
    assert terminal and len(terminal) == len(set(terminal))
    assert set(terminal) <= set(states)
    sources = {row["from"] for row in edges_doc["transitions"]}
    assert set(states) - sources == set(terminal), "only declared terminal states may lack rules"


@pytest.mark.parametrize("kind", KINDS)
def test_every_pair_has_rule_or_explicit_denial(kind: str) -> None:
    _assert_complete_pairs(
        _load(f"{kind}_states.yaml")["states"], _load(f"{kind}_transitions.yaml")
    )


def _expression_refs(expression: list[Any], operator: str) -> set[str]:
    if expression[0] == operator:
        assert len(expression) == 2 and isinstance(expression[1], str)
        return {expression[1]}
    result: set[str] = set()
    for child in expression[1:]:
        if isinstance(child, list):
            result |= _expression_refs(cast("list[Any]", child), operator)
    return result


def _resolved_inputs(
    name: str, definitions: dict[str, Any], stack: tuple[str, ...] = ()
) -> set[str]:
    assert name in definitions, f"undefined guard: {name}"
    assert name not in stack, f"guard cycle: {(*stack, name)}"
    raw_expression = definitions[name]["predicate"]
    assert isinstance(raw_expression, list) and raw_expression, f"{name}: predicate missing"
    expression = cast("list[Any]", raw_expression)
    inputs = _expression_refs(expression, "fact")
    for dependency in _expression_refs(expression, "guard"):
        inputs |= _resolved_inputs(dependency, definitions, (*stack, name))
    return inputs


@pytest.mark.parametrize("kind", KINDS)
def test_every_transition_guard_has_full_definition_and_exact_reverse_links(kind: str) -> None:
    catalogue = _load("guards.yaml")
    registry = _load("transition_guards.yaml")
    definitions = catalogue["guards"]
    rows = _load(f"{kind}_transitions.yaml")["transitions"]
    assert set(definitions) == set(registry["guards"]), "guard catalogue/registry drift"
    for name, definition in definitions.items():
        assert definition["predicate"] == registry["guards"][name]["predicate"]
        inputs = _resolved_inputs(name, definitions)
        assert set(definition["inputs"]) == inputs
        for key, specification in definition["inputs"].items():
            assert specification == registry["facts"][key], f"{name}: input contract drift: {key}"
        assert definition["preconditions"] and definition["rejection_conditions"]
        assert definition["approval_status"] == "NOT_APPROVED"
        assert definition["verification_status"] == "NOT_IMPLEMENTED"
        assert definition["source_refs"]
        assert definition["source_status"] in {"SOURCE_IDENTIFIED", "SOURCE_MISSING"}
        if definition["source_status"] == "SOURCE_MISSING":
            assert definition["missing_sources"], f"{name}: missing-source explanation absent"
        assert set(definition["guard_dependencies"]) == _expression_refs(
            definition["predicate"], "guard"
        )
        expected = {row["id"] for row in rows if name in row["guards"]}
        links = definition["transitions"][kind.upper()]
        assert len(links) == len(set(links))
        assert set(links) == expected, f"{name}: stale {kind} reverse links"
    for row in rows:
        assert row["guards"] and len(row["guards"]) == len(set(row["guards"]))
        for name in row["guards"]:
            assert name in definitions, f"{row['id']}: undefined guard {name}"


def test_guard_catalogue_remains_non_authorizing_draft() -> None:
    catalogue = _load("guards.yaml")
    assert catalogue["schema_version"] == 1
    assert catalogue["status"] == "DRAFT"
    assert catalogue["approval_status"] == "NOT_APPROVED"
    assert catalogue["runtime_enabled"] is False
    assert catalogue["contract_pack_status"] == "NOT_FROZEN"
    assert catalogue["predicate_registry"] == "transition_guards.yaml"
    assert catalogue["failure_policy"] == "DENY_KEEP_SOURCE_AND_ROLLBACK_ALL_WRITES"


@pytest.mark.parametrize(
    "damage",
    [
        "remove_denial",
        "overlap",
        "duplicate_rule",
        "duplicate_denial",
        "unknown_state",
        "not_denied",
    ],
)
def test_completeness_check_rejects_corrupted_tables(damage: str) -> None:
    document = _load("task_transitions.yaml")
    if damage == "remove_denial":
        document["denied_transitions"].pop()
    elif damage == "overlap":
        document["denied_transitions"].append(
            dict(document["transitions"][0], decision="DENIED", reason="invalid overlap")
        )
    elif damage == "duplicate_rule":
        document["transitions"].append(document["transitions"][0])
    elif damage == "duplicate_denial":
        document["denied_transitions"].append(document["denied_transitions"][0])
    elif damage == "unknown_state":
        document["denied_transitions"][0]["from"] = "UNDECLARED"
    else:
        document["denied_transitions"][0]["decision"] = "ASSUMED_DENIED"
    with pytest.raises(AssertionError):
        _assert_complete_pairs(_load("task_states.yaml")["states"], document)


def test_dp1_missing_edge_sources_are_not_promoted_to_approval() -> None:
    catalogue = _load("guards.yaml")
    rows = {row["id"]: row for row in _load("step_transitions.yaml")["transitions"]}
    missing = catalogue["step_edges_with_missing_formal_source"]
    expected_groups = {
        "G1": {"S05", "S06", "S10", "S11", "S31", "S32"},
        "G2": {"S21", "S23"},
        "G3": {"S16", "S17", "S22"},
    }
    assert len(missing) == len({row["id"] for row in missing}) == 11
    assert {row["id"] for row in missing} == {
        row["id"] for row in rows.values() if row["source_status"] == "UNRESOLVED"
    }
    for group, edge_ids in expected_groups.items():
        assert {row["id"] for row in missing if row["group"] == group} == edge_ids
    for row in missing:
        edge = rows[row["id"]]
        assert (row["from"], row["to"], row["guards"]) == (edge["from"], edge["to"], edge["guards"])
        assert row["source_status"] == "SOURCE_MISSING"
        assert row["upstream_source_status"] == "UNRESOLVED"
        assert row["approval_status"] == "NOT_APPROVED"
        assert row["candidate_requirements"] and row["missing_source"]
        assert row["dp1_formal_definition_requirements"] and row["unresolved_formalization"]
        assert row["unresolved_dependencies"] == edge["unresolved_dependencies"]
        if row["group"] == "G3":
            assert row["disposition"] == "ALWAYS_DENY_PENDING_SAFETY_DECISION"
            assert catalogue["guards"]["g3_safety_decision_open"]["predicate"] == ["literal", False]
    assert catalogue["guards"]["fail_decision_policy_open"]["predicate"] == ["literal", False]


@pytest.mark.parametrize("corruption", ["undefined", "cycle"])
def test_guard_reference_check_denies_undefined_or_cyclic_dependencies(corruption: str) -> None:
    definitions = _load("guards.yaml")["guards"]
    definitions["fenced_commit"]["predicate"] = [
        "guard",
        "UNDEFINED_GUARD" if corruption == "undefined" else "fenced_commit",
    ]
    with pytest.raises(AssertionError, match="undefined guard|guard cycle"):
        _resolved_inputs("fenced_commit", definitions)
