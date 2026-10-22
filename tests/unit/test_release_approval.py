"""K4-008 DRAFT advisory preflight; no real reviewer or release is exercised."""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import replace
from typing import Any

import pytest

from jarvis.regression_evaluator import RegressionReport
from jarvis.test_runner import TestResult
from jarvis.version_manager import SemanticVersion


def module() -> Any:
    assert importlib.util.find_spec("jarvis.release_approval") is not None, (
        "release approval missing"
    )
    return importlib.import_module("jarvis.release_approval")


def passed_result() -> TestResult:
    return TestResult("example", 4, 0, 0, 4, 1.0, 0.1, "", "", exit_code=0)


def candidate(**changes: Any) -> Any:
    release = module()
    result = passed_result()
    report = RegressionReport(
        component="example",
        old_version=SemanticVersion.parse("1.0.0"),
        new_version=SemanticVersion.parse("1.1.0"),
        old_results=result,
        new_results=result,
        regressions=[],
        improvements=[],
        verdict="PASS",
    )
    return replace(
        release.ReleaseCandidate(
            component="example",
            version="1.1.0",
            test_result=result,
            regression_report=report,
            security_scan_result={"status": "completed", "findings": []},
            changelog="Local draft release candidate",
            git_sha="a" * 40,
        ),
        **changes,
    )


def test_all_gates_pass_produces_advisory_approved() -> None:
    release = module()
    gate = release.ReleaseGate()
    item = candidate()
    assert gate.check_test_gate(item) is True
    assert gate.check_regression_gate(item) is True
    assert gate.check_security_gate(item) is True
    assert gate.evaluate(item) == (release.ReleaseDecision.APPROVED, [])
    assert release.DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert release.RUNTIME_ENABLED is False
    assert gate.runtime_enabled is False
    assert gate.draft_status == release.DRAFT_STATUS


@pytest.mark.parametrize(
    "changes",
    [
        {"passed": 3, "failed": 1, "pass_rate": 0.75, "exit_code": 1},
        {"passed": 3, "errors": 1, "pass_rate": 0.75, "exit_code": 1},
        {"passed": 3, "skipped": 1, "pass_rate": 0.75},
        {"passed": 0, "total": 0, "pass_rate": 0.0, "exit_code": 5},
        {"timed_out": True},
        {"runner_error": "incomplete report"},
        {"exit_code": None},
        {"exit_code": 2},
        {"pass_rate": float("nan")},
        {"total": 5},
        {"passed": True},
        {"skill_name": "different-component"},
    ],
)
def test_failed_incomplete_or_inconsistent_tests_reject(changes: dict[str, Any]) -> None:
    release = module()
    gate = release.ReleaseGate()
    item = candidate(test_result=replace(passed_result(), **changes))
    assert gate.check_test_gate(item) is False
    decision, reasons = gate.evaluate(item)
    assert decision is release.ReleaseDecision.REJECTED
    assert any("test" in reason for reason in reasons)


def test_new_regression_rejects_even_when_report_claims_pass() -> None:
    release = module()
    item = candidate()
    item = replace(
        item, regression_report=replace(item.regression_report, regressions=["lost case"])
    )
    gate = release.ReleaseGate()
    assert gate.check_regression_gate(item) is False
    decision, reasons = gate.evaluate(item)
    assert decision is release.ReleaseDecision.REJECTED
    assert any("regression" in reason for reason in reasons)


@pytest.mark.parametrize("verdict", ["FAIL", "INCONCLUSIVE"])
def test_nonpassing_regression_verdict_is_not_clean(verdict: str) -> None:
    release = module()
    item = candidate()
    item = replace(item, regression_report=replace(item.regression_report, verdict=verdict))
    assert release.ReleaseGate().check_regression_gate(item) is False
    assert release.ReleaseGate().evaluate(item)[0] is release.ReleaseDecision.REJECTED


def test_missing_regression_report_fails_closed() -> None:
    release = module()
    item = candidate(regression_report=None)
    assert release.ReleaseGate().check_regression_gate(item) is False
    assert release.ReleaseGate().evaluate(item)[0] is release.ReleaseDecision.REJECTED


@pytest.mark.parametrize("field", ["component", "new_version", "new_results"])
def test_regression_report_must_match_candidate(field: str) -> None:
    release = module()
    item = candidate()
    values = {
        "component": "other",
        "new_version": SemanticVersion.parse("9.0.0"),
        "new_results": replace(passed_result(), stdout="different evidence"),
    }
    item = replace(
        item, regression_report=replace(item.regression_report, **{field: values[field]})
    )
    assert release.ReleaseGate().check_regression_gate(item) is False


@pytest.mark.parametrize("severity", ["high", "critical", "HIGH", "Critical"])
def test_security_high_or_critical_rejects(severity: str) -> None:
    release = module()
    item = candidate(
        security_scan_result={"status": "completed", "findings": [{"severity": severity}]}
    )
    gate = release.ReleaseGate()
    assert gate.check_security_gate(item) is False
    decision, reasons = gate.evaluate(item)
    assert decision is release.ReleaseDecision.REJECTED
    assert any("security" in reason for reason in reasons)


def test_low_and_medium_security_findings_are_not_high_risk() -> None:
    release = module()
    item = candidate(
        security_scan_result={
            "status": "completed",
            "findings": [{"severity": "low"}, {"severity": "MEDIUM"}],
        }
    )
    assert release.ReleaseGate().evaluate(item) == (release.ReleaseDecision.APPROVED, [])


MALFORMED_SCANS: list[dict[str, object]] = [
    {},
    {"status": "failed", "findings": []},
    {"status": "running", "findings": []},
    {"status": "completed"},
    {"status": "completed", "findings": None},
    {"status": "completed", "findings": ["unknown"]},
    {"status": "completed", "findings": [{}]},
    {"status": "completed", "findings": [{"severity": "unknown"}]},
    {"status": "completed", "findings": [{"severity": None}]},
]


@pytest.mark.parametrize("scan", MALFORMED_SCANS)
def test_missing_incomplete_or_malformed_security_scan_rejects(scan: dict[str, object]) -> None:
    release = module()
    item = candidate(security_scan_result=scan)
    assert release.ReleaseGate().check_security_gate(item) is False
    assert release.ReleaseGate().evaluate(item)[0] is release.ReleaseDecision.REJECTED


@pytest.mark.parametrize("name", ["test", "regression", "security"])
def test_configured_soft_failure_is_conditional(name: str) -> None:
    release = module()
    item = candidate()
    if name == "test":
        item = replace(item, test_result=replace(passed_result(), timed_out=True))
        item = replace(
            item, regression_report=replace(item.regression_report, new_results=item.test_result)
        )
    elif name == "regression":
        item = replace(item, regression_report=None)
    else:
        item = replace(item, security_scan_result={})
    gate = release.ReleaseGate(hard_gates={"test", "regression", "security"} - {name})
    decision, reasons = gate.evaluate(item)
    assert decision is release.ReleaseDecision.CONDITIONAL
    assert len(reasons) == 1
    assert name in reasons[0]
    assert "soft" in reasons[0]


def test_hard_failure_dominates_soft_and_all_reasons_are_collected() -> None:
    release = module()
    item = candidate(regression_report=None, security_scan_result={})
    gate = release.ReleaseGate(hard_gates={"regression"})
    decision, reasons = gate.evaluate(item)
    assert decision is release.ReleaseDecision.REJECTED
    assert len(reasons) == 2
    assert "regression" in reasons[0] and "hard" in reasons[0]
    assert "security" in reasons[1] and "soft" in reasons[1]


def test_policy_is_detached_and_unknown_gate_names_rejected() -> None:
    release = module()
    configured = {"test", "regression", "security"}
    gate = release.ReleaseGate(hard_gates=configured)
    configured.clear()
    assert gate.evaluate(candidate(security_scan_result={}))[0] is release.ReleaseDecision.REJECTED
    with pytest.raises(ValueError, match="gate"):
        release.ReleaseGate(hard_gates={"securtiy"})


def test_all_soft_still_runs_every_gate() -> None:
    release = module()
    gate = release.ReleaseGate(hard_gates=set())
    assert gate.evaluate(candidate())[0] is release.ReleaseDecision.APPROVED
    decision, reasons = gate.evaluate(candidate(regression_report=None, security_scan_result={}))
    assert decision is release.ReleaseDecision.CONDITIONAL
    assert len(reasons) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("component", ""),
        ("component", "nul\x00component"),
        ("version", "1.0"),
        ("version", "01.0.0"),
        ("changelog", " "),
        ("git_sha", "a" * 7),
        ("git_sha", "g" * 40),
        ("git_sha", " " + "a" * 40),
    ],
)
def test_invalid_candidate_metadata_is_not_repaired(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        candidate(**{field: value})


def test_full_sha256_git_identifier_is_preserved() -> None:
    item = candidate(git_sha="ABCDEF01" * 8)
    assert item.git_sha == "ABCDEF01" * 8


def test_unknown_regression_version_is_not_guessed_from_candidate() -> None:
    release = module()
    item = candidate()
    item = replace(item, regression_report=replace(item.regression_report, new_version=None))
    assert release.ReleaseGate().check_regression_gate(item) is False
