"""Bounded, deterministic MindStore -> KnowledgeGraph projection.

No model, private-payload copies, source writes, or causal inference. ``tick``
reads one seq-indexed batch using a read-only connection; only a fully successful
batch advances the private fsync+replace cursor. A crash between a graph commit
and cursor commit is recovered through stable ``mind.entries`` origins. Already
projected nodes are not rewritten on replay (including timestamps).

Goals, evolution and missions use observation + their dedicated KG category:
the graph's node-type vocabulary has no goal/evolution/mission types. A recorded
intention, self-assessment or thought is not a verified fact about the world.
Temporal linking belongs to the existing KG maintenance stage; running its
whole-graph auto_link here would make a bounded ingestion tick unbounded.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import sqlite3
import stat
import tempfile
from collections import Counter
from collections.abc import Generator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import product_home

if TYPE_CHECKING:
    from inner_loop import MindStore
    from knowledge_graph import KnowledgeGraph

Record = dict[str, Any]
ORIGIN_TABLE = "mind.entries"
SUMMARY_LIMIT = 500


class ProjectionRule(NamedTuple):
    """Explicit allowlisted payload paths; dict/list values are never copied."""

    node_type: str
    kg_category: str
    key_fields: tuple[str, ...]
    summary_fields: tuple[str, ...]


CATEGORY_MAP: dict[str, ProjectionRule] = {
    "health_alert": ProjectionRule(
        "health_event",
        "health",
        ("metric",),
        ("metric", "severity", "value", "unit", "threshold", "message"),
    ),
    "health_daily_report": ProjectionRule(
        "health_event",
        "health",
        (),
        (
            "date",
            "non_diagnostic",
            "body",
            "metrics.sleep.status",
            "context.late_computer_activity",
        ),
    ),
    "event": ProjectionRule("observation", "sense", ("kind",), ("kind",)),
    "thought": ProjectionRule("observation", "jarvis_self", ("kind",), ("kind", "text", "round")),
    "tool_call": ProjectionRule(
        "observation",
        "tool",
        ("tool_name", "name"),
        ("tool_name", "status", "duration_ms", "risk", "error_type"),
    ),
    "goal_set": ProjectionRule("observation", "goal", ("goal_id",), ("goal_id", "goal")),
    "goal_progress": ProjectionRule(
        "observation", "goal", ("goal_id",), ("goal_id", "progress", "completed", "note")
    ),
    "goal_completed": ProjectionRule("observation", "goal", ("goal_id",), ("goal_id", "summary")),
    "evolution_proposal": ProjectionRule(
        "observation",
        "evolution",
        ("skill_name", "topic", "module", "proposal_id", "title"),
        ("topic", "summary", "skill_name", "estimated_value", "requires_human_approval"),
    ),
    "evolution_cycle": ProjectionRule(
        "observation",
        "evolution",
        ("cycle_id",),
        (
            "status",
            "candidate_count",
            "proposal_count",
            "requires_human_approval",
            "delivery_verified",
        ),
    ),
    "self_evaluation": ProjectionRule(
        "observation",
        "evolution",
        ("module",),
        ("module", "domain", "current_version", "error_type", "projected", "skipped", "unmapped"),
    ),
    "evolution_research_session": ProjectionRule(
        "observation",
        "evolution",
        (),
        ("topics.count", "domain_modules.count", "proposal_ids.count", "errors.count"),
    ),
    "evolution_research_error": ProjectionRule(
        "observation",
        "evolution",
        ("module", "domain"),
        ("stage", "module", "domain", "error_type", "status"),
    ),
    "evolution_weekly_completed": ProjectionRule(
        "observation",
        "evolution",
        ("week",),
        ("week", "proposal_ids.count", "status", "proposal_count", "completed_at"),
    ),
    "evolution_review": ProjectionRule(
        "observation",
        "evolution",
        ("review.skill_name", "evidence.topic"),
        (
            "review.useful",
            "review.novel",
            "review.evidence_sufficient",
            "review.requires_core_change",
            "review.estimated_value",
            "review.rationale",
        ),
    ),
    "evolution_proposal_approval": ProjectionRule(
        "observation", "evolution", ("proposal_id",), ("proposal_id", "reviewer")
    ),
    "evolution_proposal_processed": ProjectionRule(
        "observation",
        "evolution",
        ("proposal_id",),
        ("proposal_id", "processed", "skill_name", "version"),
    ),
    "vision_input": ProjectionRule("observation", "vision", (), ("description", "observed_at")),
    "core_mission": ProjectionRule("observation", "mission", ("key",), ("content", "importance")),
    "action_request": ProjectionRule(
        "observation", "task", ("capability_id",), ("capability_id", "state", "status", "round")
    ),
    "action": ProjectionRule(
        "observation", "task", ("capability_id",), ("capability_id", "state", "status", "round")
    ),
    "knowledge_gap": ProjectionRule(
        "observation",
        "jarvis_self",
        ("attribute", "belief_id"),
        ("subject", "attribute", "importance", "reason"),
    ),
    "gap_retired": ProjectionRule(
        "observation", "jarvis_self", ("belief_id",), ("belief_id", "reason", "status")
    ),
    "question_state": ProjectionRule(
        "observation", "jarvis_self", ("question_id",), ("question_id", "state", "status", "reason")
    ),
}

# Operational receipts/caches and full model transport bodies are not knowledge.
# New category names do NOT silently fall into this list: they are counted below
# and reported by self_evaluate so new feature owners must decide their mapping.
SKIP_CATEGORIES = frozenset(
    {
        "recall",
        "context_scan",
        "context_error",
        "belief_origin",
        "belief_score",
        "daemon_sense_merged",
        "daemon_sense_consumed",
        "model_call",
        "model_response",
        "model_error",
        "validation_error",
        "validation_degraded",
        "relevance_cache",
        "outreach_intent",
        "outreach_reconciled",
        "question_generation_intent",
    }
)
# Short public alias for callers inspecting the policy.
SKIP = SKIP_CATEGORIES

_HEALTH_KINDS = frozenset(
    {
        "heart_rate",
        "resting_heart_rate",
        "hrv",
        "steps",
        "sleep",
        "health_metric",
        "oxygen_saturation",
        "blood_oxygen_saturation",
        "spo2",
        "respiratory_rate",
        "body_temperature",
        "temperature",
        "active_energy",
        "walking_heart_rate",
    }
)
_VISION_KINDS = frozenset({"camera", "vision", "image", "screenshot"})


def _get(payload: Record, path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if part == "count" and isinstance(value, (list, dict)):
            value = len(cast(list[Any] | Record, value))
            continue
        if not isinstance(value, dict):
            return None
        value = cast(Record, value).get(part)
    return value


def _scalar(value: Any, limit: int = 180) -> str:
    if isinstance(value, str):
        # Bound work before normalization even for a 500 KB field.
        return " ".join(value[: limit * 2].split())[:limit]
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)[:limit]
    if type(value) is float and math.isfinite(value):
        return str(value)[:limit]
    return ""


def _summary(category: str, payload: Record, fields: tuple[str, ...]) -> str:
    pieces = [category]
    for path in fields:
        value = _scalar(_get(payload, path))
        if value:
            pieces.append(f"{path}={value}")
    return "; ".join(pieces)[:SUMMARY_LIMIT]


def _event(payload: Record) -> tuple[str, str, str, Record]:
    """Prefer actual sense_event.kind over the generic tool_result envelope."""
    data = payload.get("data")
    data = cast(Record, data) if isinstance(data, dict) else {}
    sense = data.get("sense_event", payload.get("sense_event"))
    if isinstance(sense, dict):
        sense = cast(Record, sense)
        kind = _scalar(sense.get("kind"), 80) or "unknown"
        source = _scalar(sense.get("source"), 120) or "unknown"
        category = "sense"
        if kind in _HEALTH_KINDS or source == "health":
            category = "health"
        elif kind == "location":
            category = "location"
        elif kind in _VISION_KINDS:
            category = "vision"
        metadata: Record = {"sense_kind": kind, "sense_source": source}
        observed = _scalar(sense.get("observed_at"), 80)
        if observed:
            metadata["event_time"] = observed
        detail = sense.get("payload")
        detail = cast(Record, detail) if isinstance(detail, dict) else {}
        # Never copy URLs, browser titles, coordinates, transcripts, or event
        # lists. Summaries retain modality, measurement and status; full source
        # is reachable via evidence_ids in mind.db when detailed reasoning needs it.
        body = _summary(
            "event",
            {
                "sense_kind": kind,
                "sense_source": source,
                **{
                    field: detail.get(field)
                    for field in (
                        "metric",
                        "value",
                        "unit",
                        "status",
                        "app",
                        "duration_seconds",
                        "count",
                    )
                },
            },
            (
                "sense_kind",
                "sense_source",
                "metric",
                "value",
                "unit",
                "status",
                "app",
                "duration_seconds",
                "count",
            ),
        )
        return category, f"sense:{kind}", body, metadata
    kind = _scalar(payload.get("kind"), 80) or "unknown"
    category = "vision" if kind in _VISION_KINDS else "tool" if kind == "tool_result" else "sense"
    body = _summary(
        "event",
        {"kind": kind, **{key: data.get(key) for key in ("role", "status", "tool_name", "round")}},
        ("kind", "role", "status", "tool_name", "round"),
    )
    return category, f"event:{kind}", body, {"event_kind": kind}


def _validate_file(path: Path) -> None:
    product_home.safe_absolute(path)
    if path.exists():
        info = path.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("Projector files must be private owned regular files (0600)")


def _empty_state() -> Record:
    return {
        "version": 1,
        "cursor": 0,
        "processed": 0,
        "projected": 0,
        "skipped": 0,
        "unmapped": 0,
        "projected_by_category": {},
        "skipped_by_category": {},
        "unmapped_categories": {},
    }


class KGProjector:
    """One bounded batch per tick. Invoke from Wiring.background()."""

    def __init__(
        self,
        root: Path,
        mind_store: MindStore,
        knowledge_graph: KnowledgeGraph,
        *,
        batch_limit: int = 200,
    ) -> None:
        if type(batch_limit) is not int or batch_limit < 1:
            raise ValueError("batch_limit must be a positive integer")
        self.root = product_home.validate_root(Path(root))
        product_home.make_private(self.root)
        self.mind_store = mind_store
        self.knowledge_graph = knowledge_graph
        if Path(mind_store.path) != self.root / "mind.db":
            raise ValueError("MindStore must belong to projector root")
        if Path(knowledge_graph.path) != self.root / "knowledge_graph.db":
            raise ValueError("KnowledgeGraph must belong to projector root")
        self.batch_limit = batch_limit
        self.state_path = self.root / "kg_projector_state.json"
        self.lock_path = self.root / ".kg_projector.lock"
        self.last_error_type: str | None = None
        self.last_durability_error_type: str | None = None
        _validate_file(self.state_path)
        _validate_file(self.lock_path)

    @contextmanager
    def _lock(self) -> Generator[bool]:
        _validate_file(self.lock_path)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("Unsafe projector lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _load_state(self) -> Record:
        _validate_file(self.state_path)
        try:
            fd = os.open(self.state_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return _empty_state()
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            state = json.load(stream)
        if type(state) is not dict:
            raise ValueError("Invalid projector state")
        state = cast(Record, state)
        if state.get("version") != 1:
            raise ValueError("Invalid projector state version")
        for key in ("cursor", "processed", "projected", "skipped", "unmapped"):
            if type(state.get(key)) is not int or state[key] < 0:
                raise ValueError("Invalid projector cursor or counters")
        for key in ("projected_by_category", "skipped_by_category", "unmapped_categories"):
            if type(state.get(key)) is not dict or any(
                type(name) is not str or type(count) is not int or count < 0
                for name, count in state[key].items()
            ):
                raise ValueError("Invalid projector category counters")
        if (
            state["processed"] != state["projected"] + state["skipped"] + state["unmapped"]
            or sum(state["projected_by_category"].values()) != state["projected"]
            or sum(state["skipped_by_category"].values()) != state["skipped"]
            or sum(state["unmapped_categories"].values()) != state["unmapped"]
        ):
            raise ValueError("Inconsistent projector accounting")
        return state

    def _save_state(self, state: Record) -> str | None:
        _validate_file(self.state_path)
        fd, name = tempfile.mkstemp(prefix=".kg-projector-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, allow_nan=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.state_path)
            # Atomic visibility has committed at replace. A directory-flush
            # error after that point cannot safely roll it back: report a
            # durability warning rather than falsely claiming an aborted batch.
            # If a crash loses this rename, stable origins still replay safely.
            try:
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError as exc:
                return type(exc).__name__
            return None
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _rows(self, cursor: int) -> list[sqlite3.Row]:
        _validate_file(Path(self.mind_store.path))
        uri = Path(self.mind_store.path).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            return db.execute(
                "SELECT seq,id,category,created_at,source,payload FROM entries "
                "WHERE seq>? ORDER BY seq LIMIT ?",
                (cursor, self.batch_limit),
            ).fetchall()

    def _existing(self, rows: list[sqlite3.Row]) -> set[str]:
        ids = [row["id"] for row in rows if row["category"] in CATEGORY_MAP]
        if not ids:
            return set()
        # Chunk even if a caller chooses a batch larger than SQLite's variable
        # limit. This queries only origins in the bounded input, never all nodes.
        existing: set[str] = set()
        with self.knowledge_graph.connection() as db:
            for offset in range(0, len(ids), 400):
                part = ids[offset : offset + 400]
                placeholders = ",".join("?" for _ in part)
                existing.update(
                    row[0]
                    for row in db.execute(
                        "SELECT origin_id FROM nodes WHERE origin_table=? AND origin_id IN ("
                        + placeholders
                        + ")",
                        [ORIGIN_TABLE, *part],
                    )
                )
        return existing

    def _project(self, row: sqlite3.Row) -> None:
        category = row["category"]
        rule = CATEGORY_MAP[category]
        payload = json.loads(row["payload"])
        if type(payload) is not dict:
            # A malformed mapped row is not acknowledged; caller can repair the
            # schema/mapping rather than silently losing that category's data.
            raise ValueError("Mapped mind payload must be a JSON object")
        payload = cast(Record, payload)
        metadata: Record = {
            "mind_category": category,
            "mind_seq": row["seq"],
            "event_time": row["created_at"],
            "projection_version": 1,
        }
        kg_category = rule.kg_category
        key = category
        for field in rule.key_fields:
            text = _scalar(_get(payload, field), 100)
            if text:
                key = f"{category}:{text}"
                break
        value = _summary(category, payload, rule.summary_fields)
        if category == "event":
            kg_category, key, value, extra = _event(payload)
            metadata.update(extra)
        elif category == "health_alert":
            observed = _scalar(payload.get("observed_at"), 80)
            if observed:
                metadata["event_time"] = observed
        # Confidence describes faithful recording, NOT truth/clinical certainty.
        # Keep all projected internal intentions below verified-fact thresholds.
        self.knowledge_graph.add_node(
            rule.node_type,
            kg_category,
            key,
            value,
            0.7,
            row["source"],
            evidence_ids=[row["id"]],
            origin_table=ORIGIN_TABLE,
            origin_id=row["id"],
            metadata=metadata,
        )

    def tick(self) -> Record:
        """Return per-batch counts; pre-publication failures do not advance cursor.

        A post-replace directory-flush error is a committed batch with an
        explicit durability_warning, not a claim that the cursor stayed put.

        Graph commits before the JSON cursor. Some nodes can exist after a failed
        batch; their origins recover them next time, while committed counters and
        cursor remain unchanged. No source entry is consumed or rewritten.
        """
        try:
            with self._lock() as acquired:
                state = self._load_state()
                result: Record = {
                    "processed": 0,
                    "projected": 0,
                    "skipped": 0,
                    "unmapped": 0,
                    "cursor": state["cursor"],
                    "projected_by_category": {},
                    "busy": not acquired,
                }
                if not acquired:
                    return result
                rows = self._rows(state["cursor"])
                if not rows:
                    self.last_error_type = None
                    return result
                existing = self._existing(rows)
                by_kind: dict[str, Counter[str]] = {
                    "projected": Counter(),
                    "skipped": Counter(),
                    "unmapped": Counter(),
                }
                for row in rows:
                    category = row["category"]
                    if category in SKIP_CATEGORIES:
                        by_kind["skipped"][category] += 1
                    elif category not in CATEGORY_MAP:
                        by_kind["unmapped"][category] += 1
                    else:
                        if row["id"] not in existing:
                            self._project(row)
                        by_kind["projected"][category] += 1
                for kind, counter_key in (
                    ("projected", "projected_by_category"),
                    ("skipped", "skipped_by_category"),
                    ("unmapped", "unmapped_categories"),
                ):
                    count = sum(by_kind[kind].values())
                    state[kind] += count
                    result[kind] = count
                    counts = Counter[str](cast(dict[str, int], state[counter_key]))
                    counts.update(by_kind[kind])
                    state[counter_key] = dict(sorted(counts.items()))
                state["processed"] += len(rows)
                state["cursor"] = rows[-1]["seq"]
                warning = self._save_state(state)
                self.last_durability_error_type = warning
                if warning:
                    result["durability_warning"] = warning
                result.update(
                    processed=len(rows),
                    cursor=state["cursor"],
                    projected_by_category=dict(sorted(by_kind["projected"].items())),
                )
                self.last_error_type = None
                return result
        except Exception as exc:
            # Never copy exception messages: SQLite or adapters may expose data.
            self.last_error_type = type(exc).__name__
            raise

    def self_evaluate(self) -> Record:
        """Committed progress and actionable policy gaps, without payload content."""
        state = self._load_state()
        unknown = state["unmapped_categories"]
        limitations = [
            "Allowlisted scalar summaries (max 500 characters), not complete private payloads; "
            "use evidence_ids to retrieve source details.",
            "Observation records are not verified world facts; reported goal completion and "
            "self-evaluation do not prove success.",
            "Cursor is append-only: mapping changes do not backfill acknowledged entries; "
            "an explicit replay/rebuild is required.",
            "No inferred causal edges. Temporal linking is delegated to KG maintenance, "
            "not an unbounded auto_link call on every ingestion batch.",
            "Existing specialized projectors can hold semantic duplicates under other origins; "
            "this projector deduplicates only mind.entries origins.",
        ]
        if unknown:
            limitations.append(
                "Unmapped mind categories require explicit CATEGORY_MAP or "
                "SKIP_CATEGORIES policy: " + ", ".join(sorted(unknown))
            )
        return {
            "module": "kg_projector",
            "domain": (
                "incremental event-to-knowledge-graph projection provenance and data minimization"
            ),
            "current_version": "1.0.0",
            "last_evaluated_at": datetime.now(UTC).isoformat(),
            **{
                key: state[key]
                for key in (
                    "cursor",
                    "processed",
                    "projected",
                    "skipped",
                    "unmapped",
                    "projected_by_category",
                    "skipped_by_category",
                    "unmapped_categories",
                )
            },
            "mapping_required": bool(unknown),
            "last_error_type": self.last_error_type,
            "last_durability_error_type": self.last_durability_error_type,
            "known_limitations": limitations,
            "improvement_directions": [
                "Review every unmapped category with its feature owner, add an explicit safe "
                "summary mapping, then replay the source without changing stable origins.",
                "Version summary policies and support targeted, bounded replay "
                "for mapping updates.",
                "Measure batch latency/backlog, and coalesce specialist projections by evidence "
                "without conflating recorded observations with confirmed facts.",
            ],
        }
