"""K4-007 offline regression comparison draft; no release authorization."""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import replace
from typing import Any

import pytest

from jarvis.test_runner import TestResult
from jarvis.version_manager import SemanticVersion


def candidate() -> Any:
    assert importlib.util.find_spec("jarvis.regression_evaluator") is not None, (
        "regression evaluator draft is missing"
    )
    return importlib.import_module("jarvis.regression_evaluator")


def result(passed: int = 10, failed: int = 0, *, wall_time: float = 10.0) -> TestResult:
    total = passed + failed
    return TestResult(
        skill_name="notes",
        passed=passed,
        failed=failed,
        errors=0,
        total=total,
        pass_rate=passed / total if total else 0.0,
        wall_time_s=wall_time,
        stdout="",
        stderr="",
        exit_code=int(failed > 0),
    )


def test_new_failure_is_detected_and_report_preserves_results() -> None:
    module = candidate()
    old, new = result(), result(9, 1)
    evaluator = module.RegressionEvaluator()
    diff = evaluator.compare(old, new)
    report = evaluator.evaluate("notes", old, new)
    assert diff.new_failures == 1
    assert diff.lost_passes == 1
    assert any("failure" in item for item in report.regressions)
    assert any("passed" in item for item in report.regressions)
    assert report.component == "notes"
    assert report.old_results == old
    assert report.new_results == new
    assert report.verdict == "FAIL"
    assert module.DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert module.RUNTIME_ENABLED is evaluator.runtime_enabled is False


@pytest.mark.parametrize(
    ("old", "new", "threshold", "degraded"),
    [
        (10.0, 12.01, 20, True),
        (10.0, 12.0, 20, False),
        (10.0, 11.99, 20, False),
        (10.0, 9.0, 20, False),
        (10.0, 10.01, 0, True),
        (10.0, 11.0, 5, True),
        (0.0, 0.0, 20, False),
        (0.0, 0.1, 20, True),
        (0.1, 0.12, 20, False),
    ],
)
def test_performance_threshold_is_strict(
    old: float, new: float, threshold: float, degraded: bool
) -> None:
    assert candidate().RegressionEvaluator.check_performance(old, new, threshold) is degraded


def test_performance_regression_is_reported_even_with_unchanged_outcomes() -> None:
    evaluator = candidate().RegressionEvaluator()
    old, new = result(), result(wall_time=12.01)
    diff = evaluator.compare(old, new)
    report = evaluator.evaluate("notes", old, new)
    assert diff.performance_regression is True
    assert any("wall time" in item for item in report.regressions)
    assert report.verdict == "FAIL"


def test_improved_pass_rate_is_pass_and_records_improvements() -> None:
    evaluator = candidate().RegressionEvaluator()
    old, new = result(8, 2), result(10)
    diff = evaluator.compare(old, new)
    report = evaluator.evaluate("notes", old, new)
    assert diff.new_failures == diff.lost_passes == 0
    assert diff.pass_rate_delta == pytest.approx(0.2)
    assert report.verdict == "PASS"
    assert report.regressions == []
    assert any("Pass rate" in item for item in report.improvements)
    assert any("failures" in item for item in report.improvements)


def test_unchanged_results_are_inconclusive_not_a_release_gate() -> None:
    old = result()
    report = candidate().RegressionEvaluator().evaluate("notes", old, old)
    assert report.verdict == "INCONCLUSIVE"
    assert report.regressions == report.improvements == []
    assert report.old_version is report.new_version is None


@pytest.mark.parametrize(
    ("old", "new", "verdict"),
    [
        (result(5, 5), result(10, 6), "FAIL"),  # Better rate cannot hide new failures.
        (result(5, 5), result(10, 0, wall_time=13), "FAIL"),
        (result(10), result(9), "FAIL"),  # Removed passing test, unchanged pass rate.
        (result(10), result(10, 0, wall_time=9), "INCONCLUSIVE"),
        (result(8, 2), result(9, 1), "PASS"),
        (result(9, 1), result(9, 1), "INCONCLUSIVE"),
        (result(9, 1), result(18, 2), "FAIL"),
    ],
)
def test_verdict_precedence(old: TestResult, new: TestResult, verdict: str) -> None:
    assert candidate().RegressionEvaluator().evaluate("notes", old, new).verdict == verdict


def test_explicit_version_labels_use_existing_semver_without_guessing_history() -> None:
    evaluator = candidate().RegressionEvaluator()
    old_version = SemanticVersion.parse("1.0.0+baseline")
    new_version = SemanticVersion.parse("1.0.1-rc.1")
    report = evaluator.evaluate(
        "notes", result(), result(), old_version=old_version, new_version=new_version
    )
    assert report.old_version == old_version
    assert report.new_version == new_version
    assert report.verdict == "INCONCLUSIVE"


@pytest.mark.parametrize("incomplete_side", ["old", "new"])
@pytest.mark.parametrize(
    "changes",
    [
        {"timed_out": True},
        {"runner_error": "report unavailable"},
        {"exit_code": 2},
        {"exit_code": None},
        {"exit_code": 1},  # Inconsistent with zero failed test outcomes.
    ],
)
def test_incomplete_run_never_claims_improvement(
    incomplete_side: str, changes: dict[str, Any]
) -> None:
    old, new = result(8, 2), result(10)
    if incomplete_side == "old":
        # exit_code=1 is normal for old's failing suite; force an inconsistent zero instead.
        adjusted = {"exit_code": 0} if changes == {"exit_code": 1} else changes
        old = replace(old, **adjusted)
    else:
        new = replace(new, **changes)
    report = candidate().RegressionEvaluator().evaluate("notes", old, new)
    assert report.verdict == "INCONCLUSIVE"
    assert any("incomplete" in item for item in report.regressions)


def test_empty_results_are_inconclusive() -> None:
    empty = replace(result(0), exit_code=5)
    report = candidate().RegressionEvaluator().evaluate("notes", empty, result())
    assert report.verdict == "INCONCLUSIVE"
    assert any("empty" in item for item in report.regressions)


def test_new_errors_fail_instead_of_passing_a_better_pass_rate() -> None:
    old = result(1, 1)
    new = replace(result(8, 1), errors=1, total=10, pass_rate=0.8)
    diff = candidate().RegressionEvaluator.compare(old, new)
    report = candidate().RegressionEvaluator().evaluate("notes", old, new)
    assert diff.new_errors == 1
    assert report.verdict == "FAIL"
    assert any("errors" in item for item in report.regressions)


def test_known_new_failure_still_fails_when_other_evidence_is_incomplete() -> None:
    old = replace(result(1, 1), timed_out=True)
    report = candidate().RegressionEvaluator().evaluate("notes", old, result(10, 2))
    assert report.verdict == "FAIL"


def test_synthetic_runner_error_is_not_claimed_as_a_new_test_error() -> None:
    new = replace(result(), errors=1, total=11, pass_rate=10 / 11, runner_error="launch failed")
    report = candidate().RegressionEvaluator().evaluate("notes", result(), new)
    assert report.verdict == "INCONCLUSIVE"


@pytest.mark.parametrize("field", ["old", "new"])
def test_mismatched_component_results_are_rejected(field: str) -> None:
    old, new = result(), result()
    if field == "old":
        old = replace(old, skill_name="other")
    else:
        new = replace(new, skill_name="other")
    with pytest.raises(ValueError, match="component|skill"):
        candidate().RegressionEvaluator().evaluate("notes", old, new)
    with pytest.raises(ValueError, match="skill"):
        candidate().RegressionEvaluator.compare(old, new)


@pytest.mark.parametrize("component", ["", " ", "notes\x00", 1])
def test_invalid_component_is_rejected(component: Any) -> None:
    with pytest.raises(ValueError, match="component"):
        candidate().RegressionEvaluator().evaluate(component, result(), result())


@pytest.mark.parametrize(
    ("old", "new", "threshold"),
    [
        (-1, 1, 20),
        (1, -1, 20),
        (1, 1, -1),
        (float("nan"), 1, 20),
        (1, float("inf"), 20),
        (1, 1, float("nan")),
        (True, 1, 20),
        (1, "12", 20),
        (1, 1, False),
    ],
)
def test_invalid_performance_inputs_raise(old: Any, new: Any, threshold: Any) -> None:
    with pytest.raises(ValueError):
        candidate().RegressionEvaluator.check_performance(old, new, threshold)


@pytest.mark.parametrize(
    "changes",
    [
        {"passed": -1},
        {"total": 9},
        {"pass_rate": 0.5},
        {"passed": True},
        {"wall_time_s": float("nan")},
        {"wall_time_s": -1},
    ],
)
def test_invalid_result_is_rejected(changes: dict[str, Any]) -> None:
    bad = replace(result(), **changes)
    with pytest.raises(ValueError):
        candidate().RegressionEvaluator.compare(result(), bad)


def test_wrong_result_type_is_rejected() -> None:
    with pytest.raises(TypeError):
        candidate().RegressionEvaluator.compare({}, result())


@pytest.mark.parametrize("version", ["1.0", " 1.0.0", "v1.0.0", 1])
def test_version_is_not_silently_repaired_or_inferred(version: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        candidate().RegressionEvaluator().evaluate("notes", result(), result(), old_version=version)


def test_aggregate_counts_do_not_claim_to_identify_replaced_cases() -> None:
    old = replace(result(9, 1), stdout="test_old_pass PASSED\ntest_old_fail FAILED")
    new = replace(result(9, 1), stdout="test_old_pass FAILED\ntest_old_fail PASSED")
    report = candidate().RegressionEvaluator().evaluate("notes", old, new)
    assert report.verdict == "INCONCLUSIVE"
    assert report.regressions == []


def test_compare_and_report_have_detached_message_lists() -> None:
    evaluator = candidate().RegressionEvaluator()
    first = evaluator.evaluate("notes", result(), result(9, 1))
    first.regressions.append("caller annotation")
    second = evaluator.evaluate("notes", result(), result(9, 1))
    assert "caller annotation" not in second.regressions


def test_aggregated_synthetic_runner_errors_remain_inconclusive() -> None:
    old = result()
    new = replace(
        old,
        errors=3,
        total=13,
        pass_rate=10 / 13,
        runner_error="file1 launch failed; file2 report missing; file3 timeout",
    )
    report = candidate().RegressionEvaluator().evaluate("notes", old, new)
    assert report.verdict == "INCONCLUSIVE"


def test_explicit_string_version_labels_are_strict_semver() -> None:
    report = (
        candidate()
        .RegressionEvaluator()
        .evaluate("notes", result(), result(), old_version="1.0.0", new_version="1.0.1")
    )
    assert report.old_version == SemanticVersion.parse("1.0.0")
    assert report.new_version == SemanticVersion.parse("1.0.1")


def test_real_runner_results_can_be_compared_without_io_or_version_ledger(tmp_path: Any) -> None:
    from jarvis.test_runner import AutomatedTestRunner, TestSpec

    old_file = tmp_path / "test_old.py"
    old_file.write_text("def test_a(): pass\ndef test_b(): pass\n", encoding="utf-8")
    new_file = tmp_path / "test_new.py"
    new_file.write_text("def test_a(): pass\ndef test_b(): assert False\n", encoding="utf-8")
    runner = AutomatedTestRunner()
    old = runner.run_tests(TestSpec("notes", old_file))
    new = runner.run_tests(TestSpec("notes", new_file))
    assert old.passed == 2
    assert new.passed == new.failed == 1
    report = (
        candidate()
        .RegressionEvaluator()
        .evaluate("notes", old, new, old_version="1.0.0", new_version="1.0.1")
    )
    assert report.verdict == "FAIL"
    assert report.old_results is old
    assert report.new_results is new
    assert any("failures" in item for item in report.regressions)
