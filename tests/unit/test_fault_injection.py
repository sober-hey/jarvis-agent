"""K0-015 DRAFT: controlled exceptions, real filesystem and offline SQLite only."""

from __future__ import annotations

import errno
import hashlib
import runpy
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from jarvis.intent import IntentRepository, OperationIntent
from jarvis.path_control import PathGuard
from jarvis.publisher import AtomicPublisher
from jarvis.repository import TaskRepository, WorkerLock

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = b"K0-015 controlled fault publication\n"


def _connection() -> sqlite3.Connection:
    fixture = runpy.run_path(str(ROOT / "tests/contract/test_sqlite_schema_draft.py"))
    connection = fixture["_apply_candidate"](fixture["_load"]())
    connection.execute("INSERT INTO system_control VALUES (1, 0, 0, 7, 0)")
    connection.commit()
    return connection


@dataclass
class Publication:
    root: Path
    repository: TaskRepository
    ledger: IntentRepository
    intent: OperationIntent
    guard: PathGuard

    @property
    def source(self) -> Path:
        return self.root / self.intent.prepared_path

    @property
    def target(self) -> Path:
        return self.root / self.intent.target_path


@pytest.fixture
def publication(tmp_path: Path) -> Iterator[Publication]:
    root = tmp_path.resolve()
    connection = _connection()
    with WorkerLock(root / "worker.lock") as lock:
        repository = TaskRepository(
            connection,
            worker_lock=lock,
            worker_epoch=7,
            draft_fixture=True,
            domain_guard=lambda _name, _binding: True,  # synthetic, never authority
        )
        repository.create_task(
            task_id="t1",
            request_key="r1",
            request_digest="a" * 64,
            task_kind="inventory.generate",
            now_ms=1,
        )
        connection.execute("INSERT INTO task_plans VALUES ('p1','t1',1,'ACTIVE',?,1)", ("b" * 64,))
        connection.execute("UPDATE tasks SET state='RUNNING', active_plan_id='p1'")
        connection.execute(
            "INSERT INTO task_steps (id,task_id,plan_id,ordinal,logical_effect_id,state) "
            "VALUES ('s1','t1','p1',1,'effect1','CLAIMED')"
        )
        connection.commit()
        (root / "prepared").mkdir()
        (root / "output").mkdir()
        (root / "prepared/result").write_bytes(PAYLOAD)
        intent = OperationIntent(
            task_id="t1",
            step_id="s1",
            logical_effect_id="effect1",
            prepared_path="prepared/result",
            target_path="output/result",
            expected_sha256=hashlib.sha256(PAYLOAD).hexdigest(),
            expected_size=len(PAYLOAD),
            input_snapshot_sha256="a" * 64,
        )
        try:
            yield Publication(
                root,
                repository,
                IntentRepository(repository),
                intent,
                PathGuard([root], permissions={root: ["read", "write"]}),
            )
        finally:
            connection.close()


def _publisher(case: Publication, injector: object) -> AtomicPublisher:
    return AtomicPublisher(
        case.guard,
        case.repository.worker_lock.path,
        intents=case.ledger,
        intent=case.intent,
        root=case.root,
        draft_fixture=True,
        fault_injector=injector,
    )


def test_disk_full_before_link_preserves_target_and_recoverable_intent(
    publication: Publication,
) -> None:
    from jarvis.fault_injection import FaultInjector

    injector = FaultInjector(draft_fixture=True)
    injector.arm("disk_full", layer="publisher", step="before_link")
    publisher = _publisher(publication, injector)
    with pytest.raises(OSError) as failure:
        publisher.publish(publication.source, publication.target, publication.intent.operation_id)
    assert failure.value.errno == errno.ENOSPC
    assert not publication.target.exists()
    assert publication.source.read_bytes() == PAYLOAD
    connection = publication.repository.connection
    assert connection.execute("SELECT state FROM operation_intents").fetchone() == (
        "NEEDS_RECONCILIATION",
    )
    assert connection.execute("SELECT state FROM task_steps").fetchone() == (
        "NEEDS_RECONCILIATION",
    )
    assert connection.execute("SELECT state FROM attempts").fetchall() == [("UNKNOWN",)]
    assert injector.events[0].fault == "disk_full"
