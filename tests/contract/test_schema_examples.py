"""Structural schema examples for five of the six draft K0 Contract Pack schemas.

Scope: for each of the five schemas below this module supplies exactly one
inline valid instance and several well-founded invalid instances, then runs the
real ``jsonschema`` Draft 2020-12 validator and asserts the exact violations.

``task_spec.schema.json`` is deliberately out of scope here: its positive and
negative examples already live as files in ``contracts/examples/`` and are
exercised by ``tests/contract/test_contract_baseline.py``.  This module asserts
that the five names below are exactly the on-disk schema set minus TaskSpec, so
a newly added schema cannot slip through uncovered.

Explicit non-claims (a green run here means none of these):

1. Only *structural* exemplification is covered.  A valid instance proves the
   instance satisfies the keywords written in the schema file; it does not prove
   the object is safe, meaningful, or acceptable to the future kernel.
2. Constraints the schema does not express are **not** rejected.  The seven gaps
   below are recorded here in prose only: no test in this module asserts that any
   unsafe instance stays acceptable, and no runtime diagnostic or warning is
   emitted, so a future draft that adds one of these constraints stays green
   (tightening the draft is progress, not a regression) and no ``-W error`` run
   is disturbed.
   (a) ``prepared_relpath``/``target_relpath`` carry only ``minLength`` and no
   path keyword, so traversal, absolute paths, UTF-8 rules and depth limits are
   unenforced (OPEN O-2, FS-023);
   (b) nothing ties ``expected_sha256``/``expected_size`` to
   ``input_snapshot_sha256`` or to the prepared artifact;
   (c) ``operation_intent`` accepts any enum ``state`` together with any other
   field values, so state-to-intent guards and the Attempt/Intent reuse rule are
   not schema-encoded;
   (d) the Reconciliation observation/decision pairing table does not exist in
   ``reconciliation.schema.json`` and retry preconditions are not expressed
   (OPEN O-5, FS-053);
   (e) ``snapshot_manifest`` has neither ``minItems`` nor a cross-field check, so
   an empty ``files`` array and ``file_count``/``total_bytes`` disagreeing with
   ``len(files)`` are structurally valid (manifest/inventory mapping FS-016 OPEN);
   (f) ``tool_manifest`` encodes no relation between ``side_effect_class`` and
   ``requires_authorization``/``supports_reconciliation``: a side-effecting class
   declaring ``requires_authorization=false`` and ``supports_reconciliation=false``
   is structurally valid, so neither an authorization obligation nor a
   reconciliation duty follows from schema validity.  Schema validity is not an
   authorization decision.
3. Contract Pack status is unchanged: DRAFT / NOT_FROZEN / NOT_VERIFIED.  These
   tests are evidence of schema behaviour only, not of a frozen contract, not of
   an implemented kernel, and not a security review.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple, Protocol, cast

import pytest
from jsonschema import validators

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_UPPERCASE = "A" * 64
SHA_SHORT = "c" * 63

# Exactly the draft pack minus task_spec.schema.json (see module docstring).
SCHEMA_NAMES: tuple[str, ...] = (
    "file_write_intent.schema.json",
    "operation_intent.schema.json",
    "reconciliation.schema.json",
    "snapshot_manifest.schema.json",
    "tool_manifest.schema.json",
)

TASK_SPEC_SCHEMA = "task_spec.schema.json"

Violation = tuple[tuple[str | int, ...], str | None]


class ValidationErrorLike(Protocol):
    """Minimal view of a jsonschema validation error (keeps Pyright strict)."""

    @property
    def validator(self) -> str | None: ...

    @property
    def message(self) -> str: ...

    @property
    def path(self) -> Iterator[str | int]: ...


class ValidatorLike(Protocol):
    """Minimal view of a jsonschema validator instance (keeps Pyright strict)."""

    def __init__(self, schema: object) -> None: ...

    def iter_errors(self, instance: object) -> Iterator[ValidationErrorLike]: ...


class InvalidCase(NamedTuple):
    """One invalid instance plus the violations it must produce, exactly."""

    schema: str
    case: str
    instance: dict[str, object]
    violations: frozenset[Violation]
    mentions: tuple[str, ...] = ()


def _load_schema(name: str) -> dict[str, object]:
    path = CONTRACTS_DIR / name
    if not path.is_file():
        raise AssertionError(f"missing contract schema: {path}")
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AssertionError(f"{name}: expected a JSON object schema")
    return cast("dict[str, object]", raw)


def _validator(name: str) -> ValidatorLike:
    factory = cast("type[ValidatorLike]", validators.validator_for(_load_schema(name)))
    return factory(_load_schema(name))


def _errors(name: str, instance: object) -> list[tuple[tuple[str | int, ...], str | None, str]]:
    found: list[tuple[tuple[str | int, ...], str | None, str]] = []
    for error in _validator(name).iter_errors(instance):
        found.append((tuple(error.path), error.validator, error.message))
    return found


def _without(mapping: dict[str, object], key: str) -> dict[str, object]:
    copy = dict(mapping)
    del copy[key]
    return copy


def _entry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"path": "notes/a.txt", "size_bytes": 12, "sha256": SHA_A}
    return base | overrides


def _snapshot(files: list[dict[str, object]], **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": 1,
        "snapshot_id": "snap-0001",
        "files": files,
        "file_count": len(files),
        "total_bytes": 12,
    }
    return base | overrides


FILE_WRITE_INTENT: dict[str, object] = {
    "schema_version": 1,
    "operation_id": "op-0001",
    "input_snapshot_sha256": SHA_A,
    "prepared_relpath": "state/tasks/task-0001/prepared/inventory.json",
    "target_relpath": "state/tasks/task-0001/output/inventory.json",
    "expected_sha256": SHA_B,
    "expected_size": 2048,
}

OPERATION_INTENT: dict[str, object] = {
    "schema_version": 1,
    "operation_id": "op-0001",
    "step_id": "step-0001",
    "operation_key": "task-0001:step-0001:file.publish.no_clobber",
    "operation_type": "file.publish.no_clobber",
    "tool_name": "filesystem.publisher",
    "tool_version": "0.1.0",
    "arguments_digest": SHA_C,
    "state": "PREPARED",
}

RECONCILIATION: dict[str, object] = {
    "schema_version": 1,
    "operation_id": "op-0001",
    "observation": "EXPECTED_EFFECT_PRESENT",
    "decision": "VERIFY",
    "reason_code": "TARGET_CONTENT_MATCHES_EXPECTED_DIGEST",
    "evidence": {"target_sha256": SHA_B, "observed_size": 2048},
}

SNAPSHOT_MANIFEST: dict[str, object] = _snapshot(
    [_entry(), _entry(path="notes/b.txt", size_bytes=0)]
)

TOOL_MANIFEST: dict[str, object] = {
    "schema_version": 1,
    "tool_name": "filesystem.publisher",
    "tool_version": "0.1.0",
    "operation_type": "file.publish.no_clobber",
    "side_effect_class": "FILE_CREATE_NO_CLOBBER",
    "requires_authorization": True,
    "supports_reconciliation": True,
}

VALID_EXAMPLES: dict[str, dict[str, object]] = {
    "file_write_intent.schema.json": FILE_WRITE_INTENT,
    "operation_intent.schema.json": OPERATION_INTENT,
    "reconciliation.schema.json": RECONCILIATION,
    "snapshot_manifest.schema.json": SNAPSHOT_MANIFEST,
    "tool_manifest.schema.json": TOOL_MANIFEST,
}

# Note on paths: "required" and "additionalProperties" are reported at the
# *object* path (empty tuple for the root, ("files", 0) for a nested item) while
# "pattern"/"minimum"/"const"/"type"/"maxItems" are reported at the *property*
# path.  That is the observed jsonschema behaviour, asserted here exactly.
INVALID_CASES: tuple[InvalidCase, ...] = (
    # --- file_write_intent.schema.json -----------------------------------
    InvalidCase(
        "file_write_intent.schema.json",
        "missing_required_target_relpath",
        _without(FILE_WRITE_INTENT, "target_relpath"),
        frozenset({((), "required")}),
        ("target_relpath",),
    ),
    InvalidCase(
        "file_write_intent.schema.json",
        "unknown_field_attempt_id",
        FILE_WRITE_INTENT | {"attempt_id": "attempt-0001"},
        frozenset({((), "additionalProperties")}),
        ("attempt_id",),
    ),
    InvalidCase(
        "file_write_intent.schema.json",
        "negative_expected_size",
        FILE_WRITE_INTENT | {"expected_size": -1},
        frozenset({(("expected_size",), "minimum")}),
    ),
    InvalidCase(
        "file_write_intent.schema.json",
        "boolean_expected_size_is_not_integer",
        FILE_WRITE_INTENT | {"expected_size": True},
        frozenset({(("expected_size",), "type")}),
        ("integer",),
    ),
    InvalidCase(
        "file_write_intent.schema.json",
        "uppercase_expected_sha256",
        FILE_WRITE_INTENT | {"expected_sha256": SHA_UPPERCASE},
        frozenset({(("expected_sha256",), "pattern")}),
    ),
    InvalidCase(
        "file_write_intent.schema.json",
        "wrong_schema_version",
        FILE_WRITE_INTENT | {"schema_version": 2},
        frozenset({(("schema_version",), "const")}),
    ),
    # --- operation_intent.schema.json -------------------------------------
    InvalidCase(
        "operation_intent.schema.json",
        "missing_required_arguments_digest",
        _without(OPERATION_INTENT, "arguments_digest"),
        frozenset({((), "required")}),
        ("arguments_digest",),
    ),
    InvalidCase(
        "operation_intent.schema.json",
        "unknown_field_attempt_id",
        OPERATION_INTENT | {"attempt_id": "attempt-0001"},
        frozenset({((), "additionalProperties")}),
        ("attempt_id",),
    ),
    InvalidCase(
        "operation_intent.schema.json",
        "unknown_state_published",
        OPERATION_INTENT | {"state": "PUBLISHED"},
        frozenset({(("state",), "enum")}),
    ),
    InvalidCase(
        "operation_intent.schema.json",
        "wrong_operation_type",
        OPERATION_INTENT | {"operation_type": "shell.execute"},
        frozenset({(("operation_type",), "const")}),
    ),
    InvalidCase(
        "operation_intent.schema.json",
        "wrong_hash_arguments_digest",
        OPERATION_INTENT | {"arguments_digest": SHA_SHORT},
        frozenset({(("arguments_digest",), "pattern")}),
    ),
    InvalidCase(
        "operation_intent.schema.json",
        "wrong_tool_name",
        OPERATION_INTENT | {"tool_name": "filesystem.shell"},
        frozenset({(("tool_name",), "const")}),
    ),
    # --- reconciliation.schema.json ---------------------------------------
    InvalidCase(
        "reconciliation.schema.json",
        "missing_required_reason_code",
        _without(RECONCILIATION, "reason_code"),
        frozenset({((), "required")}),
        ("reason_code",),
    ),
    InvalidCase(
        "reconciliation.schema.json",
        "unknown_field_confidence",
        RECONCILIATION | {"confidence": 0.9},
        frozenset({((), "additionalProperties")}),
        ("confidence",),
    ),
    InvalidCase(
        "reconciliation.schema.json",
        "unknown_observation",
        RECONCILIATION | {"observation": "TARGET_PRESENT"},
        frozenset({(("observation",), "enum")}),
    ),
    # "RETRY" is a strict prefix of the legal value "RETRY_SAFE": this case
    # exists to prove the assertion compares validator names exactly instead of
    # substring-matching messages.
    InvalidCase(
        "reconciliation.schema.json",
        "unknown_decision_retry_prefix_of_retry_safe",
        RECONCILIATION | {"decision": "RETRY"},
        frozenset({(("decision",), "enum")}),
    ),
    InvalidCase(
        "reconciliation.schema.json",
        "evidence_not_an_object",
        RECONCILIATION | {"evidence": ["target_sha256", SHA_B]},
        frozenset({(("evidence",), "type")}),
    ),
    # --- snapshot_manifest.schema.json ------------------------------------
    InvalidCase(
        "snapshot_manifest.schema.json",
        "first_file_missing_required_sha256",
        _snapshot([_without(_entry(), "sha256")]),
        frozenset({(("files", 0), "required")}),
        ("sha256",),
    ),
    InvalidCase(
        "snapshot_manifest.schema.json",
        "first_file_unknown_field_mode",
        _snapshot([_entry(mode=0o644)]),
        frozenset({(("files", 0), "additionalProperties")}),
        ("mode",),
    ),
    InvalidCase(
        "snapshot_manifest.schema.json",
        "first_file_negative_size_bytes",
        _snapshot([_entry(size_bytes=-1)]),
        frozenset({(("files", 0, "size_bytes"), "minimum")}),
    ),
    InvalidCase(
        "snapshot_manifest.schema.json",
        "first_file_bad_sha256",
        _snapshot([_entry(sha256="z" * 64)]),
        frozenset({(("files", 0, "sha256"), "pattern")}),
    ),
    InvalidCase(
        "snapshot_manifest.schema.json",
        "file_count_above_maximum",
        _snapshot([_entry()], file_count=1001),
        frozenset({(("file_count",), "maximum")}),
    ),
    InvalidCase(
        "snapshot_manifest.schema.json",
        "total_bytes_above_maximum",
        _snapshot([_entry()], total_bytes=20_971_521),
        frozenset({(("total_bytes",), "maximum")}),
    ),
    InvalidCase(
        "snapshot_manifest.schema.json",
        "files_above_max_items",
        _snapshot([_entry()] * 1001, file_count=1000),
        frozenset({(("files",), "maxItems")}),
    ),
    InvalidCase(
        "snapshot_manifest.schema.json",
        "files_not_an_array",
        _snapshot([_entry()]) | {"files": {"path": "notes/a.txt"}},
        frozenset({(("files",), "type")}),
    ),
    # --- tool_manifest.schema.json ----------------------------------------
    InvalidCase(
        "tool_manifest.schema.json",
        "missing_required_tool_version",
        _without(TOOL_MANIFEST, "tool_version"),
        frozenset({((), "required")}),
        ("tool_version",),
    ),
    InvalidCase(
        "tool_manifest.schema.json",
        "unknown_field_timeout_seconds",
        TOOL_MANIFEST | {"timeout_seconds": 30},
        frozenset({((), "additionalProperties")}),
        ("timeout_seconds",),
    ),
    InvalidCase(
        "tool_manifest.schema.json",
        "unknown_side_effect_class",
        TOOL_MANIFEST | {"side_effect_class": "FILE_OVERWRITE"},
        frozenset({(("side_effect_class",), "enum")}),
    ),
    InvalidCase(
        "tool_manifest.schema.json",
        "requires_authorization_as_string",
        TOOL_MANIFEST | {"requires_authorization": "true"},
        frozenset({(("requires_authorization",), "type")}),
    ),
    InvalidCase(
        "tool_manifest.schema.json",
        "supports_reconciliation_as_integer",
        TOOL_MANIFEST | {"supports_reconciliation": 0},
        frozenset({(("supports_reconciliation",), "type")}),
    ),
)

INVALID_PARAMS = [pytest.param(case, id=f"{case.schema}::{case.case}") for case in INVALID_CASES]


def test_five_schemas_are_exactly_the_pack_minus_task_spec() -> None:
    on_disk = {path.name for path in CONTRACTS_DIR.glob("*.schema.json")}
    assert TASK_SPEC_SCHEMA not in SCHEMA_NAMES, "TaskSpec examples live in the baseline test"
    assert (CONTRACTS_DIR / TASK_SPEC_SCHEMA).is_file(), "TaskSpec schema disappeared"
    assert on_disk == set(SCHEMA_NAMES) | {TASK_SPEC_SCHEMA}, (
        f"schema set drift: uncovered={sorted(on_disk - set(SCHEMA_NAMES))}, "
        f"missing={sorted(set(SCHEMA_NAMES) - on_disk)}"
    )


def test_every_schema_has_example_coverage() -> None:
    assert set(VALID_EXAMPLES) == set(SCHEMA_NAMES)
    counts = {
        name: sum(1 for case in INVALID_CASES if case.schema == name) for name in SCHEMA_NAMES
    }
    thin = {name: count for name, count in counts.items() if count < 4}
    assert not thin, f"schemas with fewer than four invalid cases: {thin}"
    assert {case.schema for case in INVALID_CASES} == set(SCHEMA_NAMES)
    case_ids = [f"{case.schema}::{case.case}" for case in INVALID_CASES]
    duplicates = sorted({case_id for case_id in case_ids if case_ids.count(case_id) > 1})
    assert not duplicates, f"duplicate case ids would collide as pytest ids: {duplicates}"


def test_no_invalid_case_declares_a_vacuous_expectation() -> None:
    """Table-wide guard for the vacuous-pass defect found during self-review."""
    vacuous = [f"{case.schema}::{case.case}" for case in INVALID_CASES if not case.violations]
    assert not vacuous, f"invalid cases with empty expectations would pass vacuously: {vacuous}"


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_valid_example_is_accepted(name: str) -> None:
    errors = _errors(name, VALID_EXAMPLES[name])
    assert errors == [], f"{name}: valid example rejected: {errors}"


@pytest.mark.parametrize("case", INVALID_PARAMS)
def test_invalid_example_is_rejected_for_the_stated_reason(case: InvalidCase) -> None:
    # Guard the root cause of an earlier defect: with an empty expectation a legal
    # instance passes vacuously and the case would provide no evidence at all.
    assert case.violations, (
        f"{case.schema}::{case.case}: an invalid case must declare a non-empty expected "
        "violation set; an empty set passes vacuously on a legal instance"
    )
    errors = _errors(case.schema, case.instance)
    actual = {(path, validator) for path, validator, _ in errors}
    assert actual == set(case.violations), (
        f"{case.schema}::{case.case}: expected exactly {sorted(case.violations)}, "
        f"got {sorted(actual)} (messages: {[message for *_, message in errors]})"
    )
    joined = " ".join(message for *_, message in errors)
    for token in case.mentions:
        assert token in joined, (
            f"{case.schema}::{case.case}: violation message does not mention {token!r}: {joined}"
        )
