"""Read-only platform inspection and isolated SQLite baseline verification."""

import os
import platform
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import TypedDict


class SQLiteProbe(TypedDict):
    version: str
    journal_mode: str
    foreign_keys: bool
    synchronous: int
    busy_timeout_ms: int
    integrity: str


class EnvironmentReport(TypedDict):
    python: str
    python_baseline: bool
    platform: str
    machine: str
    uv_available: bool
    directory_fd_available: bool
    nofollow_available: bool
    sqlite: SQLiteProbe
    production_ready: bool


def probe_sqlite(directory: Path) -> SQLiteProbe:
    """Verify a disposable DB, never open application or production data."""
    with tempfile.TemporaryDirectory(prefix="jarvis-probe-", dir=directory) as temporary:
        with closing(sqlite3.connect(Path(temporary) / "probe.db")) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA busy_timeout = 5000")
            mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
            foreign_keys = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
            synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
            busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    return SQLiteProbe(
        version=sqlite3.sqlite_version,
        journal_mode=mode,
        foreign_keys=foreign_keys,
        synchronous=synchronous,
        busy_timeout_ms=busy_timeout,
        integrity=integrity,
    )


def inspect_environment(directory: Path) -> EnvironmentReport:
    """Capability presence is not proof of safe publish or power-loss durability."""
    return EnvironmentReport(
        python=platform.python_version(),
        python_baseline=sys.version_info[:2] == (3, 12),
        platform=platform.system(),
        machine=platform.machine(),
        uv_available=shutil.which("uv") is not None,
        directory_fd_available=os.open in os.supports_dir_fd,
        nofollow_available=hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
        sqlite=probe_sqlite(directory),
        production_ready=False,
    )
