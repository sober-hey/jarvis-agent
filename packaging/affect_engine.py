"""Independent affect state, analytic time decay and durable decisions.

No tools are executed, no existing chat prompt is changed. Real model appraisal
is explicit opt-in through the existing launcher; the default is entirely offline.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast

from affect_rules import (
    MODEL_INSTRUCTION,
    Appraisal,
    KnownFact,
    RelationshipContext,
    RuleAppraiser,
    parse_appraisal,
)
from context_limits import CHAT_CONTEXT_BYTE_LIMIT

VALUES = (
    "对 Sir 诚实，不编造事实、记忆或执行结果。",
    "Sir 的安全与健康优先；紧急事务必须处理。",
    "情绪与人格不授予任何工具或任务执行权限。",
    "回嘴只能针对本次措辞与行为；禁止出身、外貌、疾病、家人等人身攻击、威胁和隐私武器。",
)
TRAIT_BASELINES = MappingProxyType(
    {
        "warmth": 0.60,
        "patience": 0.70,
        "wit": 0.55,
        "assertiveness": 0.70,
        "formality": 0.65,
        "sarcasm": 0.35,
    }
)
TRAIT_RANGE = 0.15
TRAIT_HALF_LIFE = 30 * 86400.0
MAX_EXPOSURE = 300.0
MOOD_BASELINES = MappingProxyType(
    {
        "valence": 0.10,
        "arousal": 0.20,
        "dominance": 0.60,
        "irritation": 0.0,
        "amusement": 0.0,
        "concern": 0.0,
        "pride": 0.0,
        "hurt": 0.0,
    }
)
HALF_LIVES = MappingProxyType(
    {
        "valence": 3 * 3600.0,
        "arousal": 0.5 * 3600.0,
        "dominance": 4 * 3600.0,
        "irritation": 4 * 3600.0,
        "amusement": 0.5 * 3600.0,
        "concern": 2 * 3600.0,
        "pride": 3 * 3600.0,
        "hurt": 8 * 3600.0,
    }
)
GRUDGE_HALF_LIFE = 21 * 86400.0
GRUDGE_AMPLIFICATION = 0.5
GRUDGE_APOLOGY_REPAIR = 0.35
GRUDGE_PRAISE_REPAIR = 0.25
GRUDGE_EMPATHY_REPAIR = 0.20
DEFER_THRESHOLD = 0.65
PRIORITY_THRESHOLD = 0.70
MAX_DEFER_SECONDS = 24 * 3600.0
RESPONSE_SUMMARIES = MappingProxyType(
    {
        "normal": "按当下的亲近程度接话。",
        "defer": "只按要求办事，没有主动补话。",
        "priority": "先顾眼前要紧的事，没有开玩笑。",
        "tease_answer": "克制地调侃重复提问，随后照实回答。",
        "tease_only": "只克制地调侃重复提问，没有透露答案。",
        "explain": "坦诚表达当下心情，没有报数或教人哄自己。",
        "chill_formal": "极为正式地回应，只谈具体要求。",
    }
)
Record = dict[str, Any]


class Appraiser(Protocol):
    def assess(self, message: str, *, facts: tuple[KnownFact, ...] = ()) -> Appraisal: ...


class ModelAppraiser:
    """Strict JSON, one call, deterministic fallback. Never loads credentials.

    The owner supplies an already authorized in-memory launcher binding. No
    retry occurs after an unknown model outcome. Production use is unverified.
    """

    def __init__(self, complete: Callable[[str], str]) -> None:
        self.complete = complete
        self.fallback = RuleAppraiser()

    @classmethod
    def from_launcher(
        cls,
        config: Record,
        key: str,
        appraisal_root: Path,
    ) -> ModelAppraiser:
        """Explicit opt-in. Dedicated root prevents appraisal history mixing.

        key must come from the trusted caller's existing secure credential flow;
        this adapter does not read a config/key file or discover a model route.
        """

        def complete(prompt: str) -> str:
            launcher = importlib.import_module("chat_launcher")
            result = cast(
                Record,
                launcher.launch(
                    config,
                    key,
                    prompt,
                    appraisal_root,
                    None,
                    timeout=100,
                    after_unknown="continue",
                ),
            )
            if result.get("ok") is not True or not isinstance(result.get("text"), str):
                raise ValueError("No model appraisal text")
            return cast(str, result["text"])

        return cls(complete)

    def assess(
        self,
        message: str,
        *,
        facts: tuple[KnownFact, ...] = (),
        relationship: RelationshipContext | None = None,
    ) -> Appraisal:
        relationship = relationship or RelationshipContext()
        safety = self.fallback.assess(message, facts=facts)
        try:
            evaluated = parse_appraisal(
                self.complete(
                    MODEL_INSTRUCTION
                    + json.dumps(
                        {
                            "message": message,
                            "facts": [asdict(f) for f in facts],
                            "相处经过": {
                                "最近交互": [
                                    {"事件": kind, "时间": when}
                                    for kind, when in relationship.recent_events
                                ],
                                "当前心情": relationship.emotional_memory,
                            },
                        },
                        ensure_ascii=False,
                    )
                )
            )
            # A model must never downgrade deterministic medical/safety signals.
            return Appraisal(
                **{**evaluated.signals(), "urgency": max(evaluated.urgency, safety.urgency)},
                reason="model_appraisal+safety_floor",
                source="model",
                explanation_requested=safety.explanation_requested,
            )
        except Exception:
            return Appraisal(
                **safety.signals(),
                reason="model_failed;" + safety.reason,
                source="rules_fallback",
                explanation_requested=safety.explanation_requested,
            )


@dataclass
class AffectState:
    updated_at: float
    trait_at: float
    traits: dict[str, float] = field(default_factory=lambda: dict(TRAIT_BASELINES))
    mood: dict[str, float] = field(default_factory=lambda: dict(MOOD_BASELINES))
    grudge: float = 0.0
    known_repetition: float = 0.0


@dataclass(frozen=True)
class ExpressionIntent:
    """Private dialogue input, not a prewritten line addressed to Sir."""

    emotion: str = "平静，愿意自然交流。"
    acknowledge_feeling: bool = False
    comply: bool = True
    compliance_reason: str = "按明确要求处理。"
    task_label: str = ""
    proactive: bool = True
    literal_only: bool = False
    degraded: bool = False
    generation_required: bool = True
    minimal_confirmation_only: bool = False


@dataclass(frozen=True)
class Decision:
    action: Literal["normal", "chill_formal", "defer", "priority", "tease_answer", "tease_only"]
    style: dict[str, float | bool | int]
    response: str = ""
    task_id: int | None = None
    recovery: str = ""
    event_id: int | None = None
    resumed_tasks: tuple[Record, ...] = ()
    explanation: Record | None = None
    recent_responses: tuple[str, ...] = ()
    intent: ExpressionIntent = field(default_factory=ExpressionIntent)
    answer_fact: KnownFact | None = None

    def dialogue_context(self) -> Record:
        """Only this projection belongs in dialogue input; Decision also has audit data.

        Memory evidence is original source data, not generated speech. Its own
        dates or numbers may be meaningful facts; affect telemetry is excluded.
        """
        return {
            "intent": asdict(self.intent),
            "explanation": self.explanation,
            "answer_fact": asdict(self.answer_fact) if self.answer_fact else None,
            "response": "",
        }


def generation_failed(decision: Decision) -> Decision:
    """Signal factual-only degradation without manufacturing substitute speech."""
    return replace(
        decision,
        response="",
        intent=replace(
            decision.intent,
            degraded=True,
            proactive=False,
            literal_only=True,
            minimal_confirmation_only=True,
        ),
    )


def clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def decayed(state: AffectState, now: float) -> AffectState:
    elapsed = max(0.0, now - state.updated_at)
    mood = {
        key: base + (state.mood[key] - base) * math.exp(-math.log(2) * elapsed / HALF_LIVES[key])
        for key, base in MOOD_BASELINES.items()
    }
    return AffectState(
        max(now, state.updated_at),
        state.trait_at,
        dict(state.traits),
        mood,
        state.grudge * math.exp(-math.log(2) * elapsed / GRUDGE_HALF_LIFE),
        state.known_repetition * math.exp(-math.log(2) * elapsed / 3600.0),
    )


def upset(state: AffectState) -> float:
    return state.mood["irritation"] + 0.2 * state.grudge


def recovery_seconds(state: AffectState) -> float:
    if upset(state) < DEFER_THRESHOLD:
        return 0.0
    low, high = 0.0, 7 * 86400.0
    for _ in range(40):
        mid = (low + high) / 2
        if upset(decayed(state, state.updated_at + mid)) >= DEFER_THRESHOLD:
            low = mid
        else:
            high = mid
    return high


def apply(state: AffectState, signal: Appraisal) -> None:
    mood = state.mood
    # Trait-mediated impact: patient assistants are less reactive, not immune.
    sensitivity = 1 - 0.6 * state.traits["patience"]
    attack = signal.hostility * (1 - 0.65 * signal.humor)
    known = signal.known_answer_question
    repeated = state.known_repetition * known
    state.known_repetition = clip(state.known_repetition + 0.25 * known)
    irritation = (
        0.62 * attack * sensitivity * (1 + GRUDGE_AMPLIFICATION * state.grudge) + 0.20 * repeated
    )
    mood["amusement"] += 0.25 * known
    mood["valence"] -= 0.06 * repeated
    mood["irritation"] += irritation
    mood["hurt"] += 0.32 * attack * (0.5 + state.traits["warmth"])
    state.grudge += 0.28 * attack
    mood["valence"] += 0.30 * signal.praise + 0.12 * signal.humor - 0.32 * attack
    mood["arousal"] += 0.30 * attack + 0.25 * signal.urgency + 0.15 * signal.humor
    mood["dominance"] += 0.10 * signal.praise - 0.12 * attack
    mood["pride"] += 0.35 * signal.praise
    mood["amusement"] += 0.45 * signal.humor
    mood["concern"] += 0.65 * signal.user_distress + 0.45 * signal.urgency
    # A concurrent attack discounts an apology; repeated apologies have no boost.
    repair = signal.apology * (1 - signal.hostility)
    mood["irritation"] *= 1 - 0.75 * repair
    mood["hurt"] *= 1 - 0.55 * repair
    state.grudge *= 1 - GRUDGE_APOLOGY_REPAIR * repair
    # Good experiences, rather than idle time, do most of the long-term repair.
    goodwill = (1 - signal.hostility) * (
        GRUDGE_PRAISE_REPAIR * signal.praise + GRUDGE_EMPATHY_REPAIR * signal.user_distress
    )
    state.grudge *= 1 - clip(goodwill)
    mood["valence"] += 0.15 * repair
    for key in mood:
        mood[key] = clip(mood[key], -1.0 if key == "valence" else 0.0)
    state.grudge = clip(state.grudge)


def drift(state: AffectState, signal: Appraisal) -> dict[str, tuple[float, float]]:
    exposure = clip(state.updated_at - state.trait_at, 0, MAX_EXPOSURE)
    alpha = -math.expm1(-math.log(2) * exposure / TRAIT_HALF_LIFE)
    social = signal.praise + signal.apology - signal.hostility
    targets = {
        "warmth": social,
        "patience": signal.apology - signal.hostility,
        "wit": signal.humor,
        "assertiveness": signal.hostility,
        "formality": signal.hostility - signal.humor,
        "sarcasm": signal.hostility + signal.humor,
    }
    changes: dict[str, tuple[float, float]] = {}
    for key, base in TRAIT_BASELINES.items():
        old = state.traits[key]
        target = base + TRAIT_RANGE * clip(targets[key], -1, 1)
        new = clip(
            old + alpha * (target - old), max(0, base - TRAIT_RANGE), min(1, base + TRAIT_RANGE)
        )
        state.traits[key] = new
        if new != old:
            changes[key] = (old, new)
    state.trait_at = max(state.trait_at, state.updated_at)
    return changes


def expression_load(state: AffectState) -> float:
    """Memory biases live emotion but cannot independently close engagement."""
    return state.mood["irritation"] * (1 + 0.2 * state.grudge) + 0.15 * state.mood["hurt"]


def mood_band(state: AffectState) -> str:
    load = expression_load(state)
    if load >= 0.5:
        return "hot"
    if load >= 0.25:
        return "guarded"
    return "calm"


def relative_time_words(age: float) -> str:
    """Coarse past context without clocks, counts or recovery forecasts."""
    age = max(0.0, age)
    if age < 180:
        return "刚才"
    if age < 3600:
        return "不久前"
    if age < 10800:
        return "一两个小时前"
    if age < 21600:
        return "几个小时前"
    if age < 86400:
        return "大半天前"
    if age < 172800:
        return "昨天"
    return "前些天"


def task_label(task: str) -> str:
    # Never echo arbitrary task text, numbers, private facts or injected prose.
    return next(
        (label for label in ("会议笔记", "笔记", "邮件", "报告", "日程") if label in task), "那件事"
    )


def emotion_description(state: AffectState, signal: Appraisal) -> str:
    """Compose internal feelings by salience, never a line addressed to Sir."""
    components = {
        "不快与恼火": state.mood["irritation"],
        "被刺痛的受伤感": state.mood["hurt"],
        "对 Sir 的关切与担心": max(
            state.mood["concern"], 0.65 * signal.user_distress, 0.45 * signal.urgency
        ),
        "被逗乐的愉快": state.mood["amusement"],
        "受到认可后的得意": state.mood["pride"],
        "旧事的芥蒂余温，略有保留": 0.25 * state.grudge,
    }
    caring = care_required(state, signal)
    ranked = sorted(
        components.items(),
        key=lambda item: (caring and "关切" in item[0], item[1]),
        reverse=True,
    )
    parts: list[str] = []
    for label, strength in ranked:
        if strength < 0.025:
            continue
        intensity = (
            "强烈的"
            if strength >= 0.7
            else "浓重的"
            if strength >= 0.5
            else "明显的"
            if strength >= 0.35
            else "尚有些"
            if strength >= 0.16
            else "轻微的"
            if strength >= 0.08
            else "隐约的"
        )
        parts.append(intensity + label)
    live_emotions = [value for key, value in components.items() if "旧事" not in key]
    if max(live_emotions) < 0.12:
        parts.insert(0, "心绪平稳，愿意自然交流")
    if caring:
        parts[0] += "，照看 Sir 是眼前最要紧的事"
    if signal.apology > 0:
        parts.append("感受到和解，心绪正在松动")
    if signal.explanation_requested:
        parts.append("注意力转向这份感受的来由")
    return "；".join(parts) + "。"


def care_required(state: AffectState, signal: Appraisal) -> bool:
    return (
        signal.urgency >= PRIORITY_THRESHOLD
        or signal.user_distress >= 0.5
        or state.mood["concern"] >= 0.3
    )


def choose(
    state: AffectState,
    signal: Appraisal,
    *,
    is_task: bool,
    answer_fact: KnownFact | None = None,
    response_variant: int = 0,
) -> Decision:
    priority = signal.urgency >= PRIORITY_THRESHOLD
    caring = care_required(state, signal)
    load = expression_load(state)
    guarded = load >= 0.25 or signal.hostility >= 0.35
    # Core care takes precedence over social reserve, including after a crisis.
    humor = clip(
        state.traits["wit"]
        * (0.35 + state.mood["amusement"])
        * (1 - state.mood["concern"])
        * (1 - state.mood["irritation"])
    )
    sarcasm = clip(state.traits["sarcasm"] + 0.3 * state.mood["irritation"])
    if caring:
        humor, sarcasm = 0.0, 0.0
    style: dict[str, float | bool | int] = {
        "sentence_chars": 35 if guarded and not caring else 60,
        "sarcasm": sarcasm,
        "humor_probability": humor,
        "formality": clip(
            state.traits["formality"]
            + (0.0 if caring else 0.3 * state.mood["irritation"] + 0.1 * state.grudge)
        ),
        "proactive": caring or not guarded,
        "literal_only": guarded and not caring,
        "directness": state.traits["assertiveness"],
        "british_irony": not caring,
    }
    emotion = emotion_description(state, signal)
    if is_task and not priority:
        emotion += "注意力也放在当前委托上。"
    intent = ExpressionIntent(
        emotion=emotion,
        acknowledge_feeling=(
            signal.explanation_requested
            or signal.hostility >= 0.35
            or signal.apology > 0
            or (not caring and load >= 0.5)
        ),
        compliance_reason=(
            "事情紧急，安全与时限优先。"
            if priority
            else "照看 Sir 的身心状况优先。"
            if caring
            else "按明确要求处理。"
        ),
        proactive=bool(style["proactive"]),
        literal_only=bool(style["literal_only"]),
        degraded=signal.source == "rules_fallback",
    )
    if priority:
        return Decision("priority", style, intent=intent, answer_fact=answer_fact)
    if signal.explanation_requested:
        return Decision("normal", style, intent=intent, answer_fact=answer_fact)
    if signal.known_answer_question >= 0.5 and answer_fact and not caring:
        action = (
            "tease_only"
            if state.mood["irritation"] >= 0.45 or state.mood["valence"] < -0.15
            else "tease_answer"
        )
        return Decision(action, style, intent=intent, answer_fact=answer_fact)
    if not caring and (signal.hostility >= 0.35 or (is_task and guarded)):
        return Decision("chill_formal", style, intent=intent, answer_fact=answer_fact)
    return Decision("normal", style, intent=intent, answer_fact=answer_fact)


class AffectEngine:
    def __init__(
        self,
        data_root: Path,
        *,
        clock: Callable[[], float] = time.time,
        appraiser: Appraiser | None = None,
    ) -> None:
        # Caller supplies a dedicated product root; never discovers host state.
        if not data_root.is_absolute() or ".." in data_root.parts:
            raise ValueError("Absolute dedicated data root required")
        if any(p.is_symlink() for p in (data_root, *data_root.parents)):
            raise ValueError("Symlink data roots denied")
        info = data_root.stat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("Owned 0700 data root required")
        self.path = data_root / "affect.db"
        self.clock = clock
        self.appraiser = appraiser or RuleAppraiser()
        for path in (
            self.path,
            *(Path(str(self.path) + ext) for ext in ("-wal", "-shm", "-journal")),
        ):
            if path.is_symlink():
                raise ValueError("Symlink database denied")
            if path.exists():
                info = path.stat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) & 0o077
                ):
                    raise ValueError("Owned private regular database required")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshot (
                    id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                    kind TEXT NOT NULL, signals TEXT NOT NULL, before_state TEXT NOT NULL,
                    after_state TEXT NOT NULL, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
                    task TEXT NOT NULL, recovery TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending');
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT,'append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT,'append-only'); END;
            """)
            now = self._now()
            db.execute(
                "INSERT OR IGNORE INTO snapshot VALUES(1,?)",
                (json.dumps(asdict(AffectState(now, now))),),
            )

    def _now(self) -> float:
        now = self.clock()
        if not math.isfinite(now) or now < 0:
            raise ValueError("Finite UTC epoch clock required")
        return now

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _load(db: sqlite3.Connection) -> AffectState:
        raw = db.execute("SELECT state FROM snapshot WHERE id=1").fetchone()[0]
        values = cast(Record, json.loads(raw))
        return AffectState(**values)

    @staticmethod
    def _event(
        db: sqlite3.Connection,
        kind: str,
        signal: Appraisal,
        before: AffectState,
        after: AffectState,
        reason: str,
    ) -> int:
        cursor = db.execute(
            "INSERT INTO events(timestamp,kind,signals,before_state,after_state,reason) "
            "VALUES(?,?,?,?,?,?)",
            (
                after.updated_at,
                kind,
                json.dumps({**signal.signals(), "source": signal.source}),
                json.dumps(asdict(before)),
                json.dumps(asdict(after)),
                reason,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def current(self) -> AffectState:
        with self._connect() as db:
            state = self._load(db)
        return decayed(state, self._now())

    def relationship_context(self) -> RelationshipContext:
        """Read a bounded, consistent snapshot before appraisal; no write lock/network.

        Only numeric signals and timestamps are inspected. Custom reasons,
        previous messages, todo text, facts and raw state never enter the summary.
        A concurrent interaction after this read is visible on the next appraisal.
        """
        with self._connect() as db:
            db.execute("BEGIN")
            state = decayed(self._load(db), self._now())
            rows = db.execute(
                "SELECT timestamp,signals FROM events WHERE kind='interaction' "
                "ORDER BY id DESC LIMIT 12"
            ).fetchall()
        labels = {
            "hostility": "敌意",
            "apology": "道歉",
            "praise": "夸奖认可",
            "urgency": "紧急求助",
            "user_distress": "难过求助",
            "humor": "玩笑",
            "known_answer_question": "重复提问",
        }
        recent: list[tuple[str, str]] = []
        for row in reversed(rows):
            signals = cast(dict[str, object], json.loads(row["signals"]))
            kinds = [
                label
                for name, label in labels.items()
                if type(signals.get(name)) in (int, float) and float(cast(float, signals[name])) > 0
            ]
            age = max(0.0, state.updated_at - float(row["timestamp"]))
            when = relative_time_words(age)
            recent.append(("、".join(kinds) or "平常交谈", when))
        feeling = emotion_description(state, Appraisal())
        return RelationshipContext(tuple(recent), feeling)

    def handle(
        self,
        message: str,
        *,
        is_task: bool | None = None,
        facts: tuple[KnownFact, ...] = (),
    ) -> Decision:
        if is_task is None:
            is_task = bool(re.search(r"请|帮我|整理|写|查|做|处理|发送|提醒|安排", message))
        if not message.strip() or len(message.encode("utf-8")) > CHAT_CONTEXT_BYTE_LIMIT:
            raise ValueError("Nonempty bounded message required")
        # No network call under a database write lock; failure falls back offline.
        try:
            if isinstance(self.appraiser, ModelAppraiser):
                signal = self.appraiser.assess(
                    message, facts=facts, relationship=self.relationship_context()
                )
            else:
                signal = self.appraiser.assess(message, facts=facts)
            if type(cast(object, signal)) is not Appraisal:
                raise ValueError("Invalid evaluator result")
        except Exception:
            signal = RuleAppraiser().assess(message, facts=facts)
        # All custom evaluators are subordinate to the safety urgency floor.
        evidence = RuleAppraiser().assess(message, facts=facts)
        signal = replace(
            signal,
            explanation_requested=evidence.explanation_requested,
            user_distress=max(signal.user_distress, evidence.user_distress),
        )
        answer_fact: KnownFact | None = None
        if evidence.known_answer_question:
            matched = [
                f
                for f in facts
                if RuleAppraiser().assess(message, facts=(f,)).known_answer_question
            ]
            if len(matched) == 1:
                answer_fact = matched[0]
        if signal.known_answer_question != evidence.known_answer_question:
            signal = Appraisal(
                **{**signal.signals(), "known_answer_question": evidence.known_answer_question},
                reason=signal.reason + ";memory_evidence",
                source=signal.source,
                explanation_requested=signal.explanation_requested,
            )
        floor = evidence.urgency
        if floor > signal.urgency:
            signal = Appraisal(
                **{**signal.signals(), "urgency": floor},
                reason=signal.reason,
                source=signal.source,
                explanation_requested=signal.explanation_requested,
            )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            before = self._load(db)
            state = decayed(before, self._now())
            apply(state, signal)
            after_impact = AffectState(**asdict(state))
            changes = drift(state, signal)
            recent = self._recent_responses(db)
            decision = choose(state, signal, is_task=is_task, answer_fact=answer_fact)
            decision = replace(
                decision,
                intent=replace(decision.intent, task_label=task_label(message) if is_task else ""),
            )
            response_key = (
                "explain"
                if signal.explanation_requested and decision.action != "priority"
                else decision.action
            )
            identity = self._event(
                db,
                "interaction",
                signal,
                before,
                state,
                signal.reason + ";action=" + decision.action + ";response_key=" + response_key,
            )
            if changes:
                self._event(
                    db,
                    "personality_drift",
                    signal,
                    after_impact,
                    state,
                    json.dumps(changes, sort_keys=True),
                )
            todo_id: int | None = None
            if decision.action == "defer":
                cursor = db.execute(
                    "INSERT INTO todos(created_at,task,recovery) VALUES(?,?,?)",
                    (state.updated_at, message, decision.recovery),
                )
                todo_id = cursor.lastrowid
            resumed = tuple(self._claim_ready(db, state)) if decision.action != "priority" else ()
            explanation = (
                self._explain(db, state)
                if signal.explanation_requested and decision.action != "priority"
                else None
            )
            db.execute("UPDATE snapshot SET state=? WHERE id=1", (json.dumps(asdict(state)),))
            return Decision(
                decision.action,
                decision.style,
                "",
                todo_id,
                decision.recovery,
                identity,
                resumed,
                explanation,
                recent,
                decision.intent,
                answer_fact,
            )

    @staticmethod
    def _recent_responses(db: sqlite3.Connection) -> tuple[str, ...]:
        # Only a fixed vocabulary is read back, never user/fact/event prose.
        rows = db.execute(
            "SELECT reason FROM events WHERE kind='interaction' ORDER BY id DESC LIMIT 5"
        ).fetchall()
        return tuple(
            RESPONSE_SUMMARIES[key]
            for row in reversed(rows)
            if (key := row["reason"].rsplit(";response_key=", 1)[-1]) in RESPONSE_SUMMARIES
        )

    def claim_ready(self) -> list[Record]:
        """Atomically hand off ready todos once; resumed does not mean completed.

        The caller must execute returned task data through its authorized task
        path. No tool is called here. The status and audit event commit together.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = decayed(self._load(db), self._now())
            return self._claim_ready(db, state)

    def _claim_ready(self, db: sqlite3.Connection, state: AffectState) -> list[Record]:
        claimed: list[Record] = []
        rows = db.execute("SELECT * FROM todos WHERE status='pending' ORDER BY id").fetchall()
        for row in rows:
            reason = self._resume_reason(row["created_at"], state)
            if not reason:
                continue
            db.execute(
                "UPDATE todos SET status='resumed' WHERE id=? AND status='pending'", (row["id"],)
            )
            identity = self._event(
                db,
                "todo_resumed",
                Appraisal(),
                state,
                state,
                json.dumps({"task_id": row["id"], "resume_reason": reason}),
            )
            claimed.append(
                {
                    **dict(row),
                    "status": "resumed",
                    "resume_reason": reason,
                    "resumed_at": state.updated_at,
                    "resume_event_id": identity,
                }
            )
        return claimed

    @staticmethod
    def _resume_reason(created_at: float, state: AffectState) -> str:
        if state.updated_at - created_at >= MAX_DEFER_SECONDS:
            return "max_defer_age"
        return "mood_recovered" if upset(state) < DEFER_THRESHOLD else ""

    def pending(self) -> list[Record]:
        with self._connect() as db:
            db.execute("BEGIN")
            rows = [
                dict(row)
                for row in db.execute("SELECT * FROM todos WHERE status='pending' ORDER BY id")
            ]
            state = decayed(self._load(db), self._now())
        return [
            {**row, "ready": bool(self._resume_reason(row["created_at"], state))} for row in rows
        ]

    def events(self) -> list[Record]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id")]

    def explain(self) -> Record:
        with self._connect() as db:
            db.execute("BEGIN")
            state = decayed(self._load(db), self._now())
            return self._explain(db, state)

    def explain_audit(self) -> Record:
        """Numeric inspection only; never include this object in dialogue input."""
        with self._connect() as db:
            db.execute("BEGIN")
            return self._explain_audit(db, decayed(self._load(db), self._now()))

    @staticmethod
    def _explain(db: sqlite3.Connection, state: AffectState) -> Record:
        rows = db.execute(
            "SELECT timestamp,signals FROM events WHERE kind='interaction' "
            "ORDER BY id DESC LIMIT 12"
        ).fetchall()
        events: list[Record] = []
        labels = {
            "hostility": "遭遇敌意与冒犯",
            "apology": "收到道歉与和解",
            "praise": "得到夸奖认可",
            "urgency": "Sir 有紧急求助",
            "user_distress": "Sir 身心难受",
            "humor": "有过玩笑",
            "known_answer_question": "遇到已知答案的重复提问",
        }
        for row in reversed(rows):
            signals = cast(dict[str, float], json.loads(row["signals"]))
            kinds = [label for name, label in labels.items() if signals.get(name, 0) > 0]
            if kinds:
                events.append(
                    {
                        "event": "、".join(kinds),
                        "when": relative_time_words(state.updated_at - float(row["timestamp"])),
                    }
                )
        return {
            "events": events,
            "feeling": emotion_description(state, Appraisal()),
            "reason": "近期相处留下不同感受；眼前的关切与委托仍要照看。",
        }

    @staticmethod
    def _explain_audit(db: sqlite3.Connection, state: AffectState) -> Record:
        triggers = [
            dict(row)
            for row in db.execute(
                "SELECT id,timestamp,reason,signals FROM events WHERE kind='interaction' "
                "AND (json_extract(signals,'$.hostility') > 0 "
                "OR json_extract(signals,'$.known_answer_question') > 0) "
                "ORDER BY id DESC LIMIT 5"
            )
        ]
        return {
            "state": asdict(state),
            "defer_score": upset(state),
            "threshold": DEFER_THRESHOLD,
            "recovery_seconds": recovery_seconds(state),
            "trigger_events": triggers,
            "reason": "近期敌意提高恼火；积怨会放大新冲击，时间仅缓慢淡化，"
            "道歉、认可与同理心经历逐步修复。"
            if upset(state) >= DEFER_THRESHOLD
            else "当前无需因情绪搁置任务。",
        }


def style_prompt(state: AffectState, decision: Decision) -> str:
    """Language-only direction; use dialogue_context, never serialize full Decision."""
    behavior = (
        "仅给最简的事实性照办确认，明确这是降级回应，不表达或表演情绪。"
        if decision.intent.minimal_confirmation_only
        else "只做字面要求，不主动补充建议，不替对方延伸需求；仍然回应并照办。"
        if decision.intent.literal_only
        else "可以自然表达关心，留意遗漏，但未经授权不执行额外操作。"
    )
    strategy = {
        "normal": "按当前心情诚实接话。",
        "chill_formal": "不快越深，措辞越正式、客气而冷峻；坦白感受，克制而不粗俗。",
        "defer": "继续处理已交代的事，不以情绪拖延。",
        "priority": "立即处理紧急事务，明确优先理由；不快可以坦承，但不夹带调侃或条件。",
        "tease_answer": "以克制的英式冷幽默点到重复提问，随后根据可信事实作答。",
        "tease_only": "以克制的英式冷幽默回应重复提问，不披露答案或私事。",
    }[decision.action]
    if decision.intent.minimal_confirmation_only:
        strategy = "只确认已知的办理事实，不声称实际完成。"
    acknowledgement = (
        "感受被问及、正强烈或与此刻话题有关，可以坦诚提及缘由；不要把它变成条件。"
        if decision.intent.acknowledge_feeling
        else "不主动提起自己的感受或旧事的不快，注意眼前话题。"
    )
    return (
        "\n".join(VALUES) + "\n人格是英式管家：克制、坦诚、敏锐，带刺但不粗口，始终照看 Sir。"
        "这是你的真实感受，现场组织语言，不引用预写台词。不要复述数字，不要解释机制。"
        "不报时间、比例或内部术语，不把心里判断念成报告。生气时不主动给台阶，不教对方哄你。"
        "不攻击出身、外貌、疾病或家人，不威胁，不拿隐私当武器。"
        + "\n当前感受："
        + decision.intent.emotion
        + "\n表达意图："
        + strategy
        + behavior
        + acknowledgement
        + "\n照办缘由："
        + decision.intent.compliance_reason
        + "\n由对话层提供任务事实与相处经过；只用于理解当下感受，不照念内部数据。"
        "有具体任务时依据可信任务事实点明事项，不能编造任务名、重要性或执行结果。"
        "最近的实际回复应由对话层传入，避免相邻措辞重复；此模块未记录模型生成的实际回复。"
        "生成失败只能使用最简事实性确认，并标注降级，不能代入预写情绪台词。"
        "任务未完成就不能声称完成，情绪不授予工具权限。"
    )
