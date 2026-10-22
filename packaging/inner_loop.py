#!/usr/bin/env python3
"""Bounded, append-only product mind. No background service is installed."""

from __future__ import annotations

import json
import os
import time
import re
import sqlite3
import stat
import uuid
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import product_home

Record = dict[str, Any]


def self_evaluate() -> dict[str, Any]:
    """Report the mind ledger contract without opening mind.db.

    Static schema/ownership audit only: it does not prove that an existing
    database matches, that writes reached disk, or that stored beliefs are true.
    """
    return {
        "module": "inner_loop",
        "domain": "append-only mind ledger, belief versions and prompt composition",
        "ledger_tables": ["entries", "belief_versions"],
        "append_only_triggers": [
            "no_update",
            "no_delete",
            "belief_no_update",
            "belief_no_delete",
        ],
        "journal_mode": "WAL",
        "known_limitations": [
            "Table and trigger presence is read from source, not from a live database.",
            "KG fusion from entries is performed outside this module by kg_projector.",
            "Belief version rows record revision history, not verified world truth.",
            "System prompt composition describes abilities; dispatch and permission gates remain separate.",
        ],
        "improvement_directions": [
            "Add an offline verifier against a caller-supplied temporary ledger.",
            "Expose bounded category coverage counters without scanning payloads.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


def build_system_prompt(
    *,
    persona: str | None = None,
    catalog: Sequence[Mapping[str, Any]] | None = None,
    skill_loader: Any = None,
) -> str:
    """Build first-person self-knowledge from a fresh capability/loaded-skill snapshot.

    Call at each think boundary, not at import time. This only describes current
    abilities; dispatch and permission gates remain unchanged. Passing the loop's
    catalog preserves its actual registered capability set, including map services.
    """
    if persona is None:
        persona = Path(__file__).with_name("JARVIS_PERSONA.md").read_text(encoding="utf-8")
    if catalog is None:
        import capabilities

        catalog = capabilities.catalog()
    abilities = [
        entry["persona_description"].strip()
        for entry in catalog
        if entry.get("available") is True
        and isinstance(entry.get("persona_description"), str)
        and entry["persona_description"].strip()
    ]
    skills: list[str] = []
    if skill_loader is not None:
        # SkillLoader currently exposes no public loaded-manifest snapshot API.
        # Read its authenticated cache under the same lock, never scan/load here:
        # merely discovering files must not install or execute a skill.
        with skill_loader._lock:
            manifests = list(skill_loader._manifests.values())
        for manifest in manifests:
            if not manifest.verify_checksum():
                raise ValueError("Loaded skill manifest checksum does not match")
            description = manifest.description_for_system_prompt or manifest.description
            description = " ".join(description.split())
            if description:
                skills.append(description if description.startswith("我") else "我能" + description)
    body = "我当前具备以下感知与执行能力：\n" + "\n".join(abilities)
    if skills:
        body += "\n\n" + "\n".join(skills)
    return persona.rstrip() + "\n\n" + body


class MindStore:
    def __init__(self, root: Path) -> None:
        self.root = product_home.validate_root(root)
        self.path = root / "mind.db"
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            product_home.safe_absolute(path)
            if path.exists():
                info = path.stat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise ValueError("Unsafe mind database")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS entries (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL, created_at TEXT NOT NULL,
                    source TEXT NOT NULL, payload TEXT NOT NULL, refs TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS no_update BEFORE UPDATE ON entries
                    BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS no_delete BEFORE DELETE ON entries
                    BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TABLE IF NOT EXISTS belief_versions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    belief_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    subject TEXT NOT NULL,
                    attribute TEXT NOT NULL,
                    value TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('self_report','observed','inferred')),
                    obtained_at TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    last_verified TEXT NOT NULL,
                    importance REAL NOT NULL,
                    memory_id TEXT,
                    refs TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS belief_latest ON belief_versions(belief_id, seq);
                CREATE TRIGGER IF NOT EXISTS belief_no_update BEFORE UPDATE ON belief_versions
                    BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS belief_no_delete BEFORE DELETE ON belief_versions
                    BEGIN SELECT RAISE(ABORT,'append only'); END;
            """)
            db.execute("CREATE INDEX IF NOT EXISTS entries_category_seq ON entries(category,seq)")
            # Non-unique: historical duplicate acknowledgements stay append-only.
            # The full pair makes durable EXISTS checks independent of backlog size.
            db.execute(
                "CREATE INDEX IF NOT EXISTS entries_origin_belief_v1 "
                "ON entries(category,json_extract(payload,'$.origin_id'),"
                "json_extract(payload,'$.belief_id'))"
            )
            for field in (
                "round",
                "phase",
                "call_id",
                "event_id",
                "belief_id",
                "question_id",
                "origin_id",
                "fingerprint",
                "data.daemon_event_id",
                "data.action_id",
            ):
                name = field.replace(".", "_")
                db.execute(
                    f"CREATE INDEX IF NOT EXISTS entries_{name} "
                    f"ON entries(category,json_extract(payload,'$.{field}'),seq)"
                )

    # 任何一条写错的查询都不该把 CPU 烧几分钟（Datasette 的做法：给连接装
    # 指令预算，超时中断并留下 OperationalError: interrupted）。
    SQL_TIME_LIMIT_MS = int(os.environ.get("JARVIS_SQL_TIME_LIMIT_MS", "8000"))

    @contextmanager
    def connection(self, *, time_limit_ms: int | None = -1) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        # One daemon, many threads (think, poll, distillation, health, evolution,
        # socket). The legacy rollback journal serialises every reader against
        # every writer, so concurrent access timed out with "database is locked".
        # WAL keeps readers running during a write; busy_timeout absorbs the
        # remaining writer-vs-writer contention instead of failing a stage.
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA cache_size=-65536")   # 64 MiB 页缓存，单机嵌入式很便宜
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("PRAGMA foreign_keys=ON")
        limit = self.SQL_TIME_LIMIT_MS if time_limit_ms == -1 else time_limit_ms
        if limit and limit > 0:
            deadline = time.monotonic() + limit / 1000.0
            db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2000)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists():
                try:
                    sidecar.chmod(0o600)
                except OSError:
                    pass
        try:
            with db:
                yield db
        finally:
            db.close()

    def append(
        self, category: str, payload: Record, *, source: str, refs: list[str] | None = None
    ) -> Record:
        if not category or not source or len(source) > 200:
            raise ValueError("Source and category required")
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(body.encode()) > 524288:
            raise ValueError("Mind entry exceeds limit")
        row: Record = {
            "id": uuid.uuid4().hex,
            "category": category,
            "created_at": datetime.now(UTC).isoformat(),
            "source": source,
            "payload": payload,
            "refs": refs or [],
        }
        with self.connection() as db:
            db.execute(
                "INSERT INTO entries(id,category,created_at,source,payload,refs) "
                "VALUES(?,?,?,?,?,?)",
                (row["id"], category, row["created_at"], source, body, json.dumps(row["refs"])),
            )
        return row

    def ingest(self, kind: str, payload: Record, *, source: str) -> Record:
        if kind not in {
            "conversation",
            "file_result",
            "time",
            "camera",
            "microphone",
            "tool_result",
        }:
            raise ValueError("Unknown perception kind")
        return self.append("event", {"kind": kind, "data": payload}, source=source)

    def beliefs(self) -> list[Record]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT b.* FROM belief_versions b "
                "JOIN (SELECT belief_id, MAX(seq) AS seq FROM belief_versions "
                "GROUP BY belief_id) latest "
                "ON latest.seq=b.seq ORDER BY b.seq"
            ).fetchall()
            beliefs = [{**dict(row), "refs": json.loads(row["refs"])} for row in rows]

        # Gaps live in the same append-only ledger without rebuilding old SQLite
        # tables: resolved IDs are shadowed by their ordinary belief versions.
        with self.connection() as db:
            gaps = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT g.payload FROM entries g WHERE g.category='knowledge_gap' "
                    "AND NOT EXISTS("
                    "SELECT 1 FROM entries r WHERE r.category='gap_retired' AND "
                    "json_extract(r.payload,'$.belief_id')=json_extract(g.payload,'$.belief_id')) "
                    "AND NOT EXISTS(SELECT 1 FROM belief_versions b WHERE "
                    "b.belief_id=json_extract(g.payload,'$.belief_id')) ORDER BY g.seq"
                )
            ]
        return beliefs + gaps

    def append_belief(
        self,
        *,
        belief_id: str | None,
        subject: str,
        attribute: str,
        value: str,
        source: str,
        confidence: float,
        last_verified: str,
        importance: float,
        memory_id: str | None = None,
        refs: list[str] | None = None,
    ) -> Record:
        if source not in {"self_report", "observed", "inferred"}:
            raise ValueError("Invalid belief source")
        if not subject.strip() or not attribute.strip() or not value.strip():
            raise ValueError("Belief fields required")
        if not 0 <= confidence <= 1 or not 0 <= importance <= 1:
            raise ValueError("Belief scores must be in 0..1")
        now = datetime.now(UTC).isoformat()
        identity = belief_id or uuid.uuid4().hex
        with self.connection() as db:
            previous = db.execute(
                "SELECT COALESCE(MAX(revision),0) FROM belief_versions WHERE belief_id=?",
                (identity,),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO belief_versions VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    previous + 1,
                    subject.strip(),
                    attribute.strip(),
                    value.strip(),
                    source,
                    now,
                    confidence,
                    last_verified,
                    importance,
                    memory_id,
                    json.dumps(refs or [], ensure_ascii=False),
                ),
            )
            row = db.execute(
                "SELECT * FROM belief_versions WHERE seq=last_insert_rowid()"
            ).fetchone()
        return {**dict(row), "refs": json.loads(row["refs"])}

    def append_belief_score(self, payload: Record, *, refs: list[str] | None = None) -> Record:
        return self.append("belief_score", payload, source="curiosity-scheduler", refs=refs)

    def entries(self, category: str | None = None, limit: int | None = None) -> list[Record]:
        sql = "SELECT * FROM entries"
        args: list[object] = []
        if category is not None:
            sql += " WHERE category=?"
            args.append(category)
        sql += " ORDER BY seq DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        with self.connection() as db:
            return [
                {
                    **dict(row),
                    "payload": json.loads(row["payload"]),
                    "refs": json.loads(row["refs"]),
                }
                for row in reversed(db.execute(sql, args).fetchall())
            ]

    @staticmethod
    def _field(field: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(\.[A-Za-z_][A-Za-z_0-9]*)*", field):
            raise ValueError("Invalid payload field")
        return f"json_extract(payload,'$.{field}')"

    def _where(self, category: str | None, where: Record | None) -> tuple[str, list[object]]:
        terms: list[str] = []
        args: list[object] = []
        if category is not None:
            terms.append("category=?")
            args.append(category)
        for field, value in (where or {}).items():
            terms.append(self._field(field) + " IS ?")
            args.append(value)
        return (" AND ".join(terms) or "1"), args

    def count(self, category: str, *, where: Record | None = None) -> int:
        clause, args = self._where(category, where)
        with self.connection() as db:
            return int(
                db.execute(f"SELECT count(*) FROM entries WHERE {clause}", args).fetchone()[0]
            )

    def exists(self, category: str, *, where: Record | None = None) -> bool:
        clause, args = self._where(category, where)
        with self.connection() as db:
            return (
                db.execute(f"SELECT 1 FROM entries WHERE {clause} LIMIT 1", args).fetchone()
                is not None
            )

    def distinct(self, category: str, field: str, *, where: Record | None = None) -> set[Any]:
        clause, args = self._where(category, where)
        expression = self._field(field)
        with self.connection() as db:
            return {
                r[0]
                for r in db.execute(
                    f"SELECT DISTINCT {expression} FROM entries WHERE {clause}", args
                )
            }

    @staticmethod
    def _decode(row: sqlite3.Row) -> Record:
        return {**dict(row), "payload": json.loads(row["payload"]), "refs": json.loads(row["refs"])}

    def recent(
        self, category: str, limit: int, *, where: Record | None = None, after_seq: int = 0
    ) -> list[Record]:
        if type(limit) is not int or limit < 0:
            raise ValueError("Nonnegative finite limit required")
        clause, args = self._where(category, where)
        with self.connection() as db:
            rows = db.execute(
                f"SELECT * FROM entries WHERE {clause} AND seq>? ORDER BY seq DESC LIMIT ?",
                [*args, after_seq, limit],
            ).fetchall()
            return [self._decode(row) for row in reversed(rows)]

    def get(self, entry_id: str) -> Record | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            return self._decode(row) if row else None

    def latest_seq(self, category: str | None = None) -> int:
        clause, args = self._where(category, None)
        with self.connection() as db:
            return int(
                db.execute(
                    f"SELECT COALESCE(MAX(seq),0) FROM entries WHERE {clause}", args
                ).fetchone()[0]
            )

    def iter_entries(
        self,
        category: str | None = None,
        *,
        where: Record | None = None,
        after_seq: int = 0,
        batch_size: int = 256,
    ) -> Generator[Record]:
        """Keyset batches on a frozen upper bound; no live read lock while yielding."""
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("Positive batch size required")
        upper = self.latest_seq(category)
        clause, args = self._where(category, where)
        while after_seq < upper:
            with self.connection() as db:
                rows = db.execute(
                    f"SELECT * FROM entries WHERE {clause} "
                    "AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
                    [*args, after_seq, upper, batch_size],
                ).fetchall()
            if not rows:
                break
            for row in rows:
                after_seq = row["seq"]
                yield self._decode(row)


if __name__ == "__main__":
    from inner_runtime import main

    raise SystemExit(main())
