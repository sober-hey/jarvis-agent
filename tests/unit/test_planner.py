"""K0-006 pure DRAFT planner; no execution or persistence authorization."""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Protocol, cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from jarvis.planner import DRAFT_STATUS, RUNTIME_ENABLED, Planner, TaskSpec

ROOT = Path(__file__).resolve().parents[2]
PHASES = (
    "input_snapshot",
    "inventory_generation",
    "prepare_output",
    "persist_intent",
    "publish_no_clobber",
    "reconcile_result",
    "independent_verification",
)


def _payload(**changes: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "request_key": "inventory-example-001",
        "task_kind": "inventory.generate",
        "input": {"root": "/authorized/不存在"},
        "output": {"name": "inventory.json"},
    } | changes


def _validate_schema(schema: object, record: object) -> None:
    class Validator(Protocol):
        def __init__(self, schema: object) -> None: ...

        def validate(self, instance: object) -> None: ...

    cast("type[Validator]", Draft202012Validator)(schema).validate(record)


def test_same_input_produces_identical_plan_and_serialized_bytes() -> None:
    spec = TaskSpec.model_validate(_payload())
    first = Planner().plan(spec)
    assert first == Planner().plan(spec)
    assert first.model_dump_json() == Planner().plan(_payload()).model_dump_json()
    assert DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert RUNTIME_ENABLED is False


@pytest.mark.parametrize(
    "changes",
    [
        {"request_key": "another-request"},
        {"input": {"root": "/another/input"}},
    ],
)
def test_different_inputs_produce_different_plans(changes: dict[str, Any]) -> None:
    first = Planner().plan(_payload())
    second = Planner().plan(_payload(**changes))
    assert first != second
    assert first.id != second.id
    assert first.plan_digest != second.plan_digest
    assert {step.operation_key for step in first.steps}.isdisjoint(
        step.operation_key for step in second.steps
    )


def test_mapping_insertion_order_does_not_change_plan() -> None:
    payload = _payload()
    assert Planner().plan(payload) == Planner().plan(dict(reversed(tuple(payload.items()))))


def test_operation_keys_are_unique_across_a_batch_and_stable_on_replay() -> None:
    plans = [
        Planner().plan(_payload(request_key=f"request-{index}", input={"root": f"/root/{index}"}))
        for index in range(64)
    ]
    keys = [step.operation_key for plan in plans for step in plan.steps]
    assert len(keys) == len(set(keys))
    for plan in plans:
        replay = Planner().plan(plan.task_spec)
        assert replay.steps == plan.steps
        for step in plan.steps:
            assert len(step.operation_key) == 64
            assert int(step.operation_key, 16) >= 0
            assert step.logical_effect_id
            assert (step.task_id, step.plan_id) == (plan.task_id, plan.id)


def test_k0_step_order_and_internal_phase_order_are_fixed() -> None:
    plan = Planner().plan(_payload())
    # MP §11.3: phases belong to ONE Step, not seven independently scheduled effects.
    assert [(step.ordinal, step.step_type) for step in plan.steps] == [(1, "inventory.generate")]
    assert plan.steps[0].phases == PHASES
    assert plan.steps[0].state == "PENDING"
    assert plan.steps[0].required is True
    assert plan.steps[0].version == 0
    assert plan.steps[0].preconditions
    assert plan.steps[0].expected_result
    assert plan.steps[0].verification_requirements
    for _ in range(8):
        assert Planner().plan(_payload()).steps == plan.steps


def test_same_request_key_keeps_task_identity_but_changes_candidate_content() -> None:
    first = Planner().plan(_payload())
    second = Planner().plan(_payload(input={"root": "/different"}))
    assert first.task_id == second.task_id
    assert first.plan_digest != second.plan_digest
    # Pure planning cannot know a prior request; repository must reject the conflict.
    assert first.steps[0].operation_key != second.steps[0].operation_key


def test_no_implicit_path_or_unicode_normalization() -> None:
    planner = Planner()
    assert planner.plan(_payload(input={"root": "/a/"})) != planner.plan(
        _payload(input={"root": "/a"})
    )
    assert planner.plan(_payload(input={"root": "/caf\u00e9"})) != planner.plan(
        _payload(input={"root": "/cafe\u0301"})
    )


def test_records_match_exact_draft_schemas_without_in_memory_extensions() -> None:
    plan = Planner().plan(_payload())
    for name, records in (
        ("plan", [plan.to_record()]),
        ("step", [step.to_record() for step in plan.steps]),
    ):
        schema = json.loads((ROOT / f"contracts/draft/{name}.schema.json").read_text())
        for record in records:
            _validate_schema(schema, record)
            assert set(record) == set(schema["required"])
    assert "steps" not in plan.to_record()
    assert "operation_key" not in plan.steps[0].to_record()
    assert plan.created_at_ms == 0  # DRAFT sentinel, not an observed wall-clock time.
    assert plan.state == "ACTIVE"  # Known schema predicate only, NOT an activation grant.


def test_task_spec_matches_existing_valid_contract_example() -> None:
    payload = json.loads((ROOT / "contracts/examples/valid_task.json").read_text())
    assert TaskSpec.model_validate(payload).model_dump(mode="json") == payload
    schema = json.loads((ROOT / "contracts/task_spec.schema.json").read_text())
    _validate_schema(schema, Planner().plan(payload).task_spec.model_dump(mode="json"))


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"task_kind": "shell.execute"},
        {"request_key": ""},
        {"request_key": 42},
        {"request_key": "bad\x00key"},
        {"request_key": "bad\ud800key"},
        {"input": {"root": "relative/path"}},
        {"input": {"root": ""}},
        {"input": {"root": "/a/../b"}},
        {"input": {"root": "/a/./b"}},
        {"input": {"root": "/bad\x00path"}},
        {"input": {"root": "/bad\ud800path"}},
        {"input": {"root": 12}},
        {"input": {"root": "/valid", "extra": True}},
        {"output": {"name": "../inventory.json"}},
        {"output": {"name": "inventory.json", "extra": True}},
        {"extra": True},
    ],
)
def test_invalid_task_specs_fail_closed(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Planner().plan(_payload(**changes))


@pytest.mark.parametrize(
    "missing", ["schema_version", "request_key", "task_kind", "input", "output"]
)
def test_required_task_spec_fields(missing: str) -> None:
    payload = _payload()
    del payload[missing]
    with pytest.raises(ValidationError):
        Planner().plan(payload)


def test_inputs_and_outputs_are_not_mutable_aliases() -> None:
    payload = _payload()
    plan = Planner().plan(payload)
    before = plan.model_dump_json()
    payload["input"]["root"] = "/changed"
    record = plan.to_record()
    record["state"] = "forged"
    assert plan.model_dump_json() == before
    with pytest.raises(ValidationError):
        plan.task_spec.input.root = "/changed"
    with pytest.raises(ValidationError):
        plan.steps[0].ordinal = 99
    assert isinstance(plan.steps, tuple)


def test_planning_does_not_read_files_database_clock_or_randomness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = Planner()
    spec = TaskSpec.model_validate(_payload())
    expected = planner.plan(spec)

    def denied(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("planner attempted an external-state dependency")

    with monkeypatch.context() as patch:
        for owner, attribute in (
            (builtins, "open"),
            (Path, "open"),
            (Path, "stat"),
            (Path, "resolve"),
            (os, "open"),
            (os, "stat"),
            (os, "listdir"),
            (os, "scandir"),
            (os, "getcwd"),
            (os, "urandom"),
            (sqlite3, "connect"),
            (time, "time"),
            (time, "time_ns"),
        ):
            patch.setattr(owner, attribute, denied)
        actual = planner.plan(spec)
    assert actual == expected


def test_plan_digest_covers_spec_and_ordered_step_definition() -> None:
    plan = Planner().plan(_payload())
    basis = {
        "planner_revision": "jarvis.k0.planner.draft.v1",
        "task_spec": _payload(),
        "task_id": plan.task_id,
        "plan_version": 1,
        "state": "ACTIVE",
        "created_at_ms": 0,
        "steps": [
            step.model_dump(
                mode="json",
                exclude={
                    "schema_version",
                    "id",
                    "task_id",
                    "plan_id",
                    "logical_effect_id",
                    "operation_key",
                },
            )
            for step in plan.steps
        ],
    }
    encoded = json.dumps(
        {"namespace": "jarvis.k0.plan.draft.v1", "payload": basis},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    assert plan.plan_digest == hashlib.sha256(encoded).hexdigest()


def test_operation_key_binds_task_and_logical_effect_identity() -> None:
    plan = Planner().plan(_payload())
    step = plan.steps[0]
    encoded = json.dumps(
        {
            "namespace": "jarvis.k0.operation.draft.v1",
            "payload": [plan.task_id, step.logical_effect_id],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    assert step.operation_key == hashlib.sha256(encoded).hexdigest()


def test_bypassed_model_validation_does_not_bypass_planner_validation() -> None:
    spec = TaskSpec.model_validate(_payload())
    forged = spec.model_copy(update={"request_key": ""})
    with pytest.raises(ValidationError):
        Planner().plan(forged)
    nested = spec.input.model_copy(update={"root": "relative"})
    with pytest.raises(ValidationError):
        Planner().plan(_payload(input=nested))


def test_determinism_across_processes_and_hash_seeds(tmp_path: Path) -> None:
    script = (
        "import json,sys; from jarvis.planner import Planner; "
        "print(Planner().plan(json.loads(sys.argv[1])).model_dump_json())"
    )
    outputs = [
        subprocess.run(
            [sys.executable, "-c", script, json.dumps(_payload())],
            check=True,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=os.environ | {"PYTHONHASHSEED": seed, "TZ": timezone},
            timeout=15,
        ).stdout
        for seed, timezone in (("1", "UTC"), ("987654", "Asia/Shanghai"))
    ]
    assert outputs[0] == outputs[1]
    assert outputs[0].strip() == Planner().plan(_payload()).model_dump_json()
