"""Voice observations share the real append-only MindStore, never a second DB.

Only the host-directory exclusion is patched for disposable fixtures beneath
TMPDIR; SQLite, append triggers and concurrent writes are not mocked.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

from inner_loop import MindStore

NOW = "2026-10-20T10:00:00+00:00"
LATER = "2026-10-20T10:00:02+00:00"


class VoiceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("voice_ledger"), "voice_ledger is required")
        self.module = importlib.import_module("voice_ledger")
        self.temp = tempfile.TemporaryDirectory(prefix="jarvis-voice-ledger-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        with patch("product_home.protected_paths", return_value=()):
            self.store = MindStore(self.root)
        self.ledger = self.module.VoiceLedger(self.store)

    def user(self, text: str = "用户语音", **kwargs: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "device_id": "browser-mic",
            "asr_confidence": 0.91,
            "audio_duration_ms": 1300,
            "timestamp": NOW,
        }
        fields.update(kwargs)
        return self.ledger.record_user(text, **fields)

    def reply(
        self, user: dict[str, Any] | None = None, **kwargs: Any
    ) -> tuple[str, dict[str, Any]]:
        user = user or self.user()
        turn_id = user["payload"]["turn_id"]
        row = self.ledger.record_assistant(turn_id, "回答文本", generation_ms=450, **kwargs)
        return turn_id, row

    def rows(self):
        with self.store.connection() as db:
            return [tuple(row) for row in db.execute("SELECT * FROM entries ORDER BY seq")]

    def test_metadata_and_existing_mindstore_append_are_used(self):
        with patch.object(self.store, "append", wraps=self.store.append) as append:
            user = self.user()
            turn_id, reply = self.reply(user)
        self.assertEqual(append.call_count, 2)
        self.assertEqual(user["category"], "voice_user")
        self.assertEqual(user["source"], "browser-mic")
        self.assertEqual(user["payload"]["timestamp"], NOW)
        self.assertEqual(user["payload"]["text"], "用户语音")
        self.assertEqual(user["payload"]["speaker"], "user")
        self.assertEqual(user["payload"]["audio_duration_ms"], 1300)
        self.assertEqual(user["payload"]["asr_confidence"], 0.91)
        self.assertEqual(reply["category"], "voice_assistant")
        self.assertEqual(reply["payload"]["speaker"], "jarvis")
        self.assertEqual(reply["payload"]["generation_ms"], 450)
        self.assertEqual(reply["payload"]["turn_id"], turn_id)
        self.assertIn(user["id"], reply["refs"])
        self.assertNotEqual(user["created_at"], NOW)

    def test_constructor_adds_no_tables_or_columns(self):
        with self.store.connection() as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            columns = [r[1] for r in db.execute("PRAGMA table_info(entries)")]
        self.assertEqual(set(tables), {"entries", "belief_versions", "sqlite_sequence"})
        self.assertEqual(
            columns, ["seq", "id", "category", "created_at", "source", "payload", "refs"]
        )
        self.assertEqual({p.name for p in self.root.glob("*.db")}, {"mind.db"})

    def test_tts_success_and_failure_are_independent_append_events(self):
        turn_id, reply = self.reply()
        original = self.rows()
        success = self.ledger.record_tts(
            turn_id, reply["id"], status="success", voice_id="voice-17", audio_duration_ms=2500
        )
        failure = self.ledger.record_tts(
            turn_id, reply["id"], status="failed", voice_id="voice-17", error="synthesis_timeout"
        )
        self.assertEqual(self.rows()[: len(original)], original)
        self.assertEqual(success["category"], "voice_assistant")
        self.assertEqual(success["payload"]["event"], "tts")
        self.assertEqual(success["payload"]["status"], "success")
        self.assertEqual(success["payload"]["audio_duration_ms"], 2500)
        self.assertEqual(failure["payload"]["status"], "failed")
        self.assertIsNone(failure["payload"]["audio_duration_ms"])
        self.assertEqual(failure["payload"]["error"], "synthesis_timeout")
        self.assertIn(reply["id"], failure["refs"])
        turn = self.ledger.get_turn(turn_id)
        self.assertEqual(turn["assistant"][0]["id"], reply["id"])
        self.assertEqual(len(turn["assistant"]), 1)
        self.assertEqual([row["id"] for row in turn["tts"]], [success["id"], failure["id"]])

    def test_playback_start_end_and_device_are_reconstructed_without_rewriting(self):
        turn_id, reply = self.reply()
        start = self.ledger.record_playback_start(
            turn_id, reply["id"], device_id="kitchen-speaker", timestamp=NOW
        )
        original = self.rows()
        playing = self.ledger.get_turn(turn_id)["playback"][0]
        self.assertEqual(playing["status"], "playing")
        self.assertIsNone(playing["ended_at"])
        end = self.ledger.record_playback_end(turn_id, start["id"], timestamp=LATER)
        self.assertEqual(self.rows()[: len(original)], original)
        playback = self.ledger.get_turn(turn_id)["playback"][0]
        self.assertEqual(playback["playback_id"], start["id"])
        self.assertEqual(playback["device_id"], "kitchen-speaker")
        self.assertEqual(playback["started_at"], NOW)
        self.assertEqual(playback["ended_at"], LATER)
        self.assertEqual(playback["status"], "ended")
        self.assertIs(playback["interrupted"], False)
        self.assertEqual(end["category"], "voice_playback")
        self.assertIn(start["id"], end["refs"])

    def test_interrupt_links_actor_target_and_interrupting_utterance(self):
        user = self.user()
        turn_id, reply = self.reply(user)
        start = self.ledger.record_playback_start(
            turn_id, reply["id"], device_id="mac", timestamp=NOW
        )
        incoming = self.user("新指令")
        original = self.rows()
        interrupt = self.ledger.record_interrupt(
            turn_id,
            interrupted_by="user",
            target_id=start["id"],
            device_id="phone",
            interrupting_turn_id=incoming["payload"]["turn_id"],
            timestamp=LATER,
        )
        self.assertEqual(self.rows()[: len(original)], original)
        self.assertEqual(interrupt["category"], "voice_interrupt")
        self.assertEqual(interrupt["payload"]["target_id"], start["id"])
        self.assertEqual(interrupt["payload"]["interrupted_by"], "user")
        self.assertEqual(interrupt["payload"]["device_id"], "phone")
        self.assertEqual(set(interrupt["refs"]), {user["id"], start["id"], incoming["id"]})
        turn = self.ledger.get_turn(turn_id)
        playback = turn["playback"][0]
        self.assertIs(playback["interrupted"], True)
        self.assertIsNone(playback["ended_at"], "interrupt observation is not an end callback")
        self.assertEqual(playback["interrupt_ids"], [interrupt["id"]])
        self.ledger.record_playback_end(turn_id, start["id"], timestamp=LATER, interrupted=True)
        self.assertEqual(len(self.ledger.get_turn(turn_id)["interrupts"]), 1)
        self.assertEqual(self.ledger.get_turn(incoming["payload"]["turn_id"])["interrupts"], [])

    def test_interruption_of_reply_marks_only_its_devices(self):
        turn_id, reply = self.reply()
        other = self.ledger.record_assistant(turn_id, "其他回答", generation_ms=20)
        first = self.ledger.record_playback_start(turn_id, reply["id"], device_id="phone")
        self.ledger.record_playback_start(turn_id, other["id"], device_id="mac")
        interrupt = self.ledger.record_interrupt(
            turn_id, interrupted_by="system", target_id=reply["id"]
        )
        playback = self.ledger.get_turn(turn_id)["playback"]
        self.assertEqual(playback[0]["playback_id"], first["id"])
        self.assertEqual(playback[0]["interrupt_ids"], [interrupt["id"]])
        self.assertTrue(playback[0]["interrupted"])
        self.assertFalse(playback[1]["interrupted"])

    def test_recent_turns_counts_turns_not_tts_or_playback_events(self):
        users = [self.user(str(i)) for i in range(4)]
        self.store.append("thought", {"turn_id": users[0]["payload"]["turn_id"]}, source="test")
        self.reply(users[0])  # old turn activity must not become a new turn
        turns = self.ledger.recent_voice_turns(2)
        self.assertEqual(
            [turn["turn_id"] for turn in turns],
            [user["payload"]["turn_id"] for user in reversed(users[2:])],
        )
        self.assertEqual(self.ledger.recent_voice_turns(0), [])
        self.assertEqual(len(self.ledger.recent_voice_turns(20)), 4)
        self.assertEqual(len(self.ledger.get_turn(users[0]["payload"]["turn_id"])["events"]), 2)
        self.assertIsNone(self.ledger.get_turn("unknown"))
        self.assertIsNone(self.ledger.get_turn("' OR 1=1 --"))
        for limit in (-1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises((TypeError, ValueError)):
                self.ledger.recent_voice_turns(limit)

    def test_db_append_only_triggers_still_protect_all_voice_rows(self):
        self.reply()
        for statement in ("UPDATE entries SET source='changed'", "DELETE FROM entries"):
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append only"):
                    with self.store.connection() as db:
                        db.execute(statement)
        self.assertEqual(len(self.rows()), 2)

    def test_queries_are_read_only_and_return_detached_values(self):
        user = self.user()
        turn_id, reply = self.reply(user)
        original = self.rows()
        with patch.object(self.store, "append", side_effect=AssertionError("query appended")):
            turn = self.ledger.get_turn(turn_id)
            turn["user"]["payload"]["text"] = "not persisted"
            turn["assistant"][0]["payload"]["text"] = "not persisted"
            self.ledger.recent_voice_turns(10)
        self.assertEqual(self.rows(), original)
        self.assertEqual(self.ledger.get_turn(turn_id)["user"]["payload"]["text"], "用户语音")
        self.assertEqual(self.ledger.get_turn(turn_id)["assistant"][0]["id"], reply["id"])

    def test_restart_recovers_full_turn(self):
        turn_id, reply = self.reply()
        self.ledger.record_tts(
            turn_id, reply["id"], status="success", voice_id="v", audio_duration_ms=0
        )
        before = self.ledger.get_turn(turn_id)
        with patch("product_home.protected_paths", return_value=()):
            reopened = self.module.VoiceLedger(MindStore(self.root))
        self.assertEqual(reopened.get_turn(turn_id), before)

    def test_unknown_asr_confidence_is_not_fabricated(self):
        user = self.user(asr_confidence=None)
        self.assertIsNone(user["payload"]["asr_confidence"])
        self.assertIsNotNone(user["payload"]["timestamp"])
        self.assertTrue(user["payload"]["turn_id"])

    def test_invalid_input_does_not_append(self):
        cases: tuple[dict[str, Any], ...] = (
            {"asr_confidence": -0.1},
            {"asr_confidence": 1.1},
            {"asr_confidence": float("nan")},
            {"asr_confidence": True},
            {"audio_duration_ms": -1},
            {"audio_duration_ms": float("inf")},
            {"device_id": ""},
            {"timestamp": "2026-10-20T10:00:00"},
            {"timestamp": "not-a-date"},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises((TypeError, ValueError)):
                self.user(**changes)
        self.assertEqual(self.rows(), [])
        turn_id, reply = self.reply()
        before = self.rows()
        for fields in (
            {"status": "maybe", "voice_id": "v"},
            {"status": "success", "voice_id": "v"},
            {"status": "failed", "voice_id": ""},
        ):
            with self.subTest(fields=fields), self.assertRaises((TypeError, ValueError)):
                self.ledger.record_tts(turn_id, reply["id"], **fields)
        with self.assertRaises(ValueError):
            self.ledger.record_assistant(turn_id, "", generation_ms=1)
        with self.assertRaises(ValueError):
            self.ledger.record_assistant(turn_id, "reply", generation_ms=-1)
        self.assertEqual(self.rows(), before)

    def test_unknown_and_cross_turn_references_are_rejected_without_writes(self):
        one, reply = self.reply()
        two, _ = self.reply()
        start = self.ledger.record_playback_start(
            one, reply["id"], device_id="phone", timestamp=NOW
        )
        original = self.rows()
        callbacks = [
            lambda: self.ledger.record_assistant("missing", "text", generation_ms=1),
            lambda: self.ledger.record_playback_start(two, reply["id"], device_id="phone"),
            lambda: self.ledger.record_playback_end(two, start["id"]),
            lambda: self.ledger.record_playback_end(
                one, start["id"], timestamp="2026-10-19T00:00:00Z"
            ),
            lambda: self.ledger.record_interrupt(two, interrupted_by="user", target_id=start["id"]),
            lambda: self.ledger.record_interrupt(one, interrupted_by="user", target_id="missing"),
            lambda: self.ledger.record_interrupt(
                one, interrupted_by="user", target_id=reply["id"], interrupting_turn_id="missing"
            ),
        ]
        for callback in callbacks:
            with self.subTest(callback=callback), self.assertRaises(ValueError):
                callback()
        self.assertEqual(self.rows(), original)

    def test_empty_queries_and_self_evaluation_do_not_write_events(self):
        self.assertIsNone(self.ledger.get_turn("missing"))
        self.assertEqual(self.ledger.recent_voice_turns(), [])
        report = self.module.self_evaluate()
        self.assertEqual(report["module"], "voice_ledger")
        for field in (
            "domain",
            "current_version",
            "known_limitations",
            "improvement_directions",
            "last_evaluated_at",
        ):
            self.assertTrue(report[field])
        self.assertEqual(self.rows(), [])

    def test_timestamp_offsets_are_normalized_but_ingress_time_is_separate(self):
        user = self.user(timestamp="2026-10-20T18:00:00+08:00")
        self.assertEqual(user["payload"]["timestamp"], NOW)
        self.assertNotEqual(user["created_at"], NOW)
        self.assertEqual(user["payload"]["text"], "用户语音")
        self.assertEqual(self.user(timestamp=None)["payload"]["timestamp"][-6:], "+00:00")

    def test_repeated_callbacks_are_audited_and_cannot_clear_an_interruption(self):
        turn_id, reply = self.reply()
        start = self.ledger.record_playback_start(
            turn_id, reply["id"], device_id="mac", timestamp=NOW
        )
        self.ledger.record_playback_end(turn_id, start["id"], timestamp=LATER, interrupted=True)
        original = self.rows()
        self.ledger.record_playback_end(turn_id, start["id"], timestamp=LATER, interrupted=False)
        self.assertEqual(self.rows()[: len(original)], original)
        turn = self.ledger.get_turn(turn_id)
        self.assertTrue(turn["playback"][0]["interrupted"])
        self.assertEqual(turn["playback"][0]["ended_at"], LATER)
        self.assertEqual(len(turn["events"]), 5)

    def test_exact_turn_lookup_uses_index_without_loading_whole_timeline(self):
        user = self.user()
        turn_id = user["payload"]["turn_id"]
        with self.store.connection() as db:
            details = " ".join(
                row[3]
                for row in db.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM entries WHERE "
                    "category IN ('voice_user','voice_assistant',"
                    "'voice_playback','voice_interrupt') "
                    "AND json_extract(payload,'$.turn_id')=? ORDER BY seq",
                    (turn_id,),
                )
            )
        self.assertIn("entries_voice_turn_seq", details)
        with patch.object(self.store, "entries", side_effect=AssertionError("full scan")):
            self.assertEqual(self.ledger.get_turn(turn_id)["user"]["id"], user["id"])
            self.assertEqual(len(self.ledger.recent_voice_turns(1)), 1)

    def test_concurrent_threads_share_turn_without_losing_events(self):
        turn_id, reply = self.reply()
        original = self.rows()

        def write(i: int) -> dict[str, Any]:
            return self.ledger.record_tts(
                turn_id, reply["id"], status="success", voice_id=f"v-{i}", audio_duration_ms=i
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(write, range(40)))
        self.assertEqual(len({row["id"] for row in rows}), 40)
        self.assertEqual(self.rows()[: len(original)], original)
        self.assertEqual(len(self.ledger.get_turn(turn_id)["tts"]), 40)

    def test_concurrent_processes_append_to_same_entries_table(self):
        program = """
import json, sys
from pathlib import Path
from unittest.mock import patch
from inner_loop import MindStore
from voice_ledger import VoiceLedger
with patch("product_home.protected_paths", return_value=()):
    ledger = VoiceLedger(MindStore(Path(sys.argv[1])))
for i in range(8):
    ledger.record_user(str(i), device_id=sys.argv[2], asr_confidence=None, audio_duration_ms=10)
print(json.dumps({"written": 8}))
"""
        # 子进程要 import voice_ledger（在 voice/）与 inner_loop（在 packaging/）。
        voice_root = Path(__file__).resolve().parent.parent
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(voice_root), str(voice_root.parent / "packaging")]),
        }
        children = [
            subprocess.Popen(
                [sys.executable, "-c", program, str(self.root), f"worker-{i}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for i in range(3)
        ]
        try:
            for child in children:
                out, err = child.communicate(timeout=45)
                self.assertEqual(child.returncode, 0, err)
                self.assertEqual(json.loads(out), {"written": 8})
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
        turns = self.ledger.recent_voice_turns(100)
        self.assertEqual(len(turns), 24)
        self.assertEqual(len({turn["turn_id"] for turn in turns}), 24)
        with self.store.connection() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
