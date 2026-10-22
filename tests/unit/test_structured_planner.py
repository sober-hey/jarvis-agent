"""K1-005 DRAFT: offline model proposals, never execution authority."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jarvis.mock_provider import MockProvider
from jarvis.model_provider import AuthError, ModelResponse, ModelUsage, TimeoutError
from jarvis.planner import Plan, Planner
from jarvis.repository import GuardRejectedError, StaleEpochError, TaskRepository, WorkerLock
from jarvis.structured_planner import (
    DRAFT_STATUS,
    RUNTIME_ENABLED,
    PlanError,
    PlanSchema,
    StructuredPlanner,
)

ROOT = Path(__file__).resolve().parents[2]
INTENT = "为 /authorized/示例 生成 inventory.json 清单"
REQUEST_KEY = "structured-planner-test-001"
SYSTEM_PROMPT = "将用户意图转换成候选计划，不执行任何操作。"


def _proposal() -> dict[str, Any]:
    return {
        "steps": [
            {
                "ordinal": 1,
                "step_type": "inventory.generate",
                "input": {"root": "/authorized/示例"},
                "output": {"name": "inventory.json"},
            }
        ]
    }


def _response() -> str:
    return json.dumps(_proposal(), ensure_ascii=False)


def _expected() -> Plan:
    return Planner().plan(
        {
            "schema_version": 1,
            "request_key": REQUEST_KEY,
            "task_kind": "inventory.generate",
            "input": {"root": "/authorized/示例"},
            "output": {"name": "inventory.json"},
        }
    )


def test_valid_json_produces_validated_k0_plan_and_separate_message_roles() -> None:
    provider = MockProvider([_response()])
    result = StructuredPlanner(provider, SYSTEM_PROMPT).plan(INTENT, request_key=REQUEST_KEY)
    assert isinstance(result, Plan)
    assert result.model_dump(mode="json") == _expected().model_dump(mode="json")
    assert result.steps[0].operation_key
    assert [(step.ordinal, step.step_type) for step in result.steps] == [(1, "inventory.generate")]
    assert len(provider.calls) == 1
    messages = provider.calls[0].messages
    assert [message["role"] for message in messages] == ["system", "user"]
    assert SYSTEM_PROMPT in str(messages[0]["content"])
    assert "steps" in str(messages[0]["content"])
    assert messages[1]["content"] == INTENT
    assert provider.calls[0].kwargs == {}
    assert DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert RUNTIME_ENABLED is False


def test_invalid_json_retries_until_success_with_three_total_calls() -> None:
    provider = MockProvider(["not JSON", '{"steps":', _response()])
    result = StructuredPlanner(provider, SYSTEM_PROMPT).plan(INTENT, request_key=REQUEST_KEY)
    assert result.model_dump(mode="json") == _expected().model_dump(mode="json")
    assert len(provider.calls) == 3
    assert all(call.messages == provider.calls[0].messages for call in provider.calls)


def test_three_invalid_responses_raise_plan_error_without_a_fourth_call() -> None:
    provider = MockProvider(["secret invalid response"] * 3 + [_response()])
    with pytest.raises(PlanError, match="3 attempts") as caught:
        StructuredPlanner(provider, SYSTEM_PROMPT).plan(INTENT, request_key=REQUEST_KEY)
    assert caught.value.attempts == 3
    assert "secret invalid response" not in str(caught.value)
    assert len(provider.calls) == 3


def test_complete_k0_json_response_is_accepted_without_repair() -> None:
    expected = _expected()
    provider = MockProvider([expected.model_dump_json()])
    result = StructuredPlanner(provider, SYSTEM_PROMPT).plan(INTENT, request_key=REQUEST_KEY)
    assert result.model_dump_json() == expected.model_dump_json()
    assert len(provider.calls) == 1


@pytest.mark.parametrize("field", ["operation_key", "ordinal", "preconditions"])
def test_invalid_complete_plan_is_rejected_and_retried(field: str) -> None:
    payload = _expected().model_dump(mode="json")
    del payload["steps"][0][field]
    provider = MockProvider([json.dumps(payload), _expected().model_dump_json()])
    result = StructuredPlanner(provider, SYSTEM_PROMPT).plan(INTENT, request_key=REQUEST_KEY)
    assert result.model_dump_json() == _expected().model_dump_json()
    assert len(provider.calls) == 2


def test_model_cannot_replace_caller_identity_with_a_valid_foreign_plan() -> None:
    foreign = _expected().task_spec.model_dump(mode="python")
    foreign["request_key"] = "model-chosen-request"
    provider = MockProvider([Planner().plan(foreign).model_dump_json(), _response()])
    result = StructuredPlanner(provider, SYSTEM_PROMPT).plan(INTENT, request_key=REQUEST_KEY)
    assert result.task_spec.request_key == REQUEST_KEY
    assert len(provider.calls) == 2


def test_same_input_and_response_produce_identical_plan_bytes() -> None:
    provider = MockProvider([_response()] * 2)
    planner = StructuredPlanner(provider, SYSTEM_PROMPT)
    first = planner.plan(INTENT, request_key=REQUEST_KEY)
    second = planner.plan(INTENT, request_key=REQUEST_KEY)
    independent = StructuredPlanner(MockProvider([_response()]), SYSTEM_PROMPT).plan(
        INTENT, request_key=REQUEST_KEY
    )
    assert first.model_dump_json() == second.model_dump_json() == independent.model_dump_json()
    assert first.steps[0].operation_key == second.steps[0].operation_key


def test_json_whitespace_and_key_order_do_not_change_plan() -> None:
    proposal = _proposal()
    proposal["steps"][0] = dict(reversed(tuple(proposal["steps"][0].items())))
    provider = MockProvider([_response(), json.dumps(proposal, indent=4)])
    planner = StructuredPlanner(provider, SYSTEM_PROMPT)
    assert (
        planner.plan(INTENT, request_key=REQUEST_KEY).model_dump_json()
        == planner.plan(INTENT, request_key=REQUEST_KEY).model_dump_json()
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "[]",
        "null",
        "{}",
        '{"steps":[]}',
        '{"steps":{},"extra":true}',
        '```json\n{"steps":[]}\n```',
        '{"steps":[],"steps":[]}',
        '{"steps":[],"unknown":NaN}',
        '{"steps":[],"unknown":Infinity}',
        _response() + " trailing text",
        _response().replace('"ordinal": 1', '"ordinal": true'),
        _response().replace('"ordinal": 1', '"ordinal": 2'),
        _response().replace('"ordinal": 1', '"ordinal": "1"'),
        _response().replace('"ordinal": 1', '"ordinal": 1, "ordinal": 1'),
        _response().replace('"inventory.generate"', '"shell.execute"'),
        _response().replace('"inventory.json"', '"../outside.json"'),
        _response().replace('"root":', '"operation_key": "model-grant", "root":'),
        json.dumps({"steps": _proposal()["steps"] * 2}),
    ],
)
def test_bad_json_or_schema_is_rejected_then_retried(invalid: str) -> None:
    provider = MockProvider([invalid, _response()])
    result = StructuredPlanner(provider, SYSTEM_PROMPT).plan(INTENT, request_key=REQUEST_KEY)
    assert result.model_dump(mode="json") == _expected().model_dump(mode="json")
    assert len(provider.calls) == 2


@pytest.mark.parametrize(
    "field", ["operation_key", "id", "plan_id", "task_id", "logical_effect_id"]
)
def test_plan_schema_rejects_missing_or_forged_step_identity(field: str) -> None:
    payload = _expected().model_dump(mode="json")
    del payload["steps"][0][field]
    with pytest.raises(ValidationError):
        PlanSchema.model_validate_json(json.dumps(payload))
    payload["steps"][0][field] = "forged"
    with pytest.raises(ValidationError):
        PlanSchema.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("steps", [[], [2], [1, 1], [2, 1]])
def test_plan_schema_rejects_empty_duplicate_or_reordered_steps(steps: list[int]) -> None:
    payload = _expected().model_dump(mode="json")
    original = payload["steps"][0]
    payload["steps"] = [{**original, "ordinal": ordinal} for ordinal in steps]
    with pytest.raises(ValidationError):
        PlanSchema.model_validate_json(json.dumps(payload))


def test_plan_schema_accepts_exact_k0_plan_and_rejects_weakened_guards() -> None:
    payload = _expected().model_dump(mode="json")
    assert PlanSchema.model_validate_json(json.dumps(payload)).model_dump(mode="json") == payload
    payload["steps"][0]["preconditions"] = []
    with pytest.raises(ValidationError):
        PlanSchema.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("error", [AuthError("denied"), TimeoutError("unknown remote outcome")])
def test_provider_errors_propagate_without_blind_retry(error: Exception) -> None:
    provider = MockProvider([_response()], errors={1: error})
    with pytest.raises(type(error)) as caught:
        StructuredPlanner(provider, SYSTEM_PROMPT).plan(INTENT, request_key=REQUEST_KEY)
    assert caught.value is error
    assert len(provider.calls) == 1


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "tool_calls", None])
def test_incomplete_or_filtered_response_is_not_retried(finish_reason: str | None) -> None:
    response = ModelResponse(_response(), ModelUsage(None, None), 0, "mock", finish_reason)
    provider = MockProvider([response, _response()])
    with pytest.raises(PlanError):
        StructuredPlanner(provider, SYSTEM_PROMPT).plan(INTENT, request_key=REQUEST_KEY)
    assert len(provider.calls) == 1


@pytest.mark.parametrize("attempts", [0, 4, True, 1.5])
def test_retry_budget_must_be_exact_integer_between_one_and_three(attempts: Any) -> None:
    with pytest.raises(ValueError):
        StructuredPlanner(MockProvider([]), SYSTEM_PROMPT, max_attempts=attempts)


def test_configured_smaller_budget_is_respected() -> None:
    provider = MockProvider(["invalid", _response()])
    with pytest.raises(PlanError) as caught:
        StructuredPlanner(provider, SYSTEM_PROMPT, max_attempts=1).plan(
            INTENT, request_key=REQUEST_KEY
        )
    assert caught.value.attempts == 1
    assert len(provider.calls) == 1


@pytest.mark.parametrize("scope", ["plan", "step"])
@pytest.mark.parametrize("value", [True, False, 1.0, 0.0])
def test_schema_literal_integer_fields_are_not_coerced(scope: str, value: Any) -> None:
    payload = _expected().model_dump(mode="json")
    target = payload if scope == "plan" else payload["steps"][0]
    target["schema_version"] = value
    with pytest.raises(ValidationError):
        PlanSchema.model_validate_json(json.dumps(payload))


def test_retry_history_does_not_enter_plan_identity() -> None:
    immediate = StructuredPlanner(MockProvider([_response()]), SYSTEM_PROMPT)
    retried = StructuredPlanner(MockProvider(["invalid", _response()]), SYSTEM_PROMPT)
    assert (
        immediate.plan(INTENT, request_key=REQUEST_KEY).model_dump_json()
        == retried.plan(INTENT, request_key=REQUEST_KEY).model_dump_json()
    )


def test_caller_not_model_owns_request_identity() -> None:
    planner = StructuredPlanner(MockProvider([_response()] * 2), SYSTEM_PROMPT)
    first = planner.plan(INTENT, request_key=REQUEST_KEY)
    second = planner.plan(INTENT, request_key="another-request")
    assert first.task_id != second.task_id
    assert first.id != second.id
    assert first.steps[0].operation_key != second.steps[0].operation_key


@pytest.mark.parametrize("intent", ["", " \n", "bad\x00intent", "bad\ud800"])
def test_invalid_intent_fails_before_provider_call(intent: str) -> None:
    provider = MockProvider([])
    with pytest.raises(ValueError):
        StructuredPlanner(provider, SYSTEM_PROMPT).plan(intent, request_key=REQUEST_KEY)
    assert not provider.calls


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[TaskRepository]:
    spec = importlib.util.spec_from_file_location(
        "k1005_schema_fixture", ROOT / "tests/contract/test_sqlite_schema_draft.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection: sqlite3.Connection = module._apply_candidate(module._load())
    connection.execute("INSERT INTO system_control VALUES (1, 0, 0, 7, 0)")
    connection.commit()
    try:
        with WorkerLock(tmp_path / "worker.lock") as lock:
            yield TaskRepository(connection, worker_lock=lock, worker_epoch=7, draft_fixture=True)
    finally:
        connection.close()


def _create_task(repository: TaskRepository) -> None:
    plan = _expected()
    repository.create_task(
        task_id=plan.task_id,
        request_key=REQUEST_KEY,
        request_digest="a" * 64,
        task_kind="inventory.generate",
        now_ms=0,
    )


def _stored_candidates(repository: TaskRepository) -> list[dict[str, Any]]:
    return [
        json.loads(row[0])
        for row in repository.connection.execute(
            "SELECT payload_json FROM events WHERE event_type = 'draft.structured_plan.proposed' "
            "ORDER BY event_sequence"
        )
    ]


def test_candidate_is_read_back_from_repository_without_activating_a_graph(
    repository: TaskRepository,
) -> None:
    _create_task(repository)
    before = deepcopy(repository.get_task(_expected().task_id))
    provider = MockProvider(["invalid", _response()])
    planner = StructuredPlanner(provider, SYSTEM_PROMPT, repository=repository)
    plan = planner.plan(INTENT, request_key=REQUEST_KEY, now_ms=19)
    candidates = _stored_candidates(repository)
    assert len(candidates) == 1
    assert candidates[0]["plan"] == plan.model_dump(mode="json")
    assert candidates[0]["trust"] == "UNTRUSTED_PROPOSAL"
    assert repository.get_task(plan.task_id) == before
    assert repository.connection.execute("SELECT COUNT(*) FROM task_plans").fetchone() == (0,)
    assert repository.connection.execute("SELECT COUNT(*) FROM task_steps").fetchone() == (0,)
    assert repository.connection.in_transaction is False


def test_invalid_response_never_reaches_repository(repository: TaskRepository) -> None:
    _create_task(repository)
    with pytest.raises(PlanError):
        StructuredPlanner(MockProvider(["invalid"] * 3), SYSTEM_PROMPT, repository=repository).plan(
            INTENT, request_key=REQUEST_KEY
        )
    assert _stored_candidates(repository) == []


def test_identical_replay_does_not_append_another_candidate(repository: TaskRepository) -> None:
    _create_task(repository)
    planner = StructuredPlanner(
        MockProvider([_response()] * 2), SYSTEM_PROMPT, repository=repository
    )
    first = planner.plan(INTENT, request_key=REQUEST_KEY)
    second = planner.plan(INTENT, request_key=REQUEST_KEY)
    assert first == second
    assert len(_stored_candidates(repository)) == 1


def test_same_request_key_different_content_is_rejected_not_overwritten(
    repository: TaskRepository,
) -> None:
    _create_task(repository)
    provider = MockProvider([_response(), _response().replace("示例", "another")])
    planner = StructuredPlanner(provider, SYSTEM_PROMPT, repository=repository)
    first = planner.plan(INTENT, request_key=REQUEST_KEY)
    with pytest.raises(GuardRejectedError):
        planner.plan(INTENT, request_key=REQUEST_KEY)
    assert len(provider.calls) == 2
    assert [candidate["plan"] for candidate in _stored_candidates(repository)] == [
        first.model_dump(mode="json")
    ]


def test_repository_failure_is_not_a_model_retry(repository: TaskRepository) -> None:
    _create_task(repository)
    repository.connection.execute("UPDATE system_control SET worker_epoch = 8")
    repository.connection.commit()
    provider = MockProvider([_response()] * 3)
    with pytest.raises(StaleEpochError):
        StructuredPlanner(provider, SYSTEM_PROMPT, repository=repository).plan(
            INTENT, request_key=REQUEST_KEY
        )
    assert len(provider.calls) == 1
    assert _stored_candidates(repository) == []


def test_candidate_event_failure_rolls_back_without_model_retry(repository: TaskRepository) -> None:
    _create_task(repository)
    repository.connection.execute(
        "CREATE TRIGGER reject_candidate BEFORE INSERT ON events "
        "WHEN NEW.event_type = 'draft.structured_plan.proposed' "
        "BEGIN SELECT RAISE(ABORT, 'injected candidate failure'); END"
    )
    repository.connection.commit()
    provider = MockProvider([_response()] * 3)
    with pytest.raises(sqlite3.IntegrityError, match="injected candidate failure"):
        StructuredPlanner(provider, SYSTEM_PROMPT, repository=repository).plan(
            INTENT, request_key=REQUEST_KEY
        )
    assert len(provider.calls) == 1
    assert _stored_candidates(repository) == []
    assert repository.connection.in_transaction is False
    assert repository.connection.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)


def test_audit_timestamp_does_not_change_plan_or_append_on_replay(
    repository: TaskRepository,
) -> None:
    _create_task(repository)
    planner = StructuredPlanner(
        MockProvider([_response()] * 2), SYSTEM_PROMPT, repository=repository
    )
    first = planner.plan(INTENT, request_key=REQUEST_KEY, now_ms=11)
    second = planner.plan(INTENT, request_key=REQUEST_KEY, now_ms=99)
    assert first.model_dump_json() == second.model_dump_json()
    rows = repository.connection.execute(
        "SELECT created_at_ms FROM events WHERE event_type = 'draft.structured_plan.proposed'"
    ).fetchall()
    assert rows == [(11,)]


def test_repository_requires_an_existing_matching_task(repository: TaskRepository) -> None:
    provider = MockProvider([_response()])
    with pytest.raises(GuardRejectedError):
        StructuredPlanner(provider, SYSTEM_PROMPT, repository=repository).plan(
            INTENT, request_key=REQUEST_KEY
        )
    assert _stored_candidates(repository) == []
