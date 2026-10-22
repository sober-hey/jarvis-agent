"""K1-002 DRAFT: local mock behavior, not real model or product acceptance."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from jarvis.mock_provider import MockProvider
from jarvis.model_provider import ModelProvider

MESSAGES = [{"role": "user", "content": "hello"}]


def test_returns_preset_responses_in_order() -> None:
    provider = MockProvider(["first", "second"])

    assert isinstance(provider, ModelProvider)
    assert asyncio.run(provider.generate(MESSAGES)).content == "first"
    assert asyncio.run(provider.generate(MESSAGES)).content == "second"


def test_records_calls_with_messages_kwargs_and_timestamp() -> None:
    provider = MockProvider(["ok"])
    before = time.time()

    asyncio.run(provider.generate(MESSAGES, model="fixture", temperature=0.2))
    after = time.time()

    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call.messages == MESSAGES
    assert call.kwargs == {"model": "fixture", "temperature": 0.2}
    assert before <= call.timestamp <= after


def test_call_records_snapshot_mutable_inputs() -> None:
    provider = MockProvider(["ok"])
    messages = [{"role": "user", "content": "original"}]
    options = {"nested": [1]}
    asyncio.run(provider.generate(messages, options=options))

    messages[0]["content"] = "changed"
    options["nested"].append(2)

    assert provider.calls[0].messages == [{"role": "user", "content": "original"}]
    assert provider.calls[0].kwargs == {"options": {"nested": [1]}}


def test_delay_is_observable_and_does_not_block_event_loop() -> None:
    async def scenario() -> None:
        provider = MockProvider(["ok"], delay=0.03)
        task = asyncio.create_task(provider.generate(MESSAGES))
        await asyncio.sleep(0)
        assert not task.done()
        assert len(provider.calls) == 1
        assert (await task).content == "ok"

    start = time.monotonic()
    asyncio.run(scenario())
    assert time.monotonic() - start >= 0.03


def test_error_injection_fires_on_nth_call_and_preserves_response_queue() -> None:
    error = TimeoutError("injected")
    provider = MockProvider(["first", "second"], errors={2: error})

    assert asyncio.run(provider.generate(MESSAGES)).content == "first"
    with pytest.raises(TimeoutError, match="injected") as caught:
        asyncio.run(provider.generate(MESSAGES))
    assert caught.value is error
    assert asyncio.run(provider.generate(MESSAGES)).content == "second"
    assert len(provider.calls) == 3


async def _collect(chunks: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in chunks]


def test_stream_yields_exact_explicit_chunks_and_records_one_call() -> None:
    provider = MockProvider(["hello world"], stream_chunks=[["hello", " ", "world"]])

    assert asyncio.run(_collect(provider.stream(MESSAGES, temperature=0))) == [
        "hello",
        " ",
        "world",
    ]
    assert len(provider.calls) == 1
    assert provider.calls[0].messages == MESSAGES
    assert provider.calls[0].kwargs == {"temperature": 0}


def test_stream_defaults_to_one_chunk_per_response() -> None:
    provider = MockProvider(["hello", "next"])

    assert asyncio.run(_collect(provider.stream(MESSAGES))) == ["hello"]
    assert asyncio.run(provider.generate(MESSAGES)).content == "next"


def test_stream_error_uses_global_call_number() -> None:
    provider = MockProvider(["first", "second"], errors={2: ValueError("stream error")})

    assert asyncio.run(provider.generate(MESSAGES)).content == "first"
    with pytest.raises(ValueError, match="stream error"):
        asyncio.run(_collect(provider.stream(MESSAGES)))
    assert asyncio.run(_collect(provider.stream(MESSAGES))) == ["second"]
    assert len(provider.calls) == 3


def test_exhaustion_is_explicit_and_recorded() -> None:
    provider = MockProvider([])

    with pytest.raises(RuntimeError, match="exhausted"):
        asyncio.run(provider.generate(MESSAGES))
    with pytest.raises(RuntimeError, match="exhausted"):
        asyncio.run(_collect(provider.stream(MESSAGES)))
    assert len(provider.calls) == 2


@pytest.mark.parametrize("delay", [-1, float("inf"), float("nan")])
def test_invalid_delay_is_rejected(delay: float) -> None:
    with pytest.raises(ValueError, match="delay"):
        MockProvider([], delay=delay)


@pytest.mark.parametrize("call_number", [0, -1, True, 1.5])
def test_invalid_error_call_number_is_rejected(call_number: int | float) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        MockProvider([], errors={call_number: RuntimeError("bad")})  # type: ignore[arg-type]


def test_invalid_error_value_is_rejected() -> None:
    with pytest.raises(TypeError, match="exception"):
        MockProvider([], errors={1: "not an exception"})  # type: ignore[dict-item]


def test_stream_chunk_sets_match_responses() -> None:
    with pytest.raises(ValueError, match="one chunk sequence per response"):
        MockProvider(["one", "two"], stream_chunks=[["one"]])


def test_stream_chunks_must_be_strings() -> None:
    with pytest.raises(TypeError, match="strings"):
        MockProvider(["one"], stream_chunks=[[1]])  # type: ignore[list-item]


def test_draft_runtime_marker_is_disabled() -> None:
    provider = MockProvider([])
    assert provider.runtime_enabled is False


def test_stream_delay_applies_to_each_chunk() -> None:
    async def scenario() -> None:
        provider = MockProvider(["ab"], delay=0.02, stream_chunks=[["a", "b"]])
        start = time.monotonic()
        received = [chunk async for chunk in provider.stream(MESSAGES)]
        assert received == ["a", "b"]
        assert time.monotonic() - start >= 0.04

    asyncio.run(scenario())


def test_empty_stream_is_recorded_and_consumes_one_response() -> None:
    provider = MockProvider(["", "next"], stream_chunks=[[], ["next"]])

    assert asyncio.run(_collect(provider.stream(MESSAGES))) == []
    assert asyncio.run(provider.generate(MESSAGES)).content == "next"
    assert len(provider.calls) == 2


def test_stream_is_lazy_and_partial_close_does_not_reuse_response() -> None:
    async def scenario() -> None:
        provider = MockProvider(["ab", "next"], stream_chunks=[["a", "b"], ["next"]])
        stream = provider.stream(MESSAGES)
        assert provider.calls == []
        assert await anext(stream) == "a"
        await stream.aclose()
        assert (await provider.generate(MESSAGES)).content == "next"
        assert len(provider.calls) == 2

    asyncio.run(scenario())


def test_simultaneous_calls_reserve_distinct_responses_in_start_order() -> None:
    async def scenario() -> None:
        provider = MockProvider(["a", "b", "c"], delay=0.01)
        responses = await asyncio.gather(*(provider.generate(MESSAGES) for _ in range(3)))
        assert [response.content for response in responses] == ["a", "b", "c"]
        assert len(provider.calls) == 3

    asyncio.run(scenario())


def test_cancelled_call_is_recorded_and_not_reused() -> None:
    async def scenario() -> None:
        provider = MockProvider(["cancelled", "next"], delay=0.01)
        task = asyncio.create_task(provider.generate(MESSAGES))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await provider.generate(MESSAGES)).content == "next"
        assert len(provider.calls) == 2

    asyncio.run(scenario())


def test_structured_preset_response_is_returned_unchanged() -> None:
    from jarvis.model_provider import ModelResponse, ModelUsage

    response = ModelResponse(
        content="fixture",
        usage=ModelUsage(tokens_in=1, tokens_out=2),
        latency_ms=0.1,
        model_id="fixture",
        finish_reason="stop",
    )
    provider = MockProvider([response], stream_chunks=[["fix", "ture"]])
    assert asyncio.run(provider.generate(MESSAGES)) is response


def test_unsupported_response_fails_explicitly() -> None:
    provider = MockProvider([{"content": "fixture"}])
    with pytest.raises(TypeError, match="str or ModelResponse"):
        asyncio.run(_collect(provider.stream(MESSAGES)))
    assert len(provider.calls) == 1


def test_error_injection_works_without_any_response() -> None:
    provider = MockProvider([], errors={1: TimeoutError("planned")})
    with pytest.raises(TimeoutError, match="planned"):
        asyncio.run(provider.generate(MESSAGES))
    with pytest.raises(RuntimeError, match="exhausted"):
        asyncio.run(provider.generate(MESSAGES))


def test_chat_satisfies_k1_001_response_contract() -> None:
    provider = MockProvider(["first", "second"])
    response = provider.chat(MESSAGES, model="fixture")
    assert response.content == "first"
    assert response.model_id == "mock"
    assert response.finish_reason == "stop"
    assert response.usage.tokens_in is None
    assert response.usage.tokens_out is None
    assert response.latency_ms >= 0
    assert asyncio.run(provider.generate(MESSAGES)).content == "second"
    assert len(provider.calls) == 2


def test_stream_chat_satisfies_k1_001_chunk_contract() -> None:
    async def scenario() -> None:
        provider = MockProvider(["ab"], stream_chunks=[["a", "b"]])
        chunks = [chunk async for chunk in provider.stream_chat(MESSAGES)]
        assert [chunk.content for chunk in chunks] == ["a", "b", ""]
        assert [chunk.finish_reason for chunk in chunks] == [None, None, "stop"]
        assert chunks[-1].usage is not None
        assert chunks[-1].usage.tokens_in is None
        assert chunks[-1].latency_ms is not None
        assert len(provider.calls) == 1

    asyncio.run(scenario())


def test_sync_chat_delay_and_error_injection() -> None:
    provider = MockProvider(["unused"], delay=0.01, errors={1: ValueError("planned")})
    start = time.monotonic()
    with pytest.raises(ValueError, match="planned"):
        provider.chat(MESSAGES)
    assert time.monotonic() - start >= 0.01
    assert len(provider.calls) == 1
