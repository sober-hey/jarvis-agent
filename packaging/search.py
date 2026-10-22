"""Bounded, failure-tolerant internet search for JARVIS."""

from datetime import datetime, timezone
from queue import Empty, Queue
from threading import Thread
from typing import Any

_TIMEOUT = 15


def self_evaluate() -> dict[str, Any]:
    """Describe implemented behavior and gaps without reading private state."""
    return {
        "module": "search",
        "domain": "web search retrieval provider resilience evidence discovery",
        "current_version": "DDGS normalized results with two daemon-thread attempts and a 15-second deadline per attempt",
        "known_limitations": [
            "DDGS is the only provider adapter; exceptions and empty results both become an empty list after at most two attempts.",
            "Timed-out daemon threads cannot be cancelled and may continue backend I/O after the caller deadline.",
            "Returned snippets are not fetched or verified; citation persistence is the caller responsibility.",
        ],
        "improvement_directions": [
            "Expose structured empty, failure and timeout outcomes without leaking provider diagnostics.",
            "Use cancellable bounded workers to avoid accumulating timed-out backend I/O.",
            "Evaluate provider diversity and source verification while retaining caller-owned citations.",
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def _attempt(query: str, max_results: int, output: Queue) -> None:
    try:
        from ddgs import DDGS

        rows = DDGS(timeout=_TIMEOUT).text(query, max_results=max_results)
        normalized = [
            {
                "title": str(row.get("title") or ""),
                "url": str(row.get("href") or row.get("url") or ""),
                "snippet": str(row.get("body") or row.get("snippet") or ""),
            }
            for row in rows[:max_results]
            if isinstance(row, dict)
        ]
        output.put(normalized)
    except Exception:
        output.put(None)


def search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """Search with a 15s deadline per attempt and at most one retry.

    DDGS also receives its HTTP timeout. A daemon worker enforces the caller's
    deadline even when a backend fans out to several engines. Timed-out I/O
    cannot be forcibly cancelled, but its worker cannot block process shutdown.
    """
    if not isinstance(query, str) or not query.strip():
        return []
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results <= 0:
        return []
    for _ in range(2):
        output: Queue = Queue(maxsize=1)
        try:
            Thread(target=_attempt, args=(query, max_results, output), daemon=True).start()
            result = output.get(timeout=_TIMEOUT)
            if result:
                return result
        except (Empty, RuntimeError):
            pass
    return []
