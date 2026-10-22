"""Structured outreach queue: local fixtures, no model or external transport."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from outreach import Outreach

CST = ZoneInfo("Asia/Shanghai")


def at(hour: int, minute: int = 0, day: int = 16) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=CST)


def intent(key: str, *, urgency: str = "normal", reason: str | None = None) -> dict[str, Any]:
    return {
        "id": key,
        "reason": reason or f"observed-change:{key}",
        "evidence": [f"sensor:{key}"],
        "urgency": urgency,
        "needs_response": True,
    }


def render(item: dict[str, Any]) -> dict[str, Any]:
    return {"text": f"fixture generated words for {item['id']}", "generation_degraded": False}


class IntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="outreach-intents-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = Outreach(self.root)

    def test_draft_publish_is_atomic_across_interrupted_fsync_and_restart(self) -> None:
        from email import policy
        from email.parser import BytesParser
        from unittest.mock import patch

        self.queue.enqueue_intent(intent("atomic-draft"), now=at(10))
        with patch("outreach.os.fsync", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.queue.dispatch_intents(at(10), render=render)
        self.assertEqual(list((self.root / "outbox").glob("*.eml")), [])
        recovered = Outreach(self.root)
        recovered.dispatch_intents(at(10), render=lambda _: self.fail("must not regenerate"))
        drafts = list((self.root / "outbox").glob("*.eml"))
        self.assertEqual(len(drafts), 1)
        parsed = BytesParser(policy=policy.default).parsebytes(drafts[0].read_bytes())
        self.assertEqual(parsed["X-JARVIS-Outbox-Only"], "yes")
        self.assertEqual(recovered.intent_history("atomic-draft")[-1]["status"], "sent")

    def test_provenance_and_intent_audit_cannot_be_rewritten(self) -> None:
        import sqlite3
        from contextlib import closing

        self.queue.enqueue_intent(intent("immutable"), now=at(10))
        delivery = self.queue.dispatch_intents(at(10), render=render)[0]
        with closing(sqlite3.connect(self.queue.db_path)) as db:
            for table in ("intent_events", "delivery_metadata"):
                with self.subTest(table=table, action="update"):
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute(f"UPDATE {table} SET payload='{{}}'")
                with self.subTest(table=table, action="delete"):
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute(f"DELETE FROM {table}")
        self.assertEqual(self.queue.intent_history("immutable")[-1]["status"], "sent")
        self.assertEqual(self.queue.delivery_metadata(delivery.key)["intent_id"], "immutable")

    def test_intent_identity_is_exact_not_substring_or_sql_wildcard(self) -> None:
        self.queue.enqueue_intent(intent("prefix-key-suffix"), now=at(10))
        self.queue.dispatch_intents(at(10), render=render)
        self.queue.enqueue_intent(intent("key"), now=at(12))
        self.queue.enqueue_intent(intent("%"), now=at(12))
        delivered = self.queue.dispatch_intents(at(12), render=render)
        self.assertEqual({row.message_ids for row in delivered}, {("key",), ("%",)})
        self.assertEqual(len(self.queue.web_events()), 3)

    def test_malformed_intent_rejected_before_persistence(self) -> None:
        invalid: list[dict[str, Any]] = [
            {**intent("empty"), "evidence": []},
            {**intent("bad-urgency"), "urgency": "urgent"},
            {**intent("bad-flag"), "needs_response": "false"},
            {**intent("multiline"), "reason": "fact\nheader"},
        ]
        for value in invalid:
            with self.subTest(id=value["id"]), self.assertRaises(ValueError):
                self.queue.enqueue_intent(value, now=at(10))
        self.assertEqual(self.queue.dispatch_intents(at(10), render=render), [])

    def test_structured_intent_is_durable_and_commits_metadata_with_both_delivery(self) -> None:
        self.assertTrue(hasattr(self.queue, "enqueue_intent"), "structured intent API missing")
        source = intent("stable-one")
        key = self.queue.enqueue_intent(source, now=at(10))
        self.assertEqual(key, source["id"])
        self.assertEqual(self.queue.web_events(), [])
        self.assertFalse((self.root / "outbox").exists())
        source["evidence"].append("must-not-mutate-persisted-input")
        seen: list[dict[str, Any]] = []

        def author(item: dict[str, Any]) -> dict[str, Any]:
            seen.append(item)
            return render(item)

        queue = Outreach(self.root)
        deliveries = queue.dispatch_intents(at(10), render=author)
        self.assertEqual(len(deliveries), 1)
        delivery = deliveries[0]
        self.assertEqual(delivery.channel, "both")
        self.assertNotEqual(delivery.text, delivery.reason)
        self.assertEqual(seen[0]["evidence"], ["sensor:stable-one"])
        self.assertEqual(queue.web_events(), deliveries)
        self.assertEqual(len(list((self.root / "outbox").glob("*.eml"))), 1)
        self.assertEqual(
            queue.delivery_metadata(delivery.key),
            {
                "intent_id": key,
                "reason": seen[0]["reason"],
                "evidence": seen[0]["evidence"],
                "urgency": "normal",
                "needs_response": True,
                "generation_degraded": False,
                "source_channel": "jarvis_proactive",
            },
        )
        self.assertEqual(queue.delivery_metadata("absent"), {})
        self.assertEqual(queue.dispatch_intents(at(10), render=author), [])
        self.assertEqual(len(seen), 1)

    def test_quiet_defers_every_nonemergency_until_cst_boundary_without_render(self) -> None:
        from datetime import UTC

        for urgency in ("low", "normal", "high"):
            with self.subTest(urgency=urgency):
                queue = Outreach(self.root / urgency)
                queue.enqueue_intent(intent(urgency, urgency=urgency), now=at(0))
                seen: list[str] = []

                def author(item: dict[str, Any], seen: list[str] = seen) -> dict[str, Any]:
                    seen.append(item["id"])
                    return render(item)

                self.assertEqual(
                    queue.dispatch_intents(at(7, 29).astimezone(UTC), render=author), []
                )
                self.assertEqual(seen, [])
                queue = Outreach(self.root / urgency)
                self.assertEqual(len(queue.dispatch_intents(at(7, 30), render=author)), 1)
                self.assertEqual(seen, [urgency])

    def test_reason_dedupe_includes_old_queued_items_and_six_hour_sent_window(self) -> None:
        queue = self.queue
        original = intent("original", reason="same-source-change")
        queue.enqueue_intent(original, now=at(0))
        queue.enqueue_intent(intent("queued-duplicate", reason=original["reason"]), now=at(8))
        self.assertEqual(len(queue.dispatch_intents(at(8), render=render)), 1)
        queue.enqueue_intent(intent("window-duplicate", reason=original["reason"]), now=at(13, 59))
        self.assertEqual(queue.dispatch_intents(at(13, 59), render=render), [])
        queue = Outreach(self.root)
        queue.enqueue_intent(intent("boundary", reason=original["reason"]), now=at(14))
        self.assertEqual(len(queue.dispatch_intents(at(14), render=render)), 1)
        self.assertTrue(hasattr(queue, "intent_history"), "reason suppression must be auditable")
        self.assertEqual(queue.intent_history("queued-duplicate")[-1]["status"], "suppressed")
        self.assertEqual(queue.intent_history("window-duplicate")[-1]["status"], "suppressed")
        self.assertEqual(queue.intent_history("missing"), [])

    def test_stable_id_reenqueue_after_restart_never_renders_or_delivers_twice(self) -> None:
        item = intent("stable-id", urgency="emergency")
        self.queue.enqueue_intent(item, now=at(10))
        try:
            self.assertEqual(self.queue.enqueue_intent(item, now=at(10)), item["id"])
        except Exception as error:
            self.fail(f"same upstream id must be idempotent, got {type(error).__name__}")
        first = self.queue.dispatch_intents(at(10), render=render)
        queue = Outreach(self.root)
        self.assertEqual(queue.enqueue_intent(item, now=at(17)), item["id"])
        self.assertEqual(queue.dispatch_intents(at(17), render=render), [])
        self.assertEqual(queue.web_events(), first)
        self.assertEqual(len(list((self.root / "outbox").glob("*.eml"))), 1)

    def test_rolling_hour_cap_two_includes_high_and_does_not_render_deferred(self) -> None:
        seen: list[str] = []
        for number, urgency in enumerate(("high", "normal", "low")):
            self.queue.enqueue_intent(intent(str(number), urgency=urgency), now=at(10))

        def author(item: dict[str, Any]) -> dict[str, Any]:
            seen.append(item["id"])
            return render(item)

        self.assertEqual(len(self.queue.dispatch_intents(at(10), render=author)), 2)
        self.assertEqual(len(seen), 2)
        self.assertEqual(Outreach(self.root).dispatch_intents(at(10, 59), render=author), [])
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(self.queue.dispatch_intents(at(11), render=author)), 1)

    def fill_day(self) -> None:
        for index in range(9):
            self.queue.enqueue_intent(intent(f"daily-{index}", urgency="high"), now=at(8))
        for hour in (8, 10, 12, 14):
            self.queue.dispatch_intents(at(hour), render=render)

    def test_cst_calendar_day_cap_eight_and_next_day_release(self) -> None:
        self.fill_day()
        self.assertEqual(len(self.queue.web_events()), 8)
        self.assertEqual(Outreach(self.root).dispatch_intents(at(23), render=render), [])
        self.assertEqual(self.queue.dispatch_intents(at(7, 29, day=17), render=render), [])
        self.assertEqual(len(self.queue.dispatch_intents(at(7, 30, day=17), render=render)), 1)

    def test_offline_one_nonemergency_persists_and_online_resets(self) -> None:
        for number in range(3):
            self.queue.enqueue_intent(intent(str(number)), now=at(10))
        self.assertEqual(len(self.queue.dispatch_intents(at(10), render=render, online=False)), 1)
        queue = Outreach(self.root)
        self.assertEqual(queue.dispatch_intents(at(12), render=render, online=False), [])
        self.assertEqual(len(queue.dispatch_intents(at(12), render=render, online=True)), 2)
        queue.enqueue_intent(intent("new-offline"), now=at(14))
        self.assertEqual(len(queue.dispatch_intents(at(14), render=render, online=False)), 1)

    def test_emergency_bypasses_with_structured_audit_but_same_id_is_once(self) -> None:
        self.fill_day()
        urgent = intent("urgent", urgency="emergency")
        self.queue.enqueue_intent(urgent, now=at(14))
        result = self.queue.dispatch_intents(at(14), render=render, online=False)
        self.assertEqual(len(result), 1)
        audit = self.queue.intent_history("urgent")
        bypasses = [event for event in audit if event.get("bypass")]
        self.assertTrue(bypasses, "emergency bypass needs structured audit")
        self.assertTrue({"hourly_limit", "daily_limit", "offline"}.issubset(bypasses[0]["bypass"]))
        quiet = intent("quiet-emergency", urgency="emergency")
        self.queue.enqueue_intent(quiet, now=at(1, day=17))
        self.assertEqual(
            len(self.queue.dispatch_intents(at(1, day=17), render=render, online=False)), 1
        )
        self.assertTrue(
            any(
                "quiet_hours" in event.get("bypass", [])
                for event in self.queue.intent_history(quiet["id"])
            )
        )
        restarted = Outreach(self.root)
        restarted.enqueue_intent(quiet, now=at(2, day=17))
        self.assertEqual(restarted.dispatch_intents(at(2, day=17), render=render, online=False), [])

    def test_renderer_failure_or_invalid_output_degrades_to_minimal_source_fact(self) -> None:
        def failed(_item: dict[str, Any]) -> dict[str, Any]:
            raise TimeoutError("must not leak model diagnostics to Sir")

        authors: tuple[Callable[[dict[str, Any]], dict[str, Any]], ...] = (
            failed,
            lambda _i: {"text": "", "generation_degraded": False},
            lambda _i: {"text": "words", "generation_degraded": "false"},
        )
        for index, author in enumerate(authors):
            with self.subTest(index=index):
                queue = Outreach(self.root / str(index))
                source = intent(str(index))
                queue.enqueue_intent(source, now=at(10))
                try:
                    result = queue.dispatch_intents(at(10), render=author)
                except Exception as error:
                    self.fail(f"renderer failure must degrade, got {type(error).__name__}")
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0].text, source["reason"])
                metadata = queue.delivery_metadata(result[0].key)
                self.assertIs(metadata["generation_degraded"], True)
                self.assertTrue(metadata.get("error_type"))
                self.assertNotIn("diagnostics", result[0].text)

    def test_render_unknown_after_process_exit_degrades_without_second_generation(self) -> None:
        import os
        import subprocess
        import sys

        source = intent("crashed-author")
        self.queue.enqueue_intent(source, now=at(10))
        script = """import os, sys
from datetime import datetime
from outreach import Outreach
q = Outreach(sys.argv[1])
def author(source):
    os._exit(23)
q.dispatch_intents(datetime.fromisoformat(sys.argv[2]), render=author)
"""
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.root), at(10).isoformat()],
            cwd=Path(__file__).parent,
            env=os.environ.copy(),
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 23, completed.stderr)
        seen: list[str] = []

        def should_not_render(item: dict[str, Any]) -> dict[str, Any]:
            seen.append(item["id"])
            return render(item)

        queue = Outreach(self.root)
        result = queue.dispatch_intents(at(10, 1), render=should_not_render)
        self.assertEqual(seen, [], "unknown generation must never call the author twice")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].text, source["reason"])
        self.assertIs(queue.delivery_metadata(result[0].key)["generation_degraded"], True)
        self.assertEqual(queue.dispatch_intents(at(12), render=should_not_render), [])

    def test_generated_journal_survives_crash_before_delivery_commit(self) -> None:
        from unittest.mock import patch

        self.queue.enqueue_intent(intent("render-done"), now=at(10))
        seen: list[str] = []

        def author(item: dict[str, Any]) -> dict[str, Any]:
            seen.append(item["id"])
            return render(item)

        with patch.object(self.queue, "_commit_intent", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.queue.dispatch_intents(at(10), render=author)
        result = Outreach(self.root).dispatch_intents(at(10, 1), render=author)
        self.assertEqual(seen, ["render-done"], "durable generated words must not be regenerated")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].text, render(intent("render-done"))["text"])

    def test_committed_delivery_and_metadata_survive_local_outbox_failure_and_retry(self) -> None:
        from unittest.mock import patch

        self.queue.enqueue_intent(intent("outbox-failed"), now=at(10))
        with patch.object(self.queue, "_write_email", side_effect=OSError("disk unavailable")):
            try:
                first = self.queue.dispatch_intents(at(10), render=render, online=False)
            except Exception as error:
                self.fail(f"local failure must leave retryable journal, got {type(error).__name__}")
        self.assertEqual(first, [])
        delivery = self.queue.web_events()[0]
        self.assertEqual(self.queue.delivery_metadata(delivery.key)["intent_id"], "outbox-failed")
        seen: list[str] = []

        def should_not_render(item: dict[str, Any]) -> dict[str, Any]:
            seen.append(item["id"])
            return render(item)

        queue = Outreach(self.root)
        result = queue.dispatch_intents(at(1, day=17), render=should_not_render, online=False)
        self.assertEqual(
            result, [delivery], "finish already committed local handoff even in quiet hours"
        )
        self.assertEqual(seen, [])
        self.assertEqual(queue.web_events(), [delivery])
        self.assertEqual(len(list((self.root / "outbox").glob("*.eml"))), 1)
        self.assertEqual(
            queue.dispatch_intents(at(10, day=17), render=should_not_render, online=False), []
        )

    def test_crash_after_eml_creation_reuses_exact_file_and_db_delivery(self) -> None:
        from unittest.mock import patch

        self.queue.enqueue_intent(intent("eml-crash"), now=at(10))
        method_name = "_write_email"
        original = getattr(self.queue, method_name)

        def crash_after_write(delivery: Any) -> None:
            original(delivery)
            raise KeyboardInterrupt

        with patch.object(self.queue, "_write_email", side_effect=crash_after_write):
            with self.assertRaises(KeyboardInterrupt):
                self.queue.dispatch_intents(at(10), render=render)
        files = list((self.root / "outbox").glob("*.eml"))
        self.assertEqual(len(files), 1)
        payload, inode = files[0].read_bytes(), files[0].stat().st_ino
        result = Outreach(self.root).dispatch_intents(at(12), render=render)
        self.assertEqual(len(result), 1, "committed delivery must finish journal on restart")
        self.assertEqual(files[0].read_bytes(), payload)
        self.assertEqual(files[0].stat().st_ino, inode)
        self.assertEqual(len(self.queue.web_events()), 1)

    def test_parallel_dispatch_claims_once_and_author_never_holds_sqlite_write_lock(self) -> None:
        import concurrent.futures
        import sqlite3
        import threading
        import time
        from contextlib import closing

        self.queue.enqueue_intent(intent("concurrent"), now=at(10))
        seen: list[str] = []
        guard = threading.Lock()

        def author(item: dict[str, Any]) -> dict[str, Any]:
            with closing(sqlite3.connect(self.queue.db_path, timeout=0.2)) as db:
                db.execute("BEGIN IMMEDIATE")
                db.rollback()
            with guard:
                seen.append(item["id"])
            time.sleep(0.05)
            return render(item)

        def dispatch(_index: int) -> list[Any]:
            return Outreach(self.root).dispatch_intents(at(10), render=author)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            batches = list(pool.map(dispatch, range(4)))
        self.assertEqual(sum(map(len, batches)), 1)
        self.assertEqual(seen, ["concurrent"])
        self.assertFalse(
            self.queue.delivery_metadata(self.queue.web_events()[0].key)["generation_degraded"]
        )


if __name__ == "__main__":
    unittest.main()
