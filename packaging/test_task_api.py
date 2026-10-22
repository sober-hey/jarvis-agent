"""Behavior checks using real private files and a deterministic fixture provider."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import product_config as config
import task_api as task


class TaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis-task-", dir="/Users/sober")
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name)
        self.root = self.parent / "data"
        self.root.mkdir(mode=0o700)
        self.input = self.root / "notes.txt"
        self.input.write_text("beta\nalpha\nbeta\n", encoding="utf-8")
        self.input.chmod(0o600)
        self.config = self.parent / "config.json"
        config.write_config(
            self.config,
            {
                "model": "fixture",
                "provider": "custom",
                "base_url": "https://example.invalid/v1",
                "api_mode": "chat_completions",
                "context_length": 256000,
                "reasoning_effort": "high",
            },
        )
        self.key = self.parent / "fake.key"
        self.key.write_text("FAKE-task-fixture")
        self.key.chmod(0o600)

    def submit(self, **kw: Any) -> dict[str, Any]:
        return task.submit(
            config_path=self.config,
            data_dir=self.root,
            key_file=self.key,
            input_path=self.input,
            instruction="整理资料",
            fixture=True,
            **kw,
        )

    def test_candidate_is_verified_and_never_published_implicitly(self) -> None:
        result = self.submit()
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertFalse((self.root / "published").exists())
        self.assertEqual(self.input.read_text(), "beta\nalpha\nbeta\n")
        candidate = self.root / str(result["candidate_relpath"])
        self.assertEqual(
            hashlib.sha256(candidate.read_bytes()).hexdigest(), result["output_sha256"]
        )
        self.assertEqual(
            result["input_sha256"], hashlib.sha256(self.input.read_bytes()).hexdigest()
        )
        self.assertEqual(task.query(self.root, str(result["task_id"])), result)

    def test_publish_is_explicit_and_idempotent(self) -> None:
        result = self.submit(publish=True, request_key="one")
        self.assertEqual(result["status"], "completed")
        target = self.root / str(result["published_relpath"])
        before = target.stat().st_ino
        again = self.submit(publish=True, request_key="one")
        self.assertEqual(result, again)
        self.assertEqual(target.stat().st_ino, before)
        self.assertEqual(len(result["operations"]), 3)

    def filesystem_snapshot(self) -> dict[str, tuple[bytes | None, int, int]]:
        return {
            str(path.relative_to(self.root)): (
                path.read_bytes() if path.is_file() else None,
                path.stat().st_ino,
                path.stat().st_mtime_ns,
            )
            for path in [self.root, *self.root.rglob("*")]
        }

    def test_completed_replay_and_query_are_strictly_read_only(self) -> None:
        import subprocess
        from unittest.mock import patch

        with patch.object(task, "atomic", wraps=task.atomic) as initial_writer:
            result = self.submit(publish=True, request_key="zero-write")
        self.assertEqual(result["status"], "completed")
        self.assertGreater(initial_writer.call_count, 0)
        before = self.filesystem_snapshot()
        with (
            patch.object(task, "atomic", wraps=task.atomic) as writer,
            patch.object(task, "save", wraps=task.save) as saver,
            patch.object(subprocess, "Popen", wraps=subprocess.Popen) as launcher,
        ):
            for action in ("publish", "query"):
                with self.subTest(action=action):
                    actual = (
                        self.submit(publish=True, request_key="zero-write")
                        if action == "publish"
                        else task.query(self.root, str(result["task_id"]))
                    )
                    self.assertEqual(actual, result)
                    self.assertEqual(self.filesystem_snapshot(), before)
                    self.assertEqual(writer.call_count, 0)
                    self.assertEqual(saver.call_count, 0)
                    self.assertEqual(launcher.call_count, 0)

    def test_completed_replay_validates_identity_receipts_and_target(self) -> None:
        import copy
        import json
        from unittest.mock import patch

        result = self.submit(publish=True, request_key="verified-replay")
        ledger = self.root / "tasks" / (str(result["task_id"]) + ".json")
        target = self.root / str(result["published_relpath"])
        original_target = target.read_bytes()
        for change in ("request", "operation", "receipt", "approval", "target", "missing"):
            with self.subTest(change=change):
                value = copy.deepcopy(result)
                if change == "request":
                    value["request_key"] = "other"
                elif change == "operation":
                    value["operations"][-1]["operation_key"] = "0" * 64
                elif change == "receipt":
                    value["operations"][-1]["receipt"]["readback_sha256"] = "0" * 64
                elif change == "approval":
                    value["approval"]["output_sha256"] = "0" * 64
                elif change == "target":
                    target.write_bytes(b"tampered")
                else:
                    target.unlink()
                ledger.write_text(json.dumps(value))
                before = self.filesystem_snapshot()
                with patch.object(task, "atomic", wraps=task.atomic) as writer:
                    with self.assertRaises((ValueError, OSError)):
                        self.submit(publish=True, request_key="verified-replay")
                    self.assertEqual(writer.call_count, 0)
                self.assertEqual(self.filesystem_snapshot(), before)
                target.write_bytes(original_target)
                target.chmod(0o600)

    def test_read_only_observation_preserves_unknown_and_cancel(self) -> None:
        import json
        from unittest.mock import patch

        result = self.submit(publish=True, request_key="uncertain-replay")
        ledger = self.root / "tasks" / (str(result["task_id"]) + ".json")
        result["operations"][-1]["receipt"] = None
        ledger.write_text(json.dumps(result))
        task.cancel(self.root, str(result["task_id"]))
        before = self.filesystem_snapshot()
        with patch.object(task, "atomic", wraps=task.atomic) as writer:
            for actual in (
                task.query(self.root, str(result["task_id"])),
                self.submit(publish=True, request_key="uncertain-replay"),
            ):
                self.assertEqual(actual["status"], "unknown")
                self.assertTrue(actual["in_flight"])
                self.assertTrue(actual["reconciliation_required"])
                self.assertTrue(actual["cancel_requested"])
            self.assertEqual(writer.call_count, 0)
        self.assertEqual(self.filesystem_snapshot(), before)

    def test_cli_publish_replay_and_query_change_no_files(self) -> None:
        import json
        import subprocess

        base = [sys.executable, "-B", str(Path(__file__).with_name("jarvis.py")), "task"]
        submit = [
            *base,
            "submit",
            "--config",
            str(self.config),
            "--data-dir",
            str(self.root),
            "--input",
            str(self.input),
            "--instruction",
            "整理资料",
            "--key-file",
            str(self.key),
            "--request-key",
            "cli-zero-write",
            "--fixture",
            "--publish",
        ]
        initial = subprocess.run(submit, capture_output=True, text=True, timeout=10)
        self.assertEqual(initial.returncode, 0, initial.stderr)
        result = json.loads(initial.stdout)
        before = self.filesystem_snapshot()
        for command in (
            submit,
            [*base, "query", "--data-dir", str(self.root), "--task-id", result["task_id"]],
        ):
            actual = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(actual.returncode, 0, actual.stderr)
            self.assertEqual(json.loads(actual.stdout), result)
            self.assertEqual(self.filesystem_snapshot(), before)

    def test_query_missing_task_creates_nothing(self) -> None:
        before = self.filesystem_snapshot()
        with self.assertRaises(FileNotFoundError):
            task.query(self.root, "task-" + "0" * 64)
        self.assertEqual(self.filesystem_snapshot(), before)

    def test_key_conflict_is_rejected(self) -> None:
        self.submit(request_key="one")
        self.input.write_text("changed")
        with self.assertRaises(ValueError):
            self.submit(request_key="one", publish=True)
        self.assertFalse((self.root / "published").exists())

    def test_cancel_stops_new_operations(self) -> None:
        result = self.submit()
        stopped = task.cancel(self.root, str(result["task_id"]))
        self.assertTrue(stopped["cancel_requested"])
        again = self.submit(publish=True)
        self.assertTrue(again["cancel_requested"])
        self.assertFalse((self.root / "published").exists())

    def test_target_collision_never_overwrites(self) -> None:
        result = self.submit()
        dest = self.root / "published"
        dest.mkdir(mode=0o700)
        target = dest / (str(result["task_id"]) + ".md")
        target.write_text("original")
        target.chmod(0o600)
        self.assertEqual(self.submit(publish=True)["status"], "failed")
        self.assertEqual(target.read_text(), "original")

    def test_candidate_tampering_is_rejected(self) -> None:
        result = self.submit()
        (self.root / str(result["candidate_relpath"])).write_text("tampered")
        self.assertEqual(self.submit(publish=True)["status"], "failed")

    def test_unknown_is_not_retried(self) -> None:
        from unittest.mock import patch

        original = task.atomic
        with patch.object(task, "atomic", wraps=task.atomic) as writer:

            def uncertain(parent: int, name: str, data: bytes, *, replace: bool = True) -> None:
                if name == "candidate.md":
                    raise OSError("injected unknown side effect")
                original(parent, name, data, replace=replace)

            writer.side_effect = uncertain
            result = self.submit()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.submit(publish=True)["status"], "unknown")
        self.assertFalse((self.root / "published").exists())
        self.assertTrue(task.cancel(self.root, str(result["task_id"]))["in_flight"])

    def test_traversal_symlink_and_outside_are_rejected(self) -> None:
        for path in [self.parent / "outside", self.root / ".." / "outside"]:
            with self.assertRaises(ValueError):
                task.submit(
                    config_path=self.config,
                    data_dir=self.root,
                    key_file=self.key,
                    input_path=path,
                    instruction="整理",
                )
        link = self.root / "link.txt"
        link.symlink_to(self.input)
        with self.assertRaises((OSError, ValueError)):
            task.submit(
                config_path=self.config,
                data_dir=self.root,
                key_file=self.key,
                input_path=link,
                instruction="整理",
            )

    def test_nonprivate_input_and_host_root_are_rejected(self) -> None:
        self.input.chmod(0o644)
        with self.assertRaises(ValueError):
            self.submit()
        with self.assertRaises(ValueError):
            task.query(Path("/Volumes/扩展硬盘/hermes"), "task-" + "0" * 64)

    def test_no_real_key_is_accepted(self) -> None:
        self.key.write_text("REAL-key-rejected")
        with self.assertRaises(ValueError):
            self.submit()

    def test_permissions_and_ledger_do_not_persist_credentials(self) -> None:
        import os
        import stat

        result = self.submit(publish=True)
        for path in self.root.rglob("*"):
            expected = 0o700 if path.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected)
            if path.is_file():
                self.assertNotIn(b"FAKE-task-fixture", path.read_bytes())
                self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertEqual(result["contract_pack"], "NOT_FROZEN")

    def test_cancel_in_flight_does_not_wait_or_admit_publish(self) -> None:
        import threading
        from unittest.mock import patch

        candidate_ready = threading.Event()
        release = threading.Event()
        original = task.atomic
        results: list[dict[str, Any]] = []
        failures: list[Exception] = []

        def paused(parent: int, name: str, data: bytes, *, replace: bool = True) -> None:
            if name == "candidate.md":
                candidate_ready.set()
                if not release.wait(5):
                    raise OSError("test timeout")
            original(parent, name, data, replace=replace)

        def worker() -> None:
            try:
                results.append(self.submit(request_key="race", publish=True))
            except Exception as exc:
                failures.append(exc)

        with patch.object(task, "atomic", side_effect=paused):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(candidate_ready.wait(5))
                task_id = "task-" + task.digest(b"race")
                stopped = task.cancel(self.root, task_id)
                self.assertTrue(stopped["cancel_requested"])
                self.assertTrue(stopped["in_flight"])
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertFalse(failures)
        self.assertTrue(results[0]["cancel_requested"])
        self.assertNotEqual(results[0]["status"], "completed")
        self.assertFalse(any((self.root / "published").glob("*.md")))

    def test_fd_and_real_cli(self) -> None:
        import json
        import subprocess

        command = [
            sys.executable,
            "-B",
            str(Path(__file__).with_name("jarvis.py")),
            "task",
            "submit",
            "--config",
            str(self.config),
            "--data-dir",
            str(self.root),
            "--input",
            str(self.input),
            "--instruction",
            "整理资料",
            "--key-fd",
            "0",
            "--fixture",
        ]
        result = subprocess.run(
            command, input="FAKE-task-fixture", text=True, capture_output=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "awaiting_approval")
        self.assertNotIn("FAKE-task-fixture", result.stdout)


class SelfEvaluationTests(unittest.TestCase):
    def test_offline_module_evaluation_describes_real_scope(self):
        import importlib
        import json
        from datetime import datetime
        from unittest.mock import patch

        module = importlib.import_module("task_api")
        hook = getattr(module, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-level self_evaluate is required")
        with (
            patch("pathlib.Path.open", side_effect=AssertionError("unexpected file IO")),
            patch("pathlib.Path.mkdir", side_effect=AssertionError("unexpected mkdir")),
            patch("subprocess.Popen", side_effect=AssertionError("unexpected process")),
        ):
            report = hook()
        self.assertEqual(report["module"], "task_api")
        self.assertTrue(report["domain"].strip())
        self.assertTrue(report["current_version"].strip())
        for key in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[key], list)
            self.assertTrue(report[key])
            self.assertTrue(all(isinstance(value, str) and value.strip() for value in report[key]))
        self.assertIn("tool", " ".join(report["known_limitations"]).lower())
        self.assertIsNotNone(datetime.fromisoformat(report["last_evaluated_at"]).utcoffset())
        report["known_limitations"].clear()
        self.assertTrue(hook()["known_limitations"])
        json.dumps(report)


if __name__ == "__main__":
    unittest.main()
