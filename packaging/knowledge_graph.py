"""Provenance-preserving SQLite fusion graph above profile and hypotheses.

This module does not replace or write either source ledger, resolve evidence
IDs, install background jobs, or call a model. Edges point cause -> effect
(including ``caused_by``, following the public add_edge example). Inferred
relations are candidates, never medical/causal findings. Source-origin nodes
are upserted; anonymous manual observations remain distinct.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
import uuid
from collections import defaultdict, deque
from collections.abc import Generator, Sequence
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from sir_profile import (
    PrivateStore,
    require_text,
    utc_now,
    validate_confidence,
    validate_evidence_ids,
    validate_topic,
)

if TYPE_CHECKING:
    from hypothesis_engine import HypothesisEngine
    from sir_profile import SirProfile

# The graph includes JARVIS/system knowledge; SirProfile keeps its own policy.
KG_CATEGORIES = frozenset(
    {
        "event",
        "family",
        "habit",
        "health",
        "identity",
        "location",
        "preference",
        "work",
        "jarvis_self",
        "mission",
        "tool",
        "skill",
        "evolution",
        "goal",
        "task",
        "device",
        "vision",
        "sense",
        "model",
        "external",
        "persona",
        "affect",
        "observability",
    }
)
NODE_TYPES = frozenset({"fact", "hypothesis", "observation", "health_event", "behavior_pattern"})
EDGE_TYPES = frozenset(
    {"caused_by", "correlates_with", "supports", "contradicts", "evolves_to", "precedes"}
)
SYMMETRIC_EDGES = frozenset({"correlates_with", "contradicts"})
Record = dict[str, Any]


def _json(value: Any) -> str:
    """Reject lossy object keys and non-JSON metadata, not just invalid syntax."""

    def check(item: Any) -> None:
        if isinstance(item, dict):
            mapping = cast(dict[Any, Any], item)
            if any(type(key) is not str for key in mapping):
                raise ValueError("JSON object keys must be strings")
            for child in mapping.values():
                check(child)
        elif isinstance(item, list):
            for child in cast(list[Any], item):
                check(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("Metadata must contain JSON values")

    try:
        check(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("Invalid JSON metadata") from exc


def _search_text(text: str) -> str:
    """One normalization for both indexed fields and bound query literals."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text)).casefold()


def _gram_set(*fields: str) -> list[str]:
    # Include single characters too: legacy queries accept e.g. 与 and 的.
    # Fields stay separate so a category/key boundary cannot create a match.
    grams: set[str] = set()
    for field in fields:
        text = _search_text(field)
        grams.update(text)
        grams.update(text[i : i + 2] for i in range(len(text) - 1))
    return sorted(grams)


def _short_grams(*fields: str) -> str:
    return json.dumps(_gram_set(*fields), ensure_ascii=False)


def _short_terms(*fields: str) -> str:
    """Space-joined grams for the nodes_short FTS5 document (one token each).

    _search_text strips every non-word character, so a gram never contains a
    unicode61 separator; each gram is exactly one token and MATCH "gram" is an
    exact gram-membership test equivalent to the former nodes_bigram table.
    """
    return " ".join(_gram_set(*fields))


def _query_terms(question: str) -> tuple[list[str], str]:
    """Never allow user text to become MATCH syntax (including OR/NEAR)."""
    short: set[str] = set()
    long: set[str] = set()
    for part in re.findall(
        r"[\u3400-\u9fff]+|[\w]+", unicodedata.normalize("NFKC", question).casefold()
    ):
        part = _search_text(part)
        if not part:
            continue
        if len(part) < 3:
            short.add(part)
        else:
            # A quoted >=3-character span is tokenized into trigrams by
            # FTS5 itself. OR-ing its overlapping trigrams would turn e.g.
            # unique no-answer nonces into matches for common "con"/"ion".
            long.add(part)
            # Preserve the existing Chinese bigram recall on long questions.
            if re.fullmatch(r"[\u3400-\u9fff]+", part):
                long.update(part[i : i + 3] for i in range(len(part) - 2))
                short.update(part[i : i + 2] for i in range(len(part) - 1))
    expression = " OR ".join('"' + gram.replace('"', '""') + '"' for gram in sorted(long))
    return sorted(short), expression


def _decode(row: sqlite3.Row) -> Record:
    result = dict(row)
    result["evidence_ids"] = json.loads(result["evidence_ids"])
    if "metadata" in result:
        result["metadata"] = json.loads(result["metadata"])
    return result


class KnowledgeGraph(PrivateStore):
    """Private, transactional graph; stable origins and idempotent edge writes."""

    @contextmanager
    def connection(
        self, *, write: bool = False, profile_path: Path | None = None
    ) -> Generator[sqlite3.Connection]:
        """Serialize each write transaction; SQLite arbitrates other processes.

        Keep this policy local: other PrivateStore users retain their journal
        settings. Readers have independent connections and do not take the lock.
        """
        with self._write_lock if write else nullcontext():
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA busy_timeout=5000")
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA foreign_keys=ON")
                db.create_function("kg_search_text", 1, _search_text, deterministic=True)
                db.create_function("kg_short_grams", 3, _short_grams, deterministic=True)
                db.create_function("kg_short_terms", 3, _short_terms, deterministic=True)
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

    def __init__(self, data_root: Path) -> None:
        self._write_lock = threading.RLock()
        super().__init__(data_root, "knowledge_graph.db")
        with self.connection(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY, node_type TEXT NOT NULL CHECK(node_type IN
                  ('fact','hypothesis','observation','health_event','behavior_pattern')),
                category TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                source TEXT NOT NULL, evidence_ids TEXT NOT NULL,
                origin_table TEXT, origin_id TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, metadata TEXT NOT NULL,
                UNIQUE(origin_table,origin_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS edges (
                id TEXT PRIMARY KEY,
                from_node TEXT NOT NULL REFERENCES nodes(id),
                to_node TEXT NOT NULL REFERENCES nodes(id),
                edge_type TEXT NOT NULL CHECK(edge_type IN
                  ('caused_by','correlates_with','supports','contradicts','evolves_to','precedes')),
                confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                evidence_ids TEXT NOT NULL, description TEXT NOT NULL,
                created_at TEXT NOT NULL, CHECK(from_node != to_node),
                UNIQUE(from_node,to_node,edge_type))""")
            db.execute("CREATE INDEX IF NOT EXISTS nodes_topic ON nodes(category,key)")
            db.execute("CREATE INDEX IF NOT EXISTS nodes_type ON nodes(node_type)")
            db.execute("CREATE INDEX IF NOT EXISTS edges_to ON edges(to_node)")
            self._migrate_search(db)

    @staticmethod
    def _migrate_search(db: sqlite3.Connection, *, rebuild: bool = False) -> None:
        """Disposable contentless indexes; caller owns the atomic transaction.

        The authoritative external content is nodes (no extra copy of values).
        content='' cannot use FTS5's rebuild command: explicitly backfill both
        indexes instead. A failed backfill rolls back DDL and rows together.
        All writers must use connection(), which registers normalization UDFs.

        Short (1-2 character) grams live in nodes_short, a contentless
        detail='none' FTS5 table of space-separated grams keyed by nodes.rowid.
        It replaces the former per-gram nodes_bigram table, which needed about
        76 rows plus a reverse index per node (~250MB at 37k nodes). Files that
        still carry nodes_bigram are migrated here; reclaiming the freed pages
        needs a separate VACUUM outside this transaction (vacuum_search_space).
        """
        existing = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE name IN "
                "('nodes_fts','nodes_short','nodes_bigram','nodes_search_insert',"
                "'nodes_search_delete','nodes_search_update')"
            )
        }
        legacy = "nodes_bigram" in existing
        fresh = legacy or len(existing) != 5
        if fresh:
            # Old triggers reference nodes_bigram; IF NOT EXISTS would keep them.
            for name in ("nodes_search_insert", "nodes_search_delete", "nodes_search_update"):
                db.execute("DROP TRIGGER IF EXISTS " + name)
            db.execute("DROP TABLE IF EXISTS nodes_bigram")
        db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
            value, category, key, content='', tokenize='trigram')""")
        db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS nodes_short USING fts5(
            grams, content='', detail='none',
            tokenize='unicode61 remove_diacritics 0')""")
        insert = """
            INSERT INTO nodes_fts(rowid,value,category,key) VALUES(
                NEW.rowid,kg_search_text(NEW.value),kg_search_text(NEW.category),
                kg_search_text(NEW.key));
            INSERT INTO nodes_short(rowid,grams) VALUES(
                NEW.rowid,kg_short_terms(NEW.value,NEW.category,NEW.key));
        """
        delete = """
            INSERT INTO nodes_fts(nodes_fts,rowid,value,category,key) VALUES(
                'delete',OLD.rowid,kg_search_text(OLD.value),kg_search_text(OLD.category),
                kg_search_text(OLD.key));
            INSERT INTO nodes_short(nodes_short,rowid,grams) VALUES(
                'delete',OLD.rowid,kg_short_terms(OLD.value,OLD.category,OLD.key));
        """
        # No executescript: sqlite3 executescript implicitly commits and would
        # break both migration failure rollback and surrounding write atomicity.
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS nodes_search_insert AFTER INSERT ON nodes "
            "BEGIN " + insert + " END"
        )
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS nodes_search_delete AFTER DELETE ON nodes "
            "BEGIN " + delete + " END"
        )
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS nodes_search_update "
            "AFTER UPDATE OF id,value,category,key ON nodes "
            "BEGIN " + delete + insert + " END"
        )
        if fresh or rebuild:
            db.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('delete-all')")
            db.execute("INSERT INTO nodes_short(nodes_short) VALUES('delete-all')")
            db.execute("""INSERT INTO nodes_fts(rowid,value,category,key)
                SELECT rowid,kg_search_text(value),kg_search_text(category),
                    kg_search_text(key) FROM nodes""")
            db.execute("""INSERT INTO nodes_short(rowid,grams)
                SELECT rowid,kg_short_terms(value,category,key) FROM nodes""")

    def migrate_search_indexes(self, *, rebuild: bool = False) -> None:
        """Idempotent R1 migration, or explicit repair/backfill of existing nodes."""
        with self.connection(write=True) as db:
            self._migrate_search(db, rebuild=rebuild)

    def rollback_search_indexes(self) -> None:
        """Drop only disposable R1 objects; restore old code before serving queries.

        Nodes, edges and their existing indexes/IDs are never changed. A fresh
        KnowledgeGraph instance will automatically reapply the migration.
        """
        with self.connection(write=True) as db:
            for name in ("nodes_search_insert", "nodes_search_delete", "nodes_search_update"):
                db.execute("DROP TRIGGER IF EXISTS " + name)
            db.execute("DROP TABLE IF EXISTS nodes_fts")
            db.execute("DROP TABLE IF EXISTS nodes_short")
            db.execute("DROP TABLE IF EXISTS nodes_bigram")

    def vacuum_search_space(self) -> None:
        """Reclaim pages freed by the nodes_bigram migration (offline only).

        VACUUM cannot run inside a transaction and rewrites the whole file, so
        call it explicitly during a stopped-writer maintenance window after a
        private backup. Data and rowids are unchanged.
        """
        with self._write_lock:
            db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            try:
                db.execute("PRAGMA busy_timeout=30000")
                db.execute("VACUUM")
            finally:
                db.close()

    def migrate_jarvis_self_dev_goal(self) -> int:
        """Reclassify the legacy mission in place; repeated runs change no rows.

        Call explicitly after a private SQLite backup of a live database. Only
        category changes: IDs, evidence, timestamps and existing edges survive.
        """
        with self.connection(write=True) as db:
            changed = db.execute(
                "UPDATE nodes SET category='mission' "
                "WHERE category='identity' AND key='jarvis_self_dev_goal'"
            ).rowcount
        return changed

    def self_evaluate(self) -> dict[str, Any]:
        """Describe retrieval/inference gaps without disclosing graph contents."""
        return {
            "module": "knowledge_graph",
            "domain": "personal knowledge graph reasoning graph neural network",
            "current_version": (
                "SQLite provenance graph with keyword retrieval and correlation auto-links"
            ),
            "known_limitations": [
                "目前只有关键词匹配，无语义检索",
                "auto_link 只做相关性，无因果推断",
            ],
            "improvement_directions": ["GraphRAG 向量检索增强", "TransE 嵌入推断因果关系"],
            "last_evaluated_at": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _node_values(
        node_type: str,
        category: str,
        key: str,
        value: str,
        confidence: float,
        source: str,
        evidence_ids: Sequence[str] | None,
        origin_table: str | None,
        origin_id: str | None,
        metadata: Record | None,
    ) -> tuple[Any, ...]:
        if node_type not in NODE_TYPES:
            raise ValueError("Unknown node type")
        if type(category) is not str or category not in KG_CATEGORIES:
            raise ValueError("Unknown knowledge graph category")
        require_text(key, "Key")
        if origin_table == "sir_profile":
            # Mirrored Sir facts must retain the original profile validation.
            validate_topic(category, key)
        require_text(value, "Node value")
        require_text(source, "Node source")
        confidence = validate_confidence(confidence)
        refs = validate_evidence_ids(evidence_ids)
        if origin_table is not None:
            require_text(origin_table, "Origin table")
        if origin_id is not None:
            require_text(origin_id, "Origin ID")
            if origin_table is None:
                raise ValueError("Origin ID requires an origin table")
        if metadata is not None and type(metadata) is not dict:
            raise ValueError("Metadata must be a JSON object")
        return (
            node_type,
            category,
            key,
            value,
            confidence,
            source,
            _json(refs),
            origin_table,
            origin_id,
            _json(metadata if metadata is not None else {}),
        )

    @staticmethod
    def _put_node(
        db: sqlite3.Connection,
        values: tuple[Any, ...],
        *,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> str:
        origin_table, origin_id = values[7:9]
        old = (
            db.execute(
                "SELECT id,created_at FROM nodes WHERE origin_table=? AND origin_id=?",
                (origin_table, origin_id),
            ).fetchone()
            if origin_id is not None
            else None
        )
        identity = old["id"] if old else uuid.uuid4().hex
        now = utc_now()
        created = created_at or (old["created_at"] if old else now)
        db.execute(
            """INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(origin_table,origin_id) DO UPDATE SET
              node_type=excluded.node_type,category=excluded.category,key=excluded.key,
              value=excluded.value,confidence=excluded.confidence,source=excluded.source,
              evidence_ids=excluded.evidence_ids,created_at=excluded.created_at,
              updated_at=excluded.updated_at,metadata=excluded.metadata""",
            (identity, *values[:9], created, updated_at or now, values[9]),
        )
        return identity

    def add_node(
        self,
        node_type: str,
        category: str,
        key: str,
        value: str,
        confidence: float,
        source: str,
        evidence_ids: Sequence[str] | None = None,
        origin_table: str | None = None,
        origin_id: str | None = None,
        metadata: Record | None = None,
    ) -> str:
        values = self._node_values(
            node_type,
            category,
            key,
            value,
            confidence,
            source,
            evidence_ids,
            origin_table,
            origin_id,
            metadata,
        )
        with self.connection(write=True) as db:
            return self._put_node(db, values)

    @staticmethod
    def _put_edge(
        db: sqlite3.Connection,
        from_node: str,
        to_node: str,
        edge_type: str,
        confidence: float,
        evidence_ids: list[str],
        description: str,
        *,
        automatic: bool = False,
    ) -> str:
        if edge_type in SYMMETRIC_EDGES:
            from_node, to_node = sorted((from_node, to_node))
        old = db.execute(
            """SELECT * FROM edges
            WHERE from_node=? AND to_node=? AND edge_type=?""",
            (from_node, to_node, edge_type),
        ).fetchone()
        if old is not None:
            if not automatic:
                # A manual assertion takes ownership of an automatic candidate.
                identity = uuid.uuid4().hex if old["id"].startswith("auto_") else old["id"]
                db.execute(
                    """UPDATE edges SET id=?,confidence=?,evidence_ids=?,description=?
                    WHERE id=?""",
                    (identity, confidence, _json(evidence_ids), description, old["id"]),
                )
                return identity
            return old["id"]
        identity = ("auto_" if automatic else "") + uuid.uuid4().hex
        db.execute(
            "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?)",
            (
                identity,
                from_node,
                to_node,
                edge_type,
                confidence,
                _json(evidence_ids),
                description,
                utc_now(),
            ),
        )
        return identity

    def add_edge(
        self,
        from_node: str,
        to_node: str,
        edge_type: str,
        confidence: float,
        evidence_ids: Sequence[str] | None = None,
        description: str = "",
    ) -> str:
        require_text(from_node, "From node")
        require_text(to_node, "To node")
        if edge_type not in EDGE_TYPES or from_node == to_node:
            raise ValueError("Invalid edge type or self edge")
        confidence = validate_confidence(confidence)
        refs = validate_evidence_ids(evidence_ids)
        if type(description) is not str:
            raise ValueError("Edge description must be text")
        with self.connection(write=True) as db:
            for identity in (from_node, to_node):
                if db.execute("SELECT 1 FROM nodes WHERE id=?", (identity,)).fetchone() is None:
                    raise KeyError(identity)
            return self._put_edge(db, from_node, to_node, edge_type, confidence, refs, description)

    def get_node(self, node_id: str) -> Record | None:
        require_text(node_id, "Node ID")
        with self.connection() as db:
            row = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            return _decode(row) if row is not None else None

    def get_all_nodes(self) -> list[Record]:
        with self.connection() as db:
            return [
                _decode(row) for row in db.execute("SELECT * FROM nodes ORDER BY created_at,id")
            ]

    def get_all_edges(self) -> list[Record]:
        with self.connection() as db:
            return [
                _decode(row) for row in db.execute("SELECT * FROM edges ORDER BY created_at,id")
            ]

    @staticmethod
    def _snapshot(db: sqlite3.Connection) -> tuple[dict[str, Record], list[Record]]:
        nodes = {
            _row["id"]: _decode(_row)
            for _row in db.execute("SELECT * FROM nodes ORDER BY created_at,id")
        }
        edges = [_decode(row) for row in db.execute("SELECT * FROM edges ORDER BY created_at,id")]
        return nodes, edges

    @staticmethod
    def _related(
        node_id: str,
        max_depth: int,
        nodes: dict[str, Record],
        edges: list[Record],
    ) -> Record:
        if node_id not in nodes:
            return {"node": None, "nodes": [], "neighbors": [], "edges": [], "depths": {}}
        adjacent: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            adjacent[edge["from_node"]].append(edge["to_node"])
            adjacent[edge["to_node"]].append(edge["from_node"])
        depths = {node_id: 0}
        queue = deque([node_id])
        while queue:
            current = queue.popleft()
            if depths[current] >= max_depth:
                continue
            for neighbor in adjacent[current]:
                if neighbor not in depths:
                    depths[neighbor] = depths[current] + 1
                    queue.append(neighbor)
        found = [nodes[identity] for identity in depths]
        return {
            "node": nodes[node_id],
            "nodes": found,
            "neighbors": [node for node in found if node["id"] != node_id],
            "edges": [
                edge for edge in edges if edge["from_node"] in depths and edge["to_node"] in depths
            ],
            "depths": depths,
        }

    def query_related(self, node_id: str, max_depth: int = 2) -> Record:
        """Undirected BFS, inclusive induced subgraph, shortest hop depths.

        Missing IDs yield an empty graph. ``node`` is the root, ``nodes`` includes
        it, and ``neighbors`` excludes it. Edge directions are always retained.
        """
        require_text(node_id, "Node ID")
        if type(max_depth) is not int or max_depth < 0:
            raise ValueError("Maximum depth must be a nonnegative integer")
        with self.connection() as db:
            nodes, edges = self._snapshot(db)
            return self._related(node_id, max_depth, nodes, edges)

    @staticmethod
    def _candidate_rows(db: sqlite3.Connection, question: str, k: int) -> list[sqlite3.Row]:
        """Union indexed rowids, then fetch only k authoritative node rows.

        Internal helper also permits uncapped candidate-set audits by passing a
        sufficiently large k. No nodes/edges snapshot or instr fallback exists.
        """
        short, expression = _query_terms(question)
        clauses: list[str] = []
        params: list[Any] = []
        if expression:
            clauses.append("SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ?")
            params.append(expression)
        if short:
            clauses.append("SELECT rowid FROM nodes_short WHERE nodes_short MATCH ?")
            params.append(" OR ".join('"' + gram.replace('"', '""') + '"' for gram in short))
        if not clauses:
            return []
        params.append(k)
        return list(
            db.execute(
                "SELECT * FROM nodes WHERE rowid IN ("
                + " UNION ".join(clauses)
                + " ORDER BY rowid LIMIT ?) ORDER BY rowid",
                params,
            )
        )

    @staticmethod
    def _one_hop(db: sqlite3.Connection, node: Record) -> Record:
        """Indexed one-hop induced graph (including edges between neighbors)."""
        identity = node["id"]
        incident = list(
            db.execute(
                "SELECT * FROM edges WHERE from_node=? UNION SELECT * FROM edges WHERE to_node=?",
                (identity, identity),
            )
        )
        neighbors = sorted(
            {
                row["to_node"] if row["from_node"] == identity else row["from_node"]
                for row in incident
            }
        )
        if not neighbors:
            return {**node, "neighbors": [], "edges": []}
        ids = json.dumps([identity, *neighbors])
        found = [
            _decode(row)
            for row in db.execute(
                "SELECT * FROM nodes WHERE id IN (SELECT value FROM json_each(?)) "
                "AND id!=? ORDER BY created_at,id",
                (ids, identity),
            )
        ]
        edges = [
            _decode(row)
            for row in db.execute(
                "SELECT e.* FROM json_each(?) s JOIN edges e ON e.from_node=s.value "
                "WHERE e.to_node IN (SELECT value FROM json_each(?)) "
                "ORDER BY e.created_at,e.id",
                (ids, ids),
            )
        ]
        return {**node, "neighbors": found, "edges": edges}

    def query(self, question: str, k: int = 20) -> list[Record]:
        """Normalized literal OR retrieval, bounded before one-hop expansion.

        FTS5 trigram handles >=3-character terms. The covering gram index
        preserves Chinese bigram recall, including bigrams of long questions;
        one-character legacy terms use the same side index, never a scan.
        Ordering is stable SQLite rowid order, not semantic relevance ranking.
        Result entries retain all authoritative fields, neighbors and edges.
        """
        if type(question) is not str:
            raise ValueError("Question must be text")
        if type(k) is not int or k < 0:
            raise ValueError("Query limit must be a nonnegative integer")
        if k == 0:
            return []
        with self.connection() as db:
            return [
                self._one_hop(db, _decode(row)) for row in self._candidate_rows(db, question, k)
            ]

    def find_causal_chain(self, start_category: str, end_category: str) -> list[Record]:
        """One shortest directed path per reachable start/end pair.

        Accept categories or node types (as in health_event -> fact). Only
        caused_by/evolves_to edges participate: correlation, support and temporal
        precedence alone do not demonstrate causality. Confidence is the weakest
        edge score, not a computed probability or independent causal validation.
        """
        require_text(start_category, "Start category")
        require_text(end_category, "End category")
        with self.connection() as db:
            nodes, edges = self._snapshot(db)
        adjacent: dict[str, list[Record]] = defaultdict(list)
        for edge in edges:
            if edge["edge_type"] in {"caused_by", "evolves_to"}:
                adjacent[edge["from_node"]].append(edge)
        paths: list[Record] = []
        for start in nodes.values():
            if start_category not in (start["category"], start["node_type"]):
                continue
            parents: dict[str, tuple[str, Record] | None] = {start["id"]: None}
            queue = deque([start["id"]])
            while queue:
                current = queue.popleft()
                if current != start["id"] and end_category in (
                    nodes[current]["category"],
                    nodes[current]["node_type"],
                ):
                    identities = [current]
                    path_edges: list[Record] = []
                    previous = parents[current]
                    while previous is not None:
                        identity, edge = previous
                        identities.append(identity)
                        path_edges.append(edge)
                        previous = parents[identity]
                    paths.append(
                        {
                            "nodes": [nodes[i] for i in reversed(identities)],
                            "edges": list(reversed(path_edges)),
                            "confidence": min(edge["confidence"] for edge in path_edges),
                        }
                    )
                for edge in adjacent[current]:
                    neighbor = edge["to_node"]
                    if neighbor not in parents:
                        parents[neighbor] = (current, edge)
                        queue.append(neighbor)
        return paths

    def sync_from_sir_profile(self, sir_profile: SirProfile) -> dict[str, int]:
        """Read every fact; preserve source timestamps, source type and notes."""
        prepared: list[tuple[tuple[Any, ...], str, str]] = []
        for fact in sir_profile.get():
            metadata = {
                "original_source": fact["source"],
                "notes": fact["notes"],
                "source_db": str(sir_profile.path),
            }
            values = self._node_values(
                "fact",
                fact["category"],
                fact["key"],
                fact["value"],
                fact["confidence"],
                "sir_profile",
                fact["evidence_ids"],
                "sir_profile",
                fact["id"],
                metadata,
            )
            prepared.append((values, fact["created_at"], fact["updated_at"]))
        with self.connection(write=True) as db:
            for values, created, updated in prepared:
                self._put_node(db, values, created_at=created, updated_at=updated)
        return {"synced": len(prepared)}

    def sync_from_hypotheses(self, hypothesis_engine: HypothesisEngine) -> dict[str, int]:
        """Import pending/confirmed; refresh previously imported retired records.

        Supporting/opposing evidence stay separate in metadata. The node's
        evidence_ids is their union for provenance, not a claim of support.
        No try_confirm/expire_stale or source mutation is performed here.
        """
        with hypothesis_engine.connection() as source:
            records = [dict(row) for row in source.execute("SELECT * FROM hypotheses ORDER BY id")]
        prepared: list[tuple[tuple[Any, ...], str, str, bool]] = []
        for row in records:
            support = validate_evidence_ids(json.loads(row["evidence_for"]))
            oppose = validate_evidence_ids(json.loads(row["evidence_against"]))
            active = row["status"] in {"pending", "confirmed"}
            metadata = {
                field: row[field]
                for field in ("status", "confirm_condition", "refute_condition", "expires_at")
            }
            metadata.update(
                {
                    "evidence_for": support,
                    "evidence_against": oppose,
                    "active": active,
                    "source_db": str(hypothesis_engine.path),
                }
            )
            values = self._node_values(
                "hypothesis",
                row["category"],
                row["key"],
                row["claim"],
                row["confidence"],
                "hypothesis",
                support + oppose,
                "hypotheses",
                row["id"],
                metadata,
            )
            prepared.append((values, row["created_at"], row["updated_at"], active))
        count = 0
        with self.connection(write=True) as db:
            for values, created, updated, active in prepared:
                exists = db.execute(
                    "SELECT 1 FROM nodes WHERE origin_table=? AND origin_id=?",
                    (values[7], values[8]),
                ).fetchone()
                if active or exists:
                    self._put_node(db, values, created_at=created, updated_at=updated)
                    count += 1
        return {"synced": count}

    @staticmethod
    def _support(node: Record) -> set[str]:
        if node["node_type"] == "hypothesis":
            if node["metadata"].get("status", "pending") not in {"pending", "confirmed"}:
                return set()
            return set(node["metadata"].get("evidence_for", node["evidence_ids"]))
        return set(node["evidence_ids"])

    @staticmethod
    def _event_time(node: Record) -> tuple[datetime | None, bool]:
        metadata = node["metadata"]
        explicit = False
        value = node["created_at"]
        for key in ("event_time", "observed_at", "occurred_at", "timestamp"):
            if key in metadata:
                value = metadata[key]
                explicit = True
                break
        if not isinstance(value, str):
            return None, explicit
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None, explicit
            return parsed.astimezone(UTC), explicit
        except (ValueError, OverflowError):
            return None, explicit

    def auto_link(self) -> dict[str, int]:
        """Reconcile simple rule-derived candidates, leaving manual edges alone.

        * Same category/different keys + shared supporting evidence: correlation
          capped at .5. No shared evidence means no such edge.
        * Behavior before health event within six hours: causal *candidate*,
          capped at .3 with explicit event timestamps, .1 with insertion-time
          fallback. Descriptions explicitly deny established causality.
        * Confirmed hypothesis -> fact requires exact category/key/value equality.

        Automatic edge IDs use a reserved auto_ prefix. Repeated calls preserve
        IDs/timestamps, refresh scores/evidence and retract obsolete automatic
        edges only. This never upgrades nodes, diagnoses, or edits source data.
        """
        with self.connection(write=True) as db:
            nodes, edges = self._snapshot(db)
            candidates: dict[tuple[str, str, str], tuple[float, list[str], str]] = {}

            def candidate(
                a: Record, b: Record, kind: str, confidence: float, refs: set[str], description: str
            ) -> None:
                start, end = a["id"], b["id"]
                if kind in SYMMETRIC_EDGES:
                    start, end = sorted((start, end))
                candidates[(start, end, kind)] = (confidence, sorted(refs), description)

            active = [
                node
                for node in nodes.values()
                if node["node_type"] != "hypothesis"
                or node["metadata"].get("status", "pending") in {"pending", "confirmed"}
            ]
            # Evidence inverted index avoids comparing every unrelated pair.
            by_evidence: dict[tuple[str, str], list[Record]] = defaultdict(list)
            pairs: dict[tuple[str, str], tuple[Record, Record]] = {}
            for node in active:
                for ref in self._support(node):
                    group = by_evidence[(node["category"], ref)]
                    for other in group:
                        if node["key"] != other["key"]:
                            pair = tuple(sorted((node["id"], other["id"])))
                            pairs[pair] = (node, other)
                    group.append(node)
            for a, b in pairs.values():
                common = self._support(a) & self._support(b)
                candidate(
                    a,
                    b,
                    "correlates_with",
                    min(0.5, a["confidence"], b["confidence"]),
                    common,
                    "共享证据的相关候选；不代表因果。",
                )

            behaviors = [n for n in active if n["node_type"] == "behavior_pattern"]
            health = [n for n in active if n["node_type"] == "health_event"]
            for behavior in behaviors:
                earlier, behavior_explicit = self._event_time(behavior)
                if earlier is None:
                    continue
                for event in health:
                    later, health_explicit = self._event_time(event)
                    if later is None or not 0 <= (later - earlier).total_seconds() <= 6 * 3600:
                        continue
                    explicit = behavior_explicit and health_explicit
                    cap = 0.3 if explicit else 0.1
                    refs = self._support(behavior) | self._support(event)
                    basis = "事件时间" if explicit else "节点创建时间（不代表实际发生时间）"
                    candidate(
                        behavior,
                        event,
                        "precedes",
                        min(cap, behavior["confidence"], event["confidence"]),
                        refs,
                        f"时序候选（非因果）：{basis}接近；仅时间先后，需独立因果推断才能认定关联。",
                    )

            facts: dict[tuple[str, str, str], list[Record]] = defaultdict(list)
            for node in active:
                if node["node_type"] == "fact":
                    facts[(node["category"], node["key"], node["value"])].append(node)
            for node in active:
                if (
                    node["node_type"] == "hypothesis"
                    and node["metadata"].get("status") == "confirmed"
                ):
                    for fact in facts[(node["category"], node["key"], node["value"])]:
                        candidate(
                            node,
                            fact,
                            "evolves_to",
                            min(node["confidence"], fact["confidence"]),
                            self._support(node) | self._support(fact),
                            "已确认假设对应同主题、同内容的事实；保留原始来源。",
                        )

            existing = {(e["from_node"], e["to_node"], e["edge_type"]): e for e in edges}
            added = updated = removed = 0
            for key, (confidence, refs, description) in candidates.items():
                old = existing.get(key)
                if old is None:
                    self._put_edge(db, *key, confidence, refs, description, automatic=True)
                    added += 1
                elif old["id"].startswith("auto_") and (
                    old["confidence"],
                    old["evidence_ids"],
                    old["description"],
                ) != (confidence, refs, description):
                    db.execute(
                        "UPDATE edges SET confidence=?,evidence_ids=?,description=? WHERE id=?",
                        (confidence, _json(refs), description, old["id"]),
                    )
                    updated += 1
            for key, old in existing.items():
                if old["id"].startswith("auto_") and key not in candidates:
                    db.execute("DELETE FROM edges WHERE id=?", (old["id"],))
                    removed += 1
        return {"added": added, "updated": updated, "removed": removed}

    def export_dot(self) -> str:
        """Graphviz DOT with quoted/escaped Unicode IDs and labels."""
        with self.connection() as db:
            nodes, edges = self._snapshot(db)

        def quote(value: Any) -> str:
            return json.dumps(str(value), ensure_ascii=False)

        lines = ["digraph knowledge_graph {"]
        for node in nodes.values():
            label = f"{node['node_type']} | {node['category']}/{node['key']}\n{node['value']}"
            lines.append(f"  {quote(node['id'])} [label={quote(label)}];")
        for edge in edges:
            label = f"{edge['edge_type']} ({edge['confidence']:.2f})"
            if edge["description"]:
                label += "\n" + edge["description"]
            lines.append(
                f"  {quote(edge['from_node'])} -> {quote(edge['to_node'])} [label={quote(label)}];"
            )
        return "\n".join([*lines, "}"]) + "\n"
