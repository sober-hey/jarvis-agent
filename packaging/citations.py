"""Persistent citation ledger for JARVIS."""

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
from uuid import uuid4


class CitationStore:
    """Store search evidence separately from conversation history.

    Connections are short-lived, so instances can safely be used by several
    threads and independent processes sharing the same data root.
    """

    def self_evaluate(self) -> dict:
        """Describe implemented behavior and gaps without reading private state."""
        return {
            "module": "citations",
            "domain": "web citation provenance evidence ledger retrieval",
            "current_version": "SQLite citation snapshots with conversation isolation and escaped literal substring lookup",
            "known_limitations": [
                "Search uses SQLite LIKE and json_tree substring scans, not semantic retrieval or relevance ranking.",
                "Stored search snapshots do not verify source claims, revisit URLs, or detect later source changes.",
                "No retention policy or duplicate-evidence consolidation is implemented.",
            ],
            "improvement_directions": [
                "Add indexed retrieval with explicit relevance and provenance while preserving literal lookup.",
                "Track source verification and freshness separately from recorded search snapshots.",
                "Introduce an explicit retention and deduplication policy with evidence references.",
            ],
            "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        }

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self._make_private_directory(self.data_root)
        self.db_path = self.data_root / "citations.db"
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS citations (
                id TEXT PRIMARY KEY,
                query TEXT,
                conversation_id TEXT,
                created_at TEXT,
                results_json TEXT
            )""")

    @staticmethod
    def _make_private_directory(path: Path) -> None:
        """Only newly created directories receive private permissions."""
        if path.is_dir():
            return
        CitationStore._make_private_directory(path.parent)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            if not path.is_dir():
                raise

    def _connect(self):
        # Precreate before SQLite sees the path: chmod-after-connect exposes
        # private evidence during the initial open. Never change global umask.
        fd = os.open(self.db_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("Citation database must be a regular file")
            os.fchmod(fd, 0o600)
            # Do not let SQLite silently recreate a DB removed during open.
            # Recheck the pathname before any schema or statement is executed.
            uri = self.db_path.absolute().as_uri() + "?mode=rw"
            db = sqlite3.connect(uri, uri=True, timeout=30)
            try:
                current = self.db_path.lstat()
                if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
                    info.st_dev,
                    info.st_ino,
                ):
                    raise OSError("Citation database changed during open")
                db.row_factory = sqlite3.Row
                return db
            except BaseException:
                db.close()
                raise
        finally:
            os.close(fd)

    @staticmethod
    def _entry(row) -> dict:
        return {
            "id": row["id"],
            "query": row["query"],
            "conversation_id": row["conversation_id"],
            "created_at": row["created_at"],
            "results": json.loads(row["results_json"]),
        }

    def record(self, query: str, results: list[dict], conversation_id: str | None) -> str:
        """Atomically persist a snapshot and return its unique citation ID."""
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if not isinstance(results, list) or any(not isinstance(result, dict) for result in results):
            raise TypeError("results must be a list of dictionaries")
        if conversation_id is not None and not isinstance(conversation_id, str):
            raise TypeError("conversation_id must be a string or None")
        payload = json.dumps(results, ensure_ascii=False, allow_nan=False)
        citation_id = uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO citations (id, query, conversation_id, created_at, results_json) VALUES (?, ?, ?, ?, ?)",
                (citation_id, query, conversation_id, created_at, payload),
            )
        return citation_id

    def get(self, citation_id: str) -> dict:
        """Return a citation, or an empty dictionary if it is unknown."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM citations WHERE id = ?", (citation_id,)).fetchone()
        return self._entry(row) if row is not None else {}

    def list_for_conversation(self, conversation_id: str) -> list[dict]:
        """Return only evidence belonging to the exact conversation ID."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM citations WHERE conversation_id = ? ORDER BY created_at, id",
                (conversation_id,),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def search_citations(self, query: str) -> list[dict]:
        """Search literal substrings in the query and decoded result text.

        JSON text values include URLs, titles and summaries regardless of the
        search provider's field names. Empty text matches every citation.
        """
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = "%" + escaped + "%"
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT c.* FROM citations AS c
                WHERE c.query LIKE ? ESCAPE '\\'
                OR EXISTS (SELECT 1 FROM json_tree(c.results_json)
                    WHERE type = 'text' AND value LIKE ? ESCAPE '\\')
                ORDER BY c.created_at, c.id""",
                (pattern, pattern),
            ).fetchall()
        return [self._entry(row) for row in rows]
