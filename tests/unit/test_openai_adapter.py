"""K1-003 DRAFT: offline Chat Completions adapter contract tests."""

from __future__ import annotations

import asyncio
import builtins
import importlib.util
import io
import json
import os
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from email.message import Message
from typing import Any
from unittest.mock import Mock

import pytest

from jarvis.model_provider import (
    AuthError,
    ModelChunk,
    ModelError,
    ProviderConfig,
    RateLimitError,
    TimeoutError,
)
from jarvis.openai_adapter import OpenAIAdapter


@pytest.fixture(autouse=True)
def deny_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Real network is forbidden in adapter unit tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


class HTTPResponse(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.headers = {"x-request-id": "provider-request-fixture"}


def test_generate_parses_response_and_uses_configured_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """All HTTP is mocked; credentials and route come only from ProviderConfig."""
    assert importlib.util.find_spec("jarvis.openai_adapter") is not None
    from jarvis.model_provider import ProviderConfig
    from jarvis.openai_adapter import OpenAIAdapter

    monkeypatch.setenv("ADAPTER_TEST_API_KEY", "fixture-key-not-a-real-secret")
    monkeypatch.setenv("ADAPTER_TEST_BASE_URL", "https://model.invalid/custom/v1/")
    config = ProviderConfig(
        base_url=os.environ["ADAPTER_TEST_BASE_URL"],
        api_key_env="ADAPTER_TEST_API_KEY",
        model_id="fixture-model",
        max_tokens=50,
        temperature=0.25,
    )
    payload = {
        "id": "completion-fixture",
        "model": "fixture-model",
        "choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
    }
    response = HTTPResponse(json.dumps(payload).encode())
    opener = Mock(return_value=response)
    monkeypatch.setattr("jarvis.openai_adapter._urlopen", opener)
    adapter = OpenAIAdapter(config)

    result = asyncio.run(
        adapter.generate(
            [{"role": "user", "content": "问候"}],
            model="fixture-model",
            timeout_seconds=12,
            max_output_tokens=50,
        )
    )

    assert result.content == "你好"
    assert result.finish_reason == "stop"
    assert result.usage.tokens_in == 7
    assert result.usage.tokens_out == 2
    assert result.model_id == "fixture-model"
    assert result.latency_ms >= 0
    sent = opener.call_args.args[0]
    assert sent.full_url == "https://model.invalid/custom/v1/chat/completions"
    assert sent.get_header("Authorization") == "Bearer fixture-key-not-a-real-secret"
    assert opener.call_args.kwargs["timeout"] == 12
    body = json.loads(sent.data)
    assert body["model"] == "fixture-model"
    assert body["messages"] == [{"role": "user", "content": "问候"}]
    assert body["max_tokens"] == 50
    assert body["stream"] is False
    opener.assert_called_once()
    assert response.closed


def test_rate_limit_is_normalized_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis import model_provider as protocol
    from jarvis.openai_adapter import OpenAIAdapter

    assert hasattr(protocol, "RateLimitError"), "normalized rate-limit error missing"
    monkeypatch.setenv("ADAPTER_TEST_API_KEY", "fixture-key")
    config = protocol.ProviderConfig("https://model.invalid/v1", "ADAPTER_TEST_API_KEY", "m", 20, 0)
    error = urllib.error.HTTPError(
        "https://model.invalid", 429, "private-body", Message(), io.BytesIO()
    )
    opener = Mock(side_effect=error)
    monkeypatch.setattr("jarvis.openai_adapter._urlopen", opener)
    with pytest.raises(protocol.RateLimitError):
        asyncio.run(OpenAIAdapter(config).generate([{"role": "user", "content": "test"}]))
    opener.assert_called_once()


def test_stream_chat_parses_sse_text_finish_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.model_provider import ProviderConfig
    from jarvis.openai_adapter import OpenAIAdapter

    monkeypatch.setenv("ADAPTER_TEST_API_KEY", "fixture-key")
    config = ProviderConfig("https://model.invalid/v1", "ADAPTER_TEST_API_KEY", "m", 20, 0)
    events: list[dict[str, Any]] = [
        {"model": "m", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"model": "m", "choices": [{"index": 0, "delta": {"content": "你"}}]},
        {"model": "m", "choices": [{"index": 0, "delta": {"content": "好"}}]},
        {"model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"model": "m", "choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 2}},
    ]
    wire = (
        b": heartbeat\r\n\r\n"
        + b"".join(
            ("data: " + json.dumps(e, ensure_ascii=False) + "\r\n\r\n").encode() for e in events
        )
        + b"data: [DONE]\r\n\r\n"
    )
    response = HTTPResponse(wire)
    opener = Mock(return_value=response)
    monkeypatch.setattr("jarvis.openai_adapter._urlopen", opener)

    async def collect() -> list[ModelChunk]:
        return [
            chunk
            async for chunk in OpenAIAdapter(config).stream_chat(
                [{"role": "user", "content": "test"}]
            )
        ]

    chunks = asyncio.run(collect())
    assert "".join(chunk.content for chunk in chunks) == "你好"
    assert all(chunk.model_id == "m" for chunk in chunks)
    assert [chunk.finish_reason for chunk in chunks if chunk.finish_reason is not None] == ["stop"]
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.tokens_in == 8
    assert chunks[-1].usage.tokens_out == 2
    assert chunks[-1].latency_ms is not None
    assert chunks[-1].latency_ms >= 0
    body = json.loads(opener.call_args.args[0].data)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    opener.assert_called_once()
    assert response.closed


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    ("error_factory", "expected"),
    [
        (
            lambda: urllib.error.HTTPError(
                "https://model.invalid", 401, "private-provider-detail", Message(), io.BytesIO()
            ),
            AuthError,
        ),
        (
            lambda: urllib.error.HTTPError(
                "https://model.invalid", 429, "private-provider-detail", Message(), io.BytesIO()
            ),
            RateLimitError,
        ),
        (lambda: builtins.TimeoutError("private-timeout-detail"), TimeoutError),
        (lambda: urllib.error.URLError(builtins.TimeoutError("private-reason")), TimeoutError),
        (
            lambda: urllib.error.HTTPError(
                "https://model.invalid", 500, "private-provider-detail", Message(), io.BytesIO()
            ),
            ModelError,
        ),
    ],
)
def test_http_and_timeout_errors_are_normalized(
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
    error_factory: Callable[[], Exception],
    expected: type[ModelError],
) -> None:
    monkeypatch.setenv("ADAPTER_TEST_API_KEY", "fixture-key")
    config = ProviderConfig("https://model.invalid/v1", "ADAPTER_TEST_API_KEY", "m", 20, 0)
    opener = Mock(side_effect=error_factory())
    monkeypatch.setattr("jarvis.openai_adapter._urlopen", opener)

    async def call() -> None:
        adapter = OpenAIAdapter(config)
        if streaming:
            async for _ in adapter.stream_chat([{"role": "user", "content": "test"}]):
                pass
        else:
            await adapter.generate([{"role": "user", "content": "test"}])

    with pytest.raises(expected) as caught:
        asyncio.run(call())
    assert "private-" not in str(caught.value)
    assert "fixture-key" not in str(caught.value)
    opener.assert_called_once()


def test_from_env_resolves_endpoint_and_missing_key_blocks_http(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADAPTER_TEST_BASE_URL", "https://model.invalid/tenant/v1")
    monkeypatch.delenv("ADAPTER_TEST_KEY_MISSING", raising=False)
    adapter = OpenAIAdapter.from_env(
        base_url_env="ADAPTER_TEST_BASE_URL",
        api_key_env="ADAPTER_TEST_KEY_MISSING",
        model_id="m",
        max_tokens=20,
        temperature=0,
    )
    opener = Mock()
    monkeypatch.setattr("jarvis.openai_adapter._urlopen", opener)
    assert adapter.config.base_url == "https://model.invalid/tenant/v1"
    with pytest.raises(AuthError):
        adapter.chat([{"role": "user", "content": "test"}])
    opener.assert_not_called()


def test_stream_closes_response_on_early_aclose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAPTER_TEST_API_KEY", "fixture-key")
    config = ProviderConfig("https://model.invalid/v1", "ADAPTER_TEST_API_KEY", "m", 20, 0)
    response = HTTPResponse(b'data: {"model":"m","choices":[{"delta":{"content":"a"}}]}\n\n')
    monkeypatch.setattr("jarvis.openai_adapter._urlopen", Mock(return_value=response))

    async def close() -> None:
        stream = OpenAIAdapter(config).stream_chat([{"role": "user", "content": "test"}])
        assert (await anext(stream)).content == "a"
        await stream.aclose()

    asyncio.run(close())
    assert response.closed


@pytest.mark.parametrize(
    "wire",
    [
        b'data: {"model":"m","choices":[{"delta":{"content":"a"}}]}\n\n',
        b"data: [DONE]\n\n",
        b"data: not-json\n\n",
    ],
)
def test_incomplete_or_malformed_stream_never_reports_success(
    monkeypatch: pytest.MonkeyPatch, wire: bytes
) -> None:
    monkeypatch.setenv("ADAPTER_TEST_API_KEY", "fixture-key")
    config = ProviderConfig("https://model.invalid/v1", "ADAPTER_TEST_API_KEY", "m", 20, 0)
    response = HTTPResponse(wire)
    monkeypatch.setattr("jarvis.openai_adapter._urlopen", Mock(return_value=response))
    received: list[ModelChunk] = []

    async def collect() -> None:
        async for chunk in OpenAIAdapter(config).stream_chat([{"role": "user", "content": "test"}]):
            received.append(chunk)

    with pytest.raises(ModelError):
        asyncio.run(collect())
    assert all(chunk.finish_reason is None for chunk in received)
    assert response.closed


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}], "usage": {}},
        {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}], "usage": None},
    ],
)
def test_missing_usage_stays_unknown(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]):
    monkeypatch.setenv("ADAPTER_TEST_API_KEY", "fixture-key")
    config = ProviderConfig("https://model.invalid/v1", "ADAPTER_TEST_API_KEY", "m", 20, 0)
    response = HTTPResponse(json.dumps(payload).encode())
    monkeypatch.setattr("jarvis.openai_adapter._urlopen", Mock(return_value=response))
    result = OpenAIAdapter(config).chat([{"role": "user", "content": "test"}])
    assert result.usage.tokens_in is None
    assert result.usage.tokens_out is None


def test_unsupported_options_are_rejected_without_http(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADAPTER_TEST_API_KEY", "fixture-key")
    config = ProviderConfig("https://model.invalid/v1", "ADAPTER_TEST_API_KEY", "m", 20, 0)
    opener = Mock()
    monkeypatch.setattr("jarvis.openai_adapter._urlopen", opener)
    with pytest.raises(ValueError, match="Unsupported"):
        OpenAIAdapter(config).chat([], tools=[{"name": "not-allowed"}])
    opener.assert_not_called()


def test_invalid_generation_options_block_http(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADAPTER_TEST_API_KEY", "fixture-key")
    config = ProviderConfig("https://model.invalid/v1", "ADAPTER_TEST_API_KEY", "m", -1, 0)
    opener = Mock()
    monkeypatch.setattr("jarvis.openai_adapter._urlopen", opener)
    with pytest.raises(ValueError, match="max_output_tokens"):
        OpenAIAdapter(config).chat([{"role": "user", "content": "test"}])
    opener.assert_not_called()


def test_invalid_finish_reason_is_not_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADAPTER_TEST_API_KEY", "fixture-key")
    config = ProviderConfig("https://model.invalid/v1", "ADAPTER_TEST_API_KEY", "m", 20, 0)
    response = HTTPResponse(b'{"choices":[{"message":{"content":"x"}}]}')
    monkeypatch.setattr("jarvis.openai_adapter._urlopen", Mock(return_value=response))
    with pytest.raises(ModelError, match="finish reason"):
        OpenAIAdapter(config).chat([{"role": "user", "content": "test"}])
    assert response.closed


@pytest.mark.parametrize(
    "base_url",
    [
        "http://model.invalid/v1",
        "https://user:private@model.invalid/v1",
        "https://model.invalid/v1?private=value",
        "https://model.invalid/v1#private",
    ],
)
def test_unsafe_endpoints_block_http(monkeypatch: pytest.MonkeyPatch, base_url: str):
    monkeypatch.setenv("ADAPTER_TEST_API_KEY", "fixture-key")
    config = ProviderConfig(base_url, "ADAPTER_TEST_API_KEY", "m", 20, 0)
    opener = Mock()
    monkeypatch.setattr("jarvis.openai_adapter._urlopen", opener)
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAIAdapter(config).chat([])
    opener.assert_not_called()


def test_transport_disables_proxy_environment_and_redirects(monkeypatch: pytest.MonkeyPatch):
    import jarvis.openai_adapter as adapter_module

    opener = Mock()
    builder = Mock(return_value=opener)
    monkeypatch.setattr("jarvis.openai_adapter.urllib.request.build_opener", builder)
    request = urllib.request.Request("https://model.invalid/v1/chat/completions")
    adapter_module._urlopen(request, timeout=12)  # pyright: ignore[reportPrivateUsage]
    handlers = builder.call_args.args
    assert isinstance(handlers[0], urllib.request.ProxyHandler)
    assert handlers[0].proxies == {}
    assert isinstance(handlers[1], adapter_module._NoRedirect)  # pyright: ignore[reportPrivateUsage]
    assert (
        handlers[1].redirect_request(request, None, 302, "found", None, "https://other.invalid")
        is None
    )
    opener.open.assert_called_once_with(request, timeout=12)


def test_multiline_sse_and_usage_absence(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADAPTER_TEST_API_KEY", "fixture-key")
    config = ProviderConfig("https://model.invalid/v1", "ADAPTER_TEST_API_KEY", "m", 20, 0)
    wire = (
        b'event: message\ndata: {"model":"m",\ndata: "choices":[{"delta":{"content":"a"},'
        b'"finish_reason":"length"}]}\n\ndata: [DONE]\n\n'
    )
    response = HTTPResponse(wire)
    monkeypatch.setattr("jarvis.openai_adapter._urlopen", Mock(return_value=response))

    async def collect() -> list[ModelChunk]:
        return [c async for c in OpenAIAdapter(config).stream_chat([])]

    chunks = asyncio.run(collect())
    assert "".join(c.content for c in chunks) == "a"
    assert chunks[-1].finish_reason == "length"
    assert chunks[-1].usage is None
    assert response.closed


def test_stream_closes_eventual_open_when_cancelled(monkeypatch: pytest.MonkeyPatch):
    import threading

    monkeypatch.setenv("ADAPTER_TEST_API_KEY", "fixture-key")
    config = ProviderConfig("https://model.invalid/v1", "ADAPTER_TEST_API_KEY", "m", 20, 0)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    response = HTTPResponse(b"")
    original_close = response.close

    def close() -> None:
        original_close()
        closed.set()

    response.close = close

    def delayed_open(*args: object, **kwargs: object) -> HTTPResponse:
        entered.set()
        assert release.wait(3)
        return response

    monkeypatch.setattr("jarvis.openai_adapter._urlopen", delayed_open)

    async def cancel() -> None:
        stream = OpenAIAdapter(config).stream_chat([])
        pending = asyncio.create_task(anext(stream))
        assert await asyncio.to_thread(entered.wait, 3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        release.set()
        assert await asyncio.to_thread(closed.wait, 3)

    asyncio.run(cancel())
    assert response.closed


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"choices":[],"error":{"message":"private-provider-detail"}}',
        b'{"choices":[{"message":{"content":null},"finish_reason":"stop"}]}',
        b'{"choices":[{"message":{"tool_calls":[{}]},"finish_reason":"tool_calls"}]}',
        b'{"choices":[{"message":{"content":"x"},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":-1}}',
    ],
)
def test_bad_response_is_normalized_and_closed(monkeypatch: pytest.MonkeyPatch, payload: bytes):
    monkeypatch.setenv("ADAPTER_TEST_API_KEY", "fixture-key")
    config = ProviderConfig("https://model.invalid/v1", "ADAPTER_TEST_API_KEY", "m", 20, 0)
    response = HTTPResponse(payload)
    monkeypatch.setattr("jarvis.openai_adapter._urlopen", Mock(return_value=response))
    with pytest.raises(ModelError) as error:
        OpenAIAdapter(config).chat([])
    assert "private-provider-detail" not in str(error.value)
    assert response.closed
