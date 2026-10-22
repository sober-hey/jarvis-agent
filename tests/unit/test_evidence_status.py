"""Pure declaration tests; synthetic Linux scopes do not prove Linux/ECS measurements."""

import importlib.util
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from scripts.evidence_status import (
        Environment,
        Evidence,
        Scope,
        Status,
        aggregate,
        production_qualification,
    )
else:
    spec = importlib.util.spec_from_file_location(
        "evidence_status", Path(__file__).resolve().parents[2] / "scripts" / "evidence_status.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    Environment = module.Environment
    Evidence = module.Evidence
    Scope = module.Scope
    Status = module.Status
    aggregate = module.aggregate
    production_qualification = module.production_qualification

DEV = Scope(Environment.DEVELOPMENT_MACOS, "candidate", "Darwin", "arm64", "rev-1")
LINUX = Scope(Environment.TARGET_LINUX, "ecs-candidate", "Linux", "x86_64", "rev-1")
PRODUCTION = replace(LINUX, environment=Environment.PRODUCTION_EVIDENCE)
MEASURED = Evidence(
    DEV,
    "check-a",
    started=True,
    valid=True,
    independent_verified=True,
    satisfies=True,
    measured_at=datetime(2026, 10, 13, tzinfo=UTC),
    source="isolated-command",
    object_id="candidate-instance",
    evidence_file="declared-evidence.json",
    reason="caller assertion only",
)


def declared(status: Status, check_id: str = "check-a", scope: Scope = DEV) -> Evidence:
    return replace(
        MEASURED,
        scope=scope,
        check_id=check_id,
        started=status is not Status.NOT_RUN,
        valid=status is not Status.NOT_VERIFIED,
        satisfies=status is not Status.FAIL,
    )


@pytest.mark.parametrize("status", tuple(Status))
def test_declared_status(status: Status) -> None:
    assert declared(status).status is status


def test_not_run_needs_no_measurement_bindings() -> None:
    evidence = Evidence(LINUX, "check-a", reason="No ECS instance or credentials")
    assert evidence.status is Status.NOT_RUN
    assert (
        evidence.measured_at
        is evidence.source
        is evidence.object_id
        is evidence.evidence_file
        is None
    )
    assert aggregate(LINUX, ("check-a",), (evidence,)) is Status.NOT_RUN


@pytest.mark.parametrize(
    "evidence",
    (
        replace(MEASURED, measured_at=None),
        replace(MEASURED, source=None),
        replace(MEASURED, source=""),
        replace(MEASURED, source=" \t"),
        replace(MEASURED, object_id=None),
        replace(MEASURED, object_id=""),
        replace(MEASURED, object_id=" \t"),
        replace(MEASURED, evidence_file=None),
        replace(MEASURED, evidence_file=""),
        replace(MEASURED, evidence_file=" \t"),
        replace(MEASURED, valid=False),
        replace(MEASURED, independent_verified=False),
    ),
)
def test_missing_binding_or_verification_blocks_pass(evidence: Evidence) -> None:
    assert evidence.status is Status.NOT_VERIFIED
    assert replace(evidence, satisfies=False).status is Status.NOT_VERIFIED
    assert replace(evidence, started=False).status is Status.NOT_RUN


@pytest.mark.parametrize("left,right", tuple(product(Status, repeat=2)))
def test_aggregation_matrix(left: Status, right: Status) -> None:
    precedence = {Status.PASS: 0, Status.NOT_RUN: 1, Status.NOT_VERIFIED: 2, Status.FAIL: 3}
    expected = max((left, right), key=precedence.__getitem__)
    records = (declared(left), declared(right, "check-b"))
    assert aggregate(DEV, ("check-a", "check-b"), records) is expected
    assert aggregate(DEV, ("check-a", "check-b"), records[::-1]) is expected


@pytest.mark.parametrize("present", tuple(Status))
def test_missing_required_check(present: Status) -> None:
    expected = present if present in (Status.FAIL, Status.NOT_VERIFIED) else Status.NOT_RUN
    assert aggregate(DEV, ("check-a", "missing"), (declared(present),)) is expected
    assert aggregate(DEV, ("missing",), ()) is Status.NOT_RUN


@pytest.mark.parametrize("required", ((), ("check-a", "check-a"), ("",), (" \t",)))
def test_reject_invalid_required(required: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        aggregate(DEV, required, ())


@pytest.mark.parametrize(
    "records",
    (
        (MEASURED, MEASURED),
        (MEASURED, replace(MEASURED, check_id="extra")),
        (replace(MEASURED, scope=LINUX),),
    ),
)
def test_reject_duplicate_extra_or_cross_scope(records: tuple[Evidence, ...]) -> None:
    with pytest.raises(ValueError):
        aggregate(DEV, ("check-a",), records)


@pytest.mark.parametrize(
    "other_scope",
    (
        replace(DEV, environment=Environment.PREPRODUCTION_LINUX),
        replace(DEV, environment=Environment.PRODUCTION_EVIDENCE),
        replace(DEV, target="other-target"),
        replace(DEV, platform="Linux"),
        replace(DEV, architecture="x86_64"),
        replace(DEV, revision="rev-2"),
    ),
)
def test_reject_every_scope_binding_mismatch(other_scope: Scope) -> None:
    with pytest.raises(ValueError):
        aggregate(other_scope, ("check-a",), (MEASURED,))


@pytest.mark.parametrize("field", ("target", "platform", "architecture", "revision"))
@pytest.mark.parametrize("value", ("", " \t"))
def test_empty_scope_bindings_rejected(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        replace(DEV, **{field: value})


def test_empty_check_id() -> None:
    with pytest.raises(ValueError):
        replace(MEASURED, check_id=" \t")


@pytest.mark.parametrize("model,field", ((DEV, "revision"), (MEASURED, "reason")))
def test_frozen_models(model: Scope | Evidence, field: str) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(model, field, "modified")


@pytest.mark.parametrize("result,prerequisite", tuple(product(Status, repeat=2)))
def test_production_qualification_matrix(result: Status, prerequisite: Status) -> None:
    expected = (
        result
        if result in (Status.NOT_RUN, Status.FAIL) or prerequisite is Status.PASS
        else Status.NOT_VERIFIED
    )
    assert production_qualification(PRODUCTION, result, LINUX, prerequisite).status is expected


@pytest.mark.parametrize("environment", tuple(Environment))
def test_qualification_only_for_production(environment: Environment) -> None:
    scope = replace(PRODUCTION, environment=environment)
    if environment is Environment.PRODUCTION_EVIDENCE:
        assert (
            production_qualification(scope, Status.PASS, LINUX, Status.PASS).status is Status.PASS
        )
    else:
        with pytest.raises(ValueError):
            production_qualification(scope, Status.PASS, LINUX, Status.PASS)


@pytest.mark.parametrize(
    "prerequisite",
    (
        replace(LINUX, environment=Environment.DEVELOPMENT_MACOS),
        replace(LINUX, environment=Environment.PREPRODUCTION_LINUX),
        replace(LINUX, environment=Environment.PRODUCTION_EVIDENCE),
        replace(LINUX, target="other-target"),
        replace(LINUX, platform="Darwin"),
        replace(LINUX, architecture="arm64"),
        replace(LINUX, revision="rev-2"),
    ),
)
def test_reject_invalid_production_prerequisite(prerequisite: Scope) -> None:
    with pytest.raises(ValueError):
        production_qualification(PRODUCTION, Status.PASS, prerequisite, Status.PASS)


def test_target_pass_cannot_supply_unrun_production_evidence() -> None:
    target = aggregate(LINUX, ("check-a",), (declared(Status.PASS, scope=LINUX),))
    production = aggregate(PRODUCTION, ("check-a",), ())
    assert production_qualification(PRODUCTION, production, LINUX, target).status is Status.NOT_RUN


@pytest.mark.parametrize("reason", ("", " ", "\t"))
def test_unrun_evidence_requires_reason(reason: str) -> None:
    with pytest.raises(ValueError, match="Unrun evidence requires a reason"):
        Evidence(LINUX, "check-a", reason=reason)


@pytest.mark.parametrize("result,prerequisite", tuple(product(Status, repeat=2)))
def test_qualification_has_traceable_reason(result: Status, prerequisite: Status) -> None:
    decision = production_qualification(PRODUCTION, result, LINUX, prerequisite)
    assert decision.reason.strip()
    if result is Status.NOT_RUN:
        assert "has not run" in decision.reason
    elif result is Status.FAIL:
        assert "failed" in decision.reason
    elif prerequisite is not Status.PASS:
        assert prerequisite.value in decision.reason
        assert "Target Linux prerequisite" in decision.reason
    elif result is Status.NOT_VERIFIED:
        assert "not independently verified" in decision.reason
    else:
        assert "no release approval" in decision.reason
    field = "reason"
    with pytest.raises(FrozenInstanceError):
        setattr(decision, field, "changed")
