"""Preflight hook runs parser/classifier policy only, not host collection."""

import unittest
from unittest.mock import patch
from test_ops_self_evaluation import evaluate


class LinuxPreflightEvaluationTests(unittest.TestCase):
    def test_real_parser_classifier_and_orphan_limit(self):
        module, result = evaluate(self, "linux_preflight")
        self.assertTrue(result["ok"])
        self.assertEqual(result["runtime_wiring"], "pending")
        self.assertFalse(result["production_ready"])
        self.assertTrue(any("consumer" in text for text in result["known_limitations"]))
        with patch.object(module, "parse_os_release", return_value={}):
            _, bad = evaluate(self, "linux_preflight")
        self.assertFalse(bad["checks"]["literal_os_parser"])
        self.assertFalse(bad["ok"])

    def test_hook_does_not_collect_host_and_detects_false_readiness(self):
        module, _ = evaluate(self, "linux_preflight")
        original = module.classify_environment

        def unsafe(*args, **kwargs):
            return dict(original(*args, **kwargs), production_ready=True)

        with (
            patch.object(module, "collect_environment", side_effect=AssertionError("collector")),
            patch.object(module, "classify_environment", side_effect=unsafe),
        ):
            _, bad = evaluate(self, "linux_preflight")
        self.assertFalse(bad["checks"]["no_production_acceptance_claim"])
