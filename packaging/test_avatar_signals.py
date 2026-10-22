"""Offline avatar signal contract; fixtures never touch product data/history."""

from __future__ import annotations

import unittest
from pathlib import Path


class AvatarSignalsTests(unittest.TestCase):
    def test_idle_stream_exposes_numeric_control_channels(self) -> None:
        self.assertTrue(
            Path(__file__).with_name("avatar_signals.py").is_file(),
            "avatar signal layer has not been implemented",
        )
        from avatar_signals import AffectSnapshot, TurnIntent, TurnSignal, build_signals

        stream = build_signals(AffectSnapshot(), TurnSignal("normal", TurnIntent()), [])
        self.assertEqual(
            {event["type"] for event in stream}, {"expression", "posture", "gaze", "idle"}
        )
        self.assertEqual(stream[0]["time"], 0.0)
        self.assertEqual([event["time"] for event in stream], sorted(e["time"] for e in stream))

    def test_each_emotion_changes_continuously_without_expression_bands(self) -> None:
        from avatar_signals import AffectSnapshot, TurnSignal, build_signals

        for component in ("irritation", "hurt", "concern", "amusement", "pride", "grudge"):
            with self.subTest(component=component):
                values: list[tuple[float, ...]] = []
                for step in range(101):
                    strength = step / 100
                    snapshot = (
                        AffectSnapshot(grudge=strength)
                        if component == "grudge"
                        else AffectSnapshot(mood={component: strength})
                    )
                    params = build_signals(snapshot, TurnSignal(), [])[0]["parameters"]
                    values.append(tuple(params.values()))
                self.assertEqual(len(set(values)), 101)
                for left, right in zip(values, values[1:], strict=False):
                    self.assertLess(max(abs(a - b) for a, b in zip(left, right, strict=True)), 0.01)
                left = build_signals(
                    AffectSnapshot(mood={component: 0.449999}, grudge=0.449999),
                    TurnSignal(),
                    [],
                )[0]["parameters"]
                right = build_signals(
                    AffectSnapshot(mood={component: 0.450001}, grudge=0.450001),
                    TurnSignal(),
                    [],
                )[0]["parameters"]
                self.assertLess(max(abs(left[key] - right[key]) for key in left), 0.00001)

    def test_personality_traits_weight_expression_continuously(self) -> None:
        from avatar_signals import AffectSnapshot, TurnSignal, build_signals

        mood: dict[str, float] = dict.fromkeys(
            ("irritation", "hurt", "concern", "amusement", "pride"), 0.4
        )
        for trait in ("warmth", "patience", "wit", "assertiveness", "formality", "sarcasm"):
            with self.subTest(trait=trait):
                outputs = [
                    build_signals(
                        AffectSnapshot(mood=mood, traits={trait: value}, grudge=0.3),
                        TurnSignal(),
                        [],
                    )[0]["parameters"]
                    for value in (0.4, 0.5, 0.6)
                ]
                self.assertNotEqual(outputs[0], outputs[1])
                self.assertNotEqual(outputs[1], outputs[2])

    def test_speech_metadata_preserves_exact_bounds_without_prose(self) -> None:
        import json

        from avatar_signals import AffectSnapshot, SpeechSegment, TurnSignal, build_signals

        speech = [SpeechSegment(0, 9, 0.375, 0.0, 0.375), SpeechSegment(1, 6, 0.625, 0.5, 1.125)]
        stream = build_signals(AffectSnapshot(), TurnSignal(), speech)
        segments = [event for event in stream if event["type"] == "speech_segment"]
        self.assertEqual(len(segments), 2)
        for event, source in zip(segments, speech, strict=True):
            self.assertEqual(event["time"], source.start)
            self.assertEqual(event.get("start"), source.start)
            self.assertEqual(event.get("end"), source.end)
            self.assertEqual(event.get("duration"), source.duration)
            self.assertEqual(event.get("segment_index"), source.index)
            self.assertEqual(event.get("text_length"), source.text_length)
        self.assertLessEqual(segments[0].get("end", 0.0), segments[1].get("start", 0.0))
        self.assertEqual(json.loads(json.dumps(stream, allow_nan=False)), stream)
        self.assertEqual([e["time"] for e in stream], sorted(e["time"] for e in stream))

    def test_invalid_speech_is_rejected_instead_of_retimed(self) -> None:
        from avatar_signals import AffectSnapshot, SpeechSegment, TurnSignal, build_signals

        bad = [
            [SpeechSegment(0, 4, 1.0, -1.0, 0.0)],
            [SpeechSegment(0, 4, 1.0, 0.0, 0.5)],
            [SpeechSegment(0, 4, float("nan"), 0.0, 1.0)],
            [SpeechSegment(0, -1, 1.0, 0.0, 1.0)],
            [SpeechSegment(0, 4, 0.0, 0.0, 0.0)],
            [SpeechSegment(0, 4, 1.0, 0.0, 1.0), SpeechSegment(1, 4, 1.0, 0.9, 1.9)],
            [SpeechSegment(0, 4, 1.0, 0.0, 1.0), SpeechSegment(0, 4, 1.0, 1.0, 2.0)],
        ]
        for speech in bad:
            with self.subTest(speech=speech), self.assertRaises(ValueError):
                build_signals(AffectSnapshot(), TurnSignal(), speech)

    def test_idle_micro_movements_and_affective_posture_are_numeric(self) -> None:
        from avatar_signals import AffectSnapshot, TurnSignal, build_signals

        calm = build_signals(AffectSnapshot(), TurnSignal(), [])
        caring = build_signals(AffectSnapshot(mood={"concern": 1.0}), TurnSignal(), [])
        postures = [e["parameters"] for e in caring if e["type"] == "posture"]
        self.assertNotEqual(postures, [e["parameters"] for e in calm if e["type"] == "posture"])
        idle = [e for e in calm if e["type"] == "idle"]
        self.assertGreater(len(idle), 2)
        self.assertNotEqual(idle[0]["parameters"], idle[1]["parameters"])
        self.assertGreater(max(e["time"] for e in idle), 0.0)

    def test_readonly_engine_and_real_decision_adapter(self) -> None:
        import hashlib
        import tempfile
        from dataclasses import replace

        import avatar_signals as avatar
        from affect_engine import AffectEngine, Decision, ExpressionIntent

        self.assertTrue(callable(getattr(avatar, "signals_from_engine", None)))
        with tempfile.TemporaryDirectory(prefix="jarvis-avatar-") as folder:
            root = Path(folder)
            root.chmod(0o700)
            engine = AffectEngine(root, clock=lambda: 1000.0)
            before = hashlib.sha256((root / "affect.db").read_bytes()).hexdigest()
            decision = Decision(
                "priority",
                {},
                intent=replace(
                    ExpressionIntent(), emotion="PRIVATE_SENTINEL", task_label="PRIVATE_SENTINEL"
                ),
            )
            stream = avatar.signals_from_engine(engine, decision, [])
            self.assertEqual(before, hashlib.sha256((root / "affect.db").read_bytes()).hexdigest())
            self.assertGreater(stream[0]["parameters"]["care"], 0.0)
            self.assertNotIn("PRIVATE_SENTINEL", repr(stream))

    def test_real_voice_chunk_is_projected_without_path_or_text(self) -> None:
        import avatar_signals as avatar
        from voice import SpeechChunk

        self.assertTrue(callable(getattr(avatar.SpeechSegment, "from_voice_chunk", None)))
        chunk = SpeechChunk("PRIVATE_PATH.wav", 3, 0.5, 1.25, 1.75, "fake", 0.2, 0.1)
        segment = avatar.SpeechSegment.from_voice_chunk(chunk, text_length=7)
        self.assertEqual(segment, avatar.SpeechSegment(3, 7, 0.5, 1.25, 1.75))

    def test_cli_accepts_fixture_stdin_and_emits_only_signal_json(self) -> None:
        import json
        import subprocess
        import sys

        fixture = {
            "state": {"mood": {"irritation": 0.8, "concern": 0.2}, "grudge": 0.4},
            "turn": {"action": "priority", "intent": {"literal_only": True}},
            "speech": [{"index": 0, "text_length": 7, "duration": 0.5, "start": 0.0, "end": 0.5}],
        }
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(Path(__file__).with_name("avatar_signals.py")),
                "--input",
                "-",
            ],
            input=json.dumps(fixture),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().startswith("["))
        decoded: object = json.loads(result.stdout)
        self.assertIsInstance(decoded, list)
        self.assertIn('"speech_segment"', result.stdout)

    def test_bounds_priority_and_no_prose_for_random_mixtures(self) -> None:
        import json
        import random

        from avatar_signals import (
            EXPRESSION_AMPLITUDE_LIMIT,
            EXPRESSION_RANGES,
            GAZE_RANGES,
            IDLE_RANGES,
            POSTURE_RANGES,
            AffectSnapshot,
            TurnSignal,
            build_signals,
        )

        rng = random.Random(154)
        ranges = {
            "expression": EXPRESSION_RANGES,
            "posture": POSTURE_RANGES,
            "gaze": GAZE_RANGES,
            "idle": IDLE_RANGES,
        }
        for _ in range(200):
            mood = {
                name: rng.uniform(-2.0, 3.0)
                for name in ("irritation", "hurt", "concern", "amusement", "pride", "arousal")
            }
            traits = {
                name: rng.uniform(-2.0, 3.0)
                for name in ("warmth", "patience", "wit", "assertiveness", "formality", "sarcasm")
            }
            state = AffectSnapshot(mood, traits, rng.uniform(-2.0, 3.0))
            stream = build_signals(state, TurnSignal("priority"), [])
            params = stream[0]["parameters"]
            self.assertGreater(params["care"], params["displeasure"])
            self.assertGreater(params["focus"], params["displeasure"])
            self.assertLessEqual(max(abs(v) for v in params.values()), EXPRESSION_AMPLITUDE_LIMIT)
            for event in stream:
                self.assertEqual(set(event), {"type", "time", "parameters"})
                if event["type"] == "speech_segment":
                    self.fail("unexpected speech in idle fixture")
                for key, value in event["parameters"].items():
                    low, high = ranges[event["type"]][key]
                    self.assertGreaterEqual(value, low)
                    self.assertLessEqual(value, high)
            self.assertIsInstance(json.dumps(stream, allow_nan=False), str)

    def test_priority_suppresses_unhappy_blend_without_erasing_state(self) -> None:
        from avatar_signals import AffectSnapshot, TurnSignal, build_signals

        state = AffectSnapshot(mood={"irritation": 1.0, "hurt": 1.0}, grudge=1.0)
        ordinary = build_signals(state, TurnSignal(), [])[0]["parameters"]
        urgent = build_signals(state, TurnSignal("priority"), [])[0]["parameters"]
        self.assertLess(urgent["displeasure"], ordinary["displeasure"] * 0.1)
        self.assertGreater(urgent["care"], urgent["displeasure"])
        self.assertGreater(urgent["focus"], urgent["displeasure"])
        self.assertEqual(state.mood["irritation"], 1.0)
        self.assertEqual(state.grudge, 1.0)

    def test_invalid_numeric_inputs_are_rejected(self) -> None:
        from avatar_signals import AffectSnapshot, TurnSignal, build_signals, signals_from_fixture

        for state in (
            AffectSnapshot(mood={"hurt": float("nan")}),
            AffectSnapshot(traits={"warmth": float("inf")}),
            AffectSnapshot(grudge=float("nan")),
        ):
            with self.subTest(state=state), self.assertRaises(ValueError):
                build_signals(state, TurnSignal(), [])
        bad_payloads: list[object] = [
            {"sample_hz": 0},
            {"sample_hz": 31},
            {"idle_seconds": -1},
            {"idle_seconds": 601},
            {"turn": {"action": "unrecognized"}},
            {"turn": {"intent": {"comply": "PRIVATE_SENTINEL"}}},
            {"state": {"grudge": True}},
            {"speech": {}},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                signals_from_fixture(payload)

    def test_fixture_refuses_prose_and_unknown_fields(self) -> None:
        from avatar_signals import signals_from_fixture

        bad_payloads: list[object] = [
            {"response": "PRIVATE_SENTINEL"},
            {"state": {"mood": {"mystery": 0.1}}},
            {"state": {"traits": {"mystery": 0.1}}},
            {"turn": {"intent": {"emotion": "PRIVATE_SENTINEL"}}},
            {
                "speech": [
                    {
                        "index": 0,
                        "text_length": 2,
                        "duration": 1.0,
                        "start": 0.0,
                        "end": 1.0,
                        "text": "PRIVATE_SENTINEL",
                    }
                ]
            },
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                signals_from_fixture(payload)

    def test_actual_speak_stream_metadata_feeds_signal_layer(self) -> None:
        import tempfile
        import wave

        from avatar_signals import AffectSnapshot, SpeechSegment, TurnSignal, build_signals
        from voice import VoiceConfig, speak_stream

        class FakeTTS:
            name: str = "fixture-only"

            def available(self) -> bool:
                return True

            def synthesize(self, text: str, out_path: Path, config: VoiceConfig) -> None:
                with wave.open(str(out_path), "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(16000)
                    audio.writeframes(b"\x00\x00" * 1600)

        with tempfile.TemporaryDirectory(prefix="jarvis-avatar-voice-") as folder:
            chunks = list(speak_stream(["甲。", "乙。"], Path(folder) / "audio", backend=FakeTTS()))
            segments = [SpeechSegment.from_voice_chunk(chunk, text_length=2) for chunk in chunks]
            stream = build_signals(AffectSnapshot(), TurnSignal(), segments)
            cues = [event for event in stream if event["type"] == "speech_segment"]
            self.assertEqual(
                [(e.get("start"), e.get("end")) for e in cues],
                [(chunk.start, chunk.end) for chunk in chunks],
            )
            self.assertEqual([e.get("segment_index") for e in cues], [0, 1])
            self.assertNotIn(folder, repr(stream))
            self.assertEqual(chunks[0].end, chunks[1].start)

    def test_turn_actions_and_compliance_modulate_not_replace_mixture(self) -> None:
        from avatar_signals import AffectSnapshot, TurnIntent, TurnSignal, build_signals

        state = AffectSnapshot(mood={"amusement": 0.5, "irritation": 0.4, "concern": 0.4})
        normal = build_signals(state, TurnSignal(), [])[0]["parameters"]
        refusal = build_signals(state, TurnSignal(intent=TurnIntent(comply=False)), [])[0][
            "parameters"
        ]
        formal = build_signals(state, TurnSignal("chill_formal"), [])[0]["parameters"]
        self.assertNotEqual(refusal, normal)
        self.assertNotEqual(formal, normal)
        self.assertGreater(formal["mouth_corner"], 0.0)
        self.assertLess(formal["displeasure"], normal["displeasure"])

    def test_all_speech_fields_are_numeric_and_whitelisted(self) -> None:
        from avatar_signals import AffectSnapshot, SpeechSegment, TurnSignal, build_signals

        stream = build_signals(AffectSnapshot(), TurnSignal(), [SpeechSegment(0, 8, 1.0, 0.0, 1.0)])
        for event in stream:
            allowed = {"time", "type", "parameters"}
            if event["type"] == "speech_segment":
                allowed.update({"segment_index", "text_length", "duration", "start", "end"})
            self.assertEqual(set(event), allowed)
            self.assertTrue(
                all(
                    type(value) in (int, float)
                    for key, value in event.items()
                    if key not in {"type", "parameters"}
                )
            )
            self.assertTrue(all(isinstance(value, float) for value in event["parameters"].values()))

    def test_engine_projection_calls_only_current_once_and_never_reads_prose(self) -> None:
        from avatar_signals import (
            AffectSnapshot,
            TurnIntent,
            TurnSignal,
            signals_from_engine,
        )

        class FakeReader:
            calls: int = 0

            def current(self) -> AffectSnapshot:
                self.calls += 1
                return AffectSnapshot(mood={"concern": 0.6})

        reader = FakeReader()
        signals_from_engine(reader, TurnSignal("priority", TurnIntent()), [])
        self.assertEqual(reader.calls, 1)

    def test_same_state_is_deterministic_and_no_aliases_are_exposed(self) -> None:
        from avatar_signals import AffectSnapshot, TurnSignal, build_signals

        mood: dict[str, float] = {"concern": 0.3}
        state = AffectSnapshot(mood=mood)
        first = build_signals(state, TurnSignal(), [])
        second = build_signals(state, TurnSignal(), [])
        self.assertEqual(first, second)
        first[0]["parameters"]["care"] = 0.0
        self.assertNotEqual(first[0]["parameters"], first[4]["parameters"])
        self.assertEqual(mood, {"concern": 0.3})
        self.assertEqual(second, build_signals(state, TurnSignal(), []))


if __name__ == "__main__":
    unittest.main()
