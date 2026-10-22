"""Local, disposable lexical recall projection; never edits source evidence.

FTS5 indexes words and CJK bigrams. Three independent ranked lists (lexical,
profile topic, entity/time) are fused with RRF(60). Index setup is explicit and
separate from query latency. No model, network, vectors or reranker.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import time
import unicodedata
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from inner_loop import MindStore, Record

RRF_K = 60
MAX_QUERY_CHARS = 2048
MAX_TERMS = 64
CHANNEL_LIMIT = 32
# Ignore structural/provenance metadata, not domain-specific concepts.
TEXT_KEYS = frozenset(
    {
        "text",
        "content",
        "summary",
        "question",
        "goal",
        "reason",
        "subject",
        "attribute",
        "value",
        "title",
        "description",
        "message",
        "query",
        "name",
        "success_criteria",
    }
)
TOKEN = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]+")
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
IDENTIFIER = re.compile(
    r"(?<![a-z0-9_])(?:[a-f0-9]{32,64}|[a-z][a-z0-9_]*"
    r"_[a-z0-9_]{24,})(?![a-z0-9_])"
)


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def terms(text: str, limit: int = MAX_TERMS) -> list[str]:
    result: list[str] = []
    for part in TOKEN.findall(normalize(text)):
        if "\u3400" <= part[0] <= "\u9fff":
            result.extend(part[i : i + 2] for i in range(len(part) - 1))
        elif len(part) > 1:
            result.append(part)
    return list(dict.fromkeys(result))[:limit]


def text_values(value: Any, *, selected: bool = False, depth: int = 0) -> str:
    """Only textual content fields become a query, never IDs or JSON keys."""
    if depth > 6:
        return ""
    if isinstance(value, str):
        return value[:4096] if selected else ""
    if isinstance(value, dict):
        return " ".join(
            text_values(v, selected=selected or k in TEXT_KEYS, depth=depth + 1)
            for k, v in cast(Record, value).items()
        )[:8192]
    if isinstance(value, list):
        return " ".join(
            text_values(v, selected=selected, depth=depth + 1) for v in cast(list[Any], value)[:16]
        )[:8192]
    return ""


def background(payload: Record) -> bool:
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return True
    data = cast(Record, data)
    sense = data.get("sense_event", {})
    if not isinstance(sense, dict):
        sense = {}
    sense = cast(Record, sense)
    sample = sense.get("payload", data.get("result", {}))
    if isinstance(sample, dict) and any(
        cast(Record, sample).get(k) for k in ("synthetic", "backfill", "background_only")
    ):
        return True
    return bool(
        data.get("synthetic")
        or data.get("backfill")
        or data.get("background_only")
        or sense.get("synthetic")
        or sense.get("backfill")
        or sense.get("background_only")
        or sense.get("kind") == "backfill_summary"
    )


def effective_stamp(payload: Record, created_at: str) -> str:
    """An observation is visible only after ingestion AND all declared sample times."""
    stamps = [created_at]
    pending: list[Any] = [payload]
    while pending:
        value = pending.pop()
        if not isinstance(value, dict):
            continue
        for key, item in cast(Record, value).items():
            if isinstance(item, dict):
                pending.append(item)
            elif key in {"sampled_at", "observed_at", "timestamp", "occurred_at"}:
                if isinstance(item, str):
                    try:
                        moment = datetime.fromisoformat(item)
                        if moment.tzinfo is not None:
                            stamps.append(item)
                    except ValueError:
                        pass
    return max(stamps, key=datetime.fromisoformat)


def exact_topic(topic: str, query: str) -> bool:
    """ASCII token boundaries avoid age matching storage; CJK uses phrase spans."""
    topic, query = normalize(topic), normalize(query)
    if len(topic) < 2:
        return False
    if re.fullmatch(r"[a-z0-9_ ]+", topic):
        return re.search(r"(?<![a-z0-9_])" + re.escape(topic) + r"(?![a-z0-9_])", query) is not None
    return topic in query


def compact(record: Record) -> Record:
    """Bound only the recall view; the immutable source remains available by ID."""
    result: Record = {
        key: record[key]
        for key in (
            "id",
            "evidence_id",
            "recall_role",
            "category",
            "created_at",
            "source",
            "belief_id",
            "subject",
            "attribute",
            "value",
            "key",
            "confidence",
            "version",
        )
        if key in record
    }
    for key in ("value", "content"):
        if key in record:
            result[key] = str(record[key])[:768]
    if "payload" in record:
        result["payload"] = {"text": text_values(record["payload"])[:768]}
    if "refs" in record:
        result["refs"] = record["refs"][:8]
    return result


def readonly_optional(root: Path, name: str) -> sqlite3.Connection | None:
    """Absent optional stores stay absent; do not instantiate a writable store."""
    path = root / name
    if not path.exists():
        return None
    import os
    import stat

    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise ValueError("Unsafe optional recall store")
    db = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True, timeout=0.025)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def task_query(store: MindStore, as_of: str) -> str:
    pieces: list[str] = []
    with store.connection() as db:
        rows = db.execute(
            "SELECT payload,created_at FROM entries WHERE category='event' "
            "AND julianday(created_at)<=julianday(?) "
            "AND json_extract(payload,'$.kind') IN ('conversation','tool_result','file_result') "
            "ORDER BY seq DESC LIMIT 8",
            (as_of,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row[0])
            if not background(payload) and datetime.fromisoformat(
                effective_stamp(payload, row[1])
            ) <= datetime.fromisoformat(as_of):
                text = text_values(payload)[:512]
                if text:
                    pieces.append(text)
                if len(pieces) == 3:
                    break
        rows = db.execute(
            "SELECT e.payload FROM curiosity_question_latest_v1 q "
            "JOIN entries e ON e.seq=q.seq WHERE q.state NOT IN ('resolved','closed','cancelled') "
            "AND julianday(e.created_at)<=julianday(?) ORDER BY q.seq DESC LIMIT 2",
            (as_of,),
        ).fetchall()
        pieces.extend(text_values(json.loads(row[0]))[:256] for row in rows)
    goals = readonly_optional(store.path.parent, "goals.db")
    if goals is not None:
        try:
            rows = goals.execute(
                "SELECT goal FROM goals WHERE completed=0 "
                "AND julianday(updated_at)<=julianday(?) "
                "ORDER BY created_at DESC LIMIT 2",
                (as_of,),
            ).fetchall()
            pieces.extend(str(row[0])[:256] for row in rows)
        finally:
            goals.close()
    return "\n".join(piece[:256] for piece in pieces if piece.strip())[:MAX_QUERY_CHARS]


def self_evaluate() -> Record:
    """Describe local retrieval only; does not open private state or claim deployment."""
    return {
        "module": "inner_loop_fts",
        "domain": "task-directed local lexical memory recall",
        "current_version": "FTS5 word/CJK-bigram + exact topic + entity/time RRF(60)",
        "known_limitations": [
            "No semantic vectors or reranking; lexical mismatch is not resolved.",
            "Projection catch-up is bounded; one context sync reads at most 256 rows per kind.",
            "The caller's final context budget can still remove retrieved evidence.",
            "Retrieval success is not verified answer correctness or resident deployment.",
        ],
        "improvement_directions": [
            "Verify evidence survives the final assembled prompt budget.",
            "Evaluate a sealed human-reviewed set beyond identifier/topic questions.",
        ],
        "last_evaluated_at": None,  # static descriptor, not a clocked validation receipt
    }


class RecallIndex:
    def __init__(self, store: MindStore) -> None:
        self.store = store
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS recall_docs_v1 ("
                "doc INTEGER PRIMARY KEY, evidence TEXT NOT NULL UNIQUE, "
                "kind TEXT NOT NULL, identity TEXT NOT NULL, stamp TEXT NOT NULL, "
                "topic TEXT NOT NULL, entity TEXT NOT NULL, tokens TEXT NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS recall_identity_v1 ON recall_docs_v1(identity,stamp)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS recall_entities_v1 ("
                "entity TEXT NOT NULL,doc INTEGER NOT NULL,PRIMARY KEY(entity,doc)) "
                "WITHOUT ROWID"
            )
            db.execute("CREATE INDEX IF NOT EXISTS recall_entity_doc_v1 ON recall_entities_v1(doc)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS recall_topic_v1 ON recall_docs_v1(kind,topic,stamp)"
            )
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS recall_fts_v1 USING fts5("
                "tokens,content='recall_docs_v1',content_rowid='doc')"
            )
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS recall_docs_insert_v1 "
                "AFTER INSERT ON recall_docs_v1 BEGIN INSERT INTO "
                "recall_fts_v1(rowid,tokens) VALUES(NEW.doc,NEW.tokens); END"
            )
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS recall_docs_delete_v1 "
                "AFTER DELETE ON recall_docs_v1 BEGIN INSERT INTO "
                "recall_fts_v1(recall_fts_v1,rowid,tokens) "
                "VALUES('delete',OLD.doc,OLD.tokens); "
                "DELETE FROM recall_entities_v1 WHERE doc=OLD.doc; END"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS recall_cursor_v1 ("
                "kind TEXT PRIMARY KEY,seq INTEGER NOT NULL)"
            )
        # Initial migration is not hidden in the first timed context call.
        self.sync(full=True)

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(self.store.path, timeout=0.025)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=25")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _insert(
        db: sqlite3.Connection,
        evidence: str,
        kind: str,
        identity: str,
        stamp: str,
        text: str,
        topic: str = "",
        entity: str = "",
    ) -> None:
        # Compare instants rather than lexicographic ISO strings with offsets.
        # Invalid/future source timestamps cannot become retrieval evidence.
        if datetime.fromisoformat(stamp).tzinfo is None:
            return
        tokens = " ".join(terms(text[:8192], 4096))
        if not tokens:
            return
        db.execute(
            "INSERT OR IGNORE INTO recall_docs_v1 "
            "(evidence,kind,identity,stamp,topic,entity,tokens) VALUES(?,?,?,?,?,?,?)",
            (evidence, kind, identity, stamp, normalize(topic), normalize(entity), tokens),
        )
        doc = db.execute("SELECT doc FROM recall_docs_v1 WHERE evidence=?", (evidence,)).fetchone()[
            0
        ]
        db.executemany(
            "INSERT OR IGNORE INTO recall_entities_v1 VALUES(?,?)",
            [(e, doc) for e in set(entity.split() + [identity]) if e],
        )

    def sync(self, *, full: bool = False) -> None:
        """Incremental derived writes in batches, never a source-ledger trigger/UDF."""
        with self.connection() as db:
            for category in ("thought", "event"):
                cursor = db.execute(
                    "SELECT seq FROM recall_cursor_v1 WHERE kind=?", (category,)
                ).fetchone()
                after = int(cursor[0]) if cursor else 0
                while True:
                    rows = db.execute(
                        "SELECT * FROM entries WHERE category=? AND seq>? ORDER BY seq LIMIT 256",
                        (category, after),
                    ).fetchall()
                    for row in rows:
                        payload = json.loads(row["payload"])
                        if not background(payload) and (
                            category == "thought"
                            or payload.get("kind") in {"conversation", "tool_result", "file_result"}
                        ):
                            self._insert(
                                db,
                                "mind:entries:" + row["id"],
                                category,
                                row["id"],
                                effective_stamp(payload, row["created_at"]),
                                text_values(payload),
                                entity=" ".join(json.loads(row["refs"])),
                            )
                    if rows:
                        after = int(rows[-1]["seq"])
                        db.execute(
                            "INSERT INTO recall_cursor_v1 VALUES(?,?) ON CONFLICT(kind) "
                            "DO UPDATE SET seq=excluded.seq",
                            (category, after),
                        )
                    if not full or len(rows) < 256:
                        break
            # Keep versions for as-of queries; search suppresses later revisions.
            cursor = db.execute("SELECT seq FROM recall_cursor_v1 WHERE kind='belief'").fetchone()
            after = int(cursor[0]) if cursor else 0
            rows = db.execute(
                "SELECT * FROM belief_versions WHERE seq>? ORDER BY seq LIMIT ?",
                (after, 1000000 if full else 256),
            ).fetchall()
            for row in rows:
                latest_stamp = max(
                    (row["obtained_at"], row["last_verified"]), key=datetime.fromisoformat
                )
                self._insert(
                    db,
                    "mind:belief_versions:" + str(row["seq"]),
                    "belief",
                    row["belief_id"],
                    latest_stamp,
                    " ".join(str(row[k]) for k in ("subject", "attribute", "value")),
                    row["attribute"],
                    row["subject"],
                )
            if rows:
                db.execute(
                    "INSERT INTO recall_cursor_v1 VALUES('belief',?) ON CONFLICT(kind) "
                    "DO UPDATE SET seq=excluded.seq",
                    (int(rows[-1]["seq"]),),
                )

    def rollback(self) -> None:
        """Drop only R2 derived objects; old source evidence/triggers survive.

        Restore old application code before calling against a stopped runtime.
        Reinstantiating the new version rebuilds the disposable projection.
        """
        with self.connection() as db:
            for trigger in ("recall_docs_insert_v1", "recall_docs_delete_v1"):
                db.execute("DROP TRIGGER IF EXISTS " + trigger)
            for table in (
                "recall_fts_v1",
                "recall_entities_v1",
                "recall_docs_v1",
                "recall_cursor_v1",
            ):
                db.execute("DROP TABLE IF EXISTS " + table)

    def search(
        self,
        query: str,
        memories: list[Record],
        *,
        as_of: str,
        k: int = 7,
        deadline_ms: float = 120.0,
    ) -> tuple[list[Record], Record]:
        """Return real ranked candidates. Labels/gold are never an input.

        Explicit unknown identifiers abstain instead of matching shared ID
        fragments. SQL's progress handler actually cancels slow FTS work.
        """
        started = time.perf_counter()
        raw_query = query[:MAX_QUERY_CHARS]
        query = normalize(raw_query)
        # Automatic task sources are newline-delimited. Round-robin terms keep
        # the first event from crowding pending work/goals out of the 64-term cap.
        groups = [terms(piece, 16) for piece in query.splitlines()[:8]]
        if len(groups) > 1:
            from itertools import zip_longest

            query_terms = list(
                dict.fromkeys(
                    term for column in zip_longest(*groups, fillvalue="") for term in column if term
                )
            )[:MAX_TERMS]
        else:
            query_terms = terms(query)
        diagnostics: Record = {"status": "ok", "channels": {}, "rrf_k": RRF_K}
        if not query_terms:
            return [], diagnostics
        deadline = started + deadline_ms / 1000.0
        profiles: dict[str, Record] = {}
        profile_db = readonly_optional(self.store.path.parent, "sir_profile.db")
        if profile_db is not None:
            try:
                for row in profile_db.execute(
                    "SELECT * FROM facts WHERE "
                    "julianday(updated_at)<=julianday(?) "
                    "ORDER BY category,key LIMIT 512",
                    (as_of,),
                ):
                    if time.perf_counter() >= deadline:
                        diagnostics["status"] = "timeout"
                        return [], diagnostics
                    record = dict(row)
                    record["evidence_ids"] = json.loads(record["evidence_ids"])
                    profiles["profile:facts:" + record["id"]] = record
            finally:
                profile_db.close()
        memory_map = {"memory:facts:" + m["id"]: m for m in memories}
        channels: dict[str, list[str]] = {"lexical": [], "profile": [], "entity_time": []}
        with self.connection() as db:
            db.execute("CREATE TEMP TABLE authorized_memories(id TEXT PRIMARY KEY)")
            db.executemany(
                "INSERT INTO authorized_memories VALUES(?)", [(m["id"],) for m in memories]
            )
            # Purge deleted memories before FTS LIMIT so they cannot starve live hits.
            db.execute(
                "DELETE FROM recall_docs_v1 WHERE kind='memory' AND identity NOT IN "
                "(SELECT id FROM authorized_memories)"
            )
            # Snapshot-local memory is a caller-authorized set, including deletions.
            for evidence, memory in memory_map.items():
                if time.perf_counter() >= deadline:
                    diagnostics["status"] = "timeout"
                    return [], diagnostics
                previous = db.execute(
                    "SELECT tokens FROM recall_docs_v1 WHERE evidence=?", (evidence,)
                ).fetchone()
                tokens = " ".join(terms(memory["content"][:8192], 4096))
                if previous and previous[0] == tokens:
                    continue
                if previous:
                    db.execute("DELETE FROM recall_docs_v1 WHERE evidence=?", (evidence,))
                self._insert(
                    db, evidence, "memory", memory["id"], memory["created_at"], memory["content"]
                )
            db.set_progress_handler(lambda: int(time.perf_counter() >= deadline), 1000)
            # Only latest as-of belief version may contribute, never a correction's old value.
            valid = (
                "julianday(d.stamp)<=julianday(?) AND (d.kind!='belief' OR "
                "(NOT EXISTS (SELECT 1 FROM belief_versions b WHERE "
                "b.seq=CAST(substr(d.evidence,22) AS INTEGER) AND b.memory_id IS NOT NULL "
                "AND b.memory_id NOT IN (SELECT id FROM authorized_memories)) AND NOT EXISTS ("
                "SELECT 1 FROM belief_versions b WHERE b.belief_id=d.identity "
                "AND b.seq>CAST(substr(d.evidence,22) AS INTEGER) "
                "AND julianday(b.obtained_at)<=julianday(?) "
                "AND julianday(b.last_verified)<=julianday(?))))"
            )
            args: list[Any] = [as_of, as_of, as_of]
            try:
                identifiers = IDENTIFIER.findall(raw_query)
                if identifiers:
                    marks = ",".join("?" for _ in identifiers)
                    rows = db.execute(
                        "SELECT d.evidence FROM recall_docs_v1 d WHERE "
                        + valid
                        + " AND d.doc IN (SELECT doc FROM recall_entities_v1 "
                        "WHERE entity IN (" + marks + ")) ORDER BY d.doc DESC "
                        "LIMIT ?",
                        (*args, *identifiers, CHANNEL_LIMIT),
                    ).fetchall()
                    channels["entity_time"] = [row[0] for row in rows]
                    # A literal identifier is a strict filter, not a bag of subwords.
                    channels["lexical"] = list(channels["entity_time"])
                else:
                    expression = " OR ".join(
                        '"' + term.replace('"', '""') + '"' for term in query_terms
                    )
                    rows = db.execute(
                        "SELECT d.evidence FROM recall_fts_v1 f "
                        "JOIN recall_docs_v1 d ON d.doc=f.rowid "
                        "WHERE recall_fts_v1 MATCH ? AND "
                        + valid
                        + " ORDER BY bm25(recall_fts_v1),d.doc DESC LIMIT ?",
                        (expression, *args, CHANNEL_LIMIT * 2),
                    ).fetchall()
                    channels["lexical"] = [row[0] for row in rows]
                    # Topic exact containment is intentionally separate from FTS scoring.
                    rows = db.execute(
                        "SELECT d.evidence FROM recall_docs_v1 d WHERE "
                        + valid
                        + " AND d.kind='belief' AND length(d.topic)>=2 "
                        "AND instr(?,d.topic)>0 ORDER BY d.doc DESC LIMIT ?",
                        (*args, query, CHANNEL_LIMIT),
                    ).fetchall()
                    channels["profile"] = [
                        row[0]
                        for row in rows
                        if exact_topic(
                            db.execute(
                                "SELECT topic FROM recall_docs_v1 WHERE evidence=?", (row[0],)
                            ).fetchone()[0],
                            query,
                        )
                    ]
                    entities = [t for t in query_terms if len(t) > 1]
                    if entities:
                        marks = ",".join("?" for _ in entities)
                        rows = db.execute(
                            "SELECT d.evidence FROM recall_docs_v1 d WHERE "
                            + valid
                            + " AND d.doc IN (SELECT doc FROM recall_entities_v1 "
                            "WHERE entity IN (" + marks + ")) "
                            "ORDER BY d.doc DESC LIMIT ?",
                            (*args, *entities, CHANNEL_LIMIT),
                        ).fetchall()
                        channels["entity_time"] = [row[0] for row in rows]
                    dates = DATE.findall(query)
                    if dates:
                        marks = ",".join("?" for _ in dates)
                        rows = db.execute(
                            "SELECT d.evidence FROM recall_docs_v1 d WHERE "
                            + valid
                            + " AND substr(d.stamp,1,10) IN ("
                            + marks
                            + ") "
                            "ORDER BY d.doc DESC LIMIT ?",
                            (*args, *dates, CHANNEL_LIMIT),
                        ).fetchall()
                        channels["entity_time"] = [row[0] for row in rows]
                    for evidence, profile in profiles.items():
                        key = normalize(profile["key"])
                        if exact_topic(key, query):
                            channels["profile"].append(evidence)
                # Remove deleted caller memories before ranks/fusion, not afterwards.
                for name, ids in channels.items():
                    channels[name] = list(
                        dict.fromkeys(
                            e for e in ids if not e.startswith("memory:") or e in memory_map
                        )
                    )[:CHANNEL_LIMIT]
                scores: dict[str, float] = {}
                for ids in channels.values():
                    for rank, evidence in enumerate(ids, 1):
                        scores[evidence] = scores.get(evidence, 0.0) + 1.0 / (RRF_K + rank)
                ranked = sorted(scores, key=lambda e: (-scores[e], e))
                results: list[Record] = []
                for evidence in ranked:
                    record: Record | None = None
                    if evidence in memory_map:
                        record = dict(memory_map[evidence])
                    elif evidence in profiles:
                        record = dict(profiles[evidence])
                        record["content"] = " ".join(
                            str(record[x]) for x in ("category", "key", "value")
                        )
                    elif evidence.startswith("mind:entries:"):
                        row = db.execute(
                            "SELECT * FROM entries WHERE id=?", (evidence[13:],)
                        ).fetchone()
                        if row:
                            record = dict(row)
                            record["payload"] = json.loads(record["payload"])
                            record["refs"] = json.loads(record["refs"])
                    elif evidence.startswith("mind:belief_versions:"):
                        row = db.execute(
                            "SELECT * FROM belief_versions WHERE seq=?", (int(evidence[21:]),)
                        ).fetchone()
                        if row:
                            record = dict(row)
                            record["id"] = record["belief_id"]
                            record["refs"] = json.loads(record["refs"])
                    if record is not None:
                        record.update(evidence_id=evidence, recall_role="evidence")
                        results.append(compact(record))
                        if len(results) >= k:
                            break
                diagnostics["channels"] = {name: len(ids) for name, ids in channels.items()}
                return results, diagnostics
            except sqlite3.OperationalError as error:
                if "interrupted" not in str(error):
                    raise
                diagnostics["status"] = "timeout"
                return [], diagnostics
            finally:
                db.set_progress_handler(None, 0)

    def exploration(
        self, memories: list[Record], *, seed: int, as_of: str, exclude: set[str]
    ) -> Record | None:
        """One reproducible random item, labeled non-retrieval evidence."""
        with self.connection() as db:
            count = int(
                db.execute(
                    "SELECT COALESCE(MAX(rank),0) FROM curiosity_thought_rank_v1"
                ).fetchone()[0]
            )
            old_count = max(0, count - 8)
            population = old_count + len(memories)
            if not population:
                return None
            # A bounded probe avoids an O(history) randomized ORDER BY.
            for index in random.Random(seed).sample(range(population), min(population, 9)):
                if index >= old_count:
                    record = dict(memories[index - old_count])
                else:
                    row = db.execute(
                        "SELECT e.* FROM curiosity_thought_rank_v1 r "
                        "JOIN entries e ON e.seq=r.seq WHERE r.rank=?",
                        (index + 1,),
                    ).fetchone()
                    if not row or datetime.fromisoformat(
                        row["created_at"]
                    ) > datetime.fromisoformat(as_of):
                        continue
                    record = dict(row)
                    record["payload"] = json.loads(record["payload"])
                    record["refs"] = json.loads(record["refs"])
                    if background(record["payload"]):
                        continue
                created_at = datetime.fromisoformat(record["created_at"])
                if record["id"] not in exclude and created_at <= datetime.fromisoformat(as_of):
                    record["recall_role"] = "exploration"
                    return compact(record)
        return None
