"""CLI self-evaluation checks public distribution policy, not check()/launch."""

import unittest
from unittest.mock import patch
from test_ops_self_evaluation import evaluate


class JarvisEvaluationTests(unittest.TestCase):
    def test_resource_manifest_tracks_real_constants(self):
        module, result = evaluate(self, "jarvis")
        self.assertTrue(result["ok"])
        self.assertEqual(result["current_version"], module.VERSION)
        self.assertEqual(result["resources"], list(module.RESOURCES))
        with patch.object(module, "RESOURCES", ("LICENSE", "../escape")):
            _, bad = evaluate(self, "jarvis")
        self.assertFalse(bad["checks"]["safe_resource_manifest"])
        self.assertFalse(bad["ok"])

    def test_child_environment_is_actually_tested(self):
        module, _ = evaluate(self, "jarvis")
        original = module.child_environment

        def insecure(home, venv, env):
            return dict(original(home, venv, env), OPENAI_API_KEY="sentinel-not-a-key")

        with patch.object(module, "child_environment", side_effect=insecure):
            _, bad = evaluate(self, "jarvis")
        self.assertFalse(bad["checks"]["sanitized_environment"])
        self.assertNotIn("sentinel-not-a-key", str(bad))
