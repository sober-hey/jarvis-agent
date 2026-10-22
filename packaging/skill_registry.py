"""Persistent runtime skill metadata registry with atomic dependency checks.

Installation registers metadata only; it never downloads or executes skill code.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Self, cast

if __package__:
    from .skill_manifest import SkillManifest, compare, validate_name
else:
    from skill_manifest import SkillManifest, compare, validate_name

RUNTIME_ENABLED = True
SYSTEM_TAG = "SYSTEM"
_DEPENDENCY = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)(.*)", re.ASCII)
_CLAUSE = re.compile(r"(==|!=|>=|<=|>|<)?\s*(\S+)", re.ASCII)
_SCHEMA = {
    "skill_registry_schema": (
        "CREATE TABLE skill_registry_schema ("
        "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
        "version INTEGER NOT NULL CHECK(version = 1))"
    ),
    "skill_versions": (
        "CREATE TABLE skill_versions ("
        "name TEXT NOT NULL, version TEXT NOT NULL, "
        "manifest TEXT NOT NULL CHECK(json_valid(manifest)), "
        "active INTEGER NOT NULL CHECK(active IN (0, 1)), "
        "deleted INTEGER NOT NULL CHECK(deleted IN (0, 1)), "
        "CHECK(NOT(active = 1 AND deleted = 1)), PRIMARY KEY(name, version))"
    ),
    "one_active_skill": (
        "CREATE UNIQUE INDEX one_active_skill ON skill_versions(name) WHERE active = 1"
    ),
}


def self_evaluate() -> dict[str, object]:
    """Public registry algorithm/limits only; never open the installed skill DB."""
    return {
        "module": "skill_registry",
        "domain": "persistent skill metadata dependency graph validation upgrades and rollback",
        "current_version": "SQLite schema-v1 single-active-version registry with atomic dependency checks and immutable history",
        "known_limitations": [
            "Transactions cover metadata only, not downloaded code, filesystem installation or worker lifecycle.",
            "Connections are thread-affine; one registry instance cannot be shared freely across worker threads.",
            "Opening requires the exact standalone schema-v1 layout; no schema migration path is implemented.",
            "SYSTEM is a sticky metadata tag, not verified author identity or an execution permission grant.",
        ],
        "improvement_directions": [
            "Coordinate package filesystem and worker lifecycle changes with explicit recovery/reconciliation.",
            "Provide a documented per-thread connection strategy and tested versioned schema migrations.",
            "Keep trusted provenance and execution authorization separate from manifest tags.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


def _dependency(value: str) -> tuple[str, list[tuple[str, str]]]:
    """Bare name, name@version, or name[ @ ]>=version,<version; no fetching."""
    match = _DEPENDENCY.fullmatch(value) if isinstance(cast(object, value), str) else None
    if match is None:
        raise ValueError("invalid dependency name or requirement")
    name, requirement = match.groups()
    requirement = requirement.strip()
    if requirement.startswith("@"):
        requirement = requirement[1:].strip()
        if not requirement:
            raise ValueError("dependency @ requires a version requirement")
    if not requirement or requirement == "*":
        return name, []
    clauses: list[tuple[str, str]] = []
    for text in requirement.split(","):
        clause = _CLAUSE.fullmatch(text.strip())
        if clause is None:
            raise ValueError("unsupported dependency version requirement")
        operator, version = clause.groups()
        compare(version, version)
        clauses.append((operator or "==", version))
    return name, clauses


def _satisfies(version: str, clauses: list[tuple[str, str]]) -> bool:
    for operator, target in clauses:
        order = compare(version, target)
        if not {
            "==": order == 0,
            "!=": order != 0,
            ">=": order >= 0,
            "<=": order <= 0,
            ">": order > 0,
            "<": order < 0,
        }[operator]:
            return False
    return True


def _encode(manifest: SkillManifest) -> str:
    if type(manifest) is not SkillManifest:
        raise TypeError("manifest must be SkillManifest")
    snapshot = replace(manifest)
    names = [_dependency(value)[0] for value in snapshot.dependencies]
    if len(names) != len(set(names)):
        raise ValueError("duplicate dependency name")
    return json.dumps(snapshot.to_dict(), sort_keys=True, ensure_ascii=False, allow_nan=False)


def _check_path(path: Path) -> None:
    """Reject symlinks in the database path, including parent components."""
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise ValueError(f"symlink database path is not allowed: {component}")
    for suffix in ("-journal", "-wal", "-shm"):
        if Path(str(path) + suffix).is_symlink():
            raise ValueError("symlink database sidecar is not allowed")


class SkillRegistry(AbstractContextManager["SkillRegistry"]):
    """Single-active-version registry, immutable history and soft deletion.

    SYSTEM is an exact sticky tag, not trusted identity or a sandbox grant.
    Each connection is thread-affine; SQLite serializes read/check/write across
    instances. Transactions cover metadata only, not external filesystem effects.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    @staticmethod
    def self_evaluate() -> dict[str, object]:
        """No connection, path discovery or registry statistics are required."""
        return self_evaluate()

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            path = Path.home() / ".jarvis" / "skills" / "registry.db"
        if (
            not isinstance(cast(object, path), (str, Path))
            or not str(path).strip()
            or "\x00" in str(path)
            or str(path) == ":memory:"
        ):
            raise ValueError("path must identify a disk database")
        self.path = Path(os.path.abspath(Path(path).expanduser()))
        _check_path(self.path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_path(self.path)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            identity = os.fstat(fd)
            if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
                raise ValueError("database must be a regular file without hard links")
            _check_path(self.path)
            current = self.path.stat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise ValueError("database path changed while opening")
            self._connection = sqlite3.connect(self.path, isolation_level=None)
            try:
                with self._transaction():
                    objects = dict(
                        self._connection.execute(
                            "SELECT name, sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
                        )
                    )
                    if not objects:
                        for statement in _SCHEMA.values():
                            self._connection.execute(statement)
                        self._connection.execute("INSERT INTO skill_registry_schema VALUES(1, 1)")
                    elif objects != _SCHEMA or self._connection.execute(
                        "SELECT singleton, version FROM skill_registry_schema"
                    ).fetchall() != [(1, 1)]:
                        raise ValueError("not a compatible standalone skill registry database")
                    os.fchmod(fd, 0o600)
            except BaseException:
                self.close()
                raise
        finally:
            os.close(fd)

    @contextmanager
    def _transaction(self) -> Generator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    @staticmethod
    def _decode(payload: str) -> SkillManifest:
        return SkillManifest.from_dict(json.loads(payload), require_checksum=True)

    def _validate_graph(self) -> None:
        installed = {item.name: item for item in self.list_installed()}
        graph: dict[str, set[str]] = {}
        for item in installed.values():
            graph[item.name] = set()
            for declaration in item.dependencies:
                name, clauses = _dependency(declaration)
                if name not in installed or not _satisfies(installed[name].version, clauses):
                    raise ValueError(f"unsatisfied dependency {declaration!r} for {item.name!r}")
                graph[item.name].add(name)
        remaining = graph
        while remaining:
            leaves = {name for name, dependencies in remaining.items() if not dependencies}
            if not leaves:
                raise ValueError("dependency cycle")
            remaining = {
                name: dependencies - leaves
                for name, dependencies in remaining.items()
                if name not in leaves
            }

    def _put(self, manifest: SkillManifest, payload: str) -> None:
        row = self._connection.execute(
            "SELECT manifest FROM skill_versions WHERE name = ? AND version = ?",
            (manifest.name, manifest.version),
        ).fetchone()
        if row is not None:
            if row[0] != payload:
                raise ValueError("immutable version already exists with different manifest")
            self._connection.execute(
                "UPDATE skill_versions SET active = 1, deleted = 0 WHERE name = ? AND version = ?",
                (manifest.name, manifest.version),
            )
        else:
            self._connection.execute(
                "INSERT INTO skill_versions(name, version, manifest, active, deleted) "
                "VALUES(?, ?, ?, 1, 0)",
                (manifest.name, manifest.version, payload),
            )

    def _locked(self, name: str) -> bool:
        return any(
            SYSTEM_TAG in self._decode(row[0]).tags
            for row in self._connection.execute(
                "SELECT manifest FROM skill_versions WHERE name = ?", (name,)
            )
        )

    def install(self, manifest: SkillManifest) -> None:
        """Install metadata, or reactivate identical uninstalled historical metadata."""
        payload = _encode(manifest)
        snapshot = self._decode(payload)
        with self._transaction():
            if any(item.name == snapshot.name for item in self.list_installed()):
                raise ValueError("skill is already installed; use upgrade")
            if self._locked(snapshot.name) and SYSTEM_TAG not in snapshot.tags:
                raise PermissionError("SYSTEM lock cannot be removed")
            self._put(snapshot, payload)
            self._validate_graph()

    def upgrade(self, manifest: SkillManifest, *, allow_rollback: bool = False) -> None:
        """Strictly newer by default; opt-in rollback requires identical recorded history."""
        if type(allow_rollback) is not bool:
            raise ValueError("allow_rollback must be an exact bool")
        payload = _encode(manifest)
        snapshot = self._decode(payload)
        with self._transaction():
            old = self.get(snapshot.name)
            if old.id != snapshot.id:
                raise ValueError("skill id cannot change during upgrade")
            order = compare(snapshot.version, old.version)
            if order <= 0:
                if not allow_rollback or order == 0:
                    raise ValueError("upgrade requires a strictly newer version")
                historical = self.get(snapshot.name, snapshot.version)
                if _encode(historical) != payload:
                    raise ValueError("rollback requires an identical historical manifest")
            if self._locked(snapshot.name) and SYSTEM_TAG not in snapshot.tags:
                raise PermissionError("SYSTEM lock cannot be removed")
            self._connection.execute(
                "UPDATE skill_versions SET active = 0 WHERE name = ? AND active = 1",
                (snapshot.name,),
            )
            self._put(snapshot, payload)
            self._validate_graph()

    def uninstall(self, name: str) -> None:
        """Soft-delete active metadata; block SYSTEM and reverse dependencies."""
        validate_name(name)
        with self._transaction():
            self.get(name)
            if self._locked(name):
                raise PermissionError("SYSTEM skill cannot be uninstalled")
            self._connection.execute(
                "UPDATE skill_versions SET active = 0, deleted = 1 WHERE name = ? AND active = 1",
                (name,),
            )
            self._validate_graph()

    def get(self, name: str, version: str | None = None) -> SkillManifest:
        """Detached active or exact historical metadata; missing raises KeyError."""
        validate_name(name)
        if version is None:
            row = self._connection.execute(
                "SELECT manifest FROM skill_versions WHERE name = ? AND active = 1", (name,)
            ).fetchone()
        else:
            compare(version, version)
            row = self._connection.execute(
                "SELECT manifest FROM skill_versions WHERE name = ? AND version = ?",
                (name, version),
            ).fetchone()
        if row is None:
            raise KeyError((name, version))
        return self._decode(row[0])

    def list_installed(self) -> list[SkillManifest]:
        """Detached active manifests in exact-name sort order."""
        return [
            self._decode(row[0])
            for row in self._connection.execute(
                "SELECT manifest FROM skill_versions WHERE active = 1 ORDER BY name"
            )
        ]

    def dependency_graph(self) -> dict[str, list[str]]:
        """Active name -> sorted direct dependency names, including leaves."""
        return {
            item.name: sorted(_dependency(value)[0] for value in item.dependencies)
            for item in self.list_installed()
        }

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
