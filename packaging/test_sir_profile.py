"""Profile algorithm tests on isolated synthetic SQLite stores."""

from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sir_profile import SirProfile


class SirProfileTests(unittest.TestCase):
    def test_self_report_conflict_does_not_overwrite_canonical_fact(self):
        with tempfile.TemporaryDirectory(prefix="profile-unit-") as folder:
            root = Path(folder)
            # Host scratch is deliberately not a valid product root. Only this
            # constructor's root validation is substituted for synthetic tests.
            with patch("product_home.validate_root", return_value=root):
                profile = SirProfile(root)
            first = profile.upsert(
                "preference", "fictional-drink", "leaf-water", 0.9, "self_report", ["fixture-1"]
            )
            rejected = profile.upsert(
                "preference", "fictional-drink", "berry-water", 1.0, "inferred", ["fixture-2"]
            )
            self.assertFalse(rejected["applied"])
            self.assertEqual(profile.get("preference", "fictional-drink")[0]["id"], first["id"])
            self.assertEqual(profile.get("preference", "fictional-drink")[0]["value"], "leaf-water")
            self.assertEqual(len(profile.get_conflicts()), 1)

    def test_repeated_evidence_is_deduplicated_and_persists(self):
        with tempfile.TemporaryDirectory(prefix="profile-unit-") as folder:
            root = Path(folder)
            with patch("product_home.validate_root", return_value=root):
                profile = SirProfile(root)
            profile.upsert("habit", "fictional-routine", "walk", 0.6, "observed", ["e1", "e1"])
            profile.upsert("habit", "fictional-routine", "walk", 0.7, "observed", ["e1", "e2"])
            with patch("product_home.validate_root", return_value=root):
                reopened = SirProfile(root)
            self.assertEqual(
                reopened.get("habit", "fictional-routine")[0]["evidence_ids"], ["e1", "e2"]
            )


class MigratedLegacyTests(unittest.TestCase):
    """Migrated scratch/jarvis_profile_tests.py; synthetic data, scratch-only I/O."""

    def setUp(self):
        import tempfile
        from unittest.mock import patch

        self.temp = tempfile.TemporaryDirectory(prefix="legacy-profile-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        validation = patch("product_home.validate_root", return_value=self.root)
        validation.start()
        self.addCleanup(validation.stop)

    def test_profile_roundtrip_and_high_confidence(self):
        import importlib.util

        self.assertIsNotNone(importlib.util.find_spec("sir_profile"), "SirProfile module missing")
        from sir_profile import SirProfile

        profile = SirProfile(self.root)
        profile.upsert("identity", "name", "Synthetic Person", 1.0, "self_report")
        profile.upsert("habit", "sleep_pattern", "属夜猫型", 0.6, "inferred", notes="活跃数据")
        self.assertEqual(profile.get(category="identity")[0]["value"], "Synthetic Person")
        self.assertEqual(len(profile.get_high_confidence()), 1)
        self.assertEqual(profile.get(key="sleep_pattern")[0]["notes"], "活跃数据")
        self.assertEqual(os.stat(profile.path).st_mode & 0o777, 0o600)
        self.assertEqual(len(SirProfile(self.root).get()), 2)

    def test_source_priority_conflicts_and_evidence_dedup(self):
        from sir_profile import SirProfile

        p = SirProfile(self.root)
        p.upsert("identity", "name", "inference", 0.99, "inferred", ["old"])
        p.upsert("identity", "name", "spoken", 0.7, "self_report", ["spoken"])
        result = p.upsert("identity", "name", "wrong", 1.0, "observed", ["against"])
        self.assertEqual(p.get()[0]["value"], "spoken", "self report must not be overwritten")
        self.assertFalse(result["applied"])
        p.upsert("identity", "name", "wrong", 1.0, "observed", ["against"])
        self.assertEqual(len(p.get_conflicts()), 1)
        p.upsert("identity", "name", "new self report", 1.0, "self_report")
        self.assertEqual(p.get()[0]["value"], "spoken")
        self.assertEqual(len(p.get_conflicts()), 2)
        p.upsert("identity", "name", "spoken", 0.99, "inferred", ["new", "new"])
        fact = p.get()[0]
        self.assertEqual(fact["confidence"], 0.7)
        self.assertEqual(fact["evidence_ids"], ["spoken", "new"])
        self.assertEqual(fact["source"], "self_report")

    def test_migration_latest_sir_only_and_idempotency(self):
        from sir_profile import SirProfile

        source = self.root / "mind.db"
        with sqlite3.connect(source) as db:
            db.execute("CREATE TABLE entries (id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO entries VALUES ('e1')")
            db.execute("""CREATE TABLE belief_versions (
                seq INTEGER PRIMARY KEY, belief_id TEXT, revision INTEGER, subject TEXT,
                attribute TEXT, value TEXT, source TEXT, obtained_at TEXT, confidence REAL,
                last_verified TEXT, importance REAL, memory_id TEXT, refs TEXT)""")
            db.executemany(
                "INSERT INTO belief_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        1,
                        "n",
                        1,
                        "Sir",
                        "姓名",
                        "old",
                        "inferred",
                        "2020-01-01",
                        0.5,
                        "2020",
                        1,
                        None,
                        "[]",
                    ),
                    (
                        2,
                        "n",
                        2,
                        "Sir",
                        "姓名",
                        "actual",
                        "self_report",
                        "2021-01-01",
                        1,
                        "2021",
                        1,
                        None,
                        '["e1","missing"]',
                    ),
                    (
                        3,
                        "other",
                        1,
                        "weather",
                        "outside",
                        "rain",
                        "observed",
                        "2021",
                        0.8,
                        "2021",
                        0.5,
                        None,
                        "[]",
                    ),
                    (
                        4,
                        "custom",
                        1,
                        "Sir",
                        "自定义属性",
                        "literal value",
                        "observed",
                        "2021",
                        0.8,
                        "2021",
                        0.5,
                        None,
                        "[]",
                    ),
                ],
            )
        p = SirProfile(self.root)
        result = p.migrate_from_beliefs(source)
        self.assertEqual(result["migrated"], 2)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(p.get(key="name")[0]["value"], "actual")
        self.assertEqual(p.get(key="name")[0]["evidence_ids"], ["e1"])
        self.assertIn("missing", p.get(key="name")[0]["notes"])
        self.assertEqual(p.migrate_from_beliefs(source)["unchanged"], 2)
        self.assertEqual(len(p.get()), 2)


class SelfEvaluationTests(unittest.TestCase):
    def hook(self):
        import importlib

        module = importlib.import_module("sir_profile")
        return module, object.__new__(module.SirProfile).self_evaluate

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
        self.assertEqual(report["module"], "sir_profile")
        self.assertIn("canonical", report["current_version"].lower())
        self.assertIn("opaque", " ".join(report["known_limitations"]).lower())
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
        self.assertEqual(second["module"], "sir_profile")
