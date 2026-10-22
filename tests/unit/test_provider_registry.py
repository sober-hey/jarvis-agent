"""K1-004 DRAFT: offline registry tests, not product/runtime acceptance."""

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import TypedDict, cast

import pytest

from jarvis.mock_provider import MockProvider
from jarvis.model_provider import ModelProvider, TimeoutError
from jarvis.provider_registry import ProviderCapability, ProviderRegistry


class CapabilityFilters(TypedDict, total=False):
    context_length: int
    supports_streaming: bool
    supports_tools: bool
    model_id: str


def test_register_get_and_query_capabilities_without_invoking_provider() -> None:
    registry = ProviderRegistry()
    provider = MockProvider(["unused"])
    capability = ProviderCapability(8192, True, False, 0.01, supported_models=("fixture",))
    registry.register("fixture-route", provider, capability)

    assert registry.get("fixture-route") is provider
    assert registry.get_capabilities("fixture-route") is capability
    assert capability.supported_models == ("fixture",)
    assert provider.calls == []
    assert registry.runtime_enabled is False


@pytest.fixture
def registry() -> ProviderRegistry:
    result = ProviderRegistry()
    result.register("basic", MockProvider([]), ProviderCapability(4096, False, False, 0.001))
    result.register(
        "stream",
        MockProvider([]),
        ProviderCapability(8192, True, False, 0.002, supported_models=("small", "shared")),
    )
    result.register(
        "tools",
        MockProvider([]),
        ProviderCapability(32768, True, True, 0.02, supported_models=("large", "shared")),
    )
    return result


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({}, ["basic", "stream", "tools"]),
        ({"supports_streaming": True}, ["stream", "tools"]),
        ({"supports_streaming": False}, ["basic"]),
        ({"supports_tools": True}, ["tools"]),
        ({"supports_tools": False}, ["basic", "stream"]),
        ({"context_length": 8192}, ["stream", "tools"]),
        ({"context_length": 8193}, ["tools"]),
        ({"context_length": 32768}, ["tools"]),
        ({"context_length": 32769}, []),
        ({"model_id": "shared"}, ["stream", "tools"]),
        ({"model_id": "missing"}, []),
        ({"supports_streaming": True, "supports_tools": True, "context_length": 8192}, ["tools"]),
        ({"supports_streaming": False, "supports_tools": True}, []),
    ],
)
def test_filters(
    registry: ProviderRegistry, filters: CapabilityFilters, expected: list[str]
) -> None:
    assert registry.list_by_capability(**filters) == expected


def test_selects_lowest_cost_after_all_requirements(registry: ProviderRegistry) -> None:
    assert registry.select() is registry.get("basic")
    assert registry.select(requires_streaming=True) is registry.get("stream")
    assert registry.select(context_length=8193) is registry.get("tools")
    assert registry.select(requires_tools=True) is registry.get("tools")
    assert registry.select(model_id="large", requires_streaming=True) is registry.get("tools")
    assert registry.select(context_length=32769) is None
    assert registry.select(model_id="missing") is None


def test_cost_order_not_registration_order_and_ties_are_stable() -> None:
    registry = ProviderRegistry()
    registry.register("expensive", MockProvider([]), ProviderCapability(8192, True, True, 1))
    registry.register("cheap-first", MockProvider([]), ProviderCapability(8192, True, True, 0))
    registry.register("cheap-second", MockProvider([]), ProviderCapability(8192, True, True, 0))

    assert registry.select() is registry.get("cheap-first")
    registry.set_health("cheap-first", available=False)
    assert registry.select() is registry.get("cheap-second")


def test_unavailable_routes_skipped_but_remain_inspectable(registry: ProviderRegistry) -> None:
    registry.set_health("basic", available=False)
    assert registry.is_available("basic") is False
    assert registry.get("basic") is not None
    assert registry.list_by_capability() == ["stream", "tools"]
    assert registry.list_by_capability(available_only=False) == ["basic", "stream", "tools"]
    assert registry.select() is registry.get("stream")
    registry.set_health("stream", available=False)
    registry.set_health("tools", available=False)
    assert registry.list_by_capability() == []
    assert registry.select() is None
    registry.set_health("basic", available=True)
    assert registry.select() is registry.get("basic")


def test_health_check_updates_availability_and_recovers_without_model_call(
    registry: ProviderRegistry,
) -> None:
    seen: list[ModelProvider] = []

    def unhealthy(provider: ModelProvider) -> bool:
        seen.append(provider)
        return False

    assert registry.health_check("basic", unhealthy) is False
    assert seen == [registry.get("basic")]
    assert registry.select() is registry.get("stream")
    assert registry.health_check("basic", lambda _: True) is True
    assert registry.select() is registry.get("basic")
    assert cast(MockProvider, registry.get("basic")).calls == []


def test_probe_failure_marks_unavailable_without_exposing_exception(
    registry: ProviderRegistry,
) -> None:
    def broken(_: ModelProvider) -> bool:
        raise RuntimeError("untrusted probe detail")

    assert registry.health_check("basic", broken) is False
    assert registry.is_available("basic") is False
    assert registry.select() is registry.get("stream")


@pytest.mark.parametrize("result", [None, 1, "healthy", [], {}])
def test_non_boolean_probe_result_fails_closed(registry: ProviderRegistry, result: object) -> None:
    def malformed(_: ModelProvider) -> object:
        return result

    probe = cast(Callable[[ModelProvider], bool], malformed)
    assert registry.health_check("basic", probe) is False
    assert registry.is_available("basic") is False


def test_probe_cancellation_is_not_swallowed(registry: ProviderRegistry) -> None:
    def interrupted(_: ModelProvider) -> bool:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        registry.health_check("basic", interrupted)
    assert registry.is_available("basic") is True


def test_registry_does_not_retry_or_execute_selected_provider() -> None:
    registry = ProviderRegistry()
    broken = MockProvider([], errors={1: TimeoutError("unknown outcome")})
    other = MockProvider(["must not be called"])
    registry.register("broken", broken, ProviderCapability(8192, True, False, 0))
    registry.register("other", other, ProviderCapability(8192, True, False, 1))

    selected = registry.select()
    assert selected is broken
    assert selected is not None
    assert broken.calls == other.calls == []
    with pytest.raises(TimeoutError):
        selected.chat([])
    assert len(broken.calls) == 1
    assert other.calls == []
    assert registry.is_available("broken") is True


@pytest.mark.parametrize(
    "operation", ["get", "get_capabilities", "is_available", "set_health", "health_check"]
)
def test_unknown_names_raise_key_error(registry: ProviderRegistry, operation: str) -> None:
    with pytest.raises(KeyError):
        if operation == "set_health":
            registry.set_health("unknown", available=False)
        elif operation == "health_check":
            registry.health_check("unknown", lambda _: True)
        else:
            getattr(registry, operation)("unknown")


def test_duplicate_registration_rejected_and_keeps_health_and_original(
    registry: ProviderRegistry,
) -> None:
    original = registry.get("basic")
    registry.set_health("basic", available=False)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("basic", MockProvider([]), ProviderCapability(1024, True, True, 0))
    assert registry.get("basic") is original
    assert registry.is_available("basic") is False


@pytest.mark.parametrize("name", ["", " ", " leading", "trailing "])
def test_invalid_names_rejected(registry: ProviderRegistry, name: str) -> None:
    with pytest.raises(ValueError):
        registry.register(name, MockProvider([]), ProviderCapability(1024, False, False, 0))


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_context_capacity_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        ProviderCapability(cast(int, value), True, False, 0)


@pytest.mark.parametrize("cost", [-1, float("nan"), float("inf"), float("-inf"), True])
def test_invalid_cost_rejected(cost: object) -> None:
    with pytest.raises(ValueError):
        ProviderCapability(8192, True, False, cast(float, cost))


@pytest.mark.parametrize("models", ["fixture", ["fixture"], ("",), (" same",), ("x", "x")])
def test_invalid_model_metadata_rejected(models: object) -> None:
    with pytest.raises(ValueError):
        ProviderCapability(8192, True, False, 0, supported_models=cast(tuple[str, ...], models))


def test_capabilities_are_immutable(registry: ProviderRegistry) -> None:
    attribute = "supports_streaming"
    with pytest.raises(FrozenInstanceError):
        setattr(registry.get_capabilities("basic"), attribute, True)


@pytest.mark.parametrize("context_length", [-1, True, 1.5])
def test_invalid_context_requirements_rejected(
    registry: ProviderRegistry, context_length: object
) -> None:
    with pytest.raises(ValueError):
        registry.list_by_capability(context_length=cast(int, context_length))
    with pytest.raises(ValueError):
        registry.select(context_length=cast(int, context_length))


def test_invalid_boolean_metadata_and_filters_rejected(registry: ProviderRegistry) -> None:
    with pytest.raises(ValueError):
        ProviderCapability(8192, cast(bool, 1), False, 0)
    with pytest.raises(ValueError):
        ProviderCapability(8192, True, cast(bool, "yes"), 0)
    with pytest.raises(ValueError):
        registry.list_by_capability(supports_streaming=cast(bool, 1))
    with pytest.raises(ValueError):
        registry.list_by_capability(supports_tools=cast(bool, "yes"))
    with pytest.raises(ValueError):
        registry.list_by_capability(available_only=cast(bool, None))
    with pytest.raises(ValueError):
        registry.select(requires_streaming=cast(bool, "yes"))
    with pytest.raises(ValueError):
        registry.select(requires_tools=cast(bool, None))
    with pytest.raises(ValueError):
        registry.set_health("basic", available=cast(bool, "yes"))


def test_unknown_model_metadata_is_not_a_wildcard(registry: ProviderRegistry) -> None:
    assert registry.get_capabilities("basic").supported_models == ()
    assert "basic" not in registry.list_by_capability(model_id="small")


def test_empty_registry_has_no_match() -> None:
    registry = ProviderRegistry()
    assert registry.list_by_capability() == []
    assert registry.select() is None
