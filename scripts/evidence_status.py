"""Engineering evidence declarations only; no authenticity, IO or release approval checks."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Environment(StrEnum):
    DEVELOPMENT_MACOS = "development_macos"
    TARGET_LINUX = "target_linux"
    PREPRODUCTION_LINUX = "preproduction_linux"
    PRODUCTION_EVIDENCE = "production_evidence"


class Status(StrEnum):
    NOT_RUN = "NOT_RUN"
    NOT_VERIFIED = "NOT_VERIFIED"
    FAIL = "FAIL"
    PASS = "PASS"


@dataclass(frozen=True)
class Scope:
    environment: Environment
    target: str
    platform: str
    architecture: str
    revision: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.target, self.platform, self.architecture, self.revision)
        ):
            raise ValueError("Scope requires an environment and nonempty target bindings")


@dataclass(frozen=True)
class Evidence:
    scope: Scope
    check_id: str
    started: bool = False
    valid: bool = False
    independent_verified: bool = False
    satisfies: bool = False
    measured_at: datetime | None = None
    source: str | None = None
    object_id: str | None = None
    evidence_file: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.check_id.strip():
            raise ValueError("Evidence requires a scope and nonempty check ID")
        if not self.started and not self.reason.strip():
            raise ValueError("Unrun evidence requires a reason")
        if any(
            type(value) is not bool
            for value in (self.started, self.valid, self.independent_verified, self.satisfies)
        ):
            raise ValueError("Evidence declarations must be boolean")

    @property
    def status(self) -> Status:
        """Classify declared measurements, without reading or authenticating evidence files."""
        if not self.started:
            return Status.NOT_RUN
        bound = isinstance(self.measured_at, datetime) and all(
            isinstance(value, str) and value.strip()
            for value in (self.source, self.object_id, self.evidence_file)
        )
        if not bound or not self.valid or not self.independent_verified:
            return Status.NOT_VERIFIED
        return Status.PASS if self.satisfies else Status.FAIL


def aggregate(scope: Scope, required: tuple[str, ...], records: tuple[Evidence, ...]) -> Status:
    """Aggregate exactly one scope; missing required checks remain NOT_RUN."""
    if not required or len(set(required)) != len(required) or any(not x.strip() for x in required):
        raise ValueError("Required check IDs must be nonempty and unique")
    by_id: dict[str, Status] = {}
    for record in records:
        if record.scope != scope or record.check_id not in required or record.check_id in by_id:
            raise ValueError("Cross-scope, extra or duplicate evidence")
        by_id[record.check_id] = record.status
    statuses = {by_id.get(check_id, Status.NOT_RUN) for check_id in required}
    return next(
        status
        for status in (Status.FAIL, Status.NOT_VERIFIED, Status.NOT_RUN, Status.PASS)
        if status in statuses
    )


@dataclass(frozen=True)
class Qualification:
    status: Status
    reason: str


def production_qualification(
    scope: Scope, result: Status, prerequisite_scope: Scope, prerequisite_result: Status
) -> Qualification:
    """Return declared status and reason; callers bind results to scopes, not release permission."""
    if scope.environment is not Environment.PRODUCTION_EVIDENCE:
        raise ValueError("Qualification requires a production evidence scope")
    if prerequisite_scope.environment is not Environment.TARGET_LINUX or (
        scope.target,
        scope.platform,
        scope.architecture,
        scope.revision,
    ) != (
        prerequisite_scope.target,
        prerequisite_scope.platform,
        prerequisite_scope.architecture,
        prerequisite_scope.revision,
    ):
        raise ValueError("Qualification requires a matching target Linux prerequisite")
    if result is Status.NOT_RUN:
        return Qualification(result, "Production evidence has not run")
    if result is Status.FAIL:
        return Qualification(result, "Production evidence failed")
    if prerequisite_result is not Status.PASS:
        return Qualification(
            Status.NOT_VERIFIED, f"Target Linux prerequisite is {prerequisite_result.value}"
        )
    if result is Status.NOT_VERIFIED:
        return Qualification(result, "Production evidence is not independently verified")
    return Qualification(result, "Declared checks pass; no release approval granted")
