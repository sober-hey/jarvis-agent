"""K1-008 DRAFT: offline in-memory accounting, not runtime authorization."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext
from threading import Barrier
from typing import cast

import pytest

from jarvis.budget import BudgetConfig, BudgetExceededError, BudgetManager
from jarvis.model_provider import ModelUsage


def test_reservation_reduces_both_balances() -> None:
    manager = BudgetManager(BudgetConfig(1000, 10, 1, 2))
    before = manager.get_balance()
    record = manager.reserve("task-1", 400)

    assert manager.runtime_enabled is False
    assert before.tokens_remaining == 1000
    assert before.cost_remaining == 10
    assert manager.get_balance().tokens_remaining == 600
    assert manager.get_balance().cost_remaining == Decimal("9.2")
    assert record.estimated_tokens == 400
    assert record.reserved_cost == Decimal("0.8")
    assert record.actual_usage is None
    assert record.settled is False
    assert manager.get_usage("task-1") == record


def test_settlement_replaces_reservation_with_split_actual_cost() -> None:
    manager = BudgetManager(BudgetConfig(1000, 10, 1, 2))
    manager.reserve("task-1", 400)
    record = manager.settle("task-1", ModelUsage(100, 50))

    assert record.settled is True
    assert record.actual_usage == ModelUsage(100, 50)
    assert record.actual_cost == Decimal("0.2")
    assert record.overspent is False
    assert manager.get_balance().tokens_remaining == 850
    assert manager.get_balance().cost_remaining == Decimal("9.8")
    assert manager.get_usage("task-1") == record
    manager.reserve("task-2", 850)
    assert manager.get_balance().tokens_remaining == 0


@pytest.mark.parametrize("config", [BudgetConfig(99, 10, 1, 2), BudgetConfig(1000, 0.19, 1, 2)])
def test_insufficient_token_or_cost_balance_rejects_without_mutation(config: BudgetConfig) -> None:
    manager = BudgetManager(config)
    before = manager.get_balance()
    with pytest.raises(BudgetExceededError):
        manager.reserve("rejected", 100)
    assert manager.get_balance() == before
    with pytest.raises(KeyError):
        manager.get_usage("rejected")


def test_exact_limits_are_accepted_and_zero_does_not_bypass_negative_balance() -> None:
    manager = BudgetManager(BudgetConfig(100, 0.2, 1, 2))
    manager.reserve("full", 100)
    assert manager.get_balance().tokens_remaining == 0
    assert manager.get_balance().cost_remaining == 0
    manager.reserve("zero", 0)
    manager.settle("full", ModelUsage(50, 100))
    before = manager.get_balance()
    with pytest.raises(BudgetExceededError):
        manager.reserve("after-overspend", 0)
    assert manager.get_balance() == before


def test_overspend_is_recorded_warned_and_charged_not_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = BudgetManager(BudgetConfig(100, 0.2, 1, 2))
    manager.reserve("over", 100)
    record = manager.settle("over", ModelUsage(50, 100))

    assert record.settled is True
    assert record.overspent is True
    assert record.overspent_tokens == 50
    assert record.overspent_cost == Decimal("0.05")
    assert record.actual_cost == Decimal("0.25")
    assert manager.get_balance().tokens_remaining == -50
    assert manager.get_balance().cost_remaining == Decimal("-0.05")
    warnings = [item for item in caplog.records if item.name == "jarvis.budget"]
    assert len(warnings) == 1
    assert warnings[0].levelname == "WARNING"
    assert manager.settle("over", ModelUsage(50, 100)) == record
    assert len([item for item in caplog.records if item.name == "jarvis.budget"]) == 1


def test_tokens_can_exceed_estimate_without_exceeding_reserved_cost() -> None:
    manager = BudgetManager(BudgetConfig(1000, 10, 1, 10))
    manager.reserve("mostly-input", 100)
    record = manager.settle("mostly-input", ModelUsage(200, 0))
    assert record.overspent_tokens == 100
    assert record.overspent_cost == 0
    assert record.overspent is True
    assert manager.get_balance().cost_remaining == Decimal("9.8")


@pytest.mark.parametrize("config", [BudgetConfig(100, 100, 1, 2), BudgetConfig(1000, 0.2, 1, 2)])
def test_concurrent_reservations_cannot_overcommit_either_limit(config: BudgetConfig) -> None:
    manager = BudgetManager(config)
    barrier = Barrier(20)

    def reserve(index: int) -> bool:
        barrier.wait(timeout=5)
        try:
            manager.reserve(f"task-{index}", 10)
            return True
        except BudgetExceededError:
            return False

    with ThreadPoolExecutor(max_workers=20) as pool:
        accepted = list(pool.map(reserve, range(20)))
    assert sum(accepted) == 10
    assert manager.get_balance().tokens_remaining == config.max_total_tokens - 100
    assert manager.get_balance().cost_remaining == Decimal(str(config.max_total_cost)) - Decimal(
        "0.2"
    )


def test_concurrent_settlement_and_reservation_keep_complete_accounting() -> None:
    manager = BudgetManager(BudgetConfig(10000, 100, 1, 2))

    def run(index: int) -> None:
        manager.reserve(f"task-{index}", 100)
        manager.settle(f"task-{index}", ModelUsage(20, 30))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run, range(50)))
    assert manager.get_balance().tokens_remaining == 7500
    assert manager.get_balance().cost_remaining == 96
    assert all(manager.get_usage(f"task-{i}").settled for i in range(50))


def test_duplicate_task_ids_and_changed_settlements_cannot_refund_usage() -> None:
    manager = BudgetManager(BudgetConfig(1000, 10, 1, 2))
    manager.reserve("same", 100)
    reserved = manager.get_balance()
    with pytest.raises(ValueError, match="already reserved"):
        manager.reserve("same", 0)
    assert manager.get_balance() == reserved
    first = manager.settle("same", ModelUsage(50, 20))
    settled = manager.get_balance()
    assert manager.settle("same", ModelUsage(50, 20)) == first
    with pytest.raises(ValueError, match="already settled"):
        manager.settle("same", ModelUsage(0, 0))
    with pytest.raises(ValueError, match="already reserved"):
        manager.reserve("same", 1)
    assert manager.get_balance() == settled


def test_unknown_task_lookup_and_settlement_raise_without_charging() -> None:
    manager = BudgetManager(BudgetConfig(1000, 10, 1, 2))
    with pytest.raises(KeyError):
        manager.get_usage("missing")
    with pytest.raises(KeyError):
        manager.settle("missing", ModelUsage(1, 1))
    assert manager.get_balance().tokens_remaining == 1000


@pytest.mark.parametrize(
    "usage", [ModelUsage(None, None), ModelUsage(None, 10), ModelUsage(10, None)]
)
def test_unreported_usage_keeps_entire_reservation_pending(usage: ModelUsage) -> None:
    manager = BudgetManager(BudgetConfig(1000, 10, 1, 2))
    manager.reserve("pending", 100)
    before = manager.get_balance()
    with pytest.raises(ValueError, match="unreported"):
        manager.settle("pending", usage)
    assert manager.get_balance() == before
    assert manager.get_usage("pending").settled is False
    manager.settle("pending", ModelUsage(10, 10))
    assert manager.get_usage("pending").settled is True


def test_explicit_zero_actual_usage_releases_all_reserved_budget() -> None:
    manager = BudgetManager(BudgetConfig(1000, 10, 1, 2))
    manager.reserve("zero", 100)
    record = manager.settle("zero", ModelUsage(0, 0))
    assert record.actual_cost == 0
    assert record.overspent is False
    assert manager.get_balance().tokens_remaining == 1000
    assert manager.get_balance().cost_remaining == 10


@pytest.mark.parametrize("tokens", [-1, True, 1.5, "10", None])
def test_invalid_token_estimates_are_rejected_without_mutation(tokens: object) -> None:
    manager = BudgetManager(BudgetConfig(1000, 10, 1, 2))
    before = manager.get_balance()
    with pytest.raises(ValueError):
        manager.reserve("bad", cast(int, tokens))
    assert manager.get_balance() == before


@pytest.mark.parametrize("task_id", ["", " bad", "bad ", None, 1])
def test_invalid_task_ids_are_rejected(task_id: object) -> None:
    manager = BudgetManager(BudgetConfig(1000, 10, 1, 2))
    with pytest.raises(ValueError):
        manager.reserve(cast(str, task_id), 100)
    assert manager.get_balance().tokens_remaining == 1000


@pytest.mark.parametrize(
    "usage",
    [ModelUsage(-1, 0), ModelUsage(0, -1), ModelUsage(True, 0), ModelUsage(cast(int, 1.5), 0)],
)
def test_invalid_actual_usage_preserves_reservation(usage: ModelUsage) -> None:
    manager = BudgetManager(BudgetConfig(1000, 10, 1, 2))
    manager.reserve("bad", 100)
    before = manager.get_balance()
    with pytest.raises(ValueError):
        manager.settle("bad", usage)
    assert manager.get_balance() == before
    assert manager.get_usage("bad").settled is False


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True, "1", None])
@pytest.mark.parametrize("field", ["max_total_cost", "cost_per_1k_in", "cost_per_1k_out"])
def test_invalid_cost_configuration_rejected(field: str, value: object) -> None:
    kwargs: dict[str, object] = {
        "max_total_tokens": 100,
        "max_total_cost": 1,
        "cost_per_1k_in": 1,
        "cost_per_1k_out": 1,
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        BudgetConfig(**cast(dict[str, int], kwargs))


@pytest.mark.parametrize("value", [-1, True, 1.5, "1", None])
def test_invalid_token_limit_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        BudgetConfig(cast(int, value), 1, 1, 1)


def test_config_and_returned_snapshots_are_immutable() -> None:
    config = BudgetConfig(1000, 10, 1, 2)
    manager = BudgetManager(config)
    record = manager.reserve("immutable", 100)
    snapshot = manager.get_balance()
    for target, attribute, value in (
        (config, "max_total_tokens", 999999),
        (record, "estimated_tokens", 0),
        (snapshot, "tokens_remaining", 999999),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(target, attribute, value)
    manager.settle("immutable", ModelUsage(0, 0))
    assert snapshot.tokens_remaining == 900
    assert record.settled is False
    assert manager.get_usage("immutable").settled is True


def test_decimal_prices_have_no_accumulation_drift_or_context_rounding() -> None:
    with localcontext() as context:
        context.prec = 6
        manager = BudgetManager(BudgetConfig(1000, Decimal("0.000123456789"), 0.1, 0.1))
        manager.reserve("one", 1)
        assert manager.get_balance().cost_remaining == Decimal("0.000023456789")
        manager.settle("one", ModelUsage(1, 0))
        assert manager.get_balance().cost_remaining == Decimal("0.000023456789")
    manager = BudgetManager(BudgetConfig(1000, Decimal("0.0003"), 0.1, 0.1))
    for index in range(3):
        manager.reserve(f"decimal-{index}", 1)
    assert manager.get_balance().cost_remaining == 0
    with pytest.raises(BudgetExceededError):
        manager.reserve("fourth", 1)


def test_free_route_still_enforces_token_limit() -> None:
    manager = BudgetManager(BudgetConfig(1, 0, 0, 0))
    manager.reserve("free", 1)
    with pytest.raises(BudgetExceededError):
        manager.reserve("another", 1)
    assert manager.get_balance().cost_remaining == 0
