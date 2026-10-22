"""K0-012 DRAFT; NOT_APPROVED / NOT_FROZEN, runtime_enabled=False.

Explicit local fixtures only: real no-clobber filesystem I/O composed with the
K0-011 IN-MEMORY Intent ledger. No CLI/Worker registration, durable DB adapter,
production Capability or cancellation linearization claim. flock AND epoch are
mandatory, unknown effects require reconciliation, and no target is overwritten.
Hard links share an inode; prepared files must remain under exclusive control.
fsync success is NOT a machine restart / power-loss durability guarantee.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from jarvis.intent import IntentRepository, OperationIntent
from jarvis.path_control import PathControlError, PathGuard, PathInput, PlatformCapabilityError
from jarvis.repository import DraftOnlyError, GuardRejectedError

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


class PublishError(RuntimeError):
    """Publication rejected; never implies an unknown effect is safe to retry."""


class PublishConflict(PublishError):
    """EEXIST: keep the original target and route to read-only reconciliation."""


class VerificationError(PublishError):
    """Expected immutable Intent hash/size or controlled file identity disagrees."""


@dataclass(frozen=True)
class PublishResult:
    intent_id: str
    target: Path
    sha256: str
    size_bytes: int


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass
class _Directory:
    fd: int
    bindings: list[tuple[int, str, tuple[int, int, int]]]

    def check(self) -> None:
        for parent, name, expected in self.bindings:
            if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected:
                raise PathControlError("directory binding changed during publication")


@contextmanager
def _directory(path: Path) -> Generator[_Directory]:
    """Keep every ancestor descriptor, rejecting links/types before and after I/O."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    bindings: list[tuple[int, str, tuple[int, int, int]]] = []
    with ExitStack() as stack:
        fd = os.open("/", flags)
        stack.callback(os.close, fd)
        for name in path.parts[1:]:
            observed = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(observed.st_mode):
                raise PathControlError("directory ancestor must not be a symlink or special file")
            child = os.open(name, flags, dir_fd=fd)
            stack.callback(os.close, child)
            identity = _identity(observed)
            if _identity(os.fstat(child)) != identity:
                raise PathControlError("directory changed while opening")
            bindings.append((fd, name, identity))
            fd = child
        held = _Directory(fd, bindings)
        held.check()
        yield held
        held.check()


@contextmanager
def _regular(directory: _Directory, name: str) -> Generator[int]:
    observed = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
    if not stat.S_ISREG(observed.st_mode):
        raise PathControlError("only regular non-symlink files may be published/verified")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory.fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or _signature(os.fstat(fd)) != _signature(
            observed
        ):
            raise PathControlError("file changed while opening controlled descriptor")
        yield fd
    finally:
        os.close(fd)


def _hash_fd(fd: int) -> tuple[str, int]:
    before = os.fstat(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    if _signature(before) != _signature(os.fstat(fd)) or size != before.st_size:
        raise VerificationError("file changed during independent hash read")
    return digest.hexdigest(), size


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class AtomicPublisher:
    """One immutable OperationIntent and trusted root, for opted-in fixtures.

    Extra constructor bindings are necessary: intent_id alone cannot reconstruct
    task/Step/snapshot/expected hash or authorize an operation. The fixed lock
    must already be held by this ledger's Worker; no second per-call lock is used.
    Existing committed PREPARED intents may be resumed, but PUBLISHING/unknown or
    terminal states are never reset. Retry scheduling stays with reconciliation.
    """

    def __init__(
        self,
        guard: PathGuard,
        lock_path: PathInput,
        *,
        intents: IntentRepository,
        intent: OperationIntent,
        root: PathInput,
        draft_fixture: bool = False,
        fault_injector: object = None,
    ) -> None:
        if draft_fixture is not True:
            raise DraftOnlyError("explicit offline draft_fixture=True required")
        self._fault_injector = fault_injector
        if not all(getattr(os, flag, 0) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")):
            raise PlatformCapabilityError("nofollow directory/regular-file primitives required")
        intents.repository._validate_connection(  # pyright: ignore[reportPrivateUsage]
            intents.repository.connection
        )
        self._guard = guard
        self._root = guard.check_permission(root, "read")
        if not self._root.is_dir():
            raise PathControlError("publisher root must be an existing directory")
        self._root_identity = _identity(self._root.stat(follow_symlinks=False))
        lock = guard.check_permission(lock_path, "write")
        if lock != intents.repository.worker_lock.path:
            raise PublishError("publisher must use the repository's one fixed lifetime flock")
        if lock in {self._root / intent.prepared_path, self._root / intent.target_path}:
            raise PublishError("lock file must not alias a publication path")
        self._lock_path = lock
        self._intents = intents
        self._intent = intent

    def _check_paths(self, src: PathInput, dst: PathInput, intent_id: str) -> tuple[Path, Path]:
        # Raw spelling passes through PathGuard BEFORE pathlib can erase '..'.
        source = self._guard.check_permission(src, "read")
        target = self._guard.check_permission(dst, "write")
        self._guard.check_permission(self._root, "read")
        if _identity(self._root.stat(follow_symlinks=False)) != self._root_identity:
            raise PathControlError("publisher root identity changed")
        if (
            intent_id != self._intent.operation_id
            or source != self._root / self._intent.prepared_path
            or target != self._root / self._intent.target_path
        ):
            raise PublishError("paths/identity do not match the original OperationIntent")
        # Verification is separately authorized, not implied by write permission.
        # Its target may not exist yet, so check the read policy once created.
        if not stat.S_ISREG(source.stat(follow_symlinks=False).st_mode):
            raise PathControlError("prepared source must be a regular file")
        return source, target

    def _version(self) -> int:
        row = self._intents.repository.connection.execute(
            "SELECT version FROM task_steps WHERE id=?", (self._intent.step_id,)
        ).fetchone()
        if row is None:
            raise GuardRejectedError("original Step missing")
        return row[0]

    def _prepare(self) -> None:
        ledger = self._intents
        db = ledger.repository.connection
        row = db.execute(
            "SELECT state FROM operation_intents WHERE operation_id=?",
            (self._intent.operation_id,),
        ).fetchone()
        if row is None:
            ledger.create_intent(self._intent, expected_version=self._version(), now_ms=_now_ms())
        elif row != ("PREPARED",) or ledger.get_intent(self._intent.operation_id) != self._intent:
            raise GuardRejectedError("existing Intent requires reconciliation, not republishing")

    def _fence(self) -> None:
        repository = self._intents.repository
        # Reuse BOTH the SQL epoch barrier and the held lifetime flock. No DB
        # transaction spans os.link: cross-resource atomicity is not promised.
        with repository._write():  # pyright: ignore[reportPrivateUsage]
            task = repository.get_task(self._intent.task_id)
            if task is None:
                raise GuardRejectedError("parent Task missing")
            repository._check_guard("can_start_work", task)  # pyright: ignore[reportPrivateUsage]

    def _verify(
        self, source_identity: tuple[int, int, int], target: Path, directory: _Directory
    ) -> tuple[str, int]:
        """Fresh target open + byte read, never reuse source fd/digest as evidence."""
        self._guard.check_permission(target, "read")
        directory.check()
        try:
            with _regular(directory, target.name) as fd:
                before = os.fstat(fd)
                if _identity(before) != source_identity:
                    raise VerificationError("published target is not the controlled source inode")
                actual = _hash_fd(fd)
                named = os.stat(target.name, dir_fd=directory.fd, follow_symlinks=False)
                if _signature(named) != _signature(os.fstat(fd)):
                    raise VerificationError("target path binding changed during verification")
                if actual != (self._intent.expected_sha256, self._intent.expected_size):
                    raise VerificationError("published hash/size disagrees with immutable Intent")
                os.fsync(fd)
            directory.check()
            self._guard.check_permission(target, "read")
            return actual
        except (OSError, PathControlError) as error:
            raise VerificationError(
                "published target could not be independently verified"
            ) from error

    def publish(self, src: PathInput, dst: PathInput, intent_id: str) -> PublishResult:
        """Prepare Intent → commit Attempt → link → sync → verify → update ledger.

        EEXIST always raises PublishConflict, including equal bytes. The original
        target stays untouched; the caller can subsequently use ledger.reconcile.
        No copy/rename/delete fallback and no automatic retry or overwrite exists.
        """
        source, target = self._check_paths(src, dst, intent_id)
        repository = self._intents.repository
        repository.worker_lock.assert_held()
        self._fence()
        with _directory(source.parent) as prepared_dir, _directory(target.parent) as target_dir:
            with _regular(prepared_dir, source.name) as source_fd:
                source_info = os.fstat(source_fd)
                source_identity = _identity(source_info)
                if source_info.st_dev != os.fstat(target_dir.fd).st_dev:
                    raise OSError(errno.EXDEV, "prepared and target must be on the same volume")
                if _hash_fd(source_fd) != (
                    self._intent.expected_sha256,
                    self._intent.expected_size,
                ):
                    raise VerificationError("prepared hash/size disagrees with immutable Intent")
                # Prepared file + parent must sync BEFORE an Intent references it.
                os.fsync(source_fd)
                os.fsync(prepared_dir.fd)
                self._prepare()
                attempt = self._intents.start_attempt(
                    intent_id, expected_version=self._version(), now_ms=_now_ms()
                )
                try:
                    self._fence()
                    self._check_paths(src, dst, intent_id)
                    prepared_dir.check()
                    target_dir.check()
                    named = os.stat(source.name, dir_fd=prepared_dir.fd, follow_symlinks=False)
                    if _signature(named) != _signature(source_info) or _hash_fd(source_fd) != (
                        self._intent.expected_sha256,
                        self._intent.expected_size,
                    ):
                        raise VerificationError("prepared file changed before publication")
                    try:
                        if self._fault_injector is not None:
                            self._fault_injector.raise_if_armed("publisher", "before_link")
                        os.link(
                            source.name,
                            target.name,
                            src_dir_fd=prepared_dir.fd,
                            dst_dir_fd=target_dir.fd,
                            follow_symlinks=False,
                        )
                    except OSError as error:
                        if error.errno == errno.EEXIST:
                            raise PublishConflict(
                                "target exists; preserve it and reconcile first"
                            ) from error
                        raise
                    os.fsync(source_fd)
                    os.fsync(target_dir.fd)
                    actual = self._verify(source_identity, target, target_dir)
                    prepared_dir.check()
                    self._check_paths(src, dst, intent_id)
                except Exception as error:
                    # A failed syscall/sync/verification is not proof of no effect.
                    # If even recording fails, leave the committed ACTIVE/PUBLISHING
                    # evidence for recovery; never delete a possibly published target.
                    self._intents.finish_attempt(
                        attempt.id,
                        result="UNKNOWN",
                        error=type(error).__name__,
                        expected_version=self._version(),
                        now_ms=_now_ms(),
                    )
                    raise
                self._intents.finish_attempt(
                    attempt.id,
                    result="SUCCESS",
                    error=None,
                    expected_version=self._version(),
                    now_ms=_now_ms(),
                )
                self._intents.complete_verified(
                    intent_id,
                    root=self._root,
                    expected_version=self._version(),
                    now_ms=_now_ms(),
                )
                return PublishResult(intent_id, target, *actual)
