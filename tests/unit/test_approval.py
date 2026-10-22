"""K1-007 offline DRAFT probes; no Messages, network or runtime database."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis.approval import (
    DRAFT_STATUS,
    RUNTIME_ENABLED,
    ApprovalError,
    ApprovalGate,
    ApprovalNotification,
    ApprovalPolicy,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalStore,
    RiskLevel,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ApprovalStore]:
    with ApprovalStore(tmp_path / "approvals.sqlite3", draft_fixture=True) as ledger:
        yield ledger


def test_draft_requires_explicit_opt_in(tmp_path: Path) -> None:
    assert DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert RUNTIME_ENABLED is False
    with pytest.raises(ApprovalError, match="draft_fixture"):
        ApprovalStore(tmp_path / "not-created.sqlite3")
    assert not (tmp_path / "not-created.sqlite3").exists()


def test_low_is_automatically_approved(store: ApprovalStore) -> None:
    record = ApprovalGate(store).evaluate(
        "op-low", "file.publish.no_clobber", {"target": "new/report.txt"}, timestamp=10
    )
    assert isinstance(record, ApprovalRecord)
    assert record.risk_level is RiskLevel.LOW
    assert record.status is ApprovalStatus.APPROVED
    assert record.approved_by == "policy:auto"
    assert record.may_proceed is True
    assert record.timestamp == 10
    assert record.reason
    assert store.pending_notifications() == []


def test_medium_is_recorded_not_forged_human_approval(store: ApprovalStore) -> None:
    record = ApprovalGate(store).evaluate("op-medium", "task.cancel", {}, timestamp=10)
    assert record.risk_level is RiskLevel.MEDIUM
    assert record.status is ApprovalStatus.RECORDED
    assert record.approved_by is None
    assert record.may_proceed is True
    assert store.history("op-medium") == [record]


def test_high_is_sealed_before_imessage_notification(store: ApprovalStore) -> None:
    received: list[ApprovalNotification] = []

    def notify(notification: ApprovalNotification) -> bool:
        assert store.history("op-high")[0].status is ApprovalStatus.PENDING
        assert store.request("op-high").canonical_arguments == '{"target":"old.txt"}'
        received.append(notification)
        return True  # synthetic delivery receipt, not a real iMessage

    gate = ApprovalGate(store, imessage_notifier=notify)
    record = gate.evaluate("op-high", "file.delete", {"target": "old.txt"}, timestamp=10)
    assert record.risk_level is RiskLevel.HIGH
    assert record.approved_by is None
    assert record.status is ApprovalStatus.PENDING
    assert record.may_proceed is False
    assert len(received) == 1
    assert received[0].channel == "imessage"
    assert received[0].operation_id == "op-high"
    assert received[0].binding_digest == store.request("op-high").binding_digest
    assert store.pending_notifications() == []


def test_history_and_sealed_request_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "approval.sqlite3"
    with ApprovalStore(path, draft_fixture=True) as ledger:
        gate = ApprovalGate(ledger)
        low = gate.evaluate("op-low", "file.publish.no_clobber", {}, timestamp=10)
        high = gate.evaluate("op-high", "external.call", {"endpoint": "fake"}, timestamp=11)
        request = ledger.request("op-high")
    with ApprovalStore(path, draft_fixture=True) as reopened:
        assert reopened.history("op-low") == [low]
        assert reopened.history("op-high") == [high]
        assert reopened.history() == [low, high]
        assert reopened.request("op-high") == request
        assert reopened.pending_notifications()[0].operation_id == "op-high"
        with pytest.raises(ApprovalError, match="missing"):
            reopened.request("nonexistent")


def test_policy_is_configurable_and_copied(store: ApprovalStore) -> None:
    config = {"file.publish.no_clobber": RiskLevel.MEDIUM, "local.inspect": RiskLevel.LOW}
    policy = ApprovalPolicy(config)
    config["local.inspect"] = RiskLevel.HIGH
    assert policy.classify("local.inspect") is RiskLevel.LOW
    record = ApprovalGate(store, policy=policy).evaluate(
        "op-configured", "file.publish.no_clobber", {}, timestamp=10
    )
    assert record.risk_level is RiskLevel.MEDIUM
    assert policy.classify("unrecognized") is RiskLevel.HIGH


@pytest.mark.parametrize("operation", ["file.delete", "external.call", "production.change"])
def test_configuration_cannot_downgrade_mandatory_high_risk(operation: str) -> None:
    with pytest.raises(ValueError, match="HIGH"):
        ApprovalPolicy({operation: RiskLevel.LOW})


def test_production_context_always_requires_confirmation(store: ApprovalStore) -> None:
    result = ApprovalGate(store).evaluate(
        "op-prod", "file.publish.no_clobber", {"environment": "production"}, timestamp=10
    )
    assert result.risk_level is RiskLevel.HIGH
    assert result.may_proceed is False


def test_replay_is_idempotent_and_altered_arguments_are_rejected(store: ApprovalStore) -> None:
    calls: list[ApprovalNotification] = []
    gate = ApprovalGate(
        store, imessage_notifier=lambda notification: calls.append(notification) or True
    )
    record = gate.evaluate("op", "file.delete", {"a": 1, "b": 2}, timestamp=10)
    assert gate.evaluate("op", "file.delete", {"b": 2, "a": 1}, timestamp=11) == record
    assert len(store.history("op")) == 1
    assert len(calls) == 1
    with pytest.raises(ApprovalError, match="binding"):
        gate.evaluate("op", "file.delete", {"a": 999}, timestamp=12)
    with pytest.raises(ApprovalError, match="binding"):
        gate.evaluate("op", "file.publish.no_clobber", {"a": 1, "b": 2}, timestamp=12)


def test_notification_failure_stays_blocked_and_is_retryable(store: ApprovalStore) -> None:
    def fail(_notification: ApprovalNotification) -> bool:
        raise OSError("private transport detail must not enter SQLite")

    gate = ApprovalGate(store, imessage_notifier=fail)
    result = gate.evaluate("op", "file.delete", {}, timestamp=10)
    assert result.may_proceed is False
    assert len(store.pending_notifications()) == 1
    row = store.connection.execute(
        "SELECT attempts, last_error FROM draft_approval_notifications"
    ).fetchone()
    assert row == (1, "OSError")

    def succeeds(_notification: ApprovalNotification) -> bool:
        return True

    gate.imessage_notifier = succeeds
    assert gate.retry_notifications() == 1
    assert store.pending_notifications() == []
    assert store.history("op") == [result]
    assert store.connection.execute(
        "SELECT attempts FROM draft_approval_notifications"
    ).fetchone() == (2,)


def test_unverified_delivery_is_not_marked_sent(store: ApprovalStore) -> None:
    gate = ApprovalGate(store, imessage_notifier=lambda _notification: False)
    gate.evaluate("op", "external.call", {}, timestamp=10)
    assert len(store.pending_notifications()) == 1


def test_model_or_cli_cannot_supply_an_approver(store: ApprovalStore) -> None:
    gate = ApprovalGate(store)
    gate.evaluate("op", "file.delete", {}, timestamp=10)
    with pytest.raises(ApprovalError, match="trusted"):
        gate.confirm("op", proof="approved_by=owner", timestamp=11)
    assert store.history("op")[-1].may_proceed is False


def test_fixture_confirmation_uses_verified_identity_and_bound_request(
    store: ApprovalStore,
) -> None:
    seen: list[str] = []
    proof = object()

    def verify(request: ApprovalRequest, supplied: object) -> str | None:
        if supplied is not proof:
            return None
        seen.append(request.binding_digest)
        return "fixture-human:owner"

    gate = ApprovalGate(store, confirmation_verifier=verify, validity_ms=20)
    gate.evaluate("op", "file.delete", {"target": "fixed"}, timestamp=10)
    with pytest.raises(ApprovalError, match="identity"):
        gate.confirm("op", proof="model forgery", timestamp=11)
    approved = gate.confirm("op", proof=proof, timestamp=12)
    assert approved.approved_by == "fixture-human:owner"
    assert approved.risk_level is RiskLevel.HIGH
    assert approved.may_proceed is True
    assert seen == [store.request("op").binding_digest]
    assert [row.status for row in store.history("op")] == [
        ApprovalStatus.PENDING,
        ApprovalStatus.APPROVED,
    ]
    with pytest.raises(ApprovalError, match="pending"):
        gate.confirm("op", proof=proof, timestamp=13)
    assert store.pending_notifications() == []


def test_expired_confirmation_fails_closed(store: ApprovalStore) -> None:
    gate = ApprovalGate(
        store, confirmation_verifier=lambda _request, _proof: "owner", validity_ms=5
    )
    gate.evaluate("op", "production.change", {}, timestamp=10)
    with pytest.raises(ApprovalError, match="expired"):
        gate.confirm("op", proof=object(), timestamp=15)
    assert store.history("op")[-1].status is ApprovalStatus.PENDING


def test_transaction_rolls_back_request_if_audit_insert_fails(store: ApprovalStore) -> None:
    store.connection.execute(
        "CREATE TRIGGER reject_audit BEFORE INSERT ON draft_approval_records "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        ApprovalGate(store).evaluate("op", "file.delete", {}, timestamp=10)
    assert store.history() == []
    assert store.pending_notifications() == []
    with pytest.raises(ApprovalError, match="missing"):
        store.request("op")
    assert store.connection.in_transaction is False


@pytest.mark.parametrize("value", [-1, True, 2**63])
def test_invalid_timestamp_is_rejected(store: ApprovalStore, value: int) -> None:
    with pytest.raises(ValueError, match="timestamp"):
        ApprovalGate(store).evaluate("op", "file.delete", {}, timestamp=value)
    assert store.history() == []


def test_existing_unrelated_database_is_rejected_without_migration(tmp_path: Path) -> None:
    path = tmp_path / "unrelated.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")
    before = path.read_bytes()
    with pytest.raises(ApprovalError, match="standalone"):
        ApprovalStore(path, draft_fixture=True)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("tasks",)]


def test_policy_replay_cannot_downgrade_existing_high_request(store: ApprovalStore) -> None:
    strict = ApprovalGate(store, policy=ApprovalPolicy({"local.inspect": RiskLevel.HIGH}))
    strict.evaluate("op", "local.inspect", {}, timestamp=10)
    lower = ApprovalGate(store, policy=ApprovalPolicy({"local.inspect": RiskLevel.LOW}))
    with pytest.raises(ApprovalError, match="binding"):
        lower.evaluate("op", "local.inspect", {}, timestamp=11)
    assert store.history("op")[-1].status is ApprovalStatus.PENDING


def test_notification_retries_automatically_on_replay(store: ApprovalStore) -> None:
    calls: list[ApprovalNotification] = []

    def notify(notification: ApprovalNotification) -> bool:
        calls.append(notification)
        return len(calls) > 1

    gate = ApprovalGate(store, imessage_notifier=notify)
    first = gate.evaluate("op", "external.call", {}, timestamp=10)
    assert store.pending_notifications()
    assert gate.evaluate("op", "external.call", {}, timestamp=11) == first
    assert len(calls) == 2
    assert store.pending_notifications() == []
    assert first.may_proceed is False


def test_nan_and_non_string_argument_keys_are_rejected(store: ApprovalStore) -> None:
    gate = ApprovalGate(store)
    with pytest.raises(ValueError):
        gate.evaluate("nan", "file.delete", {"value": float("nan")}, timestamp=10)
    with pytest.raises(ValueError):
        gate.evaluate("keys", "file.delete", {1: "ambiguous"}, timestamp=10)  # type: ignore[dict-item]
    assert store.history() == []
