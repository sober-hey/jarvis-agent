"""K2-004 Semantic Memory DRAFT; NOT_FROZEN / NOT_APPROVED.

runtime_enabled=False. Offline structured knowledge candidates, not episodic
records, authenticated facts, durable storage or runtime authorization.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import ClassVar

from jarvis.memory_schema import MemoryEntry, MemoryType

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


def _text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be nonblank text without NUL")


def _timestamp(name: str, value: object, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ValueError(f"{name} must be integer epoch milliseconds >= {minimum}")


def _number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number, not bool")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class KnowledgeEntry(MemoryEntry):
    """A subject → predicate → object candidate with shared provenance metadata.

    All times use the MemoryEntry UTC epoch millisecond convention. None means
    never verified; a confidence value or a source reference is not proof.
    """

    subject: str
    predicate: str
    object_value: str
    confidence: float
    last_verified_at: int | None = None

    def __post_init__(self) -> None:
        MemoryEntry.__post_init__(self)
        if self.type is not MemoryType.SEMANTIC:
            raise ValueError("KnowledgeEntry requires MemoryType.SEMANTIC")
        for name in ("subject", "predicate", "object_value"):
            _text(name, getattr(self, name))
        confidence = _number("confidence", self.confidence)
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")
        object.__setattr__(self, "confidence", confidence)
        if self.last_verified_at is not None:
            _timestamp("last_verified_at", self.last_verified_at, self.created_at)


class SemanticMemory:
    """Detached single-process fixture, with explicit caller-owned mutations.

    No persistence, automatic verification/decay, model calls or runtime wiring.
    Fixture owners must serialize all calls. Reads never refresh access metadata.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(self) -> None:
        self._entries: dict[str, KnowledgeEntry] = {}
        self._subjects: dict[str, set[str]] = {}
        self._predicates: dict[str, set[str]] = {}

    @staticmethod
    def _snapshot(entry: object) -> KnowledgeEntry:
        if not isinstance(entry, KnowledgeEntry):
            raise TypeError("expected KnowledgeEntry")
        return replace(entry)

    def store(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        """Append a candidate; identical ID replay is idempotent, conflicts fail.

        Distinct IDs may assert conflicting values. No implicit merge, overwrite
        or fact promotion occurs, even for a high-confidence candidate.
        """
        checked = self._snapshot(entry)
        prior = self._entries.get(checked.id)
        if prior is not None and prior != checked:
            raise ValueError("conflicting id cannot overwrite knowledge")
        self._entries[checked.id] = checked
        self._subjects.setdefault(checked.subject, set()).add(checked.id)
        self._predicates.setdefault(checked.predicate, set()).add(checked.id)
        return replace(checked)

    def _ordered(self, ids: set[str]) -> list[KnowledgeEntry]:
        return [
            replace(entry)
            for entry in sorted(
                (self._entries[id] for id in ids), key=lambda item: (item.created_at, item.id)
            )
        ]

    def retrieve(self, subject: str, *, predicate: str | None = None) -> list[KnowledgeEntry]:
        """Exact case-sensitive graph lookup; optionally intersect with predicate.

        Results are detached and ordered by creation time then lexical ID.
        """
        _text("subject", subject)
        ids = self._subjects.get(subject, set())
        if predicate is not None:
            _text("predicate", predicate)
            ids = ids & self._predicates.get(predicate, set())
        return self._ordered(ids)

    def by_predicate(self, predicate: str) -> list[KnowledgeEntry]:
        """Exact predicate lookup across all subjects, with the same stable order."""
        _text("predicate", predicate)
        return self._ordered(self._predicates.get(predicate, set()))

    def search(self, query: str) -> list[KnowledgeEntry]:
        """Unicode casefolded literal substring over triple fields and content.

        Not embeddings, FTS or authenticated evidence retrieval. No provenance
        or tag search; blank queries fail instead of returning every candidate.
        """
        _text("query", query)
        needle = query.casefold()
        return self._ordered(
            {
                id
                for id, entry in self._entries.items()
                if any(
                    needle in value.casefold()
                    for value in (entry.subject, entry.predicate, entry.object_value, entry.content)
                )
            }
        )

    def update_confidence(self, id: str, delta: float) -> KnowledgeEntry:
        """Explicitly add finite delta and clamp to [0, 1]; unknown IDs raise KeyError.

        This is not verification and never changes source, last_verified_at or
        access metadata. Callers own evidence/policy for changing confidence.
        """
        _text("id", id)
        amount = _number("delta", delta)
        prior = self._entries[id]
        updated = replace(prior, confidence=max(0.0, min(1.0, prior.confidence + amount)))
        self._entries[id] = updated
        return replace(updated)

    @staticmethod
    def merge(entry1: KnowledgeEntry, entry2: KnowledgeEntry) -> KnowledgeEntry:
        """Pure conservative reconciliation candidate for the same subject/predicate.

        Higher stored confidence wins; ties prefer later last_verified_at (None
        is oldest), later created_at, then lexical ID ascending. The winner's
        full metadata is preserved, even for equal object values: confidence is
        never boosted and verification is never borrowed from a conflicting
        assertion. Same-ID different payloads and unrelated keys are rejected.
        Neither source entry nor the store is changed; original conflict evidence
        remains available. This heuristic is not a truth/authorization decision
        or K2-007's future provenance/conflict policy.
        """
        first = SemanticMemory._snapshot(entry1)
        second = SemanticMemory._snapshot(entry2)
        if first.id == second.id and first != second:
            raise ValueError("conflicting id cannot identify two knowledge candidates")
        if (first.subject, first.predicate) != (second.subject, second.predicate):
            raise ValueError("merge requires identical subject and predicate")

        def rank(entry: KnowledgeEntry) -> tuple[float, int, int, str]:
            verified = -1 if entry.last_verified_at is None else entry.last_verified_at
            return (-entry.confidence, -verified, -entry.created_at, entry.id)

        return replace(min((first, second), key=rank))

    @staticmethod
    def decayed_confidence(
        entry: KnowledgeEntry,
        *,
        now_ms: int,
        half_life_ms: int = 30 * 24 * 60 * 60 * 1000,
    ) -> float:
        """Pure confidence * 2**(-age_ms/half_life_ms), bounded to [0, 1].

        Age starts at last_verified_at, or created_at for never-verified data;
        reads do not count as verification. Backward clocks clamp age to zero.
        The 30-day default is a configurable DRAFT heuristic, not retention or
        truth policy. Nothing is scheduled, written, refreshed or deleted.
        """
        checked = SemanticMemory._snapshot(entry)
        _timestamp("now_ms", now_ms)
        _timestamp("half_life_ms", half_life_ms, 1)
        baseline = (
            checked.created_at if checked.last_verified_at is None else checked.last_verified_at
        )
        age_ms = max(0, now_ms - baseline)
        return checked.confidence * 2.0 ** (-age_ms / half_life_ms)
