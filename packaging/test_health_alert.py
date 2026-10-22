"""Synthetic alert manager behavior using real isolated SQLite ledgers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from health_alert import HealthAlertManager
from health_rules import HealthAlert
from knowledge_graph import KnowledgeGraph
from test_vision import assert_evaluation
import product_home


class HealthAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"], prefix="health-alert-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.allowed = patch.object(product_home, "protected_paths", return_value=())
        self.allowed.start()
        self.addCleanup(self.allowed.stop)
        self.graph = KnowledgeGraph(self.root)
        self.manager = HealthAlertManager(self.root, self.graph)

    def test_self_evaluate_describes_partial_persistence_and_no_delivery(self):
        report = assert_evaluation(self, self.manager.self_evaluate, "health_alert")
        text = " ".join(report["known_limitations"])
        for term in ("critical", "1000", "去重", "送达"):
            self.assertIn(term, text)

    def test_only_critical_in_mind_all_alerts_in_graph_with_provenance(self):
        now = datetime.now(UTC).isoformat()
        alerts = [
            dict(
                id="fixture-" + severity,
                severity=severity,
                metric="sleep",
                message="合成提醒",
                observed_at=now,
                evidence_ids=["fixture-ref"],
            )
            for severity in ("critical", "warning", "info")
        ]
        advice = self.manager.process_alerts(alerts)
        self.assertEqual(len(advice), 3)
        self.assertTrue(all(text.startswith("Sir，") for text in advice))
        nodes = self.graph.get_all_nodes()
        self.assertEqual(len(nodes), 3)
        self.assertEqual({n["node_type"] for n in nodes}, {"health_event"})
        rows = self.manager.mind.entries(category="health_alert", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["refs"], ["fixture-ref"])
        self.assertEqual(rows[0]["payload"]["severity"], "critical")
        self.assertEqual(len(self.manager.get_recent_alerts()), 1)

    def test_recent_alerts_use_observed_time_and_ignore_malformed_rows(self):
        now = datetime.now(UTC)
        for observed in (
            (now - timedelta(days=20)).isoformat(),
            "invalid",
            now.replace(tzinfo=None).isoformat(),
            now.isoformat(),
        ):
            self.manager.mind.append("health_alert", {"observed_at": observed}, source="fixture")
        rows = self.manager.get_recent_alerts(days=7)
        self.assertEqual(len(rows), 1)
        for days in (True, -1, 1.5):
            with self.assertRaises(ValueError):
                self.manager.get_recent_alerts(days)

    def test_normalization_accepts_dataclass_and_sanitizes_non_json_values(self):
        alert = HealthAlert("warning", "sleep", "合成提醒", 4, 5)
        item = self.manager._normalize(alert)
        self.assertEqual(item["value"], 4)
        self.assertEqual(item["threshold"], 5)
        unsafe = self.manager._normalize(
            dict(severity="critical", metric="sleep", value=float("nan"), observed_at="naive")
        )
        self.assertNotIn("value", unsafe)
        self.assertIsNotNone(datetime.fromisoformat(unsafe["observed_at"]).utcoffset())
        with self.assertRaises(ValueError):
            self.manager._normalize(42)

    def test_report_is_bounded_and_does_not_write(self):
        before = self.manager.mind.entries(category="health_alert", limit=10)
        report = self.manager.generate_health_report(
            {"summary": "合成状态" * 100}, [dict(severity="critical", metric="sleep")]
        )
        self.assertLessEqual(len(report), 200)
        self.assertTrue(report.startswith("Sir，"))
        self.assertIn("sleep", report)
        self.assertEqual(self.manager.mind.entries(category="health_alert", limit=10), before)
        self.assertEqual(self.graph.get_all_nodes(), [])

    def test_graph_failure_does_not_claim_alert_persisted(self):
        with patch.object(
            self.graph, "add_node", side_effect=RuntimeError("synthetic graph failure")
        ):
            with self.assertRaises(RuntimeError):
                self.manager.process_alerts([dict(severity="critical", metric="sleep")])
        self.assertEqual(self.manager.mind.entries(category="health_alert", limit=10), [])


if __name__ == "__main__":
    unittest.main()
