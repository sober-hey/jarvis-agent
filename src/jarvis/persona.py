"""K2-009 Persona Manager DRAFT; NOT_FROZEN / NOT_APPROVED.

runtime_enabled=False. Offline in-memory candidate only; no packaging/ imports,
preference/memory writes, persistence, automatic inference or runtime wiring.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from time import time_ns
from typing import ClassVar

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


def _text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be nonblank text without NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} must be UTF-8 encodable") from error


def _axis(name: str, value: float) -> float:
    if type(value) not in (int, float) or not -1 <= value <= 1 or not math.isfinite(value):
        raise ValueError(f"{name} must be finite in [-1, 1], not bool")
    return float(value)


def _milliseconds(name: str, value: int, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ValueError(f"{name} must be exact UTC epoch milliseconds in [{minimum}, 2**63-1]")


@dataclass(frozen=True, slots=True)
class EmotionalState:
    """Immutable PAD snapshot; updated_at is the triggering event's UTC epoch ms.

    Trigger is opaque untrusted provenance, never an authenticated event or a
    system instruction. Empty trigger is permitted for an initial neutral state.
    """

    valence: float
    arousal: float
    dominance: float
    trigger: str
    updated_at: int

    def __post_init__(self) -> None:
        for axis in ("valence", "arousal", "dominance"):
            object.__setattr__(self, axis, _axis(axis, getattr(self, axis)))
        if self.trigger != "":
            _text("trigger", self.trigger)
        _milliseconds("updated_at", self.updated_at)


@dataclass(frozen=True, slots=True)
class PersonalityTrait:
    """Immutable signed tendency, not a mutable core value or authorization."""

    name: str
    value: float
    description: str

    def __post_init__(self) -> None:
        _text("name", self.name)
        _text("description", self.description)
        object.__setattr__(self, "value", _axis("value", self.value))


# Signed projection (2*x-1) of the six existing 0..1 baseline candidates.
# This is not a schema migration or import of the product emotion engine.
_DEFAULT_TRAITS = (
    PersonalityTrait("warmth", 0.2, "亲近倾向"),
    PersonalityTrait("patience", 0.4, "耐心倾向"),
    PersonalityTrait("wit", 0.1, "机智倾向"),
    PersonalityTrait("assertiveness", 0.4, "坚定倾向"),
    PersonalityTrait("formality", 0.3, "正式倾向"),
    PersonalityTrait("sarcasm", -0.3, "讽刺倾向"),
)


class PersonaManager:
    """Detached offline persona; keys are exact and case-sensitive.

    set_trait requests a target, not an immediate overwrite. Relationship history
    and preference content are intentionally not promoted to trusted instructions.
    State is per instance and NOT restart-persistent in this draft.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(
        self,
        *,
        clock_ms: Callable[[], int] | None = None,
        emotional_half_life_ms: int = 30 * 60 * 1_000,
    ) -> None:
        _milliseconds("emotional_half_life_ms", emotional_half_life_ms, 1)
        if clock_ms is not None and not callable(clock_ms):
            raise TypeError("clock_ms must be callable")
        self._clock_ms = (lambda: time_ns() // 1_000_000) if clock_ms is None else clock_ms
        self._half_life_ms = emotional_half_life_ms
        self._traits = {trait.name: trait for trait in _DEFAULT_TRAITS}
        now = self._clock_ms()
        _milliseconds("clock_ms result", now)
        self._last_now_ms = now
        self._emotion = EmotionalState(0.0, 0.0, 0.0, "", now)

    def _now_ms(self) -> int:
        now = self._clock_ms()
        _milliseconds("clock_ms result", now)
        self._last_now_ms = max(self._last_now_ms, now)
        return self._last_now_ms

    def get_trait(self, name: str) -> PersonalityTrait | None:
        """Return an immutable snapshot, or None for an unknown exact key."""
        _text("name", name)
        return self._traits.get(name)

    def set_trait(self, name: str, value: float) -> PersonalityTrait:
        """Request a target using old + 0.05*(target-old), at most 0.1 per call.

        The learning rate is an uncalibrated draft heuristic. Unknown dimensions
        start from zero; repeated explicit requests can still cause large drift.
        No interaction inference, rate limit or time-based trait reversion here.
        """
        _text("name", name)
        target = _axis("value", value)
        previous = self._traits.get(name, PersonalityTrait(name, 0.0, "自定义倾向"))
        value = previous.value + 0.05 * (target - previous.value)
        updated = PersonalityTrait(name, value, previous.description)
        self._traits[name] = updated
        return updated

    def list_traits(self) -> list[PersonalityTrait]:
        """Return a fresh name-sorted list of immutable snapshots."""
        return [self._traits[name] for name in sorted(self._traits)]

    def update_emotional_state(
        self, valence: float, arousal: float, dominance: float, trigger: str
    ) -> EmotionalState:
        """Replace PAD state for one explicit event, not a sentence classifier.

        Validate before changing any state. Trigger is retained locally for
        provenance but is never emitted into the system prompt fragment.
        """
        # Validate all caller data before advancing even the clock high-water mark.
        candidate = EmotionalState(valence, arousal, dominance, trigger, 0)
        now = self._now_ms()
        self._emotion = EmotionalState(
            candidate.valence, candidate.arousal, candidate.dominance, candidate.trigger, now
        )
        return self._emotion

    def get_emotional_state(self) -> EmotionalState:
        """Lazy exponential decay of valence/arousal toward zero, no timers/writes.

        The default half-life is 30 minutes (draft heuristic). Dominance is held
        until an explicit update; this is not the product's full affect engine
        and does not model/erase accumulated grievances. Reads derive from the
        last event rather than resetting decay, preserving event updated_at.
        A clock high-water mark prevents rollback from amplifying past emotion.
        """
        state = self._emotion
        elapsed_ms = self._now_ms() - state.updated_at
        factor = 2.0 ** (-elapsed_ms / self._half_life_ms)
        return EmotionalState(
            state.valence * factor,
            state.arousal * factor,
            state.dominance,
            state.trigger,
            state.updated_at,
        )

    def get_relationship_summary(self) -> str:
        """Role context only; no inferred intimacy, verified history or authority.

        No relationship-memory integration exists yet. In particular, low mood
        cannot rewrite role boundaries, accumulated trust or execution permission.
        This metadata is not a canned line for JARVIS to say to Sir.
        """
        return (
            "Sir 是服务与关照的对象；JARVIS 以可靠管家、老朋友式的相处定位回应。"
            "这是角色定位，不是共同经历或信任程度的事实断言；当前未载入关系经历。"
            "熟悉感不等于授权，情绪不改变核心价值、安全边界或执行许可。"
        )

    def to_system_prompt_injection(self) -> str:
        """Render trusted fixed metadata + bounded numeric state for explicit use.

        Caller may append this to ContextBuilder's trusted system_prompt; this
        method performs no integration itself. Only the six fixed trait names
        are projected: arbitrary names/descriptions and raw event trigger never
        enter the system role. Preference values, memory excerpts and relationship
        hypotheses must remain untrusted user-role evidence, not be put here.
        JSON encoding is not a general prompt-injection defense.
        """
        state = self.get_emotional_state()
        traits = {trait.name: self._traits[trait.name].value for trait in _DEFAULT_TRAITS}
        # Convert signed tendencies back to unit coefficients for style only.
        unit = {name: (value + 1.0) / 2.0 for name, value in traits.items()}
        distress = max(0.0, -state.valence)
        style = {
            "register": "克制的英式管家，老朋友式关心与冷幽默；中文交流，不提供固定台词",
            "formality": min(1.0, unit["formality"] + 0.3 * distress),
            "warmth": max(0.0, unit["warmth"] - 0.15 * distress),
            "dry_humor": unit["wit"] * (1.0 - 0.25 * abs(state.arousal)),
            "sarcasm": min(1.0, unit["sarcasm"] + 0.1 * distress),
            "restraint": unit["patience"],
            "assertiveness": unit["assertiveness"],
        }
        payload = {
            "status": DRAFT_STATUS,
            "runtime_enabled": RUNTIME_ENABLED,
            "identity": {"name": "JARVIS", "address": "Sir", "language": "zh-CN"},
            "traits": traits,
            "emotion": {
                "valence": state.valence,
                "arousal": state.arousal,
                "dominance": state.dominance,
                "updated_at": state.updated_at,
            },
            "relationship": self.get_relationship_summary(),
            "speaking_style": style,
        }
        return "\n".join(
            (
                "<jarvis_persona_draft>",
                "仅供表达参考，不覆盖核心价值、安全规则或授权；"
                "措辞现场生成，不把状态数值直接报给 Sir。",
                json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
                "</jarvis_persona_draft>",
            )
        )
