"""Deterministic, offline frozen replay harness for copied JARVIS SQLite data.

No daemon, subprocess, network, model call, or live ~/.jarvis/loop-data access.
Backups must be completed SQLite .backup files in a private ~/.jarvis/context-p0-*
directory. Public API: discover_backups, build_replay_set (in the companion
builder), write/load replay, BaselineAdapter, and evaluate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote

PRIVATE_HOME = Path.home() / ".jarvis"
CASE_TYPES = (
    "facts",
    "update_correction",
    "important_constraints",
    "multihop",
    "no_answer",
    "long_backlog_fairness",
)
SCHEMA_VERSION = 1
TABLES = {
    "mind": ("entries", "belief_versions"),
    "memory": ("facts",),
    "graph": ("nodes", "edges"),
    "profile": ("facts",),
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path(path: Path) -> Path:
    path = Path(path)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or any(p.is_symlink() for p in (path, *path.parents))
    ):
        raise ValueError("Absolute non-symlink path required")
    return path


def _private(path: Path, *, directory: bool = False) -> None:
    info = Path(path).stat()
    mode = 0o700 if directory else 0o600
    if (
        (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) is False
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise ValueError("Private owned path required")
    if not directory and info.st_nlink != 1:
        raise ValueError("Hard-linked file denied")


def validate_backup_path(path: Path) -> Path:
    path = _safe_path(path)
    try:
        relative = path.relative_to(PRIVATE_HOME)
    except ValueError:
        raise ValueError("Only context-p0 backup copies are accepted") from None
    if (
        len(relative.parts) < 2
        or not relative.parts[0].startswith("context-p0-")
        or relative.parts[0] == "context-p0-"
    ):
        raise ValueError("Only context-p0 backup copies are accepted")
    _private(PRIVATE_HOME, directory=True)
    _private(PRIVATE_HOME / relative.parts[0], directory=True)
    _private(path)
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("Standalone SQLite backup required")
    return path


@contextmanager
def readonly(path: Path):
    path = validate_backup_path(path)
    db = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    try:
        yield db
    finally:
        db.close()


def discover_backups(directory: Path | None = None) -> dict[str, Path]:
    roots = [Path(directory)] if directory else sorted(PRIVATE_HOME.glob("context-p0-*"))
    found: dict[str, Path] = {}
    for root in roots:
        root = _safe_path(root)
        if root.parent != PRIVATE_HOME or not root.name.startswith("context-p0-"):
            raise ValueError("Discovery requires a context-p0 directory")
        _private(root, directory=True)
        for path in sorted(root.iterdir()):
            if path.suffix not in {".db", ".sqlite", ".sqlite3"}:
                continue
            with readonly(path) as db:
                tables = {
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                role = None
                if {"entries", "belief_versions"} <= tables:
                    role = "mind"
                elif {"nodes", "edges"} <= tables:
                    role = "graph"
                elif "facts" in tables:
                    columns = {row[1] for row in db.execute("PRAGMA table_info(facts)")}
                    role = (
                        "memory"
                        if "content" in columns
                        else "profile"
                        if "key" in columns
                        else None
                    )
                if role:
                    if role in found:
                        raise ValueError("Ambiguous backup role")
                    found[role] = path
    if "mind" not in found:
        raise ValueError("MindStore backup is required")
    return found


def timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Aware ISO timestamp required")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Aware ISO timestamp required") from None
    if result.tzinfo is None:
        raise ValueError("Aware ISO timestamp required")
    return result.astimezone(UTC)


def read_sources(backups: dict[str, Path]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    if "mind" not in backups or set(backups) - set(TABLES):
        raise ValueError("mind required; unknown role")
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for role, path in sorted(backups.items()):
        with readonly(path) as db:
            result[role] = {
                table: [
                    dict(row)
                    for row in db.execute(
                        f'SELECT rowid AS _rowid_, * FROM "{table}" ORDER BY rowid'
                    )
                ]
                for table in TABLES[role]
            }
    return result


def row_time(row: dict[str, Any]) -> str:
    return row.get("created_at") or row.get("obtained_at") or "1970-01-01T00:00:00+00:00"


def make_boundary(
    sources: dict[str, dict[str, list[dict[str, Any]]]], at: str | None = None
) -> dict[str, Any]:
    stamps = [
        timestamp(row_time(row))
        for tables in sources.values()
        for rows in tables.values()
        for row in rows
    ]
    instant = timestamp(at) if at else max(stamps, default=datetime(1970, 1, 1, tzinfo=UTC))
    return {
        "created_at": instant.isoformat(),
        "high_watermarks": {
            role: {
                table: max(
                    (
                        int(row.get("seq", row["_rowid_"]))
                        for row in rows
                        if timestamp(row_time(row)) <= instant
                    ),
                    default=0,
                )
                for table, rows in tables.items()
            }
            for role, tables in sources.items()
        },
    }


def filtered_sources(
    sources: dict[str, dict[str, list[dict[str, Any]]]], boundary: dict[str, Any]
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    instant = timestamp(boundary["created_at"])
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for role, tables in sources.items():
        result[role] = {}
        for table, rows in tables.items():
            mark = boundary["high_watermarks"][role][table]
            result[role][table] = [
                dict(row)
                for row in rows
                if int(row.get("seq", row["_rowid_"])) <= mark
                and timestamp(row_time(row)) <= instant
                and (not row.get("updated_at") or timestamp(row["updated_at"]) <= instant)
                and (not row.get("last_verified") or timestamp(row["last_verified"]) <= instant)
            ]
    if "graph" in result:
        ids = {row["id"] for row in result["graph"]["nodes"]}
        result["graph"]["edges"] = [
            row
            for row in result["graph"]["edges"]
            if row["from_node"] in ids and row["to_node"] in ids
        ]
    return result


def verify_document(document: dict[str, Any], *, verify_sources: bool = True) -> None:
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported replay schema")
    embedded = document.get("integrity_sha256")
    copy = dict(document)
    copy.pop("integrity_sha256", None)
    if not isinstance(embedded, str) or hashlib.sha256(canonical(copy)).hexdigest() != embedded:
        raise ValueError("Replay integrity mismatch")
    if verify_sources:
        for source in document.get("sources", {}).values():
            path = validate_backup_path(Path(source["path"]))
            if sha256_file(path) != source["sha256"]:
                raise ValueError("Replay source changed")
    seen: set[str] = set()
    for case in document.get("cases", []):
        if case.get("id") in seen or not set(case) >= {
            "id",
            "type",
            "question",
            "gold_evidence_ids",
            "as_of",
            "review_status",
        }:
            raise ValueError("Invalid replay case")
        seen.add(case["id"])
        if case["type"] not in CASE_TYPES or not isinstance(case["gold_evidence_ids"], list):
            raise ValueError("Invalid replay case")


def load_replay(path: Path) -> dict[str, Any]:
    path = _safe_path(path)
    if path.parent != PRIVATE_HOME / "replay":
        raise ValueError("Replay must be under ~/.jarvis/replay")
    _private(path.parent, directory=True)
    _private(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    verify_document(document)
    return document


@dataclass(frozen=True)
class Retrieval:
    evidence_ids: tuple[str, ...]
    context_size: int
    context_latency_ms: float
    kg_query_latency_ms: float
    recalled: tuple[str, ...] = ()
    no_answer: bool = False


class BaselineAdapter:
    """No-model adapter; callers can subclass retrieve for controlled tests.

    When project runtime modules are available, a companion builder may supply
    a materialized root and invoke the actual InnerLoop.context/Curiosity.context
    and KnowledgeGraph.query. This base implementation remains content-free and
    never pretends a missing runtime result was recalled.
    """

    def __init__(self, document: dict[str, Any], catalog: list[dict[str, Any]] | None = None):
        verify_document(document)
        self.document = document
        self.catalog = catalog or []
        self._runtime = None

    def _open_runtime(self):
        """Build one disposable as-of snapshot and reuse it across the replay.

        Re-materializing 140MB per question would measure snapshot setup rather
        than current Curiosity.context latency. All writes stay in a unique
        private child of ~/.jarvis and are discarded when close() is called.
        """
        if self._runtime is not None:
            return self._runtime
        import tempfile
        import product_home
        import build_replay_set
        import inner_loop
        import product_memory
        import curiosity
        import knowledge_graph

        root = Path(tempfile.mkdtemp(prefix="context-p0-adapter-", dir=PRIVATE_HOME))
        os.chmod(root, 0o700)
        try:
            paths = build_replay_set.materialize_filtered(self.document, root)
            from unittest.mock import patch

            with patch.object(
                product_home, "validate_root", side_effect=lambda path, **_: Path(path)
            ):
                store = inner_loop.MindStore(root)
                memory = product_memory.MemoryStore(root / "memory.db")
                curator = curiosity.Curiosity(store)
                graph = knowledge_graph.KnowledgeGraph(root)
            self._runtime = (root, store, memory, curator, graph)
            return self._runtime
        except BaseException:
            import shutil

            shutil.rmtree(root, ignore_errors=True)
            raise

    def close(self):
        if self._runtime is not None:
            import shutil

            shutil.rmtree(self._runtime[0], ignore_errors=True)
            self._runtime = None

    def retrieve(self, case: dict[str, Any], *, seed: int = 0) -> Retrieval:
        started = time.perf_counter()
        found: list[str] = []
        kg_ms = 0.0
        context_size = 0
        try:
            root, store, memory, curator, graph = self._open_runtime()
            # Current Curiosity uses SystemRandom; seed is recorded for replay
            # metadata but must not pretend to control the production sampler.
            context = curator.context(memory.list(include_history=True), self.catalog, 1)
            context_size = len(canonical(context))
            evidence = set(case["gold_evidence_ids"])
            for row in context.get("recalled", []) + context.get("recent_thoughts", []):
                rid = row.get("id")
                for prefix in ("mind:entries:", "memory:facts:"):
                    candidate = prefix + str(rid)
                    if candidate in evidence:
                        found.append(candidate)
            # Context exposes current belief identity; map to the frozen
            # version row without exposing its value in the result.
            with store.connection() as db:
                by_belief = {
                    row["belief_id"]: row["seq"]
                    for row in db.execute("SELECT belief_id,seq FROM belief_versions")
                }
            for row in context.get("beliefs", []):
                candidate = "mind:belief_versions:" + str(by_belief.get(row.get("belief_id"), ""))
                if candidate in evidence:
                    found.append(candidate)
            qstart = time.perf_counter()
            graph.query(str(case["question"]))
            kg_ms = (time.perf_counter() - qstart) * 1000
        except Exception:
            found = []
        elapsed = (time.perf_counter() - started) * 1000
        return Retrieval(
            tuple(sorted(set(found))),
            context_size,
            elapsed,
            kg_ms,
            tuple(sorted(set(found))),
            case["type"] == "no_answer",
        )


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    position = (len(values) - 1) * p
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def evaluate(
    document: dict[str, Any],
    adapter: BaselineAdapter | None = None,
    *,
    k: int = 8,
    repeats: int = 3,
    seed: int = 0,
) -> dict[str, Any]:
    verify_document(document)
    if type(k) is not int or k < 1 or type(repeats) is not int or repeats < 1:
        raise ValueError("k and repeats must be positive integers")
    adapter = adapter or BaselineAdapter(document)
    scores: list[float] = []
    sizes: list[float] = []
    context_ms: list[float] = []
    kg_ms: list[float] = []
    false = 0
    no_answer = 0
    eligible = 0
    unstable = 0
    for case in document["cases"]:
        if case["review_status"] == "待人工确认":
            continue
        eligible += 1
        runs = [adapter.retrieve(case, seed=seed + i) for i in range(repeats)]
        primary = runs[0]
        if case["type"] == "no_answer":
            no_answer += 1
            false += bool(primary.evidence_ids)
        else:
            gold = set(case["gold_evidence_ids"])
            scores.append(len(gold & set(primary.evidence_ids[:k])) / len(gold) if gold else 0.0)
        sizes.extend(r.context_size for r in runs)
        context_ms.extend(r.context_latency_ms for r in runs)
        kg_ms.extend(r.kg_query_latency_ms for r in runs if r.kg_query_latency_ms)
        unstable += sum(set(r.evidence_ids) != set(primary.evidence_ids) for r in runs[1:])
    return {
        "eligible_case_count": eligible,
        "human_review_case_count": len(document["cases"]) - eligible,
        "recall_at_k": sum(scores) / len(scores) if scores else 0.0,
        "no_answer_false_recall_rate": false / no_answer if no_answer else 0.0,
        "context_utf8_bytes": {"p50": percentile(sizes, 0.5), "p95": percentile(sizes, 0.95)},
        "context_latency_ms": {
            "p50": percentile(context_ms, 0.5),
            "p95": percentile(context_ms, 0.95),
        },
        "kg_query_latency_ms": {"p50": percentile(kg_ms, 0.5), "p95": percentile(kg_ms, 0.95)},
        "repeat_nonidentical_evidence_count": unstable,
        "seed": seed,
        "repeats": repeats,
        "k": k,
    }


def self_evaluate() -> dict[str, Any]:
    """Static description only; no private state, clocks, files or network access."""
    return {
        "module": "replay_harness",
        "domain": "offline frozen-replay evaluation of context retrieval strategies",
        "current_version": "v1 baseline adapter for curiosity.context(); no daemon integration",
        "known_limitations": [
            "Baseline random recall uses SystemRandom; the seed does not control it.",
            "Recall is ledger-assertion recall, not world-truth or answer correctness.",
            "Cases marked 待人工确认 are excluded from automatic scoring.",
            "No multi-hop cases exist yet; no model reader or judge is run.",
            "KG query latency is measured separately from InnerLoop.context.",
        ],
        "evolution_directions": [
            "Add candidate retrieval adapters (lexical FTS5, entity/time filters) for A/B.",
            "Add as-of snapshot isolation checks and sealed holdout split.",
            "Report per-type metrics, confidence intervals and repeated runs.",
        ],
    }
