"""Durable, local-only outreach queue with replayable SSE and draft email.

Call Outreach(data_root).enqueue(...) / .dispatch_due(now) from the owner loop.
No scheduler, listener, SMTP client, or background thread starts on import.
Python 3.12+. sent means handed to the durable local channel, not read by Sir.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
DEDUPE_WINDOW = timedelta(hours=6)
LOW_HOURLY_LIMIT = 1
REGULAR_HOURLY_LIMIT = 2  # low + normal together; rolling hour, not clock hour
# Proactive attention budget: high is still non-emergency; two/hour and eight
# per CST calendar day prevent repeated unsolicited interruptions, not replies.
PROACTIVE_HOURLY_LIMIT = 2
PROACTIVE_DAILY_LIMIT = 8
PROACTIVE_OFFLINE_LIMIT = 1  # one per absence, not per dispatch or process
QUIET_END_MINUTE = 7 * 60 + 30  # midnight through 07:30 CST, end exclusive
STUDY_DIGEST_HOUR = 18
SSE_POLL_SECONDS = 0.1
SSE_HEARTBEAT_SECONDS = 10.0
DEFAULT_RECIPIENT = "sir@example.invalid"
DEFAULT_SENDER = "jarvis@example.invalid"

# iMessage 出站：脚本路径与收件号码属于部署环境信息，不写死在源码里。
# 真机上用环境变量覆盖；未配置时该通道不可用（调用方应据此降级到 web/email）。
IM_SEND_SCRIPT = Path(
    os.environ.get("JARVIS_IM_SEND_SCRIPT", str(Path.home() / ".hermes/scripts/im_send.py"))
)
IMESSAGE_SIR = os.environ.get("JARVIS_IMESSAGE_SIR", "+8610000000000")
type Urgency = Literal["low", "normal", "high", "emergency"]
type Channel = Literal["web", "email", "both", "voice", "imessage", "auto"]
type Status = Literal["queued", "sent", "suppressed", "failed"]
type MessageRow = tuple[str, str, str, str, str, str, str | None, int]
type DeliveryRow = tuple[int, str, str, str, str, str, str, str, str]


@dataclass(frozen=True)
class Message:
    id: str
    created_at: str
    urgency: Urgency
    reason: str
    text: str
    channel: Channel
    dedupe_key: str | None
    studying: bool
    status: Status


@dataclass(frozen=True)
class StateEvent:
    id: int
    message_id: str
    created_at: str
    status: Status
    detail: str


@dataclass(frozen=True)
class Delivery:
    id: int
    key: str
    created_at: str
    urgency: Urgency
    reason: str
    text: str
    channel: Channel
    kind: str
    message_ids: tuple[str, ...]


@dataclass(frozen=True)
class SirState:
    """Supplied by the caller, never inferred from a chat window or session."""

    studying: bool = False
    voice_available: bool = False
    imessage_available: bool = False
    web_available: bool = True
    can_interrupt: bool = True


def select_channel(urgency: Urgency, state: SirState) -> Channel:
    """Voice-first when audible and interruptible; all adapters remain local."""
    if urgency == "emergency":
        if state.voice_available:
            return "voice"
        if state.imessage_available:
            return "imessage"
    elif state.voice_available and state.can_interrupt and not state.studying:
        return "voice"
    if urgency == "high" and state.imessage_available:
        return "imessage"
    return "web" if state.web_available else "email"


class ChannelAdapter(Protocol):
    def deliver(self, delivery: Delivery, data_root: Path) -> None: ...


class FakeVoiceAdapter:
    """Records a request only; never calls TTS, audio devices or subprocesses."""

    def deliver(self, delivery: Delivery, data_root: Path) -> None:
        _write_stub(delivery, data_root, "voice")


class RealIMessageAdapter:
    """Explicit opt-in transport; errors are audited, never raised or retried.

    The channel protocol returns None, so queue ``sent`` remains a handoff,
    not a transport receipt. Read imessage.log for the actual send outcome.
    """

    def deliver(self, delivery: Delivery, data_root: Path) -> None:
        record: dict[str, Any] = {
            "created_at": datetime.now(CST).isoformat(),
            "delivery_key": delivery.key,
            "ok": False,
        }
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(IM_SEND_SCRIPT),
                    "--to",
                    IMESSAGE_SIR,
                    "--signature",
                    "JARVIS",
                    delivery.text,
                ],
                cwd=str(IM_SEND_SCRIPT.parent),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            record["returncode"] = result.returncode
            record["stderr"] = result.stderr.strip()
            payload: object = json.loads(result.stdout)
            record["result"] = payload
            record["ok"] = (
                result.returncode == 0
                and isinstance(payload, dict)
                and cast(dict[str, Any], payload).get("ok") is True
            )
        except Exception as error:
            record["error_type"] = type(error).__name__
            record["error"] = str(error)
        try:
            data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(data_root / "imessage.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            logging.getLogger(__name__).exception("Cannot write iMessage delivery audit")


class FakeIMessageAdapter:
    """Records a request only; never accesses Messages, imsg or AppleScript."""

    def deliver(self, delivery: Delivery, data_root: Path) -> None:
        _write_stub(delivery, data_root, "imessage")


def _write_stub(delivery: Delivery, data_root: Path, channel: str) -> None:
    directory = data_root / "channel-stubs" / channel
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps({"fake": True, "delivery": asdict(delivery)}, ensure_ascii=False)
    target = directory / f"{delivery.key}.json"
    try:
        with target.open("x", encoding="utf-8") as stream:
            stream.write(payload)
    except FileExistsError:
        if target.read_text(encoding="utf-8") != payload:
            raise ValueError("fake delivery conflicts with durable request") from None


type DigestBuilder = Callable[[tuple[Message, ...], str], str]


class EmailSender(Protocol):
    """Future explicit integration point; no default or SMTP implementation."""

    def send(self, message: EmailMessage) -> None: ...


def send_email(message: EmailMessage, sender: EmailSender, *, enabled: bool = False) -> None:
    """Reserved opt-in transport boundary; dispatch_due never calls this."""
    if not enabled:
        raise PermissionError("Real email is disabled; use the outbox draft")
    sender.send(message)


def _now(value: datetime | None) -> datetime:
    result = value if value is not None else datetime.now(CST)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return result.astimezone(CST)


def _delivery(row: DeliveryRow) -> Delivery:
    ids = cast(list[str], json.loads(row[8]))
    return Delivery(
        row[0],
        row[1],
        row[2],
        cast(Urgency, row[3]),
        row[4],
        row[5],
        cast(Channel, row[6]),
        row[7],
        tuple(ids),
    )


def compose_email(delivery: Delivery, recipient: str = DEFAULT_RECIPIENT) -> EmailMessage:
    mail = EmailMessage(policy=policy.SMTP)
    mail["From"] = DEFAULT_SENDER
    mail["To"] = recipient
    mail["Date"] = format_datetime(datetime.fromisoformat(delivery.created_at))
    mail["Message-ID"] = f"<{delivery.key}@jarvis.invalid>"
    mail["Subject"] = delivery.reason
    mail["X-JARVIS-Outbox-Only"] = "yes"
    mail["X-JARVIS-Urgency"] = delivery.urgency
    mail.set_content(delivery.text)
    return mail


class Outreach:
    """One data-root queue; SQLite serializes writers across processes.

    studying is caller supplied, persisted with each item. dispatch_due can
    override it for current context. Digests keep requested channels separate
    to avoid disclosing email-only content on the web. The study limit is one
    logical delivery per day globally; other channel batches remain queued.
    A caller digest_builder must author aggregate words; without one, digest
    items remain queued. Failed items need operator reconciliation and are
    never automatically retried. Injected adapters are an explicit trust
    boundary; defaults only record fake requests, never deliver externally.
    """

    def __init__(
        self,
        data_root: Path | str,
        *,
        recipient: str = DEFAULT_RECIPIENT,
        digest_builder: DigestBuilder | None = None,
        voice_adapter: ChannelAdapter | None = None,
        imessage_adapter: ChannelAdapter | None = None,
    ) -> None:
        self.root = Path(data_root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.root / "outreach.db"
        self.recipient = recipient
        self.digest_builder = digest_builder
        self.voice_adapter: ChannelAdapter = voice_adapter or FakeVoiceAdapter()
        self.imessage_adapter: ChannelAdapter = imessage_adapter or FakeIMessageAdapter()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    urgency TEXT NOT NULL, reason TEXT NOT NULL, text TEXT NOT NULL,
                    channel TEXT NOT NULL, dedupe_key TEXT, studying INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL
                    REFERENCES messages(id), created_at TEXT NOT NULL,
                    status TEXT NOT NULL, detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL, urgency TEXT NOT NULL,
                    reason TEXT NOT NULL, text TEXT NOT NULL, channel TEXT NOT NULL,
                    kind TEXT NOT NULL, message_ids TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS event_message ON events(message_id,id);
                CREATE INDEX IF NOT EXISTS message_dedupe ON messages(dedupe_key);
                CREATE TABLE IF NOT EXISTS intents (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', generated TEXT, claimed_at TEXT,
                    delivery_key TEXT
                );
                CREATE TABLE IF NOT EXISTS intent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, intent_id TEXT NOT NULL
                    REFERENCES intents(id), payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_presence (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), offline_sent INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO intent_presence VALUES(1,0);
                CREATE TABLE IF NOT EXISTS delivery_metadata (
                    key TEXT PRIMARY KEY REFERENCES deliveries(key), payload TEXT NOT NULL
                );
            """)
            for table in ("messages", "events", "deliveries", "intent_events", "delivery_metadata"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                        f"BEFORE {action} ON {table} BEGIN SELECT "
                        f"RAISE(ABORT, 'append-only {table}'); END"
                    )
        self.db_path.chmod(0o600)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=15)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _event(
        db: sqlite3.Connection, message_id: str, now: datetime, status: Status, detail: str
    ) -> None:
        db.execute(
            "INSERT INTO events(message_id,created_at,status,detail) VALUES(?,?,?,?)",
            (message_id, now.isoformat(), status, detail),
        )

    def enqueue(
        self,
        text: str,
        *,
        reason: str,
        urgency: Urgency = "normal",
        channel: Channel = "auto",
        dedupe_key: str | None = None,
        studying: bool = False,
        state: SirState | None = None,
        now: datetime | None = None,
    ) -> str:
        moment = _now(now)
        if urgency not in ("low", "normal", "high", "emergency"):
            raise ValueError("invalid urgency")
        if channel not in ("web", "email", "both", "voice", "imessage", "auto"):
            raise ValueError("invalid channel")
        if not text.strip() or not reason.strip() or dedupe_key == "":
            raise ValueError("text, reason and supplied dedupe_key must not be empty")
        if "\r" in reason or "\n" in reason:
            raise ValueError("reason must be a single line")
        message_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = False
            if dedupe_key is not None and urgency != "emergency":
                duplicate = (
                    db.execute(
                        """
                    SELECT 1 FROM messages m WHERE dedupe_key=?
                    AND created_at<=? AND urgency!='emergency'
                    AND ((SELECT status FROM events WHERE message_id=m.id
                          ORDER BY id DESC LIMIT 1)='queued'
                         OR EXISTS(SELECT 1 FROM events e WHERE e.message_id=m.id
                                   AND e.status='sent' AND e.created_at>? AND e.created_at<=?))
                    LIMIT 1
                """,
                        (
                            dedupe_key,
                            moment.isoformat(),
                            (moment - DEDUPE_WINDOW).isoformat(),
                            moment.isoformat(),
                        ),
                    ).fetchone()
                    is not None
                )
            db.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
                (
                    message_id,
                    moment.isoformat(),
                    urgency,
                    reason,
                    text,
                    channel,
                    dedupe_key,
                    int(state.studying if state is not None else studying),
                ),
            )
            self._event(db, message_id, moment, "queued", "enqueued")
            if duplicate:
                self._event(db, message_id, moment, "suppressed", "dedupe window")
            if urgency == "emergency":
                selected = (
                    select_channel(urgency, state or SirState()) if channel == "auto" else channel
                )
                self._deliver(db, [self._get(db, message_id)], moment, "single", selected)
        return message_id

    def enqueue_intent(self, intent: dict[str, Any], *, now: datetime | None = None) -> str:
        """Persist source facts, never words addressed to Sir; id is upstream-owned."""
        moment = _now(now)
        if (
            not isinstance(intent.get("id"), str)
            or not 1 <= len(intent["id"]) <= 200
            or not isinstance(intent.get("reason"), str)
            or not 1 <= len(intent["reason"].strip()) <= 500
            or any(c in intent["reason"] for c in "\r\n")
            or intent.get("urgency") not in ("low", "normal", "high", "emergency")
            or type(intent.get("needs_response")) is not bool
            or not isinstance(intent.get("evidence"), list)
            or not 1 <= len(intent["evidence"]) <= 8
            or any(not isinstance(r, str) or not r for r in intent["evidence"])
        ):
            raise ValueError("Invalid structured outreach intent")
        key = cast(str, intent["id"])
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM intents WHERE id=?", (key,)).fetchone():
                return key
            duplicate = False
            policy = "reason_dedupe"
            if intent["urgency"] != "emergency":

                def question_identity(item: dict[str, Any]) -> tuple[str, ...]:
                    if not item.get("needs_response"):
                        # Non-question notifications retain their existing policy.
                        return ("reason", str(item["reason"]))

                    belief_id = item.get("belief_id")
                    subject = item.get("gap_subject")
                    attribute = item.get("gap_attribute")
                    if not belief_id:
                        upstream = str(item["id"]).removeprefix("mind:")
                        if upstream.startswith("scheduler:"):
                            belief_id = upstream.removeprefix("scheduler:")
                        # The daemon sends reviewed-state evidence, not gap
                        # fields. Follow its durable provenance, never its words.
                        mind_path = self.root / "mind.db"
                        if mind_path.exists():
                            from contextlib import closing

                            with closing(
                                sqlite3.connect(mind_path.as_uri() + "?mode=ro", uri=True)
                            ) as mind:
                                pending = list(item["evidence"])
                                visited: set[str] = set()
                                while pending and len(visited) < 64:
                                    ref = pending.pop()
                                    if ref in visited:
                                        continue
                                    visited.add(ref)
                                    row = mind.execute(
                                        "SELECT payload,refs FROM entries WHERE id=?", (ref,)
                                    ).fetchone()
                                    if not row:
                                        continue
                                    payload = json.loads(row[0])
                                    decision = payload.get("decision", payload)
                                    belief_id = decision.get("belief_id") or belief_id
                                    gap = payload.get("question", {})
                                    if isinstance(gap, dict):
                                        gap = cast(dict[str, Any], gap).get("gap", gap)
                                        subject = gap.get(
                                            "subject", gap.get("gap_subject", subject)
                                        )
                                        attribute = gap.get(
                                            "attribute", gap.get("gap_attribute", attribute)
                                        )
                                    pending.extend(json.loads(row[1]))
                                if belief_id:
                                    row = mind.execute(
                                        "SELECT subject,attribute FROM belief_versions "
                                        "WHERE belief_id=? ORDER BY seq DESC LIMIT 1",
                                        (belief_id,),
                                    ).fetchone()
                                    if row is None:
                                        row = mind.execute(
                                            "SELECT json_extract(payload,'$.subject'),"
                                            "json_extract(payload,'$.attribute') FROM entries "
                                            "WHERE category='knowledge_gap' AND "
                                            "json_extract(payload,'$.belief_id')=? "
                                            "ORDER BY seq DESC LIMIT 1",
                                            (belief_id,),
                                        ).fetchone()
                                    if row:
                                        subject, attribute = subject or row[0], attribute or row[1]
                    if not belief_id and not subject and not attribute:
                        # No resolvable question provenance (offline callers,
                        # plain upstream ids): the reason is the only stable
                        # identity left. Two identical reasons inside the window
                        # are one outreach, so reason dedupe applies.
                        return ("reason", str(item["reason"]))
                    return (
                        "question",
                        str(belief_id or item["id"]),
                        str(subject or ""),
                        str(attribute or ""),
                    )

                identity = question_identity(intent)
                policy = (
                    "question_identity_dedupe" if identity[0] == "question" else "reason_dedupe"
                )
                for payload, created_at, delivered_at in db.execute(
                    """SELECT i.payload,i.created_at,d.created_at
                    FROM intents i LEFT JOIN deliveries d ON d.key=i.delivery_key
                    WHERE i.created_at<=? AND (
                        i.status IN ('queued','generating','generated')
                        OR (d.created_at>? AND d.created_at<=?))""",
                    (moment.isoformat(), (moment - DEDUPE_WINDOW).isoformat(), moment.isoformat()),
                ):
                    previous = cast(dict[str, Any], json.loads(payload))
                    in_window = identity[0] != "question" or (
                        datetime.fromisoformat(delivered_at or created_at) > moment - DEDUPE_WINDOW
                    )
                    if in_window and question_identity(previous) == identity:
                        duplicate = True
                        break
            db.execute(
                "INSERT INTO intents(id,payload,created_at,status) VALUES(?,?,?,?)",
                (
                    key,
                    json.dumps(intent, ensure_ascii=False),
                    moment.isoformat(),
                    "suppressed" if duplicate else "queued",
                ),
            )
            self._intent_event(db, key, moment, "queued", {})
            if duplicate:
                self._intent_event(
                    db,
                    key,
                    moment,
                    "suppressed",
                    {
                        "policy": policy
                    },
                )
        return key

    @staticmethod
    def _intent_event(
        db: sqlite3.Connection, key: str, now: datetime, status: str, detail: dict[str, Any]
    ) -> None:
        event = {"intent_id": key, "created_at": now.isoformat(), "status": status, **detail}
        db.execute(
            "INSERT INTO intent_events(intent_id,payload) VALUES(?,?)",
            (key, json.dumps(event, ensure_ascii=False)),
        )

    def intent_history(self, id: str) -> list[dict[str, Any]]:
        """Append-only structured audit, including suppression and emergency bypass."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM intent_events WHERE intent_id=? ORDER BY id", (id,)
            ).fetchall()
        return [cast(dict[str, Any], json.loads(row[0])) for row in rows]

    def delivery_metadata(self, key: str) -> dict[str, Any]:
        """Source provenance committed in the same transaction as the delivery."""
        with self._connect() as db:
            row = db.execute("SELECT payload FROM delivery_metadata WHERE key=?", (key,)).fetchone()
        return cast(dict[str, Any], json.loads(row[0])) if row else {}

    def _intent_blocks(self, now: datetime, online: bool) -> list[str]:
        """Only new proactive deliveries consume this budget, never legacy replies."""
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self._connect() as db:
            hourly = db.execute(
                "SELECT count(*) FROM deliveries WHERE kind='proactive' AND urgency!='emergency' "
                "AND created_at>? AND created_at<=?",
                ((now - timedelta(hours=1)).isoformat(), now.isoformat()),
            ).fetchone()[0]
            daily = db.execute(
                "SELECT count(*) FROM deliveries WHERE kind='proactive' AND urgency!='emergency' "
                "AND created_at>=? AND created_at<=?",
                (day.isoformat(), now.isoformat()),
            ).fetchone()[0]
            offline_sent = db.execute("SELECT offline_sent FROM intent_presence").fetchone()[0]
        blocks: list[str] = []
        if now.hour * 60 + now.minute < QUIET_END_MINUTE:
            blocks.append("quiet_hours")
        if hourly >= PROACTIVE_HOURLY_LIMIT:
            blocks.append("hourly_limit")
        if daily >= PROACTIVE_DAILY_LIMIT:
            blocks.append("daily_limit")
        if not online:
            blocks.append("offline")  # emergency records bypass even on its first offline turn
            if offline_sent < PROACTIVE_OFFLINE_LIMIT:
                blocks.remove("offline")
        return blocks

    @staticmethod
    def _fallback_intent(source: dict[str, Any], error_type: str) -> dict[str, Any]:
        return {"text": source["reason"], "generation_degraded": True, "error_type": error_type}

    def _render_intent(
        self, source: dict[str, Any], author: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> dict[str, Any]:
        try:
            raw = cast(Callable[[dict[str, Any]], object], author)(json.loads(json.dumps(source)))
            if not isinstance(raw, dict):
                raise ValueError("invalid render result")
            generated = cast(dict[str, Any], raw)
            if (
                not isinstance(generated.get("text"), str)
                or not generated["text"].strip()
                or type(generated.get("generation_degraded")) is not bool
            ):
                raise ValueError("invalid render result")
            result = {
                "text": generated["text"],
                "generation_degraded": generated["generation_degraded"],
            }
            if isinstance(generated.get("error_type"), str):
                result["error_type"] = generated["error_type"]
            return result
        except Exception as error:
            return self._fallback_intent(source, type(error).__name__)

    def _commit_intent(
        self,
        key: str,
        source: dict[str, Any],
        generated: dict[str, Any],
        now: datetime,
        *,
        online: bool,
        bypass: list[str],
    ) -> Delivery:
        delivery_key = uuid.uuid5(uuid.NAMESPACE_URL, "jarvis-proactive:" + key).hex
        metadata = {
            name: source[name] for name in ("reason", "evidence", "urgency", "needs_response")
        }
        metadata.update(
            intent_id=key,
            generation_degraded=generated["generation_degraded"],
            source_channel="jarvis_proactive",
        )
        if "error_type" in generated:
            metadata["error_type"] = generated["error_type"]
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """INSERT INTO deliveries
                (key,created_at,urgency,reason,text,channel,kind,message_ids)
                VALUES(?,?,?,?,?,'both','proactive',?)""",
                (
                    delivery_key,
                    now.isoformat(),
                    source["urgency"],
                    source["reason"],
                    generated["text"],
                    json.dumps([key]),
                ),
            )
            db.execute(
                "INSERT INTO delivery_metadata VALUES(?,?)",
                (delivery_key, json.dumps(metadata, ensure_ascii=False)),
            )
            db.execute(
                "UPDATE intents SET status='committed', delivery_key=? WHERE id=?",
                (delivery_key, key),
            )
            if not online and source["urgency"] != "emergency":
                db.execute("UPDATE intent_presence SET offline_sent=offline_sent+1")
            if source["urgency"] == "emergency":
                self._intent_event(db, key, now, "bypass", {"bypass": bypass, "online": online})
            self._intent_event(db, key, now, "committed", {"delivery_key": delivery_key})
            result = Delivery(
                int(cursor.lastrowid or 0),
                delivery_key,
                now.isoformat(),
                cast(Urgency, source["urgency"]),
                source["reason"],
                generated["text"],
                "both",
                "proactive",
                (key,),
            )
        return result

    def _generate_intent(
        self,
        key: str,
        source: dict[str, Any],
        status: str,
        cached: str | None,
        now: datetime,
        author: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        if status == "generated" and cached is not None:
            return cast(dict[str, Any], json.loads(cached))
        if status == "generating":
            # The previous lock holder died somewhere around the external call.
            # At-most-once authoring beats speculative repeat model requests.
            generated = self._fallback_intent(source, "GenerationOutcomeUnknown")
        else:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE intents SET status='generating',claimed_at=? WHERE id=?",
                    (now.isoformat(), key),
                )
                self._intent_event(db, key, now, "generating", {})
            generated = self._render_intent(source, author)
        with self._connect() as db:
            db.execute(
                "UPDATE intents SET status='generated',generated=? WHERE id=?",
                (json.dumps(generated, ensure_ascii=False), key),
            )
            self._intent_event(
                db,
                key,
                now,
                "generated",
                {
                    "generation_degraded": generated["generation_degraded"],
                    **(
                        {"error_type": generated["error_type"]} if "error_type" in generated else {}
                    ),
                },
            )
        return generated

    def _existing_intent_delivery(self, key: str) -> Delivery | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT id,key,created_at,urgency,reason,text,channel,kind,message_ids
                FROM deliveries WHERE key=(SELECT delivery_key FROM intents WHERE id=?)""",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return Delivery(
            int(row[0]),
            row[1],
            row[2],
            cast(Urgency, row[3]),
            row[4],
            row[5],
            cast(Channel, row[6]),
            row[7],
            tuple(cast(list[str], json.loads(row[8]))),
        )

    def _finish_intent(self, delivery: Delivery, now: datetime) -> bool:
        key = delivery.message_ids[0]
        try:
            self._write_email(delivery)
        except (OSError, ValueError) as error:
            with self._connect() as db:
                self._intent_event(
                    db, key, now, "outbox_pending", {"error_type": type(error).__name__}
                )
            return False
        with self._connect() as db:
            db.execute("UPDATE intents SET status='sent' WHERE id=?", (key,))
            self._intent_event(db, key, now, "sent", {"delivery_key": delivery.key})
        return True

    def dispatch_intents(
        self,
        now: datetime | None = None,
        *,
        render: Callable[[dict[str, Any]], dict[str, Any]],
        online: bool = True,
    ) -> list[Delivery]:
        """Render ready source intents and hand off to durable local channels only.

        The POSIX advisory lock serializes dispatchers, including rendering, but
        no SQLite write transaction is held while the author runs. Never call
        this method recursively from render. No real transport is invoked.
        """
        moment = _now(now)
        sent: list[Delivery] = []
        with (self.root / "intents.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with self._connect() as db:
                    if online:
                        db.execute("UPDATE intent_presence SET offline_sent=0")
                    rows = cast(
                        list[tuple[str, str, str, str | None]],
                        db.execute(
                            "SELECT id,payload,status,generated FROM intents WHERE status IN "
                            "('queued','generating','generated','committed') AND created_at<=? "
                            "ORDER BY CASE json_extract(payload,'$.urgency') "
                            "WHEN 'emergency' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,rowid",
                            (moment.isoformat(),),
                        ).fetchall(),
                    )
                for key, payload, status, cached in rows:
                    source = cast(dict[str, Any], json.loads(payload))
                    existing = self._existing_intent_delivery(key)
                    if existing is not None:
                        if self._finish_intent(existing, moment):
                            sent.append(existing)
                        continue
                    blocks = self._intent_blocks(moment, online)
                    if source["urgency"] != "emergency" and blocks:
                        continue
                    bypass = blocks.copy()
                    if source["urgency"] == "emergency" and not online and "offline" not in bypass:
                        bypass.append("offline")
                    existing = self._existing_intent_delivery(key)
                    if existing is not None:
                        if self._finish_intent(existing, moment):
                            sent.append(existing)
                        continue
                    generated = (
                        cast(dict[str, Any], json.loads(cached))
                        if status == "committed" and cached is not None
                        else self._generate_intent(key, source, status, cached, moment, render)
                    )
                    result = self._commit_intent(
                        key, source, generated, moment, online=online, bypass=bypass
                    )
                    if self._finish_intent(result, moment):
                        sent.append(result)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        return sent

    @staticmethod
    def _get(db: sqlite3.Connection, message_id: str) -> Message:
        row = cast(
            MessageRow | None,
            db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone(),
        )
        event = cast(
            tuple[str] | None,
            db.execute(
                "SELECT status FROM events WHERE message_id=? ORDER BY id DESC LIMIT 1",
                (message_id,),
            ).fetchone(),
        )
        if row is None or event is None:
            raise KeyError(message_id)
        return Message(
            row[0],
            row[1],
            cast(Urgency, row[2]),
            row[3],
            row[4],
            cast(Channel, row[5]),
            row[6],
            bool(row[7]),
            cast(Status, event[0]),
        )

    def get(self, message_id: str) -> Message:
        with self._connect() as db:
            return self._get(db, message_id)

    def history(self, message_id: str) -> list[StateEvent]:
        with self._connect() as db:
            rows = cast(
                list[tuple[int, str, str, str, str]],
                db.execute(
                    "SELECT * FROM events WHERE message_id=? ORDER BY id", (message_id,)
                ).fetchall(),
            )
        return [StateEvent(r[0], r[1], r[2], cast(Status, r[3]), r[4]) for r in rows]

    def _write_email(self, delivery: Delivery) -> None:
        outbox = self.root / "outbox"
        outbox.mkdir(mode=0o700, exist_ok=True)
        target = outbox / f"{delivery.key}.eml"
        mail = compose_email(delivery, self.recipient)
        metadata = self.delivery_metadata(delivery.key)
        if metadata:
            mail["X-JARVIS-Generation-Degraded"] = (
                "yes" if metadata.get("generation_degraded") else "no"
            )
            mail["X-JARVIS-Source-Channel"] = "jarvis_proactive"
        payload = mail.as_bytes()
        # Stage fully flushed bytes before publishing the deterministic final
        # name. A crash during write/fsync leaves no partial .eml to poison retry.
        # link() is exclusive/no-overwrite even when another writer won the race.
        stage = outbox / f".{delivery.key}.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(stage, target)
            except FileExistsError:
                if target.read_bytes() != payload:
                    raise ValueError("outbox draft conflicts with delivery") from None
            directory = os.open(outbox, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            stage.unlink(missing_ok=True)

    def _deliver(
        self,
        db: sqlite3.Connection,
        messages: list[Message],
        now: datetime,
        kind: str,
        channel: Channel,
    ) -> Delivery | None:
        first = messages[0]
        key = uuid.uuid5(
            uuid.NAMESPACE_URL, "jarvis-outreach:" + ":".join(m.id for m in messages)
        ).hex
        if kind == "single":
            text = first.text
        elif self.digest_builder is None:
            return None  # only the caller may author words for Sir
        else:
            text = self.digest_builder(tuple(messages), kind)
            if not text.strip():
                raise ValueError("caller digest_builder returned empty text")
        reason = first.reason
        urgency: Urgency = (
            "normal" if any(m.urgency == "normal" for m in messages) else first.urgency
        )
        draft = Delivery(
            0,
            key,
            now.isoformat(),
            urgency,
            reason,
            text,
            channel,
            kind,
            tuple(m.id for m in messages),
        )
        try:
            if channel in ("email", "both"):
                self._write_email(draft)
            elif channel == "voice":
                self.voice_adapter.deliver(draft, self.root)
            elif channel == "imessage":
                self.imessage_adapter.deliver(draft, self.root)
        except (OSError, ValueError) as error:
            for item in messages:
                self._event(db, item.id, now, "failed", f"local outbox: {type(error).__name__}")
            return None
        cursor = db.execute(
            """INSERT INTO deliveries
            (key,created_at,urgency,reason,text,channel,kind,message_ids)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                key,
                now.isoformat(),
                urgency,
                reason,
                text,
                channel,
                kind,
                json.dumps(draft.message_ids),
            ),
        )
        result = Delivery(
            int(cursor.lastrowid or 0),
            key,
            now.isoformat(),
            urgency,
            reason,
            text,
            channel,
            kind,
            draft.message_ids,
        )
        for item in messages:
            self._event(db, item.id, now, "sent", f"local channel delivery {result.id}; {kind}")
        return result

    def dispatch_due(
        self,
        now: datetime | None = None,
        *,
        studying: bool | None = None,
        state: SirState | None = None,
        imessage_available: bool | None = None,
    ) -> list[Delivery]:
        moment = _now(now)
        sent: list[Delivery] = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = cast(
                list[tuple[str]],
                db.execute(
                    """
                SELECT id FROM messages m WHERE created_at<=? AND
                (SELECT status FROM events WHERE message_id=m.id ORDER BY id DESC LIMIT 1)='queued'
                ORDER BY CASE urgency WHEN 'emergency' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, rowid
            """,
                    (moment.isoformat(),),
                ).fetchall(),
            )
            pending = [self._get(db, row[0]) for row in rows]
            recent_rows = cast(
                list[DeliveryRow],
                db.execute(
                    "SELECT * FROM deliveries WHERE created_at>? AND created_at<=?",
                    ((moment - timedelta(hours=1)).isoformat(), moment.isoformat()),
                ).fetchall(),
            )
            recent = [_delivery(row) for row in recent_rows]
            day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
            study_rows = cast(
                list[tuple[str]],
                db.execute(
                    "SELECT channel FROM deliveries WHERE kind='study' "
                    "AND created_at>=? AND created_at<=?",
                    (day_start.isoformat(), moment.isoformat()),
                ).fetchall(),
            )
            studied_channels = {r[0] for r in study_rows}
            quiet = moment.hour * 60 + moment.minute < QUIET_END_MINUTE
            cutoff = moment.replace(hour=7, minute=30, second=0, microsecond=0)
            batches: dict[tuple[str, Channel], list[Message]] = {}
            for item in pending:
                is_studying = (
                    studying
                    if studying is not None
                    else (state.studying if state is not None else item.studying)
                )
                route_state = replace(state or SirState(), studying=is_studying)
                # Availability alone must not override per-message study quieting.
                if imessage_available is not None:
                    route_state = replace(route_state, imessage_available=imessage_available)
                selected = (
                    select_channel(item.urgency, route_state)
                    if item.channel == "auto"
                    else item.channel
                )
                if item.urgency in ("high", "emergency"):
                    batches[(item.id, selected)] = [item]
                elif quiet:
                    continue
                elif item.urgency == "low" and is_studying:
                    if moment.hour >= STUDY_DIGEST_HOUR and not studied_channels:
                        batches.setdefault(("study", selected), []).append(item)
                elif datetime.fromisoformat(item.created_at) < cutoff:
                    batches.setdefault(("quiet", selected), []).append(item)
                else:
                    batches[(item.id, selected)] = [item]
            for (batch_kind, channel), items in batches.items():
                if batch_kind == "study" and studied_channels:
                    continue
                urgency = (
                    "normal" if any(i.urgency == "normal" for i in items) else items[0].urgency
                )
                if urgency in ("low", "normal"):
                    regular = [d for d in recent if d.urgency in ("low", "normal")]
                    if len(regular) >= REGULAR_HOURLY_LIMIT:
                        continue
                    if (
                        urgency == "low"
                        and sum(d.urgency == "low" for d in regular) >= LOW_HOURLY_LIMIT
                    ):
                        continue
                kind = batch_kind if batch_kind in ("quiet", "study") else "single"
                result = self._deliver(db, items, moment, kind, channel)
                if result is not None:
                    sent.append(result)
                    recent.append(result)
                    if kind == "study":
                        studied_channels.add(channel)
        return sent

    def web_events(self, after_id: int = 0, *, limit: int = 100) -> list[Delivery]:
        if after_id < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid cursor or limit")
        with self._connect() as db:
            rows = cast(
                list[DeliveryRow],
                db.execute(
                    "SELECT * FROM deliveries WHERE id>? AND channel IN ('web','both') "
                    "ORDER BY id LIMIT ?",
                    (after_id, limit),
                ).fetchall(),
            )
        return [_delivery(row) for row in rows]


def serve_sse(
    handler: BaseHTTPRequestHandler,
    queue: Outreach,
    *,
    after_id: int = 0,
    stop: threading.Event | None = None,
) -> None:
    """Mountable blocking SSE handler; run in a threaded local HTTP server.

    Durable IDs support Last-Event-ID reconnection. New clients replay all
    deliveries unless they explicitly provide a cursor. No browser receipt is
    inferred from a successful write. Owner must restrict access to loopback.
    """
    if after_id < 0:
        handler.send_error(400, "Invalid event cursor")
        return
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    stopped = stop if stop is not None else threading.Event()
    import time

    heartbeat = time.monotonic()
    try:
        handler.wfile.write(b": connected\n\n")
        handler.wfile.flush()
        while not stopped.is_set():
            for item in queue.web_events(after_id):
                payload = json.dumps(asdict(item), ensure_ascii=False)
                handler.wfile.write(f"id: {item.id}\nevent: outreach\ndata: {payload}\n\n".encode())
                handler.wfile.flush()
                after_id = item.id
            if time.monotonic() - heartbeat >= SSE_HEARTBEAT_SECONDS:
                handler.wfile.write(b": heartbeat\n\n")
                handler.wfile.flush()
                heartbeat = time.monotonic()
            stopped.wait(SSE_POLL_SECONDS)
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        pass


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, queue: Outreach, port: int = 0) -> None:
        self.stop_event = threading.Event()
        self.queue = queue
        super().__init__(("127.0.0.1", port), make_demo_handler(queue, self.stop_event))

    def server_close(self) -> None:
        self.stop_event.set()
        super().server_close()


def make_demo_handler(queue: Outreach, stop: threading.Event) -> type[BaseHTTPRequestHandler]:
    """Read-only demo HTTP routes; no public enqueue endpoint or CORS access."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            expected_host = f"127.0.0.1:{cast(ThreadingHTTPServer, self.server).server_port}"
            if self.headers.get("Host") != expected_host:
                self.send_error(403, "Loopback Host required")
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/events":
                value = (
                    self.headers.get("Last-Event-ID")
                    or parse_qs(parsed.query).get("after", ["0"])[0]
                )
                try:
                    cursor = int(value)
                except ValueError:
                    self.send_error(400, "Invalid event cursor")
                    return
                serve_sse(self, queue, after_id=cursor, stop=stop)
            elif parsed.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            elif parsed.path in ("/", "/outreach-demo.html"):
                page = Path(__file__).resolve().parents[1] / "apps/web/outreach-demo.html"
                payload = page.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'unsafe-inline'; "
                    "style-src 'unsafe-inline'; connect-src 'self'",
                )
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_error(404)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="JARVIS local outreach SSE demo; no real email")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--demo-message", action="store_true", help="enqueue one clearly marked demo message"
    )
    args = parser.parse_args()
    queue = Outreach(cast(Path, args.data_root))
    server = DemoServer(queue, cast(int, args.port))
    if args.demo_message:
        queue.enqueue(
            "【演示文本】刚才说的通道已经通了，之后有事就能放到你面前。",
            reason="【演示文本】本地通道验证",
            urgency="high",
        )
    print(f"http://127.0.0.1:{server.server_port}/outreach-demo.html", flush=True)
    runner = threading.Thread(target=server.serve_forever, daemon=True)
    runner.start()
    try:
        while True:
            queue.dispatch_due()
            server.stop_event.wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop_event.set()
        server.shutdown()
        server.server_close()
        runner.join(timeout=5)


def self_evaluate() -> dict[str, object]:
    """Describe delivery semantics without accessing the user's outbox."""
    from datetime import timezone

    return {
        "module": "outreach",
        "domain": "durable proactive notification scheduling and local delivery",
        "current_version": (
            "SQLite append-only events, bounded proactive intent dispatch, local SSE and "
            "email drafts with injectable voice/iMessage adapters"
        ),
        "known_limitations": [
            "sent records local channel handoff, not recipient read acknowledgement; email is a draft without SMTP sending.",
            "SQLite transactions cannot atomically commit external iMessage delivery; injected adapter failures require reconciliation and failed messages are not automatically retried.",
            "Studying and channel availability are caller-supplied state; aggregate digest content requires an injected author.",
        ],
        "improvement_directions": [
            "Add transport-specific idempotency and reconciliation receipts before claiming end-to-end delivery guarantees.",
            "Measure acknowledgement and retry outcomes separately from local queue handoff while retaining caller-authored digest text.",
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    main()
