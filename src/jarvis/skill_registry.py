"""K4-002 Skill Registry DRAFT; NOT_FROZEN / NOT_APPROVED.

runtime_enabled=False. Explicit offline SQLite metadata only: install/upgrade
never fetch or execute artifacts, grant permissions or enable production skills.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import ClassVar, Self, cast

from jarvis.skill_manifest import SkillCapability, SkillManifest, compare

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False
SYSTEM_TAG = "SYSTEM"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*", re.ASCII)
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


def _name(name: str) -> None:
    if not isinstance(cast(object, name), str) or _NAME.fullmatch(name) is None:
        raise ValueError("skill name must be an exact ASCII identifier")


def _dependency(value: str) -> tuple[str, list[tuple[str, str]]]:
    """Bare name, name@version, or name[ @ ]>=version,<version; no resolution."""
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
        compare(version, version)  # Validate the exact token before looking anything up.
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
    candidate: object = manifest
    if not isinstance(cast(object, candidate), SkillManifest):
        raise TypeError("manifest must be SkillManifest")
    # K4-001 owns validation/checksum rules; replace revalidates mutable containers
    # and takes a detached snapshot without resealing stale caller checksums.
    snapshot = replace(candidate)
    _name(snapshot.name)
    names = [_dependency(value)[0] for value in snapshot.dependencies]
    if len(names) != len(set(names)):
        raise ValueError("duplicate dependency name")
    data = asdict(snapshot)
    data["capabilities"] = sorted(item.value for item in snapshot.capabilities)
    return json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False)


class SkillRegistry(AbstractContextManager["SkillRegistry"]):
    """Standalone offline registry with immutable version payloads and soft deletion.

    Only one active version per name; explicit-version reads include history.
    SYSTEM is an exact manifest tag and is sticky across upgrades/reinstallation.
    It is an administrative fixture label, not trusted identity or authorization.
    SQLite serializes read/check/write transactions across registry instances;
    each connection is thread-affine. No dependency downloading or code rollback:
    failures roll back only this database transaction, not external side effects.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(self, path: str | Path, *, draft_fixture: bool = False) -> None:
        if draft_fixture is not True:
            raise ValueError("explicit offline draft_fixture=True required")
        if (
            not isinstance(cast(object, path), (str, Path))
            or not str(path).strip()
            or "\x00" in str(path)
        ):
            raise ValueError("path must be nonblank text or Path without NUL")
        self._connection = sqlite3.connect(path, isolation_level=None)
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
                    raise ValueError("not a compatible standalone skill registry draft database")
        except BaseException:
            self.close()
            raise

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
        data = json.loads(payload)
        data["capabilities"] = {SkillCapability(item) for item in data["capabilities"]}
        return SkillManifest(**data)

    def _validate_graph(self) -> None:
        installed = {item.name: item for item in self.list_installed()}
        graph: dict[str, list[str]] = {}
        for item in installed.values():
            graph[item.name] = []
            for declaration in item.dependencies:
                name, clauses = _dependency(declaration)
                if name not in installed or not _satisfies(installed[name].version, clauses):
                    raise ValueError(f"unsatisfied dependency {declaration!r} for {item.name!r}")
                graph[item.name].append(name)
        # Iterative topological elimination avoids recursion limits on long chains.
        remaining = {name: set(dependencies) for name, dependencies in graph.items()}
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
        """Install metadata or reactivate identical uninstalled historical metadata."""
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
        """Switch metadata atomically; default remains strictly-newer-only.

        Explicit offline rollback may reactivate an identical, already recorded
        older manifest. It cannot introduce an unseen version or rewrite history;
        dependency and SYSTEM checks remain unchanged. This is not a runtime grant.
        """
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
        _name(name)
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
        """Detached metadata; omitted version reads active only, missing raises KeyError."""
        _name(name)
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
        """Return detached active manifests in exact-name sort order."""
        return [
            self._decode(row[0])
            for row in self._connection.execute(
                "SELECT manifest FROM skill_versions WHERE active = 1 ORDER BY name"
            )
        ]

    def dependency_graph(self) -> dict[str, list[str]]:
        """Active skill -> sorted direct dependency names, including leaf nodes."""
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
