"""Storage and resident scale regression; synthetic history only."""

from __future__ import annotations

import ast
import sqlite3
import tempfile
import unittest
from pathlib import Path

from inner_loop import MindStore


class BoundedStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        self.store = MindStore(self.root)

    def test_sql_queries_preserve_order_null_and_exact_filters(self) -> None:
        rows = [
            self.store.append("test", {"round": i, "data": {"key": i % 2}}, source="test")
            for i in range(5)
        ]
        self.assertEqual(self.store.count("test"), 5)
        self.assertTrue(self.store.exists("test", where={"round": 3}))
        self.assertFalse(self.store.exists("test", where={"round": 7}))
        self.assertEqual(self.store.distinct("test", "data.key"), {0, 1})
        self.assertEqual(self.store.recent("test", 2), self.store.entries("test", limit=2))
        self.assertEqual(
            self.store.recent("test", 2, where={"data.key": 0}),
            self.store.entries("test", limit=5)[2::2],
        )
        self.assertEqual(
            list(self.store.iter_entries("test", batch_size=2)), self.store.entries("test", limit=5)
        )
        selected = self.store.get(rows[2]["id"])
        assert selected is not None
        self.assertEqual(selected["payload"], {"round": 2, "data": {"key": 0}})
        self.assertEqual(self.store.latest_seq("test"), 5)
        self.assertEqual(len(self.store.recent("test", 5, after_seq=3)), 2)
        with self.assertRaises(ValueError):
            self.store.distinct("test", "bad' SQL")

    def test_migration_existing_ledger_and_explicit_close(self) -> None:
        self.store.append("old", {"round": 1}, source="legacy")
        reopened = MindStore(self.root)
        self.assertEqual(reopened.count("old"), 1)
        with reopened.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM entries").fetchone()[0], 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")

    def test_production_entries_calls_require_explicit_finite_limit(self) -> None:
        # No whitelist: audit/export APIs must use iter_entries, never entries.
        violations: list[str] = []
        for path in Path(__file__).parent.rglob("*.py"):
            if path.name.startswith("test") or "runtime" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "entries"
                ):
                    limit = (
                        node.args[1]
                        if len(node.args) > 1
                        else next((k.value for k in node.keywords if k.arg == "limit"), None)
                    )
                    if limit is None or isinstance(limit, ast.Constant) and limit.value is None:
                        violations.append(f"{path.name}:{node.lineno}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
