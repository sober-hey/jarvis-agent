"""Offline evaluation and real local adapter behavior for ToolRouter."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import tool_router


class SelfEvaluationTests(unittest.TestCase):
    def test_evaluation_has_own_scope_and_requires_no_adapters(self):
        hook = getattr(tool_router, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-level self_evaluate is required")
        with (
            patch.object(Path, "open", side_effect=AssertionError("unexpected file IO")),
            patch.object(Path, "mkdir", side_effect=AssertionError("unexpected mkdir")),
            patch("subprocess.Popen", side_effect=AssertionError("unexpected process")),
            patch.object(tool_router.ToolRouter, "_adapter", side_effect=AssertionError("adapter")),
        ):
            report = hook()
        self.assertEqual(report["module"], "tool_router")
        for key in ("domain", "current_version"):
            self.assertTrue(report[key].strip())
        for key in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[key], list)
            self.assertTrue(report[key])
            self.assertTrue(all(isinstance(value, str) and value.strip() for value in report[key]))
        self.assertIn("audit", " ".join(report["known_limitations"]).lower())
        self.assertIsNotNone(datetime.fromisoformat(report["last_evaluated_at"]).utcoffset())
        report["known_limitations"].clear()
        self.assertTrue(hook()["known_limitations"])
        json.dumps(report)


class RouterBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wave2-router-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.router = tool_router.ToolRouter(allowed_roots=[self.root], repo_root=self.root)

    def test_routes_real_file_and_omits_content_from_audit(self):
        written = self.router.route("write_file", {"path": "note.txt", "content": "private-body"})
        self.assertEqual(written["status"], "completed", written)
        self.assertEqual((self.root / "note.txt").read_text(), "private-body")
        result = self.router.route("read_file", {"path": "note.txt"})
        self.assertEqual(result["result"]["content"], "private-body")
        self.assertNotIn("private-body", json.dumps(written["audit"]))

    def test_malformed_and_escape_arguments_have_no_file_effect(self):
        result = self.router.route("write_file", {"path": "bad", "content": 42})
        self.assertEqual(result["error"], "invalid_arguments")
        result = self.router.route("write_file", {"path": "../escape", "content": "x"})
        self.assertEqual(result["status"], "denied")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_audit_failure_does_not_retry_completed_effect(self):
        class FailingStore:
            def append(self, *args, **kwargs):
                raise RuntimeError("storage unavailable")

        self.router.mind_store = FailingStore()
        import file_ops

        with patch.object(
            file_ops.FileOps, "write_file", autospec=True, side_effect=file_ops.FileOps.write_file
        ) as writer:
            result = self.router.route("write_file", {"path": "once", "content": "x"})
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["audit"]["persisted"])
        self.assertEqual((self.root / "once").read_text(), "x")

    def test_exported_schema_is_detached_and_construction_is_lazy(self):
        self.assertEqual(self.router._instances, {})
        schema = self.router.to_openai_tools()
        schema[0]["function"]["parameters"]["properties"].clear()
        self.assertTrue(self.router.registry["read_file"]["parameters"]["properties"])
        self.assertEqual(self.router._instances, {})
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
