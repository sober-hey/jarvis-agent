"""Engine hook checks static isolation policy without invoking self_test."""

from pathlib import Path
import unittest
from unittest.mock import patch
from test_ops_self_evaluation import evaluate


class EngineEvaluationTests(unittest.TestCase):
    def test_environment_and_fixed_layout_and_invalid_mutation(self):
        module, result = evaluate(self, "engine_entry")
        self.assertTrue(result["ok"])
        self.assertEqual(result["upstream_commit"], module.COMMIT)
        self.assertFalse(result["sdk_initialization_unmodified"])
        with patch.object(module, "PYTHON", Path("/unrelated/python3")):
            _, bad = evaluate(self, "engine_entry")
        self.assertFalse(bad["ok"])
        self.assertFalse(bad["checks"]["fixed_interpreter_layout"])

    def test_environment_policy_change_is_visible_without_leaking_values(self):
        module, _ = evaluate(self, "engine_entry")
        original = module.environment

        def insecure(root):
            return dict(original(root), OPENAI_API_KEY="sentinel-not-a-real-key")

        with patch.object(module, "environment", side_effect=insecure):
            _, bad = evaluate(self, "engine_entry")
        self.assertFalse(bad["checks"]["minimal_environment"])
        self.assertNotIn("sentinel-not-a-real-key", str(bad))
