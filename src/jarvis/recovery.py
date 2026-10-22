"""K3-008 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

Offline restart/resume *analysis and markers only*. No scheduler execution, event
replay, transport, K0 takeover, OS wake hook or production reconnection is enabled.
Existing drafts own separate databases: reopening Scheduler restores persisted
jobs; EventStore.checkpoint flushes WAL (not a consumer cursor); replay is inclusive
by opaque event ID; OutboxRelay.poll is read-only eligibility, not delivery.

This module reads those databases through fresh mode=ro connections and persists
only its own standalone recovery ledger. Snapshots across the three databases are
NOT atomic. Event high-watermarks are append sequence observations, NOT evidence
that earlier events were processed. Never use them to skip unprocessed work.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from time import time_ns
from typing import Any, ClassVar
from uuid import uuid4

from jarvis import scheduler as scheduler_module
from jarvis.event_schema import _millis, _payload_json, _text  # pyright: ignore[reportPrivateUsage]
from jarvis.event_store import EventStore
from jarvis.outbox import create_outbox_schema

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False

_SQL = (
    "CREATE TABLE recovery_sources (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "databases TEXT NOT NULL)",
    "CREATE TABLE recovery_sessions (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
    "session_id TEXT NOT NULL UNIQUE, started_at INTEGER NOT NULL CHECK(started_at>=0), "
    "shutdown_at INTEGER CHECK(shutdown_at IS NULL OR shutdown_at>=0), "
    "crash_detected_at INTEGER CHECK(crash_detected_at IS NULL OR crash_detected_at>=0), "
    "recovered_checkpoint INTEGER, recovery_attempted_at INTEGER, "
    "CHECK(shutdown_at IS NULL OR crash_detected_at IS NULL))",
    "CREATE TABLE recovery_checkpoints (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
    "timestamp INTEGER NOT NULL CHECK(timestamp>=0), scheduler_checkpoint TEXT NOT NULL, "
    "event_store_high_watermark INTEGER NOT NULL CHECK(event_store_high_watermark>=0), "
    "outbox_pending_count INTEGER NOT NULL CHECK(outbox_pending_count>=0), "
    "session_id TEXT NOT NULL REFERENCES recovery_sessions(session_id))",
)
_EXPECTED = {statement.split()[2]: statement for statement in _SQL}
_CP_COLUMNS = (
    "timestamp, scheduler_checkpoint, event_store_high_watermark, outbox_pending_count, session_id"
)


def _objects(connection: sqlite3.Connection) -> dict[str, str]:
    return dict(
        connection.execute(
            "SELECT name, sql FROM main.sqlite_master WHERE name NOT GLOB 'sqlite_*'"
        ).fetchall()
    )


def _ledger(connection: sqlite3.Connection) -> bool:
    """Validate without creating tables or altering the caller's transaction."""
    if (
        connection.in_transaction
        or connection.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL
        or connection.row_factory is not None
        or connection.text_factory is not str
    ):
        raise ValueError("requires an idle explicit-transaction connection with tuple/str rows")
    if any(row[1] not in {"main", "temp"} for row in connection.execute("PRAGMA database_list")):
        raise ValueError("attached databases are not supported")
    if connection.execute("SELECT 1 FROM sqlite_temp_master LIMIT 1").fetchone():
        raise ValueError("temporary objects are not supported")
    objects = _objects(connection)
    if objects and objects != _EXPECTED:
        raise ValueError("requires an empty or matching standalone recovery draft database")
    return bool(objects)


@dataclass(frozen=True, slots=True)
class RecoveryPoint:
    """UTC epoch ms; scheduler JSON snapshot is detached, not deeply immutable."""

    timestamp: int
    scheduler_checkpoint: dict[str, Any]
    event_store_high_watermark: int
    outbox_pending_count: int
    session_id: str

    def __post_init__(self) -> None:
        for name in ("timestamp", "event_store_high_watermark", "outbox_pending_count"):
            _millis(name, getattr(self, name))
        _text("session_id", self.session_id)
        object.__setattr__(
            self, "scheduler_checkpoint", json.loads(_payload_json(self.scheduler_checkpoint))
        )


@dataclass(frozen=True, slots=True)
class StartupStatus:
    session_id: str | None
    clean_shutdown: bool
    crash_count: int
    last_crash_at: int | None


class StartupProbe:
    """Read-only ledger inspection; an empty ledger has no unclean predecessor.

    Unclean is evidence of a missing shutdown marker, not proof of a host crash:
    SIGKILL, reboot and abandoned fixtures look alike. last_crash_at is detection
    time, NOT the unknowable instant when the preceding process failed.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def status(self, connection: sqlite3.Connection) -> StartupStatus:
        if not _ledger(connection):
            return StartupStatus(None, True, 0, None)
        row = connection.execute(
            "SELECT session_id, shutdown_at FROM recovery_sessions ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        crashes = connection.execute(
            "SELECT COUNT(*) FROM recovery_sessions WHERE crash_detected_at IS NOT NULL"
        ).fetchone()[0]
        last = connection.execute(
            "SELECT crash_detected_at FROM recovery_sessions "
            "WHERE crash_detected_at IS NOT NULL ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return StartupStatus(
            row[0] if row else None,
            row is None or row[1] is not None,
            crashes,
            last[0] if last else None,
        )

    def is_first_start(self, connection: sqlite3.Connection) -> bool:
        return self.status(connection).session_id is None

    def is_clean_shutdown(self, connection: sqlite3.Connection) -> bool:
        return self.status(connection).clean_shutdown


def _point(row: tuple[Any, ...]) -> RecoveryPoint:
    return RecoveryPoint(row[0], json.loads(row[1]), row[2], row[3], row[4])


@dataclass(frozen=True, slots=True)
class RecoveryAnalysis:
    """Loaded historical markers plus current inventory, never execution permission."""

    recovery_point: RecoveryPoint | None
    current_scheduler_checkpoint: dict[str, Any]
    current_event_store_high_watermark: int
    current_outbox_pending_count: int
    events_after_checkpoint: int
    crash_count: int
    last_crash_at: int | None
    runtime_enabled: bool = False


class RecoveryManager:
    """Single coordinator, opt-in fixture only; caller owns the ledger connection.

    Mutations own BEGIN IMMEDIATE/commit/rollback and reject caller transactions.
    Supply a fresh session_id per process lifecycle, reusing it for wake/resume in
    that same process. Repeated recover calls do not count additional crashes.
    Shutdown must be explicitly marked after other components have stopped; the
    manager cannot infer clean shutdown from close() or a periodic checkpoint.
    Stale or already-clean session IDs cannot write again. No lease/fencing claim.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(
        self,
        *,
        scheduler_database: str | Path,
        event_store_database: str | Path,
        outbox_database: str | Path,
        session_id: str | None = None,
        clock: Callable[[], int] | None = None,
        draft_fixture: bool = False,
    ) -> None:
        if draft_fixture is not True:
            raise ValueError("explicit offline draft_fixture=True required")
        self.session_id = str(uuid4()) if session_id is None else session_id
        _text("session_id", self.session_id)
        self._clock = clock if clock is not None else lambda: time_ns() // 1_000_000
        self._paths = {
            "scheduler": Path(scheduler_database).resolve(),
            "event_store": Path(event_store_database).resolve(),
            "outbox": Path(outbox_database).resolve(),
        }
        if len(set(self._paths.values())) != 3:
            raise ValueError("subsystem drafts require three distinct standalone databases")
        self._binding = json.dumps({k: str(v) for k, v in self._paths.items()}, sort_keys=True)

    def _now(self) -> int:
        now = self._clock()
        _millis("clock", now)
        return now

    def _begin(self, connection: sqlite3.Connection) -> None:
        _ledger(connection)
        main = next(
            row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"
        )
        if main and Path(main).resolve() in self._paths.values():
            raise ValueError("recovery ledger must not share a subsystem database")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA fullfsync=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            objects = _objects(connection)
            if not objects:
                for statement in _SQL:
                    connection.execute(statement)
            elif objects != _EXPECTED:
                raise ValueError("unrecognized recovery schema")
            binding = connection.execute("SELECT databases FROM recovery_sources").fetchone()
            if binding is None:
                connection.execute("INSERT INTO recovery_sources VALUES (1, ?)", (self._binding,))
            elif binding != (self._binding,):
                raise ValueError("recovery ledger is bound to different subsystem databases")
        except BaseException:
            connection.rollback()
            raise

    def _activate(self, connection: sqlite3.Connection, now: int) -> None:
        latest = connection.execute(
            "SELECT session_id, shutdown_at FROM recovery_sessions ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        known = connection.execute(
            "SELECT 1 FROM recovery_sessions WHERE session_id=?", (self.session_id,)
        ).fetchone()
        if known:
            if latest != (self.session_id, None):
                raise ValueError("stale or cleanly closed recovery session")
            return
        if latest is not None and latest[1] is None:
            connection.execute(
                "UPDATE recovery_sessions SET crash_detected_at=? WHERE session_id=?",
                (now, latest[0]),
            )
        connection.execute(
            "INSERT INTO recovery_sessions (session_id, started_at) VALUES (?, ?)",
            (self.session_id, now),
        )

    def _inventory(self, since: int = 0) -> tuple[dict[str, Any], int, int, int]:
        # Use real candidate DDL as a single source; never initialize source DBs.
        with closing(sqlite3.connect(":memory:")) as fixture:
            create_outbox_schema(fixture, draft_fixture=True)
            outbox_expected = _objects(fixture)
        expected = {
            "scheduler": {
                "scheduled_jobs": scheduler_module._TABLE_SQL,  # pyright: ignore[reportPrivateUsage]
                "scheduled_jobs_due": scheduler_module._INDEX_SQL,  # pyright: ignore[reportPrivateUsage]
            },
            "event_store": EventStore._expected_objects(),  # pyright: ignore[reportPrivateUsage]
            "outbox": outbox_expected,
        }
        jobs: list[dict[str, Any]] = []
        high = pending = after = 0
        for name, path in self._paths.items():
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            try:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                if _objects(connection) != expected[name]:
                    raise ValueError(f"unrecognized standalone {name} draft database")
                if name == "scheduler":
                    cursor = connection.execute("SELECT * FROM scheduled_jobs ORDER BY id")
                    columns = [column[0] for column in cursor.description]
                    for row in cursor.fetchall():
                        # Validate through the real scheduler decoder, including cron/deadlines.
                        scheduler_module._job(row)  # pyright: ignore[reportPrivateUsage]
                        job = dict(zip(columns, row, strict=True))
                        job["payload"] = json.loads(job["payload"])
                        jobs.append(job)
                elif name == "event_store":
                    high = connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM durable_events"
                    ).fetchone()[0]
                    after = connection.execute(
                        "SELECT COUNT(*) FROM durable_events WHERE sequence>?", (since,)
                    ).fetchone()[0]
                else:
                    # Includes future PENDING and retryable FAILED, excludes dead letters.
                    pending = connection.execute(
                        "SELECT COUNT(*) FROM outbox "
                        "WHERE status IN ('PENDING','FAILED') AND attempts<5"
                    ).fetchone()[0]
            finally:
                connection.close()
        return {"jobs": jobs}, high, pending, after

    def checkpoint(self, connection: sqlite3.Connection) -> RecoveryPoint:
        """Commit detached markers; never flush or mutate a subsystem database."""
        now = self._now()
        self._begin(connection)
        try:
            scheduler, high, pending, _ = self._inventory()
            self._activate(connection, now)
            point = RecoveryPoint(now, scheduler, high, pending, self.session_id)
            connection.execute(
                f"INSERT INTO recovery_checkpoints ({_CP_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
                (now, _payload_json(point.scheduler_checkpoint), high, pending, self.session_id),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return point

    def list_checkpoints(self, connection: sqlite3.Connection) -> list[RecoveryPoint]:
        """Newest durable insertion first, including ties and backward wall clocks.

        Sequence order is authoritative, not timestamp order. Read-only: no schema
        creation, session activation, crash detection or commit of caller work.
        """
        if not _ledger(connection):
            return []
        binding = connection.execute("SELECT databases FROM recovery_sources").fetchone()
        if binding != (self._binding,):
            raise ValueError("recovery ledger is bound to different subsystem databases")
        return [
            _point(row)
            for row in connection.execute(
                f"SELECT {_CP_COLUMNS} FROM recovery_checkpoints ORDER BY sequence DESC"
            ).fetchall()
        ]

    def recover(self, connection: sqlite3.Connection) -> RecoveryAnalysis:
        """Load the latest committed markers and record analysis, never rewind live state.

        Reopening the three read-only source connections probes accessibility.
        The old scheduler snapshot/event append boundary/outbox count are returned
        intact beside current observations. Events after the boundary are merely
        counted; earlier unprocessed events may exist too. A regressed event log
        is rejected instead of silently treating missing history as recovered.
        """
        now = self._now()
        self._begin(connection)
        try:
            row = connection.execute(
                f"SELECT sequence, {_CP_COLUMNS} FROM recovery_checkpoints "
                "ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            point = _point(row[1:]) if row else None
            scheduler, high, pending, after = self._inventory(
                point.event_store_high_watermark if point else 0
            )
            if point is not None and high < point.event_store_high_watermark:
                raise ValueError("event store high-watermark regressed; reconciliation required")
            self._activate(connection, now)
            connection.execute(
                "UPDATE recovery_sessions SET recovered_checkpoint=?, recovery_attempted_at=? "
                "WHERE session_id=?",
                (row[0] if row else None, now, self.session_id),
            )
            crashes = connection.execute(
                "SELECT COUNT(*) FROM recovery_sessions WHERE crash_detected_at IS NOT NULL"
            ).fetchone()[0]
            last = connection.execute(
                "SELECT crash_detected_at FROM recovery_sessions "
                "WHERE crash_detected_at IS NOT NULL ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            result = RecoveryAnalysis(
                point,
                scheduler,
                high,
                pending,
                after,
                crashes,
                last[0] if last else None,
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return result

    def mark_clean_shutdown(self, connection: sqlite3.Connection) -> None:
        """Explicit lifecycle marker only; caller must already have stopped components."""
        now = self._now()
        self._begin(connection)
        try:
            latest = connection.execute(
                "SELECT session_id, shutdown_at FROM recovery_sessions "
                "ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            if latest is None or latest[0] != self.session_id:
                raise ValueError("only the current session may mark clean shutdown")
            if latest[1] is None:
                connection.execute(
                    "UPDATE recovery_sessions SET shutdown_at=? WHERE session_id=?",
                    (now, self.session_id),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
