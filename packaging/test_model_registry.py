"""Disposable offline acceptance tests; no real messages or provider calls."""

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, UTC
from unittest.mock import patch

from model_registry import ModelRegistry

ARTIFACTS = Path("/Volumes/扩展硬盘/hermes/.hermes/cache/scratch/wave2-cognition/research-tests")
ARTIFACTS.mkdir(parents=True, exist_ok=True)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ARTIFACTS)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = ModelRegistry(self.root)

    def test_seed_exact_and_private(self):
        row = self.registry.get("deepseek-flash")
        self.assertEqual(row["task_types"], ["general", "fast"])
        self.assertEqual(row["quality_score"], 0.72)
        self.assertEqual(row["has_key"], 1)
        self.assertEqual(row["key_path"], "~/.jarvis/api.key")
        self.assertEqual(self.registry.db_path.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(self.registry.get("missing"))
        self.assertEqual(len(self.registry.list_all()), 1)

    def test_patch_reopen_task_rank_and_key_boundary(self):
        for model, score, keyed in [("a", 0.9, 0), ("b", 0.95, 1), ("c", 0.8, 0)]:
            self.registry.upsert(
                model,
                provider="example",
                name=model,
                task_types=["code"],
                quality_score=score,
                has_key=keyed,
                source_urls=["https://example.org"],
            )
        self.registry.upsert("a", notes="updated")
        registry = ModelRegistry(self.root)
        self.assertEqual([m["id"] for m in registry.best_for_task("code")], ["b", "a", "c"])
        self.assertEqual([m["id"] for m in registry.needs_key_request()], ["a"])
        self.assertEqual(registry.get("a")["notes"], "updated")
        self.assertEqual(registry.get("a")["task_types"], ["code"])
        self.assertEqual(registry.best_for_task("coding"), [])
        self.registry.upsert("deepseek-flash", has_key=0, quality_score=0.91)
        self.assertEqual(ModelRegistry(self.root).get("deepseek-flash")["has_key"], 0)

    def test_bad_fields_and_json_are_rejected(self):
        for fields in [
            {"bad": "bad"},
            {"quality_score": float("nan")},
            {"quality_score": 1.1},
            {"task_types": "code"},
            {"has_key": 2},
            {"source_urls": [1]},
        ]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.registry.upsert("deepseek-flash", **fields)
        with sqlite3.connect(self.registry.db_path) as db:
            raw = db.execute("select task_types from models").fetchone()[0]
        self.assertEqual(json.loads(raw), ["general", "fast"])


class EffortSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ARTIFACTS)
        self.addCleanup(self.tmp.cleanup)
        self.registry = ModelRegistry(Path(self.tmp.name))

    def test_exact_nearest_and_lower_tie_effort(self):
        self.registry.upsert(
            "candidate",
            provider="fixture",
            name="Candidate",
            task_types=["code"],
            supported_efforts=["low", "high"],
        )
        self.assertEqual(self.registry.best_effort_for("candidate", "low"), "low")
        self.assertEqual(self.registry.best_effort_for("candidate", "medium"), "low")
        self.assertEqual(self.registry.best_effort_for("candidate", "ultra"), "high")
        self.registry.upsert("candidate", notes="preserve supported efforts")
        reopened = ModelRegistry(Path(self.tmp.name))
        self.assertEqual(reopened.get("candidate")["supported_efforts"], ["low", "high"])

    def test_missing_model_and_invalid_effort_sets_are_explicit(self):
        with self.assertRaises(KeyError):
            self.registry.best_effort_for("missing", "medium")
        with self.assertRaises(ValueError):
            self.registry.best_effort_for("deepseek-flash", "unknown")
        for efforts in ([], ["invented"]):
            self.registry.upsert(
                "candidate",
                provider="fixture",
                name="Candidate",
                task_types=["code"],
                supported_efforts=efforts,
            )
            with self.assertRaises(ValueError):
                self.registry.best_effort_for("candidate", "medium")

    def test_symlink_database_is_rejected_without_target_changes(self):
        root = Path(self.tmp.name) / "symlink-root"
        root.mkdir(mode=0o700)
        target = root / "untouched"
        target.write_bytes(b"test-only sentinel")
        (root / "model_registry.db").symlink_to(target)
        with self.assertRaises(OSError):
            ModelRegistry(root)
        self.assertEqual(target.read_bytes(), b"test-only sentinel")


class SelfEvaluationTests(unittest.TestCase):
    def evaluate(self):
        import importlib

        module = importlib.import_module("model_registry")
        self.assertTrue(
            callable(getattr(module.ModelRegistry, "self_evaluate", None)),
            "component-owned self_evaluate hook is missing",
        )
        return module.ModelRegistry.__new__(module.ModelRegistry).self_evaluate()

    def test_metadata_schema_identity_and_utc_timestamp(self):
        from datetime import datetime, timezone

        before = datetime.now(timezone.utc)
        report = self.evaluate()
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
        self.assertEqual(report["module"], "model_registry")
        self.assertIn("model", report["domain"].lower())
        for key in ("domain", "current_version"):
            self.assertIsInstance(report[key], str)
            self.assertTrue(report[key].strip())
        for key in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[key], list)
            self.assertTrue(report[key])
            self.assertTrue(all(isinstance(x, str) and x.strip() for x in report[key]))
        stamp = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.utcoffset().total_seconds(), 0)
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, datetime.now(timezone.utc))

    def test_metadata_never_reads_credentials_database_or_network(self):
        from unittest.mock import patch

        # Import first, then deny file, database and network boundaries. The
        # instance has no attributes, so even reading data_root would fail.
        import importlib

        importlib.import_module("model_registry")
        with (
            patch("builtins.open", side_effect=AssertionError("file read forbidden")),
            patch("pathlib.Path.open", side_effect=AssertionError("path read forbidden")),
            patch("sqlite3.connect", side_effect=AssertionError("runtime DB forbidden")),
            patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
        ):
            report = self.evaluate()
        import json

        self.assertIsInstance(json.dumps(report), str)

    def test_reports_do_not_share_mutable_metadata(self):
        first = self.evaluate()
        expected = self.evaluate()
        first["known_limitations"].clear()
        first["improvement_directions"].append("test-only mutation")
        fresh = self.evaluate()
        self.assertEqual(fresh["known_limitations"], expected["known_limitations"])
        self.assertEqual(fresh["improvement_directions"], expected["improvement_directions"])
