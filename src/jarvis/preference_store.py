"""K2-005 Preference Store DRAFT; NOT_FROZEN / NOT_APPROVED.

runtime_enabled=False. Standalone SQLite fixture only, never a core DB migration.
Preference content is untrusted data; EXPLICIT is a caller label, not verified
Sir identity or authorization. Timestamps use UTC epoch milliseconds, matching
MemoryEntry. No runtime integration, background inference or external calls.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from time import time_ns
from typing import ClassVar, cast

from jarvis.memory_schema import MemoryType

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False

type PreferenceValue = (
    None | bool | int | float | str | list[PreferenceValue] | dict[str, PreferenceValue]
)


def _text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be nonblank text without NUL")


def _json_types(value: object) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in cast(list[object], value):
            _json_types(item)
        return
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise TypeError("JSON object keys must be strings")
            _json_types(item)
        return
    raise ValueError("value must contain only finite, exact JSON types")


def _encode(value: PreferenceValue) -> str:
    # Encode first to detect cycles rather than recursing forever in _json_types.
    result = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    _json_types(value)
    return result


def _strings(name: str, values: list[str]) -> None:
    if not isinstance(cast(object, values), list):
        raise TypeError(f"{name} must be a list[str]")
    for value in values:
        _text(name, value)


class PreferenceSource(StrEnum):
    """Provenance claims, not trust or identity assertions."""

    EXPLICIT = "EXPLICIT"
    INFERRED = "INFERRED"
    OBSERVED = "OBSERVED"


_DEFAULT_CONFIDENCE = {
    PreferenceSource.EXPLICIT: 1.0,
    PreferenceSource.INFERRED: 0.7,
    PreferenceSource.OBSERVED: 0.5,
}
_RANK = "CASE source WHEN 'EXPLICIT' THEN 3 WHEN 'INFERRED' THEN 2 ELSE 1 END"
_COLUMNS = "key, value, source, confidence, created_at, updated_at, evidence"
_SCHEMA = {
    "preference_store_schema": (
        "CREATE TABLE preference_store_schema ("
        "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
        "version INTEGER NOT NULL CHECK (version = 1))"
    ),
    "preference_candidates": (
        "CREATE TABLE preference_candidates ("
        "key TEXT NOT NULL CHECK (length(trim(key)) > 0), "
        "value TEXT NOT NULL CHECK (json_valid(value)), "
        "source TEXT NOT NULL CHECK (source IN ('EXPLICIT', 'INFERRED', 'OBSERVED')), "
        "confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1), "
        "created_at INTEGER NOT NULL CHECK (typeof(created_at) = 'integer' AND created_at >= 0), "
        "updated_at INTEGER NOT NULL CHECK (typeof(updated_at) = 'integer' "
        "AND updated_at >= created_at), "
        "evidence TEXT NOT NULL CHECK (json_valid(evidence) AND json_type(evidence) = 'array'), "
        "PRIMARY KEY (key, source))"
    ),
}


@dataclass(slots=True)
class Preference:
    """Detached JSON candidate; dates are UTC epoch ms, confidence is in [0, 1].

    Neither confidence nor provenance verifies truth. Values/evidence are copied
    on creation; returned objects can be edited without changing persisted data.
    """

    key: str
    value: PreferenceValue
    source: PreferenceSource
    confidence: float
    created_at: int
    updated_at: int
    evidence: list[str] = field(default_factory=list[str])

    def __post_init__(self) -> None:
        _text("key", self.key)
        if not isinstance(cast(object, self.source), PreferenceSource):
            raise TypeError("source must be PreferenceSource")
        if (
            type(self.confidence) not in (float, int)
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("confidence must be finite and in [0, 1], not bool")
        for name, value in (("created_at", self.created_at), ("updated_at", self.updated_at)):
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise ValueError(f"{name} must be a nonnegative SQLite integer (UTC epoch ms)")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        _strings("evidence", self.evidence)
        self.value = cast(PreferenceValue, json.loads(_encode(self.value)))
        self.evidence = self.evidence.copy()
        self.confidence = float(self.confidence)


type PreferenceInference = Callable[[list[str]], list[Preference]]


class PreferenceStore:
    """Opted-in local draft, separate from MemorySchema/K0 and production data.

    Persists one latest candidate per (key, source). Effective reads always use
    EXPLICIT > INFERRED > OBSERVED, regardless of confidence, time or write order.
    Equal-source writes replace that candidate, not other sources. No automatic
    inference, confirmation, evidence merging, expiry, network calls or retries.
    SQLite serializes writes; each store's connection is thread-affine. This is
    plaintext SQLite, not encrypted storage or a filesystem/identity sandbox.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED
    memory_type: ClassVar[MemoryType] = MemoryType.PREFERENCE

    def __init__(
        self,
        path: str | Path,
        *,
        draft_fixture: bool = False,
        inference: PreferenceInference | None = None,
    ) -> None:
        if draft_fixture is not True:
            raise ValueError("explicit offline draft_fixture=True required")
        if (
            not isinstance(cast(object, path), (str, Path))
            or not str(path).strip()
            or "\x00" in str(path)
        ):
            raise ValueError("path must be nonblank text or Path without NUL")
        if inference is not None and not callable(inference):
            raise TypeError("inference must be callable")
        self._inference = inference
        self._connection = sqlite3.connect(path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        try:
            with self._write():
                objects = dict(
                    self._connection.execute(
                        "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
                    ).fetchall()
                )
                if not objects:
                    for statement in _SCHEMA.values():
                        self._connection.execute(statement)
                    self._connection.execute("INSERT INTO preference_store_schema VALUES (1, 1)")
                elif objects != _SCHEMA:
                    raise ValueError("requires standalone preference draft database")
                versions = self._connection.execute(
                    "SELECT singleton, version FROM preference_store_schema"
                ).fetchall()
                if [tuple(row) for row in versions] != [(1, 1)]:
                    raise ValueError("unsupported standalone preference draft version")
        except BaseException:
            self._connection.close()
            raise

    @contextmanager
    def _write(self) -> Generator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def __enter__(self) -> PreferenceStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the owned connection; every successful write already committed."""
        self._connection.close()

    @staticmethod
    def _preference(row: sqlite3.Row) -> Preference:
        return Preference(
            key=row["key"],
            value=json.loads(row["value"]),
            source=PreferenceSource(row["source"]),
            confidence=row["confidence"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            evidence=json.loads(row["evidence"]),
        )

    def set(
        self,
        key: str,
        value: PreferenceValue,
        source: PreferenceSource,
        *,
        confidence: float | None = None,
        evidence: list[str] | None = None,
    ) -> Preference:
        """Explicit write/confirmation step; return the effective winning snapshot.

        Default confidence values (1.0/0.7/0.5) are draft heuristics, not calibrated
        probabilities. Same-source creation time survives updates; updated_at
        clamps backward clocks. Lower-priority candidates are stored but hidden.
        """
        if not isinstance(cast(object, source), PreferenceSource):
            raise TypeError("source must be PreferenceSource")
        now = time_ns() // 1_000_000
        candidate = Preference(
            key,
            value,
            source,
            _DEFAULT_CONFIDENCE[source] if confidence is None else confidence,
            now,
            now,
            [] if evidence is None else evidence,
        )
        encoded_value = _encode(candidate.value)
        encoded_evidence = json.dumps(candidate.evidence, ensure_ascii=False)
        with self._write():
            self._connection.execute(
                f"INSERT INTO preference_candidates ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key, source) DO UPDATE SET value = excluded.value, "
                "confidence = excluded.confidence, evidence = excluded.evidence, "
                "updated_at = max(preference_candidates.updated_at, excluded.updated_at)",
                (
                    candidate.key,
                    encoded_value,
                    candidate.source.value,
                    candidate.confidence,
                    candidate.created_at,
                    candidate.updated_at,
                    encoded_evidence,
                ),
            )
            effective = self.get(key)
            assert effective is not None
        return effective

    def get(self, key: str) -> Preference | None:
        """Return the highest-source candidate, without implicit access/time writes."""
        _text("key", key)
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM preference_candidates WHERE key = ? "
            f"ORDER BY {_RANK} DESC LIMIT 1",
            (key,),
        ).fetchone()
        return None if row is None else self._preference(row)

    def list_all(self) -> list[Preference]:
        """Return one effective snapshot per key, in exact case-sensitive key order."""
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM (SELECT {_COLUMNS}, "
            f"row_number() OVER (PARTITION BY key ORDER BY {_RANK} DESC) AS rank "
            "FROM preference_candidates) WHERE rank = 1 ORDER BY key"
        ).fetchall()
        return [self._preference(row) for row in rows]

    def delete(self, key: str) -> bool:
        """Delete ALL source candidates so hidden values never reappear; report existence.

        Logical deletion only: not secure erasure of SQLite pages/journal/backups.
        """
        _text("key", key)
        with self._write():
            changed = self._connection.execute(
                "DELETE FROM preference_candidates WHERE key = ?", (key,)
            ).rowcount
        return changed > 0

    def infer(self, observations: list[str]) -> list[Preference]:
        """Call an injected semantic inference interface; return proposals, NEVER set.

        No phrase tables or fabricated fallback. Empty input returns []; nonempty
        input needs a caller-supplied inference callable. Providers receive copied
        raw observations (untrusted data); privacy/transport authorization remains
        the caller's responsibility. Only INFERRED proposals with nonempty evidence
        drawn from those observations are accepted. These checks verify provenance
        membership, NOT whether the model's semantic interpretation is correct.
        Confirmation is a separate, deliberate set(..., source=INFERRED) call;
        infer does not promote hypotheses to EXPLICIT or overwrite explicit data.
        """
        _strings("observations", observations)
        original = observations.copy()
        if not original:
            return []
        if self._inference is None:
            raise RuntimeError("no inference callable configured")
        output = self._inference(original.copy())
        if not isinstance(cast(object, output), list):
            raise TypeError("inference must return list[Preference]")
        result: list[Preference] = []
        keys: set[str] = set()
        for candidate in output:
            if not isinstance(cast(object, candidate), Preference):
                raise TypeError("inference must return Preference objects")
            checked = replace(deepcopy(candidate))
            if checked.source is not PreferenceSource.INFERRED:
                raise ValueError("inference proposals must have INFERRED source")
            if not checked.evidence or not set(checked.evidence).issubset(original):
                raise ValueError("inference evidence must reference supplied observations")
            if checked.key in keys:
                raise ValueError("duplicate inference key")
            keys.add(checked.key)
            result.append(checked)
        return result
