"""K4-008 Release Approval DRAFT; NOT_FROZEN / NOT_APPROVED.

runtime_enabled=False. Pure offline quality preflight, NOT a human approval or
execution grant. APPROVED means only that supplied evidence passed these gates.
No ApprovalGate confirmation, ProposalManager transition, registry activation,
release, notification, Git operation or packaging/ integration is performed.
Evidence provenance and authenticated, artifact-bound human review remain pending.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, cast

from jarvis.regression_evaluator import RegressionReport
from jarvis.test_runner import TestResult
from jarvis.version_manager import SemanticVersion

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False
_GATES = frozenset({"test", "regression", "security"})


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    """Caller-supplied evidence, not verified provenance or a sealed artifact.

    security_scan_result uses {"status": "completed", "findings": [...]}; each
    finding requires severity info/low/medium/high/critical, case-insensitive.
    Missing, incomplete or unrecognized scan data fails the security gate.
    Nested dict/list evidence is not deeply immutable; do not mutate during use.
    """

    component: str
    version: str
    test_result: TestResult
    regression_report: RegressionReport | None
    security_scan_result: dict[str, object]
    changelog: str
    git_sha: str

    def __post_init__(self) -> None:
        for name in ("component", "changelog"):
            text = getattr(self, name)
            if type(text) is not str or not text.strip() or "\x00" in text:
                raise ValueError(f"{name} must be nonblank text without NUL")
        SemanticVersion.parse(self.version)
        if (
            type(self.git_sha) is not str
            or len(self.git_sha) not in (40, 64)
            or any(character not in "0123456789abcdefABCDEF" for character in self.git_sha)
        ):
            raise ValueError("git_sha must be a full hexadecimal Git object ID")


class ReleaseDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CONDITIONAL = "CONDITIONAL"


class ReleaseGate:
    """Evaluate all three gates using trusted developer configuration only.

    All gates are hard by default; hard_gates explicitly selects the hard set,
    with its complement soft. Soft failures are CONDITIONAL, never APPROVED.
    Unknown names fail configuration rather than silently weakening policy.
    This configurable DRAFT policy must not replace production safety floors.
    """

    draft_status: ClassVar[str] = DRAFT_STATUS
    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(self, *, hard_gates: Iterable[str] | None = None) -> None:
        hard = _GATES if hard_gates is None else frozenset(hard_gates)
        if hard - _GATES:
            raise ValueError("unknown release gate; expected test, regression or security")
        self._hard_gates = hard

    def evaluate(self, candidate: ReleaseCandidate) -> tuple[ReleaseDecision, list[str]]:
        """Return advisory decision and all failed gates, in deterministic order."""
        checks = (
            ("test", self.check_test_gate(candidate)),
            ("regression", self.check_regression_gate(candidate)),
            ("security", self.check_security_gate(candidate)),
        )
        reasons: list[str] = []
        hard_failure = False
        for name, passed in checks:
            if not passed:
                hard = name in self._hard_gates
                hard_failure |= hard
                reasons.append(
                    f"{name}: {'hard' if hard else 'soft'} gate failed or evidence incomplete"
                )
        if hard_failure:
            return ReleaseDecision.REJECTED, reasons
        if reasons:
            return ReleaseDecision.CONDITIONAL, reasons
        return ReleaseDecision.APPROVED, reasons

    @staticmethod
    def check_test_gate(candidate: ReleaseCandidate) -> bool:
        """Require a complete matching run, nonempty suite, no failures or skips."""
        result = candidate.test_result
        if not isinstance(cast(object, result), TestResult):
            return False
        counts = (result.passed, result.failed, result.errors, result.skipped, result.total)
        return (
            all(type(count) is int and count >= 0 for count in counts)
            and result.skill_name == candidate.component
            and result.total > 0
            and result.passed == result.total
            and result.failed == result.errors == result.skipped == 0
            and type(result.pass_rate) in (int, float)
            and result.pass_rate == 1.0
            and result.timed_out is False
            and result.runner_error is None
            and type(result.exit_code) is int
            and result.exit_code == 0
        )

    @staticmethod
    def check_regression_gate(candidate: ReleaseCandidate) -> bool:
        """Missing/inconclusive evidence is not proof of no new regressions.

        Report identity/version and new test evidence must match the candidate.
        A PASS verdict alone cannot override explicitly listed regressions.
        """
        report = candidate.regression_report
        if report is None or not isinstance(cast(object, report), RegressionReport):
            return False
        return (
            report.component == candidate.component
            and report.new_version == SemanticVersion.parse(candidate.version)
            and report.new_results == candidate.test_result
            and report.verdict == "PASS"
            and type(report.regressions) is list
            and not report.regressions
        )

    @staticmethod
    def check_security_gate(candidate: ReleaseCandidate) -> bool:
        """Completed scan with recognized findings and no high/critical severity."""
        scan = candidate.security_scan_result
        if not isinstance(cast(object, scan), dict) or scan.get("status") != "completed":
            return False
        findings = scan.get("findings")
        if not isinstance(findings, list):
            return False
        for finding in cast(list[object], findings):
            if not isinstance(finding, dict):
                return False
            severity = cast(dict[str, object], finding).get("severity")
            if not isinstance(severity, str) or severity.lower() not in {"info", "low", "medium"}:
                return False
        return True
