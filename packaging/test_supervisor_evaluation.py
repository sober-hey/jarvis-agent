"""Supervisor hook evaluates identity semantics without process/lock IO."""

import unittest
from unittest.mock import patch
from test_ops_self_evaluation import evaluate


class SupervisorEvaluationTests(unittest.TestCase):
    def test_real_identity_predicate_and_negative_control(self):
        module, result = evaluate(self, "supervisor")
        self.assertTrue(result["ok"])
        self.assertTrue(result["checks"]["pid_reuse_rejected"])
        with patch.object(module, "same_identity", return_value=True):
            _, bad = evaluate(self, "supervisor")
        self.assertFalse(bad["checks"]["pid_reuse_rejected"])
        self.assertFalse(bad["ok"])

    def test_no_supervisor_or_owner_is_constructed(self):
        module, _ = evaluate(self, "supervisor")
        with (
            patch.object(module, "Supervisor", side_effect=AssertionError("constructor")),
            patch.object(module, "OwnerLock", side_effect=AssertionError("owner")),
            patch.object(module, "process_table", side_effect=AssertionError("ps")),
        ):
            _, result = evaluate(self, "supervisor")
        self.assertTrue(result["ok"])
