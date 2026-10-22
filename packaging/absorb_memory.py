"""Explicit publication of archive evidence into product-owned long-term memory.

Absorption remains independent by default. Only current factual evidence is
published; lessons, persona proposals, suspicious text and digestion stay local.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from . import absorb
    from .product_memory import MemoryStore, bounded, normalized
except ImportError:
    import absorb
    from product_memory import MemoryStore, bounded, normalized

FACT_TYPES = {"fact", "preference", "relationship", "habit", "event"}


def self_evaluate() -> dict[str, Any]:
    """Describe implemented behavior and gaps; never inspect runtime records."""
    return {
        "module": "absorb_memory",
        "domain": "archive factual memory publication tombstone reconciliation",
        "current_version": "Explicit archive-to-memory reconciliation with provenance, current-fact filtering and tombstone-aware SQLite transactions",
        "known_limitations": [
            "Identity and conflict handling use archive decisions and normalized text, not semantic equivalence checking.",
            "Only fact, preference, relationship, habit and event entries are published; lessons and persona proposals remain local.",
            "Reconciliation materializes exported beliefs before one write transaction; it has no incremental publication cursor.",
        ],
        "improvement_directions": [
            "Measure large-archive reconciliation cost and explore bounded preparation without weakening transaction guarantees.",
            "Expand synthetic tests for retirement, source provenance and user-deletion replay protection.",
            "Study semantic duplicate candidates without taking ownership of unrelated direct-user facts.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


def publish(db_path: Path, memory_path: Path) -> dict[str, int]:
    """Reconcile one authoritative archive, atomically and without resurrection."""
    namespace = "archive:" + hashlib.sha256(str(db_path.absolute()).encode()).hexdigest()
    rows = absorb.export_beliefs(db_path)
    prepared: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        if row["type"] not in FACT_TYPES:
            continue
        try:
            date = f" @ {row['claimed_time']}" if row["type"] == "event" else ""
            content = bounded(f"{row['subject']} {row['attribute']}{date}：{row['value']}")
        except ValueError:
            skipped += 1
            continue
        source = json.dumps(
            {
                "archive_entry_id": row["id"],
                "source_kind": row["source_kind"],
                "claimed_time": row["claimed_time"],
                "confidence": row["confidence"],
                "conflict": bool(row["conflict"]),
                "reason": row["reason"],
                "sources": row["sources"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        prepared.append({**row, "content": content, "provenance": source})
    store = MemoryStore(memory_path)
    added = retired = 0
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS archive_facts ("
            "namespace TEXT NOT NULL, entry_id INTEGER NOT NULL, "
            "fact_id TEXT NOT NULL REFERENCES facts(id), "
            "PRIMARY KEY(namespace,entry_id))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS archive_retirements ("
            "fact_id TEXT PRIMARY KEY REFERENCES facts(id))"
        )
        # Inactivate before insertion so exact-normalized identities remain unique.
        for row in prepared:
            if row["current"]:
                continue
            key = normalized(row["content"])
            if row["status"] == "superseded":
                db.execute("INSERT OR IGNORE INTO tombstones VALUES (?)", (key,))
            else:
                db.execute(
                    "INSERT OR IGNORE INTO archive_retirements SELECT f.id FROM facts f "
                    "JOIN archive_facts a ON a.fact_id=f.id WHERE a.namespace=? "
                    "AND a.entry_id=? AND f.status='active'",
                    (namespace, row["id"]),
                )
            cursor = db.execute(
                "UPDATE facts SET status=? WHERE status='active' AND id IN "
                "(SELECT fact_id FROM archive_facts WHERE namespace=? AND entry_id=?)",
                ("deleted" if row["status"] == "superseded" else "corrected", namespace, row["id"]),
            )
            retired += cursor.rowcount
        for row in prepared:
            if not row["current"]:
                continue
            mapped = db.execute(
                "SELECT f.* FROM facts f JOIN archive_facts a ON a.fact_id=f.id "
                "WHERE a.namespace=? AND a.entry_id=?",
                (namespace, row["id"]),
            ).fetchone()
            key = normalized(row["content"])
            tombstoned = db.execute(
                "SELECT 1 FROM tombstones WHERE normalized=?", (key,)
            ).fetchone()
            if mapped is not None:
                if mapped["status"] == "active":
                    db.execute(
                        "UPDATE facts SET source_message=? WHERE id=?",
                        (row["provenance"], mapped["id"]),
                    )
                    continue
                automatic = db.execute(
                    "SELECT 1 FROM archive_retirements WHERE fact_id=?", (mapped["id"],)
                ).fetchone()
                if not automatic or tombstoned:
                    continue  # User corrections/deletions always win over replay.
                replacement = store.insert(
                    db,
                    row["content"],
                    namespace,
                    row["provenance"],
                    mapped["version"] + 1,
                    mapped["id"],
                )
                db.execute(
                    "UPDATE archive_facts SET fact_id=? WHERE namespace=? AND entry_id=?",
                    (replacement["id"], namespace, row["id"]),
                )
                added += 1
                continue
            if tombstoned:
                continue
            if db.execute(
                "SELECT 1 FROM facts WHERE normalized=? AND status='active'", (key,)
            ).fetchone():
                continue  # Do not take ownership of an unrelated direct user fact.
            fact = store.insert(db, row["content"], namespace, row["provenance"], 1, None)
            db.execute(
                "INSERT INTO archive_facts VALUES (?,?,?)", (namespace, row["id"], fact["id"])
            )
            added += 1
        if retired:
            db.execute("UPDATE metadata SET value=value+1 WHERE key='revision'")
    return {"added": added, "retired": retired, "skipped": skipped}
