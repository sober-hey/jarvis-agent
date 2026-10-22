"""Bounded resident-history queries with real, disposable SQLite stores."""

from __future__ import annotations

import inspect
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from chat_launcher import prepare_root
from daemon_wiring import FixtureModel, Wiring
from jarvis_daemon import CST


class BoundedWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="jarvis-acceptance-bounded-wiring-", dir="/tmp"
        )
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "data"
        self.root.mkdir(mode=0o700)
        self.now = datetime(2026, 10, 16, 12, tzinfo=CST).timestamp()
        # Unit-level history checks inject the fake directly. Wiring.create's
        # fixture gate remains acceptance-only; these roots obey caller TMPDIR.
        self.wiring = self.restart()

    def restart(self) -> Wiring:
        return Wiring(prepare_root(self.root), FixtureModel(), fixture=True, clock=lambda: self.now)

    def test_connection_context_closes_after_commit_and_rollback(self) -> None:
        with self.wiring.connect() as db:
            db.execute("INSERT INTO once_keys VALUES('committed')")
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")
        failed: sqlite3.Connection = db
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.wiring.connect() as failed:
                failed.execute("INSERT INTO once_keys VALUES('rolled-back')")
                raise RuntimeError("rollback")
        assert failed is not None
        with self.assertRaises(sqlite3.ProgrammingError):
            failed.execute("SELECT 1")
        with self.wiring.connect() as check:
            self.assertEqual(
                check.execute("SELECT key FROM once_keys").fetchall(), [("committed",)]
            )

    def test_resident_paths_do_not_materialize_history(self) -> None:
        store = self.wiring.mind.store
        original = store.entries

        def guarded(
            category: str | None = None, limit: int | None = None
        ) -> list[dict[str, object]]:
            caller = inspect.currentframe()
            assert caller is not None and caller.f_back is not None
            self.assertFalse(
                Path(caller.f_back.f_code.co_filename).name == "daemon_wiring.py" and limit is None,
                "daemon path requested unbounded history",
            )
            return original(category, limit)

        with patch.object(store, "entries", new=guarded):
            self.assertEqual(self.wiring.poll(), 1)
            self.wiring.mind.context()
            self.wiring.compact_pending()
            self.assertTrue(self.wiring.think())
            self.assertEqual(self.wiring.queue_accounting()["pending"], 0)
            self.assertTrue(self.wiring.completed_thought_calls())

    def test_steady_think_does_not_run_full_completion_audit(self) -> None:
        self.wiring.poll()
        with patch.object(
            self.wiring, "completed_thought_calls", side_effect=AssertionError("full audit in tick")
        ):
            self.assertTrue(self.wiring.think())

    def test_late_partial_commit_is_repaired_after_checkpoint_and_restart(self) -> None:
        store = self.wiring.mind.store
        event = store.ingest("tool_result", {"daemon_event_id": "old"}, source="test")
        call = store.append(
            "model_call", {"context": {"daemon_pending_senses": [event]}}, source="test"
        )
        store.append(
            "model_response",
            {"call_id": call["id"], "text": json.dumps({"thoughts": [{}, {}]})},
            source="test",
        )
        store.append("thought", {"call_id": call["id"]}, source="test")
        self.wiring.reconcile_consumption()
        self.assertEqual(store.entries("daemon_sense_consumed"), [])
        store.append("thought", {"call_id": call["id"]}, source="test")
        restarted = self.restart()
        restarted.reconcile_consumption()
        receipts = store.entries("daemon_sense_consumed")
        self.assertEqual([r["payload"]["event_id"] for r in receipts], [event["id"]])

    def test_degraded_round_is_complete_when_all_retained_thoughts_are_stored(self) -> None:
        store = self.wiring.mind.store
        event = store.ingest("tool_result", {"daemon_event_id": "deg"}, source="test")
        call = store.append(
            "model_call", {"context": {"daemon_pending_senses": [event]}}, source="test"
        )
        store.append(
            "model_response",
            {"call_id": call["id"], "text": json.dumps({"thoughts": [{}, {}, {}]})},
            source="test",
        )
        store.append(
            "validation_degraded", {"call_id": call["id"], "dropped_count": 2}, source="test"
        )
        self.assertFalse(self.wiring.thought_call_complete(call["id"]))
        store.append("thought", {"call_id": call["id"]}, source="test")
        self.assertTrue(self.wiring.thought_call_complete(call["id"]))
        self.wiring.reconcile_consumption()
        receipts = store.entries("daemon_sense_consumed")
        self.assertEqual([r["payload"]["event_id"] for r in receipts], [event["id"]])

    def test_fully_dropped_round_is_not_complete(self) -> None:
        store = self.wiring.mind.store
        call = store.append("model_call", {"context": {}}, source="test")
        store.append(
            "model_response",
            {"call_id": call["id"], "text": json.dumps({"thoughts": [{}, {}]})},
            source="test",
        )
        store.append(
            "validation_degraded", {"call_id": call["id"], "dropped_count": 2}, source="test"
        )
        self.assertFalse(self.wiring.thought_call_complete(call["id"]))

    def test_accounting_preserves_orphan_duplicate_and_overlap_semantics(self) -> None:
        store = self.wiring.mind.store
        store.append("daemon_sense_consumed", {"event_id": "orphan"}, source="test")
        store.append("daemon_sense_consumed", {"event_id": "orphan"}, source="test")
        self.assertEqual(
            self.wiring.queue_accounting(),
            {
                "enqueued": 0,
                "consumed": 1,
                "merged": 0,
                "suspended": 0,
                "pending": 0,
                "residual": -1,
            },
        )
        store.append("daemon_sense_suspended", {"event_id": "orphan"}, source="test")
        with self.assertRaisesRegex(RuntimeError, "OverlappingSenseReceipts"):
            self.wiring.queue_accounting()

    def test_partial_merge_recovery_retries_before_advancing_checkpoint(self) -> None:
        store = self.wiring.mind.store
        events = [
            store.ingest("tool_result", {"daemon_event_id": str(n)}, source="test")
            for n in range(3)
        ]
        store.ingest(
            "tool_result",
            {"daemon_event_id": "summary:old", "merged_event_ids": [e["id"] for e in events]},
            source="test",
        )
        original = store.append
        count = 0

        def interrupted(
            category: str,
            payload: dict[str, object],
            *,
            source: str = "jarvis",
            refs: list[str] | None = None,
        ) -> dict[str, object]:
            nonlocal count
            if category == "daemon_sense_merged":
                count += 1
                if count == 2:
                    raise OSError("receipt crash")
            return original(category, payload, source=source, refs=refs)

        with patch.object(store, "append", new=interrupted):
            with self.assertRaisesRegex(OSError, "receipt crash"):
                self.wiring.reconcile_merges()
        restarted = self.restart()
        self.assertEqual(restarted.queue_accounting()["merged"], 3)
        restarted.reconcile_merges()
        self.assertEqual(len(store.entries("daemon_sense_merged")), 3)

    def test_checkpoint_excludes_older_completed_work_from_later_ticks(self) -> None:
        self.wiring.poll()
        self.assertTrue(self.wiring.think())
        with patch.object(
            self.wiring, "thought_call_complete", side_effect=AssertionError("old call revisited")
        ):
            self.wiring.reconcile_consumption()
            self.wiring.reconcile_merges()


if __name__ == "__main__":
    unittest.main()


class RawContextCaptureTests(unittest.TestCase):
    """Untrimmed-context capture is observation only and never fatal."""

    def test_capture_writes_private_copy_without_mutating_context(self):
        import stat
        from daemon_wiring import capture_raw_context

        with tempfile.TemporaryDirectory(prefix="jarvis-rawcap-", dir=Path.home()) as folder:
            root = Path(folder)
            root.chmod(0o700)
            context = {"as_of": "2026-10-20T00:00:00+00:00", "recalled": [{"id": "r1"}]}
            snapshot = json.dumps(context, sort_keys=True)
            capture_raw_context(root, context)
            self.assertEqual(json.dumps(context, sort_keys=True), snapshot)
            path = root / "raw-context-capture.jsonl"
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            record = json.loads(path.read_text().strip())
            self.assertEqual(record["context"], context)
            self.assertEqual(record["as_of"], context["as_of"])

    def test_capture_never_raises(self):
        from daemon_wiring import capture_raw_context

        capture_raw_context(Path("/nonexistent-root-for-rawcap"), {"as_of": None})
