"""Synthetic attachments and mocked transports; no screen/model/network use."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_vision import assert_evaluation
from vision_input import VisionInput

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1EAAAAASUVORK5CYII="
)


class VisionInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"], prefix="vision-input-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.obj = VisionInput(self.root)
        self.image = self.root / "fixture.png"
        self.image.write_bytes(PNG)

    def test_self_evaluate_has_owned_pure_hook_and_unverified_capture(self):
        report = assert_evaluation(self, self.obj.self_evaluate, "vision_input")
        text = " ".join(report["known_limitations"])
        self.assertIn("未验", text)
        self.assertIn("could not create image", text)
        self.assertIn("KG", text)

    def test_region_validation_never_calls_capture(self):
        with patch.object(self.obj, "_capture") as capture:
            for values in ((0, 0, 0, 1), (0, 0, 1, -1), (True, 0, 1, 1), (0, 0, 1.1, 1)):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    self.obj.capture_region(*values)
            capture.assert_not_called()
            self.obj.capture_region(-5, -8, 20, 30)
            capture.assert_called_once_with("-5,-8,20,30")

    def test_mock_capture_private_output_and_failure_cleanup(self):
        def fake(command, **kwargs):
            self.assertEqual(command[:4], ["/usr/sbin/screencapture", "-x", "-t", "png"])
            Path(command[-1]).write_bytes(PNG)

        with patch("subprocess.run", side_effect=fake) as run:
            target = Path(self.obj.capture_region(-1, 0, 3, 4))
        self.assertEqual(target.read_bytes(), PNG)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertIn("-R", run.call_args.args[0])
        target.unlink()
        with patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "fake", stderr=b"could not create image"),
        ):
            with self.assertRaisesRegex(RuntimeError, "截图失败"):
                self.obj.capture_screen()
        self.assertEqual(list(self.obj.vision_dir.iterdir()), [])

    def test_attachment_archived_and_two_rows_committed_with_provenance(self):
        import product_home
        from inner_loop import MindStore

        with patch.object(product_home, "protected_paths", return_value=()):
            record = self.obj.add_to_think_context(str(self.image), "合成几何图", "图中是什么")
            store = MindStore(self.root)
            attachments = store.entries(category="vision_input", limit=10)
            events = store.entries(category="event", limit=10)
            recent = self.obj.list_recent_images()
        self.assertEqual(len(attachments), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["refs"], [record["entry_id"]])
        self.assertEqual(events[0]["payload"]["kind"], "camera")
        self.assertEqual(events[0]["payload"]["data"]["source"], "vision_input")
        copied = Path(record["image_path"])
        self.assertEqual(copied.parent, self.obj.vision_dir)
        self.assertEqual(copied.read_bytes(), PNG)
        self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
        self.assertEqual(recent[0]["entry_id"], record["entry_id"])
        self.assertEqual(attachments[0]["payload"]["timestamp_source"], "ingestion")
        self.assertNotIn(base64.b64encode(PNG).decode(), json.dumps(events))

    def test_context_validation_and_missing_history(self):
        self.assertEqual(self.obj.list_recent_images(), [])
        for days in (-1, True, 1.1):
            with self.assertRaises(ValueError):
                self.obj.list_recent_images(days)
        for desc, question in (("", ""), ("x", None), ("x" * 32769, "")):
            with self.assertRaises(ValueError):
                self.obj.add_to_think_context(str(self.image), desc, question)
        self.assertFalse((self.root / "mind.db").exists())

    def test_transaction_failure_rolls_back_both_rows_and_copied_image(self):
        import product_home
        from inner_loop import MindStore

        with patch.object(product_home, "protected_paths", return_value=()):
            store = MindStore(self.root)
            with store.connection() as db:
                db.execute(
                    "CREATE TRIGGER reject_event BEFORE INSERT ON entries WHEN NEW.category='event' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
                )
            with self.assertRaises(Exception):
                self.obj.add_to_think_context(str(self.image), "合成图")
            self.assertEqual(store.entries(category="vision_input", limit=10), [])
            self.assertEqual(store.entries(category="event", limit=10), [])
        self.assertEqual(list(self.obj.vision_dir.iterdir()), [])
        self.assertEqual(self.image.read_bytes(), PNG)

    def test_describe_mocked_chat_and_responses_payloads(self):
        import chat_entry

        for mode in ("chat_completions", "responses"):
            route = dict(
                provider="custom",
                model="fixture",
                base_url="https://invalid.example/v1",
                api_mode=mode,
                reasoning_effort=None,
            )
            events = (
                [json.dumps({"choices": [{"delta": {"content": "合成图"}}]}), "[DONE]"]
                if mode == "chat_completions"
                else [
                    json.dumps({"type": "response.output_text.delta", "delta": "合成图"}),
                    json.dumps({"type": "response.completed"}),
                ]
            )
            lines = Mock()
            with (
                self.subTest(mode=mode),
                patch.object(
                    chat_entry.stream_config, "read_config", return_value=Mock(values=route)
                ),
                patch.object(chat_entry.stream_config, "read_key", return_value="FAKE_KEY"),
                patch.object(chat_entry, "validate_route"),
                patch.object(chat_entry, "_stream_lines", return_value=lines) as stream,
                patch.object(chat_entry, "_sse_events", return_value=events),
            ):
                self.assertEqual(self.obj.describe_image(str(self.image)), "合成图")
                payload = stream.call_args.args[1]
                self.assertIn("input" if mode == "responses" else "messages", payload)
                self.assertNotIn("messages" if mode == "responses" else "input", payload)
                lines.close.assert_called_once()

    def test_incomplete_or_secret_stream_is_not_published(self):
        import chat_entry

        route = dict(
            provider="custom",
            model="fixture",
            base_url="https://invalid.example",
            api_mode="chat_completions",
            reasoning_effort=None,
        )
        for events in (
            [json.dumps({"choices": [{"delta": {"content": "partial"}}]})],
            [json.dumps({"choices": [{"delta": {"content": "FAKE_KEY"}}]}), "[DONE]"],
            [json.dumps({"choices": [{"delta": {"tool_calls": [{}]}}]}), "[DONE]"],
        ):
            lines = Mock()
            with (
                patch.object(
                    chat_entry.stream_config, "read_config", return_value=Mock(values=route)
                ),
                patch.object(chat_entry.stream_config, "read_key", return_value="FAKE_KEY"),
                patch.object(chat_entry, "validate_route"),
                patch.object(chat_entry, "_stream_lines", return_value=lines),
                patch.object(chat_entry, "_sse_events", return_value=events),
            ):
                with self.assertRaisesRegex(RuntimeError, "未生成可靠描述"):
                    self.obj.describe_image(str(self.image))
                lines.close.assert_called_once()
        self.assertFalse((self.root / "mind.db").exists())


if __name__ == "__main__":
    unittest.main()
