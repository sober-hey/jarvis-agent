"""K3-006 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

Offline standalone rolling-hour accounting only. A False check must be escalated
by the caller to ApprovalGate with RiskLevel.HIGH (there is no ApprovalGate.HIGH).
A True check is advisory, not a reservation, execution grant or safety approval.
No product database migration, scheduler wiring, live sends or model calls.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from threading import RLock
from typing import Any, ClassVar, cast
from uuid import uuid4

from jarvis.approval import RiskLevel
from jarvis.event_schema import Event, EventPriority, EventType

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False
HOUR_MS = 3_600_000


def _integer(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError(f"{name} must be an exact nonnegative SQLite integer")
    return value


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be nonempty text without NUL")
    value.encode("utf-8", errors="strict")
    return value


def _money(value: object) -> Fraction:
    if type(value) not in (int, float, Decimal):
        raise ValueError("cost must be a finite nonnegative CNY amount")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("cost must be a finite nonnegative CNY amount")
    return Fraction(amount)


def _decimal(value: Fraction) -> Decimal:
    with localcontext() as context:
        context.prec = len(str(abs(value.numerator))) + len(str(value.denominator)) + 1
        return Decimal(value.numerator) / Decimal(value.denominator)


def _utc_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class AutonomyQuota:
    """Trusted shared ceilings; zero in any dimension disables autonomy."""

    max_actions_per_hour: int
    max_tokens_per_hour: int
    max_notifications_per_hour: int
    max_cost_per_hour: int | float | Decimal

    def __post_init__(self) -> None:
        for name in (
            "max_actions_per_hour",
            "max_tokens_per_hour",
            "max_notifications_per_hour",
        ):
            _integer(getattr(self, name), name)
        _money(self.max_cost_per_hour)


class AutonomyBudgetError(RuntimeError):
    """Fixture/database/clock precondition failed; caller must not execute."""


_SCHEMA = (
    "CREATE TABLE draft_autonomy_entries ("
    "id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL CHECK(created_at>=0), "
    "kind TEXT NOT NULL CHECK(kind IN ('action','notification')), "
    "label TEXT NOT NULL, tokens INTEGER NOT NULL CHECK(tokens>=0), cost TEXT NOT NULL)",
    "CREATE INDEX draft_autonomy_time ON draft_autonomy_entries(created_at)",
    "CREATE TABLE draft_autonomy_state ("
    "id INTEGER PRIMARY KEY CHECK(id=1), high_water_ms INTEGER NOT NULL CHECK(high_water_ms>=0), "
    "consecutive_exceeded INTEGER NOT NULL CHECK(consecutive_exceeded>=0), "
    "last_denied_ms INTEGER)",
    "CREATE TABLE draft_autonomy_urgent ("
    "id TEXT PRIMARY KEY NOT NULL, created_at INTEGER NOT NULL, payload TEXT NOT NULL)",
)


class AutonomyLedger:
    """Durable usage in (now-1 hour, now], with timestamps in UTC epoch ms.

    record_* records actual usage even after exhaustion; refusing to account for
    an overspend would hide it. The caller owns check-before-effect and actual
    usage reporting. Checks do not reserve: concurrent check/execute sequences
    can overshoot. This draft is NOT a hard pre-dispatch budget or an executor.
    The trusted owner supplies an isolated DB path and quota, never model input.
    """

    runtime_enabled: ClassVar[bool] = False

    def __init__(
        self,
        path: str | Path,
        quota: AutonomyQuota,
        *,
        clock: Callable[[], int] = _utc_ms,
        draft_fixture: bool = False,
    ) -> None:
        if draft_fixture is not True:
            raise AutonomyBudgetError("explicit offline draft_fixture=True required")
        if not isinstance(cast(object, quota), AutonomyQuota):
            raise TypeError("quota must be AutonomyQuota")
        self.quota = quota
        self._clock = clock
        self._lock = RLock()
        self._connection = sqlite3.connect(path, autocommit=True, check_same_thread=False)
        try:
            with self._write():
                actual = self._schema()
                expected = {" ".join(statement.split()) for statement in _SCHEMA}
                if actual and actual != expected:
                    raise AutonomyBudgetError("standalone autonomy fixture database required")
                if not actual:
                    for statement in _SCHEMA:
                        self._connection.execute(statement)
                    self._connection.execute("INSERT INTO draft_autonomy_state VALUES (1,0,0,NULL)")
                self._now()
        except BaseException:
            self._connection.close()
            raise

    def _schema(self) -> set[str]:
        return {
            " ".join(row[0].split())
            for row in self._connection.execute(
                "SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }

    def __enter__(self) -> AutonomyLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _write(self) -> Generator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

    def _now(self) -> int:
        now = _integer(self._clock(), "clock (UTC ms)")
        row = self._connection.execute(
            "SELECT high_water_ms FROM draft_autonomy_state WHERE id=1"
        ).fetchone()
        if row is None or now < row[0]:
            raise AutonomyBudgetError("missing state or UTC clock moved backwards")
        return now

    def _advance(self, now: int) -> None:
        self._connection.execute(
            "UPDATE draft_autonomy_state SET high_water_ms=? WHERE id=1", (now,)
        )

    def record_action(
        self, action_type: str, tokens_used: int, cost: int | float | Decimal
    ) -> None:
        """Record one actual action, including known excess; never execute it."""
        label = _text(action_type, "action_type")
        tokens = _integer(tokens_used, "tokens_used")
        amount = str(_decimal(_money(cost)))
        with self._write():
            now = self._now()
            self._connection.execute(
                "INSERT INTO draft_autonomy_entries(created_at,kind,label,tokens,cost) "
                "VALUES (?,'action',?,?,?)",
                (now, label, tokens, amount),
            )
            self._advance(now)

    def record_notification(self, channel: str) -> None:
        """Account for one notification; neither creates nor verifies a send."""
        label = _text(channel, "channel")
        with self._write():
            now = self._now()
            self._connection.execute(
                "INSERT INTO draft_autonomy_entries(created_at,kind,label,tokens,cost) "
                "VALUES (?,'notification',?,0,'0')",
                (now, label),
            )
            self._advance(now)

    def _usage(self, now: int, window_start: int) -> dict[str, int | Decimal]:
        actions = tokens = notifications = 0
        cost = Fraction(0)
        rows = self._connection.execute(
            "SELECT kind,tokens,cost FROM draft_autonomy_entries "
            "WHERE created_at>? AND created_at>=? AND created_at<=?",
            (now - HOUR_MS, window_start, now),
        )
        for kind, used_tokens, used_cost in rows:
            actions += int(kind == "action")
            notifications += int(kind == "notification")
            tokens += used_tokens
            cost += Fraction(Decimal(used_cost))
        return {
            "actions": actions,
            "tokens": tokens,
            "notifications": notifications,
            "cost": _decimal(cost),
        }

    def get_usage(self, window_start: int | None = None) -> dict[str, int | Decimal]:
        """Current rolling usage, optionally narrowed by an inclusive UTC-ms bound."""
        if window_start is not None:
            _integer(window_start, "window_start")
        with self._lock:
            now = self._now()
            return self._usage(now, window_start if window_start is not None else 0)

    def check_quota(self, action_type: str) -> bool:
        """Check all four shared ceilings, regardless of action/channel name.

        Exhaustion (usage >= ceiling) returns False. Unknown names receive the
        same conservative checks. False requires RiskLevel.HIGH at ApprovalGate;
        successful checks never lower an operation's existing risk floor.
        """
        label = _text(action_type, "action_type")
        with self._write():
            now = self._now()
            usage = self._usage(now, 0)
            allowed = (
                usage["actions"] < self.quota.max_actions_per_hour
                and usage["tokens"] < self.quota.max_tokens_per_hour
                and usage["notifications"] < self.quota.max_notifications_per_hour
                and usage["cost"] < _decimal(_money(self.quota.max_cost_per_hour))
            )
            self._record_check(now, label, allowed, usage)
            self._advance(now)
            return allowed

    def _record_check(
        self, now: int, label: str, allowed: bool, usage: dict[str, int | Decimal]
    ) -> None:
        row = self._connection.execute(
            "SELECT consecutive_exceeded,last_denied_ms FROM draft_autonomy_state WHERE id=1"
        ).fetchone()
        streak, last_denied = row
        if last_denied is None or now - last_denied >= HOUR_MS:
            streak = 0
        # Saturate at three: one durable URGENT event per uninterrupted streak,
        # not one for every poll while the same exhausted condition persists.
        streak = 0 if allowed else min(streak + 1, 3)
        if not allowed and streak == 3 and row[0] != 3:
            payload = {
                "reason": "autonomy_quota_exceeded",
                "action_type": label,
                "consecutive_exceeded": 3,
                "required_risk_level": RiskLevel.HIGH.value,
                "usage": {
                    key: str(value) if isinstance(value, Decimal) else value
                    for key, value in usage.items()
                },
            }
            self._connection.execute(
                "INSERT INTO draft_autonomy_urgent(id,created_at,payload) VALUES (?,?,?)",
                (uuid4().hex, now, json.dumps(payload, sort_keys=True, allow_nan=False)),
            )
        self._connection.execute(
            "UPDATE draft_autonomy_state SET consecutive_exceeded=?,last_denied_ms=? WHERE id=1",
            (streak, None if allowed else now),
        )

    def reset_window(self) -> None:
        """Advance the rolling window; NEVER grant a fresh hour early.

        Entries expire by time, not by a caller-triggered clear. Keep historical
        records/alerts for inspection. Only an hour without a denied check clears
        an old denial streak; unexpired usage and URGENT history are retained.
        """
        with self._write():
            now = self._now()
            self._connection.execute(
                "UPDATE draft_autonomy_state SET consecutive_exceeded=0,last_denied_ms=NULL "
                "WHERE last_denied_ms<=?",
                (now - HOUR_MS,),
            )
            self._advance(now)

    @property
    def urgent(self) -> bool:
        """Sticky historical flag, not an external notification receipt."""
        with self._lock:
            return (
                self._connection.execute("SELECT 1 FROM draft_autonomy_urgent LIMIT 1").fetchone()
                is not None
            )

    @property
    def urgent_events(self) -> list[Event]:
        """Detached durable URGENT intents; no bus/outbox publication is claimed.

        session_id is a fixed fixture namespace, not a trusted runtime identity.
        Polling does not rewrite/acknowledge events or consume notification quota.
        """
        with self._lock:
            return [
                Event(
                    id=identity,
                    type=EventType.DECISION,
                    priority=EventPriority.URGENT,
                    source="autonomy_budget",
                    payload=cast(dict[str, Any], json.loads(payload)),
                    created_at=created_at,
                    session_id="autonomy-budget-draft",
                    tags=("autonomy", "quota_exceeded"),
                )
                for identity, created_at, payload in self._connection.execute(
                    "SELECT id,created_at,payload FROM draft_autonomy_urgent ORDER BY rowid"
                )
            ]
