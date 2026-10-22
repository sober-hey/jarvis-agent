"""K0-011 offline DRAFT probes; no publisher or runtime database is used."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jarvis.repository import TaskRepository, WorkerLock

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Any]:
    # Render the existing K0-004 schema, including its table-name aliases.
    spec = importlib.util.spec_from_file_location(
        "k0011_schema_fixture", ROOT / "tests/contract/test_sqlite_schema_draft.py"
    )
    assert spec is not None and spec.loader is not None
    schema = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schema)
    db = schema._apply_candidate(schema._load())
    db.execute("INSERT INTO system_control VALUES (1, 0, 0, 7, 0)")
    db.commit()
    with WorkerLock(tmp_path / "worker.lock") as lock:
        repository = TaskRepository(
            db,
            worker_lock=lock,
            worker_epoch=7,
            draft_fixture=True,
            domain_guard=lambda _guard, _task: True,  # synthetic, NOT authorization
        )
        repository.create_task(
            task_id="t1",
            request_key="r1",
            request_digest="a" * 64,
            task_kind="inventory.generate",
            now_ms=1,
        )
        db.execute("INSERT INTO task_plans VALUES ('p1','t1',1,'ACTIVE',?,1)", ("b" * 64,))
        db.execute("UPDATE tasks SET state='RUNNING', active_plan_id='p1' WHERE id='t1'")
        db.execute(
            "INSERT INTO task_steps (id,task_id,plan_id,ordinal,logical_effect_id,state) "
            "VALUES ('s1','t1','p1',1,'effect1','CLAIMED')"
        )
        db.commit()
        from jarvis.intent import IntentRepository, OperationIntent

        intent = OperationIntent(
            task_id="t1",
            step_id="s1",
            logical_effect_id="effect1",
            target_path="output/inventory.json",
            prepared_path="prepared/inventory.json",
            expected_sha256=hashlib.sha256(b"inventory").hexdigest(),
            expected_size=len(b"inventory"),
            input_snapshot_sha256="a" * 64,
        )
        (tmp_path / "prepared").mkdir()
        (tmp_path / "output").mkdir()
        (tmp_path / intent.prepared_path).write_bytes(b"inventory")
        try:
            yield IntentRepository(repository), intent, tmp_path
        finally:
            db.close()


def _prepare(store: Any) -> Any:
    ledger, intent, _root = store
    ledger.create_intent(intent, expected_version=0, now_ms=10)
    return ledger.start_attempt(intent.operation_id, expected_version=1, now_ms=20)


def _unknown(store: Any) -> Any:
    ledger, _intent, _root = store
    attempt = _prepare(store)
    return ledger.finish_attempt(
        attempt.id,
        result="UNKNOWN",
        error="publisher timeout",
        now_ms=30,
        expected_version=2,
    )


def test_intent_is_committed_before_any_attempt_or_effect(store: Any) -> None:
    ledger, intent, root = store
    ledger.create_intent(intent, expected_version=0, now_ms=10)
    db = ledger.repository.connection
    assert not db.in_transaction
    assert ledger.get_intent(intent.operation_id) == intent
    assert db.execute("SELECT state,version FROM task_steps").fetchone() == ("PREPARING", 1)
    assert db.execute("SELECT COUNT(*) FROM attempts").fetchone() == (0,)
    assert not (root / intent.target_path).exists()
    attempt = ledger.start_attempt(intent.operation_id, expected_version=1, now_ms=20)
    assert not db.in_transaction
    assert attempt.started_at == 20 and attempt.finished_at is None
    assert db.execute("SELECT state FROM operation_intents").fetchone() == ("PUBLISHING",)


def test_duplicate_key_rejected_without_replacing_original(store: Any) -> None:
    from jarvis.intent import DuplicateIntentError

    ledger, intent, _root = store
    ledger.create_intent(intent, expected_version=0, now_ms=10)
    for candidate in (intent, replace(intent, target_path="output/changed.json")):
        with pytest.raises(DuplicateIntentError):
            ledger.create_intent(candidate, expected_version=1, now_ms=11)
    assert ledger.get_intent(intent.operation_id) == intent
    assert ledger.repository.connection.execute(
        "SELECT COUNT(*) FROM operation_intents"
    ).fetchone() == (1,)


@pytest.mark.parametrize("table", ["operation_intents", "file_write_intents", "events"])
def test_create_is_one_atomic_transaction(store: Any, table: str) -> None:
    ledger, intent, _root = store
    db = ledger.repository.connection
    db.execute(
        f"CREATE TRIGGER fail_intent BEFORE INSERT ON {table} "
        "BEGIN SELECT RAISE(ABORT,'injected write failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        ledger.create_intent(intent, expected_version=0, now_ms=10)
    assert db.execute("SELECT state,version FROM task_steps").fetchone() == ("CLAIMED", 0)
    assert db.execute("SELECT COUNT(*) FROM operation_intents").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM file_write_intents").fetchone() == (0,)
    assert not db.in_transaction


def test_attempt_results_are_persisted_and_immutable(store: Any) -> None:
    ledger, intent, _root = store
    finished = _unknown(store)
    assert finished.started_at == 20 and finished.finished_at == 30
    assert finished.result == "UNKNOWN" and finished.error == "publisher timeout"
    assert ledger.get_attempt(finished.id) == finished
    assert ledger.unknown_intents() == (intent,)
    from jarvis.repository import GuardRejectedError

    with pytest.raises(GuardRejectedError):
        ledger.finish_attempt(
            finished.id, result="SUCCESS", error=None, now_ms=31, expected_version=3
        )
    assert ledger.get_attempt(finished.id) == finished
    assert ledger.repository.connection.execute("PRAGMA table_info(attempts)").fetchall()
    assert "result" not in {
        row[1] for row in ledger.repository.connection.execute("PRAGMA table_info(attempts)")
    }


def test_reconcile_present_effect_then_independent_verification_completes(store: Any) -> None:
    ledger, intent, root = store
    _unknown(store)
    target = root / intent.target_path
    target.write_bytes(b"inventory")
    before = target.stat().st_mtime_ns
    report = ledger.reconcile(intent.operation_id, root=root, expected_version=3, now_ms=40)
    assert report.observation == "EXPECTED_EFFECT_PRESENT" and report.decision == "VERIFY"
    db = ledger.repository.connection
    assert db.execute("SELECT state FROM task_steps").fetchone() == ("VERIFYING",)
    # Existence/hash match is NOT independent verification or an invented COMPLETED Intent state.
    assert db.execute("SELECT state FROM operation_intents").fetchone() == ("NEEDS_RECONCILIATION",)
    ledger.complete_verified(intent.operation_id, root=root, expected_version=4, now_ms=50)
    assert db.execute("SELECT state,version FROM task_steps").fetchone() == ("COMPLETED", 5)
    assert db.execute("SELECT state FROM operation_intents").fetchone() == ("VERIFIED",)
    assert target.stat().st_mtime_ns == before
    assert ledger.unknown_intents() == ()


def test_reconcile_absent_can_retry_same_intent_only(store: Any) -> None:
    ledger, intent, root = store
    first = _unknown(store)
    report = ledger.reconcile(intent.operation_id, root=root, expected_version=3, now_ms=40)
    assert report.observation == "TARGET_ABSENT" and report.decision == "RETRY_SAFE"
    db = ledger.repository.connection
    assert db.execute("SELECT state FROM task_steps").fetchone() == ("RETRY_WAIT",)
    ledger.prepare_retry(intent.operation_id, root=root, expected_version=4, now_ms=50)
    second = ledger.start_attempt(intent.operation_id, expected_version=7, now_ms=60)
    assert second.attempt_no == first.attempt_no + 1
    assert second.operation_id == first.operation_id
    assert ledger.get_intent(intent.operation_id) == intent
    assert db.execute("SELECT COUNT(*) FROM operation_intents").fetchone() == (1,)


@pytest.mark.parametrize(
    "case",
    ["conflicting", "missing_prepared", "symlink_target", "symlink_parent", "missing_parent"],
)
def test_reconcile_never_overwrites_or_retries_on_weak_evidence(store: Any, case: str) -> None:
    ledger, intent, root = store
    _unknown(store)
    target = root / intent.target_path
    if case == "conflicting":
        target.write_bytes(b"someone else's output")
    elif case == "missing_prepared":
        (root / intent.prepared_path).unlink()
    elif case == "symlink_target":
        target.symlink_to(root / intent.prepared_path)
    elif case == "symlink_parent":
        (root / "output").rmdir()
        (root / "output").symlink_to(root / "prepared", target_is_directory=True)
    else:
        (root / "output").rmdir()
    report = ledger.reconcile(intent.operation_id, root=root, expected_version=3, now_ms=40)
    assert report.decision == "BLOCK"
    assert ledger.repository.connection.execute("SELECT state FROM task_steps").fetchone() == (
        "BLOCKED",
    )
    assert ledger.repository.connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (1,)
    if case == "conflicting":
        assert target.read_bytes() == b"someone else's output"


@pytest.mark.parametrize("mutation", ["epoch", "cancel", "stop", "active", "cas", "guard"])
def test_start_denials_cannot_be_overridden_by_fixture_boolean(store: Any, mutation: str) -> None:
    from jarvis.repository import RepositoryError

    ledger, intent, _root = store
    ledger.create_intent(intent, expected_version=0, now_ms=10)
    db = ledger.repository.connection
    if mutation == "epoch":
        db.execute("UPDATE system_control SET worker_epoch=8")
    elif mutation == "cancel":
        db.execute("UPDATE tasks SET cancel_requested_at_ms=11")
    elif mutation == "stop":
        db.execute("UPDATE system_control SET emergency_stop=1")
    elif mutation == "active":
        db.execute(
            "INSERT INTO attempts VALUES ('old','s1',?,1,7,'ACTIVE',11,NULL)",
            (intent.operation_id,),
        )
    elif mutation == "cas":
        db.execute("UPDATE task_steps SET version=5")
    else:
        ledger.repository._domain_guard = None
    db.commit()
    with pytest.raises(RepositoryError):
        ledger.start_attempt(intent.operation_id, expected_version=1, now_ms=20)
    assert db.execute("SELECT state FROM operation_intents").fetchone() == ("PREPARED",)
    assert db.execute("SELECT state FROM task_steps").fetchone() == ("PREPARING",)


def test_retry_requires_policy_not_just_target_absence(store: Any) -> None:
    ledger, intent, root = store
    _unknown(store)

    def deny_retry_policy(name: str, _task: Any) -> bool:
        return name != "retry_policy_proved"

    ledger.repository._domain_guard = deny_retry_policy
    report = ledger.reconcile(intent.operation_id, root=root, expected_version=3, now_ms=40)
    assert report.observation == "TARGET_ABSENT" and report.decision == "BLOCK"


def test_reconciliation_event_is_bound_to_original_intent(store: Any) -> None:
    ledger, intent, root = store
    _unknown(store)
    ledger.reconcile(intent.operation_id, root=root, expected_version=3, now_ms=40)
    row = ledger.repository.connection.execute(
        "SELECT step_id,payload_json FROM events WHERE event_type='draft.intent.reconciled'"
    ).fetchone()
    assert row[0] == intent.step_id
    payload = json.loads(row[1])
    assert payload["operation_id"] == intent.operation_id
    assert payload["operation_key"] == intent.operation_key
    assert payload["arguments_digest"] == intent.arguments_digest
    assert payload["target_path"] == intent.target_path
    assert payload["expected_sha256"] == intent.expected_sha256


def test_changed_prepared_or_target_after_retry_decision_denies(store: Any) -> None:
    from jarvis.repository import GuardRejectedError

    ledger, intent, root = store
    _unknown(store)
    ledger.reconcile(intent.operation_id, root=root, expected_version=3, now_ms=40)
    (root / intent.target_path).write_bytes(b"late conflict")
    with pytest.raises(GuardRejectedError):
        ledger.prepare_retry(intent.operation_id, root=root, expected_version=4, now_ms=50)
    assert ledger.repository.connection.execute("SELECT state FROM task_steps").fetchone() == (
        "RETRY_WAIT",
    )


def test_successful_attempt_still_needs_verification(store: Any) -> None:
    ledger, intent, root = store
    attempt = _prepare(store)
    (root / intent.target_path).write_bytes(b"inventory")
    result = ledger.finish_attempt(
        attempt.id, result="SUCCESS", error=None, now_ms=30, expected_version=2
    )
    assert result.result == "SUCCESS" and result.error is None
    assert ledger.repository.connection.execute("SELECT state FROM task_steps").fetchone() == (
        "VERIFYING",
    )
    ledger.complete_verified(intent.operation_id, root=root, expected_version=3, now_ms=40)
    assert ledger.repository.connection.execute("SELECT state FROM task_steps").fetchone() == (
        "COMPLETED",
    )


def test_wrong_intent_metadata_does_not_load_as_original(store: Any) -> None:
    from jarvis.repository import GuardRejectedError

    ledger, intent, _root = store
    ledger.create_intent(intent, expected_version=0, now_ms=10)
    db = ledger.repository.connection
    db.execute("UPDATE file_write_intents SET target_relpath='output/tampered.json'")
    db.commit()
    with pytest.raises(GuardRejectedError):
        ledger.get_intent(intent.operation_id)


def test_guard_subset_stays_in_sync_with_contract() -> None:
    import yaml

    from jarvis.intent import STEP_DOMAIN_GUARDS

    document = yaml.safe_load((ROOT / "contracts/step_transitions.yaml").read_text())
    shared = {
        "source_state_matches",
        "version_matches",
        "target_state_matches",
        "one_row_cas",
        "event_same_transaction",
        "relationships_consistent",
        "terminal_history_preserved",
        "fenced_commit",
    }
    edges = {(edge["from"], edge["to"]): edge for edge in document["transitions"]}
    for pair, guards in STEP_DOMAIN_GUARDS.items():
        assert tuple(g for g in edges[pair]["guards"] if g not in shared) == guards


def test_active_unknown_cannot_be_blindly_retried(store: Any) -> None:
    from jarvis.repository import GuardRejectedError

    ledger, intent, root = store
    attempt = _prepare(store)
    assert ledger.unknown_intents() == (intent,)
    with pytest.raises(GuardRejectedError):
        ledger.reconcile(intent.operation_id, root=root, expected_version=2, now_ms=40)
    assert ledger.get_attempt(attempt.id).finished_at is None
    assert ledger.repository.connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (1,)


def test_stale_worker_attempt_recovered_before_reconciliation(store: Any) -> None:
    ledger, intent, root = store
    attempt = _prepare(store)
    db = ledger.repository.connection
    db.execute("UPDATE attempts SET worker_epoch=6")
    db.commit()
    recovered = ledger.recover_unknown_attempt(attempt.id, expected_version=2, now_ms=30)
    assert recovered.result == "UNKNOWN" and recovered.worker_epoch == 6
    assert recovered.error == "worker epoch lost; actual outcome requires reconciliation"
    (root / intent.target_path).write_bytes(b"inventory")
    report = ledger.reconcile(intent.operation_id, root=root, expected_version=3, now_ms=40)
    assert report.decision == "VERIFY"
    assert db.execute("SELECT COUNT(*) FROM attempts").fetchone() == (1,)


@pytest.mark.parametrize("result", ["ERROR", "UNKNOWN"])
def test_error_is_not_proof_of_no_effect(store: Any, result: str) -> None:
    ledger, intent, root = store
    attempt = _prepare(store)
    (root / intent.target_path).write_bytes(b"inventory")
    ledger.finish_attempt(
        attempt.id, result=result, error="connection lost", expected_version=2, now_ms=30
    )
    report = ledger.reconcile(intent.operation_id, root=root, expected_version=3, now_ms=40)
    assert report.decision == "VERIFY"


@pytest.mark.parametrize("phase", ["start", "finish", "reconcile", "complete", "retry"])
def test_event_failure_rolls_back_entire_operation(store: Any, phase: str) -> None:
    ledger, intent, root = store
    db = ledger.repository.connection
    from functools import partial

    if phase == "start":
        ledger.create_intent(intent, expected_version=0, now_ms=10)
        action = partial(ledger.start_attempt, intent.operation_id, expected_version=1, now_ms=20)
    elif phase == "finish":
        attempt = _prepare(store)
        action = partial(
            ledger.finish_attempt,
            attempt.id,
            result="SUCCESS",
            error=None,
            expected_version=2,
            now_ms=30,
        )
    else:
        _unknown(store)
        if phase == "reconcile":
            action = partial(
                ledger.reconcile, intent.operation_id, root=root, expected_version=3, now_ms=40
            )
        elif phase == "retry":
            ledger.reconcile(intent.operation_id, root=root, expected_version=3, now_ms=40)
            action = partial(
                ledger.prepare_retry, intent.operation_id, root=root, expected_version=4, now_ms=50
            )
        else:
            (root / intent.target_path).write_bytes(b"inventory")
            ledger.reconcile(intent.operation_id, root=root, expected_version=3, now_ms=40)
            action = partial(
                ledger.complete_verified,
                intent.operation_id,
                root=root,
                expected_version=4,
                now_ms=50,
            )
    before = "\n".join(db.iterdump())
    db.execute(
        "CREATE TRIGGER fail_event BEFORE INSERT ON events "
        "BEGIN SELECT RAISE(ABORT,'event failed'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="event failed"):
        action()
    db.execute("DROP TRIGGER fail_event")
    assert "\n".join(db.iterdump()) == before
    assert not db.in_transaction


def test_intent_cannot_be_marked_prepared_without_commit(store: Any) -> None:
    from jarvis.repository import RepositoryError

    ledger, intent, _root = store
    ledger.create_intent(intent, expected_version=0, now_ms=10)
    db = ledger.repository.connection
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(RepositoryError):
        ledger.start_attempt(intent.operation_id, expected_version=1, now_ms=20)
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM attempts").fetchone() == (0,)


def test_verification_without_independent_guard_is_denied(store: Any) -> None:
    from jarvis.repository import GuardRejectedError

    ledger, intent, root = store
    _unknown(store)
    (root / intent.target_path).write_bytes(b"inventory")
    ledger.reconcile(intent.operation_id, root=root, expected_version=3, now_ms=40)

    def deny_verifier(name: str, _task: Any) -> bool:
        return name != "verification_passed"

    ledger.repository._domain_guard = deny_verifier
    with pytest.raises(GuardRejectedError):
        ledger.complete_verified(intent.operation_id, root=root, expected_version=4, now_ms=50)
    assert ledger.repository.connection.execute(
        "SELECT state FROM operation_intents"
    ).fetchone() == ("NEEDS_RECONCILIATION",)


@pytest.mark.parametrize(
    "malformed", ["../escape", "/absolute", "a//b", "a/./b", "a/../b", "a\x00b"]
)
def test_target_paths_are_rejected_not_normalized(store: Any, malformed: str) -> None:
    _ledger, intent, _root = store
    with pytest.raises(ValueError):
        replace(intent, target_path=malformed)


def test_matching_hardlink_effect_is_observed_without_republish(store: Any) -> None:
    import os

    ledger, intent, root = store
    _unknown(store)
    os.link(root / intent.prepared_path, root / intent.target_path)
    report = ledger.reconcile(intent.operation_id, root=root, expected_version=3, now_ms=40)
    assert report.decision == "VERIFY"
    ledger.complete_verified(intent.operation_id, root=root, expected_version=4, now_ms=50)
    assert (root / intent.prepared_path).exists()


def test_reconciliation_projection_matches_existing_schema(store: Any) -> None:
    from dataclasses import asdict

    import jsonschema

    ledger, intent, root = store
    _unknown(store)
    report = ledger.reconcile(intent.operation_id, root=root, expected_version=3, now_ms=40)
    schema = json.loads((ROOT / "contracts/reconciliation.schema.json").read_text())
    jsonschema.validate(asdict(report), schema)
    assert report.evidence["operation_key"] == intent.operation_key


def test_attempt_cannot_finish_before_start(store: Any) -> None:
    ledger, _intent, _root = store
    attempt = _prepare(store)
    with pytest.raises(ValueError):
        ledger.finish_attempt(
            attempt.id, result="SUCCESS", error=None, expected_version=2, now_ms=19
        )
    assert ledger.get_attempt(attempt.id).finished_at is None


def test_result_reloads_without_in_memory_cache(store: Any) -> None:
    from jarvis.intent import IntentRepository

    ledger, intent, _root = store
    attempt = _unknown(store)
    recovered = IntentRepository(ledger.repository)
    assert recovered.get_intent(intent.operation_id) == intent
    assert recovered.get_attempt(attempt.id) == attempt


def test_failed_commit_rolls_back_intent_and_step(store: Any) -> None:
    ledger, intent, _root = store
    db = ledger.repository.connection
    # DEFERRABLE fixture FK fails at COMMIT, after all Intent/event writes.
    db.execute(
        "CREATE TABLE commit_failure (id TEXT REFERENCES tasks(id) DEFERRABLE INITIALLY DEFERRED)"
    )
    db.execute(
        "CREATE TRIGGER inject_bad_commit AFTER INSERT ON operation_intents "
        "BEGIN INSERT INTO commit_failure VALUES ('absent-task'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        ledger.create_intent(intent, expected_version=0, now_ms=10)
    assert db.execute("SELECT COUNT(*) FROM operation_intents").fetchone() == (0,)
    assert db.execute("SELECT state,version FROM task_steps").fetchone() == ("CLAIMED", 0)
    assert not db.in_transaction


def test_intent_identity_is_stable_and_immutable() -> None:
    from jarvis.intent import OperationIntent
    from jarvis.planner import Planner

    step = (
        Planner()
        .plan(
            {
                "schema_version": 1,
                "request_key": "intent-fixture",
                "task_kind": "inventory.generate",
                "input": {"root": "/fixture/input"},
                "output": {"name": "inventory.json"},
            }
        )
        .steps[0]
    )
    arguments: dict[str, Any] = {
        "task_id": step.task_id,
        "logical_effect_id": step.logical_effect_id,
        "step_id": step.id,
        "target_path": "output/inventory.json",
        "prepared_path": "prepared/inventory.json",
        "expected_sha256": hashlib.sha256(b"inventory").hexdigest(),
        "expected_size": len(b"inventory"),
        "input_snapshot_sha256": "a" * 64,
    }
    first = OperationIntent(**arguments)
    second = OperationIntent(**dict(reversed(list(arguments.items()))))
    assert first == second
    assert first.operation_key == step.operation_key
    assert first.intended_action == "file.publish.no_clobber"
    with pytest.raises((AttributeError, TypeError)):
        first.target_path = "other"  # type: ignore[misc]
