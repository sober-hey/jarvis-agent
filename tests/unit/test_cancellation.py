"""K0-014 offline DRAFT fixtures; no runtime or persistent database opt-in."""

from __future__ import annotations

import importlib.util
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis.repository import TaskRepository
from jarvis.worker import Worker

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    spec = importlib.util.spec_from_file_location(
        "k0014_schema_fixture", ROOT / "tests/contract/test_sqlite_schema_draft.py"
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


def _repository(worker: Worker, denied: set[str] | None = None) -> TaskRepository:
    binding = worker.execute(lambda repository: repository)
    return TaskRepository(
        worker.connection,
        worker_lock=binding.worker_lock,
        worker_epoch=worker.epoch,
        draft_fixture=True,
        domain_guard=lambda guard, _task: guard not in (denied or set()),
    )


def _created(repository: TaskRepository) -> None:
    repository.create_task(
        task_id="t1",
        request_key="r1",
        request_digest="a" * 64,
        task_kind="inventory.generate",
        now_ms=1,
    )


def test_unstarted_task_cancels_immediately(connection: sqlite3.Connection, tmp_path: Path) -> None:
    # First RED is a feature assertion, not a broken collection/import.
    assert importlib.util.find_spec("jarvis.cancellation") is not None
    from jarvis.cancellation import DRAFT_STATUS, RUNTIME_ENABLED, CancellationManager

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = _repository(worker)
        _created(repository)
        result = CancellationManager(repository, draft_fixture=True).request_cancel(
            "t1", expected_version=0, actor="Sir", reason="not needed", now_ms=10
        )
        assert result.status == "CANCELED"
        task = repository.get_task("t1")
        assert task is not None
        assert (task["state"], task["version"], task["cancel_requested_at_ms"]) == (
            "CANCELED",
            2,
            10,
        )
        assert task["cancel_requested_by"] == "Sir"
        assert DRAFT_STATUS == "DRAFT_PENDING_DP" and RUNTIME_ENABLED is False


def _active(repository: TaskRepository, root: Path):
    import hashlib

    from jarvis.intent import IntentRepository, OperationIntent

    _created(repository)
    db = repository.connection
    db.execute("INSERT INTO task_plans VALUES ('p1','t1',1,'ACTIVE',?,1)", ("b" * 64,))
    db.execute("UPDATE tasks SET state='RUNNING',active_plan_id='p1'")
    db.execute(
        "INSERT INTO task_steps (id,task_id,plan_id,ordinal,logical_effect_id,state) "
        "VALUES ('s1','t1','p1',1,'effect1','CLAIMED')"
    )
    db.commit()
    (root / "output").mkdir()
    (root / "prepared").mkdir()
    intent = OperationIntent(
        task_id="t1",
        logical_effect_id="effect1",
        step_id="s1",
        target_path="output/result.json",
        prepared_path="prepared/result.json",
        expected_sha256=hashlib.sha256(b"inventory").hexdigest(),
        expected_size=len(b"inventory"),
        input_snapshot_sha256="a" * 64,
    )
    (root / intent.prepared_path).write_bytes(b"inventory")
    ledger = IntentRepository(repository)
    ledger.create_intent(intent, expected_version=0, now_ms=10)
    attempt = ledger.start_attempt(intent.operation_id, expected_version=1, now_ms=20)
    return ledger, intent, attempt


def test_running_cancel_survives_manager_replacement_until_safe_point(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.cancellation import CancellationManager
    from jarvis.reconciler import Reconciler

    denied = {"safe_checkpoint"}
    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = _repository(worker, denied)
        ledger, intent, attempt = _active(repository, tmp_path)
        manager = CancellationManager(repository, draft_fixture=True)
        assert (
            manager.request_cancel(
                "t1", expected_version=0, actor="Sir", reason="cancel", now_ms=25
            ).status
            == "QUEUED"
        )
        assert ledger.get_attempt(attempt.id).state == "ACTIVE"
        # Allow the in-flight effect to complete, then independently verify it.
        (tmp_path / intent.target_path).write_bytes(b"inventory")
        ledger.finish_attempt(
            attempt.id, result="SUCCESS", error=None, expected_version=2, now_ms=30
        )
        assert (
            Reconciler(connection, repository=repository, root=tmp_path, draft_fixture=True)
            .reconcile(intent.operation_id, now_ms=35)
            .status
            == "COMPLETED"
        )
        resumed = CancellationManager(repository, draft_fixture=True)
        assert resumed.checkpoint("t1", now_ms=40).status == "QUEUED"
        denied.clear()
        assert resumed.checkpoint("t1", now_ms=45).status == "CANCELED"
        assert ledger.get_attempt(attempt.id).result == "SUCCESS"
        assert (tmp_path / intent.target_path).read_bytes() == b"inventory"
        assert resumed.checkpoint("t1", now_ms=50).status == "CANCELED"


def test_unknown_intent_cannot_be_hidden_by_synthetic_safe_point(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.cancellation import CancellationManager

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = _repository(worker)
        ledger, intent, attempt = _active(repository, tmp_path)
        ledger.finish_attempt(
            attempt.id, result="UNKNOWN", error="interrupted", expected_version=2, now_ms=30
        )
        result = CancellationManager(repository, draft_fixture=True).request_cancel(
            "t1", expected_version=0, actor="Sir", reason="cancel", now_ms=35
        )
        assert result.status == "QUEUED"
        assert ledger.get_intent(intent.operation_id) == intent
        task = repository.get_task("t1")
        assert task is not None and task["state"] == "RUNNING"


def test_lock_conflict_is_rejected_without_writes(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.cancellation import CancellationManager

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = _repository(worker)
        _created(repository)
    before = connection.total_changes
    result = CancellationManager(repository, draft_fixture=True).request_cancel(
        "t1", expected_version=0, actor="Sir", reason="cancel", now_ms=10
    )
    assert result.status == "REJECTED" and "lock" in result.reason.lower()
    assert connection.total_changes == before
    task = repository.get_task("t1")
    assert task is not None and task["cancel_requested_at_ms"] is None


def test_emergency_stop_signals_and_seals_snapshot(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    import hashlib
    import json
    import signal
    import subprocess
    import sys

    import jarvis.cancellation as cancellation

    assert hasattr(cancellation, "EmergencyStop")
    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = _repository(worker)
        _created(repository)
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; print('ready',flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            assert process.stdout is not None and process.stdout.readline().strip() == "ready"
            result = cancellation.EmergencyStop(
                repository, archive_root=tmp_path, draft_fixture=True
            ).stop(process, actor="Sir", reason="emergency", now_ms=10)
            assert process.wait(timeout=5) == -signal.SIGTERM
            assert result.signal_sent is True
            assert connection.execute("SELECT emergency_stop FROM system_control").fetchone() == (
                1,
            )
            marker = json.loads(result.marker_path.read_text())
            assert marker["worker_pid"] == process.pid and marker["actor"] == "Sir"
            snapshot = json.loads(result.snapshot_path.read_text())
            assert snapshot["tables"]["tasks"][0]["state"] == "CREATED"
            assert snapshot["tables"]["tasks"][0]["cancel_requested_at_ms"] is None
            manifest = json.loads((result.snapshot_path.parent / "manifest.json").read_text())
            assert (
                manifest["snapshot.json"]
                == hashlib.sha256(result.snapshot_path.read_bytes()).hexdigest()
            )
            assert result.snapshot_path.stat().st_mode & 0o777 == 0o400
            assert result.marker_path.stat().st_mode & 0o777 == 0o400
            assert result.snapshot_path.parent.stat().st_mode & 0o777 == 0o500
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            # Permit pytest to remove its own fixture after validating the seal.
            for archive in tmp_path.glob("emergency-stop-*"):
                archive.chmod(0o700)


def test_interrupted_cancel_intent_is_visible_to_successor_reconciler(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.cancellation import CancellationManager
    from jarvis.reconciler import Reconciler, RecoveryManager

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as first:
        repository = _repository(first)
        _ledger, intent, attempt = _active(repository, tmp_path)
        original_epoch = first.epoch
        assert (
            CancellationManager(repository, draft_fixture=True)
            .request_cancel("t1", expected_version=0, actor="Sir", reason="cancel", now_ms=25)
            .status
            == "QUEUED"
        )
    # New epoch, same fixture DB: recovery ANALYSIS, never a restart grant.
    with Worker(connection, project_root=tmp_path, draft_fixture=True) as successor:
        repository = _repository(successor)
        reconciler = Reconciler(
            connection, repository=repository, root=tmp_path, draft_fixture=True
        )
        assert reconciler.unknown_intents() == (intent,)
        assert RecoveryManager(successor, reconciler).recover_tasks(now_ms=40) == ()
        recovered = reconciler.intents.get_attempt(attempt.id)
        assert recovered.result == "UNKNOWN" and recovered.worker_epoch == original_epoch
        assert successor.epoch > recovered.worker_epoch
        task = repository.get_task("t1")
        assert task is not None and task["cancel_requested_at_ms"] == 25
        assert task["state"] == "RUNNING"  # unknown outcome not falsely declared canceled
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (1,)
        assert reconciler.intents.get_intent(intent.operation_id) == intent


@pytest.mark.parametrize("action", ["request", "checkpoint"])
def test_stale_epoch_cannot_accept_cancel_or_assess_a_checkpoint(
    connection: sqlite3.Connection, tmp_path: Path, action: str
) -> None:
    from jarvis.cancellation import CancellationManager
    from jarvis.repository import StaleEpochError

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = _repository(worker)
        _created(repository)
        manager = CancellationManager(repository, draft_fixture=True)
        connection.execute("UPDATE system_control SET worker_epoch=worker_epoch+1")
        connection.commit()
        before = connection.total_changes
        with pytest.raises(StaleEpochError):
            if action == "request":
                manager.request_cancel(
                    "t1", expected_version=0, actor="Sir", reason="cancel", now_ms=10
                )
            else:
                manager.checkpoint("t1", now_ms=10)
        assert connection.total_changes == before


def test_emergency_stop_still_archives_without_executor_lock(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    import json
    import subprocess
    import sys

    from jarvis.cancellation import EmergencyStop

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = _repository(worker)
        _created(repository)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; print('ready',flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None and process.stdout.readline().strip() == "ready"
        result = EmergencyStop(repository, archive_root=tmp_path, draft_fixture=True).stop(
            process, actor="Sir", reason="force stop", now_ms=30
        )
        assert process.wait(timeout=5) < 0
        assert result.signal_sent is True
        metadata = json.loads(result.snapshot_path.read_text())["metadata"]
        assert metadata["database_stop_persisted"] is False
        assert "lock" in metadata["database_stop_error"].lower()
        assert result.marker_path.is_file()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        for archive in tmp_path.glob("emergency-stop-*"):
            archive.chmod(0o700)


@pytest.mark.parametrize(
    "state",
    ["CREATED", "READY", "RUNNING", "VERIFYING", "NEEDS_RECONCILIATION", "RETRY_WAIT", "BLOCKED"],
)
def test_all_contract_cancel_edges_preserve_history(
    connection: sqlite3.Connection, tmp_path: Path, state: str
) -> None:
    from jarvis.cancellation import CancellationManager

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = _repository(worker)
        _created(repository)
        connection.execute("UPDATE tasks SET state=?", (state,))
        connection.commit()
        manager = CancellationManager(repository, draft_fixture=True)
        assert (
            manager.request_cancel(
                "t1", expected_version=0, actor="Sir", reason="cancel", now_ms=10
            ).status
            == "CANCELED"
        )
        edge = connection.execute(
            "SELECT from_state,to_state,from_version,to_version FROM events "
            "WHERE event_type='draft.task.transition'"
        ).fetchone()
        assert edge == (state, "CANCELED", 1, 2)
        before = connection.total_changes
        assert manager.checkpoint("t1", now_ms=20).status == "CANCELED"
        assert connection.total_changes == before


def test_default_adapter_queues_instead_of_granting_safe_point(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.cancellation import CancellationManager

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = worker.execute(lambda repository: repository)
        _created(repository)
        result = CancellationManager(repository, draft_fixture=True).request_cancel(
            "t1", expected_version=0, actor="Sir", reason="cancel", now_ms=10
        )
        assert result.status == "QUEUED"
        task = repository.get_task("t1")
        assert task is not None and task["state"] == "CREATED"
        assert task["cancel_requested_at_ms"] == 10


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CANCELED"])
def test_terminal_cancel_is_rejected_without_mutating_history(
    connection: sqlite3.Connection, tmp_path: Path, state: str
) -> None:
    from jarvis.cancellation import CancellationManager

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = _repository(worker)
        _created(repository)
        connection.execute("UPDATE tasks SET state=?", (state,))
        connection.commit()
        before = connection.total_changes
        assert (
            CancellationManager(repository, draft_fixture=True)
            .request_cancel("t1", expected_version=0, actor="Sir", reason="cancel", now_ms=10)
            .status
            == "REJECTED"
        )
        assert connection.total_changes == before


def test_draft_opt_in_is_required(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from jarvis.cancellation import CancellationManager, EmergencyStop
    from jarvis.repository import DraftOnlyError

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = _repository(worker)
        with pytest.raises(DraftOnlyError):
            CancellationManager(repository)
        with pytest.raises(DraftOnlyError):
            EmergencyStop(repository, archive_root=tmp_path)


def test_emergency_signals_before_snapshot_io_failure(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal
    import subprocess
    import sys

    from jarvis.cancellation import EmergencyStop

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = _repository(worker)
        stop = EmergencyStop(repository, archive_root=tmp_path, draft_fixture=True)
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; print('ready',flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            assert process.stdout is not None and process.stdout.readline().strip() == "ready"

            def fail_seal(*_args: object) -> str:
                raise OSError("injected disk failure")

            monkeypatch.setattr(EmergencyStop, "_seal", staticmethod(fail_seal))
            with pytest.raises(OSError, match="injected disk failure"):
                stop.stop(process, actor="Sir", reason="stop", now_ms=10)
            assert process.wait(timeout=5) == -signal.SIGTERM
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()


def test_emergency_database_write_failure_is_exposed_and_snapshot_is_kept(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    import json
    import subprocess
    import sys

    from jarvis.cancellation import EmergencyStop

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = _repository(worker)
        connection.execute(
            "CREATE TRIGGER deny_stop BEFORE UPDATE OF emergency_stop ON system_control "
            "BEGIN SELECT RAISE(ABORT,'injected stop failure'); END"
        )
        process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        process.wait(timeout=5)
        try:
            result = EmergencyStop(repository, archive_root=tmp_path, draft_fixture=True).stop(
                process, actor="Sir", reason="stop", now_ms=10
            )
            assert result.signal_sent is False
            assert result.database_stop_persisted is False
            assert result.database_stop_error == "injected stop failure"
            assert result.snapshot_path.is_file()
            metadata = json.loads(result.snapshot_path.read_text())["metadata"]
            assert metadata["database_stop_error"] == "injected stop failure"
            assert not connection.in_transaction
        finally:
            for archive in tmp_path.glob("emergency-stop-*"):
                archive.chmod(0o700)


def test_real_worker_sigterm_releases_flock_and_successor_analyzes_cancelled_intent(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    import base64
    import json
    import os
    import select
    import signal
    import subprocess
    import sys

    from jarvis.cancellation import CancellationManager, EmergencyStop
    from jarvis.reconciler import Reconciler, RecoveryManager
    from jarvis.repository import WorkerLock

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as first:
        repository = _repository(first)
        _ledger, intent, attempt = _active(repository, tmp_path)
        manager = CancellationManager(repository, draft_fixture=True)
        assert (
            manager.request_cancel(
                "t1", expected_version=0, actor="Sir", reason="cancel", now_ms=25
            ).status
            == "QUEUED"
        )
    # Serialized IN-MEMORY fixture copy only; no shared or persistent runtime DB.
    # Child genuinely acquires Worker's fixed lifetime flock before TERM dispatch.
    script = """
import base64, sqlite3, sys
from pathlib import Path
from jarvis.worker import Worker
connection=sqlite3.connect(':memory:')
connection.deserialize(base64.b64decode(sys.stdin.buffer.readline()))
connection.execute('PRAGMA foreign_keys=ON')
with Worker(connection, project_root=Path(sys.argv[1]), draft_fixture=True) as worker:
    print(worker.epoch, flush=True)
    import time
    time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        start_new_session=True,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(base64.b64encode(connection.serialize()) + b"\n")
        process.stdin.close()
        assert select.select([process.stdout], [], [], 5)[0], "Worker failed to become ready"
        assert int(process.stdout.readline().strip()) == repository.worker_epoch + 1
        with pytest.raises(BlockingIOError):
            with WorkerLock(tmp_path / ".jarvis-worker.lock"):
                pytest.fail("real child did not retain the Worker flock")
        result = EmergencyStop(repository, archive_root=tmp_path, draft_fixture=True).stop(
            process, actor="Sir", reason="emergency", now_ms=30
        )
        assert process.wait(timeout=5) == -signal.SIGTERM
        snapshot = json.loads(result.snapshot_path.read_text())
        assert snapshot["tables"]["attempts"][0]["state"] == "ACTIVE"
        assert snapshot["tables"]["operation_intents"][0]["operation_key"] == intent.operation_key
        with Worker(connection, project_root=tmp_path, draft_fixture=True) as successor:
            reconciler = Reconciler(
                connection, repository=_repository(successor), root=tmp_path, draft_fixture=True
            )
            assert RecoveryManager(successor, reconciler).recover_tasks(now_ms=40) == ()
            assert reconciler.intents.get_attempt(attempt.id).result == "UNKNOWN"
            assert reconciler.intents.get_intent(intent.operation_id) == intent
            # Failed-closed recovery may BLOCK the Intent after recording UNKNOWN;
            # its identity/evidence survive, not necessarily its discovery filter.
            assert connection.execute(
                "SELECT state FROM operation_intents WHERE operation_id=?", (intent.operation_id,)
            ).fetchone() == ("BLOCKED",)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stdin is not None:
            process.stdin.close()
        for archive in tmp_path.glob("emergency-stop-*"):
            archive.chmod(0o700)


def test_stale_version_cancel_cannot_replace_existing_request(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.cancellation import CancellationManager
    from jarvis.repository import CASConflictError

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = worker.execute(lambda repository: repository)
        _created(repository)
        manager = CancellationManager(repository, draft_fixture=True)
        assert (
            manager.request_cancel(
                "t1", expected_version=0, actor="Sir", reason="original", now_ms=10
            ).status
            == "QUEUED"
        )
        before = connection.total_changes
        with pytest.raises(CASConflictError):
            manager.request_cancel(
                "t1", expected_version=0, actor="other", reason="replacement", now_ms=20
            )
        assert connection.total_changes == before
        assert (
            manager.request_cancel(
                "t1", expected_version=1, actor="other", reason="replacement", now_ms=20
            ).status
            == "QUEUED"
        )
        task = repository.get_task("t1")
        assert task is not None and task["cancel_requested_by"] == "Sir"
        assert task["cancel_reason"] == "original" and task["cancel_requested_at_ms"] == 10


def test_safe_cancel_blocks_new_attempt_and_preserves_prepared_intent(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.cancellation import CancellationManager
    from jarvis.intent import IntentRepository, OperationIntent
    from jarvis.repository import GuardRejectedError

    with Worker(connection, project_root=tmp_path, draft_fixture=True) as worker:
        repository = _repository(worker)
        _created(repository)
        connection.execute("INSERT INTO task_plans VALUES ('p1','t1',1,'ACTIVE',?,1)", ("b" * 64,))
        connection.execute("UPDATE tasks SET state='RUNNING',active_plan_id='p1'")
        connection.execute(
            "INSERT INTO task_steps (id,task_id,plan_id,ordinal,logical_effect_id,state) "
            "VALUES ('s1','t1','p1',1,'effect1','CLAIMED')"
        )
        connection.commit()
        ledger = IntentRepository(repository)
        intent = OperationIntent(
            task_id="t1",
            logical_effect_id="effect1",
            step_id="s1",
            target_path="output/result.json",
            prepared_path="prepared/result.json",
            expected_sha256="a" * 64,
            expected_size=0,
            input_snapshot_sha256="b" * 64,
        )
        ledger.create_intent(intent, expected_version=0, now_ms=5)
        assert (
            CancellationManager(repository, draft_fixture=True)
            .request_cancel("t1", expected_version=0, actor="Sir", reason="cancel", now_ms=10)
            .status
            == "CANCELED"
        )
        with pytest.raises(GuardRejectedError):
            ledger.start_attempt(intent.operation_id, expected_version=1, now_ms=20)
        assert ledger.get_intent(intent.operation_id) == intent
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (0,)
