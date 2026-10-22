"""Synthetic-only scheduler tests; never touch the operator's live ledgers."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import product_home
from health_alert import HealthAlertManager
from health_analyzer import HealthAnalyzer
from health_rules import HealthRules
from health_scheduler import HealthScheduler
from inner_loop import MindStore
from knowledge_graph import KnowledgeGraph

CST = ZoneInfo("Asia/Shanghai")


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 10, 19, 7, 59, tzinfo=CST)

    def __call__(self):
        return self.value


class FakeOutreach:
    def __init__(self):
        self.messages: list[dict[str, Any]] = []
        self.fail = False

    def enqueue(self, text: str, **kwargs: Any) -> str:
        if self.fail:
            raise RuntimeError("synthetic outreach failure")
        identity = f"outreach-{len(self.messages)}"
        self.messages.append({"id": identity, "text": text, **kwargs})
        return identity


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        # This override permits ONLY the test-owned scratch root; it does not
        # disable production directory/file-mode checks or touch live stores.
        self.temp = tempfile.TemporaryDirectory(
            prefix="health-scheduler-", dir=os.environ["TMPDIR"]
        )
        self.root = Path(self.temp.name)
        self.allowed = patch.object(product_home, "protected_paths", return_value=())
        self.allowed.start()
        self.clock = FakeClock()
        self.db = self.root / "synthetic-health.db"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "CREATE TABLE points(metric TEXT,ts TEXT,day TEXT,qty REAL,"
                "vmin REAL,vmax REAL,source TEXT)"
            )
            conn.execute(
                "CREATE TABLE sleep(day TEXT,total REAL,deep REAL,sleep_start TEXT,sleep_end TEXT)"
            )
        self.mind = MindStore(self.root)
        self.kg = KnowledgeGraph(self.root)
        self.manager = HealthAlertManager(self.root, self.kg)
        self.outreach = FakeOutreach()
        self.analyzer = HealthAnalyzer(self.db, self.root)
        self.schedulers: list[HealthScheduler] = []
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.allowed.stop)
        self.addCleanup(lambda: [s.close() for s in self.schedulers])

    def scheduler(self) -> HealthScheduler:
        obj = HealthScheduler(
            self.root,
            self.analyzer,
            HealthRules(self.root),
            self.manager,
            self.outreach,
            self.mind,
            self.clock,
        )
        self.schedulers.append(obj)
        return obj

    def run_tick(self, scheduler: HealthScheduler):
        scheduler.tick()
        self.assertTrue(scheduler.wait(10), "background job did not finish")
        self.assertIsNone(scheduler.self_evaluate()["last_error_type"])

    def seed(self, day: str = "2026-10-18", critical: bool = False):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for metric, value in [
                ("resting_heart_rate", 120 if critical else 65),
                ("heart_rate", 70),
                ("blood_oxygen_saturation", 98),
                ("step_count", 7000),
                ("body_temperature", 36.5),
                ("heart_rate_variability", 45),
            ]:
                conn.execute(
                    "INSERT INTO points VALUES(?,?,?,?,?,?,?)",
                    (metric, f"{day}T12:00:00+08:00", day, value, value, value, "Watch"),
                )
            conn.execute(
                "INSERT INTO sleep VALUES(?,?,?,?,?)",
                (day, 7.5, 1.5, "2026-10-17T23:30:00+08:00", "2026-10-18T07:00:00+08:00"),
            )

    def event(
        self,
        source: str = "activitywatch",
        kind: str = "app_usage",
        observed: str = "2026-10-18T01:00:00+08:00",
        **data: Any,
    ):
        return self.mind.append(
            "event",
            {
                "kind": "tool_result",
                "data": {
                    "sense_event": {
                        "source": source,
                        "kind": kind,
                        "observed_at": observed,
                        "payload": {"app": "Editor", "duration_seconds": 600, **data},
                    }
                },
            },
            source="test-sense",
        )

    def reports(self):
        return self.mind.entries(category="health_daily_report", limit=100)

    def test_before_eight_does_not_send_morning_report(self):
        self.seed()
        self.run_tick(self.scheduler())
        self.assertEqual(self.outreach.messages, [])
        self.assertEqual(self.reports(), [])

    def test_after_eight_once_and_restart_no_resend(self):
        self.seed()
        obj = self.scheduler()
        self.clock.value = self.clock.value.replace(hour=8, minute=0)
        self.run_tick(obj)
        self.run_tick(obj)
        self.run_tick(self.scheduler())
        self.assertEqual(len(self.outreach.messages), 1)
        self.assertEqual(len(self.reports()), 1)
        msg = self.outreach.messages[0]
        self.assertEqual(msg["urgency"], "normal")
        self.assertTrue(msg["text"].startswith("Sir：\n\n"))
        self.assertTrue(msg["text"].endswith("\n\nJARVIS"))
        self.assertLessEqual(len(msg["text"].split("\n\n")[1]), 300)
        self.assertEqual(
            set(self.reports()[0]["payload"]["metrics"]),
            {"heart_rate", "sleep", "steps", "spo2", "temperature"},
        )
        self.assertTrue(any(n["key"] == "health_daily_report" for n in self.kg.get_all_nodes()))
        self.assertEqual((self.root / "health_scheduler_state.json").stat().st_mode & 0o777, 0o600)

    def test_critical_immediate_dedup_and_restart_24h(self):
        self.seed(critical=True)
        obj = self.scheduler()
        self.run_tick(obj)
        self.assertEqual([m["urgency"] for m in self.outreach.messages], ["high"])
        self.clock.value += timedelta(minutes=30)
        self.run_tick(obj)
        self.run_tick(self.scheduler())
        self.assertEqual(len([m for m in self.outreach.messages if m["urgency"] == "high"]), 1)
        self.clock.value += timedelta(hours=24)
        self.seed(day="2026-10-19", critical=True)
        self.run_tick(obj)
        self.assertEqual(len([m for m in self.outreach.messages if m["urgency"] == "high"]), 2)

    def test_latest_today_data_is_checked_before_morning(self):
        self.seed(day="2026-10-19", critical=True)
        self.run_tick(self.scheduler())
        self.assertEqual([m["urgency"] for m in self.outreach.messages], ["high"])

    def test_thirty_minute_sentinel_budget(self):
        self.clock.value = self.clock.value.replace(hour=6, minute=0)
        self.seed()
        with patch.object(
            self.analyzer, "daily_summary", wraps=self.analyzer.daily_summary
        ) as summary:
            obj = self.scheduler()
            self.run_tick(obj)
            count = summary.call_count
            self.clock.value += timedelta(minutes=29)
            self.run_tick(obj)
            self.assertEqual(summary.call_count, count)
            self.clock.value += timedelta(minutes=1)
            self.run_tick(obj)
            self.assertGreater(summary.call_count, count)

    def test_missing_data_is_explicit_not_healthy_or_zero(self):
        self.clock.value = self.clock.value.replace(hour=8)
        self.run_tick(self.scheduler())
        report = self.reports()[0]["payload"]
        self.assertTrue(all(v["status"] == "missing" for v in report["metrics"].values()))
        self.assertIsNone(report["context"]["last_computer_activity_at"])
        self.assertIn("缺测", report["body"])
        self.assertFalse(any(e["edge_type"] == "caused_by" for e in self.kg.get_all_edges()))

    def test_night_context_browser_minimization_and_precedes_only(self):
        self.seed()
        self.event()
        self.event(
            source="browser_history",
            kind="page_visit",
            observed="2026-10-18T00:20:00+08:00",
            title="Private title",
            url="https://private.example/secret?q=secret",
        )
        self.clock.value = self.clock.value.replace(hour=8)
        self.run_tick(self.scheduler())
        report = self.reports()[0]["payload"]
        self.assertTrue(report["context"]["late_computer_activity"])
        self.assertEqual(report["context"]["browser_visit_count"], 1)
        self.assertIn("01:10", report["context"]["last_computer_activity_at"])
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("Private title", serialized)
        self.assertNotIn("private.example", serialized)
        edges = self.kg.get_all_edges()
        self.assertGreaterEqual(len([e for e in edges if e["edge_type"] == "precedes"]), 2)
        self.assertNotIn("caused_by", {e["edge_type"] for e in edges})
        nodes = {n["id"]: n for n in self.kg.get_all_nodes()}
        linked = [nodes[e["to_node"]]["key"] for e in edges if e["edge_type"] == "precedes"]
        self.assertIn("next_day_resting_heart_rate", linked)
        self.assertIn("next_day_hrv", linked)

    def test_backfill_and_synthetic_activity_do_not_become_sleep_evidence(self):
        self.seed()
        self.event(synthetic=True)
        self.event(kind="backfill_summary")
        self.clock.value = self.clock.value.replace(hour=8)
        self.run_tick(self.scheduler())
        self.assertIsNone(self.reports()[0]["payload"]["context"]["last_computer_activity_at"])
        self.assertFalse(any(e["edge_type"] == "precedes" for e in self.kg.get_all_edges()))

    def test_tick_never_waits_for_blocking_analyzer(self):
        started, release = threading.Event(), threading.Event()
        original = self.analyzer.daily_summary

        def slow(*args: Any, **kwargs: Any):
            started.set()
            release.wait(5)
            return original(*args, **kwargs)

        with patch.object(self.analyzer, "daily_summary", side_effect=slow):
            obj = self.scheduler()
            begin = time.monotonic()
            obj.tick()
            self.assertLess(time.monotonic() - begin, 0.2)
            self.assertTrue(started.wait(1))
            obj.tick()
            release.set()
            self.assertTrue(obj.wait(10))

    def test_outreach_failure_retries_without_duplicate_report_or_graph(self):
        self.seed()
        self.clock.value = self.clock.value.replace(hour=8)
        self.outreach.fail = True
        obj = self.scheduler()
        obj.tick()
        self.assertTrue(obj.wait(10))
        self.assertEqual(obj.self_evaluate()["last_error_type"], "RuntimeError")
        self.outreach.fail = False
        self.clock.value += timedelta(seconds=61)
        self.run_tick(self.scheduler())
        self.assertEqual(len(self.reports()), 1)
        self.assertEqual(len(self.outreach.messages), 1)

    def test_timezone_and_next_day(self):
        self.seed()
        obj = self.scheduler()
        self.clock.value = datetime.fromisoformat("2026-10-19T00:00:00+00:00")
        self.run_tick(obj)
        self.clock.value += timedelta(days=1)
        self.run_tick(obj)
        self.assertEqual(len(self.reports()), 2)

    def test_pending_critical_survives_midnight_and_old_data_window(self):
        self.seed(critical=True)
        self.clock.value = self.clock.value.replace(hour=23, minute=59)
        self.outreach.fail = True
        obj = self.scheduler()
        obj.tick()
        self.assertTrue(obj.wait(10))
        self.clock.value += timedelta(minutes=2)
        self.outreach.fail = False
        self.run_tick(self.scheduler())
        self.assertEqual(len([m for m in self.outreach.messages if m["urgency"] == "high"]), 1)

    def test_graph_failure_does_not_block_critical_outreach(self):
        self.seed(critical=True)
        with patch.object(
            self.manager, "process_alerts", side_effect=RuntimeError("graph unavailable")
        ):
            obj = self.scheduler()
            obj.tick()
            self.assertTrue(obj.wait(10))
        self.assertEqual(len([m for m in self.outreach.messages if m["urgency"] == "high"]), 1)

    def test_report_retry_does_not_create_spurious_correlations(self):
        self.seed()
        self.event()
        self.clock.value = self.clock.value.replace(hour=8)
        self.outreach.fail = True
        obj = self.scheduler()
        obj.tick()
        self.assertTrue(obj.wait(10))
        self.outreach.fail = False
        self.clock.value += timedelta(minutes=1)
        self.run_tick(self.scheduler())
        self.assertEqual({e["edge_type"] for e in self.kg.get_all_edges()}, {"precedes"})
        self.assertEqual(self.reports()[0]["payload"]["metrics"]["sleep"]["status"], "observed")

    def test_naive_clock_is_rejected(self):
        self.clock.value = datetime(2026, 10, 19, 8)
        with self.assertRaises(ValueError):
            self.scheduler().tick()

    def test_0800_report_is_not_delayed_by_sentinel_cooldown(self):
        self.seed()
        obj = self.scheduler()
        self.run_tick(obj)
        self.clock.value += timedelta(minutes=1)
        self.run_tick(obj)
        self.assertEqual(len(self.reports()), 1)

    def test_corrupt_state_fails_closed_without_resending(self):
        self.seed(critical=True)
        path = self.root / "health_scheduler_state.json"
        path.write_text("{broken", encoding="utf-8")
        path.chmod(0o600)
        obj = self.scheduler()
        obj.tick()
        self.assertTrue(obj.wait(10))
        self.assertEqual(obj.self_evaluate()["last_error_type"], "JSONDecodeError")
        self.assertEqual(self.outreach.messages, [])
        self.assertEqual(path.read_text(), "{broken")

    def test_durable_outreach_recovers_enqueue_before_save_crash(self):
        self.seed(critical=True)
        from outreach import Outreach

        durable = Outreach(self.root)
        obj = HealthScheduler(
            self.root,
            self.analyzer,
            HealthRules(self.root),
            self.manager,
            durable,
            self.mind,
            self.clock,
        )
        self.schedulers.append(obj)
        original = obj._save  # pyright: ignore[reportPrivateUsage] -- crash injection seam
        failed = False

        def save(state: dict[str, Any]):
            nonlocal failed
            if not failed and any("message_id" in r for r in state["critical"].values()):
                failed = True
                raise OSError("synthetic crash after durable enqueue")
            original(state)

        with patch.object(obj, "_save", side_effect=save):
            obj.tick()
            self.assertTrue(obj.wait(10))
        self.clock.value += timedelta(minutes=1)
        recovered = HealthScheduler(
            self.root,
            self.analyzer,
            HealthRules(self.root),
            self.manager,
            durable,
            self.mind,
            self.clock,
        )
        self.schedulers.append(recovered)
        self.run_tick(recovered)
        with closing(sqlite3.connect(durable.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM messages WHERE urgency='high'").fetchone()[0], 1
            )

    def test_dry_run_has_no_outreach_or_persistence(self):
        self.seed(critical=True)
        obj = self.scheduler()
        result = obj.dry_run()
        self.assertGreater(result["alert_count"], 0)
        self.assertEqual(self.outreach.messages, [])
        self.assertEqual(self.reports(), [])
        self.assertEqual(self.kg.get_all_nodes(), [])
        self.assertFalse((self.root / "health_scheduler_state.json").exists())


if __name__ == "__main__":
    unittest.main()
