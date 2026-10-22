"""Private, durable model research metadata; never stores API key contents."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast


class ModelRegistry:
    """One SQLite registry per data root, with short-lived connections."""

    _FIELDS = frozenset(
        {
            "provider",
            "name",
            "task_types",
            "supported_efforts",
            "price_input_per_1m",
            "price_output_per_1m",
            "quality_score",
            "has_key",
            "key_path",
            "source_urls",
            "last_updated",
            "notes",
        }
    )

    def self_evaluate(self) -> dict[str, Any]:
        """Describe implemented behavior and gaps without reading private state."""
        return {
            "module": "model_registry",
            "domain": "AI model research metadata registry task ranking reasoning effort selection",
            "current_version": "Private SQLite model metadata with patch upserts, exact task ranking and nearest supported effort lookup",
            "known_limitations": [
                "Task ranking relies on stored quality_score values; no benchmark execution or score freshness decay is implemented.",
                "has_key and key_path are metadata, including a seeded configuration; they do not verify credential availability or validity.",
                "Model discovery, pricing refresh and provider route validation are not performed by this registry.",
            ],
            "improvement_directions": [
                "Add dated reproducible benchmark evidence and explicit stale-score handling.",
                "Keep credential availability verification in an authorized boundary separate from metadata self-evaluation.",
                "Connect researched model updates to validated provider routes without storing credential contents.",
            ],
            "last_evaluated_at": datetime.now(UTC).isoformat(),
        }

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.data_root / "model_registry.db"
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS models (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                name TEXT NOT NULL,
                task_types TEXT NOT NULL,
                price_input_per_1m REAL,
                price_output_per_1m REAL,
                quality_score REAL,
                has_key INTEGER DEFAULT 0,
                key_path TEXT,
                source_urls TEXT,
                last_updated TEXT,
                notes TEXT,
                supported_efforts TEXT DEFAULT '["low", "medium", "high"]'
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(models)")}
            if "supported_efforts" not in columns:
                db.execute(
                    "ALTER TABLE models ADD COLUMN supported_efforts TEXT "
                    'DEFAULT \'["low", "medium", "high"]\''
                )
            # Seed only the owner's known configuration. Reopening must never
            # overwrite later research, credential revocation or changed paths.
            db.execute(
                "INSERT OR IGNORE INTO models "
                "(id,provider,name,task_types,quality_score,has_key,key_path,"
                "source_urls,last_updated,supported_efforts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "deepseek-flash",
                    "deepseek",
                    "DeepSeek Flash",
                    json.dumps(["general", "fast"]),
                    0.72,
                    1,
                    "~/.jarvis/api.key",
                    "[]",
                    datetime.now(UTC).isoformat(),
                    json.dumps(["low", "medium", "high"]),
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        # Create privately BEFORE SQLite opens it; reject symlinks and devices.
        fd = os.open(self.db_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("Model registry must be a regular file")
            os.fchmod(fd, 0o600)
            db = sqlite3.connect(
                self.db_path.absolute().as_uri() + "?mode=rw", uri=True, timeout=30
            )
            try:
                current = self.db_path.lstat()
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise OSError("Model registry changed during open")
                db.row_factory = sqlite3.Row
                return db
            except BaseException:
                db.close()
                raise
        finally:
            os.close(fd)

    @staticmethod
    def _entry(row: sqlite3.Row) -> dict[str, Any]:
        entry = dict(row)
        for field in ("task_types", "source_urls", "supported_efforts"):
            entry[field] = json.loads(entry[field] or "[]")
        return entry

    def upsert(
        self, model_id: str, *, supported_efforts: list[str] | None = None, **fields: Any
    ) -> None:
        """Insert a model or patch only supplied fields, preserving other metadata."""
        if not model_id.strip():
            raise ValueError("model_id must be a nonempty string")
        if set(fields) - self._FIELDS:
            raise ValueError("Unknown model fields")
        values = dict(fields)
        if supported_efforts is not None:
            values["supported_efforts"] = supported_efforts
        for field in ("task_types", "source_urls", "supported_efforts"):
            if field in values:
                value = values[field]
                if not isinstance(value, list) or any(
                    not isinstance(item, str) for item in cast(list[object], value)
                ):
                    raise ValueError(f"{field} must be a list of strings")
                values[field] = json.dumps(value, ensure_ascii=False, allow_nan=False)
        for field in ("provider", "name"):
            if field in values and (
                not isinstance(values[field], str) or not values[field].strip()
            ):
                raise ValueError(f"{field} must be a nonempty string")
        score = values.get("quality_score")
        if score is not None and (
            isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1
        ):
            raise ValueError("quality_score must be between 0 and 1")
        if "has_key" in values:
            if type(values["has_key"]) not in (bool, int) or values["has_key"] not in (0, 1):
                raise ValueError("has_key must be 0 or 1")
            values["has_key"] = int(values["has_key"])
        values.setdefault("last_updated", datetime.now(UTC).isoformat())
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute("SELECT 1 FROM models WHERE id=?", (model_id,)).fetchone()
            if exists:
                db.execute(
                    "UPDATE models SET "
                    + ",".join(f"{field}=?" for field in values)
                    + " WHERE id=?",
                    (*values.values(), model_id),
                )
            else:
                missing = {"provider", "name", "task_types"} - set(values)
                if missing:
                    raise ValueError("New models require provider, name and task_types")
                values.setdefault("has_key", 0)
                values.setdefault("source_urls", "[]")
                values.setdefault("supported_efforts", json.dumps(["low", "medium", "high"]))
                db.execute(
                    "INSERT INTO models (id,"
                    + ",".join(values)
                    + ") VALUES ("
                    + ",".join("?" for _ in range(len(values) + 1))
                    + ")",
                    (model_id, *values.values()),
                )

    def get(self, model_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        return self._entry(row) if row is not None else None

    def best_effort_for(self, model_id: str, desired_effort: str) -> str:
        """Return the nearest supported effort, preferring lower effort on ties.

        Missing models raise KeyError; invalid or empty effort sets raise ValueError.
        """
        entry = self.get(model_id)
        if entry is None:
            raise KeyError(model_id)
        supported = entry["supported_efforts"]
        if desired_effort in supported:
            return desired_effort
        order = ("low", "medium", "high", "xhigh", "max", "ultra")
        if desired_effort not in order:
            raise ValueError(f"Unknown reasoning effort: {desired_effort}")
        if not supported or any(effort not in order for effort in supported):
            raise ValueError("Model must have a nonempty list of known supported efforts")
        desired_rank = order.index(desired_effort)
        return min(
            supported,
            key=lambda effort: (abs(order.index(effort) - desired_rank), order.index(effort)),
        )

    def list_all(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM models ORDER BY id").fetchall()
        return [self._entry(row) for row in rows]

    def best_for_task(self, task_type: str) -> list[dict[str, Any]]:
        """Match exact task labels and rank best-first, including unkeyed models."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT m.* FROM models m WHERE EXISTS "
                "(SELECT 1 FROM json_each(m.task_types) WHERE value=?) "
                "ORDER BY quality_score DESC, id",
                (task_type,),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def needs_key_request(self) -> list[dict[str, Any]]:
        """Potential upgrades only: known key holders and scores <=0.8 are excluded."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM models WHERE quality_score>0.8 AND has_key=0 "
                "ORDER BY quality_score DESC, id"
            ).fetchall()
        return [self._entry(row) for row in rows]
