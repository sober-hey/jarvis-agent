"""Config launcher hook exposes public route schema and limits, never secrets."""

import unittest
from unittest.mock import patch
from test_ops_self_evaluation import evaluate


class ConfigLauncherEvaluationTests(unittest.TestCase):
    def test_real_schema_and_limit_policy(self):
        module, result = evaluate(self, "config_launcher")
        self.assertTrue(result["ok"])
        self.assertEqual(result["current_version"], module.VERSION)
        self.assertEqual(result["route_fields"], sorted(module.config.CONFIG_FIELDS))
        self.assertEqual(result["chat_byte_limit"], module.CHAT_CONTEXT_BYTE_LIMIT)
        with patch.object(module, "CHAT_CONTEXT_BYTE_LIMIT", 0):
            _, bad = evaluate(self, "config_launcher")
        self.assertFalse(bad["checks"]["positive_prompt_budget"])
        self.assertFalse(bad["ok"])

    def test_secret_field_schema_drift_is_reported(self):
        module, _ = evaluate(self, "config_launcher")
        with patch.object(
            module.config, "CONFIG_FIELDS", {*module.config.CONFIG_FIELDS, "api_key"}
        ):
            _, bad = evaluate(self, "config_launcher")
        self.assertFalse(bad["checks"]["nonsecret_route_schema"])
