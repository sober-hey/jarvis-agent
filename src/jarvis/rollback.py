"""K4-010 Rollback DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

Offline SkillRegistry metadata only, never Git checkout or production deployment.
A durable PENDING intent precedes a registry switch. The independent registry and
ledger do NOT form an atomic transaction: interrupted/uncertain operations block
new writes until external reconciliation; they are never automatically replayed.
SHA fields are optional VersionManager observations, not measured Git state.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import time_ns
from typing import ClassVar, Self, cast
from uuid import uuid4

from jarvis.skill_manifest import SkillManifest, compare
from jarvis.skill_registry import SkillRegistry
from jarvis.version_manager import VersionManager

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


def _text(name: str, value: object) -> None:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be nonblank UTF-8 text without NUL")
    value.encode("utf-8", errors="strict")


def _timestamp(value: object) -> None:
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("timestamp must be a nonnegative UTC epoch millisecond integer")


@dataclass(frozen=True, slots=True)
class RollbackRecord:
    """Actual metadata switch observation; timestamp is UTC epoch milliseconds."""

    release_id: str
    component: str
    from_version: str
    to_version: str
    reason: str
    timestamp: int
    git_sha_before: str | None = None
    git_sha_after: str | None = None

    def __post_init__(self) -> None:
        for name in ("release_id", "component", "reason"):
            _text(name, getattr(self, name))
        compare(self.from_version, self.to_version)
        _timestamp(self.timestamp)
        for value in (self.git_sha_before, self.git_sha_after):
            if value is not None and (
                type(value) is not str
                or re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", value) is None
            ):
                raise ValueError("Git SHA must be a complete hexadecimal SHA or None")


_SQL = (
    "CREATE TABLE draft_release_schema (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "version INTEGER NOT NULL CHECK(version=1))",
    "CREATE TABLE draft_rollbacks (sequence INTEGER PRIMARY KEY, "
    "release_id TEXT NOT NULL UNIQUE, component TEXT NOT NULL, from_version TEXT NOT NULL, "
    "to_version TEXT NOT NULL, reason TEXT NOT NULL, "
    "timestamp INTEGER NOT NULL CHECK(timestamp>=0), "
    "git_sha_before TEXT, git_sha_after TEXT, result TEXT NOT NULL "
    "CHECK(result IN ('PENDING','APPLIED','SUCCESS','FAILED','UNKNOWN','VERIFICATION_FAILED')), "
    "error TEXT)",
    "CREATE TABLE draft_releases (sequence INTEGER PRIMARY KEY, release_id TEXT NOT NULL UNIQUE, "
    "component TEXT NOT NULL, version TEXT NOT NULL, previous_version TEXT NOT NULL, "
    "manifest TEXT NOT NULL CHECK(json_valid(manifest)), git_sha TEXT NOT NULL, "
    "phase TEXT NOT NULL CHECK(phase IN ('CANARY','STAGED','FULL')), "
    "result TEXT NOT NULL CHECK(result IN ('PENDING','SUCCESS','FAILED','UNKNOWN','ROLLED_BACK')), "
    "created_at INTEGER NOT NULL CHECK(created_at>=0), updated_at INTEGER NOT NULL "
    "CHECK(updated_at>=0), error TEXT)",
    "CREATE TABLE draft_release_events (sequence INTEGER PRIMARY KEY, "
    "release_id TEXT NOT NULL REFERENCES draft_releases(release_id), "
    "phase TEXT NOT NULL CHECK(phase IN ('CANARY','STAGED','FULL')), "
    "timestamp INTEGER NOT NULL CHECK(timestamp>=0), result TEXT NOT NULL "
    "CHECK(result IN ('PENDING','SUCCESS','FAILED','UNKNOWN','ROLLED_BACK')), error TEXT)",
)
_EXPECTED = {statement.split()[2]: statement for statement in _SQL}
_COLUMNS = (
    "release_id,component,from_version,to_version,reason,timestamp,git_sha_before,git_sha_after"
)


class _Ledger:
    """Shared standalone release/rollback candidate ledger, thread-affine."""

    def __init__(self, database: str | Path) -> None:
        if not isinstance(cast(object, database), (str, Path)):
            raise ValueError("database must be a path")
        _text("database", str(database))
        if str(database) == ":memory:":
            raise ValueError("draft release ledger requires a file-backed database")
        self.connection = sqlite3.connect(database, isolation_level=None)
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA synchronous=FULL")
            with self.transaction():
                existing = dict(
                    self.connection.execute(
                        "SELECT name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
                    )
                )
                if not existing:
                    for statement in _SQL:
                        self.connection.execute(statement)
                    self.connection.execute("INSERT INTO draft_release_schema VALUES(1,1)")
                elif existing != _EXPECTED:
                    raise ValueError("requires standalone matching release/rollback draft database")
                if self.connection.execute("SELECT * FROM draft_release_schema").fetchall() != [
                    (1, 1)
                ]:
                    raise ValueError("draft release schema version mismatch")
        except BaseException:
            self.connection.close()
            raise

    @contextmanager
    def transaction(self) -> Generator[None, None, None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except BaseException:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise

    def check_pending(self, component: str) -> None:
        if self.connection.execute(
            "SELECT 1 FROM draft_rollbacks WHERE component=? "
            "AND result IN ('PENDING','APPLIED','UNKNOWN','VERIFICATION_FAILED')",
            (component,),
        ).fetchone():
            raise RuntimeError("unresolved rollback; reconciliation required")


class RollbackManager:
    """Single trusted offline coordinator; caller supplies actual fixture tests.

    No default passing verifier. execute() returns only after exact version readback
    and verifier=True. Failed post-switch tests are persisted and raise; history
    includes these actual switches so an unsuccessful verification is not hidden.
    Caller owns registry/version manager; close() closes only the candidate ledger.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED
    draft_status: ClassVar[str] = DRAFT_STATUS

    def __init__(
        self,
        database: str | Path,
        registry: SkillRegistry,
        *,
        verifier: Callable[[SkillManifest], bool] | None = None,
        versions: VersionManager | None = None,
        clock: Callable[[], int] | None = None,
        draft_fixture: bool = False,
    ) -> None:
        if draft_fixture is not True:
            raise ValueError("explicit offline draft_fixture=True required")
        self._registry = registry
        self._verifier = verifier
        self._versions = versions
        self._clock = clock or (lambda: time_ns() // 1_000_000)
        self._ledger = _Ledger(database)

    def _now(self) -> int:
        now = self._clock()
        _timestamp(now)
        return now

    def _sha(self, component: str, version: str) -> str | None:
        if self._versions is None:
            return None
        return next(
            (r.git_sha for r in self._versions.history(component) if r.version.to_str() == version),
            None,
        )

    def execute(
        self,
        component: str,
        target_version: str,
        *,
        release_id: str | None = None,
        reason: str = "explicit offline rollback",
    ) -> RollbackRecord:
        _text("component", component)
        _text("reason", reason)
        compare(target_version, target_version)
        if self._verifier is None:
            raise PermissionError("rollback requires an explicit test verifier")
        ledger = self._ledger
        with ledger.transaction():
            ledger.check_pending(component)
            old = self._registry.get(component)
            target = self._registry.get(component, target_version)
            if compare(target_version, old.version) >= 0:
                raise ValueError("rollback target must be an older recorded version")
            record = RollbackRecord(
                str(uuid4()) if release_id is None else release_id,
                component,
                old.version,
                target_version,
                reason,
                self._now(),
                self._sha(component, old.version),
                self._sha(component, target_version),
            )
            ledger.connection.execute(
                f"INSERT INTO draft_rollbacks ({_COLUMNS},result) "
                "VALUES(?,?,?,?,?,?,?,?,'PENDING')",
                (
                    record.release_id,
                    record.component,
                    record.from_version,
                    record.to_version,
                    record.reason,
                    record.timestamp,
                    record.git_sha_before,
                    record.git_sha_after,
                ),
            )
        try:
            # The public registry gate preserves immutable history/dependency checks.
            self._registry.upgrade(target, allow_rollback=True)
        except Exception as exc:
            # Custom registries may commit then raise: never assume failure means no effect.
            self._result(record.release_id, "UNKNOWN", str(exc))
            raise
        self._result(record.release_id, "APPLIED", None)
        try:
            passed = self._verifier(target) is True
            valid = passed and self._registry.get(component) == target
        except Exception as exc:
            self._result(record.release_id, "VERIFICATION_FAILED", str(exc))
            raise RuntimeError("rollback tests failed after metadata switch") from exc
        self._result(record.release_id, "SUCCESS" if valid else "VERIFICATION_FAILED", None)
        if not valid:
            raise RuntimeError("rollback version or tests failed after metadata switch")
        return record

    def _result(self, release_id: str, result: str, error: str | None) -> None:
        with self._ledger.transaction():
            self._ledger.connection.execute(
                "UPDATE draft_rollbacks SET result=?,error=? WHERE release_id=?",
                (result, error, release_id),
            )

    def history(self, component: str) -> list[RollbackRecord]:
        _text("component", component)
        return [
            RollbackRecord(*row)
            for row in self._ledger.connection.execute(
                f"SELECT {_COLUMNS} FROM draft_rollbacks WHERE component=? "
                "AND result IN ('SUCCESS','VERIFICATION_FAILED') ORDER BY sequence",
                (component,),
            )
        ]

    def verify(self, component: str) -> bool:
        _text("component", component)
        row = self._ledger.connection.execute(
            "SELECT to_version,result FROM draft_rollbacks WHERE component=? "
            "ORDER BY sequence DESC LIMIT 1",
            (component,),
        ).fetchone()
        if (
            row is None
            or row[1] not in {"SUCCESS", "VERIFICATION_FAILED"}
            or self._verifier is None
        ):
            return False
        try:
            current = self._registry.get(component)
            if current.version != row[0]:
                return False
            passed = self._verifier(current) is True
            return passed and self._registry.get(component) == current
        except Exception:
            return False

    def close(self) -> None:
        self._ledger.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
