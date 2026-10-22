"""K4-007 Regression Evaluator DRAFT; NOT_FROZEN / NOT_APPROVED.

runtime_enabled=False. Pure offline comparisons; no execution, ledger writes,
release approval, skill activation or rollback. Aggregate counts cannot identify
individual test cases or prove equivalent workloads.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import ClassVar, Literal, cast

from jarvis.test_runner import TestResult
from jarvis.version_manager import SemanticVersion

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False

type RegressionVerdict = Literal["PASS", "FAIL", "INCONCLUSIVE"]


@dataclass(frozen=True, slots=True)
class RegressionReport:
    """Comparison observations only, never a release authorization.

    TestResult has no version metadata. Unknown identities remain None rather
    than being guessed from a version ledger's append order. Supplied version
    labels are caller assertions, not verified source/test provenance.
    """

    component: str
    old_version: SemanticVersion | None
    new_version: SemanticVersion | None
    old_results: TestResult
    new_results: TestResult
    regressions: list[str]
    improvements: list[str]
    verdict: RegressionVerdict


@dataclass(frozen=True, slots=True)
class RegressionDiff:
    """Count observations, not case identities; pass_rate_delta is signed.

    new_errors is only inferred when neither run has runner_error: run_all can
    aggregate several synthetic errors without retaining their count. Messages
    may include uncertainty; inspect complete before interpreting lost passes
    or a timing change as a functional/performance regression.
    """

    new_failures: int
    new_errors: int
    lost_passes: int
    performance_regression: bool
    pass_rate_delta: float
    complete: bool
    regressions: list[str]
    improvements: list[str]


def _text(value: str, label: str) -> None:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError(f"{label} must be nonblank text without NUL")


def _nonnegative(value: float, label: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite non-negative numeric data")


def _validate_result(result: TestResult) -> None:
    """Mirror TestResult's count/rate invariants without executing a test gate."""
    if not isinstance(cast(object, result), TestResult):
        raise TypeError("result must be TestResult")
    _text(result.skill_name, "skill_name")
    counts = (result.passed, result.failed, result.errors, result.skipped, result.total)
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("outcome counts must be non-negative integers")
    if sum(counts[:-1]) != result.total:
        raise ValueError("total disagrees with outcome counts")
    _nonnegative(result.pass_rate, "pass_rate")
    expected = result.passed / result.total if result.total else 0.0
    if result.pass_rate != expected:
        raise ValueError("pass_rate disagrees with outcome counts")
    _nonnegative(result.wall_time_s, "wall_time_s")
    if type(result.stdout) is not str or type(result.stderr) is not str:
        raise ValueError("stdout and stderr must be text")
    if type(result.timed_out) is not bool:
        raise ValueError("timed_out must be bool")
    if result.exit_code is not None and type(result.exit_code) is not int:
        raise ValueError("exit_code must be int or None")
    if result.runner_error is not None and type(result.runner_error) is not str:
        raise ValueError("runner_error must be text or None")


def _complete(result: TestResult) -> bool:
    return (
        result.total > 0
        and not result.timed_out
        and result.runner_error is None
        and result.exit_code == int(result.failed > 0 or result.errors > 0)
    )


def _version(value: SemanticVersion | str | None) -> SemanticVersion | None:
    if value is None:
        return None
    if isinstance(value, str):
        return SemanticVersion.parse(value)
    if not isinstance(cast(object, value), SemanticVersion):
        raise TypeError("version must be SemanticVersion, strict SemVer text or None")
    return SemanticVersion.parse(value.to_str())


class RegressionEvaluator:
    """Explicit developer comparison, not a release gate or statistical benchmark.

    Caller must select comparable suites, environments and version observations.
    PASS means an observed pass-rate increase with no detected regression, not
    proof that unchanged aggregate counts preserved every individual test case.
    """

    draft_status: ClassVar[str] = DRAFT_STATUS
    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    @staticmethod
    def check_performance(
        old_wall_time: float, new_wall_time: float, threshold_pct: float = 20
    ) -> bool:
        """True only above the relative threshold; zero to positive is degraded.

        Compare exact decimal representations to avoid rounding at the boundary
        and overflow for large finite timings. This is not a statistical test.
        """
        _nonnegative(old_wall_time, "old_wall_time")
        _nonnegative(new_wall_time, "new_wall_time")
        _nonnegative(threshold_pct, "threshold_pct")
        old = Fraction(str(old_wall_time))
        new = Fraction(str(new_wall_time))
        threshold = Fraction(str(threshold_pct))
        return (new - old) * 100 > old * threshold

    @staticmethod
    def compare(old: TestResult, new: TestResult) -> RegressionDiff:
        """Find aggregate deltas; never scrape stdout to invent test identities."""
        _validate_result(old)
        _validate_result(new)
        if old.skill_name != new.skill_name:
            raise ValueError("old and new results must have the same skill_name")
        new_failures = max(0, new.failed - old.failed)
        errors_comparable = old.runner_error is None and new.runner_error is None
        added_errors = max(0, new.errors - old.errors) if errors_comparable else 0
        lost_passes = max(0, old.passed - new.passed)
        performance = RegressionEvaluator.check_performance(old.wall_time_s, new.wall_time_s)
        complete = _complete(old) and _complete(new)
        regressions: list[str] = []
        improvements: list[str] = []
        if new_failures:
            regressions.append(f"New failures: {new_failures} (aggregate count increase)")
        if added_errors:
            regressions.append(
                f"New test errors: {added_errors} (excludes runner synthetic errors)"
            )
        if lost_passes:
            regressions.append(f"Lost passed outcomes: {lost_passes} (aggregate count decrease)")
        if performance:
            regressions.append(
                f"Increased wall time: {old.wall_time_s:g}s -> {new.wall_time_s:g}s "
                "(exceeds 20% threshold; workload equivalence is caller-supplied)"
            )
        for label, result in (("old", old), ("new", new)):
            if result.total == 0:
                regressions.append(f"{label} evidence is empty; comparison is inconclusive")
            elif not _complete(result):
                regressions.append(f"{label} evidence is incomplete; comparison is inconclusive")
        delta = new.pass_rate - old.pass_rate
        if complete:
            if delta > 0:
                improvements.append(
                    f"Pass rate increased: {old.pass_rate:.2%} -> {new.pass_rate:.2%}"
                )
            if new.failed < old.failed:
                improvements.append(f"Fewer failures: {old.failed} -> {new.failed}")
            if new.errors < old.errors:
                improvements.append(f"Fewer test errors: {old.errors} -> {new.errors}")
            if new.passed > old.passed:
                improvements.append(f"More passed outcomes: {old.passed} -> {new.passed}")
            if new.wall_time_s < old.wall_time_s:
                improvements.append(
                    f"Reduced wall time: {old.wall_time_s:g}s -> {new.wall_time_s:g}s"
                )
        return RegressionDiff(
            new_failures,
            added_errors,
            lost_passes,
            performance,
            delta,
            complete,
            regressions,
            improvements,
        )

    def evaluate(
        self,
        component: str,
        old_result: TestResult,
        new_result: TestResult,
        *,
        old_version: SemanticVersion | str | None = None,
        new_version: SemanticVersion | str | None = None,
    ) -> RegressionReport:
        """FAIL dominates PASS; incomplete evidence never earns PASS.

        New observed failures/errors always fail. With complete evidence, lost
        passed counts or >20% wall-time increases also fail. Otherwise only a
        pass-rate increase yields PASS; unchanged evidence is INCONCLUSIVE.
        """
        _text(component, "component")
        diff = self.compare(old_result, new_result)
        if component != old_result.skill_name:
            raise ValueError("component must match both results' skill_name")
        old_label, new_label = _version(old_version), _version(new_version)
        verdict: RegressionVerdict = "INCONCLUSIVE"
        if diff.new_failures or diff.new_errors or (diff.complete and diff.regressions):
            verdict = "FAIL"
        elif diff.complete and diff.pass_rate_delta > 0:
            verdict = "PASS"
        return RegressionReport(
            component=component,
            old_version=old_label,
            new_version=new_label,
            old_results=old_result,
            new_results=new_result,
            regressions=list(diff.regressions),
            improvements=list(diff.improvements),
            verdict=verdict,
        )
