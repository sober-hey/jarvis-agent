"""K0-006 deterministic Planner DRAFT; NOT_FROZEN / NOT_APPROVED.

Pure in-memory proposal only: no filesystem, database, clock, randomness, model,
executor or packaging/ dependency. Calling this module grants no execution right.
K0 MP §11.3 specifies ONE inventory.generate Step; snapshot, preparation,
publication and verification are ordered internal phases, not additional Steps.

Candidate choices, NOT decisions closing D-01/D-03/D-04/D-06/D-08/D-11/D-12:
* Canonical JSON is UTF-8, sorted keys, compact separators, no NaN/Infinity.
* Task identity hashes request_key only. A reused key with different content must
  still be rejected by the repository; pure planning cannot detect past requests.
* plan_digest covers the entire TaskSpec and Step definition, plan version,
  candidate state and timestamp sentinel; it excludes derived IDs and itself.
* logical_effect_id binds the complete requested effect definition. operation_key
  hashes (task_id, logical_effect_id), never Attempt/epoch/time. SHA-256 provides
  collision-resistant global names, not a mathematical no-collision guarantee.
* created_at_ms=0 is an offline sentinel, NOT an observed creation timestamp.
  ACTIVE is the sole known Plan predicate, NOT a persisted activation or grant.
* Step details and operation_key are in-memory extensions, NOT new DB columns.
  to_record() emits ONLY the existing strict draft schema fields. D-08/D-12
  recovery carriers remain unresolved; projections alone cannot restore a plan.

TaskRepository currently has NO Plan/Step insertion API (its fixture tests insert
rows directly). This module deliberately neither adds one nor bypasses its
flock/epoch/CAS/transaction fences. Future persistence needs a separate approved
adapter and atomic graph activation; do not insert these records as runtime work.
Replanning, retries, cancellation and unknown-effect reconciliation belong to the
executor/repository, not to this deterministic transformation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.domain.states import StepState

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False
_PLANNER_REVISION = "jarvis.k0.planner.draft.v1"


def _digest(namespace: str, payload: object) -> str:
    """Domain-separated candidate hashes; never Python's process-salted hash()."""
    encoded = json.dumps(
        {"namespace": namespace, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utf8_text(value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError("nonempty text without NUL required")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError("text must be safely UTF-8 encodable") from error
    return value


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        revalidate_instances="always",
    )


class TaskInput(_FrozenModel):
    """Lexical authorization-root proposal; no existence or permission lookup."""

    root: str

    @field_validator("root")
    @classmethod
    def validate_root(cls, value: str) -> str:
        _utf8_text(value)
        if not value.startswith("/"):
            raise ValueError("input.root must be an absolute POSIX path")
        if any(part in {".", ".."} for part in value.split("/")):
            raise ValueError("dot path segments are not accepted")
        # Never resolve symlinks, expand ~, trim, normalize Unicode or inspect disk.
        return value


class TaskOutput(_FrozenModel):
    name: Literal["inventory.json"]


class TaskSpec(_FrozenModel):
    """Strict static projection of contracts/task_spec.schema.json.

    Additional lexical path/UTF-8 rejection is a DRAFT validation proposal only;
    the schema's shape alone does not establish filesystem safety/authorization.
    No required request field is defaulted or silently repaired.
    """

    schema_version: Literal[1]
    request_key: str = Field(min_length=1)
    task_kind: Literal["inventory.generate"]
    input: TaskInput
    output: TaskOutput

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be the exact integer 1")
        return value

    @field_validator("request_key")
    @classmethod
    def validate_request_key(cls, value: str) -> str:
        return _utf8_text(value)


class Step(_FrozenModel):
    """Immutable scheduling proposal plus an exact structural row projection."""

    schema_version: Literal[1] = 1
    id: str
    task_id: str
    plan_id: str
    ordinal: int
    logical_effect_id: str
    state: StepState = StepState.PENDING
    required: bool = True
    version: Literal[0] = 0
    operation_key: str
    step_type: Literal["inventory.generate"]
    input: TaskInput
    output: TaskOutput
    phases: tuple[str, ...]
    preconditions: tuple[str, ...]
    expected_result: str
    verification_requirements: tuple[str, ...]

    def to_record(self) -> dict[str, Any]:
        """Only step.schema.json fields; operation_key belongs to future Intent."""
        return self.model_dump(
            mode="json",
            include={
                "schema_version",
                "id",
                "task_id",
                "plan_id",
                "ordinal",
                "logical_effect_id",
                "state",
                "required",
                "version",
            },
        )


class Plan(_FrozenModel):
    """Detached DRAFT candidate, not an activated/persisted execution graph."""

    schema_version: Literal[1] = 1
    id: str
    task_id: str
    plan_version: Literal[1] = 1
    state: Literal["ACTIVE"] = "ACTIVE"
    plan_digest: str
    created_at_ms: Literal[0] = 0
    task_spec: TaskSpec
    steps: tuple[Step, ...]

    def to_record(self) -> dict[str, Any]:
        """Only plan.schema.json fields; Steps are separate structural records."""
        return self.model_dump(
            mode="json",
            include={
                "schema_version",
                "id",
                "task_id",
                "plan_version",
                "state",
                "plan_digest",
                "created_at_ms",
            },
        )


class Planner:
    """Stateless TaskSpec → Plan transformation for the one K0 task kind.

    Different request keys represent different tasks even with identical content.
    A nonexistent absolute root can be planned; execution must separately verify
    authorization, snapshot safety and all guards. No open-ended intent parsing.
    """

    __slots__ = ()

    def plan(self, task_spec: TaskSpec | Mapping[str, Any]) -> Plan:
        # Revalidate even an existing model: model_copy/model_construct can bypass
        # Pydantic validation. Dump also detaches all nested caller-owned mappings.
        spec = TaskSpec.model_validate(
            task_spec.model_dump(mode="python") if isinstance(task_spec, TaskSpec) else task_spec
        )
        task_id = "task-" + _digest(
            "jarvis.k0.task.request-key.draft.v1", {"request_key": spec.request_key}
        )
        definition = {
            "ordinal": 1,
            "step_type": spec.task_kind,
            "input": spec.input.model_dump(mode="json"),
            "output": spec.output.model_dump(mode="json"),
            "state": StepState.PENDING.value,
            "required": True,
            "version": 0,
            "phases": (
                "input_snapshot",
                "inventory_generation",
                "prepare_output",
                "persist_intent",
                "publish_no_clobber",
                "reconcile_result",
                "independent_verification",
            ),
            "preconditions": (
                "trusted_input_root_authorized",
                "safe_input_snapshot_within_k0_limits",
                "durable_intent_before_target_effect",
                "execution_guards_and_fences_satisfied",
            ),
            "expected_result": "deterministic_inventory_published_without_overwrite",
            "verification_requirements": (
                "published_bytes_match_prepared_sha256_and_size",
                "inventory_matches_controlled_snapshot",
                "independent_verification_evidence",
            ),
        }
        basis = {
            "planner_revision": _PLANNER_REVISION,
            "task_spec": spec.model_dump(mode="json"),
            "task_id": task_id,
            "plan_version": 1,
            "state": "ACTIVE",
            "created_at_ms": 0,
            "steps": [definition],
        }
        plan_digest = _digest("jarvis.k0.plan.draft.v1", basis)
        plan_id = "plan-" + plan_digest
        logical_effect_id = "effect-" + _digest(
            "jarvis.k0.effect.draft.v1",
            {"task_spec": spec.model_dump(mode="json"), "step": definition},
        )
        step = Step.model_validate(
            {
                **definition,
                "id": "step-" + _digest("jarvis.k0.step.draft.v1", [plan_id, 1]),
                "task_id": task_id,
                "plan_id": plan_id,
                "logical_effect_id": logical_effect_id,
                "operation_key": _digest(
                    "jarvis.k0.operation.draft.v1", [task_id, logical_effect_id]
                ),
                "input": spec.input,
                "output": spec.output,
                "state": StepState.PENDING,
            }
        )
        return Plan(
            id=plan_id,
            task_id=task_id,
            plan_digest=plan_digest,
            task_spec=spec,
            steps=(step,),
        )
