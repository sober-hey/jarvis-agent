"""Bootstrap hook must inspect its real fixed forwarding target without executing it."""

from pathlib import Path
import unittest
from unittest.mock import patch
from test_ops_self_evaluation import evaluate


class BootstrapEvaluationTests(unittest.TestCase):
    def test_real_forward_target_and_missing_target(self):
        module, result = evaluate(self, "bootstrap")
        self.assertTrue(result["ok"])
        self.assertEqual(result["entrypoint"], "jarvis.py")
        with patch.object(module, "__file__", str(Path("/nonexistent-ops-hook/bootstrap.py"))):
            _, absent = evaluate(self, "bootstrap")
        self.assertFalse(absent["checks"]["entrypoint_present"])
        self.assertFalse(absent["ok"])
