"""K2-007 Provenance & Conflict DRAFT; NOT_FROZEN / NOT_APPROVED.

runtime_enabled=False. Detached offline candidates, never authenticated evidence
or permission to overwrite a product store. Times are UTC epoch milliseconds.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from itertools import combinations
from typing import ClassVar, cast

from jarvis.semantic_memory import KnowledgeEntry, SemanticMemory

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


def _text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be nonblank text without NUL")


def _timestamp(name: str, value: object) -> None:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError(f"{name} must be nonnegative integer epoch milliseconds")


class SourceType(StrEnum):
    """Claimed origin, not a source authenticity or trust ranking."""

    USER_STATEMENT = "USER_STATEMENT"
    OBSERVED = "OBSERVED"
    INFERRED = "INFERRED"
    EXTERNAL_DOC = "EXTERNAL_DOC"


@dataclass(frozen=True, slots=True)
class Provenance:
    """Origin claim; source_id identifies the speaker/message/observation/doc.

    Evidence references are opaque, not fetched or executed. A list is accepted
    as requested; construction and tracker boundaries always detach that list.
    """

    source_type: SourceType
    source_id: str
    recorded_at: int
    confidence: float
    evidence_refs: list[str] = field(default_factory=lambda: list[str]())

    def __post_init__(self) -> None:
        _require_enum(self.source_type, SourceType)
        _text("source_id", self.source_id)
        _timestamp("recorded_at", self.recorded_at)
        value = cast(object, self.confidence)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be finite and in [0, 1]")
        try:
            confidence = float(value)
        except OverflowError as error:
            raise ValueError("confidence must be finite and in [0, 1]") from error
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be finite and in [0, 1]")
        object.__setattr__(self, "confidence", confidence)
        if not isinstance(cast(object, self.evidence_refs), list):
            raise TypeError("evidence_refs must be a list of strings")
        for reference in self.evidence_refs:
            _text("evidence reference", reference)
        object.__setattr__(self, "evidence_refs", list(self.evidence_refs))


class ConflictType(StrEnum):
    """Syntactic relation candidates, not a judgment of factual truth."""

    CONTRADICTION = "CONTRADICTION"
    SUPERSESSION = "SUPERSESSION"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    """Unordered pair with a stable collision-free ID and optional resolution.

    The original detector only emits CONTRADICTION or DUPLICATE. SUPERSESSION
    requires an explicit resolve call; a newer assertion alone is not proof.
    """

    memory_id_1: str
    memory_id_2: str
    conflict_type: ConflictType
    detected_at: int
    resolution: str | None = None

    def __post_init__(self) -> None:
        _text("memory_id_1", self.memory_id_1)
        _text("memory_id_2", self.memory_id_2)
        if self.memory_id_1 == self.memory_id_2:
            raise ValueError("conflict requires distinct memory ids")
        _require_enum(self.conflict_type, ConflictType)
        _timestamp("detected_at", self.detected_at)
        if self.resolution is not None:
            _text("resolution", self.resolution)

    @property
    def conflict_id(self) -> str:
        """JSON framing avoids delimiter collisions; independent of type/order."""
        return json.dumps(sorted((self.memory_id_1, self.memory_id_2)), ensure_ascii=False)


def _require_enum[T: StrEnum](value: object, expected: type[T]) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"expected {expected.__name__}")


class ProvenanceTracker:
    """Single-process offline source ledger; callers serialize mutations.

    Reads are detached. Origin types and confidence never certify truth.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(self, *, clock_ms: Callable[[], int] | None = None) -> None:
        self._chains: dict[str, list[Provenance]] = {}
        self._entries: dict[str, KnowledgeEntry] = {}
        self._conflicts: dict[str, ConflictRecord] = {}
        self._duplicate_aliases: dict[str, str] = {}
        self._effective_aliases: dict[str, str] = {}
        self._clock_ms = clock_ms if clock_ms is not None else lambda: time.time_ns() // 1_000_000

    @staticmethod
    def _root(memory_id: str, aliases: dict[str, str]) -> str:
        while memory_id in aliases:
            memory_id = aliases[memory_id]
        return memory_id

    def record(self, memory_id: str, provenance: Provenance) -> None:
        """Append an origin claim; exact replay is idempotent, not corroboration."""
        _text("memory_id", memory_id)
        if not isinstance(cast(object, provenance), Provenance):
            raise TypeError("expected Provenance")
        checked = replace(provenance)
        root = self._root(memory_id, self._duplicate_aliases)
        chain = self._chains.setdefault(root, [])
        if checked not in chain:
            chain.append(checked)

    def get_chain(self, memory_id: str) -> list[Provenance]:
        """Chronological claims, stable on ties; unknown IDs return an empty list."""
        _text("memory_id", memory_id)
        root = self._root(memory_id, self._duplicate_aliases)
        return [
            replace(item)
            for item in sorted(self._chains.get(root, []), key=lambda item: item.recorded_at)
        ]

    def detect_conflicts(self, entries: Iterable[KnowledgeEntry]) -> list[ConflictRecord]:
        """Snapshot a batch and compare exact, case-sensitive subjects/objects.

        Per the requested DRAFT rule, same subject / different object values
        produces CONTRADICTION even across predicates. This deliberately broad
        heuristic can flag independent facts; it is not logical contradiction
        analysis. DUPLICATE requires the full triple to match. Cross-predicate
        candidates may only be annotated, never superseded or merged.
        Batch validation precedes writes. Replays preserve IDs, times/resolutions.
        No resolution is automatic. Each call compares only its supplied batch.
        """
        checked: dict[str, KnowledgeEntry] = {}
        for entry in entries:
            if not isinstance(cast(object, entry), KnowledgeEntry):
                raise TypeError("expected KnowledgeEntry")
            snapshot = replace(entry)
            prior = checked.get(snapshot.id, self._entries.get(snapshot.id))
            if prior is not None and prior != snapshot:
                raise ValueError("conflicting id cannot overwrite a knowledge candidate")
            checked[snapshot.id] = snapshot
        now = self._clock_ms()
        _timestamp("detected_at", now)
        detected: list[ConflictRecord] = []
        for first, second in combinations(sorted(checked.values(), key=lambda item: item.id), 2):
            if first.subject != second.subject:
                continue
            if first.object_value != second.object_value:
                kind = ConflictType.CONTRADICTION
            elif first.predicate == second.predicate:
                kind = ConflictType.DUPLICATE
            else:
                continue
            candidate = ConflictRecord(first.id, second.id, kind, now)
            detected.append(self._conflicts.get(candidate.conflict_id, candidate))
        self._entries.update(checked)
        for conflict in detected:
            self._conflicts[conflict.conflict_id] = conflict
        return [replace(conflict) for conflict in detected]

    def get_conflict(self, conflict_id: str) -> ConflictRecord:
        """Read a detached audit record; unknown IDs raise KeyError."""
        _text("conflict_id", conflict_id)
        return replace(self._conflicts[conflict_id])

    def get_effective_entry(self, memory_id: str) -> KnowledgeEntry:
        """Read the tracker-local resolved view, never modifying SemanticMemory."""
        _text("memory_id", memory_id)
        root = self._root(memory_id, self._effective_aliases)
        return replace(self._entries[root])

    def resolve(self, conflict_id: str, resolution: str) -> ConflictRecord:
        """Apply an explicit caller decision; repeat is idempotent, rewrite fails.

        SUPERSESSION redirects the older created_at to the newer; equal times
        reject rather than invent recency. DUPLICATE coalesces equal triples and
        unions source claims, retaining the conservative SemanticMemory winner
        without boosting confidence. Other nonblank text annotates a contradiction
        without selecting truth. Original entries and source claims are retained.
        Stale multi-pair decisions reject; they cannot overwrite a newer winner.
        """
        _text("conflict_id", conflict_id)
        _text("resolution", resolution)
        conflict = self._conflicts[conflict_id]
        if conflict.resolution is not None:
            if conflict.resolution != resolution:
                raise ValueError("conflict already resolved; cannot rewrite history")
            return replace(conflict)
        first = self.get_effective_entry(conflict.memory_id_1)
        second = self.get_effective_entry(conflict.memory_id_2)
        kind = conflict.conflict_type
        if resolution in ("SUPERSESSION", "DUPLICATE"):
            if first.id == second.id:
                raise ValueError("stale conflict: candidates already share a winner")
            if (first.subject, first.predicate) != (second.subject, second.predicate):
                raise ValueError("automatic strategy requires identical subject and predicate")
            if resolution == "SUPERSESSION":
                if first.created_at == second.created_at:
                    raise ValueError("equal timestamp cannot establish supersession")
                winner = max((first, second), key=lambda item: item.created_at)
                kind = ConflictType.SUPERSESSION
            else:
                if (first.subject, first.predicate, first.object_value) != (
                    second.subject,
                    second.predicate,
                    second.object_value,
                ):
                    raise ValueError("duplicate merge requires identical triples")
                winner = SemanticMemory.merge(first, second)
                kind = ConflictType.DUPLICATE
            loser = second if winner.id == first.id else first
            if kind is ConflictType.DUPLICATE:
                winner_root = self._root(winner.id, self._duplicate_aliases)
                loser_root = self._root(loser.id, self._duplicate_aliases)
                combined = self.get_chain(winner_root)
                for claim in self.get_chain(loser_root):
                    if claim not in combined:
                        combined.append(claim)
                self._chains[winner_root] = combined
                self._duplicate_aliases[loser_root] = winner_root
            self._effective_aliases[loser.id] = winner.id
        updated = replace(conflict, conflict_type=kind, resolution=resolution)
        self._conflicts[conflict_id] = updated
        return replace(updated)
