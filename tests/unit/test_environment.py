"""Real SQLite and environment tests for the bootstrap deliverable."""

from pathlib import Path

from jarvis.environment import inspect_environment, probe_sqlite


def test_sqlite_baseline_and_cleanup(tmp_path: Path) -> None:
    result = probe_sqlite(tmp_path)
    assert result["journal_mode"] == "wal"
    assert result["foreign_keys"] is True
    assert result["synchronous"] == 2
    assert result["busy_timeout_ms"] == 5000
    assert result["integrity"] == "ok"
    assert list(tmp_path.iterdir()) == []


def test_probe_does_not_modify_existing_data(tmp_path: Path) -> None:
    existing = tmp_path / "jarvis.db"
    existing.write_bytes(b"user-data-not-a-database")
    result = inspect_environment(tmp_path)
    assert result["production_ready"] is False
    assert result["python_baseline"] is True
    assert existing.read_bytes() == b"user-data-not-a-database"
    assert list(tmp_path.iterdir()) == [existing]
