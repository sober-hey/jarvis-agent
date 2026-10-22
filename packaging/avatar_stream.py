"""Bounded, numeric avatar read projection; never initializes product stores.

Affect uses the existing pure decay calculation over a read-only snapshot.
Daemon/mind projections expose only enum/boolean facts, never their prose.
Voice levels are an optional in-process callback, not a writable HTTP route.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import avatar_signals as cues
from affect_engine import AffectState, decayed

MAX_CONNECTIONS = 4
PUSH_INTERVAL = 0.2  # Hard cap: 5 frames/second/client; no catch-up bursts.
STREAM_SECONDS = 60.0  # Force native EventSource reconnect and release idle slots.
VOICE_TTL = 0.75
ACTIONS = {"normal", "priority", "chill_formal", "defer", "tease_answer", "tease_only"}


def _safe_file(path: Path) -> bool:
    if any(p.is_symlink() for p in (path, *path.parents)):
        return False
    try:
        info = path.stat()
        return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
    except OSError:
        return False


def _database(path: Path) -> sqlite3.Connection:
    if not _safe_file(path) or any(
        not _safe_file(p)
        for p in (Path(str(path) + ext) for ext in ("-wal", "-shm"))
        if p.exists() or p.is_symlink()
    ):
        raise ValueError("unavailable database")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.05)
    db.execute("PRAGMA query_only=ON")
    return db


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("finite number required")
    return float(value)


class AvatarSource:
    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.root, self.clock = root, clock
        self.lock = threading.Lock()
        self.voice_level = 0.0
        self.voice_seen: float | None = None
        self.sequence = 0

    def on_audio_level(self, speaker: str, level: float) -> None:
        """Compatible with voice_port.BrainSink(on_level=...); user is ignored.

        There is no playback inference from generated text or synthesis duration.
        A disconnected/stalled callback expires to silence within VOICE_TTL.
        """
        if speaker != "assistant":
            return
        value = max(0.0, min(1.0, _number(level)))
        with self.lock:
            self.voice_level, self.voice_seen = value, time.monotonic()

    def affect(self, now: float) -> tuple[cues.AffectSnapshot, str]:
        with closing(_database(self.root / "affect.db")) as db:
            row = db.execute(
                "SELECT state FROM snapshot WHERE id=1 AND length(state)<=32768"
            ).fetchone()
            if row is None:
                raise ValueError("missing state")
            values: dict[str, Any] = json.loads(row[0])
            # Same decay as AffectEngine.current(), without constructor/create/write.
            state = decayed(AffectState(**values), now)
            snapshot = cues.AffectSnapshot(
                {
                    k: _number(state.mood.get(k, 0))
                    for k in ("irritation", "hurt", "concern", "amusement", "pride", "arousal")
                },
                {k: _number(state.traits[k]) for k in cues.TRAIT_DEFAULTS},
                _number(state.grudge),
            )
            row = db.execute(
                "SELECT substr(reason,instr(reason,';action=')+8,32),timestamp "
                "FROM events WHERE kind='interaction' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            action = "normal"
            if row and 0 <= now - _number(row[1]) <= 300:
                candidate = str(row[0]).split(";", 1)[0]
                if candidate in ACTIONS:
                    action = candidate
            return snapshot, action

    def daemon_activity(self, now: float) -> str:
        path = self.root / "daemon.heartbeat"
        if not _safe_file(path):
            raise ValueError("missing heartbeat")
        with path.open() as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("heartbeat size")
        data = json.loads(raw)
        # Daemon writes before/after a stage, whose model budget is 2100s.
        phase = data["phase"]
        model_phase = phase in {"think", "reflection", "tasks", "dispatch"}
        ttl = 2130 if model_phase else 660
        if not 0 <= now - _number(data["wall_time"]) <= ttl or phase == "stopped":
            raise ValueError("stale heartbeat")
        return "thinking" if model_phase else "idle"

    def proactive(self, now: float) -> bool:
        with closing(_database(self.root / "mind.db")) as db:
            row = db.execute(
                "SELECT created_at FROM entries WHERE category IN "
                "('action','action_request','question_generation_intent') ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return False
            from datetime import datetime

            age = now - datetime.fromisoformat(row[0]).timestamp()
            return 0 <= age <= 300

    def frame(self, *, thinking: bool = False) -> dict[str, Any]:
        now = self.clock()
        missing: list[str] = []
        snapshot, action, activity, proactive = None, "normal", "idle", False
        errors = (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            OverflowError,
            AttributeError,
            RecursionError,
            sqlite3.Error,
        )
        try:
            snapshot, action = self.affect(now)
        except errors:
            missing.append("affect")
        try:
            activity = self.daemon_activity(now)
        except errors:
            if not thinking:
                missing.append("daemon_activity")
        try:
            proactive = self.proactive(now)
        except errors:
            missing.append("proactive_intent")
        with self.lock:
            voice_live = (
                self.voice_seen is not None and time.monotonic() - self.voice_seen <= VOICE_TTL
            )
            level = self.voice_level if voice_live else 0.0
            self.sequence += 1
            sequence = self.sequence
        if not voice_live:
            missing.append("voice_level")
        activity = "speaking" if level > 0 else "thinking" if thinking else activity
        if snapshot is not None:
            turn = cues.TurnSignal(action, cues.TurnIntent(proactive=proactive))
            stream = cues.build_signals(snapshot, turn, [], idle_seconds=0)
            channels = {item["type"]: item["parameters"] for item in stream}
        else:
            channels = {
                "expression": dict.fromkeys(cues.EXPRESSION_RANGES, 0.0),
                "posture": dict.fromkeys(cues.POSTURE_RANGES, 0.0),
                "gaze": dict.fromkeys(cues.GAZE_RANGES, 0.0),
                "idle": dict.fromkeys(cues.IDLE_RANGES, 0.0),
            }
        channels["mouth"] = {"open": level, "level": level}
        return {
            "schema_version": 1,
            "sequence": sequence,
            "timestamp": now,
            "degraded": bool(missing),
            "missing_sources": missing,
            "activity": activity,
            "action": action,
            "channels": channels,
        }


def self_evaluate() -> dict[str, Any]:
    """Exercise only an ephemeral callback; never call frame/read product files."""
    from datetime import datetime, timezone

    source = AvatarSource(Path(__file__).parent, clock=lambda: 0.0)
    source.on_audio_level("assistant", 2.0)
    clamped = source.voice_level
    source.on_audio_level("user", 0.0)
    user_ignored = source.voice_level == clamped
    nonfinite_rejected = False
    try:
        source.on_audio_level("assistant", float("nan"))
    except ValueError:
        nonfinite_rejected = True
    return {
        "module": "avatar_stream",
        "domain": "read-only numeric avatar projection and expiring voice levels",
        "current_version": "1",
        "version_scope": "frame schema and offline evaluation contract",
        "known_limitations": [
            "This hook does not read affect.db, mind.db or daemon heartbeat state.",
            "Voice levels require an injected callback; generated text is not playback evidence.",
            "SSE delivery and resident source wiring are not verified by callback probes.",
        ],
        "improvement_directions": [
            "Verify source freshness and playback callback wiring in isolated integration tests."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_verified": False,
        "scope": "in-memory audio callback validation only",
        "limits": {
            "push_interval_seconds": PUSH_INTERVAL,
            "max_connections": MAX_CONNECTIONS,
            "stream_seconds": STREAM_SECONDS,
            "voice_ttl_seconds": VOICE_TTL,
        },
        "offline_probe": {
            "assistant_level_after_clamp": clamped,
            "user_level_ignored": user_ignored,
            "nonfinite_level_rejected": nonfinite_rejected,
        },
    }
