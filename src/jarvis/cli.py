"""K0-008 CLI DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

This is an offline interface harness, not a persistent runtime adapter. A trusted
Python test/bootstrap must inject a DraftSession and explicitly opt in. No
packaging/ integration, migration runner or installed console script is added.
init exports an offline snapshot only; Repository/Worker still reject file DBs.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer

from jarvis.planner import Planner, TaskSpec
from jarvis.repository import (
    CASConflictError,
    DraftOnlyError,
    GuardRejectedError,
    LockNotHeldError,
    RepositoryError,
    StaleEpochError,
    TaskRepository,
    WorkerLock,
)
from jarvis.worker import Worker

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False
app = typer.Typer(help="JARVIS K0-008 offline CLI DRAFT (runtime disabled).", no_args_is_help=True)


@dataclass
class DraftSession:
    """Caller-owned in-memory fixture; not a capability or trusted runtime grant."""

    connection: sqlite3.Connection
    project_root: Path
    task_specs: dict[str, TaskSpec] = field(default_factory=lambda: dict[str, TaskSpec]())
    opted_in: bool = field(default=False, init=False)


def _emit(**payload: Any) -> None:
    typer.echo(
        json.dumps(
            {
                "schema_version": 1,
                "draft_status": DRAFT_STATUS,
                "runtime_enabled": RUNTIME_ENABLED,
                **payload,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
    )


class CLIError(Exception):
    """Sanitized command failure, independent of raw exception messages."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


def _fail(code: str, message: str, exit_code: int) -> None:
    typer.echo(
        json.dumps(
            {"schema_version": 1, "error": {"code": code, "message": message, "retryable": False}},
            ensure_ascii=False,
        ),
        err=True,
    )
    raise typer.Exit(exit_code)


@contextmanager
def _errors() -> Generator[None]:
    try:
        yield
    except CLIError as error:
        _fail(error.code, error.message, error.exit_code)
    except CASConflictError:
        _fail("VERSION_CONFLICT", "任务状态或版本已变化；请查询后重新提交。", 4)
    except BlockingIOError:
        _fail("WORKER_ALREADY_RUNNING", "执行器锁已被占用；未启动另一个 Worker。", 10)
    except LockNotHeldError:
        _fail("WORKER_LOCK_LOST", "执行器锁未持有或已失效；拒绝写入。", 10)
    except StaleEpochError:
        _fail("STALE_EPOCH", "Worker epoch 已过期或不可用；停止写入并先恢复对账。", 7)
    except DraftOnlyError:
        _fail("DRAFT_ONLY", "仅允许显式注入的内存草案夹具；持久化运行尚未接入。", 5)
    except GuardRejectedError:
        _fail("POLICY_DENIED", "Repository 安全守卫拒绝操作；未绕过守卫或自动重试。", 5)
    except FileExistsError:
        _fail("REQUEST_CONFLICT", "目标已存在；init 不会覆盖现有数据库。", 4)
    except (ValueError, UnicodeError):
        _fail("INVALID_REQUEST", "请求内容或参数无效；请检查严格 TaskSpec 与取消原因。", 2)
    except sqlite3.Error:
        _fail("DATABASE_ERROR", "数据库操作失败；未自动重试，请先检查数据库状态。", 8)
    except OSError:
        _fail("PATH_DENIED", "文件路径不存在、不可访问或不能安全创建。", 5)
    except RepositoryError:
        _fail("DATABASE_ERROR", "Repository 拒绝当前数据库配置或事务状态。", 8)


def _fixture(ctx: typer.Context) -> DraftSession:
    if not isinstance(ctx.obj, DraftSession) or ctx.obj.opted_in is not True:
        raise DraftOnlyError("explicit injected offline session required")
    session = ctx.obj
    # Validate via the existing Worker boundary, without acquiring a lock/epoch.
    Worker(session.connection, project_root=session.project_root, draft_fixture=True)
    return session


@app.callback()
def main(
    ctx: typer.Context,
    draft_fixture: Annotated[bool, typer.Option(help="Explicit offline test opt-in only.")] = False,
) -> None:
    if isinstance(ctx.obj, DraftSession):
        ctx.obj.opted_in = draft_fixture


@app.command()
def init(
    ctx: typer.Context,
    db: Annotated[
        Path, typer.Option(help="New offline SQLite snapshot; never opened for runtime.")
    ],
) -> None:
    """Export the injected schema fixture; do not migrate or overwrite any DB."""
    with _errors():
        session = _fixture(ctx)
        if session.connection.in_transaction:
            raise RepositoryError("cannot export caller transaction")
        snapshot = session.connection.serialize()
        descriptor = os.open(db, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(snapshot)
        _emit(database=str(db), database_kind="offline_fixture_snapshot")


def _request_digest(task_spec: TaskSpec) -> str:
    canonical = json.dumps(
        task_spec.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@app.command("create-task")
def create_task(
    ctx: typer.Context,
    spec: Annotated[Path, typer.Option(help="Strict K0 TaskSpec JSON file.")],
) -> None:
    """Create an in-memory task through the real flock/epoch-fenced Repository."""
    with _errors():
        session = _fixture(ctx)
        task_spec = TaskSpec.model_validate_json(spec.read_text(encoding="utf-8"))
        candidate = Planner().plan(task_spec)
        digest = _request_digest(task_spec)
        with Worker(
            session.connection, project_root=session.project_root, draft_fixture=True
        ) as worker:

            def create(repository: TaskRepository) -> tuple[dict[str, Any], bool]:
                duplicate = repository.get_task(candidate.task_id) is not None
                task = repository.create_task(
                    task_id=candidate.task_id,
                    request_key=task_spec.request_key,
                    request_digest=digest,
                    task_kind=task_spec.task_kind,
                    now_ms=time.time_ns() // 1_000_000,
                )
                return task, duplicate

            task, duplicate = worker.execute(create)
        session.task_specs[task["id"]] = task_spec
        _emit(task_id=task["id"], state=task["state"], duplicate_request=duplicate)


@app.command()
def plan(ctx: typer.Context, task_id: str) -> None:
    """Return a detached proposal; never persist or activate its graph."""
    with _errors():
        session = _fixture(ctx)
        task = _get_task(_reader(session), task_id)
        task_spec = session.task_specs.get(task_id)
        if task_spec is None:
            raise CLIError("RECOVERY_REQUIRED", "原始 TaskSpec 不在内存夹具中，不能重建计划。", 7)
        proposal = Planner().plan(task_spec)
        if (
            proposal.task_id != task_id
            or task["request_digest"] != _request_digest(task_spec)
            or task["request_key"] != task_spec.request_key
            or task["task_kind"] != task_spec.task_kind
        ):
            raise CLIError("REQUEST_CONFLICT", "内存 TaskSpec 与原始任务请求不匹配。", 4)
        _emit(task_id=task_id, persisted=False, plan=proposal.model_dump(mode="json"))


def _reader(session: DraftSession) -> TaskRepository:
    row = session.connection.execute(
        "SELECT worker_epoch FROM system_control WHERE singleton_id = 1"
    ).fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0:
        raise StaleEpochError("global epoch unavailable")
    return TaskRepository(
        session.connection,
        worker_lock=WorkerLock(session.project_root / ".jarvis-worker.lock"),
        worker_epoch=row[0],
        draft_fixture=True,
    )


def _get_task(repository: TaskRepository, task_id: str) -> dict[str, Any]:
    task = repository.get_task(task_id)
    if task is None:
        raise CLIError("TASK_NOT_FOUND", "任务不存在。", 3)
    return task


@app.command()
def status(ctx: typer.Context, task_id: str) -> None:
    """Read through Repository.get_task; never take the executor lifetime lock."""
    with _errors():
        task = _get_task(_reader(_fixture(ctx)), task_id)
        _emit(task=task)


@app.command()
def cancel(
    ctx: typer.Context,
    task_id: str,
    reason: Annotated[str, typer.Option(help="Nonempty cancellation reason.")],
    expected_version: Annotated[int | None, typer.Option(min=0)] = None,
) -> None:
    """Persist a cancellation request; never self-certify CANCELED/effects settled."""
    with _errors():
        session = _fixture(ctx)
        with Worker(
            session.connection, project_root=session.project_root, draft_fixture=True
        ) as worker:

            def request(repository: TaskRepository) -> dict[str, Any]:
                task = _get_task(repository, task_id)
                return repository.request_cancel(
                    task_id,
                    expected_version=task["version"]
                    if expected_version is None
                    else expected_version,
                    actor="draft-cli",
                    reason=reason,
                    now_ms=time.time_ns() // 1_000_000,
                )

            task = worker.execute(request)
        _emit(
            task_id=task["id"],
            state=task["state"],
            cancel_requested=task["cancel_requested_at_ms"] is not None,
            cancellation_complete=task["state"] == "CANCELED",
        )


@app.command()
def run(ctx: typer.Context) -> None:
    """Start the offline Worker and inventory unfinished tasks; no executor exists yet."""
    with _errors():
        session = _fixture(ctx)
        with Worker(
            session.connection, project_root=session.project_root, draft_fixture=True
        ) as worker:
            unfinished = worker.recover_tasks()
            epoch = worker.epoch
        _emit(
            mode="recovery_inventory_only",
            execution_enabled=False,
            worker_epoch=epoch,
            unfinished_tasks=unfinished,
        )


if __name__ == "__main__":
    app()
