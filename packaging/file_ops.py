"""UTF-8 file operations with component-aware optional root restrictions.

Relative paths are anchored to the first allowed root (or the construction-time
working directory when unrestricted). Configured roots must already exist;
trusted root aliases are canonicalized once, while in-tree symlinks are rejected.
Access uses POSIX no-follow directory descriptors. This is not an OS sandbox:
external directory moves/writers and process crashes require a stronger runtime.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
import fnmatch
import os
from pathlib import Path
import secrets
import stat
from threading import RLock
from typing import Any, NamedTuple
import unicodedata


def self_evaluate() -> dict[str, Any]:
    """Describe file-operation boundaries without inspecting any filesystem."""
    from datetime import UTC, datetime

    return {
        "module": "file_ops",
        "domain": "root-restricted UTF-8 file operations and V4A patch transactions",
        "current_version": "POSIX no-follow dir-fd access with staged writes and exception rollback",
        "known_limitations": [
            "Patch rollback provides exception atomicity, not crash durability or simultaneous multi-file visibility.",
            "Root restrictions are not an OS sandbox; external directory moves and concurrent writers remain unsupported.",
            "Content operations decode UTF-8 and read whole files into memory; binary and streaming edits are not supported.",
        ],
        "improvement_directions": [
            "Add a recoverable patch journal and directory fsync before claiming crash-safe multi-file publication.",
            "Add configurable file-size limits and bounded reads before supporting large-file workflows.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


class _Chunk(NamedTuple):
    context: str | None
    old: list[str]
    new: list[str]
    eof: bool


class _Operation(NamedTuple):
    kind: str
    path: str
    lines: list[str]
    chunks: list[_Chunk]
    move: str | None = None


class FileOps:
    """Read and modify regular files; an empty root list denies all access."""

    def __init__(self, allowed_roots: list[Path] | None = None):
        self._lock = RLock()
        self._cwd = Path.cwd()
        self._aliases: list[tuple[Path, Path]] = []
        self._identities: dict[Path, tuple[int, int]] = {}
        roots: list[Path] = []
        self.allowed_roots: list[Path] | None = None if allowed_roots is None else roots
        if allowed_roots is not None:
            for entry in allowed_roots:
                alias = Path(os.path.abspath(Path(entry).expanduser()))
                root = alias.resolve(strict=True)
                info = root.stat()
                if not stat.S_ISDIR(info.st_mode):
                    raise NotADirectoryError(str(root))
                roots.append(root)
                self._aliases.extend(((alias, root), (root, root)))
                self._identities[root] = (info.st_dev, info.st_ino)
        self._base = self.allowed_roots[0] if self.allowed_roots else self._cwd

    def _path(self, path: str) -> Path:
        raw = os.fspath(path)
        if type(raw) is not str or not raw or "\x00" in raw:
            raise ValueError("path must be a nonempty text path without NUL")
        candidate = Path(raw).expanduser()
        if self.allowed_roots is not None and ".." in candidate.parts:
            raise PermissionError(f"parent traversal is not allowed: {path}")
        if not candidate.is_absolute():
            candidate = self._base / candidate
        if self.allowed_roots is None:
            return candidate.resolve(strict=False)
        aliases = sorted(self._aliases, key=lambda pair: len(pair[0].parts), reverse=True)
        for alias, root in aliases:
            if candidate.is_relative_to(alias):
                target = root / candidate.relative_to(alias)
                resolved = target.resolve(strict=False)
                if not resolved.is_relative_to(root):
                    raise PermissionError(f"path escapes allowed root: {path}")
                # Keep the unresolved suffix: dir-fd access rejects in-tree
                # symlinks instead of following a checked-then-swapped pathname.
                return target
        raise PermissionError(f"path is outside allowed roots: {path}")

    @contextmanager
    def _directory(
        self, path: Path, create: bool = False, created: list[Path] | None = None
    ) -> Generator[int, None, None]:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(path.anchor, flags)
        current = Path(path.anchor)
        try:
            for part in path.parts[1:]:
                current = current / part
                try:
                    next_fd = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    if self.allowed_roots is not None and not any(
                        current != root and current.is_relative_to(root)
                        for root in self.allowed_roots
                    ):
                        raise PermissionError(f"allowed root or ancestor is missing: {current}")
                    try:
                        os.mkdir(part, dir_fd=fd)
                        if created is not None:
                            created.append(current)
                    except FileExistsError:
                        pass
                    next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
                expected = self._identities.get(current)
                info = os.fstat(fd)
                if expected is not None and (info.st_dev, info.st_ino) != expected:
                    raise PermissionError(f"allowed root was replaced: {current}")
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _regular(info: os.stat_result, path: Path) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"not a regular file (symlinks are not supported): {path}")

    @classmethod
    def _read_bytes(cls, fd: int, path: Path) -> tuple[bytes, os.stat_result]:
        before = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        cls._regular(before, path)
        handle = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(handle, "rb") as stream:
            info = os.fstat(stream.fileno())
            cls._regular(info, path)
            if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
                raise RuntimeError(f"file changed during open: {path}")
            data = stream.read()
            after = os.fstat(stream.fileno())
            if cls._signature(info) != cls._signature(after):
                raise RuntimeError(f"file changed during read: {path}")
            return data, after

    @staticmethod
    def _signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    @staticmethod
    def _stage(fd: int, data: bytes, mode: int) -> str:
        # mkstemp does not offer dir_fd, so use an unpredictable O_EXCL leaf.
        name = ".fileops-" + secrets.token_hex(16)
        handle = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), mode)
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(name, dir_fd=fd)
            raise
        return name

    def read_file(self, path: str, start_line: int = 1, end_line: int = 0) -> dict[str, Any]:
        """Read an inclusive, one-based line range, preserving line endings."""
        if type(start_line) is not int or type(end_line) is not int:
            raise ValueError("line numbers must be integers")
        if start_line < 1 or end_line < 0 or (end_line and end_line < start_line):
            raise ValueError("invalid line range")
        with self._lock:
            target = self._path(path)
            with self._directory(target.parent) as fd:
                data, _ = self._read_bytes(fd, target)
            lines = data.decode("utf-8").splitlines(keepends=True)
            stop = min(end_line or len(lines), len(lines))
            return {
                "path": str(target),
                "content": "".join(lines[start_line - 1 : stop]),
                "total_lines": len(lines),
                "start_line": start_line,
                "end_line": stop,
            }

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        """Atomically replace one file, creating missing parent directories."""
        data = content.encode("utf-8")
        with self._lock:
            target = self._path(path)
            with self._directory(target.parent, create=True) as fd:
                try:
                    info = os.stat(target.name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    mode = 0o644
                else:
                    self._regular(info, target)
                    mode = stat.S_IMODE(info.st_mode)
                staged = self._stage(fd, data, mode)
                try:
                    os.replace(staged, target.name, src_dir_fd=fd, dst_dir_fd=fd)
                finally:
                    try:
                        os.unlink(staged, dir_fd=fd)
                    except FileNotFoundError:
                        pass
            return {"path": str(target), "bytes": len(data)}

    def edit_file(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> dict[str, Any]:
        """Replace exact nonoverlapping occurrences; ambiguity is an error."""
        if not old_string:
            raise ValueError("old_string must not be empty")
        with self._lock:
            target = self._path(path)
            with self._directory(target.parent) as fd:
                data, info = self._read_bytes(fd, target)
                text = data.decode("utf-8")
                count = text.count(old_string)
                if not count or (count != 1 and not replace_all):
                    raise ValueError(f"old_string matched {count} times; expected a unique match")
                replacement = text.replace(old_string, new_string).encode("utf-8")
                staged = self._stage(fd, replacement, stat.S_IMODE(info.st_mode))
                try:
                    current = os.stat(target.name, dir_fd=fd, follow_symlinks=False)
                    if self._signature(current) != self._signature(info):
                        raise RuntimeError(f"file changed during edit: {target}")
                    os.replace(staged, target.name, src_dir_fd=fd, dst_dir_fd=fd)
                finally:
                    try:
                        os.unlink(staged, dir_fd=fd)
                    except FileNotFoundError:
                        pass
            return {"path": str(target), "replacements": count}

    def delete_file(self, path: str) -> dict[str, Any]:
        """Delete a regular file. A missing path is an idempotent no-op."""
        with self._lock:
            target = self._path(path)
            try:
                with self._directory(target.parent) as fd:
                    info = os.stat(target.name, dir_fd=fd, follow_symlinks=False)
                    self._regular(info, target)
                    os.unlink(target.name, dir_fd=fd)
            except FileNotFoundError:
                return {"path": str(target), "deleted": False}
            return {"path": str(target), "deleted": True}

    def list_dir(self, path: str, pattern: str = "*") -> list[dict[str, Any]]:
        """List sorted glob matches without following symlinks or special files.

        Names for nested patterns (``**/*.py``) are relative to the listed dir.
        """
        if type(pattern) is not str or not pattern or "\x00" in pattern:
            raise ValueError("pattern must be a nonempty glob")
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise PermissionError("glob pattern must stay inside the listed directory")
        parts = pattern.split("/")
        matches: dict[str, dict[str, Any]] = {}
        with self._lock:
            target = self._path(path)
            with self._directory(target) as root_fd:

                def visit(fd: int, prefix: str, index: int) -> None:
                    part = parts[index]
                    if part == "**" and index + 1 < len(parts):
                        visit(fd, prefix, index + 1)
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            info = entry.stat(follow_symlinks=False)
                            is_dir = stat.S_ISDIR(info.st_mode)
                            if not is_dir and not stat.S_ISREG(info.st_mode):
                                continue
                            name = prefix + entry.name
                            if part != "**" and not fnmatch.fnmatchcase(entry.name, part):
                                continue
                            if index == len(parts) - 1:
                                matches[name] = {
                                    "name": name,
                                    "type": "dir" if is_dir else "file",
                                    "size": info.st_size,
                                }
                            if is_dir and (part == "**" or index + 1 < len(parts)):
                                child = os.open(
                                    entry.name,
                                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=fd,
                                )
                                try:
                                    visit(child, name + "/", index if part == "**" else index + 1)
                                finally:
                                    os.close(child)

                visit(root_fd, "", 0)
        return [matches[name] for name in sorted(matches)]

    @staticmethod
    def _parse_patch(patch_text: str) -> list[_Operation]:
        lines = patch_text.strip("\r\n").split("\n")
        lines = [line[:-1] if line.endswith("\r") else line for line in lines]
        if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
            raise ValueError("patch must be enclosed by *** Begin Patch / *** End Patch")
        operations: list[_Operation] = []
        i = 1
        while i < len(lines) - 1:
            header = lines[i]
            kind = next(
                (
                    name
                    for name in ("Add", "Update", "Delete")
                    if header.startswith(f"*** {name} File: ")
                ),
                None,
            )
            if kind is None:
                raise ValueError(f"invalid operation at patch line {i + 1}: {header}")
            path = header.split(": ", 1)[1]
            if not path or path != path.strip():
                raise ValueError(f"invalid path in patch header: {header}")
            i += 1
            added: list[str] = []
            chunks: list[_Chunk] = []
            move = None
            if kind == "Update" and i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
                move = lines[i][len("*** Move to: ") :]
                if not move or move != move.strip():
                    raise ValueError("invalid move destination")
                i += 1
            if kind == "Add":
                while i < len(lines) - 1 and not lines[i].startswith("*** "):
                    if not lines[i].startswith("+"):
                        raise ValueError(f"added line must start with + at patch line {i + 1}")
                    added.append(lines[i][1:])
                    i += 1
            elif kind == "Update":
                while i < len(lines) - 1 and not lines[i].startswith("*** "):
                    header = lines[i]
                    if header != "@@" and not header.startswith("@@ "):
                        # Codex also accepts an initial context block without @@.
                        if chunks or not header or header[0] not in " +-":
                            raise ValueError(f"expected @@ hunk at patch line {i + 1}")
                        context = None
                    else:
                        context = None if header == "@@" else header[3:]
                        if context is not None and context.endswith(" @@"):
                            context = context[:-3]
                        i += 1
                    old: list[str] = []
                    new: list[str] = []
                    eof = False
                    while i < len(lines) - 1:
                        line = lines[i]
                        if line == "*** End of File":
                            eof = True
                            i += 1
                            break
                        if line.startswith("@@") or line.startswith("*** "):
                            break
                        if not line:  # An unprefixed blank is an empty context line.
                            old.append("")
                            new.append("")
                        elif line[0] == " ":
                            old.append(line[1:])
                            new.append(line[1:])
                        elif line[0] == "-":
                            old.append(line[1:])
                        elif line[0] == "+":
                            new.append(line[1:])
                        else:
                            raise ValueError(f"invalid hunk line {i + 1}: {line}")
                        i += 1
                    if not old and not new:
                        raise ValueError(f"empty update hunk for {path}")
                    chunks.append(_Chunk(context, old, new, eof))
                    if eof:
                        break
                if not chunks:
                    raise ValueError(f"update contains no hunks: {path}")
            operations.append(_Operation(kind.lower(), path, added, chunks, move))
        return operations

    @staticmethod
    def _find_lines(lines: list[str], wanted: list[str], start: int, eof: bool = False) -> int:
        if not wanted:
            return len(lines)
        last = len(lines) - len(wanted)
        positions = range(last, last + 1) if eof else range(start, last + 1)

        # Match exact text first, then the whitespace tolerance used by V4A.
        def exact(value: str) -> str:
            return value

        for normalize in (exact, str.rstrip, str.strip):
            expected = [normalize(line) for line in wanted]
            for pos in positions:
                if (
                    pos >= start
                    and [normalize(line) for line in lines[pos : pos + len(wanted)]] == expected
                ):
                    return pos
        return -1

    @classmethod
    def _updated_text(cls, text: str, chunks: list[_Chunk]) -> bytes:
        newline = "\r\n" if "\r\n" in text and text.count("\n") == text.count("\r\n") else "\n"
        lines = text.replace("\r\n", "\n").split("\n")
        if lines[-1] == "":
            lines.pop()
        output: list[str] = []
        cursor = 0
        for chunk in chunks:
            start = cursor
            if chunk.context:
                # A context header can either identify the line before a hunk
                # (canonical V4A) or the replaced line ("@@ context @@").
                context_at = cls._find_lines(lines, [chunk.context], cursor)
                if context_at < 0:
                    raise ValueError(f"context not found: {chunk.context}")
                start = context_at
                if not chunk.old or cls._find_lines(lines, chunk.old, start) != start:
                    start += 1
            old, new = chunk.old, chunk.new
            match = cls._find_lines(lines, old, start, chunk.eof)
            if match < 0 and old and old[-1] == "":
                # Accommodate a final blank context line at the physical EOF.
                old = old[:-1]
                if new and new[-1] == "":
                    new = new[:-1]
                match = cls._find_lines(lines, old, start, chunk.eof)
            if match < 0:
                raise ValueError(f"hunk context does not match: {chunk.old!r}")
            output.extend(lines[cursor:match])
            output.extend(new)
            cursor = match + len(old)
        output.extend(lines[cursor:])
        return ((newline.join(output) + newline) if output else "").encode("utf-8")

    def _snapshot(self, target: Path) -> tuple[bytes, os.stat_result] | None:
        try:
            with self._directory(target.parent) as fd:
                return self._read_bytes(fd, target)
        except FileNotFoundError:
            return None

    def _commit_patch(
        self,
        changes: dict[Path, bytes | None],
        snapshots: dict[Path, tuple[bytes, os.stat_result] | None],
        modes: dict[Path, int],
    ) -> list[str]:
        created: list[Path] = []
        staged: dict[Path, str] = {}
        backups: dict[Path, str] = {}
        committed: list[Path] = []
        errors: list[str] = []
        interrupted: BaseException | None = None
        with ExitStack() as stack:
            directories: dict[Path, int] = {}
            try:
                for target in changes:
                    if target.parent not in directories:
                        directories[target.parent] = stack.enter_context(
                            self._directory(target.parent, create=True, created=created)
                        )
                for target, data in changes.items():
                    fd = directories[target.parent]
                    snapshot = snapshots[target]
                    mode = modes.get(
                        target, stat.S_IMODE(snapshot[1].st_mode) if snapshot is not None else 0o644
                    )
                    if data is not None:
                        staged[target] = self._stage(fd, data, mode)
                    if snapshot is not None:
                        backups[target] = self._stage(fd, snapshot[0], mode)
                # No target is changed until every output and rollback copy is
                # fully staged, fsynced, and all input identities revalidated.
                for target, snapshot in snapshots.items():
                    fd = directories[target.parent]
                    try:
                        current = os.stat(target.name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        current = None
                    if (snapshot is None) != (current is None) or (
                        snapshot is not None
                        and current is not None
                        and self._signature(snapshot[1]) != self._signature(current)
                    ):
                        raise RuntimeError(f"file changed during patch: {target}")
                for target, data in changes.items():
                    fd = directories[target.parent]
                    if data is None:
                        os.unlink(target.name, dir_fd=fd)
                    else:
                        os.replace(staged[target], target.name, src_dir_fd=fd, dst_dir_fd=fd)
                    committed.append(target)
                # Cleanup is part of the transaction too. If it fails after an
                # earlier backup was removed, reconstruct that backup from the
                # immutable in-memory snapshot before rolling back.
                for mapping in (staged, backups):
                    for target, name in list(mapping.items()):
                        try:
                            os.unlink(name, dir_fd=directories[target.parent])
                        except FileNotFoundError:
                            pass
                        del mapping[target]
            except BaseException as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                if not isinstance(exc, Exception):
                    interrupted = exc
                for target in reversed(committed):
                    fd = directories[target.parent]
                    try:
                        snapshot = snapshots[target]
                        if snapshot is None:
                            os.unlink(target.name, dir_fd=fd)
                        else:
                            if target not in backups:
                                backups[target] = self._stage(
                                    fd, snapshot[0], stat.S_IMODE(snapshot[1].st_mode)
                                )
                            os.replace(backups[target], target.name, src_dir_fd=fd, dst_dir_fd=fd)
                    except Exception as rollback_exc:
                        backup = backups.pop(target, None)
                        errors.append(
                            f"rollback failed for {target}: {rollback_exc}; "
                            f"recovery copy: {target.parent / backup if backup else 'none'}"
                        )
            finally:
                for mapping in (staged, backups):
                    for target, name in mapping.items():
                        try:
                            os.unlink(name, dir_fd=directories[target.parent])
                        except FileNotFoundError:
                            pass
                        except OSError as exc:
                            errors.append(
                                f"cannot remove temporary file {target.parent / name}: {exc}"
                            )
                if errors:
                    for directory in reversed(created):
                        try:
                            with self._directory(directory.parent) as fd:
                                os.rmdir(directory.name, dir_fd=fd)
                        except OSError:
                            pass
        if interrupted is not None:
            interrupted.add_note("; ".join(errors))
            raise interrupted
        return errors

    def apply_patch(self, patch_text: str) -> dict[str, Any]:
        """Apply V4A Add/Update/Delete operations, including Move to and EOF.

        Failure returns empty success lists and errors. Staging + rollback gives
        exception atomicity, not a multi-path visibility/crash transaction. A
        concurrent external writer is unsupported; calls on this instance are
        serialized. Permission/IO failures during rollback are reported and the
        affected recovery copy is retained instead of claiming success.
        """
        result: dict[str, list[str]] = {"added": [], "updated": [], "deleted": [], "errors": []}
        with self._lock:
            try:
                operations = self._parse_patch(patch_text)
                changes: dict[Path, bytes | None] = {}
                snapshots: dict[Path, tuple[bytes, os.stat_result] | None] = {}
                modes: dict[Path, int] = {}
                seen_names: set[str] = set()
                seen_inodes: set[tuple[int, int]] = set()

                def reserve(raw: str) -> tuple[Path, tuple[bytes, os.stat_result] | None]:
                    target = self._path(raw)
                    # Conservatively reject case aliases even on case-sensitive
                    # disks, making behavior deterministic on default APFS.
                    key = unicodedata.normalize("NFC", str(target)).casefold()
                    if key in seen_names:
                        raise ValueError(f"duplicate patch target: {raw}")
                    seen_names.add(key)
                    snapshot = self._snapshot(target)
                    if snapshot is not None:
                        inode = (snapshot[1].st_dev, snapshot[1].st_ino)
                        if inode in seen_inodes:
                            raise ValueError(f"aliased patch target: {raw}")
                        seen_inodes.add(inode)
                    snapshots[target] = snapshot
                    return target, snapshot

                for operation in operations:
                    target, snapshot = reserve(operation.path)
                    if operation.kind == "add":
                        if snapshot is not None:
                            raise FileExistsError(str(target))
                        changes[target] = (
                            "\n".join(operation.lines) + "\n" if operation.lines else ""
                        ).encode("utf-8")
                    elif snapshot is None:
                        raise FileNotFoundError(str(target))
                    elif operation.kind == "delete":
                        changes[target] = None
                    else:
                        data = self._updated_text(snapshot[0].decode("utf-8"), operation.chunks)
                        if operation.move is not None:
                            destination, existing = reserve(operation.move)
                            if existing is not None:
                                raise FileExistsError(str(destination))
                            changes[target] = None
                            changes[destination] = data
                            modes[destination] = stat.S_IMODE(snapshot[1].st_mode)
                        else:
                            changes[target] = data
                errors = self._commit_patch(changes, snapshots, modes)
                if errors:
                    result["errors"] = errors
                    return result
                for operation in operations:
                    category = {"add": "added", "update": "updated", "delete": "deleted"}[
                        operation.kind
                    ]
                    result[category].append(operation.move or operation.path)
            except Exception as exc:
                result["errors"] = [f"{type(exc).__name__}: {exc}"]
        return result
