"""Generic belief bookkeeping and deterministic value-of-information scheduling."""

from __future__ import annotations

import hashlib
import json
import math
import random
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import product_memory
from inner_loop import MindStore, Record
from inner_loop_fts import RecallIndex, task_query

INITIAL_CONFIDENCE = {"self_report": 0.6, "observed": 0.9, "inferred": 0.4}
HALF_LIVES = {"self_report": 30 * 86400.0, "observed": 7 * 86400.0, "inferred": 3 * 86400.0}
TOP_K = 8
# Two discoveries per thought keep novelty interpretable alongside existing work;
# sixteen active gaps bound backlog to two full ranking windows. Below the
# question threshold a gap cannot justify interrupting Sir, so prune that tail.
MAX_NEW_GAPS = 2
MAX_ACTIVE_GAPS = 16
MIN_GAP_IMPORTANCE = 0.25
VOI_THRESHOLD = 0.15
QUESTION_THRESHOLD = 0.25
RELEVANCE_THRESHOLD = 0.15
RECENT_THOUGHTS = 8
# Acknowledging obsolete observations is housekeeping, not fresh evidence. Drain
# at most one small batch per context so a cold/catalog rebuild cannot flood the
# append-only ledger; unacknowledged origins remain pending for later rounds.
MAX_HISTORICAL_ORIGINS_PER_ROUND = 32
# A direct statement remains usable for a full decay interval. Confidence
# decay alone must not create an immediate request to repeat that statement.
SELF_REPORT_RECONFIRM_SECONDS = 30 * 86400.0


def self_evaluate() -> dict[str, Any]:
    """Report bounded belief/curiosity policy without opening the ledger.

    This is a static policy and validator-availability audit. It does not read
    mind.db, run the scheduler, or prove that questions were useful.
    """
    return {
        "module": "curiosity",
        "domain": "belief confidence, knowledge gaps and value-of-information scheduling",
        "limits": {
            "top_k": TOP_K,
            "max_new_gaps": MAX_NEW_GAPS,
            "max_active_gaps": MAX_ACTIVE_GAPS,
            "min_gap_importance": MIN_GAP_IMPORTANCE,
            "voi_threshold": VOI_THRESHOLD,
            "question_threshold": QUESTION_THRESHOLD,
            "recent_thoughts": RECENT_THOUGHTS,
            "max_historical_origins_per_round": MAX_HISTORICAL_ORIGINS_PER_ROUND,
        },
        "validators": sorted(
            name
            for name in ("validate_gaps", "validate_updates", "validate_matrix")
            if callable(globals().get(name))
        ),
        "known_limitations": [
            "Policy limits are reported, not exercised; no ranking or scheduling is performed.",
            "Gap and belief values come from caller/model output and are not independently verified here.",
            "Curiosity projections live in mind.db derived tables and are disposable, not provenance.",
        ],
        "improvement_directions": [
            "Add an offline replay test that feeds recorded evidence through confidence and gap selection.",
            "Report observed-versus-synthetic evidence ratios without scanning raw payloads.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


INSTRUCTIONS = """补充返回 belief_updates 数组，每项为
{"origin_id":"memory:完整ID 或待处理工具事件ID","belief_id":"原信念ID或null",
"subject":"关于谁，自由写","attribute":"哪方面，自由标注，不设候选列表",
"value":"证据支持的值","importance":0.8,"conflict":false}。
把 pending_memories 逐条标注为信念，origin_id 和 belief_id 均用其 memory:完整ID。
对 pending_observations，可从感知形成一条有范围的信念：belief_id 使用事件 data.belief_id；
已有同 ID 信念就更新它。来源由事件证据决定，真实感知为 observed，本人自述为 self_report。
只有模型解释证据后才写 belief_updates；证据不足可以暂留背景，合成或历史汇总不能算真实观察。
只据实际样本的精度和范围写值，冲突时 conflict=true。不要把单次样本推广为持续习惯。
relevance 为 relevance_candidates 与所有能力的笛卡尔积打分：
这个数据源的输出能否改变我对这条信念的把握？0到1，并说理由。
cached_relevance 已有的配对可以不重复返回，信念或能力变化时自动失效。
scheduler_question 只在无相关数据源且高不确定时提供一句询问；有数据源的先查。
自由联想可以接上 recent_thoughts，也可以由 recalled 的旧片段游离。
同时可返回 knowledge_gaps 数组，表示我意识到自己对 Sir 仍不知道的方面，每项为
{"subject":"关于谁，自由写","attribute":"未知的哪方面，自由标注",
"importance":0.8,"reason":"这项未知为何值得补足"}。
从已有信念、感知和对 Sir 的整体了解自由发现空白，不是复核已知事实，也不是替数据源编造任务。
空白的值保持 null，不确定度为1；attribute 由我自由命名，同一方面沿用原标注。
先检查 beliefs 与 known_gap_identities，已提出、查清或淘汰过的同一空白不重复提出。
每轮最多两项，重要度低的会被淘汰；新空白下一轮与已有信念一起评估相关度，不给它预填答案。
工具返回只有真正填补了目标空白才写 belief_updates；只证明工具有记录不等于回答了未知。
"""


def event_evidence(event: Record, catalog: list[Record]) -> Record | None:
    """Determine provenance mechanically; semantic interpretation stays with the model."""
    data = event["payload"]["data"]
    sense = data.get("sense_event")
    if isinstance(sense, dict):
        sense = cast(Record, sense)
        result = sense.get("payload", {})
        capability = next(
            (
                c
                for c in catalog
                if c.get("call", {}).get("arguments", {}).get("source") == sense.get("source")
            ),
            None,
        )
        if (
            not capability
            or event["source"] != "senses:" + str(sense.get("source"))
            or event["payload"]["kind"] != "tool_result"
            or sense.get("background_only")
            or result.get("synthetic")
            or sense.get("kind") == "backfill_summary"
            or result.get("status") != "available"
        ):
            return None
        identity = (
            "perception:"
            + hashlib.sha256(
                json.dumps([sense["source"], sense["kind"]], ensure_ascii=False).encode()
            ).hexdigest()[:24]
        )
        return {
            "belief_id": identity,
            "source": "observed",
            "result": result,
            "capability_id": capability["id"],
            "verified_at": result["sampled_at"],
        }
    if event["payload"]["kind"] == "conversation" and event["source"] in {"chat", "sir", "owner"}:
        if data.get("role", "user") != "user" or data.get("synthetic") or data.get("backfill"):
            return None
        # One statement provides a stable provisional identity; future semantic
        # updates can explicitly target an existing person-level belief.
        return {
            "belief_id": "statement:" + event["id"],
            "source": "self_report",
            "result": data,
            "verified_at": event["created_at"],
            "capability_id": None,
        }
    return None


def confidence(belief: Record, now: datetime) -> float:
    if belief["source"] == "gap":
        return 0.0
    verified = datetime.fromisoformat(belief["last_verified"])
    age = max(0.0, (now - verified).total_seconds())
    return float(belief["confidence"]) * math.exp(-math.log(2) * age / HALF_LIVES[belief["source"]])


def context_capabilities(catalog: list[Record]) -> list[Record]:
    """Shared prompt/cache shape; execution continues to use the full catalog."""
    fields = (
        "id",
        "name",
        "persona_description",
        "call",
        "implementation",
        "available",
        "costs_money",
        "security_sensitive",
    )
    return [{key: row[key] for key in fields if key in row} for row in catalog]


def fingerprint(belief: Record, catalog: list[Record]) -> str:
    payload = {
        k: belief[k]
        for k in ("belief_id", "revision", "subject", "attribute", "value", "source", "importance")
    }
    return hashlib.sha256(
        json.dumps(
            [payload, sorted(context_capabilities(catalog), key=lambda row: row["id"])],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()


class Curiosity:
    def __init__(self, store: MindStore) -> None:
        self.store = store
        self._prepare_history()
        self.recall_index = RecallIndex(store)

    def _prepare_history(self) -> None:
        """Derived, disposable projections; the append-only ledger stays authoritative."""
        with self.store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS curiosity_cursor_v1 (
                    catalog TEXT PRIMARY KEY, seq INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS curiosity_pending_v1 (
                    catalog TEXT NOT NULL, seq INTEGER NOT NULL, id TEXT NOT NULL,
                    identity TEXT NOT NULL, verified TEXT NOT NULL,
                    evidence TEXT, PRIMARY KEY(catalog,seq)
                );
                CREATE INDEX IF NOT EXISTS curiosity_pending_identity_v1
                    ON curiosity_pending_v1(catalog,identity,verified DESC,seq);
                CREATE INDEX IF NOT EXISTS curiosity_pending_id_v1
                    ON curiosity_pending_v1(catalog,id);
                CREATE TABLE IF NOT EXISTS curiosity_focus_v1 (
                    identity TEXT PRIMARY KEY, seq INTEGER NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS curiosity_focus_insert_v1
                    AFTER INSERT ON entries WHEN NEW.category='thought'
                    BEGIN
                      INSERT INTO curiosity_focus_v1(identity,seq)
                      SELECT value,NEW.seq FROM json_each(NEW.refs) WHERE 1
                      ON CONFLICT(identity) DO UPDATE SET seq=excluded.seq;
                    END;
                INSERT OR IGNORE INTO curiosity_focus_v1(identity,seq)
                    SELECT j.value,MAX(e.seq) FROM entries e,json_each(e.refs) j
                    WHERE e.category='thought' GROUP BY j.value;
                CREATE TABLE IF NOT EXISTS curiosity_evidence_v1 (
                    identity TEXT PRIMARY KEY, seq INTEGER NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS curiosity_evidence_insert_v1
                    AFTER INSERT ON entries WHEN NEW.category IN ('event','conflict')
                    BEGIN
                      INSERT INTO curiosity_evidence_v1(identity,seq)
                      SELECT CASE WHEN NEW.category='conflict'
                        THEN json_extract(NEW.payload,'$.belief_id')
                        ELSE COALESCE(json_extract(NEW.payload,'$.data.belief_id'),'*') END, NEW.seq
                      WHERE NEW.category='conflict' OR (
                        COALESCE(json_extract(NEW.payload,'$.data.synthetic'),0)=0 AND
                        COALESCE(json_extract(NEW.payload,'$.data.backfill'),0)=0 AND
                        (json_extract(NEW.payload,'$.data.belief_id') IS NOT NULL OR (
                         json_extract(NEW.payload,'$.kind')='conversation' AND
                         NEW.source IN ('chat','sir','owner') AND
                         COALESCE(json_extract(NEW.payload,'$.data.role'),'user')!='assistant')))
                      ON CONFLICT(identity) DO UPDATE SET seq=excluded.seq;
                    END;
                CREATE TABLE IF NOT EXISTS curiosity_question_latest_v1 (
                    question_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, state TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS curiosity_question_latest_insert_v1
                    AFTER INSERT ON entries WHEN NEW.category='question_state'
                    BEGIN
                      INSERT INTO curiosity_question_latest_v1 VALUES (
                        json_extract(NEW.payload,'$.question_id'),NEW.seq,
                        json_extract(NEW.payload,'$.state'))
                      ON CONFLICT(question_id) DO UPDATE SET seq=excluded.seq,state=excluded.state;
                    END;
                CREATE TABLE IF NOT EXISTS curiosity_resolvable_v1 (
                    belief_id TEXT NOT NULL, capability_id TEXT NOT NULL,
                    PRIMARY KEY(belief_id,capability_id)
                );
                CREATE TRIGGER IF NOT EXISTS curiosity_resolvable_insert_v1
                    AFTER INSERT ON entries WHEN NEW.category='action_request'
                    BEGIN
                      INSERT OR IGNORE INTO curiosity_resolvable_v1
                      SELECT json_extract(NEW.payload,'$.belief_id'),
                             json_extract(j.value,'$.capability_id')
                      FROM json_each(NEW.payload,'$.question.assessments') j
                      WHERE json_extract(NEW.payload,'$.belief_id') IS NOT NULL
                      AND json_extract(j.value,'$.can_resolve')=1;
                    END;
                INSERT OR IGNORE INTO curiosity_resolvable_v1
                  SELECT json_extract(e.payload,'$.belief_id'),
                         json_extract(j.value,'$.capability_id')
                  FROM entries e,json_each(e.payload,'$.question.assessments') j
                  WHERE e.category='action_request'
                  AND json_extract(e.payload,'$.belief_id') IS NOT NULL
                  AND json_extract(j.value,'$.can_resolve')=1;
                CREATE TABLE IF NOT EXISTS curiosity_thought_rank_v1 (
                    rank INTEGER PRIMARY KEY, seq INTEGER NOT NULL UNIQUE
                );
                CREATE TRIGGER IF NOT EXISTS curiosity_thought_rank_insert_v1
                    AFTER INSERT ON entries WHEN NEW.category='thought'
                    BEGIN
                      INSERT INTO curiosity_thought_rank_v1(seq) VALUES(NEW.seq);
                    END;
                INSERT OR IGNORE INTO curiosity_thought_rank_v1(seq)
                    SELECT seq FROM entries WHERE category='thought' ORDER BY seq;
                CREATE INDEX IF NOT EXISTS runtime_category_seq_v1 ON entries(category,seq);
                CREATE INDEX IF NOT EXISTS runtime_origin_v1 ON entries(
                    category,json_extract(payload,'$.origin_id'));
                CREATE INDEX IF NOT EXISTS runtime_question_v1 ON entries(
                    category,json_extract(payload,'$.question_id'),seq);
                CREATE INDEX IF NOT EXISTS runtime_belief_capability_v1 ON entries(
                    category,json_extract(payload,'$.belief_id'),
                    json_extract(payload,'$.capability_id'),seq);
                CREATE INDEX IF NOT EXISTS runtime_capability_v1 ON entries(
                    category,json_extract(payload,'$.capability_id'),seq);
                CREATE INDEX IF NOT EXISTS runtime_fingerprint_v1 ON entries(
                    category,json_extract(payload,'$.fingerprint'),seq);
                CREATE INDEX IF NOT EXISTS runtime_action_result_v1 ON entries(
                    category,json_extract(payload,'$.data.action_id'),
                    json_extract(payload,'$.data.result.status'));
            """)

            if not db.execute(
                "SELECT 1 FROM curiosity_cursor_v1 WHERE catalog='attention-v1'"
            ).fetchone():
                db.executescript("""
                    INSERT OR IGNORE INTO curiosity_question_latest_v1
                    SELECT json_extract(payload,'$.question_id'),MAX(seq),
                           json_extract(payload,'$.state') FROM entries
                    WHERE category='question_state' GROUP BY json_extract(payload,'$.question_id');
                    INSERT OR IGNORE INTO curiosity_evidence_v1
                    SELECT CASE WHEN category='conflict' THEN json_extract(payload,'$.belief_id')
                      ELSE COALESCE(json_extract(payload,'$.data.belief_id'),'*') END,MAX(seq)
                    FROM entries WHERE category='conflict' OR (category='event' AND
                      COALESCE(json_extract(payload,'$.data.synthetic'),0)=0 AND
                      COALESCE(json_extract(payload,'$.data.backfill'),0)=0 AND (
                       json_extract(payload,'$.data.belief_id') IS NOT NULL OR (
                        json_extract(payload,'$.kind')='conversation' AND
                        source IN ('chat','sir','owner') AND
                        COALESCE(json_extract(payload,'$.data.role'),'user')!='assistant')))
                    GROUP BY 1;
                    INSERT INTO curiosity_cursor_v1 VALUES('attention-v1',0);
                """)

    def _pending_events(self, catalog: list[Record]) -> Iterator[Record]:
        key = hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest()
        with self.store.connection() as db:
            row = db.execute(
                "SELECT seq FROM curiosity_cursor_v1 WHERE catalog=?", (key,)
            ).fetchone()
            cutoff = int(row[0]) if row else 0
        # Only new ledger rows are decoded. Restart resumes a durable cursor;
        # catalog changes intentionally rebuild provenance against the new catalog.
        for row in self.store.iter_entries(after_seq=cutoff):
            with self.store.connection() as db:
                if row["category"] == "event":
                    evidence = event_evidence(row, catalog)
                    data = row["payload"]["data"]
                    if evidence or (
                        row["payload"]["kind"] == "tool_result" and data.get("belief_id")
                    ):
                        handled = db.execute(
                            "SELECT 1 FROM entries WHERE category='belief_origin' "
                            "AND json_extract(payload,'$.origin_id')=? LIMIT 1",
                            (row["id"],),
                        ).fetchone()
                        if not handled:
                            db.execute(
                                "INSERT OR IGNORE INTO curiosity_pending_v1 VALUES(?,?,?,?,?,?)",
                                (
                                    key,
                                    row["seq"],
                                    row["id"],
                                    evidence["belief_id"] if evidence else row["id"],
                                    evidence["verified_at"] if evidence else row["created_at"],
                                    json.dumps(evidence) if evidence else None,
                                ),
                            )
                elif row["category"] == "belief_origin":
                    db.execute(
                        "DELETE FROM curiosity_pending_v1 WHERE catalog=? AND id=?",
                        (key, row["payload"]["origin_id"]),
                    )
                db.execute(
                    "INSERT INTO curiosity_cursor_v1 VALUES(?,?) "
                    "ON CONFLICT(catalog) DO UPDATE SET seq=excluded.seq",
                    (key, row["seq"]),
                )
        # Pending work, not historical payloads. The identity index supplies the
        # newest timestamp (earlier seq wins ties), matching the old Python walk.
        with self.store.connection() as db:
            # Catalog-specific projections can be stale even after their cursor
            # passed another catalog's receipt. Recheck the authoritative ledger.
            # Unary + removes TEXT affinity so SQLite can use the JSON expression
            # index for this correlated origin lookup (IDs are already strings).
            rows = db.execute(
                "SELECT p.*, e.payload,e.refs,e.category,e.created_at,e.source FROM "
                "curiosity_pending_v1 p JOIN entries e ON e.seq=p.seq WHERE p.catalog=? "
                "AND NOT EXISTS (SELECT 1 FROM entries o WHERE o.category='belief_origin' "
                "AND json_extract(o.payload,'$.origin_id')=+p.id) "
                "ORDER BY p.seq",
                (key,),
            )
            while batch := rows.fetchmany(256):
                for row in batch:
                    result = dict(row)
                    result["payload"] = json.loads(result["payload"])
                    result["refs"] = json.loads(result["refs"])
                    result["_evidence"] = (
                        json.loads(result["evidence"]) if result["evidence"] else None
                    )
                    yield result

    def _acknowledge_historical(self, origins: list[tuple[str, str]]) -> None:
        """One durable receipt per origin/belief, atomically checked under SQLite's writer lock.

        Keep a receipt (rather than silently discarding evidence) so catalog
        rebuilds/restarts know this origin was examined. Never revise old rows.
        The caller bounds this batch, and one transaction avoids a commit storm.
        """
        with self.store.connection() as db:
            for origin, identity in origins:
                db.execute(
                    "INSERT INTO entries(id,category,created_at,source,payload,refs) "
                    "SELECT ?, 'belief_origin', ?, 'curiosity', ?, ? "
                    "WHERE NOT EXISTS (SELECT 1 FROM entries WHERE category='belief_origin' "
                    "AND json_extract(payload,'$.origin_id')=? "
                    "AND json_extract(payload,'$.belief_id')=?)",
                    (
                        uuid.uuid4().hex,
                        datetime.now(UTC).isoformat(),
                        json.dumps(
                            {
                                "origin_id": origin,
                                "belief_id": identity,
                                "status": "historical_or_duplicate",
                            }
                        ),
                        json.dumps([origin, identity]),
                        origin,
                        identity,
                    ),
                )

    def _question_blocked(self, identity: str) -> bool:
        with self.store.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM entries q WHERE q.category IN "
                    "('question_state','question_generation_intent','question_generation_failure') "
                    "AND (json_extract(q.payload,'$.question_id')=? OR "
                    "EXISTS(SELECT 1 FROM entries r WHERE r.category='action_request' "
                    "AND json_extract(r.payload,'$.belief_id')=? AND "
                    "json_extract(r.payload,'$.question_id')="
                    "json_extract(q.payload,'$.question_id'))) "
                    "LIMIT 1",
                    ("scheduler:" + identity, identity),
                ).fetchone()
                is not None
            )

    def _attempted(self, belief: Record, capability: Record, now: datetime) -> bool:
        with self.store.connection() as db:
            if capability.get("implementation") != "senses_read_only":
                return (
                    db.execute(
                        "SELECT 1 FROM entries WHERE category='action' AND "
                        "json_extract(payload,'$.capability_id')=? LIMIT 1",
                        (capability["id"],),
                    ).fetchone()
                    is not None
                )
            sql = (
                "SELECT 1 FROM entries a WHERE a.category='action' AND "
                "json_extract(a.payload,'$.belief_id')=? AND "
                "json_extract(a.payload,'$.capability_id')=? AND "
                "json_extract(a.payload,'$.belief_revision')=?"
            )
            args: list[object] = [belief["belief_id"], capability["id"], belief["revision"]]
            if belief["source"] != "gap":
                # julianday preserves offset-aware timestamp comparison.
                sql += (
                    " AND (julianday(a.created_at)>julianday(?)-?/86400.0 OR NOT EXISTS("
                    "SELECT 1 FROM entries e WHERE e.category='event' AND "
                    "json_extract(e.payload,'$.data.action_id')=a.id AND "
                    "json_extract(e.payload,'$.data.result.status')='available'))"
                )
                args.extend([now.isoformat(), HALF_LIVES[belief["source"]]])
            return db.execute(sql + " LIMIT 1", args).fetchone() is not None

    def discover(
        self, gaps: list[Record], round_number: int, *, defer_excess: bool = False
    ) -> None:
        """Persist model discoveries once; IDs are domain-neutral normalized labels."""
        known = {gap_identity(b["subject"], b["attribute"]) for b in self.store.beliefs()}
        now = datetime.now(UTC).isoformat()
        accepted = 0
        for gap in sorted(gaps, key=lambda g: -g["importance"]):
            identity = gap_identity(gap["subject"], gap["attribute"])
            if identity in known or any(
                self.store.exists(category, where={"belief_id": identity})
                for category in ("knowledge_gap", "gap_retired")
            ):
                continue
            if defer_excess and accepted >= MAX_NEW_GAPS:
                continue
            if gap["importance"] < MIN_GAP_IMPORTANCE or accepted >= MAX_NEW_GAPS:
                self.store.append(
                    "gap_retired",
                    {
                        "belief_id": identity,
                        "reason": "low_importance"
                        if gap["importance"] < MIN_GAP_IMPORTANCE
                        else "per_round_limit",
                        "round": round_number,
                    },
                    source="curiosity",
                )
                continue
            self.store.append(
                "knowledge_gap",
                {
                    **gap,
                    "belief_id": identity,
                    "revision": 1,
                    "value": None,
                    "source": "gap",
                    "confidence": 0.0,
                    "last_verified": now,
                    "obtained_at": now,
                    "memory_id": None,
                    "refs": [],
                    "round": round_number,
                },
                source="model",
            )
            accepted += 1
        active = sorted(
            (b for b in self.store.beliefs() if b["source"] == "gap"),
            key=lambda b: (-b["importance"], b["obtained_at"]),
        )
        for gap in active[MAX_ACTIVE_GAPS:]:
            self.store.append(
                "gap_retired",
                {"belief_id": gap["belief_id"], "reason": "active_limit", "round": round_number},
                source="curiosity",
            )

    def question_eligible(self, belief: Record, now: datetime) -> bool:
        if belief["source"] != "self_report":
            return True
        verified = datetime.fromisoformat(belief["last_verified"])
        if (now - verified).total_seconds() >= SELF_REPORT_RECONFIRM_SECONDS:
            return True
        with self.store.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM entries WHERE category='conflict' AND "
                    "json_extract(payload,'$.belief_id')=? AND julianday(created_at)>julianday(?) "
                    "LIMIT 1",
                    (belief["belief_id"], verified.isoformat()),
                ).fetchone()
                is not None
            )

    def attention(self, belief: Record) -> Record:
        """Explain waiting work and reopen attention only on evidence, not ticks."""
        identity = belief["belief_id"]
        with self.store.connection() as db:
            focus = db.execute(
                "SELECT e.seq,e.created_at FROM curiosity_focus_v1 f "
                "JOIN entries e ON e.seq=f.seq WHERE f.identity=?",
                (identity,),
            ).fetchone()
            waiting = db.execute(
                "SELECT q.seq,e.created_at FROM curiosity_question_latest_v1 q "
                "JOIN entries e ON e.seq=q.seq WHERE q.state IN "
                "('ask_sir','pending_sir_permission') AND q.question_id IN ("
                "SELECT ? UNION SELECT json_extract(payload,'$.question_id') FROM entries "
                "WHERE category='action_request' AND json_extract(payload,'$.belief_id')=?) "
                "ORDER BY q.seq DESC LIMIT 1",
                ("scheduler:" + identity, identity),
            ).fetchone()
            cutoff = max(int(focus["seq"]) if focus else 0, int(waiting["seq"]) if waiting else 0)
            evidence = (
                db.execute(
                    "SELECT 1 FROM curiosity_evidence_v1 "
                    "WHERE identity IN (?, '*') AND seq>? LIMIT 1",
                    (identity, cutoff),
                ).fetchone()
                if waiting
                else None
            )
        cutoff_at = max(
            (datetime.fromisoformat(r["created_at"]) for r in (focus, waiting) if r),
            default=datetime.min.replace(tzinfo=UTC),
        )
        new_evidence = bool(evidence) or bool(
            waiting and datetime.fromisoformat(belief["last_verified"]) > cutoff_at
        )
        return {
            "status": "awaiting_sir" if waiting else "open",
            "last_thought_at": focus["created_at"] if focus else None,
            "new_evidence": new_evidence,
            "eligible_focus": not waiting or new_evidence,
        }

    def context(
        self,
        memories: list[Record],
        catalog: list[Record],
        round_number: int,
        *,
        query: str | None = None,
    ) -> Record:
        now = datetime.now(UTC)
        memories = [{k: m[k] for k in ("id", "content", "created_at", "version")} for m in memories]
        beliefs = self.store.beliefs()
        active_memory = {"memory:" + m["id"] for m in memories}
        # A deleted/corrected memory stops being a candidate; its history remains.
        beliefs = [b for b in beliefs if not b["memory_id"] or b["memory_id"] in active_memory]
        imported = {b["memory_id"] for b in beliefs}
        pending = [
            {**m, "origin_id": "memory:" + m["id"]}
            for m in memories
            if "memory:" + m["id"] not in imported
        ]
        for belief in beliefs:
            belief["current_confidence"] = confidence(belief, now)
            belief["u"] = 1 - belief["current_confidence"]
            belief["attention"] = self.attention(belief)
        candidates = sorted(
            (b for b in beliefs if b["attention"]["eligible_focus"]),
            key=lambda b: b["u"] * b["importance"],
            reverse=True,
        )[:TOP_K]
        # Pending memory IDs are stable; model freely supplies their attributes and importance.
        candidates.extend(
            {
                "belief_id": m["origin_id"],
                "value": m["content"],
                "source": "self_report",
                "pending": True,
            }
            for m in pending[:TOP_K]
        )
        observations: list[Record] = []
        # Latest event per provisional identity bounds backlog while preserving the
        # actual source timestamp. Historical summaries are background only.
        latest: dict[str, Record] = {}
        historical: list[tuple[str, str]] = []
        for event in self._pending_events(catalog):
            evidence = event.pop("_evidence")
            data = event["payload"]["data"]
            if evidence:
                identity = evidence["belief_id"]
                existing = latest.get(identity)
                if (
                    existing
                    and existing["payload"]["data"]["evidence"]["verified_at"]
                    >= evidence["verified_at"]
                ):
                    continue
                prior_belief = next((b for b in beliefs if b["belief_id"] == identity), None)
                if prior_belief and evidence["verified_at"] <= prior_belief["last_verified"]:
                    if len(historical) < MAX_HISTORICAL_ORIGINS_PER_ROUND:
                        historical.append((event["id"], identity))
                    continue
                latest[identity] = {
                    **event,
                    "payload": {
                        **event["payload"],
                        "data": {
                            **data,
                            "belief_id": identity,
                            "evidence": evidence,
                        },
                    },
                }
            elif event["payload"]["kind"] == "tool_result" and data.get("belief_id"):
                observations.append(event)
        if historical:
            self._acknowledge_historical(historical)
        observations.extend(list(latest.values())[-TOP_K:])
        candidate_ids = {b["belief_id"] for b in candidates}
        for event in observations:
            evidence = event["payload"]["data"].get("evidence")
            if evidence and evidence["belief_id"] not in candidate_ids:
                candidates.append(
                    {
                        "belief_id": evidence["belief_id"],
                        "pending": True,
                        "value": evidence["result"],
                        "source": evidence["source"],
                    }
                )
                candidate_ids.add(evidence["belief_id"])
        cache: list[Record] = []
        for belief in beliefs:
            if belief["belief_id"] in {e["payload"]["data"]["belief_id"] for e in observations}:
                continue
            key = fingerprint(belief, catalog)
            matches = self.store.recent("relevance_cache", 1, where={"fingerprint": key})
            if matches:
                cache.extend(matches[0]["payload"]["rows"])
        recent = self.store.recent("thought", RECENT_THOUGHTS)
        self.recall_index.sync()
        seed = random.SystemRandom().getrandbits(63)
        recall_query = query if query is not None else task_query(self.store, now.isoformat())
        recalled, recall_diagnostics = self.recall_index.search(
            recall_query, memories, as_of=now.isoformat(), k=TOP_K - 1
        )
        # Exploration is deliberately not a candidate answer or retrieval hit.
        exploration = self.recall_index.exploration(
            memories,
            seed=seed,
            as_of=now.isoformat(),
            exclude={r["id"] for r in recalled} | {r["id"] for r in recent},
        )
        if exploration is not None:
            recalled.append(exploration)
        self.store.append(
            "recall",
            {
                "round": round_number,
                "seed": seed,
                "strategy": "lexical-rrf-v1",
                "query_sha256": hashlib.sha256(recall_query.encode()).hexdigest(),
                "diagnostics": recall_diagnostics,
                "exploration_ids": [
                    r["id"] for r in recalled if r.get("recall_role") == "exploration"
                ],
                "recalled_ids": [r["id"] for r in recalled],
            },
            source="curiosity",
        )
        return {
            "beliefs": beliefs,
            "known_gap_identities": [
                {k: b[k] for k in ("belief_id", "subject", "attribute")}
                for b in beliefs
                if b["source"] == "gap"
            ],
            "pending_memories": pending,
            "pending_observations": observations,
            "relevance_candidates": candidates,
            "cached_relevance": cache,
            "recent_thoughts": recent,
            "recalled": recalled,
            "recall_seed": seed,
            "recall_strategy": "lexical-rrf-v1",
            "recall_diagnostics": recall_diagnostics,
            "as_of": now.isoformat(),
        }

    def update(
        self, updates: list[Record], context: Record, *, relevance: list[Record] | None = None
    ) -> None:
        validate_matrix(relevance or [], context, context.get("capabilities", []))
        memories = {m["origin_id"]: m for m in context["pending_memories"]}
        observations = {e["id"]: e for e in context["pending_observations"]}
        for update in updates:
            origin = update["origin_id"]
            if origin in memories:
                memory = memories[origin]
                identity, source, last_verified, memory_id = (
                    origin,
                    "self_report",
                    memory["created_at"],
                    origin,
                )
                if update["belief_id"] != identity:
                    raise ValueError("Memory belief identity mismatch")
                refs = [origin]
            elif origin in observations and observations[origin]["payload"]["data"].get("evidence"):
                event = observations[origin]
                evidence = event_evidence(event, context.get("capabilities", []))
                if evidence is None:
                    raise ValueError("Invalid perception provenance")
                identity = evidence["belief_id"]
                old = next((b for b in self.store.beliefs() if b["belief_id"] == identity), None)
                if update["belief_id"] not in {None, identity}:
                    raise ValueError("Perception target mismatch")
                source, last_verified, memory_id = (
                    evidence["source"],
                    evidence["verified_at"],
                    old["memory_id"] if old else None,
                )
                stamp = datetime.fromisoformat(last_verified)
                if stamp.tzinfo is None or stamp > datetime.now(UTC):
                    raise ValueError("Invalid evidence time")
                if old and stamp <= datetime.fromisoformat(old["last_verified"]):
                    continue
                refs = [origin] + ([identity] if old else [])
                if old and update["conflict"]:
                    self.store.append(
                        "conflict",
                        {
                            "belief_id": identity,
                            "previous_value": old["value"],
                            "observed_value": update["value"],
                            "current_source": source,
                        },
                        source="curiosity",
                        refs=refs,
                    )
            elif origin in observations:
                event = observations[origin]
                identity = event["payload"]["data"]["belief_id"]
                old = next(b for b in self.store.beliefs() if b["belief_id"] == identity)
                if update["belief_id"] != identity:
                    raise ValueError("Observation target mismatch")
                # Tool delivery is not evidence about the person. Semantic labels
                # may change; stable IDs alone determine the update target.
                rows = context.get("cached_relevance", []) + (relevance or [])
                matches = [r for r in rows if r["belief_id"] == identity]
                source_rows = [r for r in matches if r["capability_id"] == event["source"]]
                result = event["payload"]["data"]["result"]
                capability = next(
                    (c for c in context.get("capabilities", []) if c["id"] == event["source"]), None
                )
                verified_at = event["created_at"]
                real_invalid = False
                if capability and capability.get("implementation") == "senses_read_only":
                    try:
                        sampled = datetime.fromisoformat(result.get("sampled_at"))
                        real_invalid = (
                            result.get("status") != "available"
                            or bool(result.get("backfill"))
                            or result.get("source") != capability["call"]["arguments"]["source"]
                            or sampled.tzinfo is None
                            or sampled > datetime.now(UTC)
                            or (
                                old["source"] != "gap"
                                and sampled <= datetime.fromisoformat(old["last_verified"])
                            )
                        )
                        verified_at = sampled.isoformat()
                    except (ValueError, TypeError):
                        real_invalid = True
                if (
                    real_invalid
                    or event["payload"]["data"]["result"].get("synthetic") is True
                    or not source_rows
                    or float(source_rows[-1]["score"]) <= 0
                    or event["payload"]["data"]["result"].get("source_invalid") is True
                    or event["payload"]["data"]["result"].get("invalid_source") is True
                    or event["source"] not in {c["id"] for c in context.get("capabilities", [])}
                    or event["created_at"] <= old["last_verified"]
                    or (matches and all(float(r["score"]) == 0 for r in matches))
                ):
                    self.store.append(
                        "belief_origin",
                        {
                            "origin_id": origin,
                            "belief_id": identity,
                            "status": "attempted_no_evidence",
                        },
                        source="curiosity",
                        refs=[origin, identity],
                    )
                    continue
                source, last_verified, memory_id = "observed", verified_at, old["memory_id"]
                refs = [origin, identity]
                if update["conflict"]:
                    self.store.append(
                        "conflict",
                        {
                            "belief_id": identity,
                            "previous_value": old["value"],
                            "observed_value": update["value"],
                            "current_source": "observed",
                        },
                        source="curiosity",
                        refs=refs,
                    )
            else:
                raise ValueError("Unknown belief origin")
            self.store.append_belief(
                belief_id=identity,
                subject=update["subject"],
                attribute=update["attribute"],
                value=update["value"],
                source=source,
                confidence=INITIAL_CONFIDENCE[source],
                last_verified=last_verified,
                importance=float(update["importance"]),
                memory_id=memory_id,
                refs=refs,
            )
            self.store.append(
                "belief_origin",
                {"origin_id": origin, "belief_id": identity},
                source="curiosity",
                refs=refs,
            )

    def decide(
        self, relevance: list[Record], context: Record, catalog: list[Record], round_number: int
    ) -> Record:
        eligible_ids = {b["belief_id"] for b in context["relevance_candidates"]}
        beliefs = [
            b
            for b in context.get("beliefs", context["relevance_candidates"])
            if b["belief_id"] in eligible_ids and not b.get("pending")
        ]
        validate_matrix(relevance, context, catalog)
        rows = context["cached_relevance"] + relevance
        scores: list[Record] = []
        now = datetime.fromisoformat(context["as_of"])
        question_ids = {
            "scheduler:" + b["belief_id"] for b in beliefs if self._question_blocked(b["belief_id"])
        }
        for belief in beliefs:
            u = 1 - confidence(belief, now)
            ranked: list[Record] = []
            selected_rows: list[Record] = []
            for capability in catalog:
                matches = [
                    r
                    for r in rows
                    if r["belief_id"] == belief["belief_id"]
                    and r["capability_id"] == capability["id"]
                ]
                if belief["belief_id"] in eligible_ids and not matches:
                    raise ValueError("Incomplete relevance matrix")
                r = float(matches[-1]["score"]) if matches else 0.0
                reason = matches[-1]["reason"] if matches else "outside top K"
                blocked = (
                    not capability.get("available", True)
                    or capability["costs_money"]
                    or capability["security_sensitive"]
                )
                voi = u * belief["importance"] * r
                ranked.append(
                    {
                        "capability_id": capability["id"],
                        "r": r,
                        "reason": reason,
                        "raw_voi": voi,
                        "voi": 0.0 if blocked else voi,
                        "available": capability.get("available", True),
                        "permission_required": bool(
                            capability.get("available", True)
                            and (capability["costs_money"] or capability["security_sensitive"])
                        ),
                    }
                )
                if matches:
                    selected_rows.append(matches[-1])
            if selected_rows:
                self.store.append(
                    "relevance_cache",
                    {"fingerprint": fingerprint(belief, catalog), "rows": selected_rows},
                    source="curiosity",
                )
            scores.append(
                {
                    "belief_id": belief["belief_id"],
                    "source": belief["source"],
                    "u": u,
                    "importance": belief["importance"],
                    "uncertainty_priority": u * belief["importance"],
                    "capabilities": ranked,
                }
            )
        # A pending action cannot be blindly repeated, even after restart or an error.
        real_ids = {c["id"] for c in catalog if c.get("implementation") == "senses_read_only"}
        attempted = {
            (b["belief_id"], c["id"])
            for b in beliefs
            for c in catalog
            if self._attempted(b, c, now)
        }
        # Any blocked attempt for this belief also suppresses a source-free ask.
        for b in beliefs:
            with self.store.connection() as db:
                historical = db.execute(
                    "SELECT json_extract(payload,'$.capability_id') FROM entries "
                    "WHERE category='action' AND json_extract(payload,'$.belief_id')=? "
                    "AND json_extract(payload,'$.capability_id') NOT IN ("
                    + ",".join("?" for _ in real_ids)
                    + ") LIMIT 1",
                    (b["belief_id"], *real_ids),
                ).fetchone()
            if historical:
                attempted.add((b["belief_id"], historical[0]))
        delivered = {
            c["id"]
            for c in catalog
            if c["id"] not in real_ids
            and self.store.exists("action", where={"capability_id": c["id"]})
        }
        choices = [
            (c["voi"], s, c)
            for s in scores
            for c in s["capabilities"]
            if (s["belief_id"], c["capability_id"]) not in attempted
            and c["capability_id"] not in delivered
        ]
        choices.sort(key=lambda item: (-item[0], item[1]["belief_id"], item[2]["capability_id"]))
        decision: Record = {"kind": "associate", "reason": "no executable value above threshold"}
        if choices and choices[0][0] > VOI_THRESHOLD:
            _, belief_score, capability_score = choices[0]
            decision = {
                "kind": "execute",
                "belief_id": belief_score["belief_id"],
                "capability_id": capability_score["capability_id"],
                "reason": "highest VOI above threshold",
            }
        else:
            permissions = [
                (c["raw_voi"], s, c)
                for s in scores
                for c in s["capabilities"]
                if c["permission_required"]
                and c["raw_voi"] > VOI_THRESHOLD
                and "scheduler:" + s["belief_id"] not in question_ids
            ]
            askable = {
                b["belief_id"]
                for b in beliefs
                if self.question_eligible(b, now)
                and "scheduler:" + b["belief_id"] not in question_ids
            }
            with self.store.connection() as db:
                source_answerable = {
                    b["belief_id"]
                    for b in beliefs
                    if db.execute(
                        "SELECT 1 FROM curiosity_resolvable_v1 WHERE belief_id=? LIMIT 1",
                        (b["belief_id"],),
                    ).fetchone()
                }
            high = sorted(
                (
                    s
                    for s in scores
                    if s["belief_id"] in askable
                    and s["belief_id"] not in source_answerable
                    and not any(c["r"] > RELEVANCE_THRESHOLD for c in s["capabilities"])
                    and not any(identity == s["belief_id"] for identity, _ in attempted)
                ),
                key=lambda s: s["uncertainty_priority"],
                reverse=True,
            )
            if permissions:
                _, s, c = max(permissions, key=lambda item: item[0])
                decision = {
                    "kind": "pending_sir_permission",
                    "belief_id": s["belief_id"],
                    "capability_id": c["capability_id"],
                    "reason": "paid or security-sensitive; executable VOI is zero",
                }
            elif (
                high
                and high[0]["uncertainty_priority"] > QUESTION_THRESHOLD
                and not any(c["r"] > RELEVANCE_THRESHOLD for c in high[0]["capabilities"])
                and not any(identity == high[0]["belief_id"] for identity, _ in attempted)
            ):
                decision = {
                    "kind": "ask_sir",
                    "belief_id": high[0]["belief_id"],
                    "reason": "high uncertainty without a relevant available source",
                }
        # Queued legacy requests are arbitrated here, after ranked beliefs.
        # A fixed local sample is immutable: one delivery can answer later
        # requests for that same sample without executing it again.
        if decision["kind"] == "associate":
            # Free-form legacy requests cannot bypass scored real-source selection.
            # Query only unresolved requests; historical identities stay in SQL.
            requests: list[Record] = []
            if not real_ids:
                with self.store.connection() as db:
                    cursor = db.execute(
                        "SELECT r.* FROM entries r WHERE r.category='action_request' "
                        "AND NOT EXISTS(SELECT 1 FROM entries q WHERE q.category IN "
                        "('question_state','question_generation_intent',"
                        "'question_generation_failure') "
                        "AND json_extract(q.payload,'$.question_id')="
                        "json_extract(r.payload,'$.question_id')) "
                        "ORDER BY r.seq"
                    )
                    while batch := cursor.fetchmany(256):
                        requests.extend(
                            {**dict(r), "payload": json.loads(r["payload"])} for r in batch
                        )
            for request in requests:
                p = request["payload"]
                # New question gaps are always scored, including fixture catalogs.
                if p["question"].get("gap") or p.get("state") == "awaiting_voi":
                    continue
                identity = p.get("belief_id") or p["question_id"]
                if self._question_blocked(identity):
                    continue
                question = p["question"]
                selected = question["selected_capability"]
                bound = next((b for b in beliefs if b["belief_id"] == identity), None)
                if p.get("belief_id") and bound is None:
                    continue
                if selected is None and (bound is None or not self.question_eligible(bound, now)):
                    continue
                if selected in delivered:
                    self.store.append(
                        "question_state",
                        {
                            "question_id": p["question_id"],
                            "text": question["text"],
                            "state": "evidence_available",
                            "detail": "existing_result",
                        },
                        source="curiosity-scheduler",
                        refs=[request["id"]],
                    )
                    continue
                if selected is not None:
                    cap = next((c for c in catalog if c["id"] == selected), None)
                    if cap is None or not cap.get("available", True):
                        continue
                    decision = {
                        "kind": "pending_sir_permission"
                        if cap["costs_money"] or cap["security_sensitive"]
                        else "execute",
                        "belief_id": p.get("belief_id") or p["question_id"],
                        "capability_id": selected,
                        "question_id": p["question_id"],
                        "text": question["text"],
                        "reason": "queued model request after ranked beliefs",
                    }
                elif not any(
                    c["r"] > RELEVANCE_THRESHOLD for s in scores for c in s["capabilities"]
                ):
                    decision = {
                        "kind": "ask_sir",
                        "belief_id": p.get("belief_id") or p["question_id"],
                        "question_id": p["question_id"],
                        "text": question["text"],
                        "reason": question["missing_information"],
                    }
                if decision["kind"] != "associate":
                    break
        # A prompt subset supplies no evidence about hidden capabilities. Never
        # turn a missing relevance score into zero and ask Sir prematurely.
        if decision["kind"] == "ask_sir" and context.get("context_omitted", {}).get(
            "capabilities", 0
        ):
            decision = {"kind": "associate", "reason": "capability assessment incomplete; deferred"}
        decision["round"] = round_number
        payload: Record = {
            "round": round_number,
            "as_of": context["as_of"],
            "scores": scores,
            "decision": decision,
            "constants": {
                "voi_threshold": VOI_THRESHOLD,
                "question_threshold": QUESTION_THRESHOLD,
                "relevance_threshold": RELEVANCE_THRESHOLD,
                "top_k": TOP_K,
            },
        }
        self.store.append_belief_score(payload)
        return decision


def gap_identity(subject: str, attribute: str) -> str:
    labels = [product_memory.normalized(s) for s in (subject, attribute)]
    return "gap:" + hashlib.sha256(json.dumps(labels, ensure_ascii=False).encode()).hexdigest()[:24]


def validate_gaps(raw: Any) -> list[Record]:
    if not isinstance(raw, list) or len(cast(list[object], raw)) > 16:
        raise ValueError("Bounded knowledge gaps required")
    gaps: list[Record] = []
    for item in cast(list[object], raw):
        # Coerce bare strings to the required object shape.
        # deepseek-flash (and other lean models) sometimes emit a string array
        # instead of the documented object array; treat the string as both
        # attribute and reason so the round does not fail validation entirely.
        if isinstance(item, str):
            text = item.strip()
            if not text or len(text) > 1000:
                raise ValueError("Knowledge gap text")
            item = {
                "subject": "Sir",
                "attribute": text[:200],
                "importance": 0.5,
                "reason": text[:500],
            }
        if not isinstance(item, dict):
            raise ValueError("Knowledge gap object")
        row = cast(Record, item)
        if set(row) != {"subject", "attribute", "importance", "reason"}:
            raise ValueError("Knowledge gap fields")
        if any(
            not isinstance(row[k], str) or not row[k].strip() or len(row[k]) > 1000
            for k in ("subject", "attribute", "reason")
        ):
            raise ValueError("Knowledge gap text")
        if type(row["importance"]) not in {int, float} or not 0 <= row["importance"] <= 1:
            raise ValueError("Knowledge gap importance")
        gaps.append(row)
    return gaps


def validate_updates(raw: Any) -> list[Record]:
    if not isinstance(raw, list) or len(cast(list[object], raw)) > 16:
        raise ValueError("Bounded belief updates required")
    updates: list[Record] = []
    for item in cast(list[object], raw):
        if not isinstance(item, dict) or set(cast(Record, item)) != {
            "origin_id",
            "belief_id",
            "subject",
            "attribute",
            "value",
            "importance",
            "conflict",
        }:
            raise ValueError("Belief update fields")
        row = cast(Record, item)
        if any(
            not isinstance(row[k], str) or not row[k].strip() or len(row[k]) > 3000
            for k in ("origin_id", "subject", "attribute", "value")
        ):
            raise ValueError("Belief update text")
        if row["belief_id"] is not None and not isinstance(row["belief_id"], str):
            raise ValueError("Belief identity")
        if (
            type(row["importance"]) not in {int, float}
            or not 0 <= row["importance"] <= 1
            or type(row["conflict"]) is not bool
        ):
            raise ValueError("Belief update scores")
        updates.append(row)
    return updates


def validate_matrix(
    relevance: list[Record],
    context: Record,
    catalog: list[Record],
    *,
    require_complete: bool = True,
) -> set[tuple[str, str]]:
    """Validate rows strictly; optionally report missing pairs without inventing scores.

    Scheduling and belief updates keep the complete-matrix default. The thought
    boundary may defer those dependent operations while retaining grounded text.
    """
    candidates = {b["belief_id"] for b in context.get("relevance_candidates", [])}
    capability_ids = {c["id"] for c in catalog}
    seen: set[tuple[str, str]] = set()
    cached = [
        r
        for r in context.get("cached_relevance", [])
        if r["belief_id"] in candidates and r["capability_id"] in capability_ids
    ]
    for row in relevance:
        required = {"belief_id", "capability_id", "score", "reason"}
        if set(row) != required:
            raise ValueError("Invalid relevance row")
        key = (row["belief_id"], row["capability_id"])
        if key in seen:
            raise ValueError("Duplicate relevance row")
        seen.add(key)
        if row["belief_id"] not in candidates:
            raise ValueError("Relevance belief outside current candidates")
        if catalog and row["capability_id"] not in capability_ids:
            raise ValueError("Unknown relevance capability")
        if type(row["score"]) not in {int, float} or not 0 <= row["score"] <= 1:
            raise ValueError("Relevance score")
        if not isinstance(row["reason"], str) or not row["reason"].strip():
            raise ValueError("Relevance reason")
    if catalog:
        expected = {
            (belief_id, capability_id)
            for belief_id in candidates
            for capability_id in capability_ids
        }
        missing = expected - seen - {(r["belief_id"], r["capability_id"]) for r in cached}
        if missing and require_complete:
            raise ValueError("Incomplete relevance matrix")
        return missing
    return set()
