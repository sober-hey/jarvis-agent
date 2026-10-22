"""K4-001 offline skill manifest DRAFT; no runtime skill execution."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest


def _module() -> Any:
    assert importlib.util.find_spec("jarvis.skill_manifest") is not None, "skill manifest missing"
    return importlib.import_module("jarvis.skill_manifest")


def _manifest(**changes: Any) -> Any:
    module = _module()
    fields = {
        "id": "skill-1",
        "name": "read-notes",
        "version": "1.0.0",
        "description": "Read offline notes / 读取笔记",
        "trigger_patterns": ["read notes", "notes:*"],
        "capabilities": {module.SkillCapability.READ_ONLY},
        "dependencies": [],
        "created_at": 1000,
        "author": "fixture-author",
        "tags": ["offline"],
    }
    fields.update(changes)
    return module.SkillManifest(**fields)


def test_register_get_list_and_draft_boundary() -> None:
    module = _module()
    assert module.DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert module.RUNTIME_ENABLED is False
    registry = module.ManifestRegistry()
    assert registry.list_all() == []
    assert registry.get("unknown") is None
    manifest = _manifest()
    assert registry.register(manifest) is None
    assert registry.get(manifest.name) == manifest
    assert registry.list_all() == [manifest]


def test_trigger_search_matches_case_sensitive_globs_without_execution() -> None:
    module = _module()
    registry = module.ManifestRegistry()
    registry.register(_manifest(id="z", name="zeta", trigger_patterns=["notes:*", "notes:*"]))
    registry.register(_manifest(id="a", name="alpha", trigger_patterns=["notes:today"]))
    registry.register(_manifest(id="b", name="beta", trigger_patterns=["other"]))
    assert [item.name for item in registry.find_by_trigger("notes:today")] == ["alpha", "zeta"]
    assert [item.name for item in registry.find_by_trigger("notes:tomorrow")] == ["zeta"]
    assert registry.find_by_trigger("Notes:today") == []
    assert registry.find_by_trigger("read notes later") == []
    assert registry.find_by_trigger("no match") == []
    registry.find_by_trigger("notes:today")[0].tags.append("caller mutation")
    assert registry.get("alpha").tags == ["offline"]
    with pytest.raises(ValueError):
        registry.find_by_trigger("")


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("1.0.0", "1.0.0", 0),
        ("2.0.0", "10.0.0", -1),
        ("1.2.9", "1.10.0", -1),
        ("1.2.10", "1.2.9", 1),
        ("1.0.0-alpha", "1.0.0-alpha.1", -1),
        ("1.0.0-alpha.1", "1.0.0-alpha.beta", -1),
        ("1.0.0-alpha.beta", "1.0.0-beta", -1),
        ("1.0.0-beta.2", "1.0.0-beta.11", -1),
        ("1.0.0-beta.11", "1.0.0-rc.1", -1),
        ("1.0.0-rc.1", "1.0.0", -1),
        ("1.0.0+build.1", "1.0.0+build.2", 0),
        ("0.0.0-0+001", "0.0.0", -1),
        ("1.0.0-ABC", "1.0.0-abc", -1),
        ("1.0.0-alpha.2", "1.0.0-alpha.10", -1),
    ],
)
def test_semver_precedence(left: str, right: str, expected: int) -> None:
    compare = _module().compare
    assert compare(left, right) == expected
    assert compare(right, left) == -expected


@pytest.mark.parametrize(
    "version",
    [
        "1",
        "1.0",
        "v1.0.0",
        "01.0.0",
        "1.00.0",
        "1.0.00",
        "-1.0.0",
        "1.0.0-01",
        "1.0.0-alpha.01",
        "1.0.0-",
        "1.0.0+",
        "1.0.0-a..b",
        "1.0.0+b..c",
        "1.0.0+a_b",
        " 1.0.0",
        "1.0.0\n",
        "１.0.0",
        "1.0.0-α",
        "1.0.0+one+two",
        "1.0.0\x00",
    ],
)
def test_invalid_semver_is_rejected_without_normalizing(version: str) -> None:
    with pytest.raises(ValueError, match="version"):
        _module().compare(version, "1.0.0")
    with pytest.raises(ValueError, match="version"):
        _module().compare("1.0.0", version)


def test_checksum_has_independent_sha256_vector_and_all_fields() -> None:
    module = _module()
    manifest = _manifest(
        capabilities={module.SkillCapability.NETWORK, module.SkillCapability.READ_ONLY}
    )
    payload = {
        "id": manifest.id,
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "trigger_patterns": manifest.trigger_patterns,
        "capabilities": ["NETWORK", "READ_ONLY"],
        "dependencies": manifest.dependencies,
        "created_at": manifest.created_at,
        "author": manifest.author,
        "tags": manifest.tags,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    assert manifest.checksum == hashlib.sha256(canonical).hexdigest()
    assert manifest.compute_checksum() == manifest.checksum
    assert manifest.verify_checksum() is True
    assert _manifest(checksum=manifest.checksum, capabilities=manifest.capabilities) == manifest
    assert replace(manifest, checksum="").checksum == manifest.checksum
    for field, value in {
        "id": "different-id",
        "name": "different-name",
        "version": "1.0.0+build",
        "description": "changed",
        "trigger_patterns": ["changed"],
        "capabilities": {module.SkillCapability.FILE_WRITE},
        "dependencies": ["other"],
        "created_at": 1001,
        "author": "other",
        "tags": ["changed"],
    }.items():
        changed = replace(manifest, **{field: value, "checksum": ""})
        assert changed.checksum != manifest.checksum, field
        with pytest.raises(ValueError, match="checksum"):
            replace(manifest, **{field: value})


def test_checksum_rejects_stale_or_invalid_digest_and_mutable_tampering() -> None:
    module = _module()
    manifest = _manifest()
    for checksum in ["0" * 64, "f" * 63, "F" * 64, "not-a-digest", None]:
        with pytest.raises((TypeError, ValueError), match="checksum"):
            _manifest(checksum=checksum)
    manifest.trigger_patterns.append("tampered")
    assert manifest.verify_checksum() is False
    registry = module.ManifestRegistry()
    with pytest.raises(ValueError, match="checksum"):
        registry.validate(manifest)
    with pytest.raises(ValueError, match="checksum"):
        registry.register(manifest)
    assert registry.list_all() == []


def test_containers_are_snapshotted_at_construction_registration_and_query() -> None:
    module = _module()
    triggers = ["notes:*"]
    capabilities = {module.SkillCapability.READ_ONLY}
    dependencies: list[str] = []
    tags = ["offline"]
    manifest = _manifest(
        trigger_patterns=triggers, capabilities=capabilities, dependencies=dependencies, tags=tags
    )
    triggers.append("outside")
    capabilities.add(module.SkillCapability.PROCESS_EXEC)
    dependencies.append("outside")
    tags.append("outside")
    assert manifest.trigger_patterns == ["notes:*"]
    assert manifest.capabilities == {module.SkillCapability.READ_ONLY}
    assert manifest.dependencies == []
    assert manifest.tags == ["offline"]
    with pytest.raises(FrozenInstanceError):
        manifest.name = "mutated"
    registry = module.ManifestRegistry()
    registry.register(manifest)
    manifest.capabilities.add(module.SkillCapability.PROCESS_EXEC)
    manifest.tags.append("outside")
    fetched = registry.get("read-notes")
    assert fetched.verify_checksum() is True
    assert fetched.capabilities == {module.SkillCapability.READ_ONLY}
    fetched.dependencies.append("outside")
    fetched.trigger_patterns.clear()
    registry.list_all()[0].tags.append("outside")
    assert registry.get("read-notes").dependencies == []
    assert registry.get("read-notes").trigger_patterns == ["notes:*"]
    assert registry.get("read-notes").tags == ["offline"]


def test_dependencies_must_be_registered_by_exact_name_and_validation_is_read_only() -> None:
    module = _module()
    registry = module.ManifestRegistry()
    consumer = _manifest(id="consumer-id", name="consumer", dependencies=["read-notes", "other"])
    with pytest.raises(ValueError, match="missing dependencies.*read-notes.*other"):
        registry.validate(consumer)
    with pytest.raises(ValueError, match="missing dependencies"):
        registry.register(consumer)
    assert registry.list_all() == []
    registry.register(_manifest())
    with pytest.raises(ValueError, match="missing dependencies.*other"):
        registry.register(consumer)
    assert [item.name for item in registry.list_all()] == ["read-notes"]
    registry.register(_manifest(id="other-id", name="other", dependencies=["read-notes"]))
    before = registry.list_all()
    assert registry.validate(consumer) is True
    assert registry.list_all() == before
    registry.register(consumer)
    assert registry.get("consumer").dependencies == ["read-notes", "other"]
    with pytest.raises(ValueError, match="missing dependencies"):
        registry.register(_manifest(id="case-id", name="case", dependencies=["Read-Notes"]))


def test_duplicate_names_and_ids_never_replace_history() -> None:
    module = _module()
    registry = module.ManifestRegistry()
    original = _manifest()
    registry.register(original)
    for collision in [
        original,
        _manifest(id="new-id", version="2.0.0"),
        _manifest(name="other"),
    ]:
        with pytest.raises(ValueError, match="already registered"):
            registry.register(collision)
        assert registry.list_all() == [original]
    assert registry.validate(original) is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", ""),
        ("name", " \t"),
        ("name", "skill\x00name"),
        ("author", None),
        ("description", ""),
        ("description", "\ud800"),
        ("version", "01.0.0"),
        ("created_at", -1),
        ("created_at", True),
        ("created_at", 1.0),
        ("created_at", 2**63),
        ("trigger_patterns", "read"),
        ("trigger_patterns", ("read",)),
        ("trigger_patterns", [""]),
        ("trigger_patterns", [1]),
        ("capabilities", {"READ_ONLY"}),
        ("capabilities", ["NETWORK"]),
        ("capabilities", {None}),
        ("dependencies", "read-notes"),
        ("dependencies", ["read-notes"]),
        ("dependencies", ["a", "a"]),
        ("dependencies", [""]),
        ("tags", "tag"),
        ("tags", [False]),
    ],
)
def test_malformed_manifest_fields_fail_closed(field: str, value: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        _manifest(**{field: value})


def test_empty_effects_and_triggers_are_explicit_no_effect_metadata() -> None:
    manifest = _manifest(trigger_patterns=[], capabilities=set(), created_at=0)
    registry = _module().ManifestRegistry()
    registry.register(manifest)
    assert registry.find_by_trigger("anything") == []
    assert registry.get(manifest.name).capabilities == set()
    with pytest.raises(TypeError):
        registry.register({"name": "not-a-manifest"})
    with pytest.raises(TypeError):
        registry.validate(None)


def test_capability_contract_and_order_independent_set_digest() -> None:
    module = _module()
    assert {capability.value for capability in module.SkillCapability} == {
        "READ_ONLY",
        "FILE_WRITE",
        "NETWORK",
        "PROCESS_EXEC",
        "MODEL_CALL",
    }
    values = list(module.SkillCapability)
    first = _manifest(capabilities=set(values))
    second = _manifest(capabilities=set(reversed(values)))
    assert first.checksum == second.checksum
    registry = module.ManifestRegistry()
    registry.register(first)
    assert registry.get(first.name).capabilities == set(values)
    assert (
        replace(
            first, trigger_patterns=list(reversed(first.trigger_patterns)), checksum=""
        ).checksum
        != first.checksum
    )


@pytest.mark.parametrize("field", ["trigger_patterns", "capabilities", "dependencies", "tags"])
def test_malformed_mutable_contents_are_revalidated_before_registration(field: str) -> None:
    module = _module()
    candidate = _manifest()
    values = getattr(candidate, field)
    if field == "capabilities":
        values.add("NETWORK")
    else:
        values.append(None)
    assert candidate.verify_checksum() is False
    registry = module.ManifestRegistry()
    with pytest.raises((TypeError, ValueError)):
        registry.register(candidate)
    assert registry.list_all() == []


def test_cycles_cannot_be_registered_in_prerequisite_first_registry() -> None:
    module = _module()
    registry = module.ManifestRegistry()
    for name, dependency in [("a", "b"), ("b", "a")]:
        with pytest.raises(ValueError, match="missing dependencies"):
            registry.register(_manifest(id=name, name=name, dependencies=[dependency]))
    assert registry.list_all() == []
