"""K4-006 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

Explicit trusted local pytest invocation only; no production runtime wiring.
This is not a sandbox and does not authorize skill activation or self-modification.
"""

from __future__ import annotations

import math
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, cast

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


@dataclass(frozen=True, slots=True)
class TestSpec:
    """Explicit trusted file; rates are fractions, timeout is per test file."""

    skill_name: str
    test_file_path: str | Path
    timeout_s: float = 60.0
    required_pass_rate: float = 1.0
    __test__: ClassVar[bool] = False


@dataclass(frozen=True, slots=True)
class TestResult:
    """JUnit outcome counts plus the complete captured pytest process streams.

    Skips count in total, never in passed. A runner failure adds one synthetic
    error outcome; an incomplete run is never eligible for the quality gate.
    """

    skill_name: str
    passed: int
    failed: int
    errors: int
    total: int
    pass_rate: float
    wall_time_s: float
    stdout: str
    stderr: str
    skipped: int = 0
    exit_code: int | None = None
    timed_out: bool = False
    runner_error: str | None = None
    __test__: ClassVar[bool] = False


class AutomatedTestRunner:
    """Offline developer tool; only explicitly invoked trusted test code is run.

    SkillManifest has no test-path metadata. The caller must explicitly supply
    TestSpecs for run_all; do not infer paths from skill names or execute a
    registry entry as code. Subprocesses share the developer's OS permissions.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(self, specs: Iterable[TestSpec] = ()) -> None:
        registered: dict[str, list[TestSpec]] = {}
        identities: set[tuple[str, Path]] = set()
        for spec in specs:
            _validate_spec(spec)
            identity = spec.skill_name, Path(spec.test_file_path).resolve()
            if identity in identities:
                raise ValueError("duplicate test file for skill")
            identities.add(identity)
            registered.setdefault(spec.skill_name, []).append(spec)
        self._specs = {name: tuple(entries) for name, entries in registered.items()}

    def run_tests(self, spec: TestSpec) -> TestResult:
        """Run one explicit file with this interpreter's pytest and collect JUnit.

        Shell execution is never used. POSIX children get a separate process
        group, killed/reaped on timeout. All stdout/stderr bytes are retained
        (UTF-8 replacement for invalid sequences), not regex-parsed for counts.
        """
        _validate_spec(spec)
        path = Path(spec.test_file_path).resolve(strict=True)
        if not path.is_file() or path.suffix != ".py":
            raise ValueError("test_file_path must be a Python test file")
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="jarvis-test-runner-") as directory:
            report = Path(directory) / "report.xml"
            env = os.environ.copy()
            env.pop("PYTEST_ADDOPTS", None)
            env.pop("PYTEST_PLUGINS", None)
            env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "-q",
                "--capture=tee-sys",
                "-p",
                "no:cacheprovider",
                f"--junitxml={report}",
                "--",
                str(path),
            ]
            timed_out = False
            runner_error = None
            exit_code = None
            # Files avoid pipe deadlock and preserve even timeout/crash output.
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                try:
                    with subprocess.Popen(
                        command,
                        stdout=out,
                        stderr=err,
                        env=env,
                        start_new_session=os.name == "posix",
                    ) as process:
                        try:
                            exit_code = process.wait(timeout=spec.timeout_s)
                        except subprocess.TimeoutExpired:
                            timed_out = True
                            runner_error = f"pytest timeout after {spec.timeout_s:g}s"
                        finally:
                            _stop_process_group(process)
                            exit_code = process.wait()
                except OSError as exc:
                    runner_error = f"pytest launch error: {exc}"
                out.seek(0)
                err.seek(0)
                stdout = out.read().decode("utf-8", errors="replace")
                stderr = err.read().decode("utf-8", errors="replace")
            passed = failed = errors = skipped = 0
            if runner_error is None:
                try:
                    passed, failed, errors, skipped = _read_report(report)
                except (OSError, ET.ParseError, ValueError, KeyError) as exc:
                    runner_error = f"invalid pytest JUnit report: {exc}"
                if exit_code not in (0, 1, 5):
                    runner_error = runner_error or f"pytest abnormal exit: {exit_code}"
            if runner_error is not None:
                errors += 1
            total = passed + failed + errors + skipped
            return TestResult(
                skill_name=spec.skill_name,
                passed=passed,
                failed=failed,
                errors=errors,
                total=total,
                pass_rate=passed / total if total else 0.0,
                wall_time_s=time.monotonic() - started,
                stdout=stdout,
                stderr=stderr,
                skipped=skipped,
                exit_code=exit_code,
                timed_out=timed_out,
                runner_error=runner_error,
            )

    def run_all(self, skill_name: str) -> TestResult:
        """Aggregate every explicitly registered file, sequentially and weighted.

        Each file keeps its own timeout. Unknown/empty skill suites fail rather
        than returning an artificial 100 percent result. No release is approved.
        """
        _validate_name(skill_name)
        specs = self._specs.get(skill_name)
        if not specs:
            raise KeyError(f"no test specifications for skill: {skill_name}")
        started = time.monotonic()
        results = [self.run_tests(spec) for spec in specs]
        passed = sum(result.passed for result in results)
        failed = sum(result.failed for result in results)
        errors = sum(result.errors for result in results)
        skipped = sum(result.skipped for result in results)
        total = sum(result.total for result in results)
        messages = [result.runner_error for result in results if result.runner_error is not None]
        abnormal = next(
            (result.exit_code for result in results if result.exit_code not in (0, 1)), 0
        )
        return TestResult(
            skill_name=skill_name,
            passed=passed,
            failed=failed,
            errors=errors,
            total=total,
            pass_rate=passed / total if total else 0.0,
            wall_time_s=time.monotonic() - started,
            stdout="".join(
                f"\n=== {spec.test_file_path} ===\n{result.stdout}"
                for spec, result in zip(specs, results, strict=True)
            ),
            stderr="".join(
                f"\n=== {spec.test_file_path} ===\n{result.stderr}"
                for spec, result in zip(specs, results, strict=True)
            ),
            skipped=skipped,
            exit_code=abnormal if abnormal != 0 else int(any(r.exit_code == 1 for r in results)),
            timed_out=any(result.timed_out for result in results),
            runner_error="; ".join(messages) if messages else None,
        )

    @staticmethod
    def check_gate(result: TestResult, spec: TestSpec) -> bool:
        """Default threshold is 100%; incomplete/errored/mismatched runs fail closed."""
        _validate_spec(spec)
        _validate_result(result)
        return (
            result.skill_name == spec.skill_name
            and result.total > 0
            and result.errors == 0
            and not result.timed_out
            and result.runner_error is None
            and result.exit_code == int(result.failed > 0)
            and result.pass_rate >= spec.required_pass_rate
        )

    @staticmethod
    def record_result(result: TestResult, connection: sqlite3.Connection) -> None:
        """Append full output to caller-selected SQLite candidate history.

        A top-level savepoint commits this insert; inside a caller transaction it
        releases only our savepoint (the caller must commit). Never commits or
        rolls back unrelated work. This is not an approved core schema migration.
        """
        _validate_result(result)
        connection.execute("SAVEPOINT jarvis_test_result")
        try:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS test_results (
                    id INTEGER PRIMARY KEY,
                    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    skill_name TEXT NOT NULL,
                    passed INTEGER NOT NULL, failed INTEGER NOT NULL, errors INTEGER NOT NULL,
                    total INTEGER NOT NULL, pass_rate REAL NOT NULL, wall_time_s REAL NOT NULL,
                    stdout TEXT NOT NULL, stderr TEXT NOT NULL, skipped INTEGER NOT NULL,
                    exit_code INTEGER, timed_out INTEGER NOT NULL, runner_error TEXT
                )"""
            )
            connection.execute(
                """INSERT INTO test_results
                (skill_name, passed, failed, errors, total, pass_rate, wall_time_s,
                 stdout, stderr, skipped, exit_code, timed_out, runner_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
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
                ),
            )
            connection.execute("RELEASE SAVEPOINT jarvis_test_result")
        except BaseException:
            connection.execute("ROLLBACK TO SAVEPOINT jarvis_test_result")
            connection.execute("RELEASE SAVEPOINT jarvis_test_result")
            raise


def _validate_name(value: str) -> None:
    if not isinstance(cast(object, value), str) or not value.strip() or "\x00" in value:
        raise ValueError("skill_name must be non-empty text without NUL")


def _finite(value: float, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(cast(object, value), (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{label} must be finite numeric data")


def _validate_spec(spec: TestSpec) -> None:
    if not isinstance(cast(object, spec), TestSpec):
        raise TypeError("spec must be TestSpec")
    _validate_name(spec.skill_name)
    _finite(spec.timeout_s, "timeout_s")
    _finite(spec.required_pass_rate, "required_pass_rate")
    if spec.timeout_s <= 0 or not 0 <= spec.required_pass_rate <= 1:
        raise ValueError("timeout_s must be positive; required_pass_rate must be between 0 and 1")
    if (
        not isinstance(cast(object, spec.test_file_path), (str, Path))
        or not str(spec.test_file_path).strip()
    ):
        raise ValueError("test_file_path must be a non-empty path")
    if "\x00" in str(spec.test_file_path):
        raise ValueError("test_file_path must not contain NUL")


def _validate_result(result: TestResult) -> None:
    if not isinstance(cast(object, result), TestResult):
        raise TypeError("result must be TestResult")
    _validate_name(result.skill_name)
    counts = (result.passed, result.failed, result.errors, result.skipped, result.total)
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("outcome counts must be non-negative integers")
    if result.total != result.passed + result.failed + result.errors + result.skipped:
        raise ValueError("total disagrees with outcome counts")
    _finite(result.pass_rate, "pass_rate")
    expected = result.passed / result.total if result.total else 0.0
    if result.pass_rate != expected:
        raise ValueError("pass_rate disagrees with outcome counts")
    _finite(result.wall_time_s, "wall_time_s")
    if result.wall_time_s < 0:
        raise ValueError("wall_time_s must be non-negative")
    if not isinstance(cast(object, result.stdout), str) or not isinstance(
        cast(object, result.stderr), str
    ):
        raise ValueError("stdout and stderr must be text")
    if type(result.timed_out) is not bool:
        raise ValueError("timed_out must be bool")
    if result.exit_code is not None and type(result.exit_code) is not int:
        raise ValueError("exit_code must be int or None")
    if result.runner_error is not None and not isinstance(cast(object, result.runner_error), str):
        raise ValueError("runner_error must be text or None")


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.kill()


def _read_report(path: Path) -> tuple[int, int, int, int]:
    root = ET.parse(path).getroot()
    if root.tag not in ("testsuites", "testsuite"):
        raise ValueError("unexpected JUnit root")
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        raise ValueError("missing JUnit testsuite")
    passed = failed = errors = skipped = 0
    for suite in suites:
        counts = [0, 0, 0, 0]  # passed, failed, errors, skipped
        for case in suite.findall("testcase"):
            if case.find("error") is not None:
                counts[2] += 1
            elif case.find("failure") is not None:
                counts[1] += 1
            elif case.find("skipped") is not None:
                counts[3] += 1
            else:
                counts[0] += 1
        summary = tuple(
            int(suite.attrib[key]) for key in ("tests", "failures", "errors", "skipped")
        )
        if summary != (sum(counts), counts[1], counts[2], counts[3]):
            raise ValueError("JUnit summary disagrees with testcase outcomes")
        passed += counts[0]
        failed += counts[1]
        errors += counts[2]
        skipped += counts[3]
    return passed, failed, errors, skipped
