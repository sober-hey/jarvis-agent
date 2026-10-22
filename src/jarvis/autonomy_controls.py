"""K3-007 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

Offline behavior-type policy only, not an execution grant or approval bypass.
Defaults/overrides/classification must come from a trusted owner, not model tools.
No budget, ApprovalGate, scheduler, Worker, product DB or packaging/ wiring.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import ClassVar

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


class ActionClass(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    NOTIFY = "NOTIFY"
    EXTERNAL = "EXTERNAL"
    DANGEROUS = "DANGEROUS"


class PermissionMode(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class AutonomyControlError(RuntimeError):
    """Fixture/database/clock precondition failed; caller must not execute."""


def _action(value: ActionClass) -> None:
    if type(value) is not ActionClass:
        raise ValueError("action_class must be ActionClass, never an untrusted label")


def _mode(value: PermissionMode) -> None:
    if type(value) is not PermissionMode:
        raise ValueError("mode must be PermissionMode")


def _timestamp(value: int) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("timestamp must be an exact nonnegative UTC-ms SQLite integer")
    return value


def _utc_ms() -> int:
    return time.time_ns() // 1_000_000


def _defaults() -> dict[ActionClass, PermissionMode]:
    return {
        ActionClass.READ: PermissionMode.ALLOW,
        ActionClass.WRITE: PermissionMode.REQUIRE_APPROVAL,
        ActionClass.NOTIFY: PermissionMode.ALLOW,
        ActionClass.EXTERNAL: PermissionMode.REQUIRE_APPROVAL,
        ActionClass.DANGEROUS: PermissionMode.DENY,
    }


@dataclass(frozen=True, slots=True)
class ControlPolicy:
    """Detached immutable defaults; a sparse trusted map replaces named classes."""

    modes: Mapping[ActionClass, PermissionMode] = field(default_factory=_defaults)

    def __post_init__(self) -> None:
        merged = _defaults()
        for action, mode in self.modes.items():
            _action(action)
            _mode(mode)
            merged[action] = mode
        object.__setattr__(self, "modes", MappingProxyType(merged))


@dataclass(frozen=True, slots=True)
class PermissionResult:
    """ALLOW=(True,False), REQUIRE_APPROVAL=(False,True), DENY=(False,False)."""

    allowed: bool
    requires_approval: bool
    reason: str


@dataclass(frozen=True, slots=True)
class OverrideRecord:
    id: int
    action_class: ActionClass
    mode: PermissionMode
    created_at: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class ControlHistory:
    id: int
    event_type: str
    action_class: ActionClass
    mode: PermissionMode | None
    created_at: int
    expires_at: int | None
    target_id: int | None


_SCHEMA = (
    "CREATE TABLE draft_control_history ("
    "id INTEGER PRIMARY KEY, "
    "event_type TEXT NOT NULL CHECK(event_type IN ('OVERRIDE','RESET','EXPIRE')), "
    "action_class TEXT NOT NULL "
    "CHECK(action_class IN ('READ','WRITE','NOTIFY','EXTERNAL','DANGEROUS')), "
    "mode TEXT CHECK(mode IN ('ALLOW','REQUIRE_APPROVAL','DENY')), "
    "created_at INTEGER NOT NULL CHECK(created_at>=0), expires_at INTEGER, "
    "target_id INTEGER REFERENCES draft_control_history(id), "
    "CHECK((event_type='OVERRIDE' AND mode IS NOT NULL AND expires_at IS NOT NULL "
    "AND expires_at>created_at AND target_id IS NULL) OR "
    "(event_type='RESET' AND mode IS NULL AND expires_at IS NULL) OR "
    "(event_type='EXPIRE' AND mode IS NOT NULL AND expires_at IS NOT NULL "
    "AND expires_at<=created_at AND target_id IS NOT NULL)))",
    "CREATE TABLE draft_control_current ("
    "action_class TEXT PRIMARY KEY NOT NULL "
    "CHECK(action_class IN ('READ','WRITE','NOTIFY','EXTERNAL','DANGEROUS')), "
    "override_id INTEGER UNIQUE NOT NULL REFERENCES draft_control_history(id))",
    "CREATE TABLE draft_control_state ("
    "id INTEGER PRIMARY KEY CHECK(id=1), "
    "high_water_ms INTEGER NOT NULL CHECK(high_water_ms>=0))",
    "CREATE INDEX draft_control_history_action ON draft_control_history(action_class,id)",
    "CREATE TRIGGER draft_control_history_no_update BEFORE UPDATE ON draft_control_history "
    "BEGIN SELECT RAISE(ABORT,'append-only control history'); END",
    "CREATE TRIGGER draft_control_history_no_delete BEFORE DELETE ON draft_control_history "
    "BEGIN SELECT RAISE(ABORT,'append-only control history'); END",
    "CREATE TRIGGER draft_control_current_binding BEFORE INSERT ON draft_control_current "
    "WHEN NOT EXISTS(SELECT 1 FROM draft_control_history h WHERE h.id=NEW.override_id "
    "AND h.action_class=NEW.action_class AND h.event_type='OVERRIDE') "
    "BEGIN SELECT RAISE(ABORT,'invalid current override binding'); END",
    "CREATE TRIGGER draft_control_current_no_update BEFORE UPDATE ON draft_control_current "
    "BEGIN SELECT RAISE(ABORT,'replace control pointer transactionally'); END",
)


class AutonomyController:
    """Standalone fixture with UTC epoch-ms expiry and persistent audit history.

    Latest override wins; expiration/reset restores defaults, not older overrides.
    Expiry is enforced lazily at every API call/reopen, without a background task.
    Checks/list/history persist expiry and clock high-water, so they are NOT pure
    reads. Each decision is only a point-in-time snapshot; no dispatch reservation,
    revocation of in-flight work, trusted identity or one-use approval is provided.
    """

    runtime_enabled: ClassVar[bool] = False

    def __init__(
        self,
        path: str | Path,
        policy: ControlPolicy | None = None,
        *,
        clock: Callable[[], int] = _utc_ms,
        draft_fixture: bool = False,
    ) -> None:
        if draft_fixture is not True:
            raise AutonomyControlError("explicit offline draft_fixture=True required")
        if policy is not None and type(policy) is not ControlPolicy:
            raise TypeError("policy must be ControlPolicy")
        self._policy = policy if policy is not None else ControlPolicy()
        self._clock = clock
        self._lock = RLock()
        self._connection = sqlite3.connect(path, autocommit=True, check_same_thread=False)
        try:
            self._connection.execute("PRAGMA foreign_keys=ON")
            with self._write():
                actual = {
                    " ".join(row[0].split())
                    for row in self._connection.execute(
                        "SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                    )
                }
                expected = {" ".join(statement.split()) for statement in _SCHEMA}
                if actual and actual != expected:
                    raise AutonomyControlError("standalone controls fixture database required")
                if not actual:
                    for statement in _SCHEMA:
                        self._connection.execute(statement)
                    self._connection.execute("INSERT INTO draft_control_state VALUES (1,0)")
                self._synchronize()
        except BaseException:
            self._connection.close()
            raise

    @property
    def policy(self) -> ControlPolicy:
        return self._policy

    def __enter__(self) -> AutonomyController:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _write(self) -> Generator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

    def _synchronize(self) -> int:
        now = _timestamp(self._clock())
        row = self._connection.execute(
            "SELECT high_water_ms FROM draft_control_state WHERE id=1"
        ).fetchone()
        if row is None or now < row[0]:
            raise AutonomyControlError("missing state or UTC clock moved backwards")
        expired = self._connection.execute(
            "SELECT h.id,h.action_class,h.mode,h.expires_at "
            "FROM draft_control_current c JOIN draft_control_history h ON h.id=c.override_id "
            "WHERE h.expires_at<=? ORDER BY h.id",
            (now,),
        ).fetchall()
        for identity, action, mode, expires_at in expired:
            self._connection.execute(
                "INSERT INTO draft_control_history "
                "(event_type,action_class,mode,created_at,expires_at,target_id) "
                "VALUES ('EXPIRE',?,?,?,?,?)",
                (action, mode, now, expires_at, identity),
            )
            self._connection.execute(
                "DELETE FROM draft_control_current WHERE action_class=?", (action,)
            )
        self._connection.execute(
            "UPDATE draft_control_state SET high_water_ms=? WHERE id=1", (now,)
        )
        return now

    def check(self, action_class: ActionClass) -> PermissionResult:
        """Unknown classes/errors never produce an ALLOW; no approval is consumed."""
        _action(action_class)
        with self._write():
            self._synchronize()
            row = self._connection.execute(
                "SELECT h.mode FROM draft_control_current c "
                "JOIN draft_control_history h ON h.id=c.override_id WHERE c.action_class=?",
                (action_class,),
            ).fetchone()
            mode = PermissionMode(row[0]) if row else self._policy.modes[action_class]
            source = "temporary_override" if row else "default_policy"
            return PermissionResult(
                allowed=mode is PermissionMode.ALLOW,
                requires_approval=mode is PermissionMode.REQUIRE_APPROVAL,
                reason=f"{source}:{action_class.value}",
            )

    def override(
        self, action_class: ActionClass, mode: PermissionMode, expires_at: int
    ) -> OverrideRecord:
        """Trusted fixture-owner mutation only; expiry must be strictly future UTC ms."""
        _action(action_class)
        _mode(mode)
        _timestamp(expires_at)
        with self._write():
            now = self._synchronize()
            if expires_at <= now:
                raise ValueError("expires_at must be strictly in the future")
            cursor = self._connection.execute(
                "INSERT INTO draft_control_history "
                "(event_type,action_class,mode,created_at,expires_at,target_id) "
                "VALUES ('OVERRIDE',?,?,?,?,NULL)",
                (action_class, mode, now, expires_at),
            )
            identity = cursor.lastrowid
            if identity is None:
                raise AutonomyControlError("override identity missing")
            self._connection.execute(
                "DELETE FROM draft_control_current WHERE action_class=?", (action_class,)
            )
            self._connection.execute(
                "INSERT INTO draft_control_current VALUES (?,?)", (action_class, identity)
            )
            return OverrideRecord(identity, action_class, mode, now, expires_at)

    def list_overrides(self) -> list[OverrideRecord]:
        """Detached active overrides in insertion order, at most one per class."""
        with self._write():
            self._synchronize()
            return [
                OverrideRecord(row[0], ActionClass(row[1]), PermissionMode(row[2]), row[3], row[4])
                for row in self._connection.execute(
                    "SELECT h.id,h.action_class,h.mode,h.created_at,h.expires_at "
                    "FROM draft_control_current c JOIN draft_control_history h "
                    "ON h.id=c.override_id ORDER BY h.id"
                )
            ]

    def reset(self, action_class: ActionClass) -> None:
        """Restore trusted defaults for one class; keep audit, even for a no-op reset."""
        _action(action_class)
        with self._write():
            now = self._synchronize()
            row = self._connection.execute(
                "SELECT override_id FROM draft_control_current WHERE action_class=?",
                (action_class,),
            ).fetchone()
            self._connection.execute(
                "INSERT INTO draft_control_history "
                "(event_type,action_class,mode,created_at,expires_at,target_id) "
                "VALUES ('RESET',?,NULL,?,NULL,?)",
                (action_class, now, row[0] if row else None),
            )
            self._connection.execute(
                "DELETE FROM draft_control_current WHERE action_class=?", (action_class,)
            )

    def history(self, action_class: ActionClass | None = None) -> list[ControlHistory]:
        """Fixture inspection, not a tamper-proof audit or a production pagination API."""
        if action_class is not None:
            _action(action_class)
        with self._write():
            self._synchronize()
            sql = (
                "SELECT id,event_type,action_class,mode,created_at,expires_at,target_id "
                "FROM draft_control_history"
            )
            rows = self._connection.execute(
                sql
                + (" WHERE action_class=?" if action_class is not None else "")
                + " ORDER BY id",
                (action_class,) if action_class is not None else (),
            )
            return [
                ControlHistory(
                    row[0],
                    row[1],
                    ActionClass(row[2]),
                    PermissionMode(row[3]) if row[3] is not None else None,
                    row[4],
                    row[5],
                    row[6],
                )
                for row in rows
            ]
