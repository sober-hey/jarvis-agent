"""K1-001 DRAFT: provider-independent call, response and error contracts.

NOT_APPROVED / NOT_FROZEN / runtime_enabled=False.
No concrete SDK, network transport, credential lookup, tools or runtime wiring.
This narrow chat draft does not implement the full master-plan ModelRequest /
structured-output / request-identity gateway, and does not approve K1 execution.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelUsage:
    """Provider-reported tokens; None means unreported, not zero."""

    tokens_in: int | None
    tokens_out: int | None


@dataclass(frozen=True)
class ModelResponse:
    """One non-streaming result; content is an untrusted proposal."""

    content: str
    usage: ModelUsage
    latency_ms: float
    model_id: str
    finish_reason: str | None


@dataclass(frozen=True)
class ProviderConfig:
    """Serializable route/options, containing an environment name, never a key.

    No environment resolution, SDK initialization or network access occurs here.
    Use dataclasses.asdict + JSON, then ProviderConfig(**decoded) to round-trip.
    """

    base_url: str
    api_key_env: str
    model_id: str
    max_tokens: int
    temperature: float


@dataclass(frozen=True)
class ModelChunk:
    """Ordered text delta, not cumulative text.

    A successful stream ends with exactly one chunk whose finish_reason is set.
    That terminal chunk may have empty content and carries final usage/latency
    when reported. None means unknown; callers must not invent token counts.
    An iterator ending without a terminal chunk is incomplete, not success.
    Errors propagate during iteration; partial output is never a retry permit.
    """

    content: str
    model_id: str
    usage: ModelUsage | None = None
    latency_ms: float | None = None
    finish_reason: str | None = None


class ModelError(Exception):
    """Normalized failure; adapters must supply sanitized, credential-free text.

    A failure class does not prove no remote work occurred or authorize retry.
    Unknown provider failures use this base class rather than a false subtype.
    """


class RateLimitError(ModelError):
    """The provider rejected the request for a rate or quota limit."""


class AuthError(ModelError):
    """The provider rejected authentication or access credentials."""


class TimeoutError(ModelError):
    """The call timed out; its remote outcome may still be unknown."""


class ContentFilterError(ModelError):
    """The provider refused or filtered the requested content."""


class ModelProvider(ABC):
    """Draft text-model boundary; responses/chunks are untrusted proposals.

    chat is synchronous and always returns ModelResponse. stream_chat is a
    separate async-generator API, never a union return controlled by stream=.
    Adapters must normalize provider errors, close transports on aclose/cancel,
    propagate cancellation and never retry or switch providers implicitly.
    Extra kwargs are adapter options; implementations must reject unsupported
    ones rather than silently ignoring them. This ABC performs no I/O itself.
    """

    @abstractmethod
    def chat(self, messages: Sequence[Mapping[str, object]], **kwargs: object) -> ModelResponse:
        """Make one non-streaming call; latency is elapsed milliseconds."""
        raise NotImplementedError

    @abstractmethod
    async def stream_chat(
        self, messages: Sequence[Mapping[str, object]], **kwargs: object
    ) -> AsyncGenerator[ModelChunk, None]:
        """Yield ordered chunks; no await is needed before async iteration."""
        raise NotImplementedError
        yield  # Makes this abstract method an async generator, not a coroutine.

    async def generate(
        self, messages: Sequence[Mapping[str, object]], **kwargs: object
    ) -> ModelResponse:
        """Provisional async compatibility entry; calls chat once off-loop.

        Coroutine cancellation does not stop its worker thread or prove that a
        remote request did not happen. Cancellation never authorizes retry.
        Concrete async adapters may override this method for native transport.
        """
        return await asyncio.to_thread(self.chat, messages, **kwargs)
