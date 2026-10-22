"""K4-006 trusted local pytest runner candidate, not an execution sandbox."""

from __future__ import annotations

import importlib
import importlib.util
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest


def module() -> Any:
    assert importlib.util.find_spec("jarvis.test_runner") is not None, "test runner missing"
    return importlib.import_module("jarvis.test_runner")


def test_real_project_pytest_suite_and_full_output() -> None:
    candidate = module()
    path = Path(__file__).with_name("test_skill_manifest.py")
    runner = candidate.AutomatedTestRunner()
    spec = candidate.TestSpec("skill-manifest", path)
    result = runner.run_tests(spec)
    assert candidate.DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert candidate.RUNTIME_ENABLED is False
    assert runner.runtime_enabled is False
    assert result.skill_name == "skill-manifest"
    assert result.passed > 0
    assert result.total == result.passed
    assert result.failed == result.errors == result.skipped == 0
    assert result.pass_rate == 1.0
    assert result.wall_time_s > 0
    assert result.exit_code == 0
    assert "passed" in result.stdout
    assert isinstance(result.stderr, str)
    assert runner.check_gate(result, spec) is True


def suite(tmp_path: Path, text: str, name: str = "test_candidate.py") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_counts_failures_errors_skips_and_full_streams(tmp_path: Path) -> None:
    candidate = module()
    path = suite(
        tmp_path,
        "import pytest, sys\n"
        "@pytest.fixture\ndef broken():\n    raise RuntimeError('setup-error')\n"
        "def test_good():\n    print('完整 stdout')\n"
        "    print('complete stderr', file=sys.stderr)\n"
        "def test_bad():\n    assert False, 'failed assertion'\n"
        "def test_setup(broken):\n    pass\n"
        "@pytest.mark.skip(reason='not executed')\ndef test_skip():\n    pass\n",
    )
    runner = candidate.AutomatedTestRunner()
    spec = candidate.TestSpec("notes", path, required_pass_rate=0.25)
    result = runner.run_tests(spec)
    assert (result.passed, result.failed, result.errors, result.skipped, result.total) == (
        1,
        1,
        1,
        1,
        4,
    )
    assert result.pass_rate == 0.25
    assert "完整 stdout" in result.stdout
    assert "complete stderr" in result.stderr
    assert runner.check_gate(result, spec) is False  # Errors never qualify as quality evidence.


def test_configurable_gate_includes_skips_and_requires_matching_skill(tmp_path: Path) -> None:
    candidate = module()
    path = suite(
        tmp_path,
        "import pytest\n"
        "def test_good():\n    pass\n"
        "def test_bad():\n    assert False\n"
        "@pytest.mark.skip(reason='not executed')\ndef test_skip():\n    pass\n",
    )
    runner = candidate.AutomatedTestRunner()
    spec = candidate.TestSpec("notes", path)
    result = runner.run_tests(spec)
    assert (result.passed, result.failed, result.errors, result.skipped, result.total) == (
        1,
        1,
        0,
        1,
        3,
    )
    assert result.pass_rate == 1 / 3
    assert runner.check_gate(result, spec) is False
    assert runner.check_gate(result, replace(spec, required_pass_rate=1 / 3)) is True
    assert runner.check_gate(result, replace(spec, required_pass_rate=0.34)) is False
    assert (
        runner.check_gate(result, replace(spec, skill_name="other", required_pass_rate=0)) is False
    )


def test_gate_rejects_empty_and_abnormal_process_even_at_zero_threshold(tmp_path: Path) -> None:
    candidate = module()
    runner = candidate.AutomatedTestRunner()
    spec = candidate.TestSpec("notes", suite(tmp_path, ""), required_pass_rate=0)
    empty = runner.run_tests(spec)
    assert (empty.total, empty.pass_rate, empty.exit_code) == (0, 0.0, 5)
    assert runner.check_gate(empty, spec) is False
    normal = candidate.TestResult("notes", 1, 0, 0, 1, 1.0, 0.1, "", "", exit_code=0)
    for bad in (
        replace(normal, timed_out=True),
        replace(normal, exit_code=2),
        replace(normal, exit_code=1),
        replace(normal, runner_error="invalid report"),
        replace(normal, exit_code=None),
    ):
        assert runner.check_gate(bad, spec) is False


def test_timeout_returns_partial_output_and_cannot_pass(tmp_path: Path) -> None:
    candidate = module()
    path = suite(
        tmp_path,
        "import time, sys\n"
        "def test_slow():\n    print('before timeout', flush=True)\n"
        "    print('stderr before timeout', file=sys.stderr, flush=True)\n    time.sleep(30)\n",
    )
    runner = candidate.AutomatedTestRunner()
    # 0.5s 时 pytest 还没 import 完就被杀，stdout 为空（历史偶发失败）；
    # 3s 足够看到输出，仍远小于用例里的 30s sleep。
    spec = candidate.TestSpec("notes", path, timeout_s=3.0, required_pass_rate=0)
    result = runner.run_tests(spec)
    assert result.timed_out is True
    assert result.errors >= 1
    assert result.pass_rate == 0
    assert result.wall_time_s < 5
    assert "before timeout" in result.stdout
    assert "stderr before timeout" in result.stderr
    assert "timeout" in result.runner_error.lower()
    assert runner.check_gate(result, spec) is False


def test_collection_error_does_not_masquerade_as_pass(tmp_path: Path) -> None:
    candidate = module()
    runner = candidate.AutomatedTestRunner()
    spec = candidate.TestSpec("notes", suite(tmp_path, "def broken(:\n"), required_pass_rate=0)
    result = runner.run_tests(spec)
    assert result.errors > 0
    assert result.passed == 0
    assert result.pass_rate == 0
    assert "SyntaxError" in result.stdout
    assert runner.check_gate(result, spec) is False


def test_run_all_executes_only_explicitly_registered_suites(tmp_path: Path) -> None:
    candidate = module()
    first = candidate.TestSpec(
        "notes",
        suite(
            tmp_path,
            "import pytest\n@pytest.mark.parametrize('n', range(3))\ndef test_good(n):\n    pass\n",
            "test_a.py",
        ),
    )
    second = candidate.TestSpec(
        "notes", suite(tmp_path, "def test_bad():\n    assert False\n", "test_b.py")
    )
    untouched = candidate.TestSpec(
        "other", suite(tmp_path, "raise RuntimeError('do not execute')\n", "test_other.py")
    )
    inputs = [first, second, untouched]
    runner = candidate.AutomatedTestRunner(inputs)
    inputs.clear()
    result = runner.run_all("notes")
    assert (result.passed, result.failed, result.errors, result.total, result.pass_rate) == (
        3,
        1,
        0,
        4,
        0.75,
    )
    assert result.wall_time_s > 0
    assert "test_a.py" in result.stdout and "test_b.py" in result.stdout
    with pytest.raises(KeyError):
        runner.run_all("unknown")
    with pytest.raises(ValueError, match="duplicate"):
        candidate.AutomatedTestRunner([first, first])


def test_result_history_is_durable_and_preserves_complete_output(tmp_path: Path) -> None:
    candidate = module()
    runner = candidate.AutomatedTestRunner()
    spec = candidate.TestSpec(
        "notes'quoted", suite(tmp_path, "def test_good():\n    print('完整 output')\n")
    )
    result = runner.run_tests(spec)
    path = tmp_path / "test-history.db"
    connection = sqlite3.connect(path)
    try:
        runner.record_result(result, connection)
        runner.record_result(result, connection)
        assert not connection.in_transaction
    finally:
        connection.close()
    with sqlite3.connect(path) as reopened:
        rows = reopened.execute(
            "SELECT skill_name, passed, failed, errors, total, pass_rate, wall_time_s, "
            "stdout, stderr, skipped, exit_code, timed_out, runner_error "
            "FROM test_results ORDER BY id"
        ).fetchall()
    expected = (
        result.skill_name,
        result.passed,
        result.failed,
        result.errors,
        result.total,
        result.pass_rate,
        result.wall_time_s,
        result.stdout,
        result.stderr,
        result.skipped,
        result.exit_code,
        int(result.timed_out),
        result.runner_error,
    )
    assert rows == [expected, expected]


def test_record_result_does_not_commit_unrelated_caller_transaction(tmp_path: Path) -> None:
    candidate = module()
    runner = candidate.AutomatedTestRunner()
    result = candidate.TestResult("notes", 1, 0, 0, 1, 1.0, 0.1, "stdout", "stderr", exit_code=0)
    connection = sqlite3.connect(tmp_path / "history.db")
    try:
        runner.record_result(result, connection)
        connection.execute("CREATE TABLE caller(value TEXT)")
        connection.execute("INSERT INTO caller VALUES ('keep transaction open')")
        runner.record_result(result, connection)
        assert connection.in_transaction
        connection.rollback()
        assert connection.execute("SELECT COUNT(*) FROM test_results").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM caller").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize("payload", ["<invalid", "<testsuites><testcase/></testsuites>"])
def test_missing_or_inconsistent_junit_cannot_pass(tmp_path: Path, payload: str) -> None:
    candidate = module()
    # Corrupt our own report from real trusted test code, not a fake process result.
    path = suite(
        tmp_path,
        "import atexit\nfrom pathlib import Path\n"
        "def test_corrupt_report(request):\n"
        "    def overwrite():\n"
        f"        Path(request.config.option.xmlpath).write_text({payload!r})\n"
        "    atexit.register(overwrite)\n",
    )
    runner = candidate.AutomatedTestRunner()
    spec = candidate.TestSpec("notes", path)
    result = runner.run_tests(spec)
    assert result.runner_error is not None
    assert result.errors > 0
    assert runner.check_gate(result, spec) is False


@pytest.mark.parametrize(
    "changes",
    [
        {"skill_name": ""},
        {"skill_name": "bad\x00name"},
        {"timeout_s": 0},
        {"timeout_s": -1},
        {"timeout_s": float("inf")},
        {"timeout_s": True},
        {"required_pass_rate": -0.1},
        {"required_pass_rate": 1.1},
        {"required_pass_rate": float("nan")},
        {"required_pass_rate": True},
        {"test_file_path": ""},
        {"test_file_path": "bad\x00path.py"},
    ],
)
def test_invalid_specs_rejected_before_execution(tmp_path: Path, changes: dict[str, Any]) -> None:
    candidate = module()
    spec = candidate.TestSpec("notes", tmp_path / "unused.py")
    spec = replace(spec, **changes)
    with pytest.raises(ValueError):
        candidate.AutomatedTestRunner().run_tests(spec)
    with pytest.raises(ValueError):
        candidate.AutomatedTestRunner([spec])


def test_nonexistent_or_non_file_test_paths_rejected(tmp_path: Path) -> None:
    candidate = module()
    runner = candidate.AutomatedTestRunner()
    with pytest.raises(FileNotFoundError):
        runner.run_tests(candidate.TestSpec("notes", tmp_path / "missing.py"))
    with pytest.raises(ValueError, match="Python test file"):
        runner.run_tests(candidate.TestSpec("notes", tmp_path))
    with pytest.raises(ValueError, match="Python test file"):
        runner.run_tests(candidate.TestSpec("notes", suite(tmp_path, "", "not-tests.txt")))


def test_pytest_environment_options_cannot_override_file_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = module()
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only -k do_not_run_anything")
    monkeypatch.setenv("PYTEST_PLUGINS", "missing_unsafe_plugin")
    path = suite(tmp_path, "def test_good():\n    pass\n", "test space;not-shell.py")
    result = candidate.AutomatedTestRunner().run_tests(candidate.TestSpec("notes", path))
    assert result.passed == result.total == 1
    assert result.errors == 0


def test_record_insert_failure_rolls_back_own_work_not_callers(tmp_path: Path) -> None:
    candidate = module()
    runner = candidate.AutomatedTestRunner()
    result = candidate.TestResult("notes", 1, 0, 0, 1, 1.0, 0.1, "stdout", "stderr", exit_code=0)
    connection = sqlite3.connect(tmp_path / "history.db")
    try:
        runner.record_result(result, connection)
        connection.execute("CREATE TABLE caller(value TEXT)")
        connection.execute(
            "CREATE TRIGGER reject_result BEFORE INSERT ON test_results "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        connection.execute("INSERT INTO caller VALUES ('unrelated pending')")
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            runner.record_result(result, connection)
        assert connection.in_transaction
        assert connection.execute("SELECT COUNT(*) FROM test_results").fetchone() == (1,)
        assert connection.execute("SELECT value FROM caller").fetchone() == ("unrelated pending",)
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("changes", [{"pass_rate": 1.0}, {"total": 3}, {"passed": True}])
def test_inconsistent_results_rejected_by_gate_and_history(
    changes: dict[str, Any],
    tmp_path: Path,
) -> None:
    candidate = module()
    result = candidate.TestResult("notes", 1, 1, 0, 2, 0.5, 0.1, "", "", exit_code=1)
    result = replace(result, **changes)
    spec = candidate.TestSpec("notes", tmp_path / "unused.py")
    with pytest.raises(ValueError):
        candidate.AutomatedTestRunner.check_gate(result, spec)
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(ValueError):
            candidate.AutomatedTestRunner.record_result(result, connection)
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_timeout_stops_descendant_in_same_process_group(tmp_path: Path) -> None:
    import os
    import time

    if os.name != "posix":
        pytest.skip("POSIX process-group cleanup contract")
    candidate = module()
    pid_file = tmp_path / "child.pid"
    path = suite(
        tmp_path,
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        "def test_spawn():\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])\n"
        f"    Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "    time.sleep(30)\n",
    )
    result = candidate.AutomatedTestRunner().run_tests(
        candidate.TestSpec("notes", path, timeout_s=0.75)
    )
    assert result.timed_out
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        os.kill(pid, 9)
        pytest.fail("timed-out pytest left its descendant alive")
