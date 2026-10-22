"""K1-001 DRAFT: provider-independent model protocol contract tests."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import asdict, is_dataclass

import pytest

import jarvis.model_provider as protocol


def test_model_response_fields_and_nested_usage() -> None:
    assert importlib.util.find_spec("jarvis.model_provider") is not None, "provider draft missing"
    from jarvis.model_provider import ModelResponse, ModelUsage

    response = ModelResponse(
        content="fixture output",
        usage=ModelUsage(tokens_in=12, tokens_out=7),
        latency_ms=18.5,
        model_id="fixture-model",
        finish_reason="stop",
    )

    assert is_dataclass(response)
    assert asdict(response) == {
        "content": "fixture output",
        "usage": {"tokens_in": 12, "tokens_out": 7},
        "latency_ms": 18.5,
        "model_id": "fixture-model",
        "finish_reason": "stop",
    }


def test_provider_config_json_round_trip_keeps_only_env_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert hasattr(protocol, "ProviderConfig"), "provider configuration contract missing"
    config = protocol.ProviderConfig(
        base_url="https://fixture.invalid/v1",
        api_key_env="JARVIS_FIXTURE_MODEL_KEY",
        model_id="fixture-model",
        max_tokens=512,
        temperature=0.25,
    )
    monkeypatch.setenv("JARVIS_FIXTURE_MODEL_KEY", "fixture-secret-not-for-serialization")

    encoded = json.dumps(asdict(config), allow_nan=False)

    assert json.loads(encoded) == {
        "base_url": "https://fixture.invalid/v1",
        "api_key_env": "JARVIS_FIXTURE_MODEL_KEY",
        "model_id": "fixture-model",
        "max_tokens": 512,
        "temperature": 0.25,
    }
    assert protocol.ProviderConfig(**json.loads(encoded)) == config
    assert "fixture-secret-not-for-serialization" not in encoded
    assert "fixture-secret-not-for-serialization" not in repr(config)


def test_mock_provider_chat_and_async_stream_share_typed_contract() -> None:
    assert hasattr(protocol.ModelProvider, "chat"), "non-streaming contract missing"
    assert hasattr(protocol.ModelProvider, "stream_chat"), "streaming contract missing"
    assert hasattr(protocol, "ModelChunk"), "typed stream chunk missing"
    assert inspect.isabstract(protocol.ModelProvider)
    with pytest.raises(TypeError, match="abstract"):
        protocol.ModelProvider()  # pyright: ignore[reportAbstractUsage]

    expected = protocol.ModelResponse(
        content="fixture output",
        usage=protocol.ModelUsage(tokens_in=12, tokens_out=7),
        latency_ms=18.5,
        model_id="fixture-model",
        finish_reason="stop",
    )
    received: list[tuple[Sequence[Mapping[str, object]], dict[str, object]]] = []

    class FixtureProvider(protocol.ModelProvider):
        def chat(
            self, messages: Sequence[Mapping[str, object]], **kwargs: object
        ) -> protocol.ModelResponse:
            received.append((messages, kwargs))
            return expected

        async def stream_chat(
            self, messages: Sequence[Mapping[str, object]], **kwargs: object
        ) -> AsyncGenerator[protocol.ModelChunk, None]:
            received.append((messages, kwargs))
            yield protocol.ModelChunk(content="fixture ", model_id="fixture-model")
            yield protocol.ModelChunk(content="output", model_id="fixture-model")
            yield protocol.ModelChunk(
                content="",
                model_id="fixture-model",
                usage=expected.usage,
                latency_ms=expected.latency_ms,
                finish_reason="stop",
            )

    provider = FixtureProvider()
    messages: list[Mapping[str, object]] = [{"role": "user", "content": "fixture input"}]
    assert provider.chat(messages, temperature=0.25) is expected
    assert asyncio.run(provider.generate(messages, temperature=0.25)) is expected

    async def collect() -> list[protocol.ModelChunk]:
        stream = provider.stream_chat(messages, temperature=0.25)
        assert inspect.isasyncgenfunction(provider.stream_chat)
        chunks = [chunk async for chunk in stream]
        assert all(isinstance(chunk, protocol.ModelChunk) for chunk in chunks)
        return chunks

    chunks = asyncio.run(collect())
    assert "".join(chunk.content for chunk in chunks) == expected.content
    assert [chunk.finish_reason for chunk in chunks] == [None, None, "stop"]
    assert chunks[0].usage is None
    assert chunks[-1].usage == expected.usage
    assert chunks[-1].latency_ms == expected.latency_ms
    assert all(chunk.model_id == expected.model_id for chunk in chunks)
    assert received == [(messages, {"temperature": 0.25})] * 3


@pytest.mark.parametrize(
    "error_name", ["RateLimitError", "AuthError", "TimeoutError", "ContentFilterError"]
)
def test_normalized_error_hierarchy(error_name: str) -> None:
    assert hasattr(protocol, "ModelError"), "normalized model error missing"
    assert hasattr(protocol, error_name), f"{error_name} contract missing"
    error_type = getattr(protocol, error_name)

    assert issubclass(error_type, protocol.ModelError)
    assert issubclass(protocol.ModelError, Exception)
    error = error_type("sanitized fixture failure")
    assert str(error) == "sanitized fixture failure"
    assert error.args == ("sanitized fixture failure",)
    assert not hasattr(error, "retry_allowed")
    with pytest.raises(protocol.ModelError) as captured:
        raise error
    assert type(captured.value) is error_type


@pytest.mark.parametrize("close_early", [False, True])
def test_stream_errors_propagate_and_early_close_runs_cleanup(close_early: bool) -> None:
    error = protocol.TimeoutError("sanitized timeout")
    cleaned_up: list[bool] = []
    calls: list[str] = []

    class FailingProvider(protocol.ModelProvider):
        def chat(
            self, messages: Sequence[Mapping[str, object]], **kwargs: object
        ) -> protocol.ModelResponse:
            calls.append("chat")
            raise error

        async def stream_chat(
            self, messages: Sequence[Mapping[str, object]], **kwargs: object
        ) -> AsyncGenerator[protocol.ModelChunk, None]:
            calls.append("stream_chat")
            try:
                yield protocol.ModelChunk(content="partial", model_id="fixture-model")
                raise error
            finally:
                cleaned_up.append(True)

    provider = FailingProvider()
    messages: list[Mapping[str, object]] = [{"role": "user", "content": "fixture"}]
    with pytest.raises(protocol.ModelError) as captured:
        provider.chat(messages)
    assert captured.value is error
    with pytest.raises(protocol.ModelError) as captured:
        asyncio.run(provider.generate(messages))
    assert captured.value is error

    async def consume() -> None:
        stream = provider.stream_chat(messages)
        first = await anext(stream)
        assert first.content == "partial"
        assert first.finish_reason is None
        if close_early:
            await stream.aclose()
        else:
            with pytest.raises(protocol.ModelError) as stream_error:
                await anext(stream)
            assert stream_error.value is error
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(consume())
    assert cleaned_up == [True]
    assert calls == ["chat", "chat", "stream_chat"]


def test_usage_distinguishes_missing_from_reported_zero() -> None:
    unknown = protocol.ModelUsage(tokens_in=None, tokens_out=None)
    zero = protocol.ModelUsage(tokens_in=0, tokens_out=0)
    response = protocol.ModelResponse(
        content="", usage=unknown, latency_ms=0.0, model_id="fixture", finish_reason="length"
    )
    assert asdict(response)["usage"] == {"tokens_in": None, "tokens_out": None}
    assert unknown != zero
    assert response.content == ""
    assert response.finish_reason == "length"
