"""File-only voice contract tests; every artifact lives in a temporary directory."""

from __future__ import annotations

import importlib.util
import shutil
import sys
import unittest
import wave
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    import voice
else:
    # 语音模块在 voice/，测试在 voice/tests/，所以取上一层。
    spec = importlib.util.spec_from_file_location(
        "voice", Path(__file__).parent.parent / "voice.py"
    )
    if spec is not None and spec.loader is not None and spec.origin and Path(spec.origin).exists():
        voice = importlib.util.module_from_spec(spec)
        sys.modules["voice"] = voice
        spec.loader.exec_module(voice)
    else:
        voice = None


def write_wav(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * int(seconds * 16000))


# 音频转换走系统 ffmpeg。没有它的机器（CI runner、干净容器）跳过，而不是报失败——
# 这不是产品缺陷，是环境缺件。
NEEDS_FFMPEG = unittest.skipUnless(shutil.which("ffmpeg"), "需要系统 ffmpeg 做音频转换")


class FakeASR:
    name: str = "fake-asr"

    def available(self) -> bool:
        return True

    def transcribe_chunk(self, audio_path: Path) -> voice.ChunkResult:
        with wave.open(str(audio_path)) as stream:
            duration = stream.getnframes() / stream.getframerate()
        return voice.ChunkResult(text="你好", language="zh", duration=duration)


class VoiceTests(unittest.TestCase):
    def test_transcribe_contract(self) -> None:
        self.assertIsNotNone(voice, "voice.py must implement the file-only interface")
        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            audio = Path(root) / "input.wav"
            write_wav(audio, 0.1)
            result = voice.transcribe(audio, backend=FakeASR())
        self.assertEqual(result["text"], "你好")
        self.assertEqual(result["language"], "zh")
        self.assertAlmostEqual(result["duration"], 0.1)
        self.assertEqual(result["backend"], "fake-asr")
        self.assertEqual(len(result["segments"]), 1)


class FeatureTests(unittest.TestCase):
    def test_long_audio_segment_offsets_and_cleanup(self) -> None:
        class RecordingASR(FakeASR):
            paths: list[Path] = []
            durations: list[float] = []

            def transcribe_chunk(self, audio_path: Path) -> voice.ChunkResult:
                self.paths.append(audio_path)
                result = super().transcribe_chunk(audio_path)
                self.durations.append(result.duration)
                return result

        backend = RecordingASR()
        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            source = Path(root) / "long.wav"
            write_wav(source, 2.5)
            result = voice.transcribe(source, backend=backend, chunk_seconds=1.0)
            self.assertEqual(backend.durations, [1.0, 1.0, 0.5])
            self.assertEqual([s["start"] for s in result["segments"]], [0.0, 1.0, 2.0])
            self.assertEqual(result["segments"][-1]["end"], 2.5)
            self.assertTrue(all(not path.exists() for path in backend.paths))
            self.assertTrue(source.exists())

    def test_backend_failure_is_explicit_and_not_retried(self) -> None:
        class BrokenASR(FakeASR):
            calls = 0

            def transcribe_chunk(self, audio_path: Path) -> voice.ChunkResult:
                self.calls += 1
                raise RuntimeError("private provider detail")

        backend = BrokenASR()
        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            source = Path(root) / "audio.wav"
            write_wav(source, 1.0)
            with self.assertRaisesRegex(voice.VoiceError, "fake-asr.*chunk 1") as caught:
                voice.transcribe(source, backend=backend)
            self.assertNotIn("private provider detail", str(caught.exception))
            self.assertEqual(backend.calls, 1)
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    def test_auto_selection_and_unavailability(self) -> None:
        class Missing(FakeASR):
            def available(self) -> bool:
                return False

        selected = voice.select_asr([Missing(), FakeASR()])
        self.assertEqual(selected.name, "fake-asr")
        with self.assertRaisesRegex(voice.BackendUnavailable, "No available ASR"):
            voice.select_asr([Missing()])

    def test_bad_chunk_sizes_missing_and_empty_audio(self) -> None:
        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            source = Path(root) / "audio.wav"
            write_wav(source, 0.1)
            for seconds in [0.0, -1.0, float("nan"), float("inf"), 0.000001]:
                with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                    voice.transcribe(source, backend=FakeASR(), chunk_seconds=seconds)
            with self.assertRaises(FileNotFoundError):
                voice.transcribe(Path(root) / "missing.wav", backend=FakeASR())
            empty = Path(root) / "empty.wav"
            write_wav(empty, 0.0)
            with self.assertRaisesRegex(voice.VoiceError, "empty"):
                voice.transcribe(empty, backend=FakeASR())

    def test_mixed_and_unknown_language(self) -> None:
        class MixedASR(FakeASR):
            calls = 0

            def transcribe_chunk(self, audio_path: Path) -> voice.ChunkResult:
                self.calls += 1
                return voice.ChunkResult(
                    text="你好", language="zh" if self.calls == 1 else "en", duration=1.0
                )

        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            source = Path(root) / "input.wav"
            write_wav(source, 2.0)
            self.assertEqual(
                voice.transcribe(source, backend=MixedASR(), chunk_seconds=1.0)["language"], "mixed"
            )

    def test_speak_configuration_output_and_no_overwrite(self) -> None:
        class FakeTTS:
            name = "fake-tts"
            seen: list[voice.VoiceConfig] = []

            def available(self) -> bool:
                return True

            def synthesize(self, text: str, out_path: Path, config: voice.VoiceConfig) -> None:
                self.seen.append(config)
                write_wav(out_path, 0.1)

        backend = FakeTTS()
        config = voice.VoiceConfig(voice="test-male", rate=140)
        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            target = Path(root) / "speech.wav"
            self.assertEqual(
                voice.speak("你好", target, backend=backend, config=config), str(target)
            )
            self.assertEqual(backend.seen, [config])
            self.assertGreater(target.stat().st_size, 44)
            with self.assertRaises(FileExistsError):
                voice.speak("你好", target, backend=backend)
            with self.assertRaises(ValueError):
                voice.speak("  ", Path(root) / "empty.wav", backend=backend)

    def test_tts_failure_no_partial_output(self) -> None:
        class BrokenTTS:
            name = "broken-tts"

            def available(self) -> bool:
                return True

            def synthesize(self, text: str, out_path: Path, config: voice.VoiceConfig) -> None:
                out_path.write_bytes(b"partial")
                raise OSError("provider failed")

        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            target = Path(root) / "out.wav"
            with self.assertRaisesRegex(voice.VoiceError, "broken-tts"):
                voice.speak("你好", target, backend=BrokenTTS())
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_bad_timestamps_are_rejected(self) -> None:
        class InvalidASR(FakeASR):
            def transcribe_chunk(self, audio_path: Path) -> voice.ChunkResult:
                return voice.ChunkResult(
                    text="你好",
                    language="zh",
                    duration=1.0,
                    segments=[voice.Segment(start=0.8, end=0.1, text="你好")],
                )

        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            source = Path(root) / "input.wav"
            write_wav(source, 1.0)
            with self.assertRaisesRegex(voice.VoiceError, "invalid segment"):
                voice.transcribe(source, backend=InvalidASR())


class AdapterTests(unittest.TestCase):
    def test_lifelog_rejects_malformed_worker_response(self) -> None:
        for response in ["not-json", "{}", "[]", '{"text":42,"language":"zh","duration":1}']:
            with (
                self.subTest(response=response),
                patch.object(voice, "_run", return_value=response),
            ):
                with self.assertRaisesRegex(voice.VoiceError, "invalid response"):
                    voice.LifelogASR().transcribe_chunk(Path("unused.wav"))

    def test_custom_model_paths_and_missing_assets(self) -> None:
        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            backend = voice.LifelogASR(
                root=Path(root), helpers=Path(root), python=Path(root) / "python"
            )
            self.assertFalse(backend.available())
            self.assertFalse(voice.LifelogASR(model="nonexistent").available())

    def test_subprocess_timeout_hides_payload(self) -> None:
        backend = voice.LifelogASR(python=Path(sys.executable), timeout=0.000001)
        with self.assertRaisesRegex(voice.VoiceError, "TimeoutExpired") as caught:
            backend.transcribe_chunk(Path("unused.wav"))
        self.assertNotIn("unused.wav", str(caught.exception))

    @NEEDS_FFMPEG
    def test_stereo_resampling(self) -> None:
        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            path = Path(root) / "stereo.wav"
            with wave.open(str(path), "wb") as stream:
                stream.setnchannels(2)
                stream.setsampwidth(2)
                stream.setframerate(8000)
                stream.writeframes(b"\x00" * 3200)
            result = voice.transcribe(path, backend=FakeASR())
            self.assertAlmostEqual(result["duration"], 0.1)

    @NEEDS_FFMPEG
    def test_corrupt_audio_is_explicit_error(self) -> None:
        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            path = Path(root) / "broken.wav"
            path.write_bytes(b"not-an-audio-file")
            with self.assertRaisesRegex(voice.VoiceError, "audio conversion"):
                voice.transcribe(path, backend=FakeASR())

    def test_empty_recognition_and_unavailable_tts(self) -> None:
        class EmptyASR(FakeASR):
            def transcribe_chunk(self, audio_path: Path) -> voice.ChunkResult:
                return voice.ChunkResult(text="", language="zh", duration=0.1)

        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            path = Path(root) / "input.wav"
            write_wav(path, 0.1)
            result = voice.transcribe(path, backend=EmptyASR())
            self.assertEqual(result["language"], "und")
            self.assertEqual(result["segments"], [])
        with self.assertRaises(voice.BackendUnavailable):
            voice.select_tts([])

    def test_voice_config_limits(self) -> None:
        for name, rate in [("", 155), ("male", 0), ("male", 401)]:
            with self.subTest(name=name, rate=rate), self.assertRaises(ValueError):
                voice.VoiceConfig(voice=name, rate=rate)


class StreamingTests(unittest.TestCase):
    def test_sentence_stream_yields_before_requesting_more_text(self) -> None:
        calls: list[str] = []

        class FakeTTS:
            name = "stream-fake"

            def available(self) -> bool:
                return True

            def synthesize(self, text: str, out_path: Path, config: voice.VoiceConfig) -> None:
                calls.append(text)
                write_wav(out_path, 0.1)

        def text_source() -> Iterator[str]:
            yield "Sir，第一句。"
            self.assertEqual(len(calls), 1, "Do not eagerly consume future model text")
            yield "第二句好了！"

        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            chunks = voice.speak_stream(text_source(), Path(root) / "segments", backend=FakeTTS())
            first = next(chunks)
            self.assertEqual(calls, ["Sir，第一句。"])
            self.assertAlmostEqual(first.duration, 0.1)
            self.assertEqual(first.start, 0.0)
            self.assertGreaterEqual(first.ready_latency_seconds, 0)
            self.assertTrue(Path(first.path).exists())
            self.assertTrue(Path(first.path + ".json").exists())
            second = next(chunks)
            self.assertAlmostEqual(second.start, 0.1)
            self.assertAlmostEqual(second.end, 0.2)
            with self.assertRaises(StopIteration):
                next(chunks)

    def test_speak_default_splits_and_callback_precedes_second_synthesis(self) -> None:
        calls: list[str] = []
        ready: list[voice.SpeechChunk] = []

        class FakeTTS:
            name = "stream-fake"

            def available(self) -> bool:
                return True

            def synthesize(self, text: str, out_path: Path, config: voice.VoiceConfig) -> None:
                if calls:
                    self_outer.assertEqual(len(ready), len(calls))
                calls.append(text)
                write_wav(out_path, 0.1)

        self_outer = self
        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            target = Path(root) / "speech.wav"
            voice.speak("第一句。第二句！", target, backend=FakeTTS(), on_segment=ready.append)
            self.assertEqual(calls, ["第一句。", "第二句！"])
            self.assertEqual(len(ready), 2)
            self.assertTrue(all(Path(c.path).exists() for c in ready))
            with wave.open(str(target)) as stream:
                self.assertAlmostEqual(stream.getnframes() / stream.getframerate(), 0.2)
            self.assertTrue(Path(str(target) + ".voice.json").exists())

    def test_stream_preserves_completed_chunks_on_later_failure(self) -> None:
        class BrokenTTS:
            name = "broken-stream"
            count = 0

            def available(self) -> bool:
                return True

            def synthesize(self, text: str, out_path: Path, config: voice.VoiceConfig) -> None:
                self.count += 1
                if self.count == 2:
                    out_path.write_bytes(b"partial")
                    raise RuntimeError("later synthesis failed")
                write_wav(out_path, 0.1)

        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            out = Path(root) / "segments"
            chunks = voice.speak_stream("第一句。第二句。", out, backend=BrokenTTS())
            first = next(chunks)
            with self.assertRaises(voice.VoiceError):
                next(chunks)
            self.assertTrue(Path(first.path).exists())
            self.assertEqual(len(list(out.glob("*.wav"))), 1)

    def test_asr_latency_is_reported(self) -> None:
        with TemporaryDirectory(prefix="jarvis-voice-test-") as root:
            source = Path(root) / "audio.wav"
            write_wav(source, 0.1)
            result = voice.transcribe(source, backend=FakeASR())
            self.assertGreaterEqual(result["latency_seconds"], 0)


class SyntheticSynthesisTests(unittest.TestCase):
    def test_say_adapter_uses_stdin_and_encodes_without_real_process(self) -> None:
        import subprocess

        with TemporaryDirectory(prefix="jarvis-voice-say-test-") as root:
            target = Path(root) / "out.wav"
            config = voice.VoiceConfig(voice="Synthetic Male", rate=140)
            with (
                patch.object(voice.shutil, "which", return_value="/fake/say"),
                patch.object(voice, "_ffmpeg", return_value="/fake/ffmpeg"),
                patch.object(
                    voice, "_run", side_effect=["Synthetic Male zh_CN test", "", ""]
                ) as run,
                patch.object(subprocess, "Popen", side_effect=AssertionError("no real synthesis")),
            ):
                voice.SayTTS().synthesize("synthetic private text", target, config)
            self.assertEqual(run.call_count, 3)
            synthesis = run.call_args_list[1]
            self.assertEqual(synthesis.kwargs, {"input_text": "synthetic private text"})
            self.assertNotIn("synthetic private text", synthesis.args[0])
            self.assertIn("140", synthesis.args[0])
            self.assertEqual(synthesis.args[0][-2:], ["-f", "-"])
            self.assertEqual(run.call_args_list[2].args[0][0], "/fake/ffmpeg")
            self.assertEqual(run.call_args_list[2].args[0][-1], str(target))
            self.assertFalse(target.exists(), "mocked commands do not produce real audio")

    def test_missing_say_voice_fails_before_synthesis(self) -> None:
        with TemporaryDirectory(prefix="jarvis-voice-say-test-") as root:
            with (
                patch.object(voice.shutil, "which", return_value="/fake/say"),
                patch.object(voice, "_run", return_value="Other Voice zh_CN test") as run,
            ):
                with self.assertRaisesRegex(voice.BackendUnavailable, "voice is not installed"):
                    voice.SayTTS().synthesize(
                        "synthetic", Path(root) / "out.wav", voice.VoiceConfig()
                    )
            run.assert_called_once_with(["/fake/say", "-v", "?"], "voice listing")


class SelfEvaluationTests(unittest.TestCase):
    def test_self_evaluate_is_owned_json_report_without_backend_or_file_access(self) -> None:
        import inspect
        import json
        from datetime import UTC, datetime

        hook = voice.self_evaluate
        self.assertTrue(callable(hook), "voice must own a real self_evaluate")
        self.assertEqual(inspect.getsourcefile(hook), voice.__file__)
        before = datetime.now(UTC)
        with (
            patch.object(voice, "select_asr", side_effect=AssertionError("no ASR probe")),
            patch.object(voice, "select_tts", side_effect=AssertionError("no TTS probe")),
            patch.object(voice.subprocess, "run", side_effect=AssertionError("no process")),
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
        self.assertEqual(report["module"], "voice")
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

    def test_self_evaluate_names_real_file_pipeline_limits(self) -> None:
        hook = voice.self_evaluate
        self.assertTrue(callable(hook), "voice must own a real self_evaluate")
        limits = " ".join(hook()["known_limitations"])
        for term in ("30", "chunk", "phoneme", "say", "cleanup"):
            self.assertIn(term, limits)

    def test_self_evaluate_returns_fresh_nested_lists(self) -> None:
        hook = voice.self_evaluate
        self.assertTrue(callable(hook), "voice must own a real self_evaluate")
        first = hook()
        expected = {
            key: list(first[key]) for key in ("known_limitations", "improvement_directions")
        }
        for key in expected:
            first[key].append("synthetic mutation")
        second = hook()
        for key in expected:
            self.assertEqual(second[key], expected[key])


if __name__ == "__main__":
    unittest.main()
