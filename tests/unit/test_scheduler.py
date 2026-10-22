"""K3-005 offline persistent scheduler draft; handlers never execute."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jarvis import scheduler as module
from jarvis.scheduler import JobStatus, ScheduledJob, Scheduler


@pytest.fixture
def store(tmp_path: Path) -> Generator[Scheduler]:
    scheduler = Scheduler(tmp_path / "scheduler.db", draft_fixture=True, clock=lambda: 6000)
    try:
        yield scheduler
    finally:
        scheduler.close()


def _job(**changes: Any) -> ScheduledJob:
    return replace(
        ScheduledJob(
            id="job-1",
            name="fixture",
            schedule=timedelta(seconds=5),
            handler_key="fixture.only",
            payload={},
            created_at=1000,
        ),
        **changes,
    )


def test_register_persists_job_and_payload_snapshot(tmp_path: Path) -> None:
    payload = {"nested": {"value": 1}}
    job = module.ScheduledJob(
        id="job-1",
        name="fixture",
        schedule=timedelta(seconds=5),
        handler_key="fixture.only",
        payload=payload,
        created_at=1000,
    )
    path = tmp_path / "scheduler.db"
    scheduler = module.Scheduler(path, draft_fixture=True, clock=lambda: 1000)
    try:
        saved = scheduler.register(job)
        payload["nested"]["value"] = 9
        job.payload["nested"]["value"] = 8
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                "SELECT id, status, next_run_at, payload FROM scheduled_jobs"
            ).fetchone()
        assert row[:3] == ("job-1", "ACTIVE", 6000)
        assert saved.payload == {"nested": {"value": 1}}
        assert scheduler.get(job.id) == saved
    finally:
        scheduler.close()


def test_due_jobs_is_read_only_inclusive_and_sorted(store: Scheduler) -> None:
    first = store.register(_job(id="a"))
    last = store.register(_job(id="b"))
    store.register(_job(id="future", next_run_at=7000))
    assert store.due_jobs(5999) == []
    assert store.due_jobs(6000) == [first, last]
    assert store.due_jobs(6000) == [first, last]  # No claim or acknowledgement on poll.
    assert store.get("a").last_run_at is None
    result = store.due_jobs(6000)
    result[0].payload["mutated"] = True
    assert store.get("a").payload == {}


def test_mark_done_consumes_one_shot_and_records_time(store: Scheduler) -> None:
    store.register(_job())
    store.mark_done("job-1", None)
    completed = store.get("job-1")
    assert completed.last_run_at == 6000
    assert completed.next_run_at is None
    assert completed.status is JobStatus.ACTIVE
    assert store.due_jobs(999_999) == []
    with pytest.raises(ValueError):
        store.mark_done("job-1", None)


def test_cancel_pause_resume_and_terminal_cancel(store: Scheduler) -> None:
    store.register(_job())
    store.pause("job-1")
    assert store.get("job-1").status is JobStatus.PAUSED
    assert store.due_jobs(6000) == []
    store.pause("job-1")
    store.resume("job-1")
    assert store.get("job-1").status is JobStatus.ACTIVE
    assert len(store.due_jobs(6000)) == 1
    store.cancel("job-1")
    store.cancel("job-1")
    assert store.get("job-1").status is JobStatus.CANCELLED
    assert store.due_jobs(6000) == []
    for operation in (store.resume, store.pause):
        with pytest.raises(ValueError):
            operation("job-1")
    with pytest.raises(ValueError):
        store.mark_done("job-1", None)


def test_restart_preserves_unacknowledged_overdue_job(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.db"
    first = Scheduler(path, draft_fixture=True, clock=lambda: 6000)
    original = first.register(_job())
    assert first.due_jobs(6000) == [original]
    first.close()  # Crash before mark_done: still due.
    second = Scheduler(path, draft_fixture=True, clock=lambda: 900_000)
    try:
        assert second.due_jobs(900_000) == [original]
        second.mark_done("job-1", None)
    finally:
        second.close()
    third = Scheduler(path, draft_fixture=True, clock=lambda: 2_000_000)
    try:
        assert third.due_jobs(2_000_000) == []
        assert third.get("job-1").last_run_at == 900_000
    finally:
        third.close()


@pytest.mark.parametrize("method", ["get", "cancel", "pause", "resume"])
def test_missing_job_raises(store: Scheduler, method: str) -> None:
    with pytest.raises(KeyError):
        getattr(store, method)("unknown")


def test_duplicate_id_never_replaces_pending_or_cancelled_job(store: Scheduler) -> None:
    store.register(_job())
    store.cancel("job-1")
    with pytest.raises(sqlite3.IntegrityError):
        store.register(_job(payload={"replacement": True}))
    assert store.get("job-1").status is JobStatus.CANCELLED
    assert store.get("job-1").payload == {}


def test_mark_done_requires_due_active_job_and_one_shot_cannot_repeat(store: Scheduler) -> None:
    store.register(_job(next_run_at=7000))
    with pytest.raises(ValueError):
        store.mark_done("job-1", None)
    store.register(_job(id="due"))
    with pytest.raises(ValueError):
        store.mark_done("due", 7000)
    store.pause("due")
    with pytest.raises(ValueError):
        store.mark_done("due", None)
    assert store.get("due").last_run_at is None


def test_draft_opt_in_and_foreign_database_refusal(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.db"
    with pytest.raises(ValueError, match="draft_fixture"):
        Scheduler(path)
    assert not path.exists()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (id TEXT)")
    with pytest.raises(ValueError, match="standalone"):
        Scheduler(path, draft_fixture=True)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == [("unrelated",)]


def _ms(value: str) -> int:
    delta = datetime.fromisoformat(value).replace(tzinfo=UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


@pytest.mark.parametrize(
    ("expression", "after", "expected"),
    [
        ("*/5 * * * *", "2026-01-01T00:00:00", "2026-01-01T00:05:00"),
        ("*/5 * * * *", "2026-01-01T23:59:59.999", "2026-01-02T00:00:00"),
        ("15 8 * * 1-5", "2026-01-02T08:15:00", "2026-01-05T08:15:00"),
        ("0 0 29 2 *", "2026-01-01T00:00:00", "2028-02-29T00:00:00"),
        ("0 0 1 * 1", "2026-01-01T00:00:00", "2026-01-05T00:00:00"),
        ("0 0 * * 7", "2026-01-03T00:00:00", "2026-01-04T00:00:00"),
        ("0,30 9-17/2 * * *", "2026-01-01T09:30:00", "2026-01-01T11:00:00"),
        ("0 0 29 2 *", "2096-03-01T00:00:00", "2104-02-29T00:00:00"),
    ],
)
def test_cron_utc_strictly_after(expression: str, after: str, expected: str) -> None:
    assert module.next_cron_at(expression, _ms(after)) == _ms(expected)


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "* * * *",
        "* * * * * *",
        "*/0 * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "-1 * * * *",
        "1.5 * * * *",
        "5-1 * * * *",
        "* * * JAN *",
        "@daily",
        "1,,2 * * * *",
        "* * * * ?",
        "0 0 30 2 *",
        "1/2 * * * *",
    ],
)
def test_invalid_or_impossible_cron_rejected(expression: str) -> None:
    with pytest.raises(ValueError):
        _job(schedule=expression)


def test_cron_done_updates_next_run_at_and_last_run_at(tmp_path: Path) -> None:
    now = [_ms("2026-01-01T00:05:00")]
    scheduler = Scheduler(tmp_path / "cron.db", draft_fixture=True, clock=lambda: now[0])
    try:
        registered = scheduler.register(_job(schedule="*/5 * * * *", created_at=now[0] - 300_000))
        assert registered.next_run_at == now[0]
        now[0] += 31_000
        following = _ms("2026-01-01T00:10:00")
        scheduler.mark_done(registered.id, following)
        assert scheduler.get(registered.id).last_run_at == now[0]
        assert scheduler.get(registered.id).next_run_at == following
        assert scheduler.due_jobs(following - 1) == []
        assert [j.id for j in scheduler.due_jobs(following)] == [registered.id]
    finally:
        scheduler.close()


def test_cron_restart_keeps_earliest_unacknowledged_tick_and_paused_state(tmp_path: Path) -> None:
    path = tmp_path / "cron.db"
    created = _ms("2026-01-01T00:00:00")
    with Scheduler(path, draft_fixture=True, clock=lambda: created) as first:
        active = first.register(_job(schedule="*/5 * * * *", created_at=created))
        first.register(_job(id="paused", schedule="*/5 * * * *", created_at=created))
        first.pause("paused")
    with Scheduler(path, draft_fixture=True, clock=lambda: created + 3_600_000) as second:
        assert second.due_jobs(created + 3_600_000) == [active]
        assert active.next_run_at == created + 300_000  # Recovery must not skip to now.
        assert second.get("paused").status is JobStatus.PAUSED
        second.resume("paused")
        assert len(second.due_jobs(created + 3_600_000)) == 2


@pytest.mark.parametrize("next_time", [None, 300_000, 301_000, 900_000])
def test_cron_ack_requires_exact_next_tick(tmp_path: Path, next_time: int | None) -> None:
    with Scheduler(tmp_path / "ack.db", draft_fixture=True, clock=lambda: 350_000) as scheduler:
        scheduler.register(_job(schedule="*/5 * * * *", created_at=0))
        with pytest.raises(ValueError):
            scheduler.mark_done("job-1", next_time)
        assert scheduler.get("job-1").next_run_at == 300_000


def test_cron_backlog_advances_one_tick_not_to_restart_time(tmp_path: Path) -> None:
    with Scheduler(tmp_path / "ack.db", draft_fixture=True, clock=lambda: 3_600_000) as scheduler:
        scheduler.register(_job(schedule="*/5 * * * *", created_at=0))
        scheduler.mark_done("job-1", 600_000)
        assert scheduler.get("job-1").next_run_at == 600_000
        assert len(scheduler.due_jobs(3_600_000)) == 1  # Next missed tick remains eligible.


@pytest.mark.parametrize(
    "changes",
    [
        {"id": " "},
        {"handler_key": "bad\x00key"},
        {"name": ""},
        {"status": "ACTIVE"},
        {"created_at": True},
        {"created_at": -1},
        {"next_run_at": 999},
        {"last_run_at": 999},
        {"schedule": timedelta(0)},
        {"schedule": timedelta(seconds=-1)},
        {"schedule": timedelta(microseconds=1)},
        {"schedule": 5},
        {"payload": []},
        {"payload": {"nan": float("nan")}},
        {"payload": {1: "nonstring key"}},
        {"created_at": 2**63 - 1000},
    ],
)
def test_invalid_job_values_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        _job(**changes)


@pytest.mark.parametrize("now", [True, -1, 1.5, 2**63])
def test_due_jobs_rejects_non_sqlite_millisecond_time(store: Scheduler, now: Any) -> None:
    with pytest.raises(ValueError):
        store.due_jobs(now)


def test_registration_revalidates_mutated_payload(store: Scheduler) -> None:
    job = _job()
    job.payload["not_json"] = object()
    with pytest.raises(TypeError):
        store.register(job)
    with pytest.raises(KeyError):
        store.get(job.id)


def test_recovery_recalculates_missing_initial_deadline_without_resetting_delay(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restore.db"
    with Scheduler(path, draft_fixture=True) as first:
        first.register(_job())
    # Simulate an imported draft row lacking its calculated initial deadline.
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE scheduled_jobs SET next_run_at=NULL")
    with Scheduler(path, draft_fixture=True, clock=lambda: 900_000) as second:
        assert second.get("job-1").next_run_at == 6000  # Based on created_at, not restart.
        assert len(second.due_jobs(900_000)) == 1


def test_cancelled_and_finished_one_shot_stay_inert_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "states.db"
    with Scheduler(path, draft_fixture=True, clock=lambda: 6000) as first:
        first.register(_job(id="cancelled"))
        first.cancel("cancelled")
        first.register(_job(id="finished"))
        first.mark_done("finished", None)
    with Scheduler(path, draft_fixture=True, clock=lambda: 900_000) as second:
        assert second.due_jobs(900_000) == []
        assert second.get("cancelled").status is JobStatus.CANCELLED
        second.pause("finished")
        second.resume("finished")
        assert second.due_jobs(900_000) == []


def test_cron_clock_rollback_does_not_erase_pending_work(tmp_path: Path) -> None:
    now = [900_000]
    with Scheduler(tmp_path / "clock.db", draft_fixture=True, clock=lambda: now[0]) as scheduler:
        scheduler.register(_job(schedule="*/5 * * * *", created_at=0))
        scheduler.mark_done("job-1", 600_000)
        now[0] = 800_000
        with pytest.raises(ValueError, match="clock"):
            scheduler.mark_done("job-1", 900_000)
        assert scheduler.get("job-1").next_run_at == 600_000
        assert len(scheduler.due_jobs(800_000)) == 1


def test_process_crash_after_poll_before_ack_replays_then_persists_completion(
    tmp_path: Path,
) -> None:
    path = tmp_path / "process.db"
    script = textwrap.dedent("""
        import os, sys
        from datetime import timedelta
        from jarvis.scheduler import ScheduledJob, Scheduler
        scheduler = Scheduler(sys.argv[1], draft_fixture=True, clock=lambda: 6000)
        scheduler.register(ScheduledJob(id='job-1', name='fixture', schedule=timedelta(seconds=5),
            handler_key='must.not.import', payload={}, created_at=1000))
        assert len(scheduler.due_jobs(6000)) == 1
        os._exit(73)
    """)
    crashed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(path)], capture_output=True, text=True, timeout=20
    )
    assert crashed.returncode == 73, crashed.stderr
    with Scheduler(path, draft_fixture=True, clock=lambda: 10_000) as scheduler:
        assert [j.id for j in scheduler.due_jobs(10_000)] == ["job-1"]
        scheduler.mark_done("job-1", None)
    readback = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            textwrap.dedent("""
        import sys
        from jarvis.scheduler import Scheduler
        with Scheduler(sys.argv[1], draft_fixture=True) as scheduler:
            assert scheduler.due_jobs(100_000) == []
            assert scheduler.get('job-1').last_run_at == 10_000
    """),
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert readback.returncode == 0, readback.stderr


def test_runtime_stays_disabled_and_no_handler_import_is_attempted(store: Scheduler) -> None:
    assert module.DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert module.RUNTIME_ENABLED is False
    assert store.runtime_enabled is False
    store.register(_job(handler_key="definitely.does.not.exist:handler"))
    assert store.due_jobs(6000)[0].handler_key == "definitely.does.not.exist:handler"
