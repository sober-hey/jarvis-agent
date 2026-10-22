"""K3-004 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

Bounded content-based suppression, not exactly-once side-effect execution. No
EventStore migration, event processing, packaging/ change or runtime wiring.
Optional SQLite uses an explicit standalone fixture and caller-owned connection.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import ClassVar, cast

from jarvis.event_schema import Event

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


@dataclass(frozen=True, slots=True)
class DeduplicationWindow:
    """Candidate suppression duration in seconds and bounded LRU capacity."""

    window_seconds: float = 300
    max_cache_size: int = 10000

    def __post_init__(self) -> None:
        if type(self.window_seconds) not in (int, float):
            raise TypeError("window_seconds must be a positive finite number")
        if not math.isfinite(self.window_seconds) or self.window_seconds <= 0:
            raise ValueError("window_seconds must be a positive finite number")
        if type(self.max_cache_size) is not int:
            raise TypeError("max_cache_size must be a positive integer")
        if not 0 < self.max_cache_size < 2**63:
            raise ValueError("max_cache_size must be a positive SQLite integer")


class EventDeduplicationError(RuntimeError):
    """Unsupported fixture, caller transaction or persisted candidate layout."""


class EventDeduplicator:
    """Explicit registration records processed events; checks never register misses.

    Identity is SHA-256 of canonical JSON [type, source, payload], excluding IDs,
    timestamps and routing metadata. Array framing avoids ambiguous concatenation.
    TTL is half-open [registered_at, registered_at + window_seconds), measured in
    processing-time UTC seconds, NOT Event.created_at (UTC milliseconds). Hits and
    repeated registration touch LRU order without extending TTL. Capacity eviction
    can accept a replay earlier than TTL; distinct real events with identical
    content can be suppressed. Neither is an exactly-once processing guarantee.

    SQLite persists only hashes/times/order, in a separate empty/recognized DB;
    never pass a product, EventStore or K0 connection. Methods own transactions;
    do not use the connection concurrently through other objects. Busy/commit
    errors propagate without hidden retries. Deduplicate atomically selects and
    registers a batch, but does NOT process it. Call register after successful
    handling for explicit acknowledgment; no transaction couples this fixture to
    external effects. One connection per thread/process, or check_same_thread=False
    for a shared instance. A separate check then register is not an atomic claim.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(
        self,
        window: DeduplicationWindow | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        draft_fixture: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if window is not None and not isinstance(cast(object, window), DeduplicationWindow):
            raise TypeError("window must be DeduplicationWindow")
        self._window = window if window is not None else DeduplicationWindow()
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self._last_now: float | None = None
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._connection = connection
        self._lock = threading.RLock()
        if connection is not None:
            if draft_fixture is not True:
                raise EventDeduplicationError("explicit offline draft_fixture=True required")
            self._validate_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                objects = self._objects()
                if not objects:
                    for statement in self._statements():
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO main.event_dedup_config VALUES (1, ?, ?)",
                        (self.window.window_seconds, self.window.max_cache_size),
                    )
                self._validate_schema()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @property
    def window(self) -> DeduplicationWindow:
        return self._window

    def _now(self) -> float:
        value = self._clock()
        if type(value) not in (int, float):
            raise TypeError("clock must return nonnegative finite UTC seconds")
        if not math.isfinite(value) or value < 0:
            raise ValueError("clock must return nonnegative finite UTC seconds")
        now = float(value)
        if self._last_now is not None and now < self._last_now:
            raise ValueError("processing clock moved backward")
        return now

    @staticmethod
    def _statements() -> tuple[str, ...]:
        return (
            """CREATE TABLE event_dedup (
                dedup_key TEXT PRIMARY KEY NOT NULL CHECK (
                    length(dedup_key) = 64 AND dedup_key NOT GLOB '*[^0-9a-f]*'),
                registered_at REAL NOT NULL CHECK (registered_at >= 0),
                lru_order INTEGER NOT NULL UNIQUE CHECK (lru_order >= 0)
            ) STRICT""",
            """CREATE TABLE event_dedup_config (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                window_seconds REAL NOT NULL CHECK (window_seconds > 0),
                max_cache_size INTEGER NOT NULL CHECK (max_cache_size > 0)
            ) STRICT""",
        )

    def _objects(self) -> set[str]:
        assert self._connection is not None
        return {
            row[0]
            for row in self._connection.execute(
                "SELECT sql FROM main.sqlite_master WHERE sql IS NOT NULL"
            )
        }

    def _validate_connection(self) -> None:
        connection = self._connection
        assert connection is not None
        if (
            connection.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL
            or connection.row_factory is not None
            or connection.text_factory is not str
        ):
            raise EventDeduplicationError("requires legacy transactions, tuple rows and str text")
        if connection.in_transaction:
            raise EventDeduplicationError("caller transaction is not allowed")
        if any(
            row[1] not in {"main", "temp"} for row in connection.execute("PRAGMA database_list")
        ):
            raise EventDeduplicationError("requires standalone database without attachments")
        if connection.execute("SELECT 1 FROM sqlite_temp_master LIMIT 1").fetchone():
            raise EventDeduplicationError("temporary objects may not shadow dedup tables")

    def _validate_schema(self) -> None:
        assert self._connection is not None
        if self._objects() != set(self._statements()):
            raise EventDeduplicationError("requires empty or recognized standalone dedup draft DB")
        if self._connection.execute("SELECT * FROM main.event_dedup_config").fetchall() != [
            (1, self.window.window_seconds, self.window.max_cache_size)
        ]:
            raise EventDeduplicationError("persisted deduplication window does not match")

    @staticmethod
    def _key(event: Event) -> str:
        if not isinstance(cast(object, event), Event):
            raise TypeError("expected Event")
        checked = replace(event)  # revalidate mutable JSON payload before any writes
        canonical = json.dumps(
            [checked.type.value, checked.source, checked.payload],
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @contextmanager
    def _state(self) -> Generator[tuple[OrderedDict[str, float], float]]:
        """Copy-on-write state; SQLite serializes the whole read/check/write slice."""
        with self._lock:
            connection = self._connection
            if connection is not None:
                self._validate_connection()
                connection.execute("BEGIN IMMEDIATE")
            try:
                if connection is not None:
                    self._validate_schema()
                cache = (
                    OrderedDict(
                        connection.execute(
                            "SELECT dedup_key, registered_at FROM main.event_dedup "
                            "ORDER BY lru_order"
                        ).fetchall()
                    )
                    if connection is not None
                    else self._cache.copy()
                )
                original = list(cache.items())
                now = self._now()
                if any(registered_at > now for registered_at in cache.values()):
                    raise ValueError(
                        "processing clock moved backward behind persisted registration"
                    )
                for key, registered_at in list(cache.items()):
                    if now - registered_at >= self.window.window_seconds:
                        del cache[key]
                yield cache, now
                if connection is not None:
                    if list(cache.items()) != original:
                        connection.execute("DELETE FROM main.event_dedup")
                        connection.executemany(
                            "INSERT INTO main.event_dedup VALUES (?, ?, ?)",
                            [(key, stamp, rank) for rank, (key, stamp) in enumerate(cache.items())],
                        )
                    connection.commit()
                self._cache = cache
                self._last_now = now
            except BaseException:
                if connection is not None:
                    connection.rollback()
                raise

    def _register(self, cache: OrderedDict[str, float], key: str, now: float) -> None:
        if key not in cache:
            cache[key] = now
        cache.move_to_end(key)
        while len(cache) > self.window.max_cache_size:
            cache.popitem(last=False)

    def is_duplicate(self, event: Event) -> bool:
        """Check without registering misses; a hit touches LRU but not TTL."""
        key = self._key(event)
        with self._state() as (cache, _now):
            if key not in cache:
                return False
            cache.move_to_end(key)
            return True

    def register(self, event: Event) -> None:
        """Record a handled event; repeated registration does not extend TTL."""
        key = self._key(event)
        with self._state() as (cache, now):
            self._register(cache, key, now)

    def deduplicate(self, events: list[Event]) -> list[Event]:
        """Atomically select/register first occurrences in input order; no processing.

        A batch-local seen set prevents duplicate output even when its distinct
        contents exceed LRU capacity; only max_cache_size keys remain afterward.
        """
        keyed = [(event, self._key(event)) for event in events]
        accepted: list[Event] = []
        seen: set[str] = set()
        with self._state() as (cache, now):
            for event, key in keyed:
                if key in seen:
                    if key in cache:
                        cache.move_to_end(key)
                    continue
                seen.add(key)
                if key not in cache:
                    self._register(cache, key, now)
                    accepted.append(event)
                else:
                    cache.move_to_end(key)
        return accepted
