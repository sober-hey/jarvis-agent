"""K1-008 DRAFT: in-memory model budget reservations and accounting.

NOT_APPROVED / NOT_FROZEN / runtime_enabled=False.
No model calls, credential lookup, persistence or runtime wiring. Limits are
local to one manager; this draft is not a durable or cross-process budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from threading import RLock
from typing import ClassVar, cast

from jarvis.model_provider import ModelUsage

_LOGGER = logging.getLogger(__name__)


def _tokens(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _money(value: object, label: str) -> Fraction:
    if type(value) not in (int, float, Decimal):
        raise ValueError(f"{label} must be a finite non-negative number")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return Fraction(amount)


def _decimal(value: Fraction) -> Decimal:
    # Prices originate as finite decimals, so all ledger fractions terminate.
    # Isolate public conversion from the caller's Decimal precision/context.
    with localcontext() as context:
        context.prec = len(str(abs(value.numerator))) + len(str(value.denominator)) + 1
        return Decimal(value.numerator) / Decimal(value.denominator)


def _task_id(value: object) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("task_id must be non-empty without surrounding whitespace")


@dataclass(frozen=True)
class BudgetConfig:
    """Finite ceilings and split prices in one caller-agreed currency.

    Registry cost_per_1k_tokens is an aggregate estimate, not split billing.
    Caller must explicitly supply split rates; nothing reads registry/config.
    """

    max_total_tokens: int
    max_total_cost: int | float | Decimal
    cost_per_1k_in: int | float | Decimal
    cost_per_1k_out: int | float | Decimal

    def __post_init__(self) -> None:
        _tokens(self.max_total_tokens, "max_total_tokens")
        _money(self.max_total_cost, "max_total_cost")
        _money(self.cost_per_1k_in, "cost_per_1k_in")
        _money(self.cost_per_1k_out, "cost_per_1k_out")


@dataclass(frozen=True)
class BudgetBalance:
    """Available amounts; negative after overspend, never clamped to zero."""

    tokens_remaining: int
    cost_remaining: Decimal


@dataclass(frozen=True)
class BudgetUsage:
    """Immutable task accounting; None actual values mean unreported use."""

    task_id: str
    estimated_tokens: int
    reserved_cost: Decimal
    actual_usage: ModelUsage | None = None
    actual_cost: Decimal | None = None
    settled: bool = False
    overspent_tokens: int = 0
    overspent_cost: Decimal = Decimal(0)

    @property
    def overspent(self) -> bool:
        return self.overspent_tokens > 0 or self.overspent_cost > 0


class BudgetExceededError(ValueError):
    """Admission refused; no reservation or charge has been made."""


class BudgetManager:
    """Thread-safe local ledger, not an execution/approval capability.

    Aggregate estimates reserve every token at the higher input/output price.
    Settlements use complete ModelUsage with split rates and may drive balances
    negative. Missing usage retains the full reservation for reconciliation;
    timeouts/cancellation do not release it or authorize a retry.

    One task_id identifies one call/reservation, never all calls in a task.
    Re-reservation is rejected; identical settlement replay is idempotent and
    conflicting replay is rejected. No automatic retries, resets or releases.
    """

    runtime_enabled: ClassVar[bool] = False

    def __init__(self, config: BudgetConfig) -> None:
        if not isinstance(cast(object, config), BudgetConfig):
            raise TypeError("config must be BudgetConfig")
        self._config = config
        self._cost_in = _money(config.cost_per_1k_in, "cost_per_1k_in")
        self._cost_out = _money(config.cost_per_1k_out, "cost_per_1k_out")
        self._max_cost = _money(config.max_total_cost, "max_total_cost")
        self._charged_tokens = 0
        self._charged_cost = Fraction(0)
        self._entries: dict[str, BudgetUsage] = {}
        self._lock = RLock()

    def reserve(self, task_id: str, estimated_tokens: int) -> BudgetUsage:
        """Atomically check both balances and hold the estimate before dispatch.

        BudgetExceededError means insufficient tokens OR cost; no state change.
        Returns a snapshot, not permission to execute a model/tool operation.
        """
        _task_id(task_id)
        tokens = _tokens(estimated_tokens, "estimated_tokens")
        cost = tokens * max(self._cost_in, self._cost_out) / 1000
        with self._lock:
            if task_id in self._entries:
                raise ValueError("task_id is already reserved")
            if (
                self._charged_tokens + tokens > self._config.max_total_tokens
                or self._charged_cost + cost > self._max_cost
            ):
                raise BudgetExceededError("insufficient token or cost balance")
            record = BudgetUsage(task_id, tokens, _decimal(cost))
            self._entries[task_id] = record
            self._charged_tokens += tokens
            self._charged_cost += cost
            return record

    def settle(self, task_id: str, actual_usage: ModelUsage) -> BudgetUsage:
        """Replace the estimate with known actual usage, even if over budget.

        None token fields are unreported, never zero; reject incomplete or
        malformed use without releasing any reservation. An overage is recorded
        on the snapshot and logged once, not rejected or silently capped.
        """
        _task_id(task_id)
        if not isinstance(cast(object, actual_usage), ModelUsage):
            raise TypeError("actual_usage must be ModelUsage")
        if actual_usage.tokens_in is None or actual_usage.tokens_out is None:
            raise ValueError("actual usage is unreported; reservation retained")
        tokens_in = _tokens(actual_usage.tokens_in, "tokens_in")
        tokens_out = _tokens(actual_usage.tokens_out, "tokens_out")
        tokens = tokens_in + tokens_out
        cost = (tokens_in * self._cost_in + tokens_out * self._cost_out) / 1000
        with self._lock:
            previous = self._entries[task_id]
            if previous.settled:
                if previous.actual_usage == actual_usage:
                    return previous
                raise ValueError("task_id is already settled with different usage")
            reserved_cost = Fraction(previous.reserved_cost)
            record = BudgetUsage(
                task_id=task_id,
                estimated_tokens=previous.estimated_tokens,
                reserved_cost=previous.reserved_cost,
                actual_usage=actual_usage,
                actual_cost=_decimal(cost),
                settled=True,
                overspent_tokens=max(0, tokens - previous.estimated_tokens),
                overspent_cost=_decimal(max(Fraction(0), cost - reserved_cost)),
            )
            self._entries[task_id] = record
            self._charged_tokens += tokens - previous.estimated_tokens
            self._charged_cost += cost - reserved_cost
        if record.overspent:
            # Keep caller-provided identifiers out of log text. Durable alarm
            # delivery and trusted identity integration are outside this draft.
            _LOGGER.warning(
                "model budget reservation exceeded: extra_tokens=%d extra_cost=%s",
                record.overspent_tokens,
                record.overspent_cost,
            )
        return record

    def get_balance(self) -> BudgetBalance:
        """Return a consistent immutable balance including outstanding holds."""
        with self._lock:
            return BudgetBalance(
                self._config.max_total_tokens - self._charged_tokens,
                _decimal(self._max_cost - self._charged_cost),
            )

    def get_usage(self, task_id: str) -> BudgetUsage:
        """Return an immutable task snapshot; missing task raises KeyError."""
        _task_id(task_id)
        with self._lock:
            return self._entries[task_id]
