"""K0-008 DRAFT CLI tests; injected offline DB, never a runtime database."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Any]:
    spec = importlib.util.spec_from_file_location(
        "k0008_schema_fixture", ROOT / "tests/contract/test_sqlite_schema_draft.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = module._apply_candidate(module._load())
    connection.execute("INSERT INTO system_control VALUES (1, 0, 0, 0, 0)")
    connection.commit()
    try:
        yield connection, tmp_path
    finally:
        connection.close()


def _session(fixture: Any) -> Any:
    assert importlib.util.find_spec("jarvis.cli") is not None, "DRAFT CLI module is missing"
    cli = importlib.import_module("jarvis.cli")
    connection, project_root = fixture
    return cli.DraftSession(connection=connection, project_root=project_root)


def _invoke(session: Any, *arguments: str) -> Any:
    cli = importlib.import_module("jarvis.cli")
    return runner.invoke(cli.app, ["--draft-fixture", *arguments], obj=session)


def _payload(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_init_creates_offline_database_snapshot(session: Any, tmp_path: Path) -> None:
    session = _session(session)
    destination = tmp_path / "draft.db"
    payload = _payload(_invoke(session, "init", "--db", str(destination)))
    assert payload["runtime_enabled"] is False
    assert payload["database_kind"] == "offline_fixture_snapshot"
    assert destination.is_file()
    with sqlite3.connect(destination) as stored:
        assert stored.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        assert stored.execute("SELECT worker_epoch FROM system_control").fetchone() == (0,)
    assert not (tmp_path / ".jarvis-worker.lock").exists()


def _spec_file(tmp_path: Path, *, root: str = "/offline/input") -> Path:
    path = tmp_path / "task.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_key": "cli-request-1",
                "task_kind": "inventory.generate",
                "input": {"root": root},
                "output": {"name": "inventory.json"},
            }
        )
    )
    return path


def test_create_task_returns_repository_task_id(session: Any, tmp_path: Path) -> None:
    session = _session(session)
    specification = _spec_file(tmp_path)
    payload = _payload(_invoke(session, "create-task", "--spec", str(specification)))
    assert payload["task_id"].startswith("task-")
    assert payload["state"] == "CREATED"
    assert payload["duplicate_request"] is False
    assert session.connection.execute("SELECT id FROM tasks").fetchall() == [(payload["task_id"],)]
    assert session.connection.execute("SELECT event_type FROM events").fetchall() == [
        ("draft.task.created",)
    ]
    duplicate = _payload(_invoke(session, "create-task", "--spec", str(specification)))
    assert duplicate["task_id"] == payload["task_id"]
    assert duplicate["duplicate_request"] is True
    assert session.connection.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)


def test_plan_returns_detached_single_step_without_graph_activation(
    session: Any, tmp_path: Path
) -> None:
    session = _session(session)
    created = _payload(_invoke(session, "create-task", "--spec", str(_spec_file(tmp_path))))
    before = tuple(session.connection.iterdump())
    payload = _payload(_invoke(session, "plan", created["task_id"]))
    assert payload["persisted"] is False
    assert payload["plan"]["task_id"] == created["task_id"]
    assert [step["step_type"] for step in payload["plan"]["steps"]] == ["inventory.generate"]
    assert tuple(session.connection.iterdump()) == before


def test_status_uses_repository_without_lock_or_epoch_write(session: Any, tmp_path: Path) -> None:
    from jarvis.repository import WorkerLock

    session = _session(session)
    created = _payload(_invoke(session, "create-task", "--spec", str(_spec_file(tmp_path))))
    before = tuple(session.connection.iterdump())
    with WorkerLock(tmp_path / ".jarvis-worker.lock"):
        payload = _payload(_invoke(session, "status", created["task_id"]))
    assert payload["task"]["id"] == created["task_id"]
    assert payload["task"]["state"] == "CREATED"
    assert tuple(session.connection.iterdump()) == before


def test_cancel_accepts_request_for_unstarted_task(session: Any, tmp_path: Path) -> None:
    session = _session(session)
    created = _payload(_invoke(session, "create-task", "--spec", str(_spec_file(tmp_path))))
    result = _payload(_invoke(session, "cancel", created["task_id"], "--reason", "not needed"))
    assert result["cancel_requested"] is True
    assert result["cancellation_complete"] is False
    assert result["state"] == "CREATED"
    row = session.connection.execute(
        "SELECT cancel_requested_at_ms, cancel_reason, version FROM tasks"
    ).fetchone()
    assert row[0] is not None and row[1:] == ("not needed", 1)
    assert session.connection.execute(
        "SELECT event_type FROM events ORDER BY event_sequence"
    ).fetchall() == [
        ("draft.task.created",),
        ("draft.task.cancel_requested",),
    ]


def test_run_starts_worker_but_never_claims_unimplemented_execution(
    session: Any, tmp_path: Path
) -> None:
    session = _session(session)
    created = _payload(_invoke(session, "create-task", "--spec", str(_spec_file(tmp_path))))
    payload = _payload(_invoke(session, "run"))
    assert payload["execution_enabled"] is False
    assert payload["mode"] == "recovery_inventory_only"
    assert payload["worker_epoch"] == 2
    assert [task["id"] for task in payload["unfinished_tasks"]] == [created["task_id"]]
    assert session.connection.execute("SELECT state FROM tasks").fetchone() == ("CREATED",)
    assert session.connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (0,)


def test_cancel_cas_conflict_is_clear_and_does_not_write_request(
    session: Any, tmp_path: Path
) -> None:
    session = _session(session)
    created = _payload(_invoke(session, "create-task", "--spec", str(_spec_file(tmp_path))))
    result = _invoke(
        session, "cancel", created["task_id"], "--reason", "stop", "--expected-version", "9"
    )
    assert result.exit_code == 4
    assert json.loads(result.stderr)["error"]["code"] == "VERSION_CONFLICT"
    assert session.connection.execute("SELECT cancel_requested_at_ms FROM tasks").fetchone() == (
        None,
    )


def test_lock_contention_is_clear_and_does_not_bump_epoch(session: Any, tmp_path: Path) -> None:
    from jarvis.repository import WorkerLock

    session = _session(session)
    with WorkerLock(tmp_path / ".jarvis-worker.lock"):
        result = _invoke(session, "run")
    assert result.exit_code == 10
    assert json.loads(result.stderr)["error"]["code"] == "WORKER_ALREADY_RUNNING"
    assert session.connection.execute("SELECT worker_epoch FROM system_control").fetchone() == (0,)


def test_stale_epoch_is_clear_and_does_not_create_task(session: Any, tmp_path: Path) -> None:
    session = _session(session)
    session.connection.execute("DELETE FROM system_control")
    session.connection.commit()
    result = _invoke(session, "create-task", "--spec", str(_spec_file(tmp_path)))
    assert result.exit_code == 7
    assert json.loads(result.stderr)["error"]["code"] == "STALE_EPOCH"
    assert session.connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)


@pytest.mark.parametrize("command", ["status", "plan", "cancel"])
def test_missing_task_is_not_a_success(session: Any, command: str) -> None:
    session = _session(session)
    arguments = [command, "missing"]
    if command == "cancel":
        arguments.extend(["--reason", "stop"])
    result = _invoke(session, *arguments)
    assert result.exit_code == 3
    assert json.loads(result.stderr)["error"]["code"] == "TASK_NOT_FOUND"


def test_invalid_task_spec_returns_sanitized_json(session: Any, tmp_path: Path) -> None:
    session = _session(session)
    specification = tmp_path / "bad.json"
    specification.write_text('{"request_key":"sensitive-source-marker"}')
    result = _invoke(session, "create-task", "--spec", str(specification))
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "INVALID_REQUEST"
    assert "sensitive-source-marker" not in result.output


def test_init_never_overwrites_existing_file(session: Any, tmp_path: Path) -> None:
    session = _session(session)
    destination = tmp_path / "existing.db"
    destination.write_bytes(b"existing-data")
    result = _invoke(session, "init", "--db", str(destination))
    assert result.exit_code == 4
    assert json.loads(result.stderr)["error"]["code"] == "REQUEST_CONFLICT"
    assert destination.read_bytes() == b"existing-data"


@pytest.mark.parametrize("opt_in", [False, True])
def test_cli_without_injected_session_cannot_initialize_database(
    tmp_path: Path, opt_in: bool
) -> None:
    cli = importlib.import_module("jarvis.cli")
    destination = tmp_path / "runtime.db"
    arguments = (["--draft-fixture"] if opt_in else []) + ["init", "--db", str(destination)]
    result = runner.invoke(cli.app, arguments)
    assert result.exit_code == 5
    assert json.loads(result.stderr)["error"]["code"] == "DRAFT_ONLY"
    assert not destination.exists()


def test_file_backed_session_cannot_bypass_repository_boundary(tmp_path: Path) -> None:
    cli = importlib.import_module("jarvis.cli")
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        session = cli.DraftSession(connection=connection, project_root=tmp_path)
        result = _invoke(session, "run")
    assert result.exit_code == 5
    assert json.loads(result.stderr)["error"]["code"] == "DRAFT_ONLY"
    assert not (tmp_path / ".jarvis-worker.lock").exists()


def test_same_request_key_different_spec_is_rejected(session: Any, tmp_path: Path) -> None:
    session = _session(session)
    created = _payload(_invoke(session, "create-task", "--spec", str(_spec_file(tmp_path))))
    result = _invoke(session, "create-task", "--spec", str(_spec_file(tmp_path, root="/different")))
    assert result.exit_code == 5
    assert json.loads(result.stderr)["error"]["code"] == "POLICY_DENIED"
    assert session.task_specs[created["task_id"]].input.root == "/offline/input"
    assert session.connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (1,)


def test_plan_without_original_spec_requires_recovery(session: Any, tmp_path: Path) -> None:
    session = _session(session)
    created = _payload(_invoke(session, "create-task", "--spec", str(_spec_file(tmp_path))))
    session.task_specs.clear()
    result = _invoke(session, "plan", created["task_id"])
    assert result.exit_code == 7
    assert json.loads(result.stderr)["error"]["code"] == "RECOVERY_REQUIRED"


def test_epoch_expiry_in_repository_write_is_reported_and_rolled_back(
    session: Any, tmp_path: Path
) -> None:
    session = _session(session)
    session.connection.execute(
        "CREATE TRIGGER expire_cli AFTER INSERT ON events BEGIN "
        "UPDATE system_control SET worker_epoch = worker_epoch + 1; END"
    )
    result = _invoke(session, "create-task", "--spec", str(_spec_file(tmp_path)))
    assert result.exit_code == 7
    assert json.loads(result.stderr)["error"]["code"] == "STALE_EPOCH"
    assert session.connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
    assert session.connection.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)


def test_default_without_opt_in_rejects_even_injected_session(session: Any) -> None:
    cli = importlib.import_module("jarvis.cli")
    session = _session(session)
    result = runner.invoke(cli.app, ["run"], obj=session)
    assert result.exit_code == 5
    assert json.loads(result.stderr)["error"]["code"] == "DRAFT_ONLY"


def test_lost_lock_is_reported_as_sanitized_error(session: Any, tmp_path: Path) -> None:
    session = _session(session)
    connection = session.connection

    def replace_lock() -> int:
        # Fault injection only, not a supported lock lifecycle.
        lock = tmp_path / ".jarvis-worker.lock"
        lock.unlink()
        lock.touch()
        return 1

    connection.create_function("replace_lock", 0, replace_lock)
    connection.execute(
        "CREATE TRIGGER break_lock AFTER UPDATE ON system_control BEGIN SELECT replace_lock(); END"
    )
    result = _invoke(session, "run")
    assert result.exit_code == 10
    assert json.loads(result.stderr)["error"]["code"] == "WORKER_LOCK_LOST"


def test_init_rejects_uncommitted_fixture_without_exporting(session: Any, tmp_path: Path) -> None:
    session = _session(session)
    session.connection.execute("UPDATE system_control SET worker_epoch = 9")
    result = _invoke(session, "init", "--db", str(tmp_path / "uncommitted.db"))
    assert result.exit_code == 8
    assert json.loads(result.stderr)["error"]["code"] == "DATABASE_ERROR"
    assert not (tmp_path / "uncommitted.db").exists()
    assert session.connection.in_transaction


def test_plan_rejects_spec_not_bound_to_original_request(session: Any, tmp_path: Path) -> None:
    from jarvis.planner import TaskSpec

    session = _session(session)
    created = _payload(_invoke(session, "create-task", "--spec", str(_spec_file(tmp_path))))
    altered = _spec_file(tmp_path, root="/changed")
    session.task_specs[created["task_id"]] = TaskSpec.model_validate_json(altered.read_text())
    result = _invoke(session, "plan", created["task_id"])
    assert result.exit_code == 4
    assert json.loads(result.stderr)["error"]["code"] == "REQUEST_CONFLICT"
