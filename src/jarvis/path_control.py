"""K0-009 path/permission DRAFT; NOT_APPROVED / NOT_FROZEN, runtime disabled.

FS-020..026: deterministic policy preflight, NOT a safe read/write/execute API.
Checks use nofollow directory-fd traversal, not realpath alone. A returned Path
is not an I/O capability: a later pathname open still has a TOCTOU window. Future
snapshot/publisher adapters must retain controlled fds through the actual I/O.
Nothing here runs a command, writes a target or grants a Capability/Guard fact.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False

type PathInput = str | os.PathLike[str]
type Identity = tuple[int, int]


class PathControlError(PermissionError):
    """Invalid, unavailable or unauthorized paths fail closed."""


class PlatformCapabilityError(PathControlError):
    """Required nofollow/dir-fd primitives are unavailable; no unsafe fallback."""


class Permission(StrEnum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


def _permission(value: object) -> Permission:
    if not isinstance(value, str):
        raise PathControlError("permission must be read, write or execute")
    try:
        return Permission(value)
    except ValueError as error:
        raise PathControlError("unknown permission") from error


def _path(value: PathInput) -> Path:
    """Validate the raw spelling BEFORE Path/realpath can erase components."""
    try:
        raw = cast("object", os.fspath(value))
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise PathControlError("a nonempty UTF-8 path string is required")
        raw.encode("utf-8", errors="strict")
        if not raw.startswith("/"):
            raise PathControlError("absolute paths are required in this draft")
        if raw != "/" and any(part in {"", ".", ".."} for part in raw[1:].split("/")):
            raise PathControlError("empty, dot and parent path components are forbidden")
        return Path(raw)
    except (TypeError, UnicodeError, ValueError) as error:
        raise PathControlError("invalid path encoding or type") from error


def _require_platform() -> None:
    if (
        os.name != "posix"
        or not getattr(os, "O_NOFOLLOW", 0)
        or not getattr(os, "O_DIRECTORY", 0)
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        raise PlatformCapabilityError("nofollow, directory fds and nofollow stat are required")


def _identity(info: os.stat_result) -> Identity:
    return info.st_dev, info.st_ino


@dataclass(frozen=True)
class _Inspection:
    path: Path
    directories: Mapping[Path, Identity]


def _inspect(path: Path, *, allow_missing_leaf: bool = False) -> _Inspection:
    """Inspect with held directory fds; never open leaf contents or create files.

    Rechecking bindings detects ordinary replacements during validation. This is
    not isolation from a malicious same-UID process (FS-003), nor future I/O safety.
    """
    _require_platform()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    directories: dict[Path, Identity] = {}
    bindings: list[tuple[int, str, os.stat_result]] = []
    missing_leaf: tuple[int, str] | None = None
    try:
        with ExitStack() as stack:
            fd = os.open("/", flags)
            stack.callback(os.close, fd)
            current = Path("/")
            directories[current] = _identity(os.fstat(fd))
            components = path.parts[1:]
            for index, name in enumerate(components):
                last = index == len(components) - 1
                try:
                    observed = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    if last and allow_missing_leaf:
                        missing_leaf = fd, name
                        break
                    raise
                if stat.S_ISLNK(observed.st_mode):
                    raise PathControlError("symbolic links are forbidden in every component")
                if not (stat.S_ISREG(observed.st_mode) or stat.S_ISDIR(observed.st_mode)):
                    raise PathControlError("special files are forbidden")
                bindings.append((fd, name, observed))
                current /= name
                if stat.S_ISDIR(observed.st_mode):
                    child_fd = os.open(name, flags, dir_fd=fd)
                    stack.callback(os.close, child_fd)
                    opened = os.fstat(child_fd)
                    if not stat.S_ISDIR(opened.st_mode) or _identity(opened) != _identity(observed):
                        raise PathControlError("directory changed during validation")
                    fd = child_fd
                    directories[current] = _identity(opened)
                elif not last:
                    raise PathControlError("a non-directory cannot be a path ancestor")
            resolved = Path(os.path.realpath(path, strict=not allow_missing_leaf))
            if resolved != path:
                raise PathControlError("canonical path changed; symbolic-link alias refused")
            for parent_fd, name, observed in bindings:
                checked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if _identity(checked) != _identity(observed) or stat.S_IFMT(
                    checked.st_mode
                ) != stat.S_IFMT(observed.st_mode):
                    raise PathControlError("path binding changed during validation")
            if missing_leaf is not None:
                parent_fd, name = missing_leaf
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise PathControlError("missing target appeared during validation")
            return _Inspection(resolved, directories)
    except OSError as error:
        if isinstance(error, PathControlError):
            raise
        raise PathControlError("path could not be safely inspected") from error
    except (NotImplementedError, TypeError) as error:
        raise PlatformCapabilityError("required safe traversal operation is unsupported") from error


@dataclass(frozen=True)
class _Root:
    path: Path
    identity: Identity


@dataclass(frozen=True)
class _Rule:
    path: Path
    permissions: frozenset[Permission]


class PathGuard:
    """Immutable draft policy from TRUSTED bootstrap configuration, not model args.

    Roots must exist, be directories and have no symlink ancestors. No permissions
    supplied means read-only roots; an explicit mapping defaults to deny. Longest
    component prefix wins, including an empty set that denies a subtree. Grants
    are independent: write does not imply read/execute; execute never runs code
    and is not K0 permission for arbitrary shell execution (FS-004).

    Absolute paths only. Use raw strings at untrusted boundaries: Path objects
    may already have erased '.'/empty components. No expansion or URL decoding.
    """

    def __init__[Prefix: PathInput](
        self,
        allowed_roots: Iterable[PathInput],
        *,
        permissions: Mapping[Prefix, Iterable[str]] | None = None,
    ) -> None:
        _require_platform()
        if isinstance(allowed_roots, (str, bytes, os.PathLike)):
            raise PathControlError("allowed_roots must be a collection of explicit directories")
        roots: dict[Path, _Root] = {}
        for value in allowed_roots:
            path = _path(value)
            inspected = _inspect(path)
            identity = inspected.directories.get(path)
            if identity is None:
                raise PathControlError("allowed root must be an existing directory")
            roots[path] = _Root(path, identity)
        if not roots:
            raise PathControlError("at least one authorized root is required")
        self._roots = tuple(
            sorted(roots.values(), key=lambda root: len(root.path.parts), reverse=True)
        )
        rules: list[_Rule] = []
        if permissions is None:
            rules = [_Rule(root.path, frozenset({Permission.READ})) for root in self._roots]
        else:
            for value, grants in permissions.items():
                path = _path(value)
                self._check_root(_inspect_after_containment(path, self._roots))
                if isinstance(grants, (str, bytes)):
                    raise PathControlError("permissions must be a collection, not a string")
                rules.append(_Rule(path, frozenset(_permission(grant) for grant in grants)))
        self._rules = tuple(sorted(rules, key=lambda rule: len(rule.path.parts), reverse=True))

    def _check_root(self, inspected: _Inspection) -> None:
        root = _select_root(inspected.path, self._roots)
        if inspected.directories.get(root.path) != root.identity:
            raise PathControlError("authorized root identity changed")

    def resolve_and_check(self, path: PathInput) -> Path:
        """Return canonical existing path within roots; NOT an operation grant."""
        inspected = _inspect_after_containment(_path(path), self._roots)
        self._check_root(inspected)
        return inspected.path

    def check_permission(self, path: PathInput, permission: str) -> Path:
        """Validate explicit operation policy; write permits only a missing leaf.

        No filesystem operation is performed on behalf of the caller. OS access
        bits, capability binding, hashes and cross-filesystem publish rules are
        separate checks required of future operation adapters.
        """
        operation = _permission(permission)
        target = _path(path)
        _select_root(target, self._roots)  # Never traverse unauthorized trees.
        rule = next((rule for rule in self._rules if target.is_relative_to(rule.path)), None)
        if rule is None or operation not in rule.permissions:
            raise PathControlError("operation is not granted for this path")
        inspected = _inspect(target, allow_missing_leaf=operation is Permission.WRITE)
        self._check_root(inspected)
        return inspected.path


def _select_root(path: Path, roots: tuple[_Root, ...]) -> _Root:
    for root in roots:
        if path.is_relative_to(root.path):
            return root
    raise PathControlError("path is outside the authorized roots")


def _inspect_after_containment(path: Path, roots: tuple[_Root, ...]) -> _Inspection:
    _select_root(path, roots)
    return _inspect(path)
