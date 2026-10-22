"""Synthetic-only offline replay tests. Never opens the product data directory."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="replay-unit-", dir=os.environ["TMPDIR"])
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / ".jarvis"
        self.home.mkdir(mode=0o700)
        self.backups = self.home / "context-p0-synthetic"
        self.backups.mkdir(mode=0o700)

    def make_sources(self):
        import inner_loop
        import product_memory
        import knowledge_graph

        # Only constructors' root policy is bypassed for synthetic fixtures in
        # scratch; production replay validates its own private roots separately.
        with patch("product_home.validate_root", side_effect=lambda p: p):
            store = inner_loop.MindStore(self.backups)
            memory = product_memory.MemoryStore(self.backups / "memory.db")
            graph = knowledge_graph.KnowledgeGraph(self.backups)
        with store.connection() as db:
            for i in range(1, 31):
                db.execute(
                    "INSERT INTO entries VALUES(?,?,?,?,?,?,?)",
                    (
                        i,
                        f"t{i}",
                        "thought",
                        f"2025-01-{min(i, 28):02d}T00:00:00+00:00",
                        "test",
                        json.dumps(
                            {
                                "kind": "observation",
                                "text": f"synthetic backlog {i}",
                                "question": None,
                            }
                        ),
                        "[]",
                    ),
                )
            db.execute(
                "INSERT INTO entries VALUES(?,?,?,?,?,?,?)",
                (
                    31,
                    "ev1",
                    "event",
                    "2025-02-01T00:00:00+00:00",
                    "sir",
                    json.dumps({"kind": "conversation", "data": {"text": "synthetic"}}),
                    "[]",
                ),
            )
            for seq, rev, value, at in [
                (1, 1, "old", "2025-01-01T00:00:00+00:00"),
                (2, 2, "new", "2025-02-01T00:00:00+00:00"),
            ]:
                db.execute(
                    "INSERT INTO belief_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        seq,
                        "b1",
                        rev,
                        "fixture",
                        "limit",
                        value,
                        "self_report",
                        at,
                        0.9,
                        at,
                        0.95,
                        None,
                        "[]",
                    ),
                )
        with memory.connection() as db:
            for row in [
                (
                    "m1",
                    "synthetic old",
                    "syntheticold",
                    "s",
                    "m",
                    "2025-01-01T00:00:00+00:00",
                    1,
                    "corrected",
                    None,
                ),
                (
                    "m2",
                    "synthetic new",
                    "syntheticnew",
                    "s",
                    "m",
                    "2025-02-01T00:00:00+00:00",
                    2,
                    "active",
                    "m1",
                ),
            ]:
                db.execute("INSERT INTO facts VALUES(?,?,?,?,?,?,?,?,?)", row)
        with graph.connection(write=True) as db:
            for i in range(3):
                db.execute(
                    "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"n{i}",
                        "fact",
                        "work",
                        f"anchor{i}",
                        f"synthetic graph {i}",
                        0.9,
                        "test",
                        "[]",
                        None,
                        None,
                        "2025-01-01T00:00:00+00:00",
                        "2025-01-01T00:00:00+00:00",
                        "{}",
                    ),
                )
            for i in range(2):
                db.execute(
                    "INSERT INTO edges VALUES(?,?,?,?,?,?,?,?)",
                    (
                        f"g{i}",
                        f"n{i}",
                        f"n{i + 1}",
                        "supports",
                        0.9,
                        "[]",
                        "fixture",
                        "2025-01-01T00:00:00+00:00",
                    ),
                )
        # WAL-mode fixture connections are closed; checkpoint removes sidecars.
        for path in self.backups.glob("*.db"):
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("PRAGMA journal_mode=DELETE")
            path.chmod(0o600)
        return {"mind": store.path, "memory": memory.path, "graph": graph.path}

    def test_builder_is_deterministic_has_six_types_and_no_model(self):
        import replay_harness as rh
        import build_replay_set as builder

        sources = self.make_sources()
        with patch.object(rh, "PRIVATE_HOME", self.home):
            first = builder.build_replay_set(sources, per_type=3)
            second = builder.build_replay_set(sources, per_type=3)
        self.assertEqual(rh.canonical(first), rh.canonical(second))
        self.assertEqual({c["type"] for c in first["cases"]}, set(rh.CASE_TYPES))
        self.assertTrue(all("as_of" in c and "gold_evidence_ids" in c for c in first["cases"]))
        self.assertTrue(
            all(
                c["review_status"] == "待人工确认"
                for c in first["cases"]
                if c["type"] == "important_constraints"
            )
        )
        self.assertEqual(
            first["counts"],
            {kind: sum(c["type"] == kind for c in first["cases"]) for kind in rh.CASE_TYPES},
        )

    def test_as_of_high_watermark_excludes_future_rows_and_metrics_are_content_free(self):
        import replay_harness as rh
        import build_replay_set as builder

        sources = self.make_sources()
        with patch.object(rh, "PRIVATE_HOME", self.home):
            frozen = builder.build_replay_set(
                sources, per_type=2, as_of="2025-01-15T00:00:00+00:00"
            )
            self.assertLessEqual(frozen["snapshot_as_of"]["high_watermarks"]["mind"]["entries"], 15)
            self.assertNotIn("new", json.dumps(frozen, ensure_ascii=False))
            self.assertEqual(
                rh.canonical(frozen),
                rh.canonical(
                    builder.build_replay_set(sources, per_type=2, as_of="2025-01-15T00:00:00+00:00")
                ),
            )

            class Stub(rh.BaselineAdapter):
                def retrieve(self, case, *, seed=0):
                    return rh.Retrieval(
                        tuple(case["gold_evidence_ids"]), 42, 1.0 + seed, 2.0 + seed
                    )

            report = rh.evaluate(frozen, Stub(frozen), k=1, repeats=2, seed=4)
            self.assertIn("recall_at_k", report)
            self.assertIn("context_latency_ms", report)
            self.assertNotIn("synthetic", json.dumps(report, ensure_ascii=False))

    def test_write_and_load_are_private_and_integrity_checked(self):
        import replay_harness as rh
        import build_replay_set as builder

        sources = self.make_sources()
        with patch.object(rh, "PRIVATE_HOME", self.home):
            frozen = builder.build_replay_set(sources, per_type=1)
            result = builder.write_replay(frozen, name="fixture.json")
            replay = self.home / "replay" / "fixture.json"
            self.assertEqual(result["case_count"], len(frozen["cases"]))
            self.assertEqual(rh.load_replay(replay)["integrity_sha256"], frozen["integrity_sha256"])
            replay.write_text(
                replay.read_text().replace('"schema_version":1', '"schema_version":2'),
                encoding="utf-8",
            )
            replay.chmod(0o600)
            with self.assertRaises(ValueError):
                rh.load_replay(replay)

        import replay_harness as rh

        with patch.object(rh, "PRIVATE_HOME", self.home):
            live = self.home / "loop-data"
            live.mkdir(mode=0o700)
            path = live / "mind.db"
            sqlite3.connect(path).close()
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                rh.validate_backup_path(path)
            copied = self.backups / "mind.db"
            sqlite3.connect(copied).close()
            copied.chmod(0o600)
            self.assertEqual(rh.validate_backup_path(copied), copied)
            copied.chmod(0o644)
            with self.assertRaises(ValueError):
                rh.validate_backup_path(copied)


if __name__ == "__main__":
    unittest.main()
