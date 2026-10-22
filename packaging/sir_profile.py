"""Sir's persistent, provenance-bearing profile (not yet wired into think).

A (category, key) identifies one current assertion. Values are opaque text:
unequal values on the same key are conservatively treated as conflicts, not
semantically adjudicated by string heuristics or a hidden model call.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import stat
import uuid
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import product_home

if TYPE_CHECKING:
    from knowledge_graph import KnowledgeGraph

logger = logging.getLogger(__name__)

CATEGORIES = frozenset(
    {
        "identity",
        "family",
        "preference",
        "habit",
        "work",
        "health",
        "location",
        "event",
    }
)
SOURCES = frozenset({"self_report", "observed", "inferred"})
Record = dict[str, Any]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def validate_confidence(value: float) -> float:
    if type(value) not in (int, float):
        raise ValueError("Confidence must be a finite number in 0..1")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Confidence must be a finite number in 0..1")
    return float(value)


def require_text(value: str, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value


def validate_topic(category: str, key: str) -> None:
    if category not in CATEGORIES:
        raise ValueError("Unknown profile category")
    require_text(key, "Key")


def validate_evidence_ids(values: Sequence[str] | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError("Evidence IDs must be a list or tuple of nonempty strings")
    return list(dict.fromkeys(require_text(item, "Evidence ID") for item in values))


def decode_record(row: sqlite3.Row, arrays: tuple[str, ...] = ("evidence_ids",)) -> Record:
    result = dict(row)
    for key in arrays:
        result[key] = json.loads(result[key])
    return result


class PrivateStore:
    """Small sqlite3 wrapper, reusing product data-root safety checks.

    No persistent connections; writes lock the read/decide/write sequence.
    Existing unsafe files are rejected rather than chmod'd behind the owner.
    """

    def __init__(self, data_root: Path, filename: str) -> None:
        self.root = product_home.validate_root(Path(data_root))
        product_home.make_private(self.root)
        self.path = self.root / filename
        self.db_path = self.path
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = product_home.safe_absolute(Path(str(self.path) + suffix))
            if path.exists():
                info = path.stat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise ValueError("Database must be a private owned regular file (0600)")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)

    @contextmanager
    def connection(
        self, *, write: bool = False, profile_path: Path | None = None
    ) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if profile_path is not None:
                db.execute("ATTACH DATABASE ? AS profile", (str(profile_path),))
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


class SirProfile(PrivateStore):
    """One canonical assertion per topic; self-report outranks inference."""

    def __init__(self, data_root: Path, knowledge_graph: KnowledgeGraph | None = None) -> None:
        super().__init__(data_root, "sir_profile.db")
        self.kg = knowledge_graph
        with self.connection(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY, category TEXT NOT NULL, key TEXT NOT NULL,
                value TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                source TEXT NOT NULL CHECK(source IN ('self_report','observed','inferred')),
                evidence_ids TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, notes TEXT NOT NULL,
                UNIQUE(category,key))""")
            db.execute("CREATE INDEX IF NOT EXISTS facts_confidence ON facts(confidence)")
            db.execute("""CREATE TABLE IF NOT EXISTS fact_conflicts (
                id TEXT PRIMARY KEY, fact_id TEXT NOT NULL REFERENCES facts(id),
                category TEXT NOT NULL, key TEXT NOT NULL, existing_value TEXT NOT NULL,
                proposed_value TEXT NOT NULL, confidence REAL NOT NULL,
                source TEXT NOT NULL, evidence_ids TEXT NOT NULL, notes TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(fact_id,proposed_value,source,evidence_ids,notes))""")

    def self_evaluate(self) -> dict[str, Any]:
        """Describe implemented behavior and gaps; never inspect runtime records."""
        return {
            "module": "sir_profile",
            "domain": "personal profile provenance canonical assertions conflict resolution",
            "current_version": "SQLite canonical assertion per category/key with source priority, evidence-ID deduplication, conflict records and optional graph projection",
            "known_limitations": [
                "Values are opaque text; unequal strings are treated as potential conflicts without semantic adjudication.",
                "Evidence IDs are caller supplied; ordinary upsert does not resolve their contents or verify the assertion.",
                "Graph projection is best-effort after commit and this module does not itself install automatic extraction or think-loop consumption.",
            ],
            "improvement_directions": [
                "Evaluate semantic conflict candidates with explicit provenance and self-report priority preserved.",
                "Explore evidence-reference validation at ingestion boundaries without exposing private records in metadata.",
                "Test and design reconciliation for failed post-commit graph projections.",
            ],
            "last_evaluated_at": datetime.now(UTC).isoformat(),
        }

    def upsert(
        self,
        category: str,
        key: str,
        value: str,
        confidence: float,
        source: str,
        evidence_ids: Sequence[str] | None = None,
        notes: str = "",
    ) -> Record:
        validate_topic(category, key)
        require_text(value, "Value")
        validate_confidence(confidence)
        if source not in SOURCES or type(notes) is not str:
            raise ValueError("Invalid source or notes")
        refs = validate_evidence_ids(evidence_ids)
        with self.connection(write=True) as db:
            result = self.upsert_in_transaction(
                db, category, key, value, confidence, source, refs, notes
            )
        self.sync_committed_fact(result)
        return result

    def sync_committed_fact(self, fact: Record) -> None:
        """Mirror only committed canonical data; graph failure is non-fatal."""
        if self.kg is None:
            return
        try:
            self.kg.add_node(
                "fact",
                fact["category"],
                fact["key"],
                fact["value"],
                fact["confidence"],
                fact["source"],
                fact["evidence_ids"],
                origin_table="sir_profile",
                origin_id=fact["id"],
                metadata={
                    "original_source": fact["source"],
                    "notes": fact["notes"],
                    "source_db": str(self.path),
                },
            )
        except Exception as exc:
            logger.warning("Knowledge graph fact write failed: %s", type(exc).__name__)

    def upsert_in_transaction(
        self,
        db: sqlite3.Connection,
        category: str,
        key: str,
        value: str,
        confidence: float,
        source: str,
        evidence_ids: list[str],
        notes: str,
        *,
        schema: str = "main",
    ) -> Record:
        if schema not in {"main", "profile"}:
            raise ValueError("Invalid schema")
        table = f"{schema}.facts"
        old = db.execute(
            f"SELECT * FROM {table} WHERE category=? AND key=?", (category, key)
        ).fetchone()
        now = utc_now()
        priority = {"inferred": 0, "observed": 1, "self_report": 2}
        if old:
            different = old["value"] != value
            lower_priority = priority[source] < priority[old["source"]]
            if different and (old["source"] == "self_report" or lower_priority):
                encoded = json.dumps(evidence_ids, ensure_ascii=False)
                db.execute(
                    f"""INSERT OR IGNORE INTO {schema}.fact_conflicts
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        uuid.uuid4().hex,
                        old["id"],
                        category,
                        key,
                        old["value"],
                        value,
                        confidence,
                        source,
                        encoded,
                        notes,
                        now,
                    ),
                )
                conflict = db.execute(
                    f"""SELECT id FROM {schema}.fact_conflicts
                    WHERE fact_id=? AND proposed_value=? AND source=?
                    AND evidence_ids=? AND notes=?""",
                    (old["id"], value, source, encoded, notes),
                ).fetchone()
                return {**decode_record(old), "applied": False, "conflict_id": conflict["id"]}
            if not different:
                evidence_ids = validate_evidence_ids(json.loads(old["evidence_ids"]) + evidence_ids)
                if lower_priority:
                    confidence, source, notes = old["confidence"], old["source"], old["notes"]
                elif source == old["source"]:
                    confidence = max(confidence, old["confidence"])
                    notes = notes or old["notes"]
        identity = old["id"] if old else uuid.uuid4().hex
        created = old["created_at"] if old else now
        db.execute(
            f"""INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(category,key) DO UPDATE SET value=excluded.value,
            confidence=excluded.confidence, source=excluded.source,
            evidence_ids=excluded.evidence_ids, updated_at=excluded.updated_at,
            notes=excluded.notes""",
            (
                identity,
                category,
                key,
                value,
                confidence,
                source,
                json.dumps(evidence_ids, ensure_ascii=False),
                created,
                now,
                notes,
            ),
        )
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
        return {**decode_record(row), "applied": True, "conflict_id": None}

    def get(self, category: str | None = None, key: str | None = None) -> list[Record]:
        clauses: list[str] = []
        args: list[str] = []
        for field, value in (("category", category), ("key", key)):
            if value is not None:
                clauses.append(f"{field}=?")
                args.append(value)
        sql = "SELECT * FROM facts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self.connection() as db:
            return [decode_record(row) for row in db.execute(sql + " ORDER BY category,key", args)]

    def migrate_from_beliefs(self, mind_db_path: Path) -> dict[str, int]:
        """Import latest Sir beliefs only, without writing the source ledger.

        Known legacy attribute labels map to topic keys; unknown labels remain
        literal keys in category event, with provenance in notes. Never invent
        entry evidence: dangling/non-entry refs are retained only in notes.
        """
        source_path = product_home.safe_absolute(Path(mind_db_path))
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        mappings = {
            "姓名": ("identity", "name"),
            "年龄": ("identity", "age"),
            "星座": ("identity", "zodiac"),
            "性格": ("identity", "personality"),
            "随身设备": ("identity", "device"),
            "手机号": ("identity", "phone"),
            "系统账户名": ("identity", "account"),
            "与JARVIS关系": ("identity", "relationship"),
            "居住地": ("location", "residence"),
            "时区": ("location", "timezone"),
            "母亲": ("family", "mother"),
            "父亲": ("family", "father"),
            "当前状态": ("work", "current_status"),
            "学历": ("work", "education"),
            "职业经历": ("work", "career"),
            "备考计划B": ("work", "plan_b"),
            "语言习惯": ("habit", "language"),
            "花钱观": ("preference", "spending"),
            "睡眠习惯": ("habit", "sleep_pattern"),
            "作息": ("habit", "sleep_pattern"),
        }
        prepared: list[tuple[str, str, str, float, str, list[str], str]] = []
        skipped = 0
        source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=10)
        source.row_factory = sqlite3.Row
        try:
            source.execute("PRAGMA query_only=ON")
            source.execute("BEGIN")
            rows = source.execute("""SELECT b.* FROM belief_versions b WHERE NOT EXISTS (
                SELECT 1 FROM belief_versions newer WHERE newer.belief_id=b.belief_id
                AND newer.seq>b.seq) ORDER BY b.seq""")
            for row in rows:
                if row["subject"].casefold() != "sir":
                    skipped += 1
                    continue
                attribute = row["attribute"]
                if attribute in mappings:
                    category, key = mappings[attribute]
                else:
                    category, key = "event", attribute
                validate_topic(category, key)
                value = require_text(row["value"], "Belief value")
                confidence = validate_confidence(row["confidence"])
                if row["source"] not in SOURCES:
                    raise ValueError("Invalid legacy belief source")
                refs = validate_evidence_ids(json.loads(row["refs"]))
                evidence = [
                    ref
                    for ref in refs
                    if source.execute("SELECT 1 FROM entries WHERE id=?", (ref,)).fetchone()
                ]
                metadata = {
                    "migration": "mind.belief_versions",
                    "mind_db": str(source_path),
                    "belief_id": row["belief_id"],
                    "revision": row["revision"],
                    "subject": row["subject"],
                    "attribute": attribute,
                    "obtained_at": row["obtained_at"],
                    "last_verified": row["last_verified"],
                    "memory_id": row["memory_id"],
                    "original_refs": refs,
                    "unresolved_refs": [ref for ref in refs if ref not in evidence],
                }
                prepared.append(
                    (
                        category,
                        key,
                        value,
                        confidence,
                        row["source"],
                        evidence,
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    )
                )
        finally:
            source.close()
        counts = {"migrated": 0, "unchanged": 0, "conflicts": 0, "skipped": skipped}
        committed: list[Record] = []
        with self.connection(write=True) as db:
            for category, key, value, confidence, origin, refs, notes in prepared:
                old = db.execute(
                    "SELECT * FROM facts WHERE category=? AND key=?", (category, key)
                ).fetchone()
                if (
                    old
                    and old["value"] == value
                    and old["source"] == origin
                    and old["confidence"] == confidence
                    and old["notes"] == notes
                    and json.loads(old["evidence_ids"]) == refs
                ):
                    counts["unchanged"] += 1
                    committed.append(decode_record(old))
                    continue
                result = self.upsert_in_transaction(
                    db, category, key, value, confidence, origin, refs, notes
                )
                counts["migrated" if result["applied"] else "conflicts"] += 1
                committed.append(result)
        for fact in committed:
            self.sync_committed_fact(fact)
        return counts

    def get_conflicts(self, category: str | None = None, key: str | None = None) -> list[Record]:
        clauses: list[str] = []
        args: list[str] = []
        for field, value in (("category", category), ("key", key)):
            if value is not None:
                clauses.append(f"{field}=?")
                args.append(value)
        sql = "SELECT * FROM fact_conflicts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self.connection() as db:
            return [decode_record(row) for row in db.execute(sql + " ORDER BY created_at,id", args)]

    def get_high_confidence(self, threshold: float = 0.8) -> list[Record]:
        threshold = validate_confidence(threshold)
        with self.connection() as db:
            return [
                decode_record(row)
                for row in db.execute(
                    """SELECT * FROM facts WHERE confidence>=? ORDER BY 
                    CASE source WHEN 'self_report' THEN 2 WHEN 'observed' THEN 1 ELSE 0 END DESC,
                    confidence DESC, category, key""",
                    (threshold,),
                )
            ]
