"""Product memory persistence and Chinese retrieval acceptance."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import product_memory as memory


class MemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_chinese_retrieval_with_source(self) -> None:
        store = memory.MemoryStore(self.path)
        row = store.add("我住在天水", "session-A", "message-1")
        results = store.search("我住哪")
        self.assertTrue(results, "Chinese facts must be retrieved across sessions")
        self.assertEqual(results[0]["id"], row["id"])
        self.assertEqual(results[0]["source_session"], "session-A")
        self.assertEqual(store.search("天水")[0]["content"], "我住在天水")

    def test_correction_retires_old_value_and_versions(self) -> None:
        store = memory.MemoryStore(self.path)
        old = store.add("我住在天水", "A", "1")
        new = store.correct(old["id"], "我住在兰州")
        self.assertEqual(store.search("天水"), [])
        self.assertEqual(new["version"], 2)
        self.assertEqual(new["supersedes"], old["id"])
        self.assertEqual(store.search("兰州")[0]["id"], new["id"])
        self.assertEqual(
            {r["status"] for r in store.list(include_history=True)}, {"active", "corrected"}
        )
        self.assertEqual(store.add("我住在天水", "A", "1"), {})

    def test_delete_restart_rebuild_and_reextract_do_not_resurrect(self) -> None:
        store = memory.MemoryStore(self.path)
        row = store.add("我住在天水", "A", "1")
        store.delete(row["id"])
        restarted = memory.MemoryStore(self.path)
        restarted.rebuild()
        self.assertEqual(restarted.search("我住在哪"), [])
        self.assertEqual(restarted.add("我住在天水", "A", "1"), {})
        self.assertEqual(restarted.list(), [])
        self.assertEqual(restarted.list(include_history=True)[0]["status"], "deleted")

    def test_duplicate_facts_are_deduplicated(self) -> None:
        store = memory.MemoryStore(self.path)
        first = store.add("我住在天水", "A", "1")
        second = store.add("我住在天水。", "B", "2")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(store.list()), 1)

    def test_context_revision_discards_stale_history(self) -> None:
        store = memory.MemoryStore(self.path)
        self.assertEqual(store.history_cutoff("A", 2), 0)
        row = store.add("我住在天水", "A", "1")
        store.delete(row["id"])
        self.assertEqual(store.history_cutoff("A", 4), 4)
        self.assertEqual(store.history_cutoff("A", 6), 4)
        self.assertEqual(store.history_cutoff("unseen", 8), 8)

    def test_candidates_require_explicit_verbatim_user_fact(self) -> None:
        self.assertEqual(
            memory.candidates('{"facts":["我住在天水","我叫张三"]}', "我住在天水"), ["我住在天水"]
        )
        for prompt in (
            "我住在哪？",
            "假设我住在天水",
            "请摘要：我住在天水",
            "我想翻译：我住在天水",
        ):
            self.assertFalse(memory.extraction_eligible(prompt))


class SelfEvaluationTests(unittest.TestCase):
    def hook(self):
        import importlib

        module = importlib.import_module("product_memory")
        return module, object.__new__(module.MemoryStore).self_evaluate

    def test_honest_schema_identity_algorithm_and_fresh_timestamp(self):
        import json
        from datetime import UTC, datetime

        _module, hook = self.hook()
        before = datetime.now(UTC)
        report = hook()
        after = datetime.now(UTC)
        self.assertEqual(
            set(report),
            {
                "module",
                "domain",
                "current_version",
                "known_limitations",
                "improvement_directions",
                "last_evaluated_at",
            },
        )
        self.assertEqual(report["module"], "product_memory")
        self.assertIn("trigram", report["current_version"].lower())
        self.assertIn("logical", " ".join(report["known_limitations"]).lower())
        self.assertTrue(report["domain"].strip())
        for key in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[key], list)
            self.assertTrue(report[key])
            self.assertTrue(all(isinstance(item, str) and item.strip() for item in report[key]))
        stamp = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, after)
        json.dumps(report, allow_nan=False)

    def test_metadata_is_io_free_without_initialized_store_or_credentials(self):
        from pathlib import Path
        import sqlite3
        import socket
        import subprocess
        import os
        from unittest.mock import patch

        _module, hook = self.hook()
        with (
            patch("builtins.open", side_effect=AssertionError("file access")),
            patch.object(Path, "open", side_effect=AssertionError("path access")),
            patch.object(sqlite3, "connect", side_effect=AssertionError("database access")),
            patch.object(socket, "socket", side_effect=AssertionError("network access")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("process access")),
            patch.object(os, "open", side_effect=AssertionError("descriptor access")),
        ):
            first = hook()
            first["known_limitations"].append("mutation sentinel")
            first["improvement_directions"].clear()
            second = hook()
        self.assertNotIn("mutation sentinel", second["known_limitations"])
        self.assertTrue(second["improvement_directions"])
        self.assertEqual(second["module"], "product_memory")


if __name__ == "__main__":
    unittest.main()
