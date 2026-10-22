"""Offline K3-006 autonomy quota candidate tests; never touch product state."""

import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext
from pathlib import Path
from typing import cast

import pytest

from jarvis.approval import ApprovalGate, ApprovalPolicy, ApprovalStatus, ApprovalStore, RiskLevel
from jarvis.autonomy_budget import AutonomyBudgetError, AutonomyLedger, AutonomyQuota


class Clock:
    def __init__(self, now: int = 10_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def test_record_action_reduces_remaining_quota(tmp_path: Path) -> None:
    clock = Clock()
    quota = AutonomyQuota(2, 100, 2, Decimal("1.00"))
    with AutonomyLedger(tmp_path / "budget.db", quota, clock=clock, draft_fixture=True) as ledger:
        assert ledger.check_quota("file.read") is True
        ledger.record_action("file.read", 25, Decimal("0.10"))
        assert ledger.get_usage(clock.now) == {
            "actions": 1,
            "tokens": 25,
            "notifications": 0,
            "cost": Decimal("0.10"),
        }
        assert ledger.check_quota("file.read") is True
        ledger.record_action("file.read", 25, Decimal("0.10"))
        assert ledger.check_quota("file.read") is False


@pytest.mark.parametrize(
    ("tokens", "cost"),
    [(100, Decimal("0")), (0, Decimal("1")), (101, Decimal("1.01"))],
)
def test_each_resource_ceiling_denies_at_limit(tmp_path: Path, tokens: int, cost: Decimal) -> None:
    with AutonomyLedger(
        tmp_path / "budget.db", AutonomyQuota(10, 100, 10, 1), draft_fixture=True
    ) as ledger:
        ledger.record_action("read", tokens, cost)
        assert ledger.check_quota("other-action") is False
        assert ledger.get_usage()["tokens"] == tokens
        assert ledger.get_usage()["cost"] == cost


def test_notifications_consume_shared_quota(tmp_path: Path) -> None:
    with AutonomyLedger(
        tmp_path / "budget.db", AutonomyQuota(10, 100, 2, 1), draft_fixture=True
    ) as ledger:
        ledger.record_notification("imessage")
        assert ledger.get_usage()["notifications"] == 1
        assert ledger.check_quota("notification") is True
        ledger.record_notification("web")
        assert ledger.get_usage()["actions"] == 0
        assert ledger.check_quota("notification") is False
        assert ledger.check_quota("read") is False


def test_rolling_hour_expires_entries_individually_without_manual_reset(tmp_path: Path) -> None:
    clock = Clock()
    with AutonomyLedger(
        tmp_path / "budget.db",
        AutonomyQuota(2, 100, 10, 1),
        clock=clock,
        draft_fixture=True,
    ) as ledger:
        ledger.record_action("first", 25, Decimal("0.1"))
        clock.now += 1000
        ledger.record_action("second", 25, Decimal("0.1"))
        assert ledger.check_quota("third") is False
        clock.now += 3_599_000
        assert ledger.get_usage()["actions"] == 1
        assert ledger.check_quota("third") is True
        clock.now += 1000
        assert ledger.get_usage()["actions"] == 0


def test_reset_window_does_not_clear_unexpired_usage(tmp_path: Path) -> None:
    clock = Clock()
    with AutonomyLedger(
        tmp_path / "budget.db",
        AutonomyQuota(1, 100, 10, 1),
        clock=clock,
        draft_fixture=True,
    ) as ledger:
        ledger.record_action("read", 10, 0)
        ledger.reset_window()
        assert ledger.check_quota("read") is False
        clock.now += 3_600_000
        ledger.reset_window()
        assert ledger.check_quota("read") is True


def test_reopen_preserves_usage_and_consecutive_denials(tmp_path: Path) -> None:
    clock = Clock()
    quota = AutonomyQuota(1, 100, 10, 1)
    path = tmp_path / "budget.db"
    with AutonomyLedger(path, quota, clock=clock, draft_fixture=True) as ledger:
        ledger.record_action("read", 12, Decimal("0.12"))
        ledger.record_notification("web")
        assert ledger.check_quota("read") is False
        assert ledger.check_quota("read") is False
        assert not ledger.urgent
    with AutonomyLedger(path, quota, clock=clock, draft_fixture=True) as ledger:
        assert ledger.get_usage() == {
            "actions": 1,
            "tokens": 12,
            "notifications": 1,
            "cost": Decimal("0.12"),
        }
        assert ledger.check_quota("read") is False
        assert ledger.urgent
        event = ledger.urgent_events[0]
        assert event.priority.name == "URGENT"
        assert event.type.name == "DECISION"
        assert event.payload["consecutive_exceeded"] == 3
        assert event.payload["required_risk_level"] == "HIGH"
        assert event.created_at == clock.now
        assert event.payload["usage"]["cost"] == "0.12"
        assert ledger.check_quota("read") is False
        assert len(ledger.urgent_events) == 1
    with AutonomyLedger(path, quota, clock=clock, draft_fixture=True) as ledger:
        assert ledger.urgent_events[0].id == event.id


def test_success_and_elapsed_hour_break_denial_streak(tmp_path: Path) -> None:
    clock = Clock()
    with AutonomyLedger(
        tmp_path / "budget.db",
        AutonomyQuota(1, 100, 10, 1),
        clock=clock,
        draft_fixture=True,
    ) as ledger:
        ledger.record_action("read", 10, 0)
        assert not ledger.check_quota("read")
        assert not ledger.check_quota("read")
        clock.now += 3_600_000
        assert ledger.check_quota("read")
        ledger.record_action("read", 10, 0)
        assert not ledger.check_quota("read")
        assert not ledger.urgent
        assert not ledger.check_quota("read")
        clock.now += 3_600_000
        ledger.record_action("read", 10, 0)
        assert not ledger.check_quota("read")
        assert not ledger.urgent


def test_usage_can_be_narrowed_by_inclusive_start(tmp_path: Path) -> None:
    clock = Clock()
    with AutonomyLedger(
        tmp_path / "budget.db",
        AutonomyQuota(10, 100, 10, 1),
        clock=clock,
        draft_fixture=True,
    ) as ledger:
        ledger.record_action("one", 10, 0)
        clock.now += 1
        ledger.record_action("two", 20, 0)
        assert ledger.get_usage(clock.now)["tokens"] == 20
        assert ledger.get_usage(clock.now + 1)["tokens"] == 0


@pytest.mark.parametrize("field", ["actions", "tokens", "notifications", "cost"])
def test_zero_ceiling_disables_autonomy(tmp_path: Path, field: str) -> None:
    limits = {"actions": 10, "tokens": 100, "notifications": 10, "cost": 1}
    limits[field] = 0
    quota = AutonomyQuota(
        limits["actions"], limits["tokens"], limits["notifications"], limits["cost"]
    )
    with AutonomyLedger(tmp_path / "budget.db", quota, draft_fixture=True) as ledger:
        assert ledger.check_quota("unknown") is False


@pytest.mark.parametrize("bad", [-1, True, 1.5, "10"])
def test_invalid_quota_counters_rejected(bad: object) -> None:
    for index in range(3):
        values = [10, 100, 10]
        values[index] = cast(int, bad)
        with pytest.raises(ValueError):
            AutonomyQuota(values[0], values[1], values[2], 1)


@pytest.mark.parametrize("bad", [-1, True, float("nan"), float("inf"), "0.1"])
def test_invalid_cost_rejected_without_consumption(tmp_path: Path, bad: object) -> None:
    with pytest.raises(ValueError):
        AutonomyQuota(1, 100, 1, cast(float, bad))
    with AutonomyLedger(
        tmp_path / "budget.db", AutonomyQuota(10, 100, 10, 1), draft_fixture=True
    ) as ledger:
        with pytest.raises(ValueError):
            ledger.record_action("read", 10, cast(float, bad))
        assert ledger.get_usage()["actions"] == 0


def test_invalid_records_and_timestamps_do_not_change_usage(tmp_path: Path) -> None:
    with AutonomyLedger(
        tmp_path / "budget.db", AutonomyQuota(10, 100, 10, 1), draft_fixture=True
    ) as ledger:
        for bad in (-1, True, 1.5):
            with pytest.raises(ValueError):
                ledger.record_action("read", cast(int, bad), 0)
        for label in ("", "  ", "a\x00b", "\ud800"):
            with pytest.raises((ValueError, UnicodeError)):
                ledger.record_action(label, 0, 0)
            with pytest.raises((ValueError, UnicodeError)):
                ledger.record_notification(label)
            with pytest.raises((ValueError, UnicodeError)):
                ledger.check_quota(label)
        with pytest.raises(ValueError):
            ledger.get_usage(-1)
        assert ledger.get_usage()["actions"] == 0
        assert not ledger.urgent


def test_decimal_totals_do_not_round_with_caller_context(tmp_path: Path) -> None:
    with AutonomyLedger(
        tmp_path / "budget.db", AutonomyQuota(10, 100, 10, Decimal("0.3")), draft_fixture=True
    ) as ledger:
        with localcontext() as context:
            context.prec = 1
            ledger.record_action("read", 1, Decimal("0.1"))
            ledger.record_action("read", 1, Decimal("0.2"))
            assert ledger.get_usage()["cost"] == Decimal("0.3")
            assert not ledger.check_quota("read")


def test_clock_rollback_cannot_resurrect_or_expire_usage(tmp_path: Path) -> None:
    clock = Clock()
    quota = AutonomyQuota(1, 100, 10, 1)
    path = tmp_path / "budget.db"
    with AutonomyLedger(path, quota, clock=clock, draft_fixture=True) as ledger:
        ledger.record_action("read", 10, 0)
        clock.now -= 1
        for operation in (
            lambda: ledger.check_quota("read"),
            ledger.get_usage,
            ledger.reset_window,
            lambda: ledger.record_action("read", 10, 0),
        ):
            with pytest.raises(AutonomyBudgetError, match="clock moved backwards"):
                operation()
        clock.now += 1
        assert ledger.get_usage()["actions"] == 1
    clock.now -= 1
    with pytest.raises(AutonomyBudgetError):
        AutonomyLedger(path, quota, clock=clock, draft_fixture=True)


def test_draft_requires_opt_in_and_rejects_core_database(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    quota = AutonomyQuota(1, 100, 10, 1)
    with pytest.raises(AutonomyBudgetError, match="draft_fixture"):
        AutonomyLedger(path, quota)
    assert not path.exists()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO tasks VALUES ('retained')")
    before = path.read_bytes()
    with pytest.raises(AutonomyBudgetError, match="standalone"):
        AutonomyLedger(path, quota, draft_fixture=True)
    assert path.read_bytes() == before
    assert AutonomyLedger.runtime_enabled is False


def test_denial_streak_and_urgent_event_commit_atomically(tmp_path: Path) -> None:
    path = tmp_path / "budget.db"
    with AutonomyLedger(path, AutonomyQuota(0, 100, 10, 1), draft_fixture=True) as ledger:
        assert not ledger.check_quota("read")
        assert not ledger.check_quota("read")
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TRIGGER injected_failure BEFORE INSERT ON draft_autonomy_urgent "
                "BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            ledger.check_quota("read")
        assert not ledger.urgent
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT consecutive_exceeded FROM draft_autonomy_state"
            ).fetchone() == (2,)
            connection.execute("DROP TRIGGER injected_failure")
        assert not ledger.check_quota("read")
        assert ledger.urgent


def test_shared_ledger_serializes_threads(tmp_path: Path) -> None:
    with AutonomyLedger(
        tmp_path / "budget.db", AutonomyQuota(1, 100, 10, 1), draft_fixture=True
    ) as ledger:
        ledger.record_action("read", 1, 0)
        with ThreadPoolExecutor(max_workers=8) as executor:
            assert list(executor.map(ledger.check_quota, ["read"] * 16)) == [False] * 16
        assert len(ledger.urgent_events) == 1
        assert ledger.get_usage()["actions"] == 1
        event = ledger.urgent_events[0]
        event.payload["reason"] = "mutated"
        assert ledger.urgent_events[0].payload["reason"] == "autonomy_quota_exceeded"


def test_budget_exhaustion_can_escalate_low_policy_to_high(tmp_path: Path) -> None:
    with (
        AutonomyLedger(
            tmp_path / "budget.db", AutonomyQuota(0, 100, 10, 1), draft_fixture=True
        ) as ledger,
        ApprovalStore(tmp_path / "approval.db", draft_fixture=True) as store,
    ):
        operation_type = "file.publish.no_clobber"
        assert ApprovalPolicy().classify(operation_type) is RiskLevel.LOW
        assert not ledger.check_quota(operation_type)
        gate = ApprovalGate(store, policy=ApprovalPolicy({operation_type: RiskLevel.HIGH}))
        record = gate.evaluate("op-1", operation_type, {}, timestamp=10_000_000)
        assert record.risk_level is RiskLevel.HIGH
        assert record.status is ApprovalStatus.PENDING
        assert record.may_proceed is False


def test_usage_survives_fresh_process(tmp_path: Path) -> None:
    path = tmp_path / "budget.db"
    script = (
        "import sys;from jarvis.autonomy_budget import AutonomyLedger,AutonomyQuota;"
        "ledger=AutonomyLedger(sys.argv[1],AutonomyQuota(2,100,10,1),"
        "clock=lambda:10_000_000,draft_fixture=True);"
        "ledger.record_action('read',12,0.12);ledger.close()"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    with AutonomyLedger(
        path, AutonomyQuota(2, 100, 10, 1), clock=Clock(), draft_fixture=True
    ) as ledger:
        assert ledger.get_usage()["tokens"] == 12
        assert ledger.get_usage()["cost"] == Decimal("0.12")
