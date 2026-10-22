"""K3-008 offline recovery markers, never operation replay."""

from __future__ import annotations

import importlib
import importlib.util
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from jarvis.event_schema import Event, EventPriority, EventType
from jarvis.event_store import EventStore
from jarvis.outbox import OutboxChannel, OutboxMessage, OutboxWriter, create_outbox_schema
from jarvis.scheduler import ScheduledJob, Scheduler


def _module() -> Any:
    # Assert the missing feature as a RED failure, not a collection/import error.
    assert importlib.util.find_spec("jarvis.recovery") is not None, "recovery draft missing"
    return importlib.import_module("jarvis.recovery")


@pytest.fixture
def sources(tmp_path: Path) -> dict[str, Path]:
    paths = {name: tmp_path / f"{name}.db" for name in ("scheduler", "event_store", "outbox")}
    with Scheduler(paths["scheduler"], draft_fixture=True) as scheduler:
        scheduler.register(
            ScheduledJob(
                id="job",
                name="fixture",
                schedule=timedelta(seconds=1),
                handler_key="never.execute",
                payload={"value": 1},
                created_at=1000,
            )
        )
    with sqlite3.connect(paths["event_store"]) as connection:
        store = EventStore(connection)
        store.append(
            Event(
                id="event",
                type=EventType.OBSERVATION,
                priority=EventPriority.NORMAL,
                source="fixture",
                payload={},
                created_at=1000,
                session_id="timeline",
            )
        )
    with sqlite3.connect(paths["outbox"]) as connection:
        create_outbox_schema(connection, draft_fixture=True)
        connection.execute("BEGIN")
        OutboxWriter(draft_fixture=True).enqueue(
            OutboxMessage(
                id="message",
                channel=OutboxChannel.EMAIL,
                recipient="fixture.invalid",
                subject="fixture",
                body="never send",
                created_at=1000,
                scheduled_at=2000,
                idempotency_key="message-key",
            ),
            connection,
        )
    return paths


def _manager(
    sources: dict[str, Path],
    *,
    session_id: str = "boot-1",
    clock: Callable[[], int] = lambda: 3000,
) -> Any:
    return _module().RecoveryManager(
        scheduler_database=sources["scheduler"],
        event_store_database=sources["event_store"],
        outbox_database=sources["outbox"],
        session_id=session_id,
        clock=clock,
        draft_fixture=True,
    )


def test_checkpoint_persists_across_reopen(tmp_path: Path, sources: dict[str, Path]) -> None:
    manager = _manager(sources)
    path = tmp_path / "recovery.db"
    with sqlite3.connect(path) as connection:
        point = manager.checkpoint(connection)
        assert point.timestamp == 3000
        assert point.event_store_high_watermark == 1
        assert point.outbox_pending_count == 1
        assert point.session_id == "boot-1"
        assert point.scheduler_checkpoint["jobs"][0]["next_run_at"] == 2000
        assert not connection.in_transaction
    with sqlite3.connect(path) as reopened:
        assert manager.list_checkpoints(reopened) == [point]
        assert reopened.execute("SELECT COUNT(*) FROM recovery_checkpoints").fetchone() == (1,)


def test_recover_loads_old_markers_and_observes_new_state(
    tmp_path: Path,
    sources: dict[str, Path],
) -> None:
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        manager = _manager(sources)
        point = manager.checkpoint(connection)
        with Scheduler(sources["scheduler"], draft_fixture=True) as scheduler:
            scheduler.pause("job")
        with sqlite3.connect(sources["event_store"]) as events:
            EventStore(events).append(
                Event(
                    id="later",
                    type=EventType.ACTION,
                    priority=EventPriority.NORMAL,
                    source="fixture",
                    payload={},
                    created_at=1000,
                    session_id="timeline",
                )
            )
        result = _manager(sources, session_id="boot-2", clock=lambda: 9000).recover(connection)
        assert result.recovery_point == point
        assert result.recovery_point.scheduler_checkpoint["jobs"][0]["status"] == "ACTIVE"
        assert result.current_scheduler_checkpoint["jobs"][0]["status"] == "PAUSED"
        assert result.current_event_store_high_watermark == 2
        assert result.current_outbox_pending_count == 1
        assert result.events_after_checkpoint == 1
        assert result.crash_count == 1
        assert result.last_crash_at == 9000
        assert connection.execute(
            "SELECT recovered_checkpoint FROM recovery_sessions WHERE session_id='boot-2'"
        ).fetchone() == (1,)


def test_first_start_probe_does_not_initialize_or_write(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "empty.db") as connection:
        probe = _module().StartupProbe()
        assert probe.is_first_start(connection)
        assert probe.is_clean_shutdown(connection)
        assert connection.total_changes == 0
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_startup_and_clean_shutdown_transitions(tmp_path: Path, sources: dict[str, Path]) -> None:
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        manager = _manager(sources)
        probe = _module().StartupProbe()
        assert manager.list_checkpoints(connection) == []
        result = manager.recover(connection)
        assert result.recovery_point is None
        assert result.events_after_checkpoint == 1
        assert result.crash_count == 0
        assert not probe.is_first_start(connection)
        assert not probe.is_clean_shutdown(connection)
        manager.checkpoint(connection)  # A checkpoint is NOT a shutdown marker.
        assert not probe.is_clean_shutdown(connection)
        manager.mark_clean_shutdown(connection)
        assert probe.is_clean_shutdown(connection)
        changes = connection.total_changes
        manager.mark_clean_shutdown(connection)
        assert connection.total_changes == changes
        result = _manager(sources, session_id="boot-2").recover(connection)
        assert result.crash_count == 0
        assert result.last_crash_at is None


def test_unclean_shutdown_increments_once_per_predecessor(
    tmp_path: Path,
    sources: dict[str, Path],
) -> None:
    path = tmp_path / "recovery.db"
    with sqlite3.connect(path) as connection:
        _manager(sources).checkpoint(connection)
    with sqlite3.connect(path) as connection:
        manager = _manager(sources, session_id="boot-2", clock=lambda: 4000)
        assert manager.recover(connection).crash_count == 1
        assert manager.recover(connection).crash_count == 1  # Same process wake/resume.
        manager.checkpoint(connection)
        assert _module().StartupProbe().status(connection).last_crash_at == 4000
    with sqlite3.connect(path) as connection:
        result = _manager(sources, session_id="boot-3", clock=lambda: 5000).recover(connection)
        assert result.crash_count == 2
        assert result.last_crash_at == 5000


def test_checkpoint_without_recover_also_detects_unclean_predecessor(
    tmp_path: Path,
    sources: dict[str, Path],
) -> None:
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        _manager(sources).checkpoint(connection)
        _manager(sources, session_id="boot-2").checkpoint(connection)
        assert _module().StartupProbe().status(connection).crash_count == 1


def test_sorted_checkpoints_survive_ties_and_backward_clock(
    tmp_path: Path,
    sources: dict[str, Path],
) -> None:
    times = iter([3000, 3000, 2000, 4000])
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        manager = _manager(sources, clock=lambda: next(times))
        points = [manager.checkpoint(connection) for _ in range(3)]
        assert manager.list_checkpoints(connection) == list(reversed(points))
        assert manager.recover(connection).recovery_point == points[-1]


def test_recovery_does_not_replay_or_modify_subsystems(
    tmp_path: Path,
    sources: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("operation replay/acknowledgement is forbidden")

    from jarvis.outbox import OutboxRelay

    for cls, method in (
        (EventStore, "replay"),
        (EventStore, "checkpoint"),
        (Scheduler, "mark_done"),
        (Scheduler, "resume"),
        (OutboxRelay, "poll"),
        (OutboxRelay, "mark_delivered"),
    ):
        monkeypatch.setattr(cls, method, forbidden)
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        manager = _manager(sources)
        before = {}
        for name, path in sources.items():
            with sqlite3.connect(path) as source:
                before[name] = list(source.iterdump())
        point = manager.checkpoint(connection)
        result = manager.recover(connection)
        assert result.runtime_enabled is False
        assert manager.runtime_enabled is False
        point.scheduler_checkpoint["jobs"][0]["payload"]["value"] = 999
        assert manager.list_checkpoints(connection)[0].scheduler_checkpoint["jobs"][0][
            "payload"
        ] == {"value": 1}
        for name, path in sources.items():
            with sqlite3.connect(path) as source:
                assert list(source.iterdump()) == before[name]


def test_failed_checkpoint_rolls_back_session_and_crash_marker(
    tmp_path: Path,
    sources: dict[str, Path],
) -> None:
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        _manager(sources).checkpoint(connection)
        before = list(connection.iterdump())

        def authorize(action: int, table: str | None, *args: Any) -> int:
            if action == sqlite3.SQLITE_INSERT and table == "recovery_checkpoints":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        with pytest.raises(sqlite3.DatabaseError):
            _manager(sources, session_id="boot-2").checkpoint(connection)
        connection.set_authorizer(None)
        assert list(connection.iterdump()) == before
        assert not connection.in_transaction


def test_failed_recovery_rolls_back_markers(tmp_path: Path, sources: dict[str, Path]) -> None:
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        _manager(sources).checkpoint(connection)
        before = list(connection.iterdump())
        sources["outbox"].unlink()
        with pytest.raises(sqlite3.OperationalError):
            _manager(sources, session_id="boot-2").recover(connection)
        assert not sources["outbox"].exists()  # mode=ro never creates a missing source.
        assert list(connection.iterdump()) == before
        assert not connection.in_transaction


def test_unchanged_ledger_on_failed_bootstrap(tmp_path: Path, sources: dict[str, Path]) -> None:
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        sources["outbox"].unlink()
        with pytest.raises(sqlite3.OperationalError):
            _manager(sources).checkpoint(connection)
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


@pytest.mark.parametrize("operation", ["checkpoint", "recover", "mark_clean_shutdown"])
def test_stale_session_cannot_write(
    tmp_path: Path,
    sources: dict[str, Path],
    operation: str,
) -> None:
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        old = _manager(sources)
        old.checkpoint(connection)
        _manager(sources, session_id="boot-2").recover(connection)
        before = list(connection.iterdump())
        with pytest.raises(ValueError, match="stale|current"):
            getattr(old, operation)(connection)
        assert list(connection.iterdump()) == before


@pytest.mark.parametrize("operation", ["checkpoint", "recover"])
def test_cleanly_closed_session_cannot_reactivate(
    tmp_path: Path,
    sources: dict[str, Path],
    operation: str,
) -> None:
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        manager = _manager(sources)
        manager.checkpoint(connection)
        manager.mark_clean_shutdown(connection)
        before = list(connection.iterdump())
        with pytest.raises(ValueError, match="closed"):
            getattr(manager, operation)(connection)
        assert list(connection.iterdump()) == before


@pytest.mark.parametrize("operation", ["checkpoint", "recover", "list_checkpoints"])
def test_rejects_caller_transaction_without_rolling_back(
    tmp_path: Path,
    sources: dict[str, Path],
    operation: str,
) -> None:
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        manager = _manager(sources)
        manager.checkpoint(connection)
        connection.execute("BEGIN")
        with pytest.raises(ValueError, match="idle"):
            getattr(manager, operation)(connection)
        assert connection.in_transaction
        connection.rollback()


def test_rejects_core_or_unknown_ledger_without_writes(
    tmp_path: Path,
    sources: dict[str, Path],
) -> None:
    with sqlite3.connect(tmp_path / "core.db") as connection:
        connection.execute("CREATE TABLE tasks (id TEXT)")
        before = list(connection.iterdump())
        with pytest.raises(ValueError, match="standalone"):
            _manager(sources).checkpoint(connection)
        with pytest.raises(ValueError, match="standalone"):
            _module().StartupProbe().is_first_start(connection)
        assert list(connection.iterdump()) == before


def test_changed_source_binding_is_rejected(tmp_path: Path, sources: dict[str, Path]) -> None:
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        _manager(sources).checkpoint(connection)
        replacement = dict(sources, outbox=tmp_path / "other.db")
        with pytest.raises(ValueError, match="different"):
            _manager(replacement, session_id="boot-2").recover(connection)
        assert _module().StartupProbe().status(connection).crash_count == 0


def test_regressed_event_store_is_not_recovered(tmp_path: Path, sources: dict[str, Path]) -> None:
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        _manager(sources).checkpoint(connection)
        before = list(connection.iterdump())
        sources["event_store"].unlink()
        with sqlite3.connect(sources["event_store"]) as events:
            EventStore(events)
        with pytest.raises(ValueError, match="regressed"):
            _manager(sources, session_id="boot-2").recover(connection)
        assert list(connection.iterdump()) == before


def test_pending_count_includes_retries_and_future_but_not_dead_letters(
    tmp_path: Path,
    sources: dict[str, Path],
) -> None:
    from jarvis.outbox import OutboxRelay

    with sqlite3.connect(sources["outbox"]) as outbox:
        outbox.execute("BEGIN")
        OutboxRelay(draft_fixture=True, clock=lambda: 3000).mark_failed("message", "error", outbox)
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        manager = _manager(sources)
        assert manager.checkpoint(connection).outbox_pending_count == 1
        with sqlite3.connect(sources["outbox"]) as outbox:
            outbox.execute("UPDATE outbox SET attempts=5 WHERE id='message'")
        assert manager.checkpoint(connection).outbox_pending_count == 0


def test_requires_opt_in(sources: dict[str, Path]) -> None:
    with pytest.raises(ValueError, match="draft_fixture"):
        _module().RecoveryManager(
            scheduler_database=sources["scheduler"],
            event_store_database=sources["event_store"],
            outbox_database=sources["outbox"],
        )


@pytest.mark.parametrize("value", [True, -1, 1.1, 2**63])
def test_recovery_point_rejects_invalid_millisecond_values(value: Any) -> None:
    with pytest.raises(ValueError):
        _module().RecoveryPoint(value, {}, 0, 0, "boot")


def test_process_exit_then_new_process_recovers_committed_checkpoint(
    tmp_path: Path,
    sources: dict[str, Path],
) -> None:
    path = tmp_path / "recovery.db"
    script = """
import json, os, sqlite3, sys
from jarvis.recovery import RecoveryManager
manager = RecoveryManager(
    scheduler_database=sys.argv[2], event_store_database=sys.argv[3],
    outbox_database=sys.argv[4], session_id=sys.argv[5],
    clock=lambda: 5000, draft_fixture=True,
)
connection = sqlite3.connect(sys.argv[1])
if sys.argv[5] == 'child-1':
    manager.checkpoint(connection)
    os._exit(23)
result = manager.recover(connection)
assert result.recovery_point.session_id == 'child-1'
assert result.recovery_point.event_store_high_watermark == 1
assert result.recovery_point.scheduler_checkpoint['jobs'][0]['next_run_at'] == 2000
assert result.current_outbox_pending_count == 1
assert result.crash_count == 1
manager.mark_clean_shutdown(connection)
connection.close()
print(json.dumps({'crash_count': result.crash_count, 'ok': True}))
"""
    arguments = [
        str(path),
        *(str(sources[name]) for name in ("scheduler", "event_store", "outbox")),
    ]
    first = subprocess.run(
        [sys.executable, "-c", script, *arguments, "child-1"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert first.returncode == 23, first.stderr
    second = subprocess.run(
        [sys.executable, "-c", script, *arguments, "child-2"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    assert '"ok": true' in second.stdout
    with sqlite3.connect(path) as connection:
        assert _module().StartupProbe().is_clean_shutdown(connection)
        assert _module().StartupProbe().status(connection).crash_count == 1


def test_unknown_source_schema_is_rejected_without_markers(
    tmp_path: Path,
    sources: dict[str, Path],
) -> None:
    with sqlite3.connect(sources["scheduler"]) as source:
        source.execute("CREATE TABLE unapproved (value TEXT)")
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        with pytest.raises(ValueError, match="scheduler"):
            _manager(sources).recover(connection)
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_event_sequence_gaps_are_not_counted_as_pending_events(
    tmp_path: Path,
    sources: dict[str, Path],
) -> None:
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        manager = _manager(sources)
        manager.checkpoint(connection)
        with sqlite3.connect(sources["event_store"]) as events:
            events.execute("UPDATE sqlite_sequence SET seq=10 WHERE name='durable_events'")
        with sqlite3.connect(sources["event_store"]) as events:
            EventStore(events).append(
                Event(
                    id="gapped",
                    type=EventType.OBSERVATION,
                    priority=EventPriority.LOW,
                    source="fixture",
                    payload={},
                    created_at=1000,
                    session_id="timeline",
                )
            )
        result = manager.recover(connection)
        assert result.current_event_store_high_watermark == 11
        assert result.events_after_checkpoint == 1


def test_read_only_probes_do_not_increment_crashes(
    tmp_path: Path,
    sources: dict[str, Path],
) -> None:
    with sqlite3.connect(tmp_path / "recovery.db") as connection:
        manager = _manager(sources)
        manager.checkpoint(connection)
        before = connection.total_changes
        new_manager = _manager(sources, session_id="boot-2")
        for _ in range(3):
            assert new_manager.list_checkpoints(connection)
            assert not _module().StartupProbe().is_clean_shutdown(connection)
            assert _module().StartupProbe().status(connection).crash_count == 0
        assert connection.total_changes == before


def test_inventory_closes_all_its_connections(
    tmp_path: Path,
    sources: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[sqlite3.Connection] = []
    original = sqlite3.connect

    def tracked(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original(*args, **kwargs)
        opened.append(connection)
        return connection

    with original(tmp_path / "recovery.db") as ledger:
        monkeypatch.setattr(sqlite3, "connect", tracked)
        _manager(sources).checkpoint(ledger)
        assert len(opened) == 4
        for connection in opened:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
