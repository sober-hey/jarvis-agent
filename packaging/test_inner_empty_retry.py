"""Request-layer retry is inner-only, once, byte-identical, and observable."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock

import pytest

import chat_entry as chat
from inner_runtime import validate


def completion(text: object = "", finish: str = "stop", **message: object) -> Mock:
    response = Mock()
    response.model_dump.return_value = {
        "id": "response-id",
        "choices": [
            {"finish_reason": finish, "message": {"content": text, **message}},
        ],
        "usage": {"completion_tokens": 800},
    }
    response._request_id = "request-id"
    return response


def invoke(responses: list[object], mode: str = "inner") -> tuple[Any, Mock, dict[str, Any]]:
    api = Mock()
    api.create.side_effect = responses
    state: dict[str, Any] = {"model_request_state": "not_started", "model_attempts": []}
    params = {"model": "test", "messages": [{"role": "user", "content": "frozen"}]}
    result = chat.request_text(api, params, mode, "chat_completions", state)
    return result, api, state


@pytest.mark.parametrize("empty", ["", "  \n", None])
def test_inner_stop_empty_reasks_same_input_once(empty: object) -> None:
    (_, _, text), api, state = invoke([completion(empty), completion('{"thoughts":[]}')])
    assert text == '{"thoughts":[]}'
    assert api.create.call_count == 2
    assert api.create.call_args_list[0] == api.create.call_args_list[1]
    assert [a["attempt"] for a in state["model_attempts"]] == [1, 2]
    assert "retry_reason" not in state["model_attempts"][0]
    assert state["model_attempts"][1]["retry_reason"] == "empty_body"
    assert state["model_attempts"][0]["outcome"] == "empty_body"
    assert state["model_request_state"] == "response_received"


def test_parser_empty_error_reasks_even_without_stop() -> None:
    # BOM is not whitespace to str.strip(), but the unchanged parser rejects it.
    (_, _, text), api, state = invoke([completion("\ufeff", "length"), completion("{}")])
    assert text == "{}"
    assert api.create.call_count == 2
    assert state["model_attempts"][1]["retry_reason"] == "empty_body"


def test_second_empty_still_rejected_without_third_call() -> None:
    (_, _, text), api, state = invoke([completion(""), completion("")])
    assert api.create.call_count == 2
    assert len(state["model_attempts"]) == 2
    with pytest.raises(ValueError, match="Model output empty or exceeds bounded parse size"):
        validate(text, set(), [], [])


def test_retry_response_still_gets_strict_duplicate_key_validation() -> None:
    (_, _, text), api, _ = invoke([completion(""), completion('{"question":null,"question":{}}')])
    assert api.create.call_count == 2
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        validate(text, set(), [], [])


@pytest.mark.parametrize("mode", ["chat", "inner"])
def test_duplicate_and_invalid_provenance_are_not_retried(mode: str) -> None:
    for bad in ('{"question":null,"question":{}}', '{"thoughts":[{"refs":["invented"]}]}'):
        (_, _, text), api, _ = invoke([completion(bad)], mode)
        assert text == bad
        assert api.create.call_count == 1


def test_chat_empty_is_not_retried() -> None:
    api = Mock()
    api.create.return_value = completion("")
    state: dict[str, Any] = {"model_attempts": []}
    with pytest.raises(RuntimeError, match="no assistant text"):
        chat.request_text(api, {}, "chat", "chat_completions", state)
    assert api.create.call_count == 1


def test_transport_error_is_never_retried() -> None:
    api = Mock()
    api.create.side_effect = TimeoutError("private error")
    state: dict[str, Any] = {"model_attempts": []}
    with pytest.raises(TimeoutError):
        chat.request_text(api, {}, "inner", "chat_completions", state)
    assert api.create.call_count == 1
    assert state["model_request_state"] == "dispatched_unknown"
    assert state["model_attempts"][0]["model_request_state"] == "dispatched_unknown"
    assert "private error" not in json.dumps(state)


def test_second_transport_error_preserves_first_response_and_unknown_second_attempt() -> None:
    api = Mock()
    api.create.side_effect = [completion(""), TimeoutError("private error")]
    state: dict[str, Any] = {"model_attempts": []}
    with pytest.raises(TimeoutError):
        chat.request_text(api, {}, "inner", "chat_completions", state)
    assert api.create.call_count == 2
    assert state["model_request_state"] == "dispatched_unknown"
    assert state["model_attempts"][0]["outcome"] == "empty_body"
    assert state["model_attempts"][1]["retry_reason"] == "empty_body"


def test_tool_call_is_not_retried() -> None:
    api = Mock()
    api.create.return_value = completion("", tool_calls=[{"id": "tool"}])
    state: dict[str, Any] = {"model_attempts": []}
    with pytest.raises(RuntimeError, match="Tool response rejected"):
        chat.request_text(api, {}, "inner", "chat_completions", state)
    assert api.create.call_count == 1


def test_responses_api_parser_empty_gets_same_single_retry() -> None:
    api = Mock()
    a, b = Mock(), Mock()
    a.model_dump.return_value = {"output": [], "status": "completed"}
    b.model_dump.return_value = {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}],
    }
    api.create.side_effect = [a, b]
    state: dict[str, Any] = {"model_attempts": []}
    (_, _, text) = chat.request_text(api, {"input": []}, "inner", "responses", state)
    assert text == "{}"
    assert api.create.call_count == 2
    assert state["model_attempts"][1]["retry_reason"] == "empty_body"
