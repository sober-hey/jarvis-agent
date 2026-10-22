"""K2-006 retrieval DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

Offline MemorySchema v2 only. A private in-memory FTS5/JSON-vector sidecar leaves
its strict shared schema unchanged. BM25 is lexical relevance; semantic ranking
requires caller-supplied embeddings, never a model/network call in this module.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Sequence
from time import time_ns
from typing import Self, cast

from jarvis.memory_schema import MemoryEntry, MemorySchema, MemorySchemaError, MemoryType

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


def _limit(limit: int) -> None:
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a nonnegative integer")


def _text(name: str, value: str, *, blank: bool = False) -> None:
    if (
        not isinstance(cast(object, value), str)
        or "\x00" in value
        or (not blank and not value.strip())
    ):
        raise ValueError(f"{name} must be text without NUL" + ("" if blank else " and nonblank"))


def _vector(value: object) -> list[float]:
    if not isinstance(value, list) or not value:
        raise ValueError("embedding must be a nonempty list of finite numbers")
    result: list[float] = []
    for component in cast(list[object], value):
        if type(component) not in (int, float):
            raise ValueError("embedding components must be finite numbers, not bool")
        try:
            number = float(cast(float, component))
        except OverflowError as error:
            raise ValueError("embedding components must be finite") from error
        if not math.isfinite(number):
            raise ValueError("embedding components must be finite")
        result.append(number)
    if not any(result):
        raise ValueError("zero-norm embedding is not searchable")
    return result


def _unit(vector: list[float]) -> list[float]:
    # Scaling first avoids overflow/underflow for valid very large/small vectors.
    scale = max(abs(value) for value in vector)
    scaled = [value / scale for value in vector]
    norm = math.sqrt(math.fsum(value * value for value in scaled))
    return [value / norm for value in scaled]


class MemoryRetriever:
    """Offline retrieval candidate; no runtime wiring or durable index.

    Accepts the same standalone in-memory v2 connection as MemorySchema. The
    caller owns/serializes that connection. FTS is rebuilt from one fresh source
    snapshot per lexical/hybrid query; JSON embeddings live only in the private
    sidecar for this retriever's lifetime. No source DDL, implicit access updates,
    provider selection, trust promotion or execution of PROCEDURAL data occurs.
    """

    runtime_enabled = RUNTIME_ENABLED

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._schema = MemorySchema(connection, draft_fixture=True)
        self._schema.by_time_range(0, 0)  # Reject missing/v1/foreign schemas.
        self._index = sqlite3.connect(":memory:")
        self._closed = False
        try:
            self._index.execute(
                "CREATE VIRTUAL TABLE memory_fts USING fts5(memory_id UNINDEXED, content)"
            )
            self._index.execute(
                "CREATE TABLE memory_embeddings (memory_id TEXT PRIMARY KEY NOT NULL, "
                "content TEXT NOT NULL, embedding_id TEXT, vector_json TEXT NOT NULL "
                "CHECK(json_valid(vector_json) AND json_type(vector_json) = 'array'))"
            )
        except BaseException:
            self._index.close()
            raise

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("retriever is closed")

    def close(self) -> None:
        """Idempotently close only the private sidecar, not the supplied connection."""
        if not self._closed:
            self._index.close()
            self._closed = True

    def _snapshot(self) -> dict[str, MemoryEntry]:
        self._ensure_open()
        return {entry.id: entry for entry in self._schema.by_time_range(0, 2**63 - 1)}

    @staticmethod
    def _types(types: Sequence[MemoryType] | None) -> set[MemoryType] | None:
        if types is None:
            return None
        if isinstance(types, (str, bytes)) or not isinstance(cast(object, types), Sequence):
            raise ValueError("types must be a sequence of MemoryType values")
        if any(not isinstance(cast(object, kind), MemoryType) for kind in types):
            raise ValueError("types must contain MemoryType values")
        return set(types)

    def _fts_scores(self, query: str, entries: dict[str, MemoryEntry]) -> dict[str, float]:
        if not query.strip():
            return {}
        with self._index:
            self._index.execute("DELETE FROM memory_fts")
            self._index.executemany(
                "INSERT INTO memory_fts(memory_id, content) VALUES (?, ?)",
                ((entry.id, entry.content) for entry in entries.values()),
            )
        # Bound MATCH is not enough: quote terms to disallow FTS query operators.
        match = " AND ".join('"' + term.replace('"', '""') + '"' for term in query.split())
        return {
            row[0]: -row[1]
            for row in self._index.execute(
                "SELECT memory_id, bm25(memory_fts) FROM memory_fts WHERE memory_fts MATCH ?",
                (match,),
            )
        }

    @staticmethod
    def _rank(
        scores: dict[str, float], entries: dict[str, MemoryEntry], limit: int
    ) -> list[MemoryEntry]:
        return [entries[id] for id in sorted(scores, key=lambda id: (-scores[id], id))[:limit]]

    def search(
        self, query: str, types: Sequence[MemoryType] | None = None, limit: int = 10
    ) -> list[MemoryEntry]:
        """Literal whitespace-separated AND terms; descending BM25 relevance.

        Indexes content only across every MemoryType. Empty/blank queries return
        []. Terms are unicode61 tokens (case-insensitive), not substrings. No CJK
        word segmentation, stemming or synonym model is provided by this draft.
        Type filters precede the result limit; equal scores sort by memory ID.
        """
        self._ensure_open()
        _text("query", query, blank=True)
        _limit(limit)
        allowed = self._types(types)
        if not query.strip() or limit == 0 or allowed == set():
            return []
        entries = self._snapshot()
        scores = self._fts_scores(query, entries)
        return self._rank(
            {
                id: score
                for id, score in scores.items()
                if allowed is None or entries[id].type in allowed
            },
            entries,
            limit,
        )

    def set_embedding(self, memory_id: str, embedding: list[float]) -> None:
        """Store caller/mock vector as JSON in the private sidecar, not embedding_id.

        The shared schema has an opaque embedding_id reference, not a JSON vector
        column. This explicit draft loader binds a vector to current content and
        embedding_id; content/reference changes invalidate it until reloaded.
        Callers must supply one compatible embedding model/space for comparisons.
        """
        self._ensure_open()
        _text("memory_id", memory_id)
        vector = _vector(embedding)
        entry = self._snapshot().get(memory_id)
        if entry is None:
            raise KeyError(memory_id)
        with self._index:
            self._index.execute(
                "INSERT INTO memory_embeddings(memory_id, content, embedding_id, vector_json) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(memory_id) DO UPDATE SET "
                "content = excluded.content, embedding_id = excluded.embedding_id, "
                "vector_json = excluded.vector_json",
                (entry.id, entry.content, entry.embedding_id, json.dumps(vector, allow_nan=False)),
            )

    def _vector_scores(
        self, vector: list[float], entries: dict[str, MemoryEntry]
    ) -> dict[str, float]:
        unit = _unit(vector)
        scores: dict[str, float] = {}
        for id, content, embedding_id, raw in self._index.execute(
            "SELECT memory_id, content, embedding_id, vector_json FROM memory_embeddings"
        ):
            entry = entries.get(id)
            if entry is None or entry.content != content or entry.embedding_id != embedding_id:
                continue
            stored = _vector(json.loads(raw))
            if len(stored) != len(unit):
                continue  # Different-dimensional model; no partial dot product.
            cosine = math.fsum(a * b for a, b in zip(unit, _unit(stored), strict=True))
            scores[id] = max(-1.0, min(1.0, cosine))
        return scores

    def search_by_embedding(self, embedding: list[float], limit: int = 10) -> list[MemoryEntry]:
        """Exact O(N*d) cosine scan; skip missing/stale/incompatible-size vectors.

        Finite, nonzero vectors required. Negative similarities remain eligible;
        equal cosine scores sort by memory ID. No approximate/vector extension.
        """
        self._ensure_open()
        vector = _vector(embedding)
        _limit(limit)
        entries = self._snapshot()
        return self._rank(self._vector_scores(vector, entries), entries, limit)

    def hybrid_search(
        self, query: str, embedding: list[float], alpha: float = 0.5, *, limit: int = 10
    ) -> list[MemoryEntry]:
        """Rank union of all candidates before limiting, alpha weights lexical.

        Score = alpha * (-BM25 / max(-BM25)) + (1-alpha) * ((cosine+1)/2).
        Missing channel scores are 0; BM25's best hit is 1. No early per-channel
        truncation. alpha=1/0 returns only the lexical/vector channel, respectively,
        in exactly its pure ordering. This is a draft heuristic, not calibrated
        semantic quality evidence. Query/vector are validated even at endpoints.
        """
        self._ensure_open()
        _text("query", query, blank=True)
        vector = _vector(embedding)
        _limit(limit)
        if type(alpha) not in (float, int) or not 0 <= alpha <= 1 or not math.isfinite(alpha):
            raise ValueError("alpha must be finite and in [0, 1]")
        entries = self._snapshot()
        lexical = self._fts_scores(query, entries) if alpha > 0 else {}
        semantic = self._vector_scores(vector, entries) if alpha < 1 else {}
        if alpha == 1:
            return self._rank(lexical, entries, limit)
        if alpha == 0:
            return self._rank(semantic, entries, limit)
        maximum = max(lexical.values(), default=1.0)
        scores = {
            id: alpha * (lexical.get(id, 0.0) / maximum)
            + (1 - alpha) * ((semantic[id] + 1) / 2 if id in semantic else 0.0)
            for id in lexical.keys() | semantic.keys()
        }
        return self._rank(scores, entries, limit)

    def recall_by_time(
        self, start: int, end: int, types: Sequence[MemoryType] | None = None
    ) -> list[MemoryEntry]:
        """Inclusive created_at range in UTC epoch ms, ordered by created_at/id."""
        self._ensure_open()
        allowed = self._types(types)
        return [
            entry
            for entry in self._schema.by_time_range(start, end)
            if allowed is None or entry.type in allowed
        ]

    def update_access(self, memory_id: str) -> None:
        """Explicit atomic touch; missing IDs raise KeyError, clocks never regress.

        Owns the transaction and refuses caller transactions. A failed UPDATE or
        commit rolls back; no retrieval path touches access metadata implicitly.
        """
        self._ensure_open()
        _text("memory_id", memory_id)
        self._schema.by_time_range(0, 0)
        if self.connection.in_transaction:
            raise MemorySchemaError("caller transaction is not allowed")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            changed = self.connection.execute(
                "UPDATE memories SET last_accessed_at = max(last_accessed_at, ?), "
                "access_count = access_count + 1 WHERE id = ?",
                (time_ns() // 1_000_000, memory_id),
            ).rowcount
            if changed != 1:
                raise KeyError(memory_id)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
