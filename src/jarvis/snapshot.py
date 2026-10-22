"""K0-010 input snapshot DRAFT; NOT_FROZEN / NOT_APPROVED, runtime disabled.

Synchronous, bounded IN-MEMORY freeze only: no lazy files, execution callbacks,
DB writes, artifact persistence or production validity claim. Every retained byte
is copied before create() returns. A frozen result is not an execution grant.
PathGuard is policy preflight; actual reads retain nofollow directory/file fds.
Ordinary concurrent changes fail closed; no global point-in-time consistency or
protection against malicious same-UID writers is promised (MP section 12).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Generator, Iterable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jarvis.path_control import PathControlError, PathGuard, PathInput, PlatformCapabilityError

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False
MAX_FILES = 1000
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_DEPTH = 16
_REVISION = "jarvis.k0.snapshot.draft.v1"


class SnapshotError(ValueError):
    """Invalid or unstable source; no partial snapshot is returned."""


class SnapshotVerificationError(SnapshotError):
    """Snapshot bytes, provenance, plan binding or live source disagree."""


@dataclass(frozen=True, slots=True)
class FileMetadata:
    """Source provenance, excluded from the deterministic content address."""

    device: int
    inode: int
    size_bytes: int
    mtime_ns: int
    ctime_ns: int
    mode: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> FileMetadata:
        return cls(
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode
        )


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    path: str
    source_path: str
    content: bytes
    sha256: str
    size_bytes: int
    metadata: FileMetadata


@dataclass(frozen=True, slots=True)
class FrozenSnapshot:
    """Detached immutable bytes + provenance; NOT a persisted artifact row."""

    plan_id: str
    snapshot_id: str
    files: tuple[SnapshotFile, ...]
    source_root: str
    source_files: tuple[str, ...]
    source_directories: tuple[str, ...]
    manifest_bytes: bytes
    status: Literal["FROZEN_IN_MEMORY_NOT_COMMITTED"] = "FROZEN_IN_MEMORY_NOT_COMMITTED"

    @property
    def binding(self) -> tuple[str, str]:
        return self.plan_id, self.snapshot_id


def _json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _manifest(files: tuple[SnapshotFile, ...]) -> tuple[str, bytes]:
    body = {
        "schema_version": 1,
        "files": [
            {"path": item.path, "size_bytes": item.size_bytes, "sha256": item.sha256}
            for item in files
        ],
        "file_count": len(files),
        "total_bytes": sum(item.size_bytes for item in files),
    }
    snapshot_id = (
        "snapshot-"
        + hashlib.sha256(_json_bytes({"namespace": _REVISION, "payload": body})).hexdigest()
    )
    return snapshot_id, _json_bytes({**body, "snapshot_id": snapshot_id})


def _plan_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise SnapshotError("plan_id must be nonempty text without NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise SnapshotError("plan_id must be safely UTF-8 encodable") from error
    return value  # Validate, never strip or rewrite an identifier.


def _sources(values: Iterable[PathInput]) -> tuple[PathInput, ...]:
    if isinstance(values, (str, bytes, os.PathLike)):
        raise SnapshotError("sources must be a collection of explicit paths")
    try:
        bounded: list[PathInput] = []
        for value in values:
            if len(bounded) >= MAX_FILES:
                raise SnapshotError("source selection exceeds the K0 limit")
            bounded.append(value)
        return tuple(bounded)
    except TypeError as error:
        raise SnapshotError("sources must be an iterable of explicit paths") from error


@contextmanager
def _open_checked(guard: PathGuard, path: Path, *, directory: bool) -> Generator[int]:
    """Retain nofollow fd traversal through I/O; recheck original path bindings."""
    guard.check_permission(path, "read")
    if not getattr(os, "O_NONBLOCK", 0):
        raise PlatformCapabilityError("nonblocking open required to reject raced special files")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    bindings: list[tuple[int, str, FileMetadata]] = []
    try:
        with ExitStack() as stack:
            fd = os.open("/", flags)
            stack.callback(os.close, fd)
            for index, component in enumerate(path.parts[1:]):
                is_directory = directory or index < len(path.parts) - 2
                observed = os.stat(component, dir_fd=fd, follow_symlinks=False)
                required = stat.S_ISDIR if is_directory else stat.S_ISREG
                if not required(observed.st_mode):
                    raise PathControlError("source type changed or is unsupported")
                metadata = FileMetadata.from_stat(observed)
                bindings.append((fd, component, metadata))
                child_flags = (
                    flags if is_directory else (os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                )
                child = os.open(component, child_flags, dir_fd=fd)
                stack.callback(os.close, child)
                if FileMetadata.from_stat(os.fstat(child)) != metadata:
                    raise PathControlError("source changed while opening controlled fd")
                fd = child
            guard.check_permission(path, "read")
            yield fd
            # Directory ancestors may legitimately change membership elsewhere;
            # identity/type must not. The leaf's complete metadata must match.
            for index, (parent_fd, name, observed) in enumerate(bindings):
                checked = FileMetadata.from_stat(
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                )
                if index == len(bindings) - 1:
                    unchanged = checked == observed
                else:
                    unchanged = (checked.device, checked.inode, stat.S_IFMT(checked.mode)) == (
                        observed.device,
                        observed.inode,
                        stat.S_IFMT(observed.mode),
                    )
                if not unchanged:
                    raise PathControlError("source path changed during snapshot I/O")
            guard.check_permission(path, "read")
    except (NotImplementedError, TypeError) as error:
        raise PlatformCapabilityError("controlled fd operation is unsupported") from error
    except OSError as error:
        if isinstance(error, PathControlError):
            raise
        raise PathControlError("source could not be safely read") from error


class Snapshot:
    """DRAFT builder rooted in trusted PathGuard policy and one explicit root.

    create(files, directories=(), plan_id=...) captures explicit regular files and
    flat directories. Nested directories are rejected, never silently skipped.
    An empty directory is a valid source; an empty source selection is not.
    verify(snapshot, plan_id=...) rehashes both frozen and original source bytes.
    """

    def __init__(self, guard: PathGuard, *, root: PathInput) -> None:
        self._guard = guard
        self._root = guard.check_permission(root, "read")
        with _open_checked(guard, self._root, directory=True) as fd:
            info = os.fstat(fd)
            self._root_identity = info.st_dev, info.st_ino

    def _check_root(self) -> None:
        with _open_checked(self._guard, self._root, directory=True) as fd:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != self._root_identity:
                raise PathControlError("snapshot root identity changed")

    def _path(self, value: PathInput) -> Path:
        # Pass the raw spelling to PathGuard before Path can erase components.
        path = self._guard.check_permission(value, "read")
        if not path.is_relative_to(self._root):
            raise PathControlError("source is outside the selected snapshot root")
        if len(path.relative_to(self._root).parts) > MAX_DEPTH:
            raise SnapshotError("source depth exceeds the K0 limit")
        return path

    def _read_file(self, path: Path, remaining: int) -> SnapshotFile:
        with _open_checked(self._guard, path, directory=False) as fd:
            before = FileMetadata.from_stat(os.fstat(fd))
            limit = min(MAX_FILE_BYTES, remaining)
            if before.size_bytes > limit:
                raise SnapshotError("source byte limit exceeded")
            chunks: list[bytes] = []
            size = 0
            digest = hashlib.sha256()
            while chunk := os.read(fd, min(64 * 1024, limit - size + 1)):
                size += len(chunk)
                if size > limit:
                    raise SnapshotError("source grew beyond byte limit")
                digest.update(chunk)
                chunks.append(chunk)
            if FileMetadata.from_stat(os.fstat(fd)) != before or size != before.size_bytes:
                raise SnapshotError("source changed during reading")
            return SnapshotFile(
                path.relative_to(self._root).as_posix(),
                str(path),
                b"".join(chunks),
                digest.hexdigest(),
                size,
                before,
            )

    def _capture(
        self, source_files: tuple[str, ...], source_directories: tuple[str, ...]
    ) -> tuple[SnapshotFile, ...]:
        self._check_root()
        result: dict[str, SnapshotFile] = {}
        total = 0

        def add(path: Path) -> None:
            nonlocal total
            relative = path.relative_to(self._root).as_posix()
            if relative in result:
                raise SnapshotError("duplicate snapshot path")
            if len(result) >= MAX_FILES:
                raise SnapshotError("source file count exceeds the K0 limit")
            entry = self._read_file(path, MAX_TOTAL_BYTES - total)
            result[relative] = entry
            total += entry.size_bytes

        for source in source_files:
            add(self._path(source))
        for source in source_directories:
            directory = self._path(source)
            with _open_checked(self._guard, directory, directory=True) as fd:
                before = FileMetadata.from_stat(os.fstat(fd))
                names = os.listdir(fd)
                try:
                    names.sort(key=lambda name: name.encode("utf-8", errors="strict"))
                except UnicodeError as error:
                    raise SnapshotError("directory name cannot be safely UTF-8 encoded") from error
                if len(names) + len(result) > MAX_FILES:
                    raise SnapshotError("source file count exceeds the K0 limit")
                for name in names:
                    path = self._path(directory / name)
                    observed = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(observed.st_mode):
                        raise SnapshotError("only flat directories are supported in this draft")
                    if not stat.S_ISREG(observed.st_mode):
                        raise PathControlError("only regular source files are supported")
                    add(path)
                if (
                    names != sorted(os.listdir(fd), key=lambda name: name.encode("utf-8"))
                    or FileMetadata.from_stat(os.fstat(fd)) != before
                ):
                    raise SnapshotError("directory changed during capture")
        self._check_root()
        return tuple(result[name] for name in sorted(result, key=lambda name: name.encode("utf-8")))

    def create(
        self,
        files: Iterable[PathInput] = (),
        *,
        directories: Iterable[PathInput] = (),
        plan_id: str,
    ) -> FrozenSnapshot:
        plan_id = _plan_id(plan_id)
        source_files = tuple(str(self._path(value)) for value in _sources(files))
        source_directories = tuple(str(self._path(value)) for value in _sources(directories))
        if not source_files and not source_directories:
            raise SnapshotError("at least one explicit file or directory source is required")
        if len(set(source_files)) != len(source_files) or len(set(source_directories)) != len(
            source_directories
        ):
            raise SnapshotError("duplicate source selection")
        source_files = tuple(sorted(source_files, key=lambda path: path.encode("utf-8")))
        source_directories = tuple(
            sorted(source_directories, key=lambda path: path.encode("utf-8"))
        )
        captured = self._capture(source_files, source_directories)
        snapshot_id, manifest_bytes = _manifest(captured)
        frozen = FrozenSnapshot(
            plan_id,
            snapshot_id,
            captured,
            str(self._root),
            source_files,
            source_directories,
            manifest_bytes,
        )
        # A second bounded pass catches an earlier file changing while later
        # files were copied. This is still not global filesystem isolation.
        self.verify(frozen, plan_id=plan_id)
        return frozen

    def verify(self, snapshot: object, *, plan_id: str) -> Literal[True]:
        try:
            if (
                not isinstance(snapshot, FrozenSnapshot)
                or snapshot.plan_id != _plan_id(plan_id)
                or snapshot.source_root != str(self._root)
                or snapshot.status != "FROZEN_IN_MEMORY_NOT_COMMITTED"
            ):
                raise SnapshotVerificationError("snapshot binding or root mismatch")
            for entry in snapshot.files:
                if (
                    type(entry.content) is not bytes
                    or len(entry.content) != entry.size_bytes
                    or hashlib.sha256(entry.content).hexdigest() != entry.sha256
                ):
                    raise SnapshotVerificationError("frozen bytes do not match their digest/size")
            if _manifest(snapshot.files) != (snapshot.snapshot_id, snapshot.manifest_bytes):
                raise SnapshotVerificationError("snapshot identity or manifest mismatch")
            if not snapshot.source_files and not snapshot.source_directories:
                raise SnapshotVerificationError("snapshot has no source provenance")
            if self._capture(snapshot.source_files, snapshot.source_directories) != snapshot.files:
                raise SnapshotVerificationError("live source differs from frozen snapshot")
        except (OSError, ValueError, TypeError, AttributeError) as error:
            if isinstance(error, SnapshotVerificationError):
                raise
            raise SnapshotVerificationError(
                "snapshot source or integrity verification failed"
            ) from error
        return True
