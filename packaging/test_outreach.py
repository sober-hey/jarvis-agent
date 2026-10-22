"""Outreach acceptance tests; all data stays in disposable scratch roots."""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    import outreach as outreach_types
    from outreach import Delivery

SPEC = importlib.util.spec_from_file_location("outreach", Path(__file__).with_name("outreach.py"))
outreach: Any = None
if SPEC is not None and SPEC.loader is not None and Path(str(SPEC.origin)).exists():
    import sys

    outreach = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = outreach
    SPEC.loader.exec_module(outreach)
if TYPE_CHECKING:
    outreach = outreach_types

SCRATCH = Path(tempfile.gettempdir())
CST = ZoneInfo("Asia/Shanghai")


def at(hour: int, minute: int = 0, day: int = 15) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=CST)


class OutreachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(outreach, "outreach.py must implement the durable queue")
        self.temp = tempfile.TemporaryDirectory(prefix="outreach-test-", dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def queue(self) -> outreach_types.Outreach:
        def demo_digest(messages: tuple[outreach_types.Message, ...], _kind: str) -> str:
            # Explicit caller-side fixture, never a product text template.
            return "\n\n".join(message.text for message in messages)

        return outreach.Outreach(self.root, digest_builder=demo_digest)

    def test_default_mail_writes_eml_without_any_smtp_connection(self) -> None:
        from email import policy
        from email.parser import BytesParser
        from unittest.mock import patch

        queue = self.queue()
        self.assertTrue(hasattr(queue, "dispatch_due"), "dispatch_due must deliver to local outbox")
        with (
            patch("smtplib.SMTP", side_effect=AssertionError("SMTP forbidden")) as smtp,
            patch("smtplib.SMTP_SSL", side_effect=AssertionError("SMTP_SSL forbidden")) as ssl,
            patch(
                "socket.create_connection", side_effect=AssertionError("network forbidden")
            ) as network,
        ):
            message_id = queue.enqueue(
                "今天复习顺利吗？", reason="询问状态", channel="email", now=at(10)
            )
            deliveries = queue.dispatch_due(at(10))
            self.assertEqual(len(deliveries), 1)
            self.assertEqual(queue.get(message_id).status, "sent")
            files = list((self.root / "outbox").glob("*.eml"))
            self.assertEqual(len(files), 1)
            mail = BytesParser(policy=policy.default).parsebytes(files[0].read_bytes())
            self.assertEqual(str(mail["To"]), "sir@example.invalid")
            self.assertEqual(str(mail.get_content()).rstrip("\r\n"), "今天复习顺利吗？")
            self.assertEqual(queue.dispatch_due(at(10)), [])
            smtp.assert_not_called()
            ssl.assert_not_called()
            network.assert_not_called()

    def test_dedupe_within_window_then_expires_and_does_not_refresh(self) -> None:
        queue = self.queue()
        first = queue.enqueue("一", reason="去重", dedupe_key="check", now=at(10))
        duplicate = queue.enqueue("二", reason="去重", dedupe_key="check", now=at(10, 1))
        self.assertEqual(queue.get(duplicate).status, "suppressed")
        self.assertEqual(len(queue.dispatch_due(at(10, 2))), 1)
        self.assertEqual(queue.get(first).status, "sent")
        again = queue.enqueue("三", reason="去重", dedupe_key="check", now=at(11))
        self.assertEqual(queue.get(again).status, "suppressed")
        later = queue.enqueue("四", reason="窗口到期", dedupe_key="check", now=at(17))
        self.assertEqual(queue.get(later).status, "queued")
        self.assertEqual(len(queue.dispatch_due(at(17))), 1)

    def test_hourly_caps_defer_and_high_bypasses_cap(self) -> None:
        queue = self.queue()
        low1 = queue.enqueue("低一", reason="频控", urgency="low", now=at(10))
        low2 = queue.enqueue("低二", reason="频控", urgency="low", now=at(10))
        normal1 = queue.enqueue("普通一", reason="频控", now=at(10))
        normal2 = queue.enqueue("普通二", reason="频控", now=at(10))
        high = queue.enqueue("高", reason="高优先级", urgency="high", now=at(10))
        sent = queue.dispatch_due(at(10))
        self.assertEqual(len(sent), 3)
        for message_id in (low1, normal1, high):
            self.assertEqual(queue.get(message_id).status, "sent")
        for message_id in (low2, normal2):
            self.assertEqual(queue.get(message_id).status, "queued")
        self.assertEqual(queue.dispatch_due(at(10, 59)), [])
        self.assertEqual(len(queue.dispatch_due(at(11))), 2)

    def test_quiet_period_accumulates_then_one_digest_at_boundary(self) -> None:
        queue = self.queue()
        ids = [queue.enqueue(f"夜间{i}", reason="积压", now=at(0, i)) for i in range(3)]
        self.assertEqual(queue.dispatch_due(at(7, 29)), [])
        queue = self.queue()
        sent = queue.dispatch_due(at(7, 30))
        self.assertEqual(len(sent), 1)
        self.assertEqual(set(sent[0].message_ids), set(ids))
        for i in range(3):
            self.assertIn(f"夜间{i}", sent[0].text)
        self.assertTrue(all(queue.get(i).status == "sent" for i in ids))
        self.assertEqual(queue.dispatch_due(at(7, 30)), [])

    def test_backlog_enqueued_before_quiet_is_also_digest(self) -> None:
        queue = self.queue()
        queue.enqueue("昨晚的消息", reason="没有运行调度", now=at(23, day=14))
        queue.enqueue("清晨的消息", reason="积压", now=at(5))
        self.assertEqual(len(queue.dispatch_due(at(8))), 1)

    def test_emergency_immediately_dispatches_even_while_studying_and_quiet(self) -> None:
        queue = self.queue()
        first = queue.enqueue(
            "紧急一",
            reason="演示紧急",
            urgency="emergency",
            studying=True,
            dedupe_key="same",
            now=at(1),
        )
        second = queue.enqueue(
            "紧急二",
            reason="演示紧急",
            urgency="emergency",
            studying=True,
            dedupe_key="same",
            now=at(1),
        )
        self.assertEqual(queue.get(first).status, "sent")
        self.assertEqual(queue.get(second).status, "sent")
        self.assertEqual(len(queue.history(first)), 2)
        self.assertEqual(queue.dispatch_due(at(1)), [])

    def test_high_keeps_dedupe_but_bypasses_quiet(self) -> None:
        queue = self.queue()
        first = queue.enqueue("高", reason="重要", urgency="high", dedupe_key="same", now=at(1))
        duplicate = queue.enqueue(
            "高重复", reason="重要", urgency="high", dedupe_key="same", now=at(1)
        )
        self.assertEqual(queue.get(duplicate).status, "suppressed")
        self.assertEqual(len(queue.dispatch_due(at(1))), 1)
        self.assertEqual(queue.get(first).status, "sent")

    def test_studying_low_is_once_daily_and_next_batch_waits_next_day(self) -> None:
        queue = self.queue()
        ids = [
            queue.enqueue(f"低{i}", reason="备考", urgency="low", studying=True, now=at(10))
            for i in range(3)
        ]
        self.assertEqual(queue.dispatch_due(at(17, 59)), [])
        sent = queue.dispatch_due(at(18))
        self.assertEqual(len(sent), 1)
        self.assertEqual(set(sent[0].message_ids), set(ids))
        last = queue.enqueue(
            "晚来的低消息", reason="备考", urgency="low", studying=True, now=at(19)
        )
        self.assertEqual(queue.dispatch_due(at(20)), [])
        self.assertEqual(queue.get(last).status, "queued")
        self.assertEqual(len(queue.dispatch_due(at(18, day=16))), 1)

    def test_naive_time_and_empty_payload_rejected_before_writes(self) -> None:
        queue = self.queue()
        with self.assertRaises(ValueError):
            queue.enqueue("内容", reason="原因", now=datetime(2026, 10, 15))
        with self.assertRaises(ValueError):
            queue.enqueue("", reason="原因", now=at(10))
        with self.assertRaises(ValueError):
            queue.dispatch_due(datetime(2026, 10, 15))

    def test_web_events_are_durable_and_restart_does_not_resend(self) -> None:
        queue = self.queue()
        queue.enqueue("网页消息", reason="推送测试", now=at(10))
        delivery = queue.dispatch_due(at(10))[0]
        restarted = outreach.Outreach(self.root)
        self.assertEqual(restarted.web_events(0)[0].text, "网页消息")
        self.assertEqual(restarted.web_events(delivery.id), [])
        self.assertEqual(restarted.dispatch_due(at(11)), [])

    def test_long_queued_key_is_still_only_sent_once_and_window_uses_sent_time(self) -> None:
        queue = self.queue()
        original = queue.enqueue("等待很久的消息", reason="旧积压", dedupe_key="delayed", now=at(0))
        newer = queue.enqueue("同一件事的更新", reason="迟到", dedupe_key="delayed", now=at(8))
        sent = queue.dispatch_due(at(8))
        self.assertEqual(len(sent), 1)
        self.assertEqual(queue.get(original).status, "sent")
        self.assertEqual(queue.get(newer).status, "suppressed")
        third = queue.enqueue("再次重复", reason="重复", dedupe_key="delayed", now=at(9))
        self.assertEqual(queue.get(third).status, "suppressed")

    def test_sse_real_socket_gets_live_delivery_and_resumes_cursor(self) -> None:
        import http.client
        import json
        import socket
        import threading

        queue = self.queue()
        server = outreach.DemoServer(queue)
        port = server.server_port
        runner = threading.Thread(target=server.serve_forever, daemon=True)
        runner.start()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        resumed = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request("GET", "/events")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "text/event-stream; charset=utf-8")
            self.assertEqual(response.readline(), b": connected\n")
            self.assertEqual(response.readline(), b"\n")
            queue.enqueue(
                "实时消息 <script>alert(1)</script>",
                reason="SSE 真连接",
                urgency="high",
                now=at(10),
            )
            delivery = queue.dispatch_due(at(10))[0]
            self.assertEqual(response.readline(), f"id: {delivery.id}\n".encode())
            self.assertEqual(response.readline(), b"event: outreach\n")
            payload = json.loads(response.readline()[6:])
            self.assertEqual(payload["text"], delivery.text)
            self.assertEqual(response.readline(), b"\n")
            resumed.request("GET", "/events", headers={"Last-Event-ID": str(delivery.id)})
            replay = resumed.getresponse()
            self.assertEqual(replay.readline(), b": connected\n")
            self.assertEqual(replay.readline(), b"\n")
            queue.enqueue("续接消息", reason="重连", urgency="high", now=at(10, 1))
            second = queue.dispatch_due(at(10, 1))[0]
            self.assertEqual(replay.readline(), f"id: {second.id}\n".encode())
            self.assertEqual(replay.readline(), b"event: outreach\n")
            self.assertEqual(json.loads(replay.readline()[6:])["text"], "续接消息")
            response.close()
            replay.close()
        finally:
            server.stop_event.set()
            conn.close()
            resumed.close()
            server.shutdown()
            server.server_close()
            runner.join(timeout=3)
        self.assertFalse(runner.is_alive())
        with socket.socket() as probe:
            probe.settimeout(1)
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)

    def test_outbox_failure_is_append_only_failed_and_not_retried(self) -> None:
        queue = self.queue()
        (self.root / "outbox").write_text("blocked path", encoding="utf-8")
        message_id = queue.enqueue("消息", reason="失败测试", channel="both", now=at(10))
        self.assertEqual(queue.dispatch_due(at(10)), [])
        self.assertEqual(queue.get(message_id).status, "failed")
        self.assertEqual([e.status for e in queue.history(message_id)], ["queued", "failed"])
        self.assertEqual(queue.web_events(), [])
        self.assertEqual(queue.dispatch_due(at(11)), [])

    def test_concurrent_dispatchers_and_enqueue_dedupe_are_serialized(self) -> None:
        import concurrent.futures

        queue = self.queue()

        def enqueue_one(_index: int) -> str:
            return outreach.Outreach(self.root).enqueue(
                "相同事项", reason="并发去重", urgency="high", dedupe_key="concurrent", now=at(10)
            )

        def dispatch_one(_index: int) -> list[Delivery]:
            return outreach.Outreach(self.root).dispatch_due(at(10))

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(enqueue_one, range(8)))
            batches = list(pool.map(dispatch_one, range(4)))
        self.assertEqual(sum(len(batch) for batch in batches), 1)
        self.assertEqual(sum(queue.get(i).status == "suppressed" for i in ids), 7)
        self.assertEqual(len(queue.web_events()), 1)

    def test_local_demo_serves_page_rejects_bad_cursor_and_foreign_host(self) -> None:
        import http.client
        import threading

        server = outreach.DemoServer(outreach.Outreach(self.root))
        runner = threading.Thread(target=server.serve_forever, daemon=True)
        runner.start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            conn.request("GET", "/outreach-demo.html")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b"EventSource('/events')", response.read())
            conn.request("GET", "/events?after=oops")
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            conn.request("GET", "/", headers={"Host": "evil.example"})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            conn.close()
            server.stop_event.set()
            server.shutdown()
            server.server_close()
            runner.join(timeout=3)

    def test_routing_matrix_respects_interruptibility_and_urgency(self) -> None:
        self.assertEqual(
            outreach.select_channel("normal", outreach.SirState(voice_available=True)), "voice"
        )
        self.assertEqual(
            outreach.select_channel(
                "normal", outreach.SirState(voice_available=True, can_interrupt=False)
            ),
            "web",
        )
        self.assertEqual(
            outreach.select_channel(
                "normal",
                outreach.SirState(voice_available=True, studying=True, web_available=False),
            ),
            "email",
        )
        self.assertEqual(
            outreach.select_channel(
                "emergency", outreach.SirState(imessage_available=True, web_available=False)
            ),
            "imessage",
        )
        self.assertEqual(
            outreach.select_channel("low", outreach.SirState(web_available=False)), "email"
        )

    def test_fake_voice_and_imessage_never_invoke_platform_or_network(self) -> None:
        import json
        from unittest.mock import patch

        queue = self.queue()
        with (
            patch("subprocess.Popen", side_effect=AssertionError("platform forbidden")) as process,
            patch(
                "socket.create_connection", side_effect=AssertionError("network forbidden")
            ) as network,
        ):
            for channel in ("voice", "imessage"):
                queue.enqueue(
                    "【演示文本】接着刚才的事情说。",
                    reason="【演示文本】假通道",
                    urgency="high",
                    channel=channel,
                    now=at(10),
                )
            self.assertEqual(len(queue.dispatch_due(at(10))), 2)
            for channel in ("voice", "imessage"):
                files = list((self.root / "channel-stubs" / channel).glob("*.json"))
                self.assertEqual(len(files), 1)
                payload = json.loads(files[0].read_text(encoding="utf-8"))
                self.assertIs(payload["fake"], True)
                self.assertEqual(payload["delivery"]["text"], "【演示文本】接着刚才的事情说。")
            process.assert_not_called()
            network.assert_not_called()

    def test_missing_caller_digest_author_keeps_backlog_queued(self) -> None:
        queue = outreach.Outreach(self.root)
        message_id = queue.enqueue(
            "【演示文本】前面那件事有补充。", reason="【演示文本】补充", now=at(1)
        )
        self.assertEqual(queue.dispatch_due(at(8)), [])
        self.assertEqual(queue.get(message_id).status, "queued")

    def test_caller_digest_text_is_used_verbatim(self) -> None:
        expected = "【演示文本】昨晚那两件事放一起看：先改时间，再确认路线。"

        def author(_items: tuple[outreach_types.Message, ...], _kind: str) -> str:
            return expected

        queue = outreach.Outreach(self.root, digest_builder=author)
        queue.enqueue(
            "【演示文本】时间改了。", reason="【演示文本】安排", channel="email", now=at(1)
        )
        queue.enqueue(
            "【演示文本】路线还没定。", reason="【演示文本】路线", channel="email", now=at(2)
        )
        self.assertEqual(queue.dispatch_due(at(8))[0].text, expected)

    def test_auto_routing_prefers_voice_and_uses_only_fake_adapters(self) -> None:
        queue = self.queue()
        state = outreach.SirState(voice_available=True, web_available=False)
        message_id = queue.enqueue(
            "刚才那件事，我又想到一点。", reason="继续之前的事情", channel="auto", now=at(10)
        )
        deliveries = queue.dispatch_due(at(10), state=state)
        self.assertEqual(deliveries[0].channel, "voice")
        self.assertEqual(queue.get(message_id).status, "sent")
        files = list((self.root / "channel-stubs" / "voice").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(queue.web_events(), [])

    def test_state_routes_high_to_imessage_while_studying(self) -> None:
        queue = self.queue()
        state = outreach.SirState(
            studying=True, voice_available=True, imessage_available=True, web_available=False
        )
        queue.enqueue(
            "之前那个安排需要你看一下。",
            reason="重要变更",
            urgency="high",
            channel="auto",
            now=at(10),
        )
        delivery = queue.dispatch_due(at(10), state=state)[0]
        self.assertEqual(delivery.channel, "imessage")
        self.assertEqual(len(list((self.root / "channel-stubs" / "imessage").glob("*.json"))), 1)

    def test_auto_route_quiet_message_uses_current_state_at_dispatch(self) -> None:
        queue = self.queue()
        queue.enqueue("早一点说的那件事，有个补充。", reason="补充", channel="auto", now=at(1))
        self.assertEqual(queue.dispatch_due(at(1)), [])
        delivery = queue.dispatch_due(at(7, 30), state=outreach.SirState(web_available=False))[0]
        self.assertEqual(delivery.channel, "email")
        self.assertEqual(len(list((self.root / "outbox").glob("*.eml"))), 1)

    def test_emergency_auto_routes_without_real_send_or_wait(self) -> None:
        queue = self.queue()
        state = outreach.SirState(studying=True, voice_available=True, can_interrupt=False)
        message_id = queue.enqueue(
            "Sir，这件事现在需要你看一下。",
            reason="演示紧急事项",
            urgency="emergency",
            channel="auto",
            state=state,
            now=at(1),
        )
        self.assertEqual(queue.get(message_id).status, "sent")
        self.assertEqual(len(list((self.root / "channel-stubs" / "voice").glob("*.json"))), 1)

    def test_study_digest_does_not_send_twice_on_overlapping_channels(self) -> None:
        queue = self.queue()
        queue.enqueue(
            "先放网页", reason="备考", channel="web", urgency="low", studying=True, now=at(10)
        )
        queue.dispatch_due(at(18))
        both = queue.enqueue(
            "网页和邮件", reason="备考", channel="both", urgency="low", studying=True, now=at(19)
        )
        self.assertEqual(queue.dispatch_due(at(20)), [])
        self.assertEqual(queue.get(both).status, "queued")

    def test_caller_can_change_studying_state_without_recreating_queue(self) -> None:
        queue = self.queue()
        message_id = queue.enqueue(
            "之前那件小事", reason="备考状态由调用方决定", urgency="low", now=at(10)
        )
        self.assertEqual(queue.dispatch_due(at(10), studying=True), [])
        self.assertEqual(queue.get(message_id).status, "queued")
        self.assertEqual(len(queue.dispatch_due(at(11), studying=False)), 1)

    def test_both_channel_has_one_logical_delivery_and_one_mail(self) -> None:
        queue = self.queue()
        message_id = queue.enqueue(
            "之前说的安排，我又想到一点。", reason="继续已有的事情", channel="both", now=at(10)
        )
        sent = queue.dispatch_due(at(10))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].message_ids, (message_id,))
        self.assertEqual(len(queue.web_events()), 1)
        self.assertEqual(len(list((self.root / "outbox").glob("*.eml"))), 1)
        self.assertEqual([e.status for e in queue.history(message_id)], ["queued", "sent"])

    def test_transport_is_disabled_by_default_and_never_invoked(self) -> None:
        from email.message import EmailMessage

        class ForbiddenSender:
            def send(self, message: EmailMessage) -> None:
                raise AssertionError("real transport must not be called")

        with self.assertRaisesRegex(PermissionError, "disabled"):
            outreach.send_email(EmailMessage(), ForbiddenSender())

    def test_utc_inputs_use_cst_quiet_boundary(self) -> None:

        queue = self.queue()
        queue.enqueue("时间换算测试", reason="UTC 调用者", now=at(7, 29).astimezone(UTC))
        self.assertEqual(queue.dispatch_due(at(7, 29).astimezone(UTC)), [])
        self.assertEqual(len(queue.dispatch_due(at(7, 30).astimezone(UTC))), 1)

    def test_restart_retains_message_and_append_only_events(self) -> None:
        queue = self.queue()
        message_id = queue.enqueue(
            "Sir，想问一下今天复习顺利吗？", reason="需要了解复习状态", now=at(10)
        )
        restarted = outreach.Outreach(self.root)
        message = restarted.get(message_id)
        self.assertEqual(message.text, "Sir，想问一下今天复习顺利吗？")
        self.assertEqual(message.status, "queued")
        self.assertEqual(len(restarted.history(message_id)), 1)
        restarted.dispatch_due(at(10))
        from contextlib import closing

        with closing(sqlite3.connect(self.root / "outreach.db")) as db:
            for table in ("messages", "events", "deliveries"):
                for operation in (f"DELETE FROM {table}", f"UPDATE {table} SET id=id"):
                    with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                        db.execute(operation)


class SelfEvaluationTests(unittest.TestCase):
    def test_owned_hook_returns_honest_schema_without_private_io(self):
        import importlib
        import json
        from datetime import datetime, timezone
        from unittest.mock import patch

        module = importlib.import_module("outreach")
        hook = getattr(module, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-owned self_evaluate is missing")
        before = datetime.now(timezone.utc)
        with (
            patch("builtins.open", side_effect=AssertionError("private read")),
            patch("pathlib.Path.open", side_effect=AssertionError("private read")),
            patch("sqlite3.connect", side_effect=AssertionError("database access")),
            patch("subprocess.Popen", side_effect=AssertionError("external process")),
            patch("socket.socket.connect", side_effect=AssertionError("network")),
        ):
            report = hook()
        self.assertEqual(
            set(report),
            {
                "module",
                "domain",
                "current_version",
                "known_limitations",
                "improvement_directions",
                "last_evaluated_at",
            },
        )
        self.assertEqual(report["module"], "outreach")
        self.assertIn("SQLite", report["current_version"])
        for field in ("domain", "current_version"):
            self.assertGreater(len(report[field]), 10)
        for field in ("known_limitations", "improvement_directions"):
            self.assertTrue(report[field])
            self.assertTrue(all(isinstance(item, str) and len(item) > 15 for item in report[field]))
        evaluated = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(evaluated.utcoffset())
        self.assertGreaterEqual(evaluated, before)
        self.assertLessEqual(evaluated, datetime.now(timezone.utc))
        json.dumps(report)

    def test_reports_own_mutable_lists(self):
        import importlib

        module = importlib.import_module("outreach")
        hook = getattr(module, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-owned self_evaluate is missing")
        report = hook()
        report["known_limitations"].clear()
        report["improvement_directions"].clear()
        fresh = hook()
        self.assertTrue(fresh["known_limitations"])
        self.assertTrue(fresh["improvement_directions"])


if __name__ == "__main__":
    unittest.main()
