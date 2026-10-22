"""Bounded local outreach acceptance: no daemon, real model, or external send.

These fixtures exercise the real mind -> Wiring -> Outreach -> product timeline
publisher. The synthetic model is the only authored leaf. State and source IDs
are read back from the real stores; assertions never depend on user-facing words.
"""

from __future__ import annotations

import json
import smtplib
import socket
import sqlite3
import tempfile
import time
import unittest
from contextlib import ExitStack, closing
from datetime import datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import daemon_wiring

CST = ZoneInfo("Asia/Shanghai")


class LocalOutreachAcceptance(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="jarvis-outreach-acceptance-", dir=Path("/tmp").resolve()
        )
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.root.mkdir(mode=0o700)
        self.now = self.at(12).timestamp()
        self.urgency = "normal"
        self.calls: list[dict[str, Any]] = []
        self.acceptance: dict[str, Any] = {"steps": []}
        guards = ExitStack()
        self.addCleanup(guards.close)
        self.forbidden = [
            guards.enter_context(patch.object(smtplib, "SMTP", side_effect=AssertionError("SMTP"))),
            guards.enter_context(
                patch.object(smtplib, "SMTP_SSL", side_effect=AssertionError("SMTP_SSL"))
            ),
            guards.enter_context(patch("outreach.send_email", side_effect=AssertionError("send"))),
            guards.enter_context(
                patch.object(socket.socket, "connect", side_effect=AssertionError("network"))
            ),
            guards.enter_context(
                patch.object(daemon_wiring.launcher, "read_key", side_effect=AssertionError("key"))
            ),
            guards.enter_context(
                patch.object(daemon_wiring.launcher, "launch", side_effect=AssertionError("model"))
            ),
        ]
        self.wiring = self.reopen()

    @staticmethod
    def at(hour: int, minute: int = 0, *, day: int = 18, second: int = 0) -> datetime:
        return datetime(2026, 10, day, hour, minute, second, tzinfo=CST)

    def reopen(self) -> daemon_wiring.Wiring:
        return daemon_wiring.Wiring(self.root, self.model, fixture=True, clock=lambda: self.now)

    def model(self, prompt: str) -> dict[str, Any]:
        context = json.loads(prompt)
        self.calls.append(context)
        if "outreach_intent" in context:
            return {"text": json.dumps({"text": "synthetic:" + context["outreach_intent"]["id"]})}
        event = [row for row in context["events"] if row["source"] == "synthetic-acceptance"][-1]
        return {
            "text": json.dumps(
                {
                    "thoughts": [
                        {
                            "id": "t1",
                            "kind": "inference",
                            "text": "synthetic concern",
                            "refs": [event["id"]],
                            "question": None,
                        }
                    ],
                    "resolved_questions": [],
                    "outreach_intents": [
                        {
                            "reason": "synthetic-change:" + event["id"],
                            "evidence": [event["id"]],
                            "urgency": self.urgency,
                            "needs_response": False,
                        }
                    ],
                }
            )
        }

    def produce(self, urgency: str = "normal") -> str:
        self.urgency = urgency
        self.wiring.mind.ingest(
            "tool_result",
            {"synthetic": True, "ordinal": len(self.calls)},
            source="synthetic-acceptance",
        )
        self.assertTrue(self.wiring.think())
        row = self.wiring.mind.store.recent("outreach_intent", 1)[0]
        self.assertEqual(row["payload"]["urgency"], urgency)
        self.assertTrue(row["payload"]["evidence"])
        self.wiring.contact()
        # Use the real persisted shared call bucket: allow its 15s refill rather
        # than mocking/resetting cognitive or outward-render budgets.
        self.now += daemon_wiring.CALL_REFILL_SECONDS
        return str(row["payload"]["id"])

    def presence(self, online: bool) -> None:
        # This is the actual persisted presence input read by Wiring.sir_online,
        # not a replacement of the decision function or accelerated wall clock.
        with closing(sqlite3.connect(self.root / "web-presence.db")) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS presence(singleton INTEGER PRIMARY KEY,seen_at REAL)"
            )
            db.execute(
                "INSERT OR REPLACE INTO presence VALUES(1,?)", (time.time() if online else 0,)
            )
        self.assertEqual(self.wiring.sir_online(), online)

    def snapshot(self, name: str, count: int, rendered: int) -> dict[str, Any]:
        timeline = daemon_wiring.launcher.read_timeline(self.root)
        deliveries = self.wiring.outreach.web_events()
        drafts = sorted((self.root / "outbox").glob("*.eml"))
        generated = [row for row in self.calls if "outreach_intent" in row]
        self.assertEqual(len(timeline), count, name)
        self.assertEqual(len(deliveries), count, name)
        self.assertEqual(len(drafts), count, name)
        self.assertEqual(len(generated), rendered, name)
        self.assertEqual(len({row["outreach_delivery_key"] for row in timeline}), count)
        parsed: list[dict[str, Any]] = []
        for delivery in deliveries:
            rows = [row for row in timeline if row["outreach_delivery_key"] == delivery.key]
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["source_channel"], "jarvis_proactive")
            self.assertEqual(row["content"], delivery.text)
            self.assertFalse(row["generation_degraded"])
            self.assertFalse(row["needs_response"])
            self.assertTrue(row["evidence"])
            for identity in row["evidence"]:
                self.assertIsNotNone(self.wiring.mind.store.get(identity))
            mail = BytesParser(policy=policy.default).parsebytes(
                (self.root / "outbox" / (delivery.key + ".eml")).read_bytes()
            )
            self.assertEqual(mail["X-JARVIS-Outbox-Only"], "yes")
            self.assertEqual(mail.get_content().strip(), row["content"].strip())
            parsed.append(
                {
                    "filename": delivery.key + ".eml",
                    "headers": dict(mail.items()),
                    "body": mail.get_content(),
                }
            )
        with closing(sqlite3.connect(self.wiring.outreach.db_path)) as db:
            intents = [
                {"id": row[0], "status": row[1], "payload": json.loads(row[2])}
                for row in db.execute("SELECT id,status,payload FROM intents ORDER BY rowid")
            ]
        record = {
            "name": name,
            "status": "PASS",
            "now": datetime.fromtimestamp(self.now, CST).isoformat(),
            "timeline": timeline,
            "drafts": parsed,
            "intents": intents,
            "histories": {
                row["id"]: self.wiring.outreach.intent_history(row["id"]) for row in intents
            },
            "thought_calls": len(self.calls) - len(generated),
            "outreach_render_calls": len(generated),
            "mind_thoughts": list(self.wiring.mind.store.iter_entries("thought")),
            "mind_outreach_intents": list(self.wiring.mind.store.iter_entries("outreach_intent")),
        }
        self.acceptance["steps"].append(record)
        return record

    def tearDown(self) -> None:
        for guard in self.forbidden:
            guard.assert_not_called()
        self.acceptance["forbidden_call_counts"] = [guard.call_count for guard in self.forbidden]
        self.acceptance["model_inputs"] = self.calls

    def test_inner_intent_quiet_boundary_and_restart(self) -> None:
        self.now = self.at(0).timestamp()
        self.produce()
        self.wiring.dispatch()
        self.snapshot("midnight_queued_without_render", 0, 0)
        self.now = self.at(7, 29, second=59).timestamp()
        self.wiring = self.reopen()
        self.wiring.contact()
        self.wiring.dispatch()
        self.snapshot("07_29_59_still_queued_after_restart", 0, 0)
        self.now = self.at(7, 30).timestamp()
        self.wiring.dispatch()
        self.snapshot("07_30_released_to_timeline_and_draft", 1, 1)
        self.wiring = self.reopen()
        self.wiring.contact()
        self.wiring.dispatch()
        self.snapshot("restart_no_duplicate_or_regeneration", 1, 1)

    def test_rolling_hour_limit_through_mind_and_timeline(self) -> None:
        self.now = self.at(11, 55).timestamp()
        self.presence(True)
        for urgency in ("high", "normal", "low"):
            self.produce(urgency)
        self.now = self.at(12).timestamp()
        self.wiring.dispatch()
        self.snapshot("two_per_rolling_hour_including_high", 2, 2)
        self.now = self.at(12, 59, second=59).timestamp()
        self.wiring = self.reopen()
        self.wiring.dispatch()
        self.snapshot("one_second_before_hour_no_render", 2, 2)
        self.now = self.at(13).timestamp()
        self.wiring.dispatch()
        self.snapshot("exact_hour_releases_remaining_intent", 3, 3)

    def test_daily_limit_persists_until_next_cst_day(self) -> None:
        self.now = self.at(7, 40).timestamp()
        self.presence(True)
        for _ in range(9):
            self.produce("high")
        for hour, count in ((8, 2), (10, 4), (12, 6), (14, 8)):
            self.now = self.at(hour).timestamp()
            self.wiring.dispatch()
            self.snapshot(f"daily_budget_{count}", count, count)
        self.now = self.at(23, 59, second=59).timestamp()
        self.wiring = self.reopen()
        self.wiring.dispatch()
        self.snapshot("ninth_deferred_for_full_cst_day", 8, 8)
        self.now = self.at(7, 29, day=19, second=59).timestamp()
        self.wiring.dispatch()
        self.snapshot("next_day_still_obeys_quiet_hours", 8, 8)
        self.now = self.at(7, 30, day=19).timestamp()
        self.wiring.dispatch()
        self.snapshot("next_day_07_30_releases_ninth", 9, 9)

    def test_offline_quota_and_emergency_bypass_are_audited(self) -> None:
        self.now = self.at(12).timestamp()
        self.presence(False)
        for _ in range(3):
            self.produce()
        self.wiring.dispatch()
        self.snapshot("offline_one_nonemergency_only", 1, 1)
        self.now = self.at(14).timestamp()
        self.wiring = self.reopen()
        self.wiring.dispatch()
        self.snapshot("offline_quota_survives_restart_and_elapsed_hour", 1, 1)
        urgent = self.produce("emergency")
        self.wiring.dispatch()
        self.snapshot("emergency_bypasses_offline_quota", 2, 2)
        self.assertTrue(
            any(
                "offline" in e.get("bypass", [])
                for e in self.wiring.outreach.intent_history(urgent)
            )
        )
        self.presence(True)
        self.wiring.dispatch()
        self.snapshot("online_releases_remaining_two", 4, 4)
        self.now = self.at(1, day=19).timestamp()
        quiet_urgent = self.produce("emergency")
        self.wiring.dispatch()
        self.snapshot("emergency_bypasses_quiet_hours", 5, 5)
        self.assertTrue(
            any(
                "quiet_hours" in e.get("bypass", [])
                for e in self.wiring.outreach.intent_history(quiet_urgent)
            )
        )


if __name__ == "__main__":
    unittest.main()
