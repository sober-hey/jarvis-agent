"""Contract tests for the parked full-duplex voice port (offline, fake backend)."""

from __future__ import annotations

import threading
import time
import unittest
from collections.abc import Callable

from voice_port import (
    BrainVoiceSink,
    NativeModeUnavailable,
    SpeakPriority,
    VoiceContext,
    VoiceMode,
)


class FakeBackend:
    def __init__(self, mode: VoiceMode = VoiceMode.BRAIN) -> None:
        self._mode = mode
        self.spoken: list[tuple[str, SpeakPriority]] = []
        self.spoke = threading.Event()

    @property
    def mode(self) -> VoiceMode:
        return self._mode

    def start(self, context: VoiceContext, sink: object) -> None:
        return None

    def update_context(self, context: VoiceContext) -> None:
        return None

    def speak(self, text: str, priority: SpeakPriority = SpeakPriority.REPLY) -> None:
        self.spoken.append((text, priority))
        self.spoke.set()

    def interrupt(self) -> None:
        return None

    def close(self) -> None:
        return None


class VoicePortTests(unittest.TestCase):
    def test_final_transcript_goes_to_timeline_then_backend_speaks(self) -> None:
        backend = FakeBackend()
        heard: list[str] = []
        sink = BrainVoiceSink(backend, lambda text: heard.append(text) or "reply")
        sink.on_user_transcript("partial", final=False)
        sink.on_user_transcript("final text", final=True)
        self.assertTrue(backend.spoke.wait(2))
        self.assertEqual(heard, ["final text"])
        self.assertEqual(backend.spoken, [("reply", SpeakPriority.REPLY)])

    def test_stale_reply_is_dropped_after_interruption(self) -> None:
        backend = FakeBackend()
        release = threading.Event()

        def slow(text: str) -> str:
            release.wait(2)
            return "stale"

        sink = BrainVoiceSink(backend, slow)
        sink.on_user_transcript("first", final=True)
        sink.on_interrupted()
        release.set()
        time.sleep(0.2)
        self.assertEqual(backend.spoken, [])

    def test_reply_failure_is_reported_never_spoken(self) -> None:
        backend = FakeBackend()
        errors: list[str] = []

        def boom(text: str) -> str:
            raise OSError("down")

        sink = BrainVoiceSink(backend, boom, on_error=errors.append)
        sink.on_user_transcript("x", final=True)
        deadline = time.monotonic() + 2
        while not errors and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(errors), 1)
        self.assertEqual(backend.spoken, [])

    def test_native_backend_is_refused_until_record_endpoint_exists(self) -> None:
        with self.assertRaises(NativeModeUnavailable):
            BrainVoiceSink(FakeBackend(VoiceMode.NATIVE), lambda t: "r")

    def test_context_requires_generated_bounded_instructions(self) -> None:
        with self.assertRaises(ValueError):
            VoiceContext(instructions="  ")
        with self.assertRaises(ValueError):
            VoiceContext(instructions="x" * 16001)

    def test_audio_level_is_clamped_for_avatar(self) -> None:
        levels: list[tuple[str, float]] = []
        sink = BrainVoiceSink(
            FakeBackend(), lambda t: "r", on_level=lambda s, v: levels.append((s, v))
        )
        sink.on_audio_level("assistant", 3.0)
        sink.on_audio_level("user", -1.0)
        self.assertEqual(levels, [("assistant", 1.0), ("user", 0.0)])


class SelfEvaluationTests(unittest.TestCase):
    def test_self_evaluate_is_owned_json_report_without_worker_or_file_access(self) -> None:
        import inspect
        import json
        from datetime import UTC, datetime
        from pathlib import Path
        from unittest.mock import patch

        import voice_port

        hook = voice_port.self_evaluate
        self.assertTrue(callable(hook), "voice_port must own a real self_evaluate")
        assert callable(hook)
        self.assertEqual(inspect.getsourcefile(hook), voice_port.__file__)
        before = datetime.now(UTC)
        with (
            patch.object(
                voice_port.threading.Thread, "start", side_effect=AssertionError("no worker")
            ),
            patch.object(voice_port, "BrainVoiceSink", side_effect=AssertionError("no backend")),
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
        self.assertEqual(report["module"], "voice_port")
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

    def test_self_evaluate_names_real_unconnected_duplex_limits(self) -> None:
        import voice_port

        hook = voice_port.self_evaluate
        self.assertTrue(callable(hook), "voice_port must own a real self_evaluate")
        assert callable(hook)
        limits = " ".join(hook()["known_limitations"])
        for term in ("NATIVE", "record-only", "adapter", "thread", "injected", "speak"):
            self.assertIn(term, limits)

    def test_self_evaluate_returns_fresh_nested_lists(self) -> None:
        import voice_port

        hook = voice_port.self_evaluate
        self.assertTrue(callable(hook), "voice_port must own a real self_evaluate")
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


class VoicePortSyntheticTests(unittest.TestCase):
    def test_blank_and_partial_transcripts_never_start_reply_worker(self) -> None:
        from unittest.mock import patch

        sink = BrainVoiceSink(FakeBackend(), lambda text: self.fail("no timeline call"))
        with patch("voice_port.threading.Thread") as worker:
            sink.on_user_transcript("partial", final=False)
            sink.on_user_transcript(" \n ", final=True)
        worker.assert_not_called()

    def test_later_final_transcript_supersedes_blocked_reply(self) -> None:
        from unittest.mock import patch

        import voice_port

        backend = FakeBackend()
        entered, release = threading.Event(), threading.Event()
        workers: list[threading.Thread] = []
        real_thread = threading.Thread

        def factory(
            *, target: Callable[..., object], args: tuple[object, ...], daemon: bool
        ) -> threading.Thread:
            worker = real_thread(target=target, args=args, daemon=daemon)
            workers.append(worker)
            return worker

        def reply(text: str) -> str:
            if text == "old":
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("synthetic test release")
            return text + "-reply"

        sink = BrainVoiceSink(backend, reply)
        try:
            with patch.object(voice_port.threading, "Thread", side_effect=factory):
                sink.on_user_transcript("old", final=True)
                self.assertTrue(entered.wait(2))
                sink.on_user_transcript("new", final=True)
                self.assertTrue(backend.spoke.wait(2))
        finally:
            release.set()
            for worker in workers:
                worker.join(2)
                self.assertFalse(worker.is_alive())
        self.assertEqual(backend.spoken, [("new-reply", SpeakPriority.REPLY)])


if __name__ == "__main__":
    unittest.main()
