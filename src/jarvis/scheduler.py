"""K3-005 DRAFT; NOT_FROZEN, NOT_APPROVED, runtime_enabled=False.

Opt-in standalone SQLite scheduling records, never a handler executor. Times are
UTC epoch milliseconds, consistent with event_schema and outbox. No production
migration, background thread, handler import or runtime wiring is added.

Single coordinator only: due_jobs is read-only, NOT a claim/lease. A due occurrence
remains eligible until mark_done commits. Crash before acknowledgement => replay,
including when a handler's effect already happened. At-least-once *eligibility*
requires a caller that executes then acknowledges; this module alone cannot
promise eventual execution. Dangerous/unknown effects still require K0 protocols,
not blind retries. Use (job.id, job.next_run_at) as an occurrence idempotency key.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from time import time_ns
from types import TracebackType
from typing import Any, ClassVar, cast

# Reuse the event draft's strict UTC-ms / JSON-native validation, not its schema.
from jarvis.event_schema import _millis, _payload_json, _text  # pyright: ignore[reportPrivateUsage]

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class JobStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


def _delay_ms(delay: timedelta) -> int:
    microseconds = (delay.days * 86400 + delay.seconds) * 1_000_000 + delay.microseconds
    if microseconds <= 0 or microseconds % 1000:
        raise ValueError("one-shot delay must be positive with whole-millisecond precision")
    millis = microseconds // 1000
    _millis("delay", millis)
    return millis


@lru_cache(maxsize=128)
def _cron(expression: str) -> tuple[tuple[frozenset[int], ...], bool, bool]:
    """Numeric five-field UTC cron; *, */n, a-b[/n] and comma lists.

    Sunday=0 or 7. Vixie day semantics: OR between restricted DOM/DOW;
    AND if either field starts with *. Names, macros and bare a/n are rejected.
    """
    if not isinstance(cast(object, expression), str) or len(expression) > 256:
        raise ValueError("cron must be text of at most 256 characters")
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron requires exactly five fields")
    parsed: list[frozenset[int]] = []
    for position, (field, (minimum, maximum)) in enumerate(
        zip(fields, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)), strict=True)
    ):
        values: set[int] = set()
        for part in field.split(","):
            match = re.fullmatch(r"(\*|[0-9]+(?:-[0-9]+)?)(?:/([0-9]+))?", part)
            if match is None:
                raise ValueError(f"invalid cron field: {field}")
            base, step_text = match.groups()
            step = int(step_text) if step_text else 1
            if not 1 <= step <= maximum - minimum + 1:
                raise ValueError("cron step outside field range")
            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                start, end = map(int, base.split("-"))
            elif step_text is None:
                start = end = int(base)
            else:
                raise ValueError("cron steps require * or a range")
            if not minimum <= start <= end <= maximum:
                raise ValueError("cron value outside field range")
            values.update(range(start, end + 1, step))
        if position == 4:
            values = {value % 7 for value in values}
        parsed.append(frozenset(values))
    return tuple(parsed), fields[2].startswith("*"), fields[4].startswith("*")


def next_cron_at(expression: str, after: int) -> int:
    """First matching whole UTC minute strictly after after (epoch ms).

    Search a full Gregorian 400-year cycle by *day*, not minutes. Impossible
    calendars fail explicitly, including February 30; sparse leap schedules may
    legitimately need eight years across a non-leap century. No local-time/DST.
    """
    _millis("after", after)
    fields, dom_star, dow_star = _cron(expression)
    minutes, hours, days, months, weekdays = fields
    try:
        start = _EPOCH + timedelta(milliseconds=after)
    except OverflowError as error:
        raise ValueError("cron timestamp exceeds datetime range") from error
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(146_098):
        dom = day.day in days
        dow = (day.weekday() + 1) % 7 in weekdays
        matched_day = (dom and dow) if dom_star or dow_star else (dom or dow)
        if day.month in months and matched_day:
            offset = day - _EPOCH
            midnight = offset.days * 86_400_000
            for hour in sorted(hours):
                for minute in sorted(minutes):
                    candidate = midnight + (hour * 60 + minute) * 60_000
                    if candidate > after:
                        return candidate
        try:
            day += timedelta(days=1)
        except OverflowError:
            break
    raise ValueError("cron has no future occurrence within a Gregorian cycle/datetime range")


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """Candidate snapshot; timedelta means one-shot, never a repeat interval.

    next_run_at=None requests initial calculation. A completed one-shot keeps
    ACTIVE with last_run_at set and next_run_at=None (no fourth status required).
    The payload is copied, but remains mutable; registration snapshots it again.
    """

    id: str
    name: str
    schedule: str | timedelta
    handler_key: str
    payload: dict[str, Any]
    created_at: int
    next_run_at: int | None = None
    last_run_at: int | None = None
    status: JobStatus = JobStatus.ACTIVE

    def __post_init__(self) -> None:
        for name in ("id", "name", "handler_key"):
            _text(name, getattr(self, name))
        if not isinstance(cast(object, self.status), JobStatus):
            raise TypeError("status must be JobStatus")
        _millis("created_at", self.created_at)
        for name in ("next_run_at", "last_run_at"):
            value = getattr(self, name)
            if value is not None:
                _millis(name, value)
                if value < self.created_at:
                    raise ValueError(f"{name} precedes created_at")
        if isinstance(self.schedule, str):
            next_cron_at(self.schedule, self.created_at)
            if self.last_run_at is not None and self.next_run_at is None:
                raise ValueError("acknowledged cron must retain a next occurrence")
        elif isinstance(cast(object, self.schedule), timedelta):
            _millis("initial due time", self.created_at + _delay_ms(self.schedule))
            if self.last_run_at is not None and self.next_run_at is not None:
                raise ValueError("completed one-shot cannot have a next occurrence")
        else:
            raise TypeError("schedule must be a cron expression or timedelta")
        object.__setattr__(self, "payload", json.loads(_payload_json(self.payload)))


def _initial(job: ScheduledJob) -> ScheduledJob:
    if job.next_run_at is None and job.last_run_at is None:
        next_time = (
            next_cron_at(job.schedule, job.created_at)
            if isinstance(job.schedule, str)
            else job.created_at + _delay_ms(job.schedule)
        )
        return replace(job, next_run_at=next_time)
    return job


_TABLE_SQL = (
    "CREATE TABLE scheduled_jobs ("
    "id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(id))>0), "
    "name TEXT NOT NULL CHECK (length(trim(name))>0), "
    "schedule_kind TEXT NOT NULL CHECK (schedule_kind IN ('cron','delay')), "
    "schedule_value TEXT NOT NULL, "
    "handler_key TEXT NOT NULL CHECK (length(trim(handler_key))>0), payload TEXT NOT NULL, "
    "next_run_at INTEGER, last_run_at INTEGER, "
    "status TEXT NOT NULL CHECK (status IN ('ACTIVE','PAUSED','CANCELLED')), "
    "created_at INTEGER NOT NULL CHECK (typeof(created_at)='integer' AND created_at>=0), "
    "CHECK (next_run_at IS NULL OR (typeof(next_run_at)='integer' "
    "AND next_run_at>=created_at)), "
    "CHECK (last_run_at IS NULL OR (typeof(last_run_at)='integer' "
    "AND last_run_at>=created_at)))"
)
_INDEX_SQL = "CREATE INDEX scheduled_jobs_due ON scheduled_jobs(status, next_run_at, id)"
_COLUMNS = (
    "id, name, schedule_kind, schedule_value, handler_key, payload, "
    "next_run_at, last_run_at, status, created_at"
)


def _job(row: tuple[Any, ...]) -> ScheduledJob:
    return ScheduledJob(
        id=row[0],
        name=row[1],
        schedule=row[3] if row[2] == "cron" else timedelta(milliseconds=int(row[3])),
        handler_key=row[4],
        payload=json.loads(row[5]),
        next_run_at=row[6],
        last_run_at=row[7],
        status=JobStatus(row[8]),
        created_at=row[9],
    )


class Scheduler:
    """Single-coordinator offline store, owning its connection/transactions.

    Mutations commit before returning; never share a business/outbox connection.
    Reopening validates/reloads persisted rows and recalculates missing initial
    deadlines, never now+delay or a future cron tick that skips overdue work.
    Existing due times are authoritative, including missed cron occurrences.
    Pause/resume retains backlog; cancelled IDs are terminal and never replaced.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(
        self,
        database: str | Path,
        *,
        draft_fixture: bool = False,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if draft_fixture is not True:
            raise ValueError("explicit offline draft_fixture=True required")
        self._clock = clock if clock is not None else lambda: time_ns() // 1_000_000
        self._connection = sqlite3.connect(database)
        try:
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA fullfsync=ON")
            self._connection.execute("BEGIN IMMEDIATE")
            objects = dict(
                self._connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
                )
            )
            if not objects:
                self._connection.execute(_TABLE_SQL)
                self._connection.execute(_INDEX_SQL)
            elif objects != {"scheduled_jobs": _TABLE_SQL, "scheduled_jobs_due": _INDEX_SQL}:
                raise ValueError(
                    "requires an empty or matching standalone scheduler draft database"
                )
            for row in self._connection.execute(
                f"SELECT {_COLUMNS} FROM scheduled_jobs"
            ).fetchall():
                job = _job(row)
                if job.status is JobStatus.ACTIVE and (restored := _initial(job)) != job:
                    self._connection.execute(
                        "UPDATE scheduled_jobs SET next_run_at=? WHERE id=?",
                        (restored.next_run_at, job.id),
                    )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            self._connection.close()
            raise

    def __enter__(self) -> Scheduler:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def get(self, job_id: str) -> ScheduledJob:
        _text("job_id", job_id)
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM scheduled_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _job(row)

    def register(self, job: ScheduledJob) -> ScheduledJob:
        """Persist a fresh snapshot; duplicate IDs never reset or replace jobs."""
        if not isinstance(cast(object, job), ScheduledJob):
            raise TypeError("job must be ScheduledJob")
        saved = _initial(replace(job))
        kind = "cron" if isinstance(saved.schedule, str) else "delay"
        value = (
            saved.schedule if isinstance(saved.schedule, str) else str(_delay_ms(saved.schedule))
        )
        with self._connection:
            self._connection.execute(
                f"INSERT INTO scheduled_jobs ({_COLUMNS}) VALUES ({','.join(['?'] * 10)})",
                (
                    saved.id,
                    saved.name,
                    kind,
                    value,
                    saved.handler_key,
                    _payload_json(saved.payload),
                    saved.next_run_at,
                    saved.last_run_at,
                    saved.status.value,
                    saved.created_at,
                ),
            )
        return saved

    def due_jobs(self, now: int) -> list[ScheduledJob]:
        """Read-only, inclusive due poll; each job contributes its oldest pending tick."""
        _millis("now", now)
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM scheduled_jobs WHERE status='ACTIVE' "
            "AND next_run_at<=? ORDER BY next_run_at, created_at, id",
            (now,),
        ).fetchall()
        return [_job(row) for row in rows]

    def _status(self, job_id: str, status: JobStatus) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            job = self.get(job_id)
            if job.status is JobStatus.CANCELLED and status is not JobStatus.CANCELLED:
                raise ValueError("cancelled jobs cannot be paused or resumed")
            if job.status is not status:
                saved = _initial(job) if status is JobStatus.ACTIVE else job
                self._connection.execute(
                    "UPDATE scheduled_jobs SET status=?, next_run_at=? WHERE id=?",
                    (status.value, saved.next_run_at, job_id),
                )

    def cancel(self, job_id: str) -> None:
        self._status(job_id, JobStatus.CANCELLED)

    def pause(self, job_id: str) -> None:
        self._status(job_id, JobStatus.PAUSED)

    def resume(self, job_id: str) -> None:
        self._status(job_id, JobStatus.ACTIVE)

    def mark_done(self, job_id: str, next_run_at: int | None) -> None:
        """Acknowledge only after handler success; atomically record and advance.

        Cron must advance to the exact tick after the persisted due tick, NOT
        after wall-clock completion (which would drop missed ticks). One-shot
        requires None. Repeated/stale cron acknowledgements fail rather than
        skipping another tick. Caller runs one occurrence at a time per job.
        """
        now = self._clock()
        _millis("clock", now)
        if next_run_at is not None:
            _millis("next_run_at", next_run_at)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            job = self.get(job_id)
            if (
                job.status is not JobStatus.ACTIVE
                or job.next_run_at is None
                or job.next_run_at > now
            ):
                raise ValueError("only a due ACTIVE occurrence can be acknowledged")
            if job.last_run_at is not None and now < job.last_run_at:
                raise ValueError("clock precedes last acknowledgement")
            expected = (
                next_cron_at(job.schedule, job.next_run_at)
                if isinstance(job.schedule, str)
                else None
            )
            if next_run_at != expected:
                raise ValueError("next_run_at must be the next cron tick, or None for one-shot")
            self._connection.execute(
                "UPDATE scheduled_jobs SET last_run_at=?, next_run_at=? WHERE id=?",
                (now, next_run_at, job_id),
            )
