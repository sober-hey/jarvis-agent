"""Vision contract tests; all fixtures are disposable and offline."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from datetime import UTC, datetime


def assert_evaluation(test: unittest.TestCase, hook: Any, module: str) -> dict[str, Any]:
    """Shared schema/freshness/read-only assertions, never a product hook."""
    started = datetime.now(UTC)
    with (
        patch("builtins.open", side_effect=AssertionError("hook performed I/O")),
        patch.object(Path, "open", side_effect=AssertionError("hook performed I/O")),
        patch("sqlite3.connect", side_effect=AssertionError("hook opened a database")),
        patch("subprocess.run", side_effect=AssertionError("hook invoked hardware")),
        patch("urllib.request.build_opener", side_effect=AssertionError("hook used network")),
    ):
        report = hook()
    test.assertEqual(
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
    test.assertEqual(report["module"], module)
    for key in ("domain", "current_version"):
        test.assertIsInstance(report[key], str)
        test.assertTrue(report[key].strip())
    for key in ("known_limitations", "improvement_directions"):
        test.assertIsInstance(report[key], list)
        test.assertTrue(report[key])
        test.assertTrue(all(isinstance(item, str) and item.strip() for item in report[key]))
    timestamp = datetime.fromisoformat(report["last_evaluated_at"])
    test.assertIsNotNone(timestamp.utcoffset())
    test.assertLessEqual(started, timestamp)
    test.assertLessEqual(timestamp, datetime.now(UTC))
    original = list(report["known_limitations"])
    report["known_limitations"].append("TEST_MUTATION")
    test.assertEqual(hook()["known_limitations"], original)
    report["known_limitations"] = original
    # Gate against borrowing another component's hook or obvious placeholders.
    from integration_registry import REGISTRY
    from dataclasses import replace

    component = replace(REGISTRY.get(module), source_paths=(module + ".py",))
    test.assertTrue(REGISTRY._has_self_evaluate(component))
    return report


def load_vision() -> Any:
    path = Path(__file__).with_name("vision.py")
    if not path.is_file():
        raise AssertionError("vision.py implementation is missing")
    spec = importlib.util.spec_from_file_location("jarvis_vision_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vision = load_vision()
        scratch = Path(os.environ.get("JARVIS_TEST_SCRATCH", tempfile.gettempdir())).resolve()
        self.temp = tempfile.TemporaryDirectory(prefix="jarvis-vision-", dir=scratch)
        self.root = Path(self.temp.name)
        self.config = self.root / "vision_config.json"
        self.route = {
            "provider": "custom:test",
            "model": "test-vision",
            "base_url": "https://example.com/v1",
        }
        self.vision.write_vision_config(self.config, self.route)
        self.key_file = self.root / "key"
        self.key_file.write_text("FAKE-vision-test-key")
        self.key_file.chmod(0o600)
        self.image = self.root / "test.png"
        self.data = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1EAAAAASUVORK5CYII="
        )
        self.image.write_bytes(self.data)
        self.raw = {
            "summary": "白底上有红色圆形。",
            "objects": ["红色圆形"],
            "people_count": 2,
            "text_in_image": ["你好"],
            "scene": "测试图",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def describe(self, transport: Any, **kwargs: Any) -> Any:
        read, write = os.pipe()
        try:
            os.write(write, b"FAKE-vision-test-key")
            os.close(write)
            return self.vision.describe(
                self.image,
                vision_config=self.config,
                key_fd=read,
                transport=transport,
                **kwargs,
            )
        finally:
            os.close(read)

    def test_self_evaluate_is_pure_owned_and_truthful_about_model_limits(self) -> None:
        report = assert_evaluation(self, self.vision.self_evaluate, "vision")
        text = " ".join(report["known_limitations"])
        self.assertIn("单次", report["current_version"])
        self.assertIn("身份", text)
        self.assertIn("重试", text)
        self.assertIn("模型", text)

    def test_config_is_independent_private_create_only(self) -> None:
        self.assertEqual(self.vision.read_vision_config(self.config), self.route)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            self.vision.write_vision_config(self.config, self.route)
        for route in (
            {**self.route, "api_key": "FAKE-no-store"},
            {**self.route, "base_url": "https://user:pass@example.com/v1"},
            {**self.route, "base_url": "http://example.com/v1"},
            {**self.route, "base_url": "https://example.com/v1?key=FAKE"},
        ):
            with self.assertRaises(self.vision.VisionError):
                self.vision.validate_config(route)
        self.config.chmod(0o644)
        with self.assertRaises(self.vision.VisionError):
            self.vision.read_vision_config(self.config)

    def test_event_provenance_and_single_request(self) -> None:
        fake = Mock(return_value=json.dumps(self.raw))
        result = self.describe(fake)
        self.assertTrue(result["ok"])
        fake.assert_called_once()
        self.assertEqual(result["people_count"], 2)
        self.assertEqual(result["event"]["source"], "camera")
        self.assertEqual(result["event"]["kind"], "vision")
        self.assertEqual(result["event"]["sensitivity"], "high")
        self.assertEqual(result["event"]["provenance"]["path"], str(self.image))
        self.assertIn("ingested_at", result["event"])
        self.assertEqual(
            result["event"]["provenance"]["sha256"], hashlib.sha256(self.data).hexdigest()
        )
        self.assertEqual(
            result["event"]["payload"]["backend"],
            {"provider": "custom:test", "model": "test-vision"},
        )
        self.assertNotIn("FAKE-vision-test-key", json.dumps(result))
        request = fake.call_args.args[2]
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request["messages"][0]["content"], self.vision.SYSTEM_PROMPT)

    def test_http_wrapper_contract_and_rejects_redirects(self) -> None:
        from unittest.mock import MagicMock
        from urllib.request import Request

        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": json.dumps(self.raw)}}]}
        ).encode()
        opener = Mock()
        opener.open.return_value = response
        with patch.object(self.vision, "build_opener", return_value=opener):
            result = self.vision._request_once(self.route, "FAKE-safe-key", {}, 3)
            self.assertEqual(json.loads(result), self.raw)
            opener.open.assert_called_once()
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://example.com/v1/chat/completions")
            self.assertEqual(request.get_method(), "POST")
            response.read.assert_called_once_with(65537)
        with self.assertRaises(self.vision.VisionError):
            self.vision._NoRedirect().redirect_request(
                Request("https://example.com"), None, 302, "move", {}, "https://evil.test"
            )
        for body in (b"[]", b"not json", b"x" * 65537):
            response.read.return_value = body
            with (
                patch.object(self.vision, "build_opener", return_value=opener),
                self.assertRaises(self.vision.VisionError),
            ):
                self.vision._request_once(self.route, "FAKE-safe-key", {}, 3)

    def test_errors_never_retry_or_expose_key_or_raw_output(self) -> None:
        for fake in (
            Mock(return_value="not JSON FAKE-secret"),
            Mock(return_value=json.dumps({**self.raw, "face_identity": "某人"})),
            Mock(return_value=json.dumps({**self.raw, "scene": "FAKE-vision-test-key"})),
            Mock(side_effect=RuntimeError("FAKE-vision-test-key HTTP body")),
        ):
            with self.subTest(fake=fake):
                result = self.describe(fake)
                self.assertFalse(result["ok"])
                self.assertFalse(result["retry"])
                self.assertNotIn("FAKE-", json.dumps(result))
                fake.assert_called_once()

    def test_key_fd_works_and_bad_key_modes_block_before_request(self) -> None:
        fake = Mock(return_value=json.dumps(self.raw))
        read, write = os.pipe()
        try:
            os.write(write, b"FAKE-vision-fd-key")
            os.close(write)
            result = self.vision.describe(
                self.image, vision_config=self.config, key_fd=read, transport=fake
            )
        finally:
            os.close(read)
        self.assertTrue(result["ok"])
        self.key_file.chmod(0o644)
        fake.reset_mock()
        self.assertFalse(
            self.vision.describe(
                self.image,
                vision_config=self.config,
                key_file=self.key_file,
                transport=fake,
            )["ok"]
        )
        fake.assert_not_called()

    def test_bad_image_and_naive_timestamp_do_not_call_model(self) -> None:
        fake = Mock(return_value=json.dumps(self.raw))
        self.image.write_bytes(b"not an image")
        self.assertFalse(self.describe(fake)["ok"])
        self.image.write_bytes(self.data)
        self.assertFalse(self.describe(fake, observed_at="2026-10-15T12:00:00")["ok"])
        fake.assert_not_called()

    def test_camera_missing_permission_timeout_never_retry(self) -> None:
        output = self.root / "frame.jpg"
        with (
            patch.object(self.vision.shutil, "which", return_value=None),
            patch.object(self.vision.subprocess, "run") as run,
        ):
            self.assertEqual(self.vision.capture(output)["error"], "capture_tool_missing")
            run.assert_not_called()
        for effect in (
            subprocess.CompletedProcess([], 1, b"", b"permission denied"),
            subprocess.TimeoutExpired("ffmpeg", 1),
        ):
            with (
                patch.object(self.vision.shutil, "which", side_effect=[None, "/fake/ffmpeg"]),
                patch.object(self.vision.subprocess, "run") as run,
            ):
                if isinstance(effect, subprocess.TimeoutExpired):
                    run.side_effect = effect
                else:
                    run.return_value = effect
                result = self.vision.capture(output)
                self.assertFalse(result["ok"])
                self.assertFalse(result["retry"])
                run.assert_called_once()
                self.assertFalse(output.exists())
                self.assertEqual(list(self.root.glob(".vision-*")), [])

    def test_camera_failures_are_classified_without_retry(self) -> None:
        output = self.root / "frame.jpg"
        for diagnostic, expected in (
            ("AVErrorApplicationIsNotAuthorizedToUseDevice (-11852)", "camera_permission_denied"),
            ("Video device not found", "camera_device_missing"),
            (
                "Selected framerate (29.970030) is not supported by the device.",
                "camera_parameters_unsupported",
            ),
            ("Unexpected driver failure FAKE-private-detail", "camera_capture_failed"),
        ):
            with (
                self.subTest(expected=expected),
                patch.object(self.vision.shutil, "which", return_value="/fake/imagesnap"),
                patch.object(
                    self.vision.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 1, b"", diagnostic.encode()),
                ) as run,
            ):
                result = self.vision.capture(output)
                self.assertEqual(result["error"], expected)
                self.assertFalse(result["retry"])
                run.assert_called_once()
                self.assertNotIn("FAKE-private-detail", json.dumps(result))
                self.assertFalse(output.exists())
                self.assertEqual(list(self.root.glob(".vision-*")), [])

    def test_ffmpeg_discovers_modes_then_captures_once(self) -> None:
        output = self.root / "frame.png"
        devices = (
            "[AVFoundation indev @ x] AVFoundation video devices:\n"
            "[AVFoundation indev @ x] [0] Capture screen 0\n"
            "[AVFoundation indev @ x] [1] MacBook Air的相机\n"
            "[AVFoundation indev @ x] [2] iPhone Camera\n"
            "[AVFoundation indev @ x] AVFoundation audio devices:\n"
            "[AVFoundation indev @ x] [0] Microphone\n"
        )
        modes = (
            "Selected video size is not supported by the device.\nSupported modes:\n"
            "  640x480@[15.000000 30.000000]fps\n"
            "  1280x720@[15.000000 30.000000]fps\n"
            "  1920x1080@[15.000000 30.000000]fps\n"
        )

        def run_fake(command: list[str], **kwargs: Any) -> Any:
            if "-list_devices" in command:
                # AVFoundation prints the device list at INFO, not ERROR level.
                self.assertEqual(command[command.index("-loglevel") + 1], "info")
                return subprocess.CompletedProcess(command, 251, b"", devices.encode())
            if command[-1] == "-":
                return subprocess.CompletedProcess(command, 251, b"", modes.encode())
            self.assertEqual(command[command.index("-i") + 1], "1:none")
            self.assertEqual(command[command.index("-framerate") + 1], "30")
            self.assertEqual(command[command.index("-video_size") + 1], "1280x720")
            self.assertEqual(command[command.index("-pixel_format") + 1], "nv12")
            self.assertLess(command.index("-framerate"), command.index("-i"))
            self.assertEqual(command[command.index("-ss") + 1], "1")
            self.assertGreater(command.index("-ss"), command.index("-i"))
            Path(command[-1]).write_bytes(self.data)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with (
            patch.object(self.vision.shutil, "which", side_effect=[None, "/fake/ffmpeg"]),
            patch.object(self.vision.subprocess, "run", side_effect=run_fake) as run,
        ):
            result = self.vision.capture(output)
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(result["capture_mode"]["video_size"], "1280x720")
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob(".vision-*")), [])

    def test_ffmpeg_stops_at_each_failed_stage_without_fallback(self) -> None:
        devices = (
            "AVFoundation video devices:\n[avf] [0] MacBook Air的相机\n"
            "AVFoundation audio devices:\n[avf] [0] microphone"
        ).encode()
        modes = b"Supported modes:\n  640x480@[15.000000 30.000000]fps\n"
        failures = (
            (b"not authorized", "camera_permission_denied"),
            (b"Video device not found", "camera_device_missing"),
            (b"Selected framerate is not supported by the device", "camera_parameters_unsupported"),
            (b"I/O error FAKE-sensitive", "camera_capture_failed"),
        )
        for stage in ("devices", "modes", "capture"):
            for diagnostic, error in failures:
                responses: list[subprocess.CompletedProcess[bytes]] = []
                if stage != "devices":
                    responses.append(subprocess.CompletedProcess([], 251, b"", devices))
                if stage == "capture":
                    responses.append(subprocess.CompletedProcess([], 251, b"", modes))
                responses.append(subprocess.CompletedProcess([], 1, b"", diagnostic))
                with (
                    self.subTest(stage=stage, error=error),
                    patch.object(self.vision.shutil, "which", side_effect=[None, "/fake/ffmpeg"]),
                    patch.object(self.vision.subprocess, "run", side_effect=responses) as run,
                ):
                    result = self.vision.capture(self.root / "failed.jpg")
                self.assertEqual(result["error"], error)
                self.assertEqual(result["stage"], stage)
                self.assertFalse(result["retry"])
                self.assertEqual(run.call_count, len(responses))
                self.assertNotIn("FAKE-sensitive", json.dumps(result))
                self.assertEqual(list(self.root.glob(".vision-*")), [])

    def test_ffmpeg_absent_camera_or_unparseable_modes_does_not_capture(self) -> None:
        devices = b"AVFoundation video devices:\n[avf] [1] Other Camera\n"
        for device, expected, expected_calls in (
            ("MacBook Air的相机", "camera_device_missing", 1),
            ("1", "camera_parameters_unsupported", 2),
        ):
            with (
                self.subTest(device=device),
                patch.object(self.vision.shutil, "which", side_effect=[None, "/fake/ffmpeg"]),
                patch.object(
                    self.vision.subprocess,
                    "run",
                    side_effect=[
                        subprocess.CompletedProcess([], 251, b"", devices),
                        subprocess.CompletedProcess([], 1, b"", b"Supported modes: (unknown)"),
                    ],
                ) as run,
            ):
                result = self.vision.capture(self.root / "bad.jpg", device=device)
            self.assertEqual(result["error"], expected)
            self.assertEqual(run.call_count, expected_calls)
            self.assertFalse(result["retry"])

    def test_ffmpeg_mode_selection_uses_advertised_non30_rate(self) -> None:
        modes = "Supported modes:\n  640x480@[29.970030 29.970030]fps\n"
        self.assertEqual(self.vision._camera_mode(modes), ("640x480", "29.97003"))
        self.assertEqual(
            self.vision._camera_mode("Supported modes:\n  640x480@[24.000000 25.000000]fps"),
            ("640x480", "25"),
        )

    def test_ffmpeg_timeout_at_each_stage_stops_and_cleans_up(self) -> None:
        devices = (
            "AVFoundation video devices:\n[avf] [0] MacBook Air的相机\n"
            "AVFoundation audio devices:\n[avf] [0] microphone"
        ).encode()
        modes = b"Supported modes:\n  640x480@[15.000000 30.000000]fps\n"
        for stage in ("devices", "modes", "capture"):
            responses: list[Any] = []
            if stage != "devices":
                responses.append(subprocess.CompletedProcess([], 251, b"", devices))
            if stage == "capture":
                responses.append(subprocess.CompletedProcess([], 251, b"", modes))
            responses.append(subprocess.TimeoutExpired("ffmpeg", 15))
            with (
                self.subTest(stage=stage),
                patch.object(self.vision.shutil, "which", side_effect=[None, "/fake/ffmpeg"]),
                patch.object(self.vision.subprocess, "run", side_effect=responses) as run,
            ):
                result = self.vision.capture(self.root / "timeout.jpg")
                self.assertEqual(result["error"], "camera_timeout_or_permission_prompt")
                self.assertEqual(result["stage"], stage)
                self.assertFalse(result["retry"])
                self.assertEqual(run.call_count, len(responses))
                budgets = [call.kwargs["timeout"] for call in run.call_args_list]
                self.assertEqual(budgets, sorted(budgets, reverse=True))
                self.assertLessEqual(budgets[0], 15)
                self.assertEqual(list(self.root.glob(".vision-*")), [])
                self.assertFalse((self.root / "timeout.jpg").exists())

    def test_capture_success_is_private_and_existing_output_is_preserved(self) -> None:
        output = self.root / "frame.png"

        def capture_fake(command: list[str], **kwargs: Any) -> Any:
            Path(command[-1]).write_bytes(self.data)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with (
            patch.object(self.vision.shutil, "which", return_value="/fake/imagesnap"),
            patch.object(self.vision.subprocess, "run", side_effect=capture_fake) as run,
        ):
            self.assertTrue(self.vision.capture(output)["ok"])
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertFalse(self.vision.capture(output)["ok"])
            run.assert_called_once()
        self.assertEqual(output.read_bytes(), self.data)

    def test_strict_json_contract_and_count_only_people(self) -> None:
        vision = load_vision()
        raw = {
            "summary": "白底上有一个红色圆形。",
            "objects": ["红色圆形"],
            "people_count": 0,
            "text_in_image": ["你好，世界"],
            "scene": "测试图",
        }
        result = vision.validate_description(json.dumps(raw))
        self.assertEqual(result, raw)
        for changed in (
            {**raw, "people_count": True},
            {**raw, "people_count": -1},
            {**raw, "faces": [{"name": "某人"}]},
            {**raw, "identity": "某人"},
            {**raw, "summary": "English only"},
            {**raw, "objects": "圆形"},
            {**raw, "text_in_image": [12]},
            {**raw, "scene": ""},
        ):
            with self.subTest(changed=changed), self.assertRaises(vision.VisionError):
                vision.validate_description(json.dumps(changed))
        for invalid in ("```json\n{}\n```", "[]", "not json", '{"summary":1,"summary":2}'):
            with self.subTest(invalid=invalid), self.assertRaises(vision.VisionError):
                vision.validate_description(invalid)
        self.assertIn("身份识别", vision.SYSTEM_PROMPT)
        self.assertIn("只计数", vision.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
