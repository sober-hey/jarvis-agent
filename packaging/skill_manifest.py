"""Runtime skill metadata. Declarations do not grant execution permissions.

Checksums cover canonical manifest metadata only, never implementation bytes.
Timestamps are nonnegative UTC epoch milliseconds.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Self, cast

RUNTIME_ENABLED = True
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*", re.ASCII)
_SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)


def self_evaluate() -> dict[str, object]:
    """Describe validation semantics without inspecting authored skill records."""
    return {
        "module": "skill_manifest",
        "domain": "skill metadata validation canonical checksums semantic versioning and trigger matching",
        "current_version": "validated dataclass manifests with canonical SHA-256 metadata and strict SemVer comparison",
        "known_limitations": [
            "The unkeyed checksum covers metadata only; it proves neither code integrity nor author identity.",
            "Frozen dataclasses retain mutable list/set fields; mutation invalidates rather than prevents stale checksums.",
            "Trigger matching is case-sensitive whole-string glob matching, not semantic intent recognition.",
            "ManifestRegistry checks literal dependency names; version requirement parsing belongs to SkillRegistry.",
        ],
        "improvement_directions": [
            "Keep metadata checksums distinct from signed author provenance and implementation digests.",
            "Evaluate deeply immutable containers while preserving the public serialization contract.",
            "Add a separately tested intent matcher and align dependency validation across registry implementations.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


def validate_name(name: str) -> None:
    """Reject non-ASCII identifiers, traversal, separators and normalization."""
    if not isinstance(cast(object, name), str) or _NAME.fullmatch(name) is None:
        raise ValueError("skill name must be an exact ASCII identifier")


def _version(value: object) -> tuple[tuple[int, int, int], tuple[str, ...]]:
    match = _SEMVER.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("version must be an exact SemVer 2.0.0 string")
    prerelease = tuple(match[4].split(".")) if match[4] is not None else ()
    if any(item.isdigit() and len(item) > 1 and item[0] == "0" for item in prerelease):
        raise ValueError("version prerelease numeric identifiers cannot have leading zeroes")
    return (int(match[1]), int(match[2]), int(match[3])), prerelease


def compare(v1: str, v2: str) -> int:
    """Return -1/0/1 by strict SemVer precedence, ignoring build metadata."""
    core1, pre1 = _version(v1)
    core2, pre2 = _version(v2)
    if core1 != core2:
        return (core1 > core2) - (core1 < core2)
    if pre1 == pre2:
        return 0
    if not pre1 or not pre2:
        return 1 if not pre1 else -1
    for first, second in zip(pre1, pre2, strict=False):
        if first == second:
            continue
        if first.isdigit() and second.isdigit():
            return (int(first) > int(second)) - (int(first) < int(second))
        if first.isdigit() != second.isdigit():
            return -1 if first.isdigit() else 1
        return (first > second) - (first < second)
    return (len(pre1) > len(pre2)) - (len(pre1) < len(pre2))


def _text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be nonempty text without NUL")
    value.encode("utf-8", errors="strict")


class SkillCapability(StrEnum):
    """Declared effects, not an execution grant or enforced sandbox policy."""

    READ_ONLY = "READ_ONLY"
    FILE_WRITE = "FILE_WRITE"
    NETWORK = "NETWORK"
    PROCESS_EXEC = "PROCESS_EXEC"
    MODEL_CALL = "MODEL_CALL"


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """Validated metadata retaining the public list/set constructor contract.

    Containers are copied, not deeply immutable. Mutation invalidates checksum;
    serialization and registry writes revalidate rather than silently reseal.
    An empty checksum seals authored metadata; nonempty digests must match.
    """

    id: str
    name: str
    version: str
    description: str
    trigger_patterns: list[str]
    capabilities: set[SkillCapability]
    dependencies: list[str]
    created_at: int
    author: str
    tags: list[str] = field(default_factory=list[str])
    checksum: str = ""
    # Authored in JARVIS's first person; empty keeps legacy metadata compatible.
    description_for_system_prompt: str = ""

    @staticmethod
    def self_evaluate() -> dict[str, object]:
        """Expose implementation metadata, independent of manifest field values."""
        return self_evaluate()

    def __post_init__(self) -> None:
        self._validate_fields()
        for name in ("trigger_patterns", "capabilities", "dependencies", "tags"):
            value = getattr(self, name)
            object.__setattr__(self, name, set(value) if name == "capabilities" else list(value))
        if type(self.checksum) is not str:
            raise TypeError("checksum must be a lowercase SHA-256 hex string")
        if self.checksum == "":
            object.__setattr__(self, "checksum", self.compute_checksum())
        if not self.verify_checksum():
            raise ValueError("checksum does not match manifest contents")

    def _validate_fields(self) -> None:
        for name in ("id", "name", "description", "author"):
            _text(name, getattr(self, name))
        if type(self.description_for_system_prompt) is not str:
            raise TypeError("description_for_system_prompt must be text")
        if self.description_for_system_prompt:
            _text("description_for_system_prompt", self.description_for_system_prompt)
        validate_name(self.name)
        _version(self.version)
        if type(self.created_at) is not int or not 0 <= self.created_at < 2**63:
            raise ValueError("created_at must be an exact nonnegative SQLite integer (UTC ms)")
        for name in ("trigger_patterns", "dependencies", "tags"):
            values: list[object] = getattr(self, name)
            if type(values) is not list:
                raise TypeError(f"{name} must be a list of strings")
            for value in values:
                _text(name, value)
        if self.name in self.dependencies:
            raise ValueError("dependencies cannot include the skill itself")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("dependencies must not contain duplicates")
        if type(self.capabilities) is not set:
            raise TypeError("capabilities must be a set of SkillCapability")
        if any(type(value) is not SkillCapability for value in self.capabilities):
            raise TypeError("capabilities must contain only SkillCapability members")

    def _metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "trigger_patterns": list(self.trigger_patterns),
            "capabilities": sorted(value.value for value in self.capabilities),
            "dependencies": list(self.dependencies),
            "created_at": self.created_at,
            "author": self.author,
            "tags": list(self.tags),
        }
        # Preserve existing on-disk checksums when the optional field is absent.
        if self.description_for_system_prompt:
            metadata["description_for_system_prompt"] = self.description_for_system_prompt
        return metadata

    def compute_checksum(self) -> str:
        """SHA-256 of canonical UTF-8 metadata JSON, excluding checksum itself."""
        self._validate_fields()
        content = json.dumps(
            self._metadata(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    def verify_checksum(self) -> bool:
        """Detect stale/invalid digests, not malicious recomputation or authorship."""
        try:
            return re.fullmatch(r"[0-9a-f]{64}", self.checksum) is not None and hmac.compare_digest(
                self.checksum, self.compute_checksum()
            )
        except (TypeError, ValueError):
            return False

    def to_dict(self) -> dict[str, object]:
        """Return a detached JSON-ready snapshot with sorted capability values."""
        snapshot = replace(self)
        return {**snapshot._metadata(), "checksum": snapshot.checksum}

    @classmethod
    def from_dict(cls, data: dict[str, object], require_checksum: bool = False) -> Self:
        """Decode JSON metadata; strict fields, list-to-enum-set capability boundary.

        Disk readers must require a supplied checksum to avoid resealing tampered
        metadata. Constructor defaults remain available for newly authored data.
        """
        if type(data) is not dict:
            raise TypeError("manifest must be a dict")
        if type(require_checksum) is not bool:
            raise TypeError("require_checksum must be an exact bool")
        allowed = {item.name for item in fields(cls)}
        if any(type(key) is not str or key not in allowed for key in data):
            raise ValueError("unknown manifest fields")
        if require_checksum and (type(data.get("checksum")) is not str or not data["checksum"]):
            raise ValueError("checksum is required")
        values = dict(data)
        if "capabilities" not in values or type(values["capabilities"]) is not list:
            raise TypeError("JSON capabilities must be a list of strings")
        capabilities = cast(list[object], values["capabilities"])
        if any(type(value) is not str for value in capabilities):
            raise TypeError("JSON capabilities must contain only strings")
        converted = {SkillCapability(cast(str, value)) for value in capabilities}
        if len(converted) != len(capabilities):
            raise ValueError("duplicate capabilities")
        values["capabilities"] = converted
        required = {
            "id",
            "name",
            "version",
            "description",
            "trigger_patterns",
            "capabilities",
            "dependencies",
            "created_at",
            "author",
        }
        if not required <= values.keys():
            raise ValueError("missing required manifest fields")
        return cls(
            id=cast(str, values["id"]),
            name=cast(str, values["name"]),
            version=cast(str, values["version"]),
            description=cast(str, values["description"]),
            trigger_patterns=cast(list[str], values["trigger_patterns"]),
            capabilities=converted,
            dependencies=cast(list[str], values["dependencies"]),
            created_at=cast(int, values["created_at"]),
            author=cast(str, values["author"]),
            tags=cast(list[str], values.get("tags", [])),
            checksum=cast(str, values.get("checksum", "")),
            description_for_system_prompt=cast(
                str, values.get("description_for_system_prompt", "")
            ),
        )

    def matches(self, text: str) -> bool:
        """Case-sensitive whole-string fnmatch globs; no regex or execution."""
        _text("text", text)
        if not self.verify_checksum():
            raise ValueError("checksum does not match manifest contents")
        return any(fnmatchcase(text, trigger) for trigger in self.trigger_patterns)


class ManifestRegistry:
    """In-memory manifest index; disk installation belongs to SkillRegistry."""

    def __init__(self) -> None:
        self._manifests: dict[str, SkillManifest] = {}

    def register(self, manifest: SkillManifest) -> None:
        self.validate(manifest)
        snapshot = replace(manifest)
        if snapshot.name in self._manifests or any(
            item.id == snapshot.id for item in self._manifests.values()
        ):
            raise ValueError("manifest name or id is already registered")
        self._manifests[snapshot.name] = snapshot

    def validate(self, manifest: SkillManifest) -> bool:
        if type(manifest) is not SkillManifest:
            raise TypeError("expected SkillManifest")
        snapshot = replace(manifest)
        missing = [name for name in snapshot.dependencies if name not in self._manifests]
        if missing:
            raise ValueError(f"missing dependencies: {', '.join(missing)}")
        return True

    def get(self, name: str) -> SkillManifest | None:
        manifest = self._manifests.get(name)
        return None if manifest is None else replace(manifest)

    def list_all(self) -> list[SkillManifest]:
        return [replace(self._manifests[name]) for name in sorted(self._manifests)]

    def find_by_trigger(self, pattern: str) -> list[SkillManifest]:
        _text("pattern", pattern)
        return [item for item in self.list_all() if item.matches(pattern)]
