"""K2-008 Deletion & Retention DRAFT; NOT_FROZEN / NOT_APPROVED.

runtime_enabled=False. Detached, single-process fixture only; no packaging/,
SQLite, FTS, embeddings, summaries, backups or product runtime are modified.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import cast

from jarvis.memory_schema import MemoryEntry, MemoryType

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False
DAY_MS = 86_400_000


def _number(name: str, value: float, *, positive: bool = False) -> None:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")


def _now() -> int:
    return time.time_ns() // 1_000_000


class RetentionDecision(StrEnum):
    """DELETE means logically unavailable, not permission to bypass the grace period."""

    KEEP = "KEEP"
    DECAY = "DECAY"
    DELETE = "DELETE"


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Offline candidate defaults, not an approved privacy/retention contract.

    Missing max-age types have no age limit. Age is since creation; decay uses
    idle time since last access. ``half_life_days`` is the requested exponential
    time constant: at that many days the factor is exp(-1), NOT 1/2. This
    intentionally differs from MemorySchema's floored base-2 ranking score.
    Grace counts from deleted_at; it is independent of age and access time.
    """

    max_age_days: Mapping[MemoryType, float] = field(default_factory=lambda: {})
    min_importance_threshold: float = 1.0
    auto_delete_below_threshold: bool = False
    user_deletable_types: frozenset[MemoryType] | set[MemoryType] = field(
        default_factory=lambda: frozenset(MemoryType)
    )
    half_life_days: float = 30.0
    grace_period_days: float = 7.0

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.max_age_days), Mapping):
            raise TypeError("max_age_days must be a mapping")
        limits = dict(self.max_age_days)
        for kind, days in limits.items():
            if not isinstance(cast(object, kind), MemoryType):
                raise TypeError("max_age_days keys must be MemoryType")
            _number("max_age_days", days)
        _number("min_importance_threshold", self.min_importance_threshold)
        if self.min_importance_threshold > 5:
            raise ValueError("min_importance_threshold must be in [0, 5]")
        if type(self.auto_delete_below_threshold) is not bool:
            raise TypeError("auto_delete_below_threshold must be a bool")
        if type(self.user_deletable_types) not in (set, frozenset) or any(
            not isinstance(cast(object, kind), MemoryType) for kind in self.user_deletable_types
        ):
            raise TypeError("user_deletable_types must be a set of MemoryType")
        _number("half_life_days", self.half_life_days, positive=True)
        _number("grace_period_days", self.grace_period_days)
        object.__setattr__(self, "max_age_days", MappingProxyType(limits))
        object.__setattr__(self, "user_deletable_types", frozenset(self.user_deletable_types))


class RetentionManager:
    """Offline draft manager; fixture owners must serialize calls.

    Owns snapshots, deleted_at sidecar and ID-only purge markers because the
    immutable MemoryEntry schema has no deleted_at field. register()/entries
    explicitly populate this detached store; evaluate() is read-only. Lost or
    restarted managers lose these marks: no durable deletion is claimed.
    """

    runtime_enabled = RUNTIME_ENABLED

    def __init__(
        self,
        policy: RetentionPolicy,
        *,
        entries: Iterable[MemoryEntry] = (),
        clock: Callable[[], int] = _now,
    ) -> None:
        if not isinstance(cast(object, policy), RetentionPolicy):
            raise TypeError("policy must be RetentionPolicy")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.policy = policy
        self._clock = clock
        self._entries: dict[str, MemoryEntry] = {}
        self._deleted_at: dict[str, int] = {}
        self._purged: set[str] = set()
        self._entries.update(self._batch(entries))

    @property
    def deleted_at(self) -> Mapping[str, int]:
        """Read-only snapshot of soft-deletion UTC epoch millisecond timestamps."""
        return MappingProxyType(dict(self._deleted_at))

    def _batch(self, entries: Iterable[MemoryEntry]) -> dict[str, MemoryEntry]:
        batch: dict[str, MemoryEntry] = {}
        for entry in entries:
            checked = self._checked(entry)
            if checked.id in batch:
                raise ValueError("duplicate memory id in batch")
            previous = self._entries.get(checked.id)
            if previous is not None and previous != checked:
                raise ValueError("conflicting memory id; silent overwrite is forbidden")
            batch[checked.id] = checked
        return batch

    @staticmethod
    def _checked(entry: MemoryEntry) -> MemoryEntry:
        if not isinstance(cast(object, entry), MemoryEntry):
            raise TypeError("entry must be MemoryEntry")
        return replace(entry)

    def register(self, entry: MemoryEntry) -> None:
        """Register an immutable snapshot without silently replacing an existing ID."""
        checked = self._checked(entry)
        if checked.id in self._purged:
            raise ValueError("memory id was purged")
        self._entries.update(self._batch([checked]))

    def get(self, memory_id: str, *, include_deleted: bool = False) -> MemoryEntry | None:
        """Read detached data; logically deleted and sub-threshold data are unavailable.

        include_deleted is an offline inspection/restore seam, not authorization.
        No actual retrieval engine, user identity or consent is integrated here.
        """
        entry = self._entries.get(memory_id)
        if entry is None:
            return None
        now_ms = self._instant()
        if not include_deleted and (
            self._evaluate(entry, now_ms) is RetentionDecision.DELETE
            or self._score(entry, now_ms) < self.policy.min_importance_threshold
        ):
            return None
        return replace(entry)

    def mark_deleted(self, memory_id: str) -> bool:
        """User-requested soft deletion. Repeats never reset deleted_at/grace."""
        entry = self._entries[memory_id]
        if entry.type not in self.policy.user_deletable_types:
            raise PermissionError("memory type is not user-deletable under this policy")
        if memory_id in self._deleted_at:
            return False
        self._deleted_at[memory_id] = self._instant()
        return True

    def restore(self, memory_id: str) -> bool:
        """Remove a soft-delete mark within grace; age/score policy still applies."""
        if memory_id not in self._entries:
            raise KeyError(memory_id)
        deleted_at = self._deleted_at.get(memory_id)
        if deleted_at is None:
            return False
        if self._past_grace(deleted_at, self._instant()):
            raise ValueError("restoration grace period has expired")
        del self._deleted_at[memory_id]
        return True

    def _past_grace(self, deleted_at: int, now_ms: int) -> bool:
        return max(0, now_ms - deleted_at) / DAY_MS > self.policy.grace_period_days

    def enforce(self, entries: Iterable[MemoryEntry]) -> list[MemoryEntry]:
        """Apply policy and return only entries hard-purged after grace, once.

        Atomically validate/import the complete batch before mutation. Existing
        owned entries are also scanned, even when omitted from a later batch.
        DELETE first sets deleted_at. Only a later call beyond (strictly >)
        grace drops content from this manager and returns it for a caller-owned
        downstream deletion. ID-only markers prevent stale batch resurrection.
        This does NOT delete caller lists, SQL/FTS rows, backups or disk bytes.
        """
        batch = self._batch(entries)
        now_ms = self._instant()
        self._entries.update(
            {key: value for key, value in batch.items() if key not in self._purged}
        )
        purged: list[MemoryEntry] = []
        for entry in tuple(self._entries.values()):
            if self._evaluate(entry, now_ms) is not RetentionDecision.DELETE:
                continue
            deleted_at = self._deleted_at.get(entry.id)
            if deleted_at is None:
                self._deleted_at[entry.id] = now_ms
            elif self._past_grace(deleted_at, now_ms):
                purged.append(entry)
                del self._entries[entry.id]
                del self._deleted_at[entry.id]
                self._purged.add(entry.id)
        return purged

    def _instant(self) -> int:
        value = self._clock()
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise ValueError("clock must return nonnegative UTC epoch milliseconds")
        return value

    def decayed_importance(self, entry: MemoryEntry) -> float:
        """Pure importance * exp(-idle_days/half_life_days), with no floor.

        Never writes the schema's integer importance or refreshes access time.
        A backward clock contributes zero idle time.
        """
        return self._score(self._checked(entry), self._instant())

    def _score(self, entry: MemoryEntry, now_ms: int) -> float:
        days = max(0, now_ms - entry.last_accessed_at) / DAY_MS
        return entry.importance * math.exp(-days / self.policy.half_life_days)

    def evaluate(self, entry: MemoryEntry) -> RetentionDecision:
        """Classify without changing entries, policy or stored importance."""
        return self._evaluate(self._checked(entry), self._instant())

    def _evaluate(self, entry: MemoryEntry, now_ms: int) -> RetentionDecision:
        if entry.id in self._deleted_at or entry.id in self._purged:
            return RetentionDecision.DELETE
        max_age = self.policy.max_age_days.get(entry.type)
        if max_age is not None and max(0, now_ms - entry.created_at) / DAY_MS >= max_age:
            return RetentionDecision.DELETE
        score = self._score(entry, now_ms)
        if score < self.policy.min_importance_threshold:
            if self.policy.auto_delete_below_threshold:
                return RetentionDecision.DELETE
            return RetentionDecision.DECAY
        return RetentionDecision.DECAY if score < entry.importance else RetentionDecision.KEEP
