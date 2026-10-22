"""Offline tests; all generated audio and service state are disposable."""

from __future__ import annotations

import importlib.util
import json
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast
from unittest.mock import patch


class FakePlayer:
    def __init__(self, *, block: bool = False) -> None:
        self.paths: list[Path] = []
        self.started = threading.Event()
        self.block = block
        self.active = False
        self.stop_count = 0

    @property
    def playing(self) -> bool:
        return self.active

    def start(self, path: Path) -> None:
        self.paths.append(path)
        self.active = self.block
        self.started.set()

    def stop(self) -> None:
        self.active = False
        self.stop_count += 1


def wait_until(predicate: object, timeout: float = 2.0) -> None:
    if not callable(predicate):
        raise TypeError("predicate must be callable")
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition timed out")
        time.sleep(0.005)


class VoiceLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep every capture test offline; real enumeration has a separate process test.
        import voice_live

        self.enumerate_audio = voice_live.list_audio_devices
        listing = patch(
            "voice_live.list_audio_devices",
            return_value=(
                voice_live.AudioDevice("0", "AirPods Pro"),
                voice_live.AudioDevice("1", "MacBook Air麦克风"),
            ),
            create=True,
        )
        self.addCleanup(listing.stop)
        listing.start()

    def test_all_zero_capture_has_dedicated_error_with_device_and_no_respawn(self) -> None:
        import voice_live

        self.assertTrue(hasattr(voice_live, "ZeroAudioError"))
        processes: list[subprocess.Popen[bytes]] = []

        def factory(argv: list[str]) -> subprocess.Popen[bytes]:
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'\\0'*640*300)"],
                stdout=subprocess.PIPE,
            )
            processes.append(child)
            return child

        capture = voice_live.FFmpegCapture("fake", factory=factory)
        received: list[bytes] = []
        with self.assertRaises(voice_live.ZeroAudioError) as caught:
            received.extend(capture.frames())
        self.assertEqual(len(received), 249)  # 250th 20ms frame reaches five seconds.
        self.assertIn("MacBook Air麦克风", str(caught.exception))
        self.assertIn("index=1", str(caught.exception))
        self.assertIn("all-zero", str(caught.exception))
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_nonzero_samples_reset_zero_window_even_below_energy_threshold(self) -> None:
        from voice_live import FFmpegCapture

        def factory(argv: list[str]) -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import sys;sys.stdout.buffer.write((b'\\0'*640*4+b'\\1'+b'\\0'*639)*100)",
                ],
                stdout=subprocess.PIPE,
            )

        capture = FFmpegCapture("fake", factory=factory, zero_audio_seconds=0.10)
        self.assertEqual(len(list(capture.frames())), 500)

    def test_zero_audio_diagnostic_survives_run_session_logging(self) -> None:
        import voice_live

        self.assertTrue(hasattr(voice_live, "ZeroAudioError"))

        class ZeroCapture:
            def frames(self) -> Iterator[bytes]:
                raise voice_live.ZeroAudioError("all-zero: MacBook Air麦克风 index=1")
                yield b""

            def close(self) -> None:
                pass

        pipeline = voice_live.TurnPipeline(
            lambda p: "", lambda s: "", lambda s, p: iter([]), FakePlayer()
        )
        with self.assertLogs(level="ERROR") as logs:
            self.assertEqual(
                voice_live.run_session(ZeroCapture(), voice_live.LiveSession(pipeline)), 2
            )
        self.assertIn("all-zero", " ".join(logs.output))
        self.assertIn("MacBook Air麦克风", " ".join(logs.output))

    def test_player_override_uses_argv_placeholder_without_shell(self) -> None:
        import os

        from voice_live import AfplayPlayer

        with patch.dict(os.environ, {"JARVIS_VOICE_PLAYER": "ffmpeg -i {audio} -f audiotoolbox"}):
            player = AfplayPlayer()
        with patch("voice_live.subprocess.Popen") as spawn:
            player.start(Path("speech with spaces.wav"))
            self.assertEqual(
                spawn.call_args.args[0],
                ["ffmpeg", "-i", "speech with spaces.wav", "-f", "audiotoolbox"],
            )
            self.assertNotIn("shell", spawn.call_args.kwargs)
        with patch("voice_live.subprocess.Popen") as spawn:
            AfplayPlayer(command="custom-player --test").start(Path("speech.wav"))
            self.assertEqual(spawn.call_args.args[0], ["custom-player", "--test", "speech.wav"])
        with patch.dict(os.environ, {}, clear=True), patch("voice_live.subprocess.Popen") as spawn:
            AfplayPlayer().start(Path("speech.wav"))
            self.assertEqual(spawn.call_args.args[0], ["/usr/bin/afplay", "speech.wav"])

    def test_cli_reports_unmatched_device_without_opening_capture(self) -> None:
        import voice_live

        with (
            patch("sys.argv", ["voice_live", "--mode", "always", "--device", "missing"]),
            patch("voice_live.shutil.which", return_value="/fake/executable"),
            patch("voice_live._spawn_capture") as spawn,
            self.assertLogs(level="ERROR") as logs,
        ):
            self.assertEqual(voice_live.main(), 2)
        self.assertIn("no unique audio device name match", " ".join(logs.output))
        spawn.assert_not_called()

    def test_cli_player_override_and_device_status_are_wired(self) -> None:
        import voice_live

        with (
            patch(
                "sys.argv",
                [
                    "voice_live",
                    "--mode",
                    "always",
                    "--device",
                    "MacBook",
                    "--player",
                    "acceptance-player --speaker",
                ],
            ),
            patch("voice_live.shutil.which", return_value="/fake/executable"),
            patch("voice_live.run_session", return_value=0),
            patch("voice_live.AfplayPlayer") as player,
        ):
            self.assertEqual(voice_live.main(), 0)
            player.assert_called_once_with(command="acceptance-player --speaker")
        with (
            patch("sys.argv", ["voice_live", "--status", "--device", "MacBook"]),
            patch("voice_live.shutil.which", return_value="/fake/executable"),
            patch("builtins.print") as output,
            patch("voice_live._spawn_capture") as spawn,
        ):
            self.assertEqual(voice_live.main(), 0)
            status = json.loads(output.call_args.args[0])
            self.assertEqual(status["device_name"], "MacBook Air麦克风")
            self.assertEqual(status["device_index"], "1")
            spawn.assert_not_called()

    def test_enumerator_is_bounded_and_accepts_ffmpeg_diagnostic_exit(self) -> None:
        import voice_live

        listing = (
            "[AVFoundation indev @ 0x1] AVFoundation audio devices:\n"
            "[AVFoundation indev @ 0x1] [7] BuiltIn Microphone\n"
        )
        with patch("voice_live.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr=listing)
            self.assertEqual(self.enumerate_audio("ffmpeg")[0].index, "7")
            self.assertEqual(run.call_args.kwargs["timeout"], 5)
            self.assertIn("-list_devices", run.call_args.args[0])
            run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="bad")
            with self.assertRaises(voice_live.CaptureError):
                self.enumerate_audio("ffmpeg")
            run.side_effect = subprocess.TimeoutExpired("ffmpeg", 5)
            with self.assertRaises(voice_live.CaptureError):
                self.enumerate_audio("ffmpeg")

    def test_capture_resolves_name_into_argv_and_reports_selection(self) -> None:
        import voice_live

        capture = voice_live.FFmpegCapture("fake", "MACBOOK")
        self.assertTrue(hasattr(capture, "selected_device"))
        self.assertEqual(capture.selected_device.name, "MacBook Air麦克风")
        self.assertEqual(capture.status()["device_index"], "1")
        self.assertEqual(capture.status()["device_name"], "MacBook Air麦克风")
        calls: list[list[str]] = []

        def factory(argv: list[str]) -> subprocess.Popen[bytes]:
            calls.append(argv)
            return subprocess.Popen(
                [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'\\1'*640)"],
                stdout=subprocess.PIPE,
            )

        capture = voice_live.FFmpegCapture("fake", "MacBook", factory=factory)
        with self.assertLogs(level="INFO") as logs:
            self.assertEqual(len(list(capture.frames())), 1)
        self.assertIn(":1", calls[0])
        self.assertIn("MacBook Air麦克风", " ".join(logs.output))

    def test_device_names_resolve_uniquely_and_ignore_video_indices(self) -> None:
        import voice_live

        self.assertTrue(hasattr(voice_live, "parse_audio_devices"))
        listing = (
            "[AVFoundation indev @ 0x1] AVFoundation video devices:\n"
            "[AVFoundation indev @ 0x1] [0] FaceTime HD Camera\n"
            "[AVFoundation indev @ 0x1] AVFoundation audio devices:\n"
            "[AVFoundation indev @ 0x1] [0] Sober 的AirPods pro\n"
            "[AVFoundation indev @ 0x1] [1] MacBook Air麦克风\n"
            "[AVFoundation indev @ 0x1] [2] USB Microphone\n"
        )
        devices = voice_live.parse_audio_devices(listing)
        self.assertEqual([device.index for device in devices], ["0", "1", "2"])
        self.assertEqual(voice_live.select_audio_device(devices, "macbook AIR").index, "1")
        self.assertEqual(voice_live.select_audio_device(devices).name, "MacBook Air麦克风")
        for query in ["missing", "micro", "0"]:
            with (
                self.subTest(query=query),
                self.assertRaisesRegex(voice_live.CaptureError, "no unique.*match"),
            ):
                # 'micro' is ambiguous after adding a second USB input.
                pool = devices + (voice_live.AudioDevice("3", "USB Microphone two"),)
                voice_live.select_audio_device(pool, query)
        fallback = voice_live.select_audio_device(devices[:1])
        self.assertIsNone(fallback.index)
        self.assertEqual(fallback.selector, "default")
        with self.assertRaisesRegex(voice_live.CaptureError, "ambiguous built-in"):
            voice_live.select_audio_device(
                devices + (voice_live.AudioDevice("4", "BuiltIn Microphone"),)
            )

    def test_default_detects_quiet_loopback_speech_without_noise_triggers(self) -> None:
        from voice_live import EnergySegmenter, LiveConfig

        segmenter = EnergySegmenter(LiveConfig())
        noise = struct.pack("<320h", *([22] * 320))  # measured room RMS ~0.00066
        speech = struct.pack("<320h", *([328] * 320))  # loopback speech RMS ~0.010
        for _ in range(300):
            event = segmenter.feed(noise)
            self.assertFalse(event.started)
            self.assertIsNone(event.utterance)
        started = False
        for _ in range(50):
            started = segmenter.feed(speech).started or started
        utterance: bytes | None = None
        for _ in range(30):
            utterance = segmenter.feed(noise).utterance or utterance
        self.assertTrue(started, "default must detect the measured quiet loopback")
        self.assertIsNotNone(utterance)
        self.assertIn(speech * 50, utterance or b"")

    def test_noise_floor_and_threshold_follow_slowly_rising_room_noise(self) -> None:
        from voice_live import EnergySegmenter, LiveConfig

        segmenter = EnergySegmenter(LiveConfig())
        thresholds: list[float] = []
        for level in [22, 30, 50, 80, 110]:
            for _ in range(300):
                self.assertFalse(segmenter.feed(struct.pack("<320h", *([level] * 320))).started)
            status = segmenter.status()
            self.assertAlmostEqual(float(str(status["noise_floor"])), level / 32768)
            thresholds.append(float(str(status["energy_threshold"])))
        self.assertEqual(thresholds, sorted(thresholds))
        self.assertGreater(thresholds[-1], thresholds[0] * 3)

    def test_adaptive_playback_uses_live_base_and_excludes_echo_from_noise(self) -> None:
        from voice_live import EnergySegmenter, LiveConfig

        segmenter = EnergySegmenter(LiveConfig())
        for _ in range(300):
            segmenter.feed(struct.pack("<320h", *([22] * 320)))
        before = segmenter.status()
        raised = segmenter.status(playback=True)
        self.assertAlmostEqual(
            float(str(raised["energy_threshold"])),
            float(str(before["energy_threshold"])) * 3,
        )
        for _ in range(300):
            self.assertFalse(
                segmenter.feed(struct.pack("<320h", *([150] * 320)), playback=True).started
            )
        self.assertEqual(segmenter.status()["noise_floor"], before["noise_floor"])
        # The raised threshold remains in effect over the existing reverb tail.
        self.assertEqual(segmenter.status()["energy_threshold"], raised["energy_threshold"])
        for _ in range(11):
            segmenter.feed(b"\0" * 640)
        self.assertAlmostEqual(
            float(str(segmenter.status()["energy_threshold"])),
            float(str(before["energy_threshold"])),
        )

    def test_explicit_threshold_overrides_adaptation_without_hiding_noise(self) -> None:
        from voice_live import EnergySegmenter, LiveConfig

        segmenter = EnergySegmenter(LiveConfig(energy_threshold=0.02))
        for level in [22, 110]:
            for _ in range(300):
                segmenter.feed(struct.pack("<320h", *([level] * 320)))
            self.assertEqual(segmenter.status()["threshold_mode"], "fixed")
            self.assertEqual(segmenter.status()["energy_threshold"], 0.02)
            self.assertAlmostEqual(float(str(segmenter.status()["noise_floor"])), level / 32768)
        for _ in range(50):
            self.assertFalse(segmenter.feed(struct.pack("<320h", *([328] * 320))).started)

    def test_cli_status_distinguishes_cold_adaptive_from_explicit_fixed(self) -> None:
        import voice_live

        for flags, mode, threshold in [
            ([], "adaptive", 0.002),
            (["--energy-threshold", "0.007"], "fixed", 0.007),
        ]:
            with (
                patch("sys.argv", ["voice_live", "--status", *flags]),
                patch("voice_live.shutil.which", return_value="/fake/executable"),
                patch("builtins.print") as output,
                patch("voice_live._spawn_capture") as spawn,
            ):
                self.assertEqual(voice_live.main(), 0)
                status = json.loads(output.call_args.args[0])
                self.assertEqual(status["threshold_mode"], mode)
                self.assertIsNone(
                    status["noise_floor"], "no recording must not invent a measurement"
                )
                self.assertEqual(status["energy_threshold"], threshold)
                spawn.assert_not_called()

    def test_energy_segmenter_keeps_onset_and_closes_on_silence(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("voice_live"), "live microphone implementation is missing"
        )
        from voice_live import EnergySegmenter, LiveConfig

        segmenter = EnergySegmenter(LiveConfig())
        quiet = struct.pack("<320h", *([0] * 320))
        loud = struct.pack("<320h", *([5000] * 320))
        for _ in range(10):
            self.assertIsNone(segmenter.feed(quiet).utterance)
        started = False
        for _ in range(20):
            started = segmenter.feed(loud).started or started
        self.assertTrue(started)
        utterance: bytes | None = None
        for _ in range(30):
            utterance = segmenter.feed(quiet).utterance or utterance
        self.assertIsNotNone(utterance)
        self.assertIn(loud * 20, utterance or b"")

    def test_timeline_posts_voice_origin_and_attempt_identity(self) -> None:
        from voice_live import TimelineClient

        observed: list[dict[str, object]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                observed.append(
                    {
                        "path": self.path,
                        "origin": self.headers.get("Origin"),
                        "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                    }
                )
                body = json.dumps({"text": "model-result"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                self.assertEqual(TimelineClient(url).reply("asr-result"), "model-result")
            finally:
                server.shutdown()
                thread.join(2)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["path"], "/api/timeline/chat")
        self.assertEqual(observed[0]["origin"], url)
        payload = cast(dict[str, object], observed[0]["body"])
        self.assertEqual(payload["prompt"], "asr-result")
        self.assertEqual(payload["source_channel"], "voice")
        self.assertNotIn("session_id", payload)
        self.assertEqual(uuid.UUID(str(payload["attempt_id"])).version, 4)

    def test_timeline_rejects_nonlocal_urls(self) -> None:
        from voice_live import TimelineClient

        for url in ["https://example.com", "http://127.0.0.1:3/path", "http://user@localhost"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                TimelineClient(url)

    def test_playback_energy_suppression_and_sustained_barge_in(self) -> None:
        from voice_live import EnergySegmenter, LiveConfig

        segmenter = EnergySegmenter(LiveConfig(energy_threshold=0.02))
        echo = struct.pack("<320h", *([1200] * 320))  # fixed override idle/playback regression
        speech = struct.pack("<320h", *([8000] * 320))
        for _ in range(100):
            event = segmenter.feed(echo, playback=True)
            self.assertFalse(event.started)
            self.assertIsNone(event.utterance)
        for _ in range(5):
            self.assertFalse(segmenter.feed(speech, playback=True).started)
        self.assertTrue(segmenter.feed(speech, playback=True).started)

    def test_disarmed_push_mode_does_not_collect(self) -> None:
        from voice_live import EnergySegmenter, LiveConfig

        segmenter = EnergySegmenter(LiveConfig())
        loud = struct.pack("<320h", *([5000] * 320))
        for _ in range(100):
            self.assertFalse(segmenter.feed(loud, enabled=False).started)
        self.assertIsNone(segmenter.finish())

    def test_asr_reply_first_sentence_played_before_next_synthesis(self) -> None:
        from voice_live import TurnPipeline

        events: list[str] = []
        player = FakePlayer()

        def asr(path: Path) -> str:
            self.assertTrue(path.is_file())
            events.append("asr")
            return "recognized-input"

        def reply(text: str) -> str:
            self.assertEqual(text, "recognized-input")
            events.append("http")
            return "model-output"

        def tts(text: str, directory: Path) -> Iterator[Path]:
            self.assertEqual(text, "model-output")
            events.append("tts-first")
            yield directory / "one.wav"
            self.assertTrue(player.started.is_set(), "first sentence must already be playing")
            events.append("tts-second")
            yield directory / "two.wav"

        pipeline = TurnPipeline(asr, reply, tts, player)
        try:
            pipeline.submit(b"\0\0" * 320)
            wait_until(lambda: not pipeline.busy)
            self.assertEqual(len(player.paths), 2)
            self.assertEqual(events, ["asr", "http", "tts-first", "tts-second"])
            self.assertEqual(pipeline.errors, [])
            self.assertFalse(player.paths[0].parent.exists())
        finally:
            self.assertTrue(pipeline.close())

    def test_interrupt_stops_audio_discards_unsynthesized_sentences_and_accepts_new_turn(
        self,
    ) -> None:
        from voice_live import TurnPipeline

        player = FakePlayer(block=True)
        synthesized: list[str] = []
        request_count: list[int] = []

        def reply(text: str) -> str:
            request_count.append(1)
            return f"reply-{len(request_count)}"

        def tts(text: str, directory: Path) -> Iterator[Path]:
            synthesized.append(text + "-first")
            yield directory / "one.wav"
            synthesized.append(text + "-second")
            yield directory / "two.wav"

        pipeline = TurnPipeline(lambda path: "input", reply, tts, player)
        try:
            pipeline.submit(b"\0\0" * 320)
            self.assertTrue(player.started.wait(2))
            pipeline.interrupt()
            wait_until(lambda: not pipeline.busy)
            self.assertGreaterEqual(player.stop_count, 1)
            self.assertEqual(synthesized, ["reply-1-first"])
            self.assertEqual(len(player.paths), 1)
            player.block = False
            pipeline.submit(b"\0\0" * 320)
            wait_until(lambda: len(request_count) == 2 and not pipeline.busy)
            self.assertEqual(synthesized, ["reply-1-first", "reply-2-first", "reply-2-second"])
            self.assertEqual(pipeline.errors, [])
        finally:
            self.assertTrue(pipeline.close())

    def test_late_model_result_cannot_play_after_interruption(self) -> None:
        from voice_live import TurnPipeline

        waiting, release = threading.Event(), threading.Event()
        player = FakePlayer()

        def reply(text: str) -> str:
            waiting.set()
            release.wait(2)
            return "discard-this"

        pipeline = TurnPipeline(
            lambda path: "input",
            reply,
            lambda text, directory: iter([directory / "one.wav"]),
            player,
        )
        try:
            pipeline.submit(b"\0\0" * 320)
            self.assertTrue(waiting.wait(2))
            pipeline.interrupt()
            release.set()
            wait_until(lambda: not pipeline.busy)
            self.assertEqual(player.paths, [])
        finally:
            release.set()
            self.assertTrue(pipeline.close())

    def test_pipeline_failures_are_logged_not_spoken_and_not_retried(self) -> None:
        from voice_live import TurnPipeline

        calls: list[int] = []
        player = FakePlayer()

        def denied(path: Path) -> str:
            calls.append(1)
            raise PermissionError("secret payload must not enter logs")

        pipeline = TurnPipeline(denied, lambda text: "no", lambda text, path: iter([]), player)
        try:
            pipeline.submit(b"\0\0" * 320)
            wait_until(lambda: not pipeline.busy)
            self.assertEqual(len(calls), 1)
            self.assertEqual(player.paths, [])
            self.assertEqual(len(pipeline.errors), 1)
            self.assertNotIn("secret", pipeline.errors[0])
        finally:
            self.assertTrue(pipeline.close())

    def test_permission_denial_exits_once_and_closes_capture(self) -> None:
        from voice_live import CaptureError, LiveSession, TurnPipeline, run_session

        attempts: list[int] = []
        closed: list[int] = []

        class DeniedCapture:
            def frames(self) -> Iterator[bytes]:
                attempts.append(1)
                raise CaptureError("microphone permission denied")
                yield b""  # generator body intentionally raises before its first frame

            def close(self) -> None:
                closed.append(1)

        player = FakePlayer()
        pipeline = TurnPipeline(lambda p: "", lambda s: "", lambda s, p: iter([]), player)
        session = LiveSession(pipeline)
        self.assertEqual(run_session(DeniedCapture(), session), 2)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(closed), 1)
        self.assertEqual(player.paths, [])

    def test_ffmpeg_capture_stall_stops_without_respawn(self) -> None:
        from voice_live import CaptureError, FFmpegCapture

        processes: list[subprocess.Popen[bytes]] = []

        def factory(argv: list[str]) -> subprocess.Popen[bytes]:
            process = subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            processes.append(process)
            return process

        capture = FFmpegCapture("fake-ffmpeg", startup_timeout=0.05, factory=factory)
        with self.assertRaises(CaptureError):
            list(capture.frames())
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        capture.close()

    def test_ffmpeg_capture_continuous_frames_and_final_cleanup(self) -> None:
        from voice_live import FFmpegCapture

        processes: list[subprocess.Popen[bytes]] = []

        def factory(argv: list[str]) -> subprocess.Popen[bytes]:
            self.assertIn("avfoundation", argv)
            self.assertIn(":1", argv)
            process = subprocess.Popen(
                [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'\\0'*640*3)"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            processes.append(process)
            return process

        capture = FFmpegCapture("fake", factory=factory)
        try:
            frames = list(capture.frames())
            self.assertEqual([len(f) for f in frames], [640, 640, 640])
        finally:
            capture.close()
        self.assertIsNotNone(processes[0].poll())

    def test_push_mode_and_always_mode_session_feed(self) -> None:
        from voice_live import LiveSession, TurnPipeline

        player = FakePlayer()
        calls: list[int] = []

        def asr(path: Path) -> str:
            calls.append(1)
            return ""

        pipeline = TurnPipeline(asr, lambda s: "", lambda s, p: iter([]), player)
        session = LiveSession(pipeline)
        loud, quiet = struct.pack("<320h", *([5000] * 320)), b"\0" * 640
        try:
            for _ in range(20):
                session.feed(loud)
            for _ in range(30):
                session.feed(quiet)
            self.assertEqual(calls, [])
            session.toggle_push()
            for _ in range(20):
                session.feed(loud)
            for _ in range(30):
                session.feed(quiet)
            wait_until(lambda: len(calls) == 1 and not pipeline.busy)
            self.assertFalse(session.armed)
            always = LiveSession(pipeline, mode="always")
            for _ in range(20):
                always.feed(loud)
            for _ in range(30):
                always.feed(quiet)
            wait_until(lambda: len(calls) == 2 and not pipeline.busy)
            self.assertTrue(always.armed)
        finally:
            self.assertTrue(pipeline.close())

    def test_session_speech_onset_interrupts_and_preserves_new_utterance(self) -> None:
        from voice_live import LiveSession, TurnPipeline

        player = FakePlayer(block=True)
        calls: list[int] = []

        def asr(path: Path) -> str:
            calls.append(1)
            return "input"

        pipeline = TurnPipeline(
            asr, lambda s: "model", lambda s, p: iter([p / "one.wav", p / "two.wav"]), player
        )
        session = LiveSession(pipeline)
        try:
            pipeline.submit(b"\0\0" * 320)
            self.assertTrue(player.started.wait(2))
            loud = struct.pack("<320h", *([9000] * 320))
            for _ in range(20):
                session.feed(loud)
            self.assertFalse(player.playing)
            player.block = False
            for _ in range(30):
                session.feed(b"\0" * 640)
            wait_until(lambda: len(calls) == 2 and not pipeline.busy)
            self.assertEqual(len(player.paths), 3)  # first old sentence, two new ones
        finally:
            self.assertTrue(pipeline.close())

    def test_noise_onset_while_model_generates_does_not_cancel_reply(self) -> None:
        # Real loopback acceptance: an onset during the model wait cancelled the turn,
        # so no reply was ever played. A non-utterance noise must not do that.
        from voice_live import LiveSession, TurnPipeline

        player = FakePlayer()
        model_entered = threading.Event()
        release_model = threading.Event()

        def reply(text: str) -> str:
            model_entered.set()
            release_model.wait(5)
            return "model"

        pipeline = TurnPipeline(
            lambda path: "input", reply, lambda s, p: iter([p / "one.wav"]), player
        )
        # Always-on is the mode where the real acceptance hit this (push mode is unarmed).
        session = LiveSession(pipeline, mode="always")
        try:
            pipeline.submit(b"\0\0" * 320)
            self.assertTrue(model_entered.wait(2))
            loud = struct.pack("<320h", *([9000] * 320))
            for _ in range(8):  # 0.16 s: passes onset, shorter than min_speech_seconds
                session.feed(loud)
            for _ in range(40):
                session.feed(b"\0" * 640)
            release_model.set()
            wait_until(lambda: not pipeline.busy)
            self.assertEqual(len(player.paths), 1, "reply must still play after a noise")
        finally:
            release_model.set()
            self.assertTrue(pipeline.close())

    def test_silent_successful_capture_is_not_a_permission_pass(self) -> None:
        from voice_live import CaptureError, FFmpegCapture

        def factory(argv: list[str]) -> subprocess.Popen[bytes]:
            return subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.PIPE)

        capture = FFmpegCapture("fake", factory=factory)
        with self.assertRaises(CaptureError):
            list(capture.frames())

    def test_pre_onset_click_is_not_misclassified_as_speech(self) -> None:
        from voice_live import EnergySegmenter, LiveConfig

        segmenter = EnergySegmenter(LiveConfig())
        loud, quiet = struct.pack("<320h", *([10000] * 320)), b"\0" * 640
        self.assertFalse(segmenter.feed(loud).started)
        for _ in range(60):
            self.assertIsNone(segmenter.feed(quiet).utterance)
        self.assertIsNone(segmenter.finish())

    def test_runtime_uses_existing_voice_functions_without_substituted_lines(self) -> None:
        from voice_live import local_speech, local_transcribe

        import voice

        with tempfile.TemporaryDirectory(prefix="jarvis-voice-adapter-") as folder:
            root = Path(folder)
            transcription: dict[str, object] = {
                "text": "recognized",
                "language": "zh",
                "segments": [],
            }
            chunk = voice.SpeechChunk(
                path=str(root / "one.wav"),
                duration=1.0,
                index=0,
                start=0.0,
                end=1.0,
                backend="fake",
                ready_latency_seconds=0.0,
                synthesis_latency_seconds=0.0,
            )
            with patch("voice.transcribe", return_value=transcription) as transcribe:
                self.assertEqual(local_transcribe(root / "input.wav"), "recognized")
                transcribe.assert_called_once_with(root / "input.wav")
            with patch("voice.speak_stream", return_value=iter([chunk])) as speak:
                self.assertEqual(list(local_speech("model reply", root)), [root / "one.wav"])
                speak.assert_called_once_with("model reply", root)

    def test_fake_audio_to_http_to_sentence_playback_end_to_end(self) -> None:
        import wave

        from voice_live import LiveSession, TimelineClient, TurnPipeline

        rows: list[dict[str, object]] = []
        failures: list[BaseException] = []
        player = FakePlayer()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                try:
                    payload: object = json.loads(
                        self.rfile.read(int(self.headers["Content-Length"]))
                    )
                    if not isinstance(payload, dict):
                        raise TypeError("expected object")
                    rows.append(cast(dict[str, object], payload))
                    response = json.dumps({"text": "reply-from-http"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                except BaseException as exc:
                    failures.append(exc)
                    self.send_error(500)

            def log_message(self, format: str, *args: object) -> None:
                pass

        def asr(path: Path) -> str:
            with wave.open(str(path)) as audio:
                self.assertEqual((audio.getframerate(), audio.getnchannels()), (16000, 1))
                self.assertGreater(audio.getnframes(), 320 * 20)
            return "recognized-through-asr"

        def tts(text: str, directory: Path) -> Iterator[Path]:
            self.assertEqual(text, "reply-from-http")
            directory.mkdir()
            output = directory / "sentence.wav"
            output.write_bytes(b"fake-audio-not-for-playback")
            yield output

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            pipeline = TurnPipeline(
                asr, TimelineClient(f"http://127.0.0.1:{server.server_port}").reply, tts, player
            )
            session = LiveSession(pipeline, mode="always")
            try:
                loud = struct.pack("<320h", *([5000] * 320))
                for frame in [b"\0" * 640] * 10 + [loud] * 20 + [b"\0" * 640] * 30:
                    session.feed(frame)
                wait_until(lambda: len(player.paths) == 1 and not pipeline.busy)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["source_channel"], "voice")
                self.assertEqual(rows[0]["prompt"], "recognized-through-asr")
                self.assertEqual(pipeline.errors, [])
                self.assertEqual(failures, [])
                self.assertFalse(player.paths[0].parent.parent.exists())
            finally:
                self.assertTrue(session.close())
                server.shutdown()
                thread.join(2)

    def test_main_push_mode_without_tty_does_not_open_microphone(self) -> None:
        from voice_live import main

        with (
            patch("sys.argv", ["voice_live"]),
            patch("sys.stdin.isatty", return_value=False),
            patch("voice_live.FFmpegCapture") as capture,
            self.assertLogs(level="ERROR") as logs,
        ):
            self.assertEqual(main(), 2)
            capture.assert_not_called()
        self.assertIn("interactive terminal", " ".join(logs.output))

    def test_rejected_short_utterance_disarms_push_mode(self) -> None:
        from voice_live import LiveSession, TurnPipeline

        player = FakePlayer()
        calls: list[int] = []

        def asr(path: Path) -> str:
            calls.append(1)
            return ""

        pipeline = TurnPipeline(asr, lambda s: "", lambda s, p: iter([]), player)
        session = LiveSession(pipeline)
        loud = struct.pack("<320h", *([5000] * 320))
        try:
            session.toggle_push()
            for _ in range(6):
                session.feed(loud)
            for _ in range(30):
                session.feed(b"\0" * 640)
            self.assertFalse(session.armed, "rejected noise must consume one push arming")
            for _ in range(20):
                session.feed(loud)
            for _ in range(30):
                session.feed(b"\0" * 640)
            self.assertFalse(pipeline.busy)
            self.assertEqual(calls, [])
        finally:
            self.assertTrue(session.close())

    def test_actual_web_service_retains_voice_in_single_timeline(self) -> None:
        import os
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer

        import web_server
        from voice_live import TimelineClient

        failures: list[BaseException] = []

        class Provider(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                try:
                    self.rfile.read(int(self.headers["Content-Length"]))
                    payload = json.dumps(
                        {
                            "choices": [
                                {"message": {"role": "assistant", "content": "FAKE-VOICE-REPLY"}}
                            ]
                        }
                    )
                    body = payload.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except BaseException as exc:
                    failures.append(exc)
                    self.send_error(500)

            def log_message(self, format: str, *args: object) -> None:
                pass

        with (
            ThreadingHTTPServer(("127.0.0.1", 0), Provider) as provider,
            tempfile.TemporaryDirectory(prefix="jarvis-voice-web-", dir=Path.home()) as folder,
        ):
            provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
            provider_thread.start()
            self.addCleanup(provider.shutdown)
            root = Path(folder)
            config = root / "fixture.json"
            config.write_text(
                json.dumps(
                    {
                        "provider": "custom",
                        "base_url": f"http://127.0.0.1:{provider.server_port}/v1",
                        "model": "FAKE-VOICE",
                        "context_length": 256000,
                        "reasoning_effort": "low",
                        "api_mode": "chat_completions",
                    }
                )
            )
            config.chmod(0o600)
            read_fd, write_fd = os.pipe()
            os.write(write_fd, b"FAKE-voice-live-offline")
            os.close(write_fd)
            try:
                server = web_server.create_server(
                    config_file=config,
                    key_fd=read_fd,
                    data_dir=root / "data",
                    test_local_port=provider.server_port,
                )
            finally:
                os.close(read_fd)
            server_thread = threading.Thread(target=server.serve_forever)
            server_thread.start()
            try:
                self.assertEqual(
                    TimelineClient(server.origin).reply("fixture-input"), "FAKE-VOICE-REPLY"
                )
                client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    client.request("GET", "/api/timeline")
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    raw: object = json.loads(response.read())
                finally:
                    client.close()
                self.assertIsInstance(raw, dict)
                entries = cast(dict[str, object], raw)["entries"]
                self.assertIsInstance(entries, list)
                rows = cast(list[dict[str, object]], entries)
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["source_channel"], "voice")
                self.assertEqual(rows[0]["content"], "fixture-input")
                self.assertEqual(rows[1]["source_channel"], "voice")
                self.assertEqual(rows[1]["content"], "FAKE-VOICE-REPLY")
                self.assertEqual(failures, [])
            finally:
                self.assertTrue(server.stop(timeout=10))
                server_thread.join(2)
                provider.shutdown()
                provider_thread.join(2)

    def test_cancel_during_tts_never_starts_stale_audio(self) -> None:
        from voice_live import TurnPipeline

        waiting, release = threading.Event(), threading.Event()
        player = FakePlayer()

        def tts(text: str, directory: Path) -> Iterator[Path]:
            waiting.set()
            release.wait(2)
            yield directory / "cancelled.wav"
            self.fail("cancelled stream must never synthesize a second sentence")

        pipeline = TurnPipeline(lambda p: "input", lambda s: "reply", tts, player)
        try:
            pipeline.submit(b"\0\0" * 320)
            self.assertTrue(waiting.wait(2))
            pipeline.interrupt()
            release.set()
            wait_until(lambda: not pipeline.busy)
            self.assertEqual(player.paths, [])
            self.assertEqual(pipeline.errors, [])
        finally:
            release.set()
            self.assertTrue(pipeline.close())

    def test_afplay_stop_terminates_and_reaps_single_process(self) -> None:
        from voice_live import AfplayPlayer

        processes: list[subprocess.Popen[bytes]] = []
        original = subprocess.Popen[bytes]

        def spawn(argv: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
            self.assertEqual(argv, ["fake-afplay", "fake.wav"])
            child = original(
                [sys.executable, "-c", "import time;time.sleep(60)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(child)
            return child

        player = AfplayPlayer("fake-afplay")
        with patch("voice_live.subprocess.Popen", side_effect=spawn):
            try:
                player.start(Path("fake.wav"))
                self.assertTrue(player.playing)
                player.stop()
                self.assertFalse(player.playing)
                self.assertEqual(len(processes), 1)
                self.assertIsNotNone(processes[0].poll())
            finally:
                player.stop()

    def test_http_uncertain_outcome_is_never_retried_or_spoken(self) -> None:
        from voice_live import LiveError, TimelineClient

        requests: list[int] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                requests.append(1)
                self.rfile.read(int(self.headers["Content-Length"]))
                body = b'{"state":"unknown"}'
                self.send_response(202)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                with self.assertRaises(LiveError):
                    TimelineClient(f"http://127.0.0.1:{server.server_port}").reply("input")
                self.assertEqual(requests, [1])
            finally:
                server.shutdown()
                worker.join(2)

    def test_max_utterance_bounds_buffer_and_echo_tail(self) -> None:
        from voice_live import EnergySegmenter, LiveConfig

        segmenter = EnergySegmenter(LiveConfig(max_utterance_seconds=0.30))
        loud, echo = struct.pack("<320h", *([8000] * 320)), struct.pack("<320h", *([1200] * 320))
        emitted: bytes | None = None
        for _ in range(15):
            emitted = segmenter.feed(loud).utterance or emitted
        self.assertIsNotNone(emitted)
        self.assertLessEqual(len(emitted or b""), 15 * 640)
        segmenter = EnergySegmenter(LiveConfig(energy_threshold=0.02))
        segmenter.feed(echo, playback=True)
        for _ in range(8):
            self.assertFalse(segmenter.feed(echo).started)
        started = False
        for _ in range(20):
            started = segmenter.feed(echo).started or started
        self.assertTrue(started, "same energy becomes audible after playback tail expires")

    def test_close_cleans_private_audio_even_with_blocked_cancelled_asr(self) -> None:
        from voice_live import TurnPipeline

        waiting, release = threading.Event(), threading.Event()
        recordings: list[Path] = []

        def asr(path: Path) -> str:
            recordings.append(path)
            waiting.set()
            release.wait(2)
            return "late"

        player = FakePlayer()
        pipeline = TurnPipeline(asr, lambda s: "no", lambda s, p: iter([]), player)
        try:
            pipeline.submit(b"\0\0" * 320)
            self.assertTrue(waiting.wait(2))
            self.assertFalse(pipeline.close(timeout=0.01))
            self.assertFalse(recordings[0].exists(), "close must not retain private recording")
        finally:
            release.set()
            self.assertTrue(pipeline.close())
        self.assertEqual(player.paths, [])

    def test_speech_end_timestamp_includes_vad_silence_wait(self) -> None:
        from voice_live import LiveSession, TurnPipeline

        player = FakePlayer()
        pipeline = TurnPipeline(lambda p: "", lambda s: "", lambda s, p: iter([]), player)
        session = LiveSession(pipeline, mode="always")
        try:
            with (
                patch.object(pipeline, "submit") as submit,
                patch("voice_live.time.monotonic", return_value=10.0),
            ):
                for _ in range(20):
                    session.feed(struct.pack("<320h", *([5000] * 320)))
                for _ in range(30):
                    session.feed(b"\0" * 640)
                submit.assert_called_once()
                self.assertAlmostEqual(submit.call_args.kwargs["ended_at"], 9.4)
        finally:
            self.assertTrue(session.close())

    def test_http_redirect_does_not_forward_transcript(self) -> None:
        from voice_live import LiveError, TimelineClient

        paths: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                paths.append(self.path)
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(307)
                self.send_header("Location", "http://127.0.0.1:9/leak")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                with self.assertRaises(LiveError):
                    TimelineClient(f"http://127.0.0.1:{server.server_port}").reply("private-input")
                self.assertEqual(paths, ["/api/timeline/chat"])
            finally:
                server.shutdown()
                worker.join(2)

    def test_config_validation_and_empty_asr_do_not_trigger_http(self) -> None:
        from voice_live import LiveConfig, TurnPipeline

        for kwargs in [
            {"energy_threshold": float("nan")},
            {"playback_multiplier": 0.5},
            {"max_utterance_seconds": 60.0},
            {"onset_seconds": 1.0},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                LiveConfig(**kwargs)
        calls: list[str] = []

        def reply(text: str) -> str:
            calls.append(text)
            return "not reached"

        player = FakePlayer()
        pipeline = TurnPipeline(lambda p: "   ", reply, lambda s, p: iter([]), player)
        try:
            pipeline.submit(b"\0\0" * 320)
            wait_until(lambda: not pipeline.busy)
            self.assertEqual(calls, [])
            self.assertEqual(player.paths, [])
        finally:
            self.assertTrue(pipeline.close())

    def test_push_cli_exits_after_capture_failure_without_stdin_thread_leak(self) -> None:
        import os
        import pty
        import select

        with tempfile.TemporaryDirectory(prefix="jarvis-voice-cli-") as folder:
            fake = Path(folder) / "fake-ffmpeg"
            fake.write_text("#!/bin/sh\nexit 1\n")
            fake.chmod(0o700)
            primary, secondary = pty.openpty()
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(Path(__file__).parent.parent / "voice_live.py"),
                    "--ffmpeg",
                    str(fake),
                ],
                stdin=secondary,
                stdout=secondary,
                stderr=secondary,
            )
            os.close(secondary)
            output = bytearray()
            try:
                self.assertEqual(process.wait(timeout=5), 2)
                while select.select([primary], [], [], 0.05)[0]:
                    try:
                        chunk = os.read(primary, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(2)
                os.close(primary)
            self.assertNotIn(b"Fatal Python error", output)


class SelfEvaluationTests(unittest.TestCase):
    def test_self_evaluate_is_owned_json_report_without_live_io(self) -> None:
        import inspect
        from datetime import UTC, datetime

        import voice_live

        hook = voice_live.self_evaluate
        self.assertTrue(callable(hook), "voice_live must own a real self_evaluate")
        assert callable(hook)
        self.assertEqual(inspect.getsourcefile(hook), voice_live.__file__)
        before = datetime.now(UTC)
        with (
            patch.object(
                voice_live, "list_audio_devices", side_effect=AssertionError("no devices")
            ),
            patch.object(voice_live, "FFmpegCapture", side_effect=AssertionError("no capture")),
            patch.object(voice_live, "AfplayPlayer", side_effect=AssertionError("no player")),
            patch.object(voice_live, "TimelineClient", side_effect=AssertionError("no HTTP")),
            patch.object(voice_live.subprocess, "run", side_effect=AssertionError("no process")),
            patch.object(
                voice_live.threading.Thread, "start", side_effect=AssertionError("no worker")
            ),
            patch.object(Path, "open", side_effect=AssertionError("no file read/write")),
        ):
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
        self.assertEqual(report["module"], "voice_live")
        for key in ("domain", "current_version"):
            self.assertIsInstance(report[key], str)
            self.assertTrue(report[key].strip())
        for key in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[key], list)
            self.assertTrue(report[key])
            self.assertTrue(all(isinstance(value, str) and value.strip() for value in report[key]))
        timestamp = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertEqual(timestamp.utcoffset(), UTC.utcoffset(timestamp))
        self.assertLessEqual(before, timestamp)
        self.assertLessEqual(timestamp, after)
        json.dumps(report, allow_nan=False)

    def test_self_evaluate_names_real_cascade_limits(self) -> None:
        import voice_live

        hook = voice_live.self_evaluate
        self.assertTrue(callable(hook), "voice_live must own a real self_evaluate")
        assert callable(hook)
        limits = " ".join(hook()["known_limitations"])
        for term in ("AVFoundation", "RMS", "echo", "blocking", "push", "timeline"):
            self.assertIn(term, limits)

    def test_self_evaluate_returns_fresh_nested_lists(self) -> None:
        import voice_live

        hook = voice_live.self_evaluate
        self.assertTrue(callable(hook), "voice_live must own a real self_evaluate")
        assert callable(hook)
        first = hook()
        expected = {
            key: list(first[key]) for key in ("known_limitations", "improvement_directions")
        }
        for key in expected:
            first[key].append("synthetic mutation")
        second = hook()
        for key in expected:
            self.assertEqual(second[key], expected[key])


class TimelineSyntheticTests(unittest.TestCase):
    def test_request_envelope_without_opening_socket(self) -> None:
        from unittest.mock import MagicMock

        from voice_live import TimelineClient

        client = TimelineClient("http://127.0.0.1:8765")
        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b'{"text":"synthetic reply"}'
        with patch.object(client._opener, "open", return_value=response) as opened:
            self.assertEqual(client.reply("synthetic input"), "synthetic reply")
        request = opened.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["prompt"], "synthetic input")
        self.assertEqual(payload["source_channel"], "voice")
        self.assertEqual(uuid.UUID(payload["attempt_id"]).version, 4)
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/timeline/chat")
        opened.assert_called_once()

    def test_uncertain_response_is_not_retried_without_opening_socket(self) -> None:
        from unittest.mock import MagicMock

        from voice_live import LiveError, TimelineClient

        client = TimelineClient("http://localhost:8765")
        response = MagicMock()
        response.__enter__.return_value.status = 202
        with patch.object(client._opener, "open", return_value=response) as opened:
            with self.assertRaisesRegex(LiveError, "uncertain; no retry"):
                client.reply("synthetic input")
        opened.assert_called_once()


if __name__ == "__main__":
    unittest.main()
