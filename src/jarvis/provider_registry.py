"""K1-004 DRAFT provider capability registry.

NOT_APPROVED / NOT_FROZEN / runtime_enabled=False.
In-memory metadata and explicit health observations only; no SDK/config/key
lookup, background probes, model calls, retry, failover or runtime wiring.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, cast

from jarvis.model_provider import ModelProvider


def _identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be a non-empty string without surrounding whitespace")


def _boolean(value: object, label: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{label} must be bool")


def _context_length(value: object, *, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"context length must be an integer >= {minimum}")


@dataclass(frozen=True)
class ProviderCapability:
    """Declared route capabilities, not discovered or verified provider facts.

    All supported models share these limits. Register separate route/provider
    instances when model limits or prices differ. Empty supported_models means
    unknown, never a wildcard. Cost is an estimate in a caller-agreed currency
    per 1,000 aggregate tokens, not split input/output billing or a budget.
    """

    max_context_tokens: int
    supports_streaming: bool
    supports_tools: bool
    cost_per_1k_tokens: float
    supported_models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _context_length(self.max_context_tokens, minimum=1)
        _boolean(self.supports_streaming, "supports_streaming")
        _boolean(self.supports_tools, "supports_tools")
        if (
            type(self.cost_per_1k_tokens) not in (int, float)
            or not math.isfinite(self.cost_per_1k_tokens)
            or self.cost_per_1k_tokens < 0
        ):
            raise ValueError("cost_per_1k_tokens must be finite and non-negative")
        if not isinstance(cast(object, self.supported_models), tuple):
            raise ValueError("supported_models must be a tuple of unique model identifiers")
        for model_id in self.supported_models:
            _identifier(model_id, "model_id")
        if len(set(self.supported_models)) != len(self.supported_models):
            raise ValueError("supported_models must contain unique identifiers")


@dataclass
class _Registration:
    provider: ModelProvider
    capabilities: ProviderCapability
    available: bool = True


class ProviderRegistry:
    """Single-threaded, offline candidate registry; explicit calls only.

    Registration initially enables selection without claiming a successful live
    health probe. get is administrative lookup (including unhealthy routes).
    Selection never invokes providers and never retries a previous request.
    """

    runtime_enabled: ClassVar[bool] = False

    def __init__(self) -> None:
        self._entries: dict[str, _Registration] = {}

    def register(
        self, name: str, provider: ModelProvider, capabilities: ProviderCapability
    ) -> None:
        """Register a unique exact route name; never silently replace an entry."""
        _identifier(name, "name")
        if name in self._entries:
            raise ValueError("provider name is already registered")
        if not isinstance(cast(object, provider), ModelProvider):
            raise TypeError("provider must implement ModelProvider")
        if not isinstance(cast(object, capabilities), ProviderCapability):
            raise TypeError("capabilities must be ProviderCapability")
        self._entries[name] = _Registration(provider, capabilities)

    def get(self, name: str) -> ModelProvider:
        """Return the registered instance; unknown names raise KeyError."""
        return self._entries[name].provider

    def get_capabilities(self, name: str) -> ProviderCapability:
        """Return immutable metadata, including declared supported models."""
        return self._entries[name].capabilities

    def is_available(self, name: str) -> bool:
        """Return the current in-memory selection eligibility flag."""
        return self._entries[name].available

    def set_health(self, name: str, *, available: bool) -> None:
        """Record an explicit health observation; no implicit probing occurs."""
        entry = self._entries[name]
        _boolean(available, "available")
        entry.available = available

    def health_check(self, name: str, probe: Callable[[ModelProvider], bool]) -> bool:
        """Run one caller-supplied synchronous, bounded, side-effect-free probe.

        Only literal True marks healthy. Ordinary exceptions or malformed probe
        results mark unavailable; exception details are not stored or exposed.
        BaseException cancellation propagates. No built-in model/network probe
        exists; caller owns probe deadlines, cost and safety. No automatic retry.
        """
        entry = self._entries[name]
        try:
            available = probe(entry.provider) is True
        except Exception:
            available = False
        entry.available = available
        return available

    def list_by_capability(
        self,
        *,
        context_length: int = 0,
        supports_streaming: bool | None = None,
        supports_tools: bool | None = None,
        model_id: str | None = None,
        available_only: bool = True,
    ) -> list[str]:
        """Return matching names in registration order; combine all predicates.

        Boolean None is unconstrained; False explicitly requests no support.
        context_length is the caller's total token requirement, inclusive of any
        reserved output. This registry neither tokenizes nor truncates content.
        """
        _context_length(context_length, minimum=0)
        _boolean(available_only, "available_only")
        if supports_streaming is not None:
            _boolean(supports_streaming, "supports_streaming")
        if supports_tools is not None:
            _boolean(supports_tools, "supports_tools")
        if model_id is not None:
            _identifier(model_id, "model_id")
        matches: list[str] = []
        for name, entry in self._entries.items():
            capability = entry.capabilities
            if available_only and not entry.available:
                continue
            if capability.max_context_tokens < context_length:
                continue
            if (
                supports_streaming is not None
                and capability.supports_streaming != supports_streaming
            ):
                continue
            if supports_tools is not None and capability.supports_tools != supports_tools:
                continue
            if model_id is not None and model_id not in capability.supported_models:
                continue
            matches.append(name)
        return matches

    def select(
        self,
        *,
        context_length: int = 0,
        requires_streaming: bool = False,
        requires_tools: bool = False,
        model_id: str | None = None,
    ) -> ModelProvider | None:
        """Return cheapest healthy match, or None; ties use registration order.

        A False requirement imposes no restriction (support is not forbidden).
        This selects an instance, not a model override or permission to dispatch.
        Caller remains responsible for using an advertised model and for request
        identity, health freshness, budgets and unknown-outcome reconciliation.
        """
        _boolean(requires_streaming, "requires_streaming")
        _boolean(requires_tools, "requires_tools")
        names = self.list_by_capability(
            context_length=context_length,
            supports_streaming=True if requires_streaming else None,
            supports_tools=True if requires_tools else None,
            model_id=model_id,
        )
        ranked = sorted(names, key=lambda name: self._entries[name].capabilities.cost_per_1k_tokens)
        return self.get(ranked[0]) if ranked else None
