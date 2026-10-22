"""K0-016 DRAFT: health monitor, structured logging, resource limits tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jarvis.health import HealthMonitor, ResourceLimitError, ResourceLimits


def _monitor(**kwargs) -> HealthMonitor:
    return HealthMonitor(draft_fixture=True, **kwargs)


def test_worker_alive_with_own_pid() -> None:
    monitor = _monitor()
    assert monitor.worker_alive(os.getpid()) is True


def test_worker_alive_with_nonexistent_pid() -> None:
    monitor = _monitor()
    assert monitor.worker_alive(999999) is False


def test_worker_alive_with_zero_or_negative() -> None:
    monitor = _monitor()
    assert monitor.worker_alive(0) is False
    assert monitor.worker_alive(-1) is False


def test_file_size_limit_passes(tmp_path: Path) -> None:
    f = tmp_path / "small.bin"
    f.write_bytes(b"x" * 100)
    monitor = _monitor(limits=ResourceLimits(max_file_size_mb=1.0))
    monitor.check_file_size(f)  # Should not raise.


def test_file_size_limit_raises(tmp_path: Path) -> None:
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MB
    monitor = _monitor(limits=ResourceLimits(max_file_size_mb=1.0))
    with pytest.raises(ResourceLimitError, match="2.0 MB"):
        monitor.check_file_size(f)


def test_op_count_limit_passes() -> None:
    monitor = _monitor(limits=ResourceLimits(max_total_ops=100))
    monitor.check_op_count(50)  # Should not raise.


def test_op_count_limit_raises() -> None:
    monitor = _monitor(limits=ResourceLimits(max_total_ops=100))
    with pytest.raises(ResourceLimitError, match="150"):
        monitor.check_op_count(150)


def test_runtime_limit_passes() -> None:
    monitor = _monitor(limits=ResourceLimits(max_runtime_s=3600.0))
    monitor.check_runtime(1800.0)  # Should not raise.


def test_runtime_limit_raises() -> None:
    monitor = _monitor(limits=ResourceLimits(max_runtime_s=3600.0))
    with pytest.raises(ResourceLimitError, match="7200s"):
        monitor.check_runtime(7200.0)


def test_health_snapshot_contains_required_fields() -> None:
    monitor = _monitor()
    snap = monitor.snapshot()
    assert snap.timestamp_ms > 0
    assert isinstance(snap.worker_alive, bool)
    assert isinstance(snap.disk_free_mb, float)
    assert isinstance(snap.db_ok, bool)
    assert isinstance(snap.within_limits, bool)
    assert isinstance(snap.violations, list)


def test_health_snapshot_with_live_pid() -> None:
    monitor = _monitor()
    snap = monitor.snapshot(worker_pid=os.getpid())
    assert snap.worker_alive is True


def test_health_snapshot_with_dead_pid() -> None:
    monitor = _monitor()
    snap = monitor.snapshot(worker_pid=999999)
    assert snap.worker_alive is False
    assert snap.within_limits is False
    assert len(snap.violations) >= 1


def test_json_log_event_format() -> None:
    monitor = _monitor()
    entry = monitor.log_event("task_started", task_id="t1", epoch=7)
    assert entry["event"] == "task_started"
    assert entry["task_id"] == "t1"
    assert entry["epoch"] == 7
    assert "ts_ms" in entry
    # Must be JSON-serialisable.
    json.dumps(entry)


def test_log_entries_retained() -> None:
    monitor = _monitor()
    monitor.log_event("start")
    monitor.log_event("stop", reason="normal")
    assert len(monitor.log_entries) == 2
    assert monitor.log_entries[0]["event"] == "start"
    assert monitor.log_entries[1]["event"] == "stop"


def test_log_to_file(tmp_path: Path) -> None:
    log_file = tmp_path / "events.jsonl"
    monitor = _monitor(log_path=log_file)
    monitor.log_event("worker_started", pid=os.getpid())
    assert log_file.exists()
    lines = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["event"] == "worker_started"


def test_draft_fixture_required() -> None:
    with pytest.raises(ValueError, match="draft_fixture"):
        HealthMonitor()
