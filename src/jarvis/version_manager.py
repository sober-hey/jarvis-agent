"""K4-003 Version Manager DRAFT; NOT_FROZEN / NOT_APPROVED.

runtime_enabled=False. Detached audit snapshots, following PlanHistory's pattern,
but persisted in a standalone SQLite candidate DB. No packaging/, deployment,
Git checkout, migration, release approval or rollback execution is performed.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from difflib import unified_diff
from pathlib import Path
from typing import ClassVar, Self, cast

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False

_SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)


@dataclass(frozen=True, slots=True)
class SemanticVersion:
    """Strict SemVer 2.0.0 identity; comparison ignores build metadata.

    Dataclass equality preserves the complete identity, including build metadata.
    Use compare(), not equality, to test equal SemVer precedence.
    """

    major: int
    minor: int
    patch: int
    pre_release: str | None = None
    build_metadata: str | None = None

    def __post_init__(self) -> None:
        for name in ("major", "minor", "patch"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be an exact nonnegative integer")
        for name in ("pre_release", "build_metadata"):
            value = getattr(self, name)
            if value is not None and (type(value) is not str or not value):
                raise ValueError(f"{name} must be nonempty text or None")
        if _SEMVER.fullmatch(self.to_str()) is None:
            raise ValueError("invalid SemanticVersion")
        if self.pre_release is not None:
            for identifier in self.pre_release.split("."):
                if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                    raise ValueError("numeric pre-release identifiers cannot have leading zeros")

    @classmethod
    def parse(cls, text: str) -> SemanticVersion:
        """Parse without trimming, repairing or coercing the source token."""
        if type(text) is not str or (match := _SEMVER.fullmatch(text)) is None:
            raise ValueError("version must be a strict SemVer 2.0.0 string")
        major, minor, patch, pre_release, build_metadata = match.groups()
        return cls(int(major), int(minor), int(patch), pre_release, build_metadata)

    def to_str(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.pre_release is not None:
            text += "-" + self.pre_release
        if self.build_metadata is not None:
            text += "+" + self.build_metadata
        return text

    def compare(self, other: SemanticVersion) -> int:
        """Return -1/0/1 by SemVer precedence, never by lexical version text."""
        if not isinstance(cast(object, other), SemanticVersion):
            raise TypeError("comparison requires a SemanticVersion")
        left_core = (self.major, self.minor, self.patch)
        right_core = (other.major, other.minor, other.patch)
        if left_core != right_core:
            return 1 if left_core > right_core else -1
        if self.pre_release == other.pre_release:
            return 0
        if self.pre_release is None:
            return 1
        if other.pre_release is None:
            return -1
        left_ids, right_ids = self.pre_release.split("."), other.pre_release.split(".")
        for left, right in zip(left_ids, right_ids, strict=False):
            if left == right:
                continue
            left_numeric, right_numeric = left.isdigit(), right.isdigit()
            if left_numeric and right_numeric:
                return 1 if int(left) > int(right) else -1
            if left_numeric != right_numeric:
                return -1 if left_numeric else 1
            return 1 if left > right else -1
        return 1 if len(left_ids) > len(right_ids) else -1


type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]


def _text(name: str, value: object, *, blank: bool = False) -> None:
    if type(value) is not str or (not blank and not value.strip()) or "\x00" in value:
        raise ValueError(f"{name} must be UTF-8 text without NUL, nonblank unless changelog")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} must be safely UTF-8 encodable") from error


def _json_value(value: object) -> None:
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        value.encode("utf-8", errors="strict")
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in cast(list[object], value):
            _json_value(item)
        return
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ValueError("test_results keys must be strings")
            key.encode("utf-8", errors="strict")
            _json_value(item)
        return
    raise ValueError("test_results must contain only exact finite JSON types")


def _results_json(value: dict[str, JSONValue]) -> str:
    if type(value) is not dict:
        raise ValueError("test_results must be a dict")
    # First encode to detect cycles before recursive exact-type validation.
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    _json_value(value)
    return encoded


@dataclass(frozen=True, slots=True)
class VersionRecord:
    """Detached observation, not proof of deployment, author identity or test truth.

    created_at is an aware observation time (persisted as UTC ISO-8601); ordering
    uses SQLite append sequence, not caller clocks or SemVer precedence. Nested
    test_results are snapshotted, not deeply immutable; record() revalidates them.
    """

    component: str
    version: SemanticVersion
    changelog: str
    author: str
    created_at: datetime
    git_sha: str | None = None
    test_results: dict[str, JSONValue] = field(default_factory=dict[str, JSONValue])

    def __post_init__(self) -> None:
        _text("component", self.component)
        _text("author", self.author)
        _text("changelog", self.changelog, blank=True)
        if not isinstance(cast(object, self.version), SemanticVersion):
            raise ValueError("version must be SemanticVersion")
        object.__setattr__(self, "version", replace(self.version))
        if (
            not isinstance(cast(object, self.created_at), datetime)
            or self.created_at.utcoffset() is None
        ):
            raise ValueError("created_at must be a timezone-aware datetime")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        if self.git_sha is not None and (
            type(self.git_sha) is not str
            or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", self.git_sha) is None
        ):
            raise ValueError("git_sha must be a complete 40/64-character hexadecimal SHA or None")
        object.__setattr__(self, "test_results", json.loads(_results_json(self.test_results)))


@dataclass(frozen=True, slots=True)
class VersionDiff:
    """Endpoint metadata differences and intervening append history, not source diff.

    records excludes the start and includes the end in traversal order. changes
    maps each changed field to (old, new); changelog_diff is a unified line diff.
    Returned nested data are detached from the database, not deeply immutable.
    """

    component: str
    from_version: SemanticVersion
    to_version: SemanticVersion
    records: tuple[VersionRecord, ...]
    changes: dict[str, tuple[object, object]]
    changelog_diff: str


_SCHEMA = (
    "CREATE TABLE draft_version_schema ("
    "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "version INTEGER NOT NULL CHECK(version=1))",
    "CREATE TABLE draft_version_records ("
    "sequence INTEGER PRIMARY KEY, component TEXT NOT NULL, version TEXT NOT NULL, "
    "changelog TEXT NOT NULL, author TEXT NOT NULL, created_at TEXT NOT NULL, "
    "git_sha TEXT, test_results TEXT NOT NULL CHECK(json_valid(test_results) "
    "AND json_type(test_results)='object'), "
    "major_change_authorized INTEGER NOT NULL CHECK(major_change_authorized IN (0,1)), "
    "UNIQUE(component,version))",
    "CREATE INDEX draft_version_component_sequence ON draft_version_records(component,sequence)",
    "CREATE TRIGGER draft_version_records_no_replace BEFORE INSERT ON draft_version_records "
    "WHEN EXISTS(SELECT 1 FROM draft_version_records "
    "WHERE sequence=NEW.sequence OR (component=NEW.component AND version=NEW.version)) "
    "BEGIN SELECT RAISE(ABORT,'append-only version history'); END",
    "CREATE TRIGGER draft_version_records_no_update BEFORE UPDATE ON draft_version_records "
    "BEGIN SELECT RAISE(ABORT,'append-only version history'); END",
    "CREATE TRIGGER draft_version_records_no_delete BEFORE DELETE ON draft_version_records "
    "BEGIN SELECT RAISE(ABORT,'append-only version history'); END",
)
_COLUMNS = "component,version,changelog,author,created_at,git_sha,test_results"


def _schema_objects(connection: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    return {
        name: (kind, sql)
        for name, kind, sql in connection.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
        )
    }


def _expected_schema() -> dict[str, tuple[str, str]]:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in _SCHEMA:
            connection.execute(statement)
        return _schema_objects(connection)
    finally:
        connection.close()


_EXPECTED_SCHEMA = _expected_schema()


class VersionManager:
    """Standalone offline ledger; instances are single-threaded, calls serialized.

    BEGIN IMMEDIATE serializes append/check across independent connections. The
    latest appended record is current *in this ledger*, not necessarily deployed
    or highest SemVer. Initial baselines need no major-change flag. Every later
    major change in either direction requires the exact boolean True, audited
    with its record. The flag is a trusted caller assertion, NOT ApprovalGate.
    """

    draft_status: ClassVar[str] = DRAFT_STATUS
    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(self, database: str | Path) -> None:
        self._closed = False
        self._connection = sqlite3.connect(database, isolation_level=None, timeout=5)
        try:
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA synchronous=FULL")
            with self._transaction(write=True):
                existing = _schema_objects(self._connection)
                if not existing:
                    for statement in _SCHEMA:
                        self._connection.execute(statement)
                    self._connection.execute("INSERT INTO draft_version_schema VALUES(1,1)")
                elif existing != _EXPECTED_SCHEMA:
                    raise ValueError("version schema mismatch; requires standalone candidate DB")
                if self._connection.execute("SELECT * FROM draft_version_schema").fetchall() != [
                    (1, 1)
                ]:
                    raise ValueError("version schema metadata mismatch")
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("version manager is closed")

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Generator[None, None, None]:
        self._ensure_open()
        self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    @staticmethod
    def _decode(row: tuple[object, ...]) -> VersionRecord:
        component, version, changelog, author, created_at, git_sha, results = row
        return VersionRecord(
            component=cast(str, component),
            version=SemanticVersion.parse(cast(str, version)),
            changelog=cast(str, changelog),
            author=cast(str, author),
            created_at=datetime.fromisoformat(cast(str, created_at)),
            git_sha=cast(str | None, git_sha),
            test_results=json.loads(cast(str, results)),
        )

    def record(self, record: VersionRecord, *, allow_major_change: bool = False) -> VersionRecord:
        """Append, replay identical identities idempotently, reject conflicting data.

        Same-major older versions can be appended (including approved rollback
        observations); append order is deliberate, not a monotonic release rule.
        Replaying an existing record never makes it current again.
        """
        if type(allow_major_change) is not bool:
            raise ValueError("allow_major_change must be an exact bool")
        if not isinstance(cast(object, record), VersionRecord):
            raise TypeError("record must be a VersionRecord")
        checked = replace(record)
        with self._transaction(write=True):
            prior = self._connection.execute(
                f"SELECT {_COLUMNS} FROM draft_version_records WHERE component=? AND version=?",
                (checked.component, checked.version.to_str()),
            ).fetchone()
            if prior is not None:
                saved = self._decode(prior)
                if saved != checked or _results_json(saved.test_results) != _results_json(
                    checked.test_results
                ):
                    raise ValueError("version identity already has different recorded data")
                return saved
            current = self._connection.execute(
                f"SELECT {_COLUMNS} FROM draft_version_records "
                "WHERE component=? ORDER BY sequence DESC LIMIT 1",
                (checked.component,),
            ).fetchone()
            if current is not None and self._decode(current).version.major != checked.version.major:
                if not allow_major_change:
                    raise PermissionError("major version change requires allow_major_change=True")
            self._connection.execute(
                f"INSERT INTO draft_version_records ({_COLUMNS},major_change_authorized) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    checked.component,
                    checked.version.to_str(),
                    checked.changelog,
                    checked.author,
                    checked.created_at.isoformat(),
                    checked.git_sha,
                    _results_json(checked.test_results),
                    int(allow_major_change),
                ),
            )
        return checked

    def current(self, component: str) -> VersionRecord | None:
        """Latest append or None for an unknown component; no deployment assertion."""
        _text("component", component)
        self._ensure_open()
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM draft_version_records "
            "WHERE component=? ORDER BY sequence DESC LIMIT 1",
            (component,),
        ).fetchone()
        return None if row is None else self._decode(row)

    def history(self, component: str) -> list[VersionRecord]:
        """Detached oldest-append-first snapshots; unknown component returns []."""
        _text("component", component)
        self._ensure_open()
        return [
            self._decode(row)
            for row in self._connection.execute(
                f"SELECT {_COLUMNS} FROM draft_version_records WHERE component=? ORDER BY sequence",
                (component,),
            )
        ]

    def diff(
        self, component: str, v1: SemanticVersion | str, v2: SemanticVersion | str
    ) -> VersionDiff:
        """Compare exact recorded identities in a consistent read; missing raises KeyError.

        No Git command is run: git_sha is only compared as claimed metadata. This
        cannot infer file/line changes from two caller-supplied SHA observations.
        """
        left = SemanticVersion.parse(v1) if isinstance(v1, str) else replace(v1)
        right = SemanticVersion.parse(v2) if isinstance(v2, str) else replace(v2)
        with self._transaction():
            records = self.history(component)
        indices = {record.version.to_str(): index for index, record in enumerate(records)}
        for version in (left, right):
            if version.to_str() not in indices:
                raise KeyError(f"unrecorded version for {component}: {version.to_str()}")
        start, end = indices[left.to_str()], indices[right.to_str()]
        first, last = records[start], records[end]
        direction = 1 if end >= start else -1
        traversed = (
            tuple(records[index] for index in range(start + direction, end + direction, direction))
            if start != end
            else ()
        )
        changes = {
            name: (getattr(first, name), getattr(last, name))
            for name in ("version", "changelog", "author", "created_at", "git_sha", "test_results")
            if getattr(first, name) != getattr(last, name)
        }
        if _results_json(first.test_results) != _results_json(last.test_results):
            changes["test_results"] = (first.test_results, last.test_results)
        changelog_diff = "\n".join(
            unified_diff(
                first.changelog.splitlines(keepends=True),
                last.changelog.splitlines(keepends=True),
                fromfile=left.to_str(),
                tofile=right.to_str(),
                lineterm="",
            )
        )
        return VersionDiff(component, left, right, traversed, changes, changelog_diff)

    def rollback_candidate(self, component: str) -> VersionRecord | None:
        """Nearest prior stable, lower-precedence same-major record with passing tests.

        Requires results['passed'] is True and any supplied failed/errors counters
        to be exact integer zero. Missing/ambiguous evidence is ineligible. These
        are unverified caller claims, not a release or safe-rollback approval.
        """
        with self._transaction():
            records = self.history(component)
        if not records:
            return None
        current = records[-1].version
        for record in reversed(records[:-1]):
            version, results = record.version, record.test_results
            if (
                version.major == current.major
                and version.pre_release is None
                and version.compare(current) < 0
                and results.get("passed") is True
                and all(
                    type(results.get(name, 0)) is int and results.get(name, 0) == 0
                    for name in ("failed", "errors")
                )
            ):
                return record
        return None
