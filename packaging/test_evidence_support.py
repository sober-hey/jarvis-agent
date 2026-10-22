"""Direct tests for evidence allocation and metadata boundaries."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evidence_support import evidence_directory


class EvidenceDirectoryTests(unittest.TestCase):
    def test_explicit_override_allocates_unique_siblings_without_rewriting_old_output(self):
        with tempfile.TemporaryDirectory() as base:
            with patch.dict(os.environ, {"JARVIS_TEST_EVIDENCE_DIR": base}):
                first = evidence_directory("cognition")
                (first / "sentinel").write_text("fixture")
                second = evidence_directory("cognition")
            self.assertEqual(first.parent, Path(base).resolve())
            self.assertEqual(second.parent, first.parent)
            self.assertNotEqual(first, second)
            self.assertEqual((first / "sentinel").read_text(), "fixture")

    def test_empty_override_rejected_before_creation(self):
        with patch.dict(os.environ, {"JARVIS_TEST_EVIDENCE_DIR": "  "}):
            with self.assertRaises(ValueError):
                evidence_directory("cognition")


class SelfEvaluationTests(unittest.TestCase):
    def test_owned_hook_returns_honest_schema_without_private_io(self):
        import importlib
        import json
        from datetime import datetime, timezone
        from unittest.mock import patch

        module = importlib.import_module("evidence_support")
        hook = getattr(module, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-owned self_evaluate is missing")
        before = datetime.now(timezone.utc)
        with (
            patch("builtins.open", side_effect=AssertionError("private read")),
            patch("pathlib.Path.open", side_effect=AssertionError("private read")),
            patch("sqlite3.connect", side_effect=AssertionError("database access")),
            patch("subprocess.Popen", side_effect=AssertionError("external process")),
            patch("socket.socket.connect", side_effect=AssertionError("network")),
        ):
            report = hook()
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
        self.assertEqual(report["module"], "evidence_support")
        self.assertIn("mkdtemp", report["current_version"])
        for field in ("domain", "current_version"):
            self.assertGreater(len(report[field]), 10)
        for field in ("known_limitations", "improvement_directions"):
            self.assertTrue(report[field])
            self.assertTrue(all(isinstance(item, str) and len(item) > 15 for item in report[field]))
        evaluated = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(evaluated.utcoffset())
        self.assertGreaterEqual(evaluated, before)
        self.assertLessEqual(evaluated, datetime.now(timezone.utc))
        json.dumps(report)

    def test_reports_own_mutable_lists(self):
        import importlib

        module = importlib.import_module("evidence_support")
        hook = getattr(module, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-owned self_evaluate is missing")
        report = hook()
        report["known_limitations"].clear()
        report["improvement_directions"].clear()
        fresh = hook()
        self.assertTrue(fresh["known_limitations"])
        self.assertTrue(fresh["improvement_directions"])


if __name__ == "__main__":
    unittest.main()
