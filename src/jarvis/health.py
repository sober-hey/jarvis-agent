"""K0-016 DRAFT: health monitor, structured logging, resource limits.

NOT_APPROVED / NOT_FROZEN / runtime_enabled=False.
Only used with explicit draft_fixture=True; never wired to production.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ResourceLimits:
    max_file_size_mb: float = 100.0
    max_total_ops: int = 10_000
    max_runtime_s: float = 86_400.0


@dataclass
class HealthSnapshot:
    timestamp_ms: int
    worker_alive: bool
    disk_free_mb: float
    memory_rss_mb: float | None
    db_ok: bool
    within_limits: bool
    violations: list[str] = field(default_factory=list)


class HealthMonitor:
    """Checks Worker liveness, disk/memory usage, DB connectivity, resource limits."""

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        log_path: Path | None = None,
        limits: ResourceLimits | None = None,
        draft_fixture: bool = False,
    ) -> None:
        if not draft_fixture:
            raise ValueError("explicit draft_fixture=True required")
        self._db_path = db_path
        self._log_path = log_path
        self._limits = limits or ResourceLimits()
        self._log_entries: list[dict] = []

    # ── liveness ──────────────────────────────────────────────────────────────

    def worker_alive(self, pid: int) -> bool:
        """Return True if the PID is running."""
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we can't signal it.
            return True

    # ── resource checks ───────────────────────────────────────────────────────

    def check_file_size(self, path: Path) -> None:
        """Raise if path exceeds max_file_size_mb."""
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > self._limits.max_file_size_mb:
            raise ResourceLimitError(
                f"{path.name} is {size_mb:.1f} MB, limit is {self._limits.max_file_size_mb} MB"
            )

    def check_op_count(self, total_ops: int) -> None:
        """Raise if total_ops exceeds max_total_ops."""
        if total_ops > self._limits.max_total_ops:
            raise ResourceLimitError(
                f"op count {total_ops} exceeds limit {self._limits.max_total_ops}"
            )

    def check_runtime(self, elapsed_s: float) -> None:
        """Raise if elapsed_s exceeds max_runtime_s."""
        if elapsed_s > self._limits.max_runtime_s:
            raise ResourceLimitError(
                f"runtime {elapsed_s:.0f}s exceeds limit {self._limits.max_runtime_s:.0f}s"
            )

    # ── snapshot ──────────────────────────────────────────────────────────────

    def snapshot(self, *, worker_pid: int | None = None) -> HealthSnapshot:
        """Produce a summary health snapshot."""
        now_ms = int(time.time() * 1000)

        # Disk free (same volume as db_path or cwd).
        check_path = str(self._db_path) if self._db_path else "."
        try:
            stat = os.statvfs(check_path)
            disk_free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
        except Exception:
            disk_free_mb = -1.0

        # Memory RSS (best-effort, not available on all platforms).
        memory_rss_mb: float | None = None
        try:
            import resource  # POSIX only

            usage = resource.getrusage(resource.RUSAGE_SELF)
            # ru_maxrss is bytes on Linux, pages on macOS (4 KB).
            if hasattr(os, "sysconf"):
                page = os.sysconf("SC_PAGESIZE") if "SC_PAGESIZE" in os.sysconf_names else 4096  # type: ignore[attr-defined]
                memory_rss_mb = (usage.ru_maxrss * page) / (1024 * 1024)
            else:
                memory_rss_mb = usage.ru_maxrss / (1024 * 1024)
        except Exception:
            memory_rss_mb = None

        # DB check.
        db_ok = False
        if self._db_path and self._db_path.exists():
            try:
                with sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True) as conn:
                    result = conn.execute("PRAGMA integrity_check").fetchone()
                    db_ok = result == ("ok",)
            except Exception:
                db_ok = False

        # Worker liveness.
        worker_alive = worker_pid is not None and self.worker_alive(worker_pid)

        violations: list[str] = []
        within_limits = True
        if not worker_alive and worker_pid is not None:
            violations.append(f"worker PID {worker_pid} not alive")
            within_limits = False
        if not db_ok and self._db_path is not None:
            violations.append("database integrity check failed")

        return HealthSnapshot(
            timestamp_ms=now_ms,
            worker_alive=worker_alive,
            disk_free_mb=disk_free_mb,
            memory_rss_mb=memory_rss_mb,
            db_ok=db_ok,
            within_limits=within_limits,
            violations=violations,
        )

    # ── structured logging ────────────────────────────────────────────────────

    def log_event(self, event: str, **kwargs) -> dict:
        """Record a structured JSON log entry."""
        entry = {
            "ts_ms": int(time.time() * 1000),
            "event": event,
            **kwargs,
        }
        self._log_entries.append(entry)
        if self._log_path:
            with open(self._log_path, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    @property
    def log_entries(self) -> list[dict]:
        return list(self._log_entries)


class ResourceLimitError(Exception):
    """Raised when a resource limit is exceeded."""
