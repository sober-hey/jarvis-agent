"""Deterministic engineering thresholds, synthetic dictionaries only."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from health_rules import HealthRules
from test_vision import assert_evaluation


class HealthRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"], prefix="health-rules-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rules = HealthRules(self.root)

    def test_self_evaluate_reports_non_diagnostic_wrist_baseline_and_active_version(self):
        report = assert_evaluation(self, self.rules.self_evaluate, "health_rules")
        text = " ".join(report["known_limitations"])
        self.assertIn("腕温", text)
        self.assertIn("偏差", text)
        self.assertIn("诊断", text)
        self.assertIn(str(self.rules.version), report["current_version"])

    def test_strict_single_value_thresholds(self):
        for metric, field, value, expected in (
            ("heart_rate", "avg_resting_heart_rate", 100, []),
            ("heart_rate", "avg_resting_heart_rate", 101, ["critical"]),
            ("heart_rate", "avg_resting_heart_rate", 45, []),
            ("heart_rate", "avg_resting_heart_rate", 44, ["warning"]),
            ("spo2", "min_spo2", 95, []),
            ("spo2", "min_spo2", 94, ["critical"]),
            ("sleep", "avg_sleep_hours", 5, []),
            ("sleep", "avg_sleep_hours", 4, ["warning"]),
        ):
            with self.subTest(metric=metric, value=value):
                alerts = self.rules.evaluate(dict(metric=metric, **{field: value}))
                self.assertEqual([a.severity for a in alerts], expected)
                for alert in alerts:
                    self.assertIn("非诊断", alert.message)
                    self.assertEqual(json.loads(alert.context)["rules_version"], 1)

    def test_invalid_readings_are_missing_not_zero(self):
        for metric, field in (
            ("heart_rate", "avg_resting_heart_rate"),
            ("spo2", "min_spo2"),
            ("sleep", "avg_sleep_hours"),
        ):
            for value in (None, True, "2", float("nan"), float("inf")):
                self.assertEqual(self.rules.evaluate(dict(metric=metric, **{field: value})), [])
        self.assertEqual(self.rules.evaluate(None), [])
        self.assertEqual(self.rules.evaluate({"metric": "unknown"}), [])

    def test_streak_requires_consecutive_dates_and_valid_values(self):
        for metric, field, count, value, severity in (
            ("sleep", "sleep_hours", 3, 5.5, "critical"),
            ("spo2", "avg_spo2", 3, 96, "warning"),
            ("steps", "steps", 7, 2000, "info"),
        ):
            rows = [{"date": f"2026-10-{day:02}", field: value} for day in range(1, count + 1)]
            analysis = dict(metric=metric, end_date=f"2026-10-{count:02}", daily=rows)
            with self.subTest(metric=metric):
                self.assertEqual([a.severity for a in self.rules.evaluate(analysis)], [severity])
                self.assertEqual(self.rules.evaluate({**analysis, "daily": rows[1:]}), [])
                self.assertEqual(self.rules.evaluate({**analysis, "end_date": "2026-10-20"}), [])
                self.assertEqual(
                    self.rules.evaluate({**analysis, "daily": [{**r, field: None} for r in rows]}),
                    [],
                )

    def test_wrist_deviation_only_not_absolute_body_temperature(self):
        for analysis, expected in (
            (dict(kind="body", avg_temperature=40), 0),
            (dict(kind="wrist", avg_temperature=40), 0),
            (dict(kind="wrist", deviation_from_baseline=1.5), 0),
            (dict(kind="wrist", deviation_from_baseline=-1.6), 1),
            (dict(kind="wrist", avg_temperature=35, baseline_temperature=33), 1),
        ):
            alerts = self.rules.evaluate(dict(metric="temperature", **analysis))
            self.assertEqual(len(alerts), expected)
            if alerts:
                self.assertIn("不使用绝对体温判断", alerts[0].message)

    def test_valid_override_is_used_and_other_instances_keep_defaults(self):
        (self.root / "health_rules.json").write_text(
            json.dumps({"version": 1, "rules": {"sleep_low": {"threshold": 7}}})
        )
        modified = HealthRules(self.root)
        self.assertEqual(modified.thresholds["sleep_low"], 7)
        self.assertEqual(len(modified.evaluate({"metric": "sleep", "avg_sleep_hours": 6})), 1)
        self.assertEqual(self.rules.evaluate({"metric": "sleep", "avg_sleep_hours": 6}), [])

    def test_invalid_override_rejected(self):
        for payload in (
            {"version": 2},
            {"version": 1, "rules": {"invented": {"threshold": 1}}},
            {"version": 1, "rules": {"sleep_low": {"threshold": True}}},
            {"version": 1, "rules": {"sleep_low": {"threshold": -1}}},
            {"version": 1, "rules": {"sleep_low": {"threshold": 1, "other": 2}}},
        ):
            (self.root / "health_rules.json").write_text(json.dumps(payload))
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                HealthRules(self.root)

    def test_daily_summary_delegates_sections_without_inventing_history(self):
        result = self.rules.evaluate(
            {
                "date": "2026-10-01",
                "heart_rate": {"avg_resting_heart_rate": 110},
                "sleep": {"avg_sleep_hours": 4},
                "steps": {"avg_steps": 1000},
            }
        )
        self.assertEqual({a.metric for a in result}, {"heart_rate", "sleep"})


if __name__ == "__main__":
    unittest.main()
