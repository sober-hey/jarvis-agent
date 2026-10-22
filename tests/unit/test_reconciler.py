"""K0-013 offline DRAFT: real SQLite/flock and synthetic safety grants only."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis.intent import IntentRepository, OperationIntent
from jarvis.repository import GuardRejectedError, TaskRepository
from jarvis.worker import Worker

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    spec = importlib.util.spec_from_file_location(
        "k0013_schema_fixture", ROOT / "tests/contract/test_sqlite_schema_draft.py"
    )
    assert spec is not None and spec.loader is not None
    schema = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schema)
    db = schema._apply_candidate(schema._load())
    db.execute("INSERT INTO system_control VALUES (1, 0, 0, 7, 0)")
    db.commit()
    try:
        yield db
    finally:
        db.close()


def _ledger(worker: Worker, denied: set[str] | None = None) -> IntentRepository:
    repository = worker.execute(lambda repository: repository)
    return IntentRepository(
        TaskRepository(
            worker.connection,
            worker_lock=repository.worker_lock,
            worker_epoch=worker.epoch,
            draft_fixture=True,
            domain_guard=lambda guard, _task: guard not in (denied or set()),
        )
    )


def _running(ledger: IntentRepository, root: Path, suffix: str = "1") -> OperationIntent:
    repository = ledger.repository
    db = repository.connection
    repository.create_task(
        task_id=f"t{suffix}",
        request_key=f"r{suffix}",
        request_digest="a" * 64,
        task_kind="inventory.generate",
        now_ms=1,
    )
    db.execute(
        "INSERT INTO task_plans VALUES (?, ?, 1, 'ACTIVE', ?, 1)",
        (f"p{suffix}", f"t{suffix}", "b" * 64),
    )
    db.execute(
        "UPDATE tasks SET state='RUNNING',active_plan_id=? WHERE id=?",
        (f"p{suffix}", f"t{suffix}"),
    )
    db.execute(
        "INSERT INTO task_steps (id,task_id,plan_id,ordinal,logical_effect_id,state) "
        "VALUES (?, ?, ?, 1, ?, 'CLAIMED')",
        (f"s{suffix}", f"t{suffix}", f"p{suffix}", f"effect{suffix}"),
    )
    db.commit()
    (root / "output").mkdir(exist_ok=True)
    (root / "prepared").mkdir(exist_ok=True)
    intent = OperationIntent(
        task_id=f"t{suffix}",
        logical_effect_id=f"effect{suffix}",
        step_id=f"s{suffix}",
        target_path=f"output/{suffix}.json",
        prepared_path=f"prepared/{suffix}.json",
        expected_sha256=hashlib.sha256(b"inventory").hexdigest(),
        expected_size=len(b"inventory"),
        input_snapshot_sha256="a" * 64,
    )
    (root / intent.prepared_path).write_bytes(b"inventory")
    ledger.create_intent(intent, expected_version=0, now_ms=10)
    return intent


def _unknown(ledger: IntentRepository, intent: OperationIntent) -> Any:
    attempt = ledger.start_attempt(intent.operation_id, expected_version=1, now_ms=20)
    return ledger.finish_attempt(
        attempt.id, result="UNKNOWN", error="publisher timeout", expected_version=2, now_ms=30
    )


def test_present_effect_completes_only_after_independent_verification(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    # Keep the first RED an assertion failure, not a module collection error.
    assert importlib.util.find_spec("jarvis.reconciler") is not None
    from jarvis.reconciler import DRAFT_STATUS, RUNTIME_ENABLED, Reconciler

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        ledger = _ledger(worker)
        intent = _running(ledger, tmp_path)
        _unknown(ledger, intent)
        target = tmp_path / intent.target_path
        target.write_bytes(b"inventory")
        before = target.stat()
        reconciler = Reconciler(
            connection, repository=ledger.repository, root=tmp_path, draft_fixture=True
        )
        assert reconciler.unknown_intents() == (intent,)
        result = reconciler.reconcile(intent.operation_id, now_ms=40)
        assert result.status == "COMPLETED"
        assert result.observation == "EXPECTED_EFFECT_PRESENT"
        assert connection.execute("SELECT state FROM operation_intents").fetchone() == ("VERIFIED",)
        assert connection.execute("SELECT state,version FROM task_steps").fetchone() == (
            "COMPLETED",
            5,
        )
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (1,)
        assert target.read_bytes() == b"inventory"
        assert target.stat().st_mtime_ns == before.st_mtime_ns
        assert reconciler.unknown_intents() == ()
        assert DRAFT_STATUS == "DRAFT_PENDING_DP" and RUNTIME_ENABLED is False


def test_absent_effect_returns_safe_to_retry_without_starting_an_attempt(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.reconciler import Reconciler

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        ledger = _ledger(worker)
        intent = _running(ledger, tmp_path)
        original = _unknown(ledger, intent)
        reconciler = Reconciler(
            connection, repository=ledger.repository, root=tmp_path, draft_fixture=True
        )
        report = reconciler.reconcile(intent.operation_id, now_ms=40)
        assert report.status == "SAFE_TO_RETRY" and report.observation == "TARGET_ABSENT"
        assert connection.execute("SELECT state FROM task_steps").fetchone() == ("RETRY_WAIT",)
        assert connection.execute("SELECT state FROM operation_intents").fetchone() == (
            "NEEDS_RECONCILIATION",
        )
        assert ledger.get_attempt(original.id) == original
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (1,)
        assert reconciler.unknown_intents() == ()
        assert not (tmp_path / intent.target_path).exists()


@pytest.mark.parametrize(
    "case", ["conflict", "missing_prepared", "bad_prepared", "symlink", "missing_parent"]
)
def test_weak_evidence_blocks_without_overwriting(
    connection: sqlite3.Connection, tmp_path: Path, case: str
) -> None:
    from jarvis.reconciler import Reconciler

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        ledger = _ledger(worker)
        intent = _running(ledger, tmp_path)
        _unknown(ledger, intent)
        target = tmp_path / intent.target_path
        if case == "conflict":
            target.write_bytes(b"another operation")
        elif case == "missing_prepared":
            (tmp_path / intent.prepared_path).unlink()
        elif case == "bad_prepared":
            (tmp_path / intent.prepared_path).write_bytes(b"damaged")
        elif case == "symlink":
            target.symlink_to(tmp_path / intent.prepared_path)
        else:
            target.parent.rmdir()
        report = Reconciler(
            connection, repository=ledger.repository, root=tmp_path, draft_fixture=True
        ).reconcile(intent.operation_id, now_ms=40)
        assert report.status == "BLOCKED"
        assert connection.execute("SELECT state FROM task_steps").fetchone() == ("BLOCKED",)
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (1,)
        if case == "conflict":
            assert target.read_bytes() == b"another operation"


@pytest.mark.parametrize("denied", ["effects_settled", "retry_policy_proved", "retry_permitted"])
def test_absence_without_safety_proofs_is_not_retryable(
    connection: sqlite3.Connection, tmp_path: Path, denied: str
) -> None:
    from jarvis.reconciler import Reconciler

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        ledger = _ledger(worker, {denied})
        intent = _running(ledger, tmp_path)
        _unknown(ledger, intent)
        result = Reconciler(
            connection, repository=ledger.repository, root=tmp_path, draft_fixture=True
        ).reconcile(intent.operation_id, now_ms=40)
        assert result.status == "BLOCKED"


def test_present_effect_does_not_bypass_independent_verification(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.reconciler import Reconciler

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        ledger = _ledger(worker, {"verification_passed"})
        intent = _running(ledger, tmp_path)
        _unknown(ledger, intent)
        (tmp_path / intent.target_path).write_bytes(b"inventory")
        reconciler = Reconciler(
            connection, repository=ledger.repository, root=tmp_path, draft_fixture=True
        )
        result = reconciler.reconcile(intent.operation_id, now_ms=40)
        assert result.status == "NEEDS_VERIFICATION"
        assert result.observation == "EXPECTED_EFFECT_PRESENT"
        assert connection.execute("SELECT state FROM task_steps").fetchone() == ("VERIFYING",)
        assert connection.execute("SELECT state FROM operation_intents").fetchone() == (
            "NEEDS_RECONCILIATION",
        )
        assert reconciler.unknown_intents() == (intent,)


def test_recovery_enumerates_and_reconciles_old_worker_tasks(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.reconciler import Reconciler, RecoveryManager

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as first:
        ledger = _ledger(first)
        absent = _running(ledger, tmp_path, "1")
        _unknown(ledger, absent)
        present = _running(ledger, tmp_path, "2")
        _unknown(ledger, present)
        (tmp_path / present.target_path).write_bytes(b"inventory")
        blocked = _running(ledger, tmp_path, "3")
        _unknown(ledger, blocked)
        (tmp_path / blocked.target_path).write_bytes(b"conflict")
        ledger.repository.create_task(
            task_id="terminal",
            request_key="terminal",
            request_digest="c" * 64,
            task_kind="inventory.generate",
            now_ms=1,
        )
        connection.execute("UPDATE tasks SET state='SUCCEEDED' WHERE id='terminal'")
        connection.commit()
    with Worker(connection, project_root=tmp_path, draft_fixture=True) as successor:
        ledger = _ledger(successor)
        reconciler = Reconciler(
            connection, repository=ledger.repository, root=tmp_path, draft_fixture=True
        )
        pending = RecoveryManager(successor, reconciler).recover_tasks(now_ms=50)
        assert [task["id"] for task in pending] == ["t1", "t2"]
        assert {task["id"] for task in successor.recover_tasks()} == {"t1", "t2", "t3"}
        assert connection.execute("SELECT state FROM tasks WHERE id='t1'").fetchone() == (
            "RUNNING",
        )
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (3,)


def test_successor_closes_older_active_attempt_as_unknown_before_reconciling(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.reconciler import Reconciler, RecoveryManager

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as first:
        ledger = _ledger(first)
        intent = _running(ledger, tmp_path)
        attempt = ledger.start_attempt(intent.operation_id, expected_version=1, now_ms=20)
    with Worker(connection, project_root=tmp_path, draft_fixture=True) as successor:
        ledger = _ledger(successor)
        reconciler = Reconciler(
            connection, repository=ledger.repository, root=tmp_path, draft_fixture=True
        )
        assert [
            row["id"] for row in RecoveryManager(successor, reconciler).recover_tasks(now_ms=40)
        ] == ["t1"]
        recovered = ledger.get_attempt(attempt.id)
        assert recovered.worker_epoch == attempt.worker_epoch
        assert recovered.result == "UNKNOWN" and recovered.finished_at == 40
        assert recovered.worker_epoch < successor.epoch
        assert connection.execute("SELECT state FROM task_steps").fetchone() == ("RETRY_WAIT",)


@pytest.mark.parametrize("case", ["current", "future", "old_not_settled"])
def test_unsettled_active_attempt_never_gets_takeover_permission(
    connection: sqlite3.Connection, tmp_path: Path, case: str
) -> None:
    from jarvis.reconciler import Reconciler, RecoveryManager

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        ledger = _ledger(worker, {"old_worker_effects_settled"})
        intent = _running(ledger, tmp_path)
        attempt = ledger.start_attempt(intent.operation_id, expected_version=1, now_ms=20)
        epoch = worker.epoch + {"current": 0, "future": 1, "old_not_settled": -1}[case]
        connection.execute("UPDATE attempts SET worker_epoch=?", (epoch,))
        connection.commit()
        reconciler = Reconciler(
            connection, repository=ledger.repository, root=tmp_path, draft_fixture=True
        )
        assert RecoveryManager(worker, reconciler).recover_tasks(now_ms=40) == ()
        assert ledger.get_attempt(attempt.id).finished_at is None
        assert connection.execute("SELECT state FROM task_steps").fetchone() == ("EXECUTING",)


@pytest.mark.parametrize("action", ["scan", "reconcile", "recover"])
def test_stale_global_epoch_refuses_all_recovery_operations(
    connection: sqlite3.Connection, tmp_path: Path, action: str
) -> None:
    from jarvis.reconciler import Reconciler, RecoveryManager
    from jarvis.repository import StaleEpochError

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        ledger = _ledger(worker)
        intent = _running(ledger, tmp_path)
        _unknown(ledger, intent)
        reconciler = Reconciler(
            connection, repository=ledger.repository, root=tmp_path, draft_fixture=True
        )
        connection.execute("UPDATE system_control SET worker_epoch=worker_epoch+1")
        connection.commit()
        before = connection.execute("SELECT COUNT(*) FROM events").fetchone()
        with pytest.raises(StaleEpochError):
            if action == "scan":
                reconciler.unknown_intents()
            elif action == "reconcile":
                reconciler.reconcile(intent.operation_id, now_ms=40)
            else:
                RecoveryManager(worker, reconciler).recover_tasks(now_ms=40)
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == before
        assert connection.execute("SELECT state FROM task_steps").fetchone() == (
            "NEEDS_RECONCILIATION",
        )


@pytest.mark.parametrize("control", ["cancel", "stop"])
def test_cancel_or_stop_disallows_takeover_but_still_reconciles_existing_effects(
    connection: sqlite3.Connection, tmp_path: Path, control: str
) -> None:
    from jarvis.reconciler import Reconciler, RecoveryManager

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        ledger = _ledger(worker)
        intent = _running(ledger, tmp_path)
        _unknown(ledger, intent)
        if control == "cancel":
            connection.execute("UPDATE tasks SET cancel_requested_at_ms=31")
        else:
            connection.execute("UPDATE system_control SET emergency_stop=1")
        connection.commit()
        reconciler = Reconciler(
            connection, repository=ledger.repository, root=tmp_path, draft_fixture=True
        )
        assert RecoveryManager(worker, reconciler).recover_tasks(now_ms=40) == ()
        assert connection.execute("SELECT state FROM task_steps").fetchone() == ("BLOCKED",)


def test_prior_retry_decision_is_rechecked_before_returning_takeover_list(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.reconciler import Reconciler, RecoveryManager

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        ledger = _ledger(worker)
        intent = _running(ledger, tmp_path)
        _unknown(ledger, intent)
        reconciler = Reconciler(
            connection, repository=ledger.repository, root=tmp_path, draft_fixture=True
        )
        manager = RecoveryManager(worker, reconciler)
        assert len(manager.recover_tasks(now_ms=40)) == 1
        (tmp_path / intent.target_path).write_bytes(b"a late effect")
        assert manager.recover_tasks(now_ms=50) == ()
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (1,)


def test_reconciliation_event_failure_rolls_back_decision_and_step(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.reconciler import Reconciler

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        ledger = _ledger(worker)
        intent = _running(ledger, tmp_path)
        _unknown(ledger, intent)
        connection.execute(
            "CREATE TRIGGER fail_reconcile BEFORE INSERT ON events WHEN "
            "NEW.event_type='draft.intent.reconciled' BEGIN "
            "SELECT RAISE(ABORT,'injected event failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected event failure"):
            Reconciler(
                connection, repository=ledger.repository, root=tmp_path, draft_fixture=True
            ).reconcile(intent.operation_id, now_ms=40)
        assert connection.execute("SELECT state,version FROM task_steps").fetchone() == (
            "NEEDS_RECONCILIATION",
            3,
        )
        assert not connection.in_transaction


def test_default_domain_adapter_never_grants_retry(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.reconciler import Reconciler

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        ledger = _ledger(worker)
        intent = _running(ledger, tmp_path)
        _unknown(ledger, intent)
        denied_repository = worker.execute(lambda repository: repository)
        with pytest.raises(GuardRejectedError, match="guard denied or unknown"):
            Reconciler(
                connection, repository=denied_repository, root=tmp_path, draft_fixture=True
            ).reconcile(intent.operation_id, now_ms=40)
        assert connection.execute("SELECT state,version FROM task_steps").fetchone() == (
            "NEEDS_RECONCILIATION",
            3,
        )
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (1,)


def test_explicit_fixture_opt_in_is_required(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.reconciler import Reconciler
    from jarvis.repository import DraftOnlyError

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        with pytest.raises(DraftOnlyError):
            Reconciler(connection, repository=_ledger(worker).repository, root=tmp_path)


def test_ready_and_unstarted_running_tasks_are_recovery_candidates(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.reconciler import Reconciler, RecoveryManager

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        ledger = _ledger(worker)
        intent = _running(ledger, tmp_path)
        # A committed PREPARED intent is safe to assess, never auto-published.
        pending = _running(ledger, tmp_path, "2")
        connection.execute("UPDATE tasks SET state='READY' WHERE id=?", (pending.task_id,))
        # READY cannot invoke _retry_safe, whose parent Task must be RUNNING.
        connection.execute("DELETE FROM events WHERE task_id=?", (pending.task_id,))
        connection.execute(
            "DELETE FROM file_write_intents WHERE operation_id=?", (pending.operation_id,)
        )
        connection.execute(
            "DELETE FROM operation_intents WHERE operation_id=?", (pending.operation_id,)
        )
        connection.execute("UPDATE task_steps SET state='PENDING' WHERE id=?", (pending.step_id,))
        connection.commit()
        reconciler = Reconciler(
            connection, repository=ledger.repository, root=tmp_path, draft_fixture=True
        )
        assert [
            task["id"] for task in RecoveryManager(worker, reconciler).recover_tasks(now_ms=40)
        ] == [intent.task_id, pending.task_id]
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (0,)


def test_unresolved_intent_in_inactive_plan_still_prevents_takeover(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.reconciler import Reconciler, RecoveryManager

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        ledger = _ledger(worker)
        intent = _running(ledger, tmp_path)
        _unknown(ledger, intent)
        (tmp_path / intent.target_path).write_bytes(b"conflicting old plan effect")
        connection.execute("UPDATE task_plans SET state='SUPERSEDED' WHERE id='p1'")
        connection.execute("INSERT INTO task_plans VALUES ('p2','t1',2,'ACTIVE',?,31)", ("c" * 64,))
        connection.execute("UPDATE tasks SET active_plan_id='p2'")
        connection.execute(
            "INSERT INTO task_steps (id,task_id,plan_id,ordinal,logical_effect_id,state) "
            "VALUES ('s2','t1','p2',1,'effect2','PENDING')"
        )
        connection.commit()
        reconciler = Reconciler(
            connection, repository=ledger.repository, root=tmp_path, draft_fixture=True
        )
        assert RecoveryManager(worker, reconciler).recover_tasks(now_ms=40) == ()
