"""K4-001 DRAFT; NOT_FROZEN, NOT_APPROVED, runtime_enabled=False.

Offline skill metadata only: capability declarations never grant permissions.
No discovery, installation, execution, persistence or production runtime wiring.
All timestamps are nonnegative UTC epoch milliseconds.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from fnmatch import fnmatchcase

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False

_SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)


def _version(value: object) -> tuple[tuple[int, int, int], tuple[str, ...]]:
    match = _SEMVER.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("version must be an exact SemVer 2.0.0 string")
    prerelease = tuple(match[4].split(".")) if match[4] is not None else ()
    if any(item.isdigit() and len(item) > 1 and item[0] == "0" for item in prerelease):
        raise ValueError("version prerelease numeric identifiers cannot have leading zeroes")
    return (int(match[1]), int(match[2]), int(match[3])), prerelease


def compare(v1: str, v2: str) -> int:
    """Return -1/0/1 by SemVer precedence; build metadata does not affect rank."""
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
    """Candidate metadata with copied list/set fields, not an execution policy.

    ``checksum=''`` seals newly authored contents; a nonempty supplied digest
    must match exactly. To author a revised copy, explicitly reset checksum.
    Registry storage is detached from caller-owned mutable containers. Names,
    IDs, authors, dependencies and tags remain opaque case-sensitive text.
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

    def __post_init__(self) -> None:
        self._validate_fields()
        object.__setattr__(self, "trigger_patterns", list(self.trigger_patterns))
        object.__setattr__(self, "capabilities", set(self.capabilities))
        object.__setattr__(self, "dependencies", list(self.dependencies))
        object.__setattr__(self, "tags", list(self.tags))
        if type(self.checksum) is not str:
            raise TypeError("checksum must be a lowercase SHA-256 hex string")
        if self.checksum == "":
            object.__setattr__(self, "checksum", self.compute_checksum())
        if not self.verify_checksum():
            raise ValueError("checksum does not match manifest contents")

    def _validate_fields(self) -> None:
        for name in ("id", "name", "description", "author"):
            _text(name, getattr(self, name))
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
        for capability in self.capabilities:
            if type(capability) is not SkillCapability:
                raise TypeError("capabilities must contain only SkillCapability members")

    def compute_checksum(self) -> str:
        """SHA-256 of canonical UTF-8 JSON, excluding only checksum itself.

        Object keys and capability values are sorted; list order is preserved.
        Unkeyed SHA-256 detects stale digests, not malicious recomputation or
        authorship. No skill implementation/code bytes are covered here.
        """
        self._validate_fields()
        content = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "trigger_patterns": self.trigger_patterns,
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "dependencies": self.dependencies,
            "created_at": self.created_at,
            "author": self.author,
            "tags": self.tags,
        }
        canonical = json.dumps(
            content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def verify_checksum(self) -> bool:
        """Check the current contents against the stored digest without resealing."""
        try:
            return re.fullmatch(r"[0-9a-f]{64}", self.checksum) is not None and hmac.compare_digest(
                self.checksum, self.compute_checksum()
            )
        except (TypeError, ValueError):
            return False


class ManifestRegistry:
    """Process-local offline metadata registry; no skill is loaded or executed."""

    def __init__(self) -> None:
        self._manifests: dict[str, SkillManifest] = {}

    def register(self, manifest: SkillManifest) -> None:
        self.validate(manifest)
        snapshot = replace(manifest)
        if snapshot.name in self._manifests or any(
            stored.id == snapshot.id for stored in self._manifests.values()
        ):
            raise ValueError("manifest name or id is already registered")
        self._manifests[snapshot.name] = snapshot

    def validate(self, manifest: SkillManifest) -> bool:
        """Validate integrity and dependencies without changing any registration.

        Dependencies are exact registered names, never import paths or version
        expressions. Register prerequisites first; no updates/replacements means
        a missing dependency or cycle cannot enter this draft registry.
        """
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
        """Match input text against stored case-sensitive shell globs, not regex.

        Whole-string matching only; an empty list declares no automatic trigger.
        Matches are advisory metadata, not permission to invoke a skill.
        """
        _text("pattern", pattern)
        return [
            manifest
            for manifest in self.list_all()
            if any(fnmatchcase(pattern, trigger) for trigger in manifest.trigger_patterns)
        ]
