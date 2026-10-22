"""K1-002 DRAFT mock provider; NOT_FROZEN; runtime_enabled=False.

Offline test/development fixture only. No API, tools or runtime wiring.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from typing import ClassVar, cast

from jarvis.model_provider import ModelChunk, ModelProvider, ModelResponse, ModelUsage


@dataclass(frozen=True)
class MockCall:
    """Snapshot at invocation start; timestamp is Unix epoch seconds."""

    messages: Sequence[Mapping[str, object]]
    kwargs: dict[str, object]
    timestamp: float


class MockProvider(ModelProvider):
    """Scripted asynchronous provider for offline fixtures.

    ``errors`` uses 1-based invocation numbers shared by generate and stream.
    Injected errors do not consume a preset response. Streaming is lazy: a call
    starts on first iteration. Each successful call consumes one response even
    if subsequently cancelled or its stream is only partially consumed.
    """

    runtime_enabled: ClassVar[bool] = False

    def __init__(
        self,
        responses: Sequence[object],
        *,
        delay: float = 0,
        errors: Mapping[int, Exception] | None = None,
        stream_chunks: Sequence[Sequence[str]] | None = None,
    ) -> None:
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("delay must be finite and non-negative")
        self.delay = delay
        self._responses = tuple(responses)
        self._errors = dict(errors or {})
        for number, error in self._errors.items():
            if type(number) is not int or number < 1:
                raise ValueError("error call numbers must be a positive integer")
            if not isinstance(cast(object, error), Exception):
                raise TypeError("error values must be exception instances")
        self._chunks: tuple[tuple[str, ...], ...] | None = None
        if stream_chunks is not None:
            if len(stream_chunks) != len(self._responses):
                raise ValueError("provide one chunk sequence per response")
            if any(
                isinstance(chunks, str)
                or any(not isinstance(cast(object, chunk), str) for chunk in chunks)
                for chunks in stream_chunks
            ):
                raise TypeError("stream chunks must be sequences of strings")
            self._chunks = tuple(tuple(chunks) for chunks in stream_chunks)
        self.calls: list[MockCall] = []
        self._response_index = 0
        self._lock = Lock()

    def _reserve(
        self, messages: Sequence[Mapping[str, object]], kwargs: dict[str, object]
    ) -> tuple[int, Exception | None]:
        call = MockCall(deepcopy(messages), deepcopy(kwargs), time.time())
        with self._lock:
            self.calls.append(call)
            error = self._errors.get(len(self.calls))
            if error is not None:
                return -1, error
            index = self._response_index
            if index >= len(self._responses):
                return -1, RuntimeError("MockProvider responses exhausted")
            self._response_index += 1
            return index, None

    async def _wait(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)

    def _materialize(self, index: int, started: float) -> ModelResponse:
        response = self._responses[index]
        if isinstance(response, ModelResponse):
            return response
        if not isinstance(response, str):
            raise TypeError("preset responses must be str or ModelResponse")
        return ModelResponse(
            content=response,
            usage=ModelUsage(tokens_in=None, tokens_out=None),
            latency_ms=(time.monotonic() - started) * 1000,
            model_id="mock",
            finish_reason="stop",
        )

    def chat(self, messages: Sequence[Mapping[str, object]], **kwargs: object) -> ModelResponse:
        """Synchronous K1-001 entry; kwargs are recorded fixture metadata."""
        started = time.monotonic()
        index, error = self._reserve(messages, kwargs)
        if self.delay:
            time.sleep(self.delay)
        if error is not None:
            raise error
        return self._materialize(index, started)

    async def generate(
        self, messages: Sequence[Mapping[str, object]], **kwargs: object
    ) -> ModelResponse:
        """Native async entry without the base class's worker thread."""
        started = time.monotonic()
        index, error = self._reserve(messages, kwargs)
        await self._wait()
        if error is not None:
            raise error
        return self._materialize(index, started)

    async def stream_chat(
        self, messages: Sequence[Mapping[str, object]], **kwargs: object
    ) -> AsyncGenerator[ModelChunk, None]:
        """K1-001 chunks, including an explicit terminal metadata chunk."""
        started = time.monotonic()
        index, error = self._reserve(messages, kwargs)
        if error is not None:
            await self._wait()
            raise error
        response = self._materialize(index, started)
        chunks = (response.content,) if self._chunks is None else self._chunks[index]
        for chunk in chunks:
            await self._wait()
            yield ModelChunk(content=chunk, model_id=response.model_id)
        yield ModelChunk(
            content="",
            model_id=response.model_id,
            usage=response.usage,
            latency_ms=(time.monotonic() - started) * 1000,
            finish_reason=response.finish_reason or "stop",
        )

    async def stream(
        self, messages: Sequence[Mapping[str, object]], **kwargs: object
    ) -> AsyncGenerator[str, None]:
        """Text-only convenience API; terminal metadata is not a text chunk."""
        chunks = self.stream_chat(messages, **kwargs)
        try:
            async for chunk in chunks:
                if chunk.finish_reason is None:
                    yield chunk.content
        finally:
            await chunks.aclose()
