"""Offline K4-003 version history candidate; no runtime activation."""

import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import util
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from jarvis import version_manager as module
from jarvis.version_manager import SemanticVersion


def test_version_manager_draft_module_exists() -> None:
    assert util.find_spec("jarvis.version_manager") is not None


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        ("0.0.0", (0, 0, 0, None, None)),
        ("12.3.45", (12, 3, 45, None, None)),
        ("1.2.3-alpha.1+build.001", (1, 2, 3, "alpha.1", "build.001")),
        ("1.0.0-0.a-b+001", (1, 0, 0, "0.a-b", "001")),
    ],
)
def test_semantic_version_parse_roundtrip(text: str, parts: tuple[object, ...]) -> None:
    version_type = SemanticVersion
    version = version_type.parse(text)
    assert (
        version.major,
        version.minor,
        version.patch,
        version.pre_release,
        version.build_metadata,
    ) == parts
    assert version.to_str() == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1",
        "1.2",
        "v1.2.3",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "-1.2.3",
        "1.2.3-",
        "1.2.3+",
        "1.2.3-a..b",
        "1.2.3+a..b",
        "1.2.3-01",
        "1.2.3-alpha.01",
        "1.2.3-α",
        "１.2.3",
        "1.2.3 ",
        "1.2.3\n",
        " 1.2.3",
    ],
)
def test_semantic_version_parse_rejects_invalid_input(text: str) -> None:
    with pytest.raises(ValueError):
        SemanticVersion.parse(text)


def test_official_semver_precedence_and_metadata() -> None:
    version_type = SemanticVersion
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.1.0",
        "2.0.0",
    ]
    versions = list(map(version_type.parse, ordered))
    for index, left in enumerate(versions):
        for other_index, right in enumerate(versions):
            expected = (index > other_index) - (index < other_index)
            assert left.compare(right) == expected
    left = version_type.parse("1.0.0+a")
    right = version_type.parse("1.0.0+b")
    assert left.compare(right) == 0
    assert left != right  # Identity is distinct from precedence.


@pytest.mark.parametrize("parts", [(-1, 0, 0), (True, 0, 0), (1, 1.0, 0)])
def test_semantic_version_constructor_validates(parts: tuple[Any, ...]) -> None:
    with pytest.raises(ValueError):
        SemanticVersion(*parts)


def _record(version: str, **overrides: object) -> Any:
    fields: dict[str, Any] = dict(
        component="jarvis",
        version=module.SemanticVersion.parse(version),
        changelog="Initial version",
        author="fixture-author",
        created_at=datetime(2026, 10, 18, tzinfo=UTC),
        git_sha="a" * 40,
        test_results={"passed": True, "failed": 0, "errors": 0},
    )
    fields.update(overrides)
    return module.VersionRecord(**fields)


def _manager(path: Path) -> Any:
    return module.VersionManager(path)


def test_record_history_and_reopen(tmp_path: Path) -> None:
    first = _record("1.0.0")
    # Record order remains causal even when observation clocks move backwards.
    second = _record(
        "1.1.0", changelog="Improvement", created_at=first.created_at - timedelta(days=1)
    )
    database = tmp_path / "versions.db"
    with _manager(database) as manager:
        assert manager.history("jarvis") == []
        assert manager.current("jarvis") is None
        assert manager.rollback_candidate("jarvis") is None
        assert manager.record(first) == first
        manager.record(_record("9.0.0", component="other"))
        manager.record(second)
        assert manager.current("jarvis") == second
        assert manager.history("jarvis") == [first, second]
    with _manager(database) as reopened:
        assert reopened.current("jarvis") == second
        assert reopened.history("other")[0].version.to_str() == "9.0.0"


def test_duplicate_replay_idempotent_but_conflict_rejected(tmp_path: Path) -> None:
    first = _record("1.0.0")
    with _manager(tmp_path / "versions.db") as manager:
        manager.record(first)
        manager.record(_record("1.1.0"))
        assert manager.record(first) == first
        with pytest.raises(ValueError, match="different"):
            manager.record(replace(first, changelog="overwrite"))
        assert len(manager.history("jarvis")) == 2
        assert manager.current("jarvis").version.to_str() == "1.1.0"


def test_snapshots_detach_mutable_test_results(tmp_path: Path) -> None:
    results: dict[str, Any] = {"passed": True, "details": {"checks": ["unit"]}}
    first = _record("1.0.0", test_results=results)
    results["details"]["checks"].append("external-mutation")
    with _manager(tmp_path / "versions.db") as manager:
        saved = manager.record(first)
        first.test_results["details"]["checks"].append("after-save")
        saved.test_results["details"]["checks"].append("returned-record")
        fetched = manager.current("jarvis")
        assert fetched.test_results["details"]["checks"] == ["unit"]
        fetched.test_results.clear()
        assert manager.current("jarvis").test_results["passed"] is True


def test_diff_tracks_endpoints_and_intermediate_records(tmp_path: Path) -> None:
    first = _record("1.0.0", changelog="Keep\nOld")
    middle = _record("1.0.1", changelog="Intermediate fix")
    last = _record(
        "1.1.0",
        changelog="Keep\nNew",
        author="reviewer",
        git_sha="b" * 40,
        test_results={"passed": True, "total": 42},
    )
    with _manager(tmp_path / "versions.db") as manager:
        for record in [first, middle, last]:
            manager.record(record)
        diff = manager.diff("jarvis", "1.0.0", last.version)
        assert diff.component == "jarvis"
        assert diff.from_version == first.version
        assert diff.to_version == last.version
        assert diff.records == (middle, last)
        assert "-Old" in diff.changelog_diff
        assert "+New" in diff.changelog_diff
        assert diff.changes["author"] == ("fixture-author", "reviewer")
        assert diff.changes["git_sha"] == ("a" * 40, "b" * 40)
        assert diff.changes["test_results"] == (first.test_results, last.test_results)
        assert "created_at" not in diff.changes
        reverse = manager.diff("jarvis", last.version, first.version)
        assert reverse.records == (middle, first)
        assert reverse.changes["author"] == ("reviewer", "fixture-author")
        same = manager.diff("jarvis", first.version, first.version)
        assert same.records == ()
        assert same.changes == {}
        assert same.changelog_diff == ""
        with pytest.raises(KeyError):
            manager.diff("jarvis", "1.0.0", "9.0.0")
        with pytest.raises(KeyError):
            manager.diff("absent", "1.0.0", "1.1.0")


def test_build_variants_are_distinct_records(tmp_path: Path) -> None:
    with _manager(tmp_path / "versions.db") as manager:
        a = _record("1.0.0+a")
        b = _record("1.0.0+b")
        manager.record(a)
        manager.record(b)
        assert manager.history("jarvis") == [a, b]
        assert manager.diff("jarvis", a.version, b.version).records == (b,)


def test_rollback_excludes_current_prerelease_failed_and_unknown(tmp_path: Path) -> None:
    safe = _record("1.0.0")
    with _manager(tmp_path / "versions.db") as manager:
        manager.record(safe)
        assert manager.rollback_candidate("jarvis") is None
        manager.record(_record("1.0.1", test_results={"passed": False}))
        manager.record(_record("1.1.0-alpha"))
        manager.record(_record("1.1.0", test_results={}))
        manager.record(_record("1.2.0"))
        assert manager.rollback_candidate("jarvis") == safe
        manager.record(_record("1.2.1"))
        assert manager.rollback_candidate("jarvis").version.to_str() == "1.2.0"


@pytest.mark.parametrize(
    "results",
    [
        {"passed": 1},
        {"passed": "true"},
        {"passed": True, "failed": 1},
        {"passed": True, "errors": 1},
        {"passed": True, "failed": False},
    ],
)
def test_rollback_requires_unambiguous_success(tmp_path: Path, results: dict[str, Any]) -> None:
    with _manager(tmp_path / "versions.db") as manager:
        manager.record(_record("1.0.0", test_results=results))
        manager.record(_record("1.0.1"))
        assert manager.rollback_candidate("jarvis") is None


def test_major_change_requires_explicit_exact_boolean_each_time(tmp_path: Path) -> None:
    with _manager(tmp_path / "versions.db") as manager:
        manager.record(_record("1.0.0"))
        for invalid_flag in [False, 1, "yes", None]:
            with pytest.raises((ValueError, PermissionError)):
                manager.record(_record("2.0.0"), allow_major_change=invalid_flag)
        assert len(manager.history("jarvis")) == 1
        manager.record(_record("2.0.0"), allow_major_change=True)
        with pytest.raises(PermissionError):
            manager.record(_record("3.0.0"))
        with pytest.raises(PermissionError):
            manager.record(_record("1.1.0"))  # Major decrease also requires authorization.
        manager.record(_record("1.1.0"), allow_major_change=True)
        assert manager.current("jarvis").version.to_str() == "1.1.0"


def test_authorized_cross_major_history_is_not_automatic_rollback(tmp_path: Path) -> None:
    with _manager(tmp_path / "versions.db") as manager:
        manager.record(_record("1.0.0"))
        manager.record(_record("2.0.0"), allow_major_change=True)
        assert manager.rollback_candidate("jarvis") is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"component": " "},
        {"component": "bad\x00name"},
        {"author": ""},
        {"created_at": datetime(2026, 10, 18)},
        {"version": "1.0.0"},
        {"git_sha": "not-a-sha"},
        {"git_sha": "a" * 39},
        {"test_results": {1: "bad-key"}},
        {"test_results": {"tuple": (1, 2)}},
        {"test_results": {"nan": float("nan")}},
    ],
)
def test_record_rejects_invalid_values(overrides: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        _record("1.0.0", **overrides)


def test_record_rejects_cycles_and_revalidates_mutable_data(tmp_path: Path) -> None:
    cycle: dict[str, Any] = {}
    cycle["self"] = cycle
    with pytest.raises(ValueError):
        _record("1.0.0", test_results=cycle)
    first = _record("1.0.0")
    first.test_results["bad"] = float("inf")
    with _manager(tmp_path / "versions.db") as manager:
        with pytest.raises(ValueError):
            manager.record(first)
        assert manager.history("jarvis") == []


def test_empty_changelog_optional_sha_and_timezone_roundtrip(tmp_path: Path) -> None:
    first = _record(
        "1.0.0",
        changelog="",
        git_sha=None,
        created_at=datetime.fromisoformat("2026-10-18T12:00:00+08:00"),
    )
    with _manager(tmp_path / "versions.db") as manager:
        manager.record(first)
        assert manager.current("jarvis") == first


def test_draft_database_rejects_unrelated_and_tampered_schema(tmp_path: Path) -> None:
    unrelated = tmp_path / "unrelated.db"
    with sqlite3.connect(unrelated) as connection:
        connection.execute("CREATE TABLE tasks(id TEXT)")
    with pytest.raises(ValueError, match="standalone"):
        _manager(unrelated)
    database = tmp_path / "versions.db"
    with _manager(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER draft_version_records_no_update")
    with pytest.raises(ValueError, match="schema"):
        _manager(database)


def test_history_is_append_only_at_sql_level(tmp_path: Path) -> None:
    database = tmp_path / "versions.db"
    with _manager(database) as manager:
        manager.record(_record("1.0.0"))
    with sqlite3.connect(database) as connection:
        for statement in [
            "UPDATE draft_version_records SET author='changed'",
            "DELETE FROM draft_version_records",
        ]:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement)


def test_close_and_draft_boundary(tmp_path: Path) -> None:
    assert module.RUNTIME_ENABLED is False
    assert module.DRAFT_STATUS == "DRAFT_PENDING_DP"
    manager = _manager(tmp_path / "versions.db")
    manager.close()
    manager.close()
    with pytest.raises(RuntimeError, match="closed"):
        manager.current("jarvis")


def test_replace_cannot_overwrite_history(tmp_path: Path) -> None:
    database = tmp_path / "versions.db"
    with _manager(database) as manager:
        first = manager.record(_record("1.0.0"))
        with sqlite3.connect(database) as connection:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(
                    "INSERT OR REPLACE INTO draft_version_records "
                    "(component,version,changelog,author,created_at,git_sha,test_results,"
                    "major_change_authorized) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        first.component,
                        "1.0.0",
                        "overwritten",
                        first.author,
                        first.created_at.isoformat(),
                        first.git_sha,
                        "{}",
                        0,
                    ),
                )
        assert manager.current("jarvis") == first


def test_sqlite_like_named_user_table_is_not_ignored(tmp_path: Path) -> None:
    database = tmp_path / "unrelated.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sqliteXowner(id TEXT)")
    with pytest.raises(ValueError, match="standalone"):
        _manager(database)


def test_replay_rejects_json_bool_integer_collision(tmp_path: Path) -> None:
    with _manager(tmp_path / "versions.db") as manager:
        first = manager.record(_record("1.0.0", test_results={"passed": True}))
        with pytest.raises(ValueError, match="different"):
            manager.record(replace(first, test_results={"passed": 1}))


def test_diff_detects_json_type_changes_and_changelog_line_endings(tmp_path: Path) -> None:
    with _manager(tmp_path / "versions.db") as manager:
        manager.record(_record("1.0.0", test_results={"passed": True}, changelog="Same\n"))
        manager.record(_record("1.0.1", test_results={"passed": 1}, changelog="Same"))
        diff = manager.diff("jarvis", "1.0.0", "1.0.1")
        assert "test_results" in diff.changes
        assert diff.changelog_diff


def test_independent_connections_serialize_baseline_major_checks(tmp_path: Path) -> None:
    database = tmp_path / "versions.db"
    with _manager(database):
        pass
    barrier = Barrier(2)

    def writer(version: str) -> str:
        with _manager(database) as manager:
            barrier.wait(timeout=5)
            try:
                manager.record(_record(version))
            except PermissionError:
                return "denied"
            return "recorded"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(writer, ["1.0.0", "2.0.0"]))
    assert sorted(outcomes) == ["denied", "recorded"]
    with _manager(database) as manager:
        assert len(manager.history("jarvis")) == 1


def test_write_failure_rolls_back_and_connection_remains_usable(tmp_path: Path) -> None:
    database = tmp_path / "versions.db"
    with _manager(database) as manager:
        manager.record(_record("1.0.0"))
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TRIGGER fixture_fail BEFORE INSERT ON draft_version_records "
                "WHEN NEW.version='1.0.1' BEGIN SELECT RAISE(ABORT,'injected failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            manager.record(_record("1.0.1"))
        assert len(manager.history("jarvis")) == 1
        with sqlite3.connect(database) as connection:
            connection.execute("DROP TRIGGER fixture_fail")
        manager.record(_record("1.0.1"))
        assert len(manager.history("jarvis")) == 2


def test_committed_history_survives_fresh_process_hard_exit(tmp_path: Path) -> None:
    database = tmp_path / "versions.db"
    program = """
import os
import sys
from datetime import UTC, datetime
from jarvis.version_manager import SemanticVersion, VersionManager, VersionRecord
manager = VersionManager(sys.argv[1])
manager.record(VersionRecord('jarvis', SemanticVersion.parse('1.0.0'), 'committed',
                             'fixture', datetime.now(UTC), test_results={'passed': True}))
# The process dies without manager.close(); SQLite recovers only committed data.
connection = manager._connection
connection.execute('BEGIN IMMEDIATE')
connection.execute("INSERT INTO draft_version_records "
                   "(component,version,changelog,author,created_at,test_results,"
                   "major_change_authorized) VALUES(?,?,?,?,?,?,?)",
                   ('jarvis', '1.0.1', 'uncommitted', 'fixture',
                    datetime.now(UTC).isoformat(), '{}', 0))
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(database)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    with _manager(database) as manager:
        assert [record.version.to_str() for record in manager.history("jarvis")] == ["1.0.0"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_values_are_bound_not_interpolated_sql(tmp_path: Path) -> None:
    component = "jarvis'; DROP TABLE draft_version_records;--"
    with _manager(tmp_path / "versions.db") as manager:
        first = _record("1.0.0", component=component)
        manager.record(first)
        assert manager.current(component) == first
        assert manager.history("jarvis") == []


@pytest.mark.parametrize(
    "parts", [(1, 0, 0, "01", None), (1, 0, 0, "", None), (1, 0, 0, None, "a..b")]
)
def test_constructor_rejects_invalid_identifiers(parts: tuple[Any, ...]) -> None:
    with pytest.raises(ValueError):
        SemanticVersion(*parts)


def test_commit_failure_rolls_back_before_returning_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingCommit(sqlite3.Connection):
        fail_next = False

        def commit(self) -> None:
            if type(self).fail_next:
                type(self).fail_next = False
                raise sqlite3.OperationalError("injected commit failure")
            super().commit()

    real_connect = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> FailingCommit:
        kwargs["factory"] = FailingCommit
        connection = real_connect(*args, **kwargs)
        assert isinstance(connection, FailingCommit)
        return connection

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    database = tmp_path / "versions.db"
    with module.VersionManager(database) as manager:
        manager.record(_record("1.0.0"))
        FailingCommit.fail_next = True
        with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
            manager.record(_record("1.0.1"))
        assert len(manager.history("jarvis")) == 1
        manager.record(_record("1.0.1"))
    with _manager(database) as reopened:
        assert len(reopened.history("jarvis")) == 2
