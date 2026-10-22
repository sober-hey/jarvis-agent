"""K0-005 DRAFT_PENDING_DP: real SQLite/flock, synthetic domain evidence only."""

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from jarvis.repository import DomainGuard, TaskRepository, WorkerLock

ROOT = Path(__file__).resolve().parents[2]


def _schema_connection() -> sqlite3.Connection:
    # Reuse the K0-004 renderer, not a second hand-maintained schema/DDL.
    path = ROOT / "tests/contract/test_sqlite_schema_draft.py"
    spec = importlib.util.spec_from_file_location("k0004_repository_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = module._apply_candidate(module._load())
    connection.execute("INSERT INTO system_control VALUES (1, 0, 0, 7, 0)")
    connection.commit()
    return connection


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    db = _schema_connection()
    try:
        yield db
    finally:
        db.close()


def test_create_and_get_task(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from jarvis.repository import TaskRepository, WorkerLock

    with WorkerLock(tmp_path / "worker.lock") as lock:
        repository = TaskRepository(
            connection, worker_lock=lock, worker_epoch=7, draft_fixture=True
        )
        task = repository.create_task(
            task_id="t1",
            request_key="r1",
            request_digest="a" * 64,
            task_kind="inventory.generate",
            now_ms=10,
        )
        assert task["state"] == "CREATED"
        assert task["version"] == 0
        assert repository.get_task("t1") == task
        assert repository.get_task("missing") is None
        assert connection.execute("SELECT event_type FROM events").fetchall() == [
            ("draft.task.created",)
        ]
        assert connection.in_transaction is False


def _repository(
    connection: sqlite3.Connection,
    lock: WorkerLock,
    *,
    domain_guard: DomainGuard | None = None,
) -> TaskRepository:
    contract = yaml.safe_load((ROOT / "contracts/task_transitions.yaml").read_text())
    guards = {(edge["from"], edge["to"]): edge["guards"] for edge in contract["transitions"]}
    return TaskRepository(
        connection,
        worker_lock=lock,
        worker_epoch=7,
        draft_fixture=True,
        transition_guards=guards,
        domain_guard=domain_guard,
    )


def _allow_fixture_guard(_name: str, _task: Mapping[str, Any]) -> bool:
    """Synthetic DRAFT_PENDING_DP evidence, not domain approval."""
    return True


def _create(repository: TaskRepository) -> dict[str, Any]:
    return repository.create_task(
        task_id="t1",
        request_key="r1",
        request_digest="a" * 64,
        task_kind="inventory.generate",
        now_ms=10,
    )


def _ready_plan(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO task_plans VALUES ('p1', 't1', 1, 'ACTIVE', ?, 10)", ("b" * 64,)
    )
    connection.execute("UPDATE tasks SET active_plan_id = 'p1' WHERE id = 't1'")
    connection.commit()


def test_transition_and_matching_event(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from jarvis.repository import WorkerLock

    with WorkerLock(tmp_path / "worker.lock") as lock:
        repository = _repository(connection, lock, domain_guard=_allow_fixture_guard)
        _create(repository)
        _ready_plan(connection)
        task = repository.transition_task_state(
            "t1",
            from_state="CREATED",
            to_state="READY",
            expected_version=0,
            now_ms=20,
        )
        assert (task["state"], task["version"]) == ("READY", 1)
        assert connection.execute(
            "SELECT from_state, to_state, from_version, to_version FROM events "
            "WHERE event_type = 'draft.task.transition'"
        ).fetchall() == [("CREATED", "READY", 0, 1)]


@pytest.mark.parametrize("version", [1, -1, True])
def test_cas_mismatch_rejected(
    connection: sqlite3.Connection,
    tmp_path: Path,
    version: int,
) -> None:
    from jarvis.repository import CASConflictError, WorkerLock

    with WorkerLock(tmp_path / "worker.lock") as lock:
        repository = _repository(connection, lock, domain_guard=_allow_fixture_guard)
        _create(repository)
        _ready_plan(connection)
        with pytest.raises((CASConflictError, ValueError)):
            repository.transition_task_state(
                "t1",
                from_state="CREATED",
                to_state="READY",
                expected_version=version,
                now_ms=20,
            )
        task = repository.get_task("t1")
        assert task is not None and task["version"] == 0
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)


def test_event_failure_rolls_back_state(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from jarvis.repository import WorkerLock

    with WorkerLock(tmp_path / "worker.lock") as lock:
        repository = _repository(connection, lock, domain_guard=_allow_fixture_guard)
        before = _create(repository)
        _ready_plan(connection)
        before = repository.get_task("t1")
        connection.execute(
            "CREATE TRIGGER fail_event BEFORE INSERT ON events "
            "WHEN NEW.event_type = 'draft.task.transition' "
            "BEGIN SELECT RAISE(ABORT, 'injected event failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected event"):
            repository.transition_task_state(
                "t1",
                from_state="CREATED",
                to_state="READY",
                expected_version=0,
                now_ms=20,
            )
        assert repository.get_task("t1") == before
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)
        assert not connection.in_transaction


def test_stale_epoch_rejects_all_writes(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from jarvis.repository import StaleEpochError, WorkerLock

    with WorkerLock(tmp_path / "worker.lock") as lock:
        repository = _repository(connection, lock, domain_guard=_allow_fixture_guard)
        before = _create(repository)
        connection.execute("UPDATE system_control SET worker_epoch = 8")
        connection.commit()
        with pytest.raises(StaleEpochError):
            repository.append_event("t1", "draft.audit.note", {}, now_ms=20)
        with pytest.raises(StaleEpochError):
            repository.create_task(
                task_id="t2",
                request_key="r2",
                request_digest="c" * 64,
                task_kind="inventory.generate",
                now_ms=20,
            )
        assert repository.get_task("t1") == before
        assert repository.get_task("t2") is None
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)


def test_unknown_domain_guard_denies(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from jarvis.repository import GuardRejectedError, WorkerLock

    with WorkerLock(tmp_path / "worker.lock") as lock:
        repository = _repository(connection, lock)
        _create(repository)
        _ready_plan(connection)
        with pytest.raises(GuardRejectedError):
            repository.transition_task_state(
                "t1",
                from_state="CREATED",
                to_state="READY",
                expected_version=0,
                now_ms=20,
            )
        task = repository.get_task("t1")
        assert task is not None and task["state"] == "CREATED"


@pytest.mark.parametrize(
    ("started", "unsafe", "known", "expected"),
    [
        (False, False, True, "STOP_NOW"),
        (True, False, True, "STOP_NOW"),
        (True, True, True, "FINISH_IN_FLIGHT"),
        (True, True, False, "RECONCILE_FIRST"),
        (None, True, True, "RECONCILE_FIRST"),
    ],
)
def test_cancel_policy_only_allows_safety_completion(
    started: object,
    unsafe: object,
    known: object,
    expected: str,
) -> None:
    from jarvis.repository import cancellation_disposition

    assert (
        cancellation_disposition(
            publish_started=started,
            interruption_affects_system=unsafe,
            effects_known=known,
        )
        == expected
    )


def test_edge_projection_and_task_step_version_requirements() -> None:
    from jarvis.repository import TASK_EDGES, TASK_GUARDS

    shared = {
        "source_state_matches",
        "version_matches",
        "target_state_matches",
        "one_row_cas",
        "event_same_transaction",
        "fenced_commit",
    }
    for kind in ("task", "step"):
        contract = yaml.safe_load((ROOT / f"contracts/{kind}_transitions.yaml").read_text())
        assert all(shared <= set(edge["guards"]) for edge in contract["transitions"])
        if kind == "task":
            assert TASK_EDGES == {(edge["from"], edge["to"]) for edge in contract["transitions"]}
            assert TASK_GUARDS == {
                (edge["from"], edge["to"]): tuple(edge["guards"])
                for edge in contract["transitions"]
            }


@pytest.mark.parametrize("failure", ["after_insert", "post_guard", "epoch_changed"])
def test_partial_transaction_failures_leave_no_writes(
    connection: sqlite3.Connection,
    tmp_path: Path,
    failure: str,
) -> None:
    from jarvis.repository import GuardRejectedError, StaleEpochError

    with WorkerLock(tmp_path / "worker.lock") as lock:

        def guard(name: str, task: Mapping[str, Any]) -> bool:
            if failure == "post_guard" and name == "relationships_consistent":
                return task["state"] == "CREATED"
            return True

        repository = _repository(connection, lock, domain_guard=guard)
        if failure == "after_insert":
            connection.execute(
                "CREATE TRIGGER reject_create BEFORE INSERT ON events "
                "BEGIN SELECT RAISE(ABORT, 'partial insert'); END"
            )
            with pytest.raises(sqlite3.IntegrityError, match="partial insert"):
                _create(repository)
            assert repository.get_task("t1") is None
            assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
        else:
            _create(repository)
            _ready_plan(connection)
            before = repository.get_task("t1")
            if failure == "epoch_changed":
                connection.execute(
                    "CREATE TRIGGER change_epoch AFTER INSERT ON events "
                    "WHEN NEW.event_type = 'draft.task.transition' "
                    "BEGIN UPDATE system_control SET worker_epoch = 8; END"
                )
            with pytest.raises((GuardRejectedError, StaleEpochError)):
                repository.transition_task_state(
                    "t1",
                    from_state="CREATED",
                    to_state="READY",
                    expected_version=0,
                    now_ms=20,
                )
            assert repository.get_task("t1") == before
            assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)
        assert connection.execute("SELECT worker_epoch FROM system_control").fetchone() == (7,)
        assert not connection.in_transaction


def test_cancel_request_prevents_start_and_is_not_completion(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    from jarvis.repository import GuardRejectedError

    with WorkerLock(tmp_path / "worker.lock") as lock:
        repository = _repository(connection, lock, domain_guard=_allow_fixture_guard)
        _create(repository)
        _ready_plan(connection)
        cancelled = repository.request_cancel(
            "t1",
            expected_version=0,
            actor="Sir",
            reason="stop",
            now_ms=20,
        )
        assert (cancelled["state"], cancelled["version"]) == ("CREATED", 1)
        with pytest.raises(GuardRejectedError, match="cancel_absent"):
            repository.transition_task_state(
                "t1",
                from_state="CREATED",
                to_state="READY",
                expected_version=1,
                now_ms=30,
            )
        assert (
            repository.request_cancel(
                "t1",
                expected_version=1,
                actor="Sir",
                reason="again",
                now_ms=30,
            )
            == cancelled
        )
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (2,)


def test_lock_required_and_exclusive(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from jarvis.repository import LockNotHeldError

    path = tmp_path / "worker.lock"
    lock = WorkerLock(path)
    repository = _repository(connection, lock)
    with pytest.raises(LockNotHeldError):
        _create(repository)
    with lock:
        with pytest.raises(BlockingIOError):
            with WorkerLock(path):
                pytest.fail("second lock must not enter")
        _create(repository)
    with pytest.raises(LockNotHeldError):
        repository.append_event("t1", "draft.audit", {}, now_ms=20)


@pytest.mark.parametrize("cause", ["missing_epoch", "foreign_keys_off", "caller_transaction"])
def test_connection_prerequisites_fail_closed(
    connection: sqlite3.Connection,
    tmp_path: Path,
    cause: str,
) -> None:
    from jarvis.repository import RepositoryError

    with WorkerLock(tmp_path / "worker.lock") as lock:
        repository = _repository(connection, lock)
        if cause == "missing_epoch":
            connection.execute("DELETE FROM system_control")
            connection.commit()
        elif cause == "foreign_keys_off":
            connection.execute("PRAGMA foreign_keys = OFF")
        else:
            connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(RepositoryError):
            _create(repository)
        assert repository.get_task("t1") is None
        connection.rollback()


def test_runtime_requires_opt_in_and_refuses_file_database(tmp_path: Path) -> None:
    from jarvis.repository import DraftOnlyError

    with sqlite3.connect(":memory:") as db:
        with pytest.raises(DraftOnlyError):
            TaskRepository(db, worker_lock=WorkerLock(tmp_path / "lock"), worker_epoch=0)
    # File contents never rendered/migrated: construction must deny immediately.
    db = sqlite3.connect(tmp_path / "denied.db")
    try:
        with pytest.raises(DraftOnlyError):
            TaskRepository(
                db, worker_lock=WorkerLock(tmp_path / "lock"), worker_epoch=0, draft_fixture=True
            )
        assert db.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        db.close()


def test_append_audit_and_request_idempotency(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from jarvis.repository import GuardRejectedError

    with WorkerLock(tmp_path / "worker.lock") as lock:
        repository = _repository(connection, lock)
        task = _create(repository)
        assert _create(repository) == task
        with pytest.raises(GuardRejectedError):
            repository.create_task(
                task_id="other",
                request_key="r1",
                request_digest="b" * 64,
                task_kind="inventory.generate",
                now_ms=20,
            )
        sequence = repository.append_event("t1", "draft.audit", {"probe": True}, now_ms=20)
        assert connection.execute(
            "SELECT payload_json, from_version FROM events WHERE event_sequence = ?",
            (sequence,),
        ).fetchone() == ('{"probe": true}', None)
        with pytest.raises(GuardRejectedError):
            repository.append_event("t1", "draft.task.transition", {}, now_ms=30)
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (2,)


def test_stale_epoch_transition_and_new_worker_takeover(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    from jarvis.repository import StaleEpochError

    path = tmp_path / "worker.lock"
    with WorkerLock(path) as lock:
        repository = _repository(connection, lock, domain_guard=_allow_fixture_guard)
        _create(repository)
        _ready_plan(connection)
        connection.execute("UPDATE system_control SET worker_epoch = 8")
        connection.commit()
        before = repository.get_task("t1")
        with pytest.raises(StaleEpochError):
            repository.transition_task_state(
                "t1",
                from_state="CREATED",
                to_state="READY",
                expected_version=0,
                now_ms=20,
            )
        assert repository.get_task("t1") == before
    with WorkerLock(path) as new_lock:
        contract = yaml.safe_load((ROOT / "contracts/task_transitions.yaml").read_text())
        guards = {(edge["from"], edge["to"]): edge["guards"] for edge in contract["transitions"]}
        next_repository = TaskRepository(
            connection,
            worker_lock=new_lock,
            worker_epoch=8,
            draft_fixture=True,
            transition_guards=guards,
            domain_guard=_allow_fixture_guard,
        )
        updated = next_repository.transition_task_state(
            "t1",
            from_state="CREATED",
            to_state="READY",
            expected_version=0,
            now_ms=20,
        )
        assert updated["version"] == 1


@pytest.mark.parametrize(
    "source,target", [("CREATED", "SUCCEEDED"), ("CREATED", "CREATED"), ("SUCCEEDED", "READY")]
)
def test_illegal_self_and_terminal_edges_rejected(
    connection: sqlite3.Connection,
    tmp_path: Path,
    source: str,
    target: str,
) -> None:
    from jarvis.repository import GuardRejectedError

    with WorkerLock(tmp_path / "worker.lock") as lock:
        repository = _repository(connection, lock, domain_guard=_allow_fixture_guard)
        before = _create(repository)
        with pytest.raises(GuardRejectedError):
            repository.transition_task_state(
                "t1",
                from_state=source,
                to_state=target,
                expected_version=0,
                now_ms=20,
            )
        assert repository.get_task("t1") == before


def test_commit_failure_rolls_back_insert_and_event(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    class FailingCommit(sqlite3.Connection):
        def commit(self) -> None:
            raise sqlite3.OperationalError("injected commit failure")

    db = sqlite3.connect(":memory:", factory=FailingCommit)
    try:
        connection.backup(db)
        db.execute("PRAGMA foreign_keys = ON")
        with WorkerLock(tmp_path / "worker.lock") as lock:
            repository = _repository(db, lock)
            with pytest.raises(sqlite3.OperationalError, match="injected commit"):
                _create(repository)
            assert repository.get_task("t1") is None
            assert db.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
            assert not db.in_transaction
    finally:
        db.close()


@pytest.mark.parametrize("autocommit", [True, False])
def test_incompatible_autocommit_mode_rejected(tmp_path: Path, autocommit: bool) -> None:
    from jarvis.repository import RepositoryError

    db = sqlite3.connect(":memory:", autocommit=autocommit)
    try:
        with pytest.raises(RepositoryError, match="legacy transaction"):
            TaskRepository(
                db, worker_lock=WorkerLock(tmp_path / "lock"), worker_epoch=7, draft_fixture=True
            )
    finally:
        db.close()


def test_removed_guard_cannot_be_supplied_by_fixture(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    from jarvis.repository import TASK_GUARDS, GuardRejectedError

    removed: dict[tuple[str, str], tuple[str, ...]] = dict(TASK_GUARDS)
    removed[("CREATED", "READY")] = tuple(
        guard for guard in removed[("CREATED", "READY")] if guard != "active_plan_bound"
    )
    with pytest.raises(GuardRejectedError, match="guard set differs"):
        TaskRepository(
            connection,
            worker_lock=WorkerLock(tmp_path / "lock"),
            worker_epoch=7,
            draft_fixture=True,
            transition_guards=removed,
        )


def test_lock_replacement_rejected_and_fd_released(tmp_path: Path) -> None:
    from jarvis.repository import LockNotHeldError

    path = tmp_path / "worker.lock"
    lock = WorkerLock(path)
    lock.__enter__()
    path.rename(tmp_path / "original.lock")
    path.touch()
    with pytest.raises(LockNotHeldError, match="replaced"):
        lock.assert_held()
    with pytest.raises(LockNotHeldError, match="replaced"):
        lock.__exit__()
    with WorkerLock(tmp_path / "original.lock"):
        pass  # descriptor on the original inode was closed even after validation failed


def test_flock_excludes_independent_process_until_release(tmp_path: Path) -> None:
    path = tmp_path / "worker.lock"
    code = (
        "import sys; from pathlib import Path; from jarvis.repository import WorkerLock; "
        "\ntry:\n with WorkerLock(Path(sys.argv[1])): pass\n"
        "except BlockingIOError: sys.exit(23)\n"
    )

    def child() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", code, str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    with WorkerLock(path):
        blocked = child()
        assert blocked.returncode == 23, blocked.stderr
    released = child()
    assert released.returncode == 0, released.stderr


def test_cancel_request_event_failure_rolls_back_fields(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    with WorkerLock(tmp_path / "worker.lock") as lock:
        repository = _repository(connection, lock)
        before = _create(repository)
        connection.execute(
            "CREATE TRIGGER reject_cancel BEFORE INSERT ON events "
            "WHEN NEW.event_type = 'draft.task.cancel_requested' "
            "BEGIN SELECT RAISE(ABORT, 'cancel event failed'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="cancel event failed"):
            repository.request_cancel(
                "t1", expected_version=0, actor="Sir", reason="stop", now_ms=20
            )
        assert repository.get_task("t1") == before
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)


@pytest.mark.parametrize("result", [False, None, 1, "true"])
def test_nonexact_guard_evidence_denied(
    connection: sqlite3.Connection,
    tmp_path: Path,
    result: object,
) -> None:
    from jarvis.repository import GuardRejectedError

    def not_trusted(_name: str, _task: Mapping[str, Any]) -> object:
        return result

    with WorkerLock(tmp_path / "worker.lock") as lock:
        repository = _repository(connection, lock, domain_guard=not_trusted)
        _create(repository)
        _ready_plan(connection)
        with pytest.raises(GuardRejectedError):
            repository.transition_task_state(
                "t1",
                from_state="CREATED",
                to_state="READY",
                expected_version=0,
                now_ms=20,
            )
        assert connection.execute("SELECT version FROM tasks").fetchone() == (0,)


@pytest.mark.parametrize("setting", ["autocommit", "row_factory"])
def test_connection_changed_after_construction_is_rejected(
    connection: sqlite3.Connection,
    tmp_path: Path,
    setting: str,
) -> None:
    from jarvis.repository import RepositoryError

    repository = _repository(connection, WorkerLock(tmp_path / "worker.lock"))
    if setting == "autocommit":
        connection.autocommit = True
    else:
        connection.row_factory = sqlite3.Row
    try:
        with pytest.raises(RepositoryError):
            _create(repository)
        assert not connection.in_transaction
    finally:
        connection.autocommit = sqlite3.LEGACY_TRANSACTION_CONTROL
        connection.row_factory = None
    assert repository.get_task("t1") is None
