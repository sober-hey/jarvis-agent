"""Daily reflection tests, with only temporary synthetic product data."""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

DAY = "2026-10-14"
STAMP = "2026-10-14T12:00:00+08:00"


def fixture(root: Path) -> None:
    """Build minimal real-format records; usable by bounded real acceptance."""
    home = root / "engine-home"
    home.mkdir(mode=0o700)
    with sqlite3.connect(home / "state.db") as db:
        db.execute(
            "CREATE TABLE messages(id INTEGER, session_id TEXT, role TEXT, "
            "content TEXT, timestamp REAL)"
        )
        for identity, role, text in [
            (1, "user", "Sir：我今天有点累。你记错了，我不喝咖啡，我喝茶。"),
            (2, "assistant", "我记错了您的饮品偏好，已纠正为茶。"),
            (3, "user", "请整理项目会议纪要，失败时告诉我证据，不要说完成了。"),
        ]:
            db.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?)",
                (identity, "web-day", role, text, datetime.fromisoformat(STAMP).timestamp()),
            )
        db.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?)",
            (
                4,
                "old",
                "user",
                "前一天的消息",
                datetime.fromisoformat("2026-10-13T12:00:00+08:00").timestamp(),
            ),
        )
    with sqlite3.connect(root / "memory.db") as db:
        db.execute(
            "CREATE TABLE facts(id TEXT, content TEXT, created_at TEXT, status TEXT, "
            "supersedes TEXT, source_message TEXT)"
        )
        db.execute(
            "INSERT INTO facts VALUES(?,?,?,?,?,?)",
            ("old", "Sir 喝咖啡", "2026-10-12T10:00:00+08:00", "corrected", None, "m1"),
        )
        db.execute(
            "INSERT INTO facts VALUES(?,?,?,?,?,?)",
            ("new", "Sir 喝茶，不喝咖啡", STAMP, "active", "old", "old"),
        )
    for identity, state, detail in [
        ("good", "completed", "会议纪要整理已完成并核验文件"),
        ("bad", "blocked", "PDF 导出失败：输入格式不支持，没有生成文件"),
    ]:
        folder = root / "file-chains" / identity
        folder.mkdir(parents=True, mode=0o700)
        (folder / "record.json").write_text(
            json.dumps(
                {
                    "task_id": identity,
                    "status": state,
                    "instruction": detail,
                    "error": detail if identity == "bad" else None,
                    "updated_at": STAMP,
                },
                ensure_ascii=False,
            )
        )
    with sqlite3.connect(root / "affect.db") as db:
        db.execute("CREATE TABLE events(id INTEGER, timestamp REAL, kind TEXT, reason TEXT)")
        db.execute(
            "INSERT INTO events VALUES(1,?,?,?)",
            (datetime.fromisoformat(STAMP).timestamp(), "concern", "Sir 自述累了"),
        )
    with sqlite3.connect(root / "mind.db") as db:
        db.execute("CREATE TABLE entries(id TEXT, created_at TEXT, category TEXT, payload TEXT)")
        db.execute(
            "INSERT INTO entries VALUES(?,?,?,?)",
            ("thought1", STAMP, "thought", '{"text":"我想先核验任务结果"}'),
        )


def sample(digest: dict[str, Any]) -> dict[str, Any]:
    refs = [row["id"] for row in digest["events"]]
    return {
        "date": digest["date"],
        "what_happened": "我整理了会议纪要，也被纠正了偏好。",
        "did_well": ["我核验了文件。"],
        "could_improve": ["我记错了偏好，也有导出失败。"],
        "sir_state": "我观察到 Sir 自述有点累，不能据此判断疾病。",
        "tomorrow": ["我想先核实输入格式。"],
        "lessons": [
            {
                "trigger": "Sir 纠正我记错的偏好或文件导出失败时",
                "action": "我先核对纠正记录和实际产物，再更新认识并告知失败原因。",
                "self_knowledge": "我需要以纠正与验收证据约束自己的判断。",
                "evidence_ids": refs,
            }
        ],
    }


class ReflectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="reflection-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def module(self) -> Any:
        return importlib.import_module("reflection")

    def snapshot(self) -> dict[str, tuple[bytes | None, int, int]]:
        return {
            str(p.relative_to(self.root)): (
                p.read_bytes() if p.is_file() else None,
                p.stat().st_ino,
                p.stat().st_mtime_ns,
            )
            for p in [self.root, *self.root.rglob("*")]
        }

    def test_collect_real_formats_read_only_and_date_filter(self) -> None:
        fixture(self.root)
        before = self.snapshot()
        digest = self.module().collect_day(DAY, self.root)
        self.assertEqual(digest["conversation"]["count"], 3)
        self.assertEqual(len(digest["completed_tasks"]), 1)
        self.assertEqual(len(digest["failed_tasks"]), 1)
        self.assertEqual(digest["corrected_memories"][0]["old_content"], "Sir 喝咖啡")
        self.assertEqual(len(digest["emotions"]), 1)
        self.assertEqual(len(digest["thoughts"]), 1)
        self.assertEqual(self.snapshot(), before)

    def test_missing_files_and_missing_tables_create_nothing(self) -> None:
        module = self.module()
        before = self.snapshot()
        self.assertEqual(module.collect_day(DAY, self.root)["events"], [])
        self.assertEqual(self.snapshot(), before)
        for name in ["memory.db", "affect.db", "mind.db"]:
            sqlite3.connect(self.root / name).close()
        before = self.snapshot()
        digest = module.collect_day(DAY, self.root)
        self.assertEqual(digest["events"], [])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(digest["warnings"])

    def test_validation_is_strict_and_evidence_grounded(self) -> None:
        fixture(self.root)
        module = self.module()
        digest = module.collect_day(DAY, self.root)
        good = sample(digest)
        # The sample intentionally cites every available event; include the
        # task and conversation ids produced by the collector.
        good["lessons"][0]["evidence_ids"] = [row["id"] for row in digest["events"]]
        self.assertEqual(module.validate_reflection(json.dumps(good), digest), good)
        bads = [
            "```json\n" + json.dumps(good) + "\n```",
            json.dumps({**good, "date": "2026-10-13"}),
            json.dumps({**good, "extra": True}),
            json.dumps({**good, "lessons": []}),
        ]
        for bad in bads:
            with self.subTest(bad=bad[:80]), self.assertRaises(ValueError):
                module.validate_reflection(bad, digest)
        good["lessons"][0]["evidence_ids"] = ["invented"]
        with self.assertRaises(ValueError):
            module.validate_reflection(json.dumps(good), digest)

    def test_one_call_cached_and_merged_lessons_keep_sources(self) -> None:
        fixture(self.root)
        module = self.module()
        digest = module.collect_day(DAY, self.root)
        calls: list[str] = []
        good = sample(digest)
        good["lessons"][0]["evidence_ids"] = [row["id"] for row in digest["events"]]

        def model(prompt: str) -> str:
            calls.append(prompt)
            return json.dumps(good, ensure_ascii=False)

        first = module.reflect(digest, self.root, model=model)
        second = module.reflect(digest, self.root, model=model)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(len(calls), 1)
        digest["date"] = "2026-10-15"
        second_day = sample(digest)
        second_day["date"] = "2026-10-15"

        def next_day_model(prompt: str) -> str:
            return json.dumps(second_day, ensure_ascii=False)

        module.reflect(digest, self.root, model=next_day_model)
        lessons = module.lessons_for_context(8, self.root, query="纠正")
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["count"], 2)
        self.assertEqual(len(lessons[0]["sources"]), 2)
        self.assertEqual(lessons[0]["kind"], "jarvis_self_knowledge")

    def test_invalid_json_is_logged_and_skipped_without_retry_or_lessons(self) -> None:
        module = self.module()
        digest = module.collect_day(DAY, self.root)
        calls: list[str] = []

        def model(prompt: str) -> str:
            calls.append(prompt)
            return "not JSON"

        self.assertEqual(module.reflect(digest, self.root, model=model)["status"], "invalid")
        self.assertEqual(module.reflect(digest, self.root, model=model)["status"], "invalid")
        self.assertEqual(len(calls), 1)
        self.assertEqual(module.lessons_for_context(8, self.root), [])
        with sqlite3.connect(self.root / "reflection.db") as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM reflections WHERE status='invalid'").fetchone()[0],
                1,
            )

    def test_wal_messages_are_visible_without_source_writes(self) -> None:
        home = self.root / "engine-home"
        home.mkdir()
        db = sqlite3.connect(home / "state.db")
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE messages(id INTEGER, session_id TEXT, role TEXT, "
            "content TEXT, timestamp REAL)"
        )
        db.execute(
            "INSERT INTO messages VALUES(1,'wal','user','待复盘',?)",
            (datetime.fromisoformat(STAMP).timestamp(),),
        )
        db.commit()
        before = self.snapshot()
        self.assertEqual(self.module().collect_day(DAY, self.root)["conversation"]["count"], 1)
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_json_key_and_malformed_lesson_rejected(self) -> None:
        fixture(self.root)
        module = self.module()
        digest = module.collect_day(DAY, self.root)
        value = sample(digest)
        raw = json.dumps(value)
        duplicate = raw.replace('"date":', '"date":"2026-10-14","date":', 1)
        with self.assertRaises(ValueError):
            module.validate_reflection(duplicate, digest)
        value["lessons"] = ["not an object"]
        with self.assertRaises(ValueError):
            module.validate_reflection(json.dumps(value), digest)

    def test_empty_day_can_reflect_without_invented_lessons(self) -> None:
        module = self.module()
        digest = module.collect_day(DAY, self.root)
        value = sample(digest)
        value["lessons"] = []
        self.assertEqual(module.validate_reflection(json.dumps(value), digest), value)

    def test_interrupted_day_is_not_automatically_retried(self) -> None:
        module = self.module()
        digest = module.collect_day(DAY, self.root)

        def interrupted(prompt: str) -> str:
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            module.reflect(digest, self.root, model=interrupted)
        result = module.reflect(digest, self.root, model=interrupted)
        self.assertEqual(result["status"], "started")
        self.assertTrue(result["cached"])

    def test_failed_and_completed_do_not_include_planned_tasks(self) -> None:
        fixture(self.root)
        folder = self.root / "file-chains" / "planned"
        folder.mkdir()
        (folder / "record.json").write_text(json.dumps({"status": "planned", "updated_at": STAMP}))
        digest = self.module().collect_day(DAY, self.root)
        self.assertEqual(len(digest["completed_tasks"]), 1)
        self.assertEqual(len(digest["failed_tasks"]), 1)

    def test_utc_boundary_and_task_mtime_fallback_are_explicit(self) -> None:
        fixture(self.root)
        home = self.root / "engine-home"
        with sqlite3.connect(home / "state.db") as db:
            db.execute(
                "INSERT INTO messages VALUES(5,'boundary','user','本地零点',?)",
                (datetime.fromisoformat("2026-10-13T16:00:00+00:00").timestamp(),),
            )
        path = self.root / "tasks" / "fallback.json"
        path.parent.mkdir()
        path.write_text('{"task_id":"fallback","status":"failed","error":"failure"}')
        stamp = datetime.fromisoformat(STAMP).timestamp()
        os.utime(path, (stamp, stamp))
        digest = self.module().collect_day(DAY, self.root)
        self.assertEqual(digest["conversation"]["count"], 4)
        self.assertTrue(any(t["time_basis"] == "file_mtime" for t in digest["failed_tasks"]))


class SelfEvaluationTests(unittest.TestCase):
    def hook(self):
        import importlib

        module = importlib.import_module("reflection")
        return module, module.self_evaluate

    def test_honest_schema_identity_algorithm_and_fresh_timestamp(self):
        import json
        from datetime import UTC, datetime

        _module, hook = self.hook()
        before = datetime.now(UTC)
        report = hook()
        after = datetime.now(UTC)
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
        self.assertEqual(report["module"], "reflection")
        self.assertIn("daily", report["current_version"].lower())
        self.assertIn("retry", " ".join(report["known_limitations"]).lower())
        self.assertTrue(report["domain"].strip())
        for key in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[key], list)
            self.assertTrue(report[key])
            self.assertTrue(all(isinstance(item, str) and item.strip() for item in report[key]))
        stamp = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, after)
        json.dumps(report, allow_nan=False)

    def test_metadata_is_io_free_without_initialized_store_or_credentials(self):
        from pathlib import Path
        import sqlite3
        import socket
        import subprocess
        import os
        from unittest.mock import patch

        _module, hook = self.hook()
        with (
            patch("builtins.open", side_effect=AssertionError("file access")),
            patch.object(Path, "open", side_effect=AssertionError("path access")),
            patch.object(sqlite3, "connect", side_effect=AssertionError("database access")),
            patch.object(socket, "socket", side_effect=AssertionError("network access")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("process access")),
            patch.object(os, "open", side_effect=AssertionError("descriptor access")),
        ):
            first = hook()
            first["known_limitations"].append("mutation sentinel")
            first["improvement_directions"].clear()
            second = hook()
        self.assertNotIn("mutation sentinel", second["known_limitations"])
        self.assertTrue(second["improvement_directions"])
        self.assertEqual(second["module"], "reflection")


if __name__ == "__main__":
    unittest.main()
