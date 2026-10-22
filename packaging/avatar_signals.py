"""Numeric-only, renderer-independent avatar cues on the audio playback timeline.

This module never produces dialogue, plays audio, or contacts a model. The
consumer owns audio playback and converts these local controls to its rig.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, NotRequired, Protocol, TypedDict, cast


class AffectView(Protocol):
    @property
    def mood(self) -> Mapping[str, float]: ...

    @property
    def traits(self) -> Mapping[str, float]: ...

    @property
    def grudge(self) -> float: ...


class AffectReader(Protocol):
    def current(self) -> AffectView: ...


class IntentView(Protocol):
    @property
    def acknowledge_feeling(self) -> bool: ...

    @property
    def comply(self) -> bool: ...

    @property
    def proactive(self) -> bool: ...

    @property
    def literal_only(self) -> bool: ...

    @property
    def degraded(self) -> bool: ...

    @property
    def minimal_confirmation_only(self) -> bool: ...


class DecisionView(Protocol):
    @property
    def action(self) -> str: ...

    @property
    def intent(self) -> IntentView: ...


class ChunkView(Protocol):
    @property
    def index(self) -> int: ...

    @property
    def duration(self) -> float: ...

    @property
    def start(self) -> float: ...

    @property
    def end(self) -> float: ...


# Quarter-scale facial travel preserves the restrained British-butler direction;
# these are rig-independent normalized deltas, not full-face presets.
EXPRESSION_AMPLITUDE_LIMIT = 0.25
EXPRESSION_RANGES = MappingProxyType(
    {
        "brow_raise": (-0.25, 0.25),  # positive raises brows; negative lowers
        "brow_knit": (0.0, 0.25),  # positive draws inner brows together
        "mouth_corner": (-0.25, 0.25),  # positive lifts, negative lowers corners
        "eyelid_open": (-0.25, 0.25),  # delta from neutral, positive opens lids
        "lip_press": (0.0, 0.25),  # closes/compresses lips, not a speech viseme
        "care": (0.0, 0.25),  # semantic blend input for a caring facial rig
        "focus": (0.0, 0.25),  # semantic blend input for attentive facial rig
        "displeasure": (0.0, 0.25),  # semantic blend, never additive over cap
    }
)
TRAIT_DEFAULTS = MappingProxyType(
    {
        "warmth": 0.60,
        "patience": 0.70,
        "wit": 0.55,
        "assertiveness": 0.70,
        "formality": 0.65,
        "sarcasm": 0.35,
    }
)


def _unit(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("finite affect values required")
    return max(0.0, min(1.0, value))


def _expression(state: AffectSnapshot, turn: TurnSignal) -> dict[str, float]:
    mood = {
        key: _unit(state.mood.get(key, 0.0))
        for key in ("irritation", "hurt", "concern", "amusement", "pride", "arousal")
    }
    traits = {key: _unit(state.traits.get(key, value)) for key, value in TRAIT_DEFAULTS.items()}
    grudge = _unit(state.grudge)
    irritation = mood["irritation"] * (1 - 0.5 * traits["patience"])
    hurt = mood["hurt"] * (0.5 + 0.5 * traits["warmth"])
    reserve = 0.12 * grudge
    concern = mood["concern"] * (0.7 + 0.3 * traits["warmth"])
    amusement = mood["amusement"] * (0.6 + 0.4 * traits["wit"])
    pride = mood["pride"] * (0.6 + 0.4 * traits["assertiveness"])
    focus = 0.15 + 0.35 * concern + 0.15 * mood["arousal"]
    restraint = 1 - 0.25 * traits["formality"] - 0.15 * traits["patience"]
    # Numeric intents are modifiers of the continuous mixture, not face presets.
    if turn.intent.acknowledge_feeling:
        restraint *= 1.05
    if turn.intent.literal_only or not turn.intent.proactive:
        restraint *= 0.92
    if turn.intent.degraded or turn.intent.minimal_confirmation_only:
        restraint *= 0.5
    if not turn.intent.comply:
        restraint *= 0.90
    # Actions change restraint, not the identity of a face. The affect mixture
    # still supplies every channel; priority below is the safety override.
    restraint *= {
        "normal": 1.0,
        "chill_formal": 0.82,
        "defer": 0.85,
        "tease_answer": 1.03,
        "tease_only": 1.05,
        "priority": 1.0,
    }.get(turn.action, 1.0)
    if turn.action == "priority":
        concern = 0.8 + 0.2 * concern
        focus = 0.85 + 0.15 * mood["arousal"]
        irritation *= 0.04
        hurt *= 0.08
        reserve *= 0.04
        amusement *= 0.02
        pride *= 0.02
    displeasure = 0.6 * irritation + 0.25 * hurt + reserve
    weights = {
        "brow_raise": 0.65 * concern + 0.3 * amusement - 0.3 * irritation,
        "brow_knit": 0.45 * irritation + 0.3 * hurt + 0.2 * concern + reserve,
        "mouth_corner": 0.65 * amusement
        + 0.3 * pride
        - 0.4 * hurt
        - 0.3 * irritation
        - reserve
        + 0.05 * concern,
        "eyelid_open": 0.55 * focus + 0.25 * concern - 0.25 * amusement - 0.2 * hurt,
        "lip_press": 0.6 * irritation + 0.2 * hurt + reserve,
        "care": concern,
        "focus": focus + 0.05 * traits["assertiveness"],
        "displeasure": displeasure * (0.8 + 0.2 * traits["sarcasm"]),
    }
    return {
        key: EXPRESSION_AMPLITUDE_LIMIT * math.tanh(value * restraint)
        for key, value in weights.items()
    }


@dataclass(frozen=True)
class AffectSnapshot:
    mood: Mapping[str, float] = field(default_factory=lambda: dict[str, float]())
    traits: Mapping[str, float] = field(default_factory=lambda: dict[str, float]())
    grudge: float = 0.0


@dataclass(frozen=True)
class TurnIntent:
    acknowledge_feeling: bool = False
    comply: bool = True
    proactive: bool = True
    literal_only: bool = False
    degraded: bool = False
    minimal_confirmation_only: bool = False


@dataclass(frozen=True)
class TurnSignal:
    action: str = "normal"
    intent: TurnIntent = field(default_factory=TurnIntent)


@dataclass(frozen=True)
class SpeechSegment:
    index: int
    text_length: int
    duration: float
    start: float
    end: float

    @classmethod
    def from_voice_chunk(cls, chunk: ChunkView, *, text_length: int) -> SpeechSegment:
        """Accept voice.SpeechChunk without importing a TTS backend or exposing prose."""
        return cls(chunk.index, text_length, chunk.duration, chunk.start, chunk.end)


class Signal(TypedDict):
    time: float
    type: Literal["expression", "posture", "gaze", "idle", "speech_segment"]
    parameters: dict[str, float]
    segment_index: NotRequired[int]
    text_length: NotRequired[int]
    start: NotRequired[float]
    end: NotRequired[float]
    duration: NotRequired[float]


# +X avatar-right, +Y up, +Z forward; angles use semantic positive directions:
# yaw right, pitch up, roll right-ear-down. Rig adapter converts handedness.
POSTURE_RANGES = MappingProxyType(
    {
        "lean_forward": (-0.08, 0.08),  # fraction of torso length toward +Z
        "head_pitch_rad": (-0.10, 0.10),
        "head_yaw_rad": (-0.10, 0.10),
        "head_roll_rad": (-0.08, 0.08),
    }
)
GAZE_RANGES = MappingProxyType(
    {
        "yaw_rad": (-0.12, 0.12),
        "pitch_rad": (-0.08, 0.08),
        "engagement": (0.0, 1.0),
    }
)
IDLE_RANGES = MappingProxyType(
    {
        "breath_offset": (-0.008, 0.008),  # torso-length fraction along +Y
        "head_sway_rad": (-0.015, 0.015),
        "eyelid_micro": (-0.025, 0.025),
    }
)
DEFAULT_SAMPLE_HZ = 10.0
DEFAULT_IDLE_SECONDS = 2.0
MAX_TIMELINE_SECONDS = 600.0
MAX_SEGMENTS = 2000


def _validate_speech(speech: Sequence[SpeechSegment]) -> None:
    if len(speech) > MAX_SEGMENTS:
        raise ValueError("too many speech segments")
    previous_end, previous_index = 0.0, -1
    for segment in speech:
        if (
            type(segment.index) is not int
            or type(segment.text_length) is not int
            or segment.index <= previous_index
            or segment.text_length < 0
        ):
            raise ValueError("ordered integer indices and nonnegative text lengths required")
        if not all(math.isfinite(v) for v in (segment.start, segment.end, segment.duration)):
            raise ValueError("finite speech timing required")
        if (
            segment.start < previous_end
            or segment.start < 0
            or segment.duration <= 0
            or segment.end <= segment.start
            or segment.end > MAX_TIMELINE_SECONDS
            or not math.isclose(
                segment.end - segment.start, segment.duration, rel_tol=1e-9, abs_tol=1e-9
            )
        ):
            raise ValueError("nonoverlapping consistent speech timing required")
        previous_end, previous_index = segment.end, segment.index


def build_signals(
    state: AffectSnapshot,
    turn: TurnSignal,
    speech: Sequence[SpeechSegment],
    *,
    sample_hz: float = DEFAULT_SAMPLE_HZ,
    idle_seconds: float = DEFAULT_IDLE_SECONDS,
) -> list[Signal]:
    """Build one bounded cue batch; time is seconds on the voice audio timeline.

    start/end are copied exactly, never stretched or sorted to hide overlap.
    Silence gaps are legal. Segment identity is its voice.SpeechChunk.index;
    the caller holds the audio registry. No path or source text is emitted.
    """
    _validate_speech(speech)
    if not math.isfinite(sample_hz) or not 1.0 <= sample_hz <= 30.0:
        raise ValueError("sample_hz must be finite and 1..30")
    if not math.isfinite(idle_seconds) or not 0 <= idle_seconds <= MAX_TIMELINE_SECONDS:
        raise ValueError("idle_seconds out of range")
    expression = _expression(state, turn)
    care = expression["care"] / EXPRESSION_AMPLITUDE_LIMIT
    focus = expression["focus"] / EXPRESSION_AMPLITUDE_LIMIT
    displeasure = expression["displeasure"] / EXPRESSION_AMPLITUDE_LIMIT
    posture = {
        "lean_forward": 0.08 * math.tanh(care + 0.3 * focus - 0.2 * displeasure),
        "head_pitch_rad": 0.10 * math.tanh(-0.5 * care + 0.2 * displeasure),
        "head_yaw_rad": 0.10 * math.tanh(-0.15 * displeasure),
        "head_roll_rad": 0.08 * math.tanh(0.3 * care - 0.2 * focus),
    }
    gaze = {
        "yaw_rad": -0.025 * displeasure * (1 - focus),
        "pitch_rad": -0.015 * care,
        "engagement": min(1.0, 0.7 + 0.2 * care + 0.1 * focus),
    }
    duration = max(idle_seconds, speech[-1].end if speech else 0.0)
    times = {0.0, duration}
    times.update(i / sample_hz for i in range(1, math.ceil(duration * sample_hz)))
    times.update(value for segment in speech for value in (segment.start, segment.end))
    stream: list[Signal] = []
    for timestamp in sorted(times):
        speaking = any(segment.start <= timestamp < segment.end for segment in speech)
        idle_scale = (0.25 if speaking else 1.0) * (1 - 0.6 * focus)
        idle = {
            "breath_offset": 0.008 * idle_scale * math.sin(2 * math.pi * timestamp / 4.8),
            "head_sway_rad": 0.015 * idle_scale * math.sin(2 * math.pi * timestamp / 7.3),
            "eyelid_micro": 0.025 * idle_scale * math.sin(2 * math.pi * timestamp / 3.7),
        }
        stream.extend(
            (
                Signal(time=timestamp, type="expression", parameters=dict(expression)),
                Signal(time=timestamp, type="posture", parameters=dict(posture)),
                Signal(time=timestamp, type="gaze", parameters=dict(gaze)),
                Signal(time=timestamp, type="idle", parameters=idle),
            )
        )
    stream.extend(
        Signal(
            time=segment.start,
            type="speech_segment",
            parameters={},
            segment_index=segment.index,
            text_length=segment.text_length,
            start=segment.start,
            end=segment.end,
            duration=segment.duration,
        )
        for segment in speech
    )
    # Stable tie order: expression, posture, gaze, idle, then segment metadata.
    return sorted(stream, key=lambda event: event["time"])


def signals_from_engine(
    engine: AffectReader,
    decision: DecisionView,
    speech: Sequence[SpeechSegment],
    *,
    sample_hz: float = DEFAULT_SAMPLE_HZ,
    idle_seconds: float = DEFAULT_IDLE_SECONDS,
) -> list[Signal]:
    """Read AffectEngine.current() exactly once; never handle(), claim or persist.

    A structural protocol avoids importing the affect/voice module's flat-import
    dependencies in python -m packaging.avatar_signals. Narrative intent, task,
    facts, audio paths and model replies are not read or copied.
    """
    state = engine.current()
    intent = decision.intent
    snapshot = AffectSnapshot(dict(state.mood), dict(state.traits), state.grudge)
    turn = TurnSignal(
        decision.action,
        TurnIntent(
            intent.acknowledge_feeling,
            intent.comply,
            intent.proactive,
            intent.literal_only,
            intent.degraded,
            intent.minimal_confirmation_only,
        ),
    )
    return build_signals(snapshot, turn, speech, sample_hz=sample_hz, idle_seconds=idle_seconds)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    data = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in data):
        raise ValueError("string JSON object keys required")
    return cast(dict[str, object], data)


def _number(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("numeric JSON value required")
    result = float(cast(int | float, value))
    if not math.isfinite(result):
        raise ValueError("finite JSON number required")
    return result


def _numeric_map(value: object) -> dict[str, float]:
    return {key: _number(item) for key, item in _object(value).items()}


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("integer JSON value required")
    return value


def _allowed(data: Mapping[str, object], keys: Sequence[str]) -> None:
    if set(data) - set(keys):
        raise ValueError("unknown field")


def signals_from_fixture(payload: object) -> list[Signal]:
    """Offline JSON contract. No user text field is accepted or propagated."""
    data = _object(payload)
    _allowed(data, ("state", "turn", "speech", "sample_hz", "idle_seconds"))
    state = _object(data.get("state", {}))
    _allowed(state, ("mood", "traits", "grudge"))
    _allowed(
        _object(state.get("mood", {})),
        ("irritation", "hurt", "concern", "amusement", "pride", "arousal"),
    )
    _allowed(_object(state.get("traits", {})), tuple(TRAIT_DEFAULTS))
    turn = _object(data.get("turn", {}))
    _allowed(turn, ("action", "intent"))
    intent = _object(turn.get("intent", {}))
    _allowed(
        intent,
        (
            "acknowledge_feeling",
            "comply",
            "proactive",
            "literal_only",
            "degraded",
            "minimal_confirmation_only",
        ),
    )
    flags: dict[str, bool] = {}
    for key, default in (
        ("acknowledge_feeling", False),
        ("comply", True),
        ("proactive", True),
        ("literal_only", False),
        ("degraded", False),
        ("minimal_confirmation_only", False),
    ):
        value = intent.get(key, default)
        if not isinstance(value, bool):
            raise ValueError("boolean intent required")
        flags[key] = value
    action = turn.get("action", "normal")
    if not isinstance(action, str) or action not in {
        "normal",
        "priority",
        "chill_formal",
        "defer",
        "tease_answer",
        "tease_only",
    }:
        raise ValueError("unknown decision action")
    raw_speech = data.get("speech", [])
    if not isinstance(raw_speech, list):
        raise ValueError("speech array required")
    speech: list[SpeechSegment] = []
    for item in cast(list[object], raw_speech):
        row = _object(item)
        _allowed(row, ("index", "text_length", "duration", "start", "end"))
        speech.append(
            SpeechSegment(
                _integer(row["index"]),
                _integer(row["text_length"]),
                _number(row["duration"]),
                _number(row["start"]),
                _number(row["end"]),
            )
        )
    return build_signals(
        AffectSnapshot(
            _numeric_map(state.get("mood", {})),
            _numeric_map(state.get("traits", {})),
            _number(state.get("grudge", 0)),
        ),
        TurnSignal(action, TurnIntent(**flags)),
        speech,
        sample_hz=_number(data.get("sample_hz", DEFAULT_SAMPLE_HZ)),
        idle_seconds=_number(data.get("idle_seconds", DEFAULT_IDLE_SECONDS)),
    )


def self_evaluate() -> dict[str, object]:
    """Probe pure cue generation in memory; no engine, voice or renderer IO."""
    from datetime import datetime, timezone

    stream = build_signals(AffectSnapshot(), TurnSignal(), [], idle_seconds=0)
    ranges: dict[str, Mapping[str, tuple[float, float]]] = {
        "expression": EXPRESSION_RANGES,
        "posture": POSTURE_RANGES,
        "gaze": GAZE_RANGES,
        "idle": IDLE_RANGES,
    }
    within_ranges = bool(stream) and {item["type"] for item in stream} == set(ranges)
    for item in stream:
        expected = ranges.get(item["type"], {})
        within_ranges = within_ranges and set(item["parameters"]) == set(expected)
        for key, value in item["parameters"].items():
            bounds = expected.get(key)
            within_ranges = within_ranges and bounds is not None and math.isfinite(value)
            if bounds is not None:
                within_ranges = within_ranges and bounds[0] <= value <= bounds[1]
    invalid_rejected = False
    try:
        build_signals(AffectSnapshot(), TurnSignal(), [], sample_hz=0, idle_seconds=0)
    except ValueError:
        invalid_rejected = True
    return {
        "module": "avatar_signals",
        "domain": "bounded numeric affect and speech timeline avatar cues",
        "current_version": "1",
        "version_scope": "offline evaluation contract, not a deployed release",
        "known_limitations": [
            "Rig-independent numeric cues do not verify a renderer or visual quality.",
            "Speech timing is supplied by the caller; audio playback is not observed.",
            "No mind/KG persistence or resident evolution wiring is provided here.",
        ],
        "improvement_directions": [
            "Validate rig adapters and actual playback synchronization separately."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_verified": False,
        "scope": "in-memory cue generation and validation only",
        "limits": {
            "max_segments": MAX_SEGMENTS,
            "max_timeline_seconds": MAX_TIMELINE_SECONDS,
            "default_sample_hz": DEFAULT_SAMPLE_HZ,
        },
        "offline_probe": {
            "event_count": len(stream),
            "channel_types": sorted({item["type"] for item in stream}),
            "within_declared_ranges": bool(within_ranges),
            "invalid_sample_rate_rejected": invalid_rejected,
        },
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="fixture JSON path, or - for stdin")
    args = parser.parse_args()
    try:
        if args.input == "-":
            raw = sys.stdin.read(2_000_001)
        else:
            with Path(args.input).open(encoding="utf-8") as source:
                raw = source.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("fixture too large")
        stream = signals_from_fixture(json.loads(raw))
        print(json.dumps(stream, ensure_ascii=False, allow_nan=False, indent=2))
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        # No input values, source text, paths or exception chains in diagnostics.
        print("avatar_signals: invalid fixture", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
