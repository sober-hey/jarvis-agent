"""Append-only voice observations on the authoritative MindStore.entries timeline.

Durations are milliseconds; event timestamps are timezone-aware ISO-8601 strings
(normalized to UTC). MindStore.created_at separately records ingestion time.
A turn starts with record_user; its returned payload.turn_id links all following
observations. Every method returns the ordinary MindStore.append record.

TTS outcomes use voice_assistant/event=tts, not a fifth category. Playback start,
end and interruption are separate observations: interruption never fabricates an
end callback or cancels a task. The query projection is reconstructed, not saved.
This module does not capture audio, synthesize speech or wire a running backend.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from inner_loop import MindStore, Record

VOICE_CATEGORIES = ("voice_user", "voice_assistant", "voice_playback", "voice_interrupt")
_CATEGORY_SQL = "category IN ('voice_user','voice_assistant','voice_playback','voice_interrupt')"


def _text(value: object, field: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{field} exceeds its length limit")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return value


def _timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    _text(value, "timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _decode(row: sqlite3.Row) -> Record:
    return {
        **dict(row),
        "payload": json.loads(row["payload"]),
        "refs": json.loads(row["refs"]),
    }


class VoiceLedger:
    """Use an existing MindStore; no second database or mutable turn table.

    Connections are owned/closed by MindStore. There is no shared SQLite
    connection or mutable cache: threads/processes inherit its WAL/busy timeout.
    References are checked before append, and remain valid because entries are
    immutable. Repeated callbacks are retained as new observations, not deduped.
    """

    def __init__(self, store: MindStore) -> None:
        self.store = store
        # The existing category/seq index serves recent turn discovery. This
        # expression index bounds exact-turn reads without introducing storage.
        with store.connection() as db:
            db.execute(
                "CREATE INDEX IF NOT EXISTS entries_voice_turn_seq "
                "ON entries(category,json_extract(payload,'$.turn_id'),seq)"
            )

    def record_user(
        self,
        text: str,
        *,
        device_id: str,
        asr_confidence: float | None,
        audio_duration_ms: float,
        timestamp: str | None = None,
    ) -> Record:
        """Start a new turn with a final ASR observation; unknown confidence=None."""
        _text(text, "text")
        _text(device_id, "device_id", maximum=200)
        _number(audio_duration_ms, "audio_duration_ms")
        if asr_confidence is not None:
            _number(asr_confidence, "asr_confidence")
            if asr_confidence > 1:
                raise ValueError("asr_confidence must be in 0..1")
        return self.store.append(
            "voice_user",
            {
                "turn_id": uuid.uuid4().hex,
                "event": "transcript",
                "speaker": "user",
                "text": text,
                "timestamp": _timestamp(timestamp),
                "device_id": device_id,
                "asr_confidence": asr_confidence,
                "audio_duration_ms": audio_duration_ms,
            },
            source=device_id,
        )

    def record_assistant(
        self,
        turn_id: str,
        text: str,
        *,
        generation_ms: float,
        timestamp: str | None = None,
    ) -> Record:
        """Append generated answer text, even if it will never be played."""
        _text(text, "text")
        _number(generation_ms, "generation_ms")
        user = self._user(turn_id)
        return self.store.append(
            "voice_assistant",
            {
                "turn_id": turn_id,
                "event": "response",
                "speaker": "jarvis",
                "text": text,
                "generation_ms": generation_ms,
                "timestamp": _timestamp(timestamp),
            },
            source="jarvis",
            refs=[user["id"]],
        )

    def record_tts(
        self,
        turn_id: str,
        assistant_id: str,
        *,
        status: str,
        voice_id: str,
        audio_duration_ms: float | None = None,
        error: str | None = None,
        timestamp: str | None = None,
    ) -> Record:
        """Append one synthesis outcome; failed/unknown duration stays None.

        error should be a sanitized error code, never credentials/provider dumps.
        """
        if status not in {"success", "failed"}:
            raise ValueError("TTS status must be success or failed")
        _text(voice_id, "voice_id")
        if audio_duration_ms is not None:
            _number(audio_duration_ms, "audio_duration_ms")
        elif status == "success":
            raise ValueError("Successful TTS requires audio_duration_ms")
        if error is not None:
            _text(error, "error")
        user = self._user(turn_id)
        self._target(turn_id, assistant_id, "voice_assistant", "response")
        return self.store.append(
            "voice_assistant",
            {
                "turn_id": turn_id,
                "event": "tts",
                "speaker": "jarvis",
                "assistant_id": assistant_id,
                "status": status,
                "voice_id": voice_id,
                "audio_duration_ms": audio_duration_ms,
                "error": error,
                "timestamp": _timestamp(timestamp),
            },
            source="tts",
            refs=[user["id"], assistant_id],
        )

    def record_playback_start(
        self,
        turn_id: str,
        assistant_id: str,
        *,
        device_id: str,
        timestamp: str | None = None,
    ) -> Record:
        """The returned entry id is the playback_id for end/interrupt callbacks."""
        _text(device_id, "device_id", maximum=200)
        user = self._user(turn_id)
        self._target(turn_id, assistant_id, "voice_assistant", "response")
        return self.store.append(
            "voice_playback",
            {
                "turn_id": turn_id,
                "event": "start",
                "assistant_id": assistant_id,
                "device_id": device_id,
                "timestamp": _timestamp(timestamp),
            },
            source=device_id,
            refs=[user["id"], assistant_id],
        )

    def record_playback_end(
        self,
        turn_id: str,
        playback_id: str,
        *,
        interrupted: bool = False,
        timestamp: str | None = None,
    ) -> Record:
        """Record actual end callback; an interrupt observation is not one."""
        if type(interrupted) is not bool:
            raise ValueError("interrupted must be a bool")
        user = self._user(turn_id)
        start = self._target(turn_id, playback_id, "voice_playback", "start")
        ended_at = _timestamp(timestamp)
        if datetime.fromisoformat(ended_at) < datetime.fromisoformat(start["payload"]["timestamp"]):
            raise ValueError("Playback cannot end before it starts")
        return self.store.append(
            "voice_playback",
            {
                "turn_id": turn_id,
                "event": "end",
                "playback_id": playback_id,
                "assistant_id": start["payload"]["assistant_id"],
                "device_id": start["payload"]["device_id"],
                "timestamp": ended_at,
                "interrupted": interrupted,
            },
            source=start["payload"]["device_id"],
            refs=[user["id"], playback_id],
        )

    def record_interrupt(
        self,
        turn_id: str,
        *,
        interrupted_by: str,
        target_id: str,
        device_id: str | None = None,
        interrupting_turn_id: str | None = None,
        timestamp: str | None = None,
    ) -> Record:
        """Link who interrupted which reply/playback and optionally the new turn.

        This only records an observation. It does not interrupt generation, cancel
        work, revoke intent or change any task state. Backend policy is separate.
        """
        _text(interrupted_by, "interrupted_by", maximum=200)
        if device_id is not None:
            _text(device_id, "device_id", maximum=200)
        user = self._user(turn_id)
        target = self._entry(target_id)
        if (
            target is None
            or target["payload"].get("turn_id") != turn_id
            or (target["category"], target["payload"].get("event"))
            not in {("voice_assistant", "response"), ("voice_playback", "start")}
        ):
            raise ValueError("Interrupt target must be this turn's reply or playback start")
        refs = [user["id"], target_id]
        if interrupting_turn_id is not None:
            refs.append(self._user(interrupting_turn_id)["id"])
        return self.store.append(
            "voice_interrupt",
            {
                "turn_id": turn_id,
                "event": "interrupt",
                "interrupted_by": interrupted_by,
                "target_id": target_id,
                "target_category": target["category"],
                "device_id": device_id,
                "interrupting_turn_id": interrupting_turn_id,
                "timestamp": _timestamp(timestamp),
            },
            source=device_id if device_id is not None else interrupted_by,
            refs=list(dict.fromkeys(refs)),
        )

    def _entry(self, entry_id: str) -> Record | None:
        _text(entry_id, "entry_id")
        with self.store.connection() as db:
            row = db.execute(
                f"SELECT * FROM entries WHERE id=? AND {_CATEGORY_SQL}", (entry_id,)
            ).fetchone()
        return _decode(row) if row is not None else None

    def _user(self, turn_id: str) -> Record:
        _text(turn_id, "turn_id")
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM entries WHERE category='voice_user' "
                "AND json_extract(payload,'$.turn_id')=? ORDER BY seq LIMIT 1",
                (turn_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Unknown voice turn")
        return _decode(row)

    def _target(self, turn_id: str, entry_id: str, category: str, event: str) -> Record:
        row = self._entry(entry_id)
        if (
            row is None
            or row["category"] != category
            or row["payload"].get("event") != event
            or row["payload"].get("turn_id") != turn_id
        ):
            raise ValueError("Reference must identify the expected event in this voice turn")
        return row

    def recent_voice_turns(self, limit: int = 20) -> list[Record]:
        """Newest turns first, ordered by user ingress seq, not late callbacks.

        limit counts turns, not events. All observations belonging to each selected
        turn are included. One read snapshot avoids mixing concurrent appends.
        """
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a nonnegative integer")
        if limit == 0:
            return []
        with self.store.connection() as db:
            db.execute("BEGIN")
            users = db.execute(
                "SELECT payload FROM entries WHERE category='voice_user' ORDER BY seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                self._project(self._turn_events(db, json.loads(user["payload"])["turn_id"]))
                for user in users
            ]

    def get_turn(self, turn_id: str) -> Record | None:
        """Return a detached aggregate with raw events, or None for an unknown ID."""
        _text(turn_id, "turn_id")
        with self.store.connection() as db:
            events = self._turn_events(db, turn_id)
        if not any(row["category"] == "voice_user" for row in events):
            return None
        return self._project(events)

    @staticmethod
    def _turn_events(db: sqlite3.Connection, turn_id: str) -> list[Record]:
        return [
            _decode(row)
            for row in db.execute(
                f"SELECT * FROM entries WHERE {_CATEGORY_SQL} "
                "AND json_extract(payload,'$.turn_id')=? ORDER BY seq",
                (turn_id,),
            )
        ]

    @staticmethod
    def _project(events: list[Record]) -> Record:
        user = next(row for row in events if row["category"] == "voice_user")
        assistant: list[Record] = []
        tts: list[Record] = []
        interrupts: list[Record] = []
        playback: dict[str, Record] = {}
        for row in events:
            payload = row["payload"]
            category = row["category"]
            event = payload["event"]
            if category == "voice_assistant":
                (assistant if event == "response" else tts).append(row)
            elif category == "voice_playback" and event == "start":
                playback[row["id"]] = {
                    "playback_id": row["id"],
                    "assistant_id": payload["assistant_id"],
                    "device_id": payload["device_id"],
                    "started_at": payload["timestamp"],
                    "ended_at": None,
                    "status": "playing",
                    "interrupted": False,
                    "interrupt_ids": [],
                }
            elif category == "voice_playback" and event == "end":
                state = playback.get(payload["playback_id"])
                if state is not None:
                    state["ended_at"] = payload["timestamp"]
                    state["status"] = "ended"
                    state["interrupted"] = state["interrupted"] or payload["interrupted"]
            elif category == "voice_interrupt":
                interrupts.append(row)
                for state in playback.values():
                    if state["playback_id"] == payload["target_id"] or (
                        state["assistant_id"] == payload["target_id"]
                        and state["status"] == "playing"
                    ):
                        state["interrupted"] = True
                        state["interrupt_ids"].append(row["id"])
        return {
            "turn_id": user["payload"]["turn_id"],
            "user": user,
            "assistant": assistant,
            "tts": tts,
            "playback": list(playback.values()),
            "interrupts": interrupts,
            "events": events,
        }


def self_evaluate() -> dict[str, Any]:
    """Static contract description, not a claim of real microphone integration."""
    return {
        "module": "voice_ledger",
        "domain": "append-only voice observations on the single mind timeline",
        "current_version": "1.0.0",
        "known_limitations": [
            "Not wired to a resident voice backend by this module.",
            "Observations depend on caller-supplied metadata, not verified physical playback.",
            "Repeated callbacks append new observations; no exactly-once callback guarantee.",
            "A turn starts with a user transcript; unsolicited assistant speech is not supported.",
        ],
        "improvement_directions": [
            "Wire ASR, generated text, TTS, playback and interruption callbacks at assembly.",
            "Define unsolicited speech turn origins at the existing voice composition boundary.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }
