"""Read-only product path policy evaluation."""

import unittest
from unittest.mock import patch
from test_ops_self_evaluation import evaluate


class ProductHomeEvaluationTests(unittest.TestCase):
    def test_current_policy_and_invalid_mutation(self):
        module, result = evaluate(self, "product_home")
        self.assertTrue(result["ok"])
        self.assertEqual(result["directory_names"], list(module.NAMES))
        with patch.object(module, "NAMES", ("home", "../escape")):
            _, bad = evaluate(self, "product_home")
        self.assertFalse(bad["ok"])
        self.assertFalse(bad["checks"]["safe_directory_names"])
