"""K0-012 DRAFT local fixtures: real links/fds/fsync, in-memory Intent ledger only."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from jarvis.intent import IntentRepository, OperationIntent
from jarvis.path_control import PathControlError, PathGuard
from jarvis.repository import (
    DraftOnlyError,
    GuardRejectedError,
    LockNotHeldError,
    StaleEpochError,
    TaskRepository,
    WorkerLock,
)

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = b"draft atomic publication\n"


@dataclass
class Case:
    root: Path
    guard: PathGuard
    ledger: IntentRepository
    intent: OperationIntent
    publisher: Any

    @property
    def src(self) -> Path:
        return self.root / self.intent.prepared_path

    @property
    def dst(self) -> Path:
        return self.root / self.intent.target_path

    @property
    def db(self) -> Any:
        return self.ledger.repository.connection

    def publish(self) -> Any:
        return self.publisher.publish(self.src, self.dst, self.intent.operation_id)


@pytest.fixture
def case(tmp_path: Path) -> Iterator[Case]:
    from jarvis.publisher import AtomicPublisher

    root = tmp_path.resolve()
    spec = importlib.util.spec_from_file_location(
        "k0012_schema_fixture", ROOT / "tests/contract/test_sqlite_schema_draft.py"
    )
    assert spec is not None and spec.loader is not None
    schema = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schema)
    db = schema._apply_candidate(schema._load())
    db.execute("INSERT INTO system_control VALUES (1, 0, 0, 7, 0)")
    db.commit()
    with WorkerLock(root / "worker.lock") as lock:
        repository = TaskRepository(
            db,
            worker_lock=lock,
            worker_epoch=7,
            draft_fixture=True,
            domain_guard=lambda _name, _binding: True,  # synthetic, not authorization
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
        guard = PathGuard([root], permissions={root: ["read", "write"]})
        ledger = IntentRepository(repository)
        publisher = AtomicPublisher(
            guard,
            lock.path,
            intents=ledger,
            intent=intent,
            root=root,
            draft_fixture=True,
        )
        try:
            yield Case(root, guard, ledger, intent, publisher)
        finally:
            db.close()


def test_normal_publication_and_independent_hash(case: Case) -> None:
    result = case.publish()
    assert result.intent_id == case.intent.operation_id
    assert result.sha256 == hashlib.sha256(case.dst.read_bytes()).hexdigest()
    assert result.size_bytes == len(PAYLOAD)
    assert case.dst.read_bytes() == PAYLOAD
    assert case.src.stat().st_ino == case.dst.stat().st_ino
    assert case.db.execute("SELECT state FROM operation_intents").fetchone() == ("VERIFIED",)
    assert case.db.execute("SELECT state FROM task_steps").fetchone() == ("COMPLETED",)
    assert case.ledger.get_intent(case.intent.operation_id) == case.intent


@pytest.mark.parametrize("payload", [b"other bytes", PAYLOAD])
def test_existing_target_never_overwritten(case: Case, payload: bytes) -> None:
    from jarvis.publisher import PublishConflict

    case.dst.write_bytes(payload)
    before = case.dst.stat()
    with pytest.raises(PublishConflict):
        case.publish()
    assert case.dst.read_bytes() == payload
    assert (case.dst.stat().st_ino, case.dst.stat().st_mtime_ns) == (
        before.st_ino,
        before.st_mtime_ns,
    )
    assert case.db.execute("SELECT state FROM operation_intents").fetchone() != ("VERIFIED",)


@pytest.mark.parametrize("component", ["leaf", "ancestor"])
def test_source_symlinks_refused(case: Case, component: str) -> None:
    if component == "leaf":
        case.src.unlink()
        case.src.symlink_to(case.root / "worker.lock")
    else:
        case.src.parent.rename(case.root / "moved")
        case.src.parent.symlink_to(case.root / "moved", target_is_directory=True)
    with pytest.raises(PathControlError):
        case.publish()
    assert not case.dst.exists()
    assert case.db.execute("SELECT COUNT(*) FROM attempts").fetchone() == (0,)


@pytest.mark.parametrize("endpoint", ["src", "dst"])
def test_outside_paths_refused(case: Case, endpoint: str) -> None:
    outside = case.root.parent / "outside"
    source, target = (outside, case.dst) if endpoint == "src" else (case.src, outside)
    with pytest.raises(PathControlError):
        case.publisher.publish(source, target, case.intent.operation_id)
    assert not case.dst.exists()


def test_hash_mismatch_after_link_preserves_evidence(case: Case, monkeypatch: Any) -> None:
    from jarvis.publisher import VerificationError

    original = os.link

    def corrupt(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        # A hard link shares the inode: the expected hash must come from the Intent,
        # not a second read of the now equally corrupted source.
        case.dst.write_bytes(b"corrupted after linking")

    monkeypatch.setattr(os, "link", corrupt)
    with pytest.raises(VerificationError):
        case.publish()
    assert case.dst.read_bytes() == b"corrupted after linking"
    assert case.db.execute("SELECT state FROM operation_intents").fetchone() == (
        "NEEDS_RECONCILIATION",
    )
    assert case.ledger.unknown_intents() == (case.intent,)


def test_intent_and_attempt_committed_before_link(case: Case, monkeypatch: Any) -> None:
    original = os.link
    calls: list[bool] = []

    def inspect(*args: Any, **kwargs: Any) -> None:
        assert not case.db.in_transaction
        assert case.ledger.get_intent(case.intent.operation_id) == case.intent
        assert case.db.execute("SELECT state FROM operation_intents").fetchone() == ("PUBLISHING",)
        assert case.db.execute("SELECT state FROM attempts").fetchone() == ("ACTIVE",)
        assert not case.dst.exists()
        assert kwargs["follow_symlinks"] is False
        assert isinstance(kwargs["src_dir_fd"], int)
        assert isinstance(kwargs["dst_dir_fd"], int)
        calls.append(True)
        original(*args, **kwargs)

    monkeypatch.setattr(os, "link", inspect)
    case.publish()
    assert calls == [True]


def test_file_and_directory_fsync(case: Case, monkeypatch: Any) -> None:
    original = os.fsync
    kinds: list[int] = []

    def sync(fd: int) -> None:
        kinds.append(stat.S_IFMT(os.fstat(fd).st_mode))
        original(fd)

    monkeypatch.setattr(os, "fsync", sync)
    case.publish()
    assert kinds.count(stat.S_IFREG) >= 2
    assert kinds.count(stat.S_IFDIR) >= 2


@pytest.mark.parametrize("kind", [stat.S_IFREG, stat.S_IFDIR])
def test_post_link_sync_failure_is_unknown(case: Case, monkeypatch: Any, kind: int) -> None:
    original_sync, original_link = os.fsync, os.link
    linked = False

    def link(*args: Any, **kwargs: Any) -> None:
        nonlocal linked
        original_link(*args, **kwargs)
        linked = True

    def sync(fd: int) -> None:
        if linked and stat.S_IFMT(os.fstat(fd).st_mode) == kind:
            raise OSError(errno.EIO, "injected sync failure")
        original_sync(fd)

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(OSError, match="injected sync"):
        case.publish()
    assert case.dst.read_bytes() == PAYLOAD
    assert case.db.execute("SELECT state FROM operation_intents").fetchone() == (
        "NEEDS_RECONCILIATION",
    )


def test_changed_prepared_hash_denies_before_intent(case: Case) -> None:
    from jarvis.publisher import VerificationError

    case.src.write_bytes(b"not the prepared bytes")
    with pytest.raises(VerificationError):
        case.publish()
    assert not case.dst.exists()
    assert case.db.execute("SELECT COUNT(*) FROM operation_intents").fetchone() == (0,)


@pytest.mark.parametrize("flag", ["epoch", "cancel", "stop", "lock"])
def test_flock_and_epoch_and_stop_guards(case: Case, flag: str) -> None:
    if flag == "epoch":
        case.db.execute("UPDATE system_control SET worker_epoch=8")
        expected = StaleEpochError
    elif flag == "cancel":
        case.db.execute("UPDATE tasks SET cancel_requested_at_ms=2")
        expected = GuardRejectedError
    elif flag == "stop":
        case.db.execute("UPDATE system_control SET emergency_stop=1")
        expected = GuardRejectedError
    else:
        case.ledger.repository.worker_lock._owner = None  # pyright: ignore[reportPrivateUsage]
        expected = LockNotHeldError
    case.db.commit()
    try:
        with pytest.raises(expected):
            case.publish()
        assert not case.dst.exists()
    finally:
        if flag == "lock":
            import threading

            case.ledger.repository.worker_lock._owner = (  # pyright: ignore[reportPrivateUsage]
                os.getpid(),
                threading.get_ident(),
            )


def test_duplicate_publication_is_not_automatic_retry(case: Case) -> None:
    case.publish()
    before = case.dst.stat()
    with pytest.raises(GuardRejectedError):
        case.publish()
    assert case.dst.stat() == before
    assert case.db.execute("SELECT COUNT(*) FROM attempts").fetchone() == (1,)


def test_unresolved_attempt_must_reconcile_first(case: Case) -> None:
    case.ledger.create_intent(case.intent, expected_version=0, now_ms=10)
    case.ledger.start_attempt(case.intent.operation_id, expected_version=1, now_ms=20)
    with pytest.raises(GuardRejectedError):
        case.publish()
    assert not case.dst.exists()
    assert case.db.execute("SELECT COUNT(*) FROM attempts").fetchone() == (1,)


def test_cross_volume_error_has_no_copy_fallback(case: Case, monkeypatch: Any) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.EXDEV, "injected cross-device error")

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(OSError) as error:
        case.publish()
    assert error.value.errno == errno.EXDEV
    assert not case.dst.exists()
    assert case.src.read_bytes() == PAYLOAD


def test_constructor_requires_explicit_fixture(case: Case) -> None:
    from jarvis.publisher import AtomicPublisher

    with pytest.raises(DraftOnlyError):
        AtomicPublisher(
            case.guard,
            case.ledger.repository.worker_lock.path,
            intents=case.ledger,
            intent=case.intent,
            root=case.root,
        )


def test_intent_identity_and_paths_cannot_change(case: Case) -> None:
    from jarvis.publisher import PublishError

    with pytest.raises(PublishError):
        case.publisher.publish(case.src, case.dst, "wrong-id")
    with pytest.raises(PublishError):
        case.publisher.publish(case.src, case.root / "output/other", case.intent.operation_id)
    assert not case.dst.exists()
    assert case.db.execute("SELECT COUNT(*) FROM operation_intents").fetchone() == (0,)


def test_raw_parent_traversal_refused(case: Case) -> None:
    with pytest.raises(PathControlError):
        case.publisher.publish(
            str(case.src.parent) + "/../prepared/result",
            case.dst,
            case.intent.operation_id,
        )
    assert not case.dst.exists()


def test_source_replaced_during_link_is_not_verified(case: Case, monkeypatch: Any) -> None:
    from jarvis.publisher import VerificationError

    original = os.link

    def replace_source(*args: Any, **kwargs: Any) -> None:
        case.src.unlink()
        case.src.write_bytes(PAYLOAD)
        original(*args, **kwargs)

    monkeypatch.setattr(os, "link", replace_source)
    with pytest.raises(VerificationError):
        case.publish()
    assert case.dst.read_bytes() == PAYLOAD
    assert case.db.execute("SELECT state FROM operation_intents").fetchone() == (
        "NEEDS_RECONCILIATION",
    )


def test_target_created_at_link_conflicts_without_overwrite(case: Case, monkeypatch: Any) -> None:
    from jarvis.publisher import PublishConflict

    original = os.link

    def create_target(*args: Any, **kwargs: Any) -> None:
        case.dst.write_bytes(b"late concurrent target")
        original(*args, **kwargs)

    monkeypatch.setattr(os, "link", create_target)
    with pytest.raises(PublishConflict):
        case.publish()
    assert case.dst.read_bytes() == b"late concurrent target"


def test_target_symlink_is_preserved_and_rejected(case: Case) -> None:
    case.dst.symlink_to(case.src)
    with pytest.raises(PathControlError):
        case.publish()
    assert case.dst.is_symlink()
    assert case.src.read_bytes() == PAYLOAD


def test_intent_transaction_failure_prevents_link(case: Case) -> None:
    import sqlite3

    case.db.execute(
        "CREATE TRIGGER reject_intent BEFORE INSERT ON operation_intents "
        "BEGIN SELECT RAISE(ABORT,'injected Intent failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected Intent"):
        case.publish()
    assert not case.dst.exists()
    assert case.db.execute("SELECT COUNT(*) FROM attempts").fetchone() == (0,)
    assert case.db.execute("SELECT state FROM task_steps").fetchone() == ("CLAIMED",)


def test_epoch_changes_after_link_cannot_submit_success(case: Case, monkeypatch: Any) -> None:
    original = os.link

    def stale(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        case.db.execute("UPDATE system_control SET worker_epoch=8")
        case.db.commit()

    monkeypatch.setattr(os, "link", stale)
    with pytest.raises(StaleEpochError):
        case.publish()
    assert case.dst.read_bytes() == PAYLOAD
    assert case.db.execute("SELECT state FROM operation_intents").fetchone() == ("PUBLISHING",)
    assert case.db.execute("SELECT state FROM attempts").fetchone() == ("ACTIVE",)


def test_unregistered_alternate_lock_denied(case: Case) -> None:
    from jarvis.publisher import AtomicPublisher, PublishError

    with pytest.raises(PublishError):
        AtomicPublisher(
            case.guard,
            case.root / "other.lock",
            intents=case.ledger,
            intent=case.intent,
            root=case.root,
            draft_fixture=True,
        )


def test_tampered_expected_hash_cannot_publish(case: Case) -> None:
    from jarvis.publisher import AtomicPublisher, VerificationError

    changed = replace(case.intent, expected_sha256="0" * 64)
    publisher = AtomicPublisher(
        case.guard,
        case.ledger.repository.worker_lock.path,
        intents=case.ledger,
        intent=changed,
        root=case.root,
        draft_fixture=True,
    )
    with pytest.raises(VerificationError):
        publisher.publish(case.src, case.dst, changed.operation_id)
    assert not case.dst.exists()
