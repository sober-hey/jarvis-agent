"""Default lifecycle policies are inspected without factories, probes or processes."""

from dataclasses import replace
import unittest
from unittest.mock import patch
from test_ops_self_evaluation import evaluate


class SupervisorComponentsEvaluationTests(unittest.TestCase):
    def test_live_default_policy_and_invalid_policy(self):
        module, result = evaluate(self, "supervisor_components")
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["health_policy"]["timeout"], module.Health("heartbeat", "/unopened").timeout
        )
        self.assertEqual(result["restart_policy"]["max_delay"], module.Restart().max_delay)
        with patch.object(module, "Restart", return_value=replace(module.Restart(), max_delay=0)):
            _, bad = evaluate(self, "supervisor_components")
        self.assertFalse(bad["checks"]["bounded_restart_policy"])
        self.assertFalse(bad["ok"])

    def test_no_factory_or_health_probe_is_executed(self):
        module, _ = evaluate(self, "supervisor_components")
        with (
            patch.object(module, "default_components", side_effect=AssertionError("factory")),
            patch.object(module, "probe", side_effect=AssertionError("health IO")),
        ):
            _, result = evaluate(self, "supervisor_components")
        self.assertTrue(result["ok"])
