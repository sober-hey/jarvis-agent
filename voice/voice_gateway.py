#!/usr/bin/env python3
"""Authenticated private-address ears/mouth for the single JARVIS BRAIN timeline.

Audio messages contain mono 16k PCM16, not WAV headers. Consecutive chunks form
one utterance; a short transport gap (default 500ms), or ``audio_end``, commits it.
Clients must gate capture locally (wake/VAD/AEC); this is not an always-on cloud
microphone. Replies are text followed by MP3 chunks and an empty final chunk.
``playback_done`` acknowledges local playback; otherwise the next utterance sends
interrupt even after synthesis finished, flushing audio buffered by the client.
Interruption discards stale speech, never retries or kills a submitted brain turn.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hmac
import ipaddress
import json
import logging
import os
import stat
import subprocess
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol, cast

from voice_port import BrainVoiceSink, SpeakPriority, VoiceContext, VoiceEventSink, VoiceMode
from voice_stream_tts import SentenceBuffer, StreamingTTS, StreamingTTSError
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed


async def answer_fragments(text: str) -> AsyncIterator[str]:
    buffer = SentenceBuffer(comma_threshold=1)
    for segment in buffer.feed(text) + buffer.finish():
        yield segment
        await asyncio.sleep(0)


class FragmentSource(Protocol):
    def __call__(self, text: str) -> AsyncIterator[str]: ...


HOST = "127.0.0.1"
TAILSCALE_HOST = "100.67.229.1"
LAN_HOST = "192.168.1.5"
BIND_HOSTS = (HOST, TAILSCALE_HOST)
PORT = 8765


def read_gateway_token(path: Path) -> str:
    """Read a dedicated owner-only token without following a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="ascii") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
                or info.st_size > 4096
            ):
                raise PermissionError("gateway token must be an owner-only 0600 regular file")
            value = handle.read(4097)
    except (OSError, UnicodeError):
        raise PermissionError("gateway token file is unavailable or unsafe") from None
    if value.endswith("\n"):
        value = value[:-1]
    if (
        not value
        or "\n" in value
        or "\r" in value
        or not value.isascii()
        or any(c.isspace() for c in value)
    ):
        raise ValueError("gateway token must contain one nonempty ASCII line")
    return value


def en0_address() -> str | None:
    try:
        result = subprocess.run(
            ["/usr/sbin/ipconfig", "getifaddr", "en0"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if (
        address.version != 4
        or not address.is_private
        or address.is_unspecified
        or address.is_loopback
    ):
        return None
    return value


def configured_bind_hosts() -> tuple[str, ...]:
    hosts = [HOST, TAILSCALE_HOST]
    lan = en0_address()
    if lan is not None:
        hosts.append(lan)
    return tuple(hosts)


MAX_TTS_BYTES = 24 * 1024 * 1024
MAX_AUDIO_BYTES = 16000 * 2 * 30
# 听到 Sir 在跟别人说话后保持沉默多久（最后一次说话之后算起）
CONVERSATION_HOLD = 20.0
CONVERSATION_PROMPT = "Sir，您沟通完了吗？还需要我跟您继续汇报吗？"
# 「嗯 / 呃 / 啊 / 哦」这类语气词不构成一个问题，不要拿它去问大脑。
FILLER_WORDS = {"嗯", "呃", "额", "啊", "哦", "唔", "唉", "诶", "欸", "哎"}
# 声纹：先只打分不拦截。参考音质量不足时拦截会误杀 Sir 本人。
SPEAKER_ENFORCE = os.environ.get("JARVIS_SPEAKER_ENFORCE", "1") != "0"
UTTERANCE_DIR = Path.home() / ".jarvis" / "utterances"
MAX_MESSAGE_BYTES = MAX_AUDIO_BYTES * 4 // 3 + 1024
LOG = logging.getLogger("jarvis.voice_gateway")


class BrainOutcomeError(RuntimeError):
    """Sanitized brain failure with an optional provider HTTP status."""

    def __init__(self, code: str, *, provider_status: int | None = None) -> None:
        self.code = code
        self.provider_status = provider_status
        super().__init__(code)


class SpeechProvider(Protocol):
    async def transcribe(self, pcm: bytes) -> str | dict[str, Any]: ...
    def synthesize(self, text: str) -> AsyncIterator[bytes]: ...
    def create_stream(self) -> StreamingTTS: ...


class BrainBridge:
    """Bounded bridge to the unchanged synchronous BRAIN sink.

    Only one final transcript per client is submitted at a time. Its legacy
    thread waits on an asyncio coroutine; no network/blocking wait runs on the
    event loop. Speech generation guards live on the client transport, so a
    superseded brain call still finishes and records the single timeline.
    """

    mode = VoiceMode.BRAIN

    def __init__(self, reply: Callable[[str], Awaitable[str]]) -> None:
        self.loop = asyncio.get_running_loop()
        self.reply = reply
        self.waiter: asyncio.Future[str] | None = None
        self.sink = BrainVoiceSink(self, self._reply_sync, on_error=self._error)

    async def _reply_async(self, text: str) -> str:
        return await self.reply(text)

    def _reply_sync(self, text: str) -> str:
        future = asyncio.run_coroutine_threadsafe(self._reply_async(text), self.loop)
        try:
            return future.result()
        except Exception as exc:
            self.loop.call_soon_threadsafe(self._resolve, exc)
            raise

    def start(self, context: VoiceContext, sink: VoiceEventSink) -> None:
        raise RuntimeError("transport_bridge_is_owned_by_session")

    def update_context(self, context: VoiceContext) -> None:
        return  # Context/personality belongs to chat_entry, not the voice vendor.

    def interrupt(self) -> None:
        return  # Transport generations suppress speech, not the brain operation.

    def close(self) -> None:
        return

    def _resolve(self, value: str | Exception | None) -> None:
        if self.waiter is not None and not self.waiter.done():
            if isinstance(value, Exception):
                self.waiter.set_exception(value)
            elif value is None:
                self.waiter.set_exception(RuntimeError("brain_failed"))
            else:
                self.waiter.set_result(value)

    def speak(self, text: str, priority: SpeakPriority = SpeakPriority.REPLY) -> None:
        self.loop.call_soon_threadsafe(self._resolve, text)

    def _error(self, message: str) -> None:
        self.loop.call_soon_threadsafe(self._resolve, None)

    async def answer(self, text: str) -> str:
        self.waiter = self.loop.create_future()
        self.sink.on_user_transcript(text, final=True)
        return await self.waiter


class ClientSession:
    def __init__(self, gateway: VoiceGateway, websocket: ServerConnection) -> None:
        self.gateway = gateway
        self.ws = websocket
        self.device_id = "legacy-" + uuid.uuid4().hex
        self.device_type: str | None = None
        self.state: dict[str, Any] = {}
        self.state_at = 0.0
        self.selected: bool | None = None
        self.generation = 0
        self.playing = False
        self.conversation_until = 0.0
        self.last_speech_at = 0.0
        self.buffer = bytearray()
        self.queue: asyncio.Queue[tuple[int, bytes | str]] = asyncio.Queue(maxsize=2)
        self.send_lock = asyncio.Lock()
        self.timer: asyncio.Task[None] | None = None
        self.output: asyncio.Task[None] | None = None
        self.outputs: set[asyncio.Task[None]] = set()
        self.bridge = BrainBridge(gateway.reply)
        self.turns: dict[int, tuple[str, str]] = {}
        self.playbacks: dict[int, tuple[str, str]] = {}
        self.active = False
        self.stream_pcm = False
        self.tts_streams: dict[int, StreamingTTS] = {}
        self.tts_completed: set[int] = set()

    async def send(self, message: dict[str, Any], generation: int | None = None) -> None:
        async with self.send_lock:
            if generation is None or generation == self.generation:
                try:
                    await asyncio.wait_for(self.ws.send(json.dumps(message)), 5)
                except TimeoutError:
                    await self.ws.close(1011, "send_timeout")
                    raise

    async def interrupt(self) -> None:
        old = self.generation
        self.generation += 1
        stream = self.tts_streams.get(old)
        if stream is not None:
            stream.cancel()
        if self.output is not None:
            self.output.cancel()
        self.tts_completed.discard(old)
        target = self.playbacks.pop(old, None) or self.turns.get(old)
        if target is not None:
            await self.gateway.observe(
                "record_interrupt",
                target[0],
                interrupted_by="user",
                target_id=target[1],
                device_id=self.device_id,
            )
        if self.playing:
            self.playing = False
            # Legacy clients accepted a generation-less interrupt. Negotiated clients
            # receive the new generation, matching the iPhone/Watch contract.
            message: dict[str, Any] = {"type": "interrupt"}
            if self.device_type is not None:
                message["generation"] = self.generation
            await self.send(message)

    # -- 与别人对话时的让路逻辑 -------------------------------------
    def note_speech(self) -> None:
        self.last_speech_at = time.monotonic()
        if self.conversation_until:
            self.conversation_until = self.last_speech_at + CONVERSATION_HOLD

    def begin_conversation(self) -> None:
        self.last_speech_at = time.monotonic()
        self.conversation_until = self.last_speech_at + CONVERSATION_HOLD

    def conversation_active(self) -> bool:
        return self.conversation_until > time.monotonic()

    async def prompt(self, text: str) -> None:
        """Speak a fixed line without involving the brain."""
        generation = self.generation
        try:
            self.playing = True
            async for chunk in self.gateway.speech.synthesize(text):
                if generation != self.generation:
                    return
                await self.send(
                    {
                        "type": "audio",
                        "data": base64.b64encode(chunk).decode(),
                        "format": "mp3",
                        "final": False,
                        "generation": generation,
                    },
                    generation,
                )
            if generation != self.generation:
                return
            self.tts_completed.add(generation)
            await self.send(
                {
                    "type": "audio",
                    "data": "",
                    "format": "mp3",
                    "final": True,
                    "generation": generation,
                },
                generation,
            )
        except Exception as exc:
            self.playing = False
            LOG.warning("prompt_failed error_type=%s", type(exc).__name__)

    async def commit(self) -> None:
        if not self.buffer:
            return
        # If a previous TTS succeeded but the client never sent playback_done
        # (e.g. MP3 arrived while selected=False and was discarded), playing
        # stays True and blocks every subsequent admit(). Clear it now so the
        # new utterance can proceed.
        if self.playing:
            LOG.info(
                "playing_stale_reset device=%s generation=%d — clearing stale playing flag",
                self.device_id,
                self.generation,
            )
            self.playing = False
        pcm = bytes(self.buffer)
        self.buffer.clear()
        # Diagnosability: without this, an utterance that ASR drops leaves no trace
        # at all and the phone looks "silent" for no visible reason.
        LOG.info(
            "audio_commit device=%s device_type=%s pcm_bytes=%d generation=%d",
            self.device_id,
            self.device_type,
            len(pcm),
            self.generation,
        )
        await self.enqueue(pcm)

    async def enqueue(self, value: bytes | str) -> None:
        try:
            self.queue.put_nowait((self.generation, value))
        except asyncio.QueueFull:
            await self.send({"type": "error", "code": "busy"})

    async def flush_after_gap(self) -> None:
        await asyncio.sleep(self.gateway.utterance_gap)
        await self.commit()

    async def control(self, value: dict[str, Any]) -> None:
        kind = value["type"]
        identity = value.get("device_id")
        if kind == "hello":
            if (
                self.device_type is not None
                or not isinstance(identity, str)
                or not identity.strip()
                or len(identity) > 200
                or value.get("device_type") not in {"iphone", "watch", "mac", "xiaoai"}
                or type(value.get("sample_rate")) is not int
                or value["sample_rate"] != 16000
                or type(value.get("channels")) is not int
                or value["channels"] != 1
                or value.get("encoding") != "pcm_s16le"
                or any(c is not self and c.device_id == identity for c in self.gateway.clients)
            ):
                raise ValueError("invalid hello")
            state = value.get("state", {})
            self.gateway.validate_state(state)
            if type(value.get("stream_pcm", False)) is not bool:
                raise ValueError("invalid PCM capability")
            self.stream_pcm = value.get("stream_pcm", False)
            self.device_id = identity
            self.device_type = value["device_type"]
            LOG.info("hello device=%s device_type=%s", identity, self.device_type)
        elif self.device_type is None or identity != self.device_id:
            raise ValueError("identity mismatch")
        if kind in {"hello", "device_state"}:
            state = value.get("state")
            self.gateway.validate_state(state)
            self.state = dict(cast(dict[str, Any], state))
            self.state_at = time.monotonic()
            await self.gateway.coordinate()
        elif kind == "text":
            text = value.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                raise ValueError("invalid text")
            if await self.gateway.admit(self):
                await self.interrupt()
                self.buffer.clear()
                if self.timer is not None:
                    self.timer.cancel()
                await self.enqueue(text)
            return
        elif kind == "audio_commit":
            await self.interrupt()
            await self.commit()

    async def receive(self, raw: str | bytes) -> None:
        kind: object = None
        try:
            value = json.loads(raw) if isinstance(raw, str) else None
            if not isinstance(value, dict):
                raise ValueError
            value = cast(dict[str, Any], value)
            kind = value.get("type")
            if kind in {"hello", "device_state", "text", "audio_commit"}:
                await self.control(value)
                return
            if self.gateway.token is not None and self.device_type is None:
                await self.send({"type": "error", "code": "hello_required"})
                return
            if kind == "playback_done":
                generation = value.get("generation", self.generation)
                if generation == self.generation and generation in self.tts_completed:
                    self.playing = False
                    target = self.playbacks.pop(generation, None)
                    if target is not None:
                        await self.gateway.observe("record_playback_end", target[0], target[1])
                    elif generation in self.turns and self.gateway.ledger is not None:
                        turn, assistant = self.turns[generation]
                        await asyncio.to_thread(
                            self.gateway.ledger.store.append,
                            "voice_playback",
                            {
                                "turn_id": turn,
                                "event": "reported_end",
                                "assistant_id": assistant,
                                "device_id": self.device_id,
                                "timestamp": datetime.now(UTC).isoformat(),
                                "generation": generation,
                            },
                            source=self.device_id,
                            refs=[assistant],
                        )
                return
            if kind == "playback_started":
                generation = value.get("generation")
                if (
                    generation == self.generation
                    and generation in self.turns
                    and generation not in self.playbacks
                ):
                    turn, assistant = self.turns[generation]
                    row = await self.gateway.observe(
                        "record_playback_start", turn, assistant, device_id=self.device_id
                    )
                    if row:
                        self.playbacks[generation] = (turn, row["id"])
                return
            if kind == "audio_commit":
                if self.timer is not None:
                    self.timer.cancel()
                await self.interrupt()
                await self.commit()
                return
            if kind == "audio_end":
                if self.timer is not None:
                    self.timer.cancel()
                await self.commit()
                return
            if (
                kind != "audio"
                or value.get("format") != "pcm_s16le"
                or type(value.get("sample_rate")) is not int
                or value["sample_rate"] != 16000
                or not isinstance(value.get("data"), str)
                or len(value["data"]) > MAX_MESSAGE_BYTES
            ):
                raise ValueError
            pcm = base64.b64decode(value["data"], validate=True)
            if not pcm or len(pcm) % 2 or len(pcm) + len(self.buffer) > MAX_AUDIO_BYTES:
                raise ValueError
        except (ValueError, binascii.Error):
            code = (
                "invalid_control" if kind in {"hello", "device_state", "text"} else "invalid_audio"
            )
            await self.send({"type": "error", "code": code})
            return
        if not await self.gateway.admit(self):
            return
        if not self.buffer:
            await self.interrupt()
        self.buffer.extend(pcm)
        if self.timer is not None:
            self.timer.cancel()
        self.timer = asyncio.create_task(self.flush_after_gap())

    speak_started: float | None = None

    async def speak(self, text: str, generation: int) -> None:
        complete = False
        cancelled = False
        started = time.monotonic()
        first_ms = -1
        frames_sent = 0
        total_bytes = 0
        mode = "fallback"
        stream: StreamingTTS | None = None

        async def emit(chunk: bytes, audio_format: str) -> None:
            nonlocal first_ms, frames_sent, total_bytes
            if generation != self.generation:
                raise asyncio.CancelledError
            if not chunk:
                return
            total_bytes += len(chunk)
            if total_bytes > MAX_TTS_BYTES:
                raise RuntimeError("tts_too_large")
            self.playing = True
            await self.send(
                {
                    "type": "audio",
                    "data": base64.b64encode(chunk).decode(),
                    "format": audio_format,
                    "final": False,
                    "generation": generation,
                    **(
                        {"sample_rate": 16000, "channels": 1} if audio_format == "pcm_s16le" else {}
                    ),
                },
                generation,
            )
            if generation != self.generation:
                raise asyncio.CancelledError
            frames_sent += 1
            if first_ms < 0:
                first_ms = round((time.monotonic() - started) * 1000)
                LOG.info(
                    "tts_first_chunk device=%s generation=%d ms=%d "
                    "tts_mode=%s tts_first_chunk_ms=%d",
                    self.device_id,
                    generation,
                    first_ms,
                    mode,
                    first_ms,
                )

        try:
            factory = getattr(self.gateway.speech, "create_stream", None)
            if self.stream_pcm and factory is not None:
                mode = "stream"
                try:
                    stream = cast(StreamingTTS, factory())
                    self.tts_streams[generation] = stream
                    async with asyncio.timeout(90):
                        async for chunk in stream.stream(self.gateway.text_fragments(text)):
                            await emit(chunk, "pcm_s16le")
                    if generation != self.generation:
                        raise asyncio.CancelledError
                    if not frames_sent:
                        raise RuntimeError("tts_empty")
                except (ConnectionClosed, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    if generation != self.generation:
                        raise asyncio.CancelledError from None
                    if stream is not None:
                        stream.cancel()
                    LOG.warning(
                        "tts_fallback device=%s generation=%d reason=%s frames_sent=%d retry=1",
                        self.device_id,
                        generation,
                        exc.code if isinstance(exc, StreamingTTSError) else type(exc).__name__,
                        frames_sent,
                    )
                    if self.playing:
                        await self.send({"type": "interrupt", "generation": generation}, generation)
                        self.playing = False
                    mode = "fallback"
                    stream = None
            if mode == "fallback":
                before = frames_sent
                async with asyncio.timeout(90):
                    async for chunk in self.gateway.speech.synthesize(text):
                        await emit(chunk, "mp3")
                if frames_sent == before:
                    raise RuntimeError("tts_empty")
            final_format = "pcm_s16le" if mode == "stream" else "mp3"
            self.tts_completed.add(generation)
            await self.send(
                {
                    "type": "audio",
                    "data": "",
                    "format": final_format,
                    "final": True,
                    "generation": generation,
                    **(
                        {"sample_rate": 16000, "channels": 1} if final_format == "pcm_s16le" else {}
                    ),
                },
                generation,
            )
            complete = True
            self.gateway.provider_result(None)
        except ConnectionClosed:
            return
        except asyncio.CancelledError:
            cancelled = True
            if stream is not None:
                stream.cancel()
            LOG.info(
                "turn_cancelled device=%s generation=%d frames_sent=%d",
                self.device_id,
                generation,
                frames_sent,
            )
            raise
        except Exception as exc:
            self.gateway.provider_result(exc)
            LOG.warning("tts_failed error_type=%s", type(exc).__name__)
            with contextlib.suppress(ConnectionClosed, TimeoutError):
                await self.send({"type": "error", "code": "tts_failed"}, generation)
        finally:
            if stream is not None:
                stream.cancel()
            self.tts_streams.pop(generation, None)
            if not complete:
                self.tts_completed.discard(generation)
            LOG.info(
                "tts_timing device=%s generation=%d tts_mode=%s "
                "tts_first_chunk_ms=%d tts_total_ms=%d frames_sent=%d status=%s",
                self.device_id,
                generation,
                mode,
                first_ms,
                round((time.monotonic() - started) * 1000),
                frames_sent,
                "success" if complete else "cancelled" if cancelled else "failed",
            )
            target = self.turns.get(generation)
            if target is not None:
                await self.gateway.observe(
                    "record_tts",
                    target[0],
                    target[1],
                    status="success" if complete else "failed",
                    voice_id=getattr(self.gateway.speech, "voice", "provider-unspecified"),
                    audio_duration_ms=None,
                    error=(
                        None
                        if complete
                        else "turn_cancelled"
                        if cancelled
                        else "synthesis_incomplete"
                    ),
                )

    async def work(self) -> None:
        while True:
            generation, source = await self.queue.get()
            try:
                if generation != self.generation:
                    continue
                self.active = True
                turn_started = time.monotonic()
                self.gateway.last_call_at = datetime.now(UTC).isoformat()
                self.gateway.publish_status()
                if isinstance(source, bytes) and self.gateway.speaker is not None:
                    owner, score = await asyncio.to_thread(self.gateway.speaker.is_owner, source)
                    await asyncio.to_thread(self.gateway.save_utterance, self.device_id, source)
                    LOG.info(
                        "speaker_score device=%s score=%s owner=%s mode=%s",
                        self.device_id,
                        "n/a" if score is None else f"{score:.2f}",
                        owner,
                        "enforce" if SPEAKER_ENFORCE else "shadow",
                    )
                    if not SPEAKER_ENFORCE:
                        owner = None
                    if owner is False:
                        # 别人在说话：不回答、不转写，进入让路状态等他们说完。
                        LOG.info(
                            "speaker_foreign device=%s score=%.2f", self.device_id, score or 0.0
                        )
                        self.begin_conversation()
                        self.gateway.speaker_events.append((self.device_id, score, False))
                        continue
                    if owner is True:
                        self.gateway.speaker_events.append((self.device_id, score, True))
                        if self.conversation_active():
                            # Sir 正和对方聊天，不要插嘴。
                            LOG.info("speaker_hold_owner_talking device=%s", self.device_id)
                            self.note_speech()
                            continue
                result = (
                    await self.gateway.speech.transcribe(source)
                    if isinstance(source, bytes)
                    else source
                )
                if isinstance(source, bytes):
                    self.gateway.provider_result(None)
                asr_done = time.monotonic()
                metadata = result if isinstance(result, dict) else {}
                text = metadata.get("text", "") if isinstance(result, dict) else result
                if not isinstance(text, str) or not text.strip() or generation != self.generation:
                    # An empty transcript (or a superseded generation) previously
                    # vanished without a trace; log it so a silent phone is diagnosable.
                    LOG.info(
                        "asr_empty device=%s superseded=%s transcript_type=%s",
                        self.device_id,
                        generation != self.generation,
                        type(text).__name__,
                    )
                    continue
                if text.strip("，。！？、…,.!? ") in FILLER_WORDS:
                    LOG.info("asr_filler device=%s text=%s", self.device_id, text)
                    self.note_speech()
                    continue
                LOG.info(
                    "asr_text device=%s chars=%d text=%s",
                    self.device_id,
                    len(text),
                    text[:80],
                )
                row = await self.gateway.observe(
                    "record_user",
                    text,
                    device_id=self.device_id,
                    asr_confidence=metadata.get("asr_confidence"),
                    emotion=metadata.get("emotion"),
                    audio_duration_ms=len(source) / 32 if isinstance(source, bytes) else 0,
                )
                turn = row["payload"]["turn_id"] if row else None
                if turn is not None:
                    self.turns[generation] = (turn, row["id"])
                await self.send(
                    {
                        "type": "text",
                        "role": "user",
                        "text": text,
                        "final": True,
                        "generation": generation,
                    },
                    generation,
                )
                if generation != self.generation:
                    continue
                start = time.monotonic()
                answer = await self.gateway.answer(self.bridge, text, turn_id=turn, started=start)
                brain_done = time.monotonic()
                self.speak_started = brain_done
                LOG.info(
                    "turn_timing device=%s asr_ms=%d brain_ms=%d answer_chars=%d",
                    self.device_id,
                    round((asr_done - turn_started) * 1000),
                    round((brain_done - start) * 1000),
                    len(answer or ""),
                )
                # Answer is recorded even if superseded, but must never be spoken.
                if generation != self.generation:
                    if turn is not None:
                        self.gateway.assistant_ids.pop(turn, None)
                    continue
                if turn is not None:
                    assistant = self.gateway.assistant_ids.pop(turn)
                    self.turns[generation] = (turn, assistant)
                if turn is not None:
                    # Only retain current and just-interrupted turn references.
                    self.turns = {g: t for g, t in self.turns.items() if g >= generation - 1}
                await self.send(
                    {
                        "type": "text",
                        "role": "assistant",
                        "text": answer,
                        "final": True,
                        "generation": generation,
                    },
                    generation,
                )
                self.output = asyncio.create_task(self.speak(answer, generation))
                self.outputs.add(self.output)
                self.output.add_done_callback(self.outputs.discard)
            except ConnectionClosed:
                return
            except Exception as exc:
                self.gateway.provider_result(exc)
                # LauncherError contains launcher-owned diagnostics, not raw vendor
                # responses. Retain its message and raise site locally; never echo
                # either on the device socket or log arbitrary provider exceptions.
                from chat_launcher import LauncherError

                if isinstance(exc, LauncherError):
                    LOG.warning(
                        "turn_failed error_type=%s error=%s",
                        type(exc).__name__,
                        str(exc),
                        exc_info=True,
                    )
                else:
                    LOG.warning("turn_failed error_type=%s", type(exc).__name__)
                with contextlib.suppress(ConnectionClosed, TimeoutError):
                    message: dict[str, Any] = {"type": "error", "code": "turn_failed"}
                    if isinstance(exc, BrainOutcomeError):
                        message["code"] = exc.code
                        if exc.provider_status is not None:
                            message["provider_status"] = exc.provider_status
                    await self.send(message, generation)
            finally:
                self.active = False
                self.queue.task_done()

    async def run(self) -> None:
        worker = asyncio.create_task(self.work())
        try:
            async for raw in self.ws:
                await self.receive(raw)
        except ConnectionClosed:
            pass
        finally:
            self.generation += 1
            tasks = [task for task in (worker, self.timer) if task is not None]
            tasks.extend(self.outputs)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            LOG.info("client_disconnected")


class VoiceGateway:
    def __init__(
        self,
        speech: SpeechProvider,
        reply: Callable[[str], Awaitable[str]],
        *,
        utterance_gap: float = 0.5,
        max_clients: int = 32,
        token: str | None = None,
        bind_hosts: tuple[str, ...] = (HOST,),
        ledger: Any = None,
        status_root: Path | None = None,
        heartbeat_ttl: float = 45,
        text_fragments: FragmentSource = answer_fragments,
        speaker: Any = None,
    ) -> None:
        if not 0 < utterance_gap <= 5 or max_clients < 1 or not 0 < heartbeat_ttl <= 120:
            raise ValueError("invalid gateway bounds")
        current_lan = en0_address()
        allowed_hosts = {HOST, TAILSCALE_HOST}
        if current_lan is not None:
            allowed_hosts.add(current_lan)
        if not bind_hosts or any(h not in allowed_hosts for h in bind_hosts):
            raise ValueError(
                "only explicit loopback, this Mac's Tailscale and en0 addresses are allowed"
            )
        if LAN_HOST in bind_hosts and en0_address() != LAN_HOST:
            raise ValueError("LAN address is not available")
        if token is not None and (
            len(token) < 24 or not token.isascii() or any(c.isspace() for c in token)
        ):
            raise ValueError("invalid gateway token")
        if any(h != HOST for h in bind_hosts) and token is None:
            raise ValueError("private network addresses require a gateway token")
        self.speech = speech
        self.reply = reply
        self.speaker = speaker
        self.speaker_events: list[tuple[str, float | None, bool]] = []
        self.text_fragments = text_fragments
        self.utterance_gap = utterance_gap
        self.max_clients = max_clients
        self.token = token
        self.bind_hosts = bind_hosts
        self.listening_hosts = bind_hosts
        self.ledger = ledger
        self.status_root = status_root
        self.heartbeat_ttl = heartbeat_ttl
        self.clients: set[ClientSession] = set()
        self.brain_calls: set[asyncio.Task[str]] = set()
        self.assistant_ids: dict[str, str] = {}
        self.owner: ClientSession | None = None
        self.coordination_lock = asyncio.Lock()
        self.running = False
        self.last_call_at: str | None = None
        self.bailian_key_valid: bool | None = None
        self.instance_id = uuid.uuid4().hex

    async def observe(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self.ledger is None:
            return None

        def write() -> Any:
            # The gateway owns richer ASR metadata than the initial ledger API.
            # Keep one transcript event, in its existing category and schema.
            if method == "record_user":
                return self.ledger.store.append(
                    "voice_user",
                    {
                        "turn_id": uuid.uuid4().hex,
                        "event": "transcript",
                        "speaker": "user",
                        "text": args[0],
                        "emotion": kwargs.get("emotion"),
                        "asr_confidence": kwargs["asr_confidence"],
                        "audio_duration_ms": kwargs["audio_duration_ms"],
                        "timestamp": datetime.now(UTC).isoformat(),
                        "device_id": kwargs["device_id"],
                    },
                    source=kwargs["device_id"],
                )
            if (
                method == "record_tts"
                and kwargs.get("status") == "success"
                and kwargs.get("audio_duration_ms") is None
            ):
                # Existing record_tts requires known audio duration. Transport
                # success is observable, duration is not: append an explicit
                # unknown-duration outcome instead of fabricating milliseconds.
                turn_id, assistant_id = args
                return self.ledger.store.append(
                    "voice_assistant",
                    {
                        "turn_id": turn_id,
                        "event": "tts",
                        "speaker": "jarvis",
                        "assistant_id": assistant_id,
                        "status": "success",
                        "voice_id": kwargs["voice_id"],
                        "audio_duration_ms": None,
                        "error": None,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                    source="tts",
                    refs=[assistant_id],
                )
            return getattr(self.ledger, method)(*args, **kwargs)

        return await asyncio.to_thread(write)

    def publish_status(self) -> None:
        if self.status_root is None:
            return
        target = self.status_root / "gateway-status.json"
        value = {
            "pid": os.getpid(),
            "instance_id": self.instance_id,
            "updated_at": time.time(),
            "gateway_running": self.running,
            "bailian_key_valid": self.bailian_key_valid,
            "last_call_at": self.last_call_at,
        }
        temporary = target.with_name(".gateway-status-" + self.instance_id)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def provider_result(self, error: Exception | None) -> None:
        if isinstance(self.speech, BailianSpeech):
            if error is None:
                self.bailian_key_valid = True
            elif getattr(error, "code", None) == "invalid_key":
                self.bailian_key_valid = False
            self.publish_status()

    @staticmethod
    def validate_state(value: Any) -> None:
        if not isinstance(value, dict):
            raise ValueError("invalid state")
        state = cast(dict[str, Any], value)
        if len(state) > 64:
            raise ValueError("invalid state")
        for key in (
            "wearing",
            "locked",
            "face_down",
            "bluetooth_enabled",
            "headphones_worn",
            "app_foreground",
            "headphones_connected",
            "screen_on",
            "charging",
        ):
            if key in state and state[key] is not None and type(state[key]) is not bool:
                raise ValueError("invalid state boolean")
        for key in ("signal_strength", "recently_used", "normalized_doa_variance"):
            if key in state and state[key] is not None:
                number = state[key]
                if type(number) not in (int, float) or not 0 <= number <= 1:
                    raise ValueError("invalid normalized telemetry")

    async def coordinate(self) -> None:
        from device_routing import DeviceState, arbitrate

        async with self.coordination_lock:
            now = time.monotonic()
            live = [
                c
                for c in self.clients
                if c.device_type is not None and now - c.state_at < self.heartbeat_ttl
            ]
            states: list[DeviceState] = []
            for client in live:
                state = client.state
                doa = state.get("normalized_doa_variance")
                states.append(
                    DeviceState(
                        client.device_id,
                        cast(str, client.device_type),
                        signal_strength=state.get("signal_strength") or 0,
                        recently_used=state.get("recently_used") or 0,
                        normalized_doa_variance=doa if doa is not None else 1,
                        wearing=state.get("wearing") is True,
                        face_down=state.get("face_down") is True,
                        locked=state.get("locked") is True,
                        bluetooth_enabled=state.get("bluetooth_enabled") is not False,
                        headphones_worn=state.get("headphones_worn") is True,
                    )
                )
            try:
                winner = arbitrate(states)
            except ValueError:
                winner = None
            # Pin an admitted utterance through ASR/brain/TTS/playback. Telemetry
            # changes cannot hand a partly-captured utterance to another device.
            # An owner that has finished capturing and is only waiting on the brain
            # ("thinking") does NOT hold the channel: another device that starts
            # speaking takes over, and its first frame interrupts the pending turn.
            # Without this the microphone stays on one device for the whole
            # brain call and the other devices are suppressed the entire time.
            if (
                self.owner is not None
                and self.owner in live
                and (self.owner.buffer or self.owner.playing or not self.owner.queue.empty())
                and any(
                    s.device_id == self.owner.device_id
                    and s.online
                    and not s.locked
                    and not s.face_down
                    and s.bluetooth_enabled
                    for s in states
                )
            ):
                winner = self.owner.device_id
            self.owner = next((c for c in live if c.device_id == winner), None)
            LOG.info(
                "arbitrate live=%s owner=%s",
                [c.device_id for c in live],
                self.owner.device_id if self.owner else None,
            )
            for client in list(self.clients):
                if client.device_type is None:
                    continue
                selected = client is self.owner
                if client.selected != selected:
                    client.selected = selected
                    if not selected:
                        client.buffer.clear()
                        if client.timer is not None:
                            client.timer.cancel()
                        await client.interrupt()
                    with contextlib.suppress(ConnectionClosed, TimeoutError):
                        await client.send({"type": "selected" if selected else "suppressed"})

    async def admit(self, client: ClientSession) -> bool:
        if client.device_type is None:
            return self.token is None  # Explicit loopback fixture/legacy compatibility.
        await self.coordinate()
        if client is not self.owner:
            await client.send({"type": "suppressed"})
            return False
        return True

    async def answer(
        self,
        bridge: BrainBridge,
        text: str,
        *,
        turn_id: str | None = None,
        started: float | None = None,
    ) -> str:
        if len(self.brain_calls) >= self.max_clients:
            raise RuntimeError("brain_busy")

        async def submit() -> str:
            answer = await bridge.answer(text)
            if turn_id is not None:
                row = await self.observe(
                    "record_assistant",
                    turn_id,
                    answer,
                    generation_ms=(time.monotonic() - (started or time.monotonic())) * 1000,
                )
                if row:
                    # Bounded diagnostic handoff, including results from clients
                    # that disconnected after submitting their one brain call.
                    if len(self.assistant_ids) >= self.max_clients * 2:
                        self.assistant_ids.pop(next(iter(self.assistant_ids)))
                    self.assistant_ids[turn_id] = row["id"]
            return answer

        task = asyncio.create_task(submit())
        self.brain_calls.add(task)

        def done(call: asyncio.Task[str]) -> None:
            self.brain_calls.discard(call)
            if not call.cancelled():
                call.exception()

        task.add_done_callback(done)
        return await asyncio.shield(task)

    async def authenticate(self, connection: ServerConnection, request: Any) -> Any:
        if self.token is not None:
            headers = request.headers.get_all("Authorization")
            if len(headers) != 1 or not hmac.compare_digest(headers[0], "Bearer " + self.token):
                response = connection.respond(
                    HTTPStatus.UNAUTHORIZED, "gateway authorization required\n"
                )
                response.headers["WWW-Authenticate"] = "Bearer"
                return response
        return None

    def save_utterance(self, device_id: str, pcm: bytes) -> None:
        """Keep the newest utterances so the speaker reference can be calibrated."""
        try:
            import wave

            UTTERANCE_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            path = UTTERANCE_DIR / f"{stamp}-{device_id[:24]}.wav"
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(pcm)
            files = sorted(UTTERANCE_DIR.glob("*.wav"))
            for stale in files[:-40]:
                stale.unlink(missing_ok=True)
        except Exception as exc:
            LOG.warning("utterance_save_failed error_type=%s", type(exc).__name__)

    async def handle(self, websocket: ServerConnection) -> None:
        if len(self.clients) >= self.max_clients:
            await websocket.close(1013, "busy")
            return
        client = ClientSession(self, websocket)
        self.clients.add(client)
        LOG.info("client_connected")
        try:
            await client.run()
        finally:
            self.clients.discard(client)
            await self.coordinate()

    @contextlib.asynccontextmanager
    async def serve(self, *, port: int = PORT) -> AsyncGenerator[Any]:
        servers: list[Any] = []
        failed: list[str] = []
        for host in self.bind_hosts:
            try:
                server = await serve(
                    self.handle,
                    host,
                    port,
                    max_size=MAX_MESSAGE_BYTES,
                    max_queue=4,
                    compression=None,
                    origins=[None],
                    logger=LOG,
                    process_request=self.authenticate,
                )
            except OSError as exc:
                if host == HOST:
                    raise
                failed.append(host)
                LOG.warning(
                    "optional_bind_unavailable host=%s error_type=%s", host, type(exc).__name__
                )
                continue
            servers.append(server)
        if not servers:
            raise OSError("no gateway address available")
        self.listening_hosts = tuple(host for host in self.bind_hosts if host not in failed)
        self.running = True
        self.publish_status()

        class ServerGroup:
            sockets = [sock for server in servers for sock in server.sockets]

        async def rebind() -> None:
            # launchd can start the gateway before Tailscale or the LAN address
            # exists; a failed optional bind must not leave the gateway loopback-only
            # until the next manual restart.
            while True:
                await asyncio.sleep(10)
                missing = [h for h in self.bind_hosts if h not in self.listening_hosts]
                if not missing:
                    continue
                for host in missing:
                    try:
                        server = await serve(
                            self.handle,
                            host,
                            port,
                            max_size=MAX_MESSAGE_BYTES,
                            max_queue=4,
                            compression=None,
                            origins=[None],
                            logger=LOG,
                            process_request=self.authenticate,
                        )
                    except OSError as exc:
                        LOG.warning(
                            "rebind_unavailable host=%s error_type=%s",
                            host,
                            type(exc).__name__,
                        )
                        continue
                    servers.append(server)
                    self.listening_hosts = tuple(
                        h for h in self.bind_hosts if h in self.listening_hosts or h == host
                    )
                    LOG.info("rebind_ok host=%s hosts=%s", host, self.listening_hosts)

        async def conversation_watch() -> None:
            while True:
                await asyncio.sleep(2)
                owner = self.owner
                if owner is None or not owner.conversation_until:
                    continue
                if time.monotonic() < owner.conversation_until:
                    continue
                owner.conversation_until = 0.0
                LOG.info("conversation_ended device=%s", owner.device_id)
                await owner.prompt(CONVERSATION_PROMPT)

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(min(5, self.heartbeat_ttl / 2))
                await self.coordinate()
                self.publish_status()

        task = asyncio.create_task(heartbeat())
        rebind_task = asyncio.create_task(rebind())
        conversation_task = asyncio.create_task(conversation_watch())
        try:
            yield ServerGroup()
        finally:
            self.running = False
            task.cancel()
            rebind_task.cancel()
            conversation_task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for server in servers:
                server.close()
                await server.wait_closed()
            self.publish_status()

    async def close(self) -> None:
        await asyncio.gather(*(client.ws.close(1001, "shutdown") for client in list(self.clients)))
        if self.brain_calls:
            await asyncio.gather(*list(self.brain_calls), return_exceptions=True)
        self.running = False
        self.publish_status()


def read_gateway_status(root: Path) -> dict[str, Any]:
    """Read a bounded private runtime heartbeat, never credentials or the cloud."""
    import stat

    path = Path(root) / "gateway-status.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "r") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 4096
        ):
            raise PermissionError("unsafe gateway status snapshot")
        value = json.load(handle)
    pid = value.get("pid")
    live = False
    if type(pid) is int and pid > 0:
        try:
            os.kill(pid, 0)
            live = True
        except ProcessLookupError:
            pass
    age = time.time() - value.get("updated_at", 0)
    return {
        "gateway_running": value.get("gateway_running") is True and live and 0 <= age < 20,
        "bailian_key_valid": value.get("bailian_key_valid"),
        "last_call_at": value.get("last_call_at"),
    }


class BailianSpeech:
    """Run the existing emotion-aware REST adapter off the asyncio event loop."""

    def __init__(self) -> None:
        from voice_dashscope import VOICE_ID

        self.voice = VOICE_ID
        self._inflight: set[asyncio.Task[dict[str, Any]]] = set()

    async def _request(self, function: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
        # asyncio cancellation cannot stop urllib in a worker thread. Keep the
        # slot until the real call ends, including disconnected/interrupted work.
        if len(self._inflight) >= 4:
            raise RuntimeError("speech_busy")
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        self._inflight.add(task)

        def done(completed: asyncio.Task[dict[str, Any]]) -> None:
            self._inflight.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        return await asyncio.shield(task)

    # One stuck request used to cost a full 60s before the reply started.
    # Bound each attempt and retry once on a fresh connection.
    ASR_ATTEMPT_TIMEOUT = 12.0

    async def transcribe(self, pcm: bytes) -> dict[str, Any]:
        from voice_dashscope import asr_transcribe

        last: Exception | None = None
        for attempt in (1, 2):
            try:
                return await self._request(
                    lambda data: asr_transcribe(data, timeout=self.ASR_ATTEMPT_TIMEOUT),
                    pcm,
                )
            except Exception as exc:
                last = exc
                LOG.warning("asr_retry attempt=%d error_type=%s", attempt, type(exc).__name__)
        assert last is not None
        raise last

    def create_stream(self) -> StreamingTTS:
        from voice_dashscope import _voice_parameters  # pyright: ignore[reportPrivateUsage]

        parameters = _voice_parameters()
        return StreamingTTS(
            voice=self.voice,
            sample_rate=16000,
            rate=parameters.get("rate", 1.0),
            pitch=parameters.get("pitch", 1.0),
            comma_threshold=1,
        )

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        from voice_dashscope import tts_synthesize

        # REST provider bounds text at 2000 characters. Each chunk stays ordered;
        # MP3 byte chunks are transported with one utterance generation.
        for start in range(0, len(text), 2000):
            result = await self._request(tts_synthesize, text[start : start + 2000])
            audio = result["audio_bytes"]
            for offset in range(0, len(audio), 65536):
                yield audio[offset : offset + 65536]


class VolcengineBailianSpeech:
    """ASR stays on Bailian; TTS uses Volcengine seed-tts-2.0 MP3 chunks.

    Deliberately has no create_stream(): the gateway then skips the PCM path
    and streams Volcengine MP3 chunks through the existing fallback branch.
    """

    def __init__(self) -> None:
        from voice_volcengine import VolcengineTTS

        self._asr = BailianSpeech()
        self._tts = VolcengineTTS()
        self.voice = self._tts.voice

    async def transcribe(self, pcm: bytes) -> dict[str, Any]:
        return await self._asr.transcribe(pcm)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        async for chunk in self._tts.synthesize(text):
            yield chunk


def build_speech(provider: str) -> Any:
    if provider == "bailian":
        return BailianSpeech()
    if provider == "volcengine":
        return VolcengineBailianSpeech()
    raise ValueError(f"unknown tts provider: {provider}")


class DashScopeSpeech:
    """Native asyncio DashScope inference protocol (no blocking vendor SDK)."""

    URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    VOICE = "cosyvoice-v3.5-flash-jarvis-f7bec1631ddf4725841995ba9c711376"

    def __init__(
        self, key: str, *, voice: str = VOICE, connector: Any = None, task_timeout: float = 90
    ) -> None:
        from websockets.asyncio.client import connect

        self.key = key
        self.voice = voice
        self.connector = connector or connect
        self.task_timeout = task_timeout

    def create_stream(self) -> StreamingTTS:
        return StreamingTTS(
            voice=self.voice,
            sample_rate=16000,
            key_path=Path.home() / ".jarvis" / "dashscope.key",
            comma_threshold=1,
        )

    @staticmethod
    def message(action: str, task_id: str, payload: dict[str, Any]) -> str:
        return json.dumps(
            {
                "header": {"action": action, "task_id": task_id, "streaming": "duplex"},
                "payload": payload,
            }
        )

    def connection(self) -> Any:
        return self.connector(
            self.URL,
            additional_headers={"Authorization": f"bearer {self.key}"},
            proxy=None,
            open_timeout=10,
            close_timeout=3,
            max_size=2**20,
            max_queue=4,
        )

    @staticmethod
    def event(raw: str | bytes, task_id: str) -> dict[str, Any]:
        value = json.loads(raw)
        header = value["header"]
        if header.get("task_id") != task_id:
            raise RuntimeError("provider_task_mismatch")
        if header.get("event") == "task-failed":
            # Provider error_message can contain private text. Never echo it.
            raise RuntimeError("provider_task_failed")
        return value

    async def transcribe(self, pcm: bytes) -> str:
        import uuid

        task_id = uuid.uuid4().hex
        texts: list[str] = []
        sender: asyncio.Task[None] | None = None
        finished = False
        async with asyncio.timeout(self.task_timeout), self.connection() as ws:
            await ws.send(
                self.message(
                    "run-task",
                    task_id,
                    {
                        "task_group": "audio",
                        "task": "asr",
                        "function": "recognition",
                        "model": "fun-asr-realtime",
                        "parameters": {"format": "pcm", "sample_rate": 16000},
                        "input": {},
                    },
                )
            )

            async def send_audio() -> None:
                for start in range(0, len(pcm), 3200):
                    await ws.send(pcm[start : start + 3200])
                    await asyncio.sleep(len(pcm[start : start + 3200]) / 32000)
                await ws.send(self.message("finish-task", task_id, {"input": {}}))

            try:
                async for raw in ws:
                    value = self.event(raw, task_id)
                    event = value["header"].get("event")
                    if event == "task-started" and sender is None:
                        sender = asyncio.create_task(send_audio())
                    elif event == "result-generated":
                        sentence = value.get("payload", {}).get("output", {}).get("sentence", {})
                        if sentence.get("sentence_end") is True:
                            texts.append(sentence.get("text", ""))
                    elif event == "task-finished":
                        finished = True
                        break
                if not finished:
                    raise RuntimeError("asr_incomplete")
                if sender is not None:
                    await sender
            finally:
                if sender is not None:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
        return "".join(texts)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        import uuid

        task_id = uuid.uuid4().hex
        sent = False
        async with asyncio.timeout(self.task_timeout), self.connection() as ws:
            await ws.send(
                self.message(
                    "run-task",
                    task_id,
                    {
                        "task_group": "audio",
                        "task": "tts",
                        "function": "SpeechSynthesizer",
                        "model": "cosyvoice-v3.5-flash",
                        "parameters": {
                            "text_type": "PlainText",
                            "voice": self.voice,
                            "format": "mp3",
                            "sample_rate": 22050,
                        },
                        "input": {},
                    },
                )
            )
            async for raw in ws:
                if isinstance(raw, bytes):
                    if not sent:
                        raise RuntimeError("tts_audio_before_start")
                    yield raw
                    continue
                value = self.event(raw, task_id)
                event = value["header"].get("event")
                if event == "task-started" and not sent:
                    sent = True
                    await ws.send(self.message("continue-task", task_id, {"input": {"text": text}}))
                    await ws.send(self.message("finish-task", task_id, {"input": {}}))
                elif event == "task-finished":
                    return
            raise RuntimeError("tts_incomplete")


class LauncherBrain:
    """Existing chat_launcher -> chat_entry, same data root/timeline as daemon."""

    def __init__(self, config: Any, key: str, root: Any) -> None:
        import chat_launcher

        self.launcher = chat_launcher
        self.config = config
        self.key = key
        self.root = root
        self.lock = asyncio.Lock()

    def _reply(self, text: str) -> str:
        import uuid

        result = self.launcher.launch(
            self.config,
            self.key,
            text,
            self.root,
            self.launcher.timeline_identity(self.root),
            attempt_id=uuid.uuid4().hex,
            source_channel="voice",
            # The resident daemon uses this same root. Queue before reservation,
            # never resubmit an attempt whose model outcome may be unknown. The
            # wait is bounded so a busy daemon cannot hold the voice channel for a
            # whole minute; the daemon itself passes 0 and simply retries later.
            lock_wait_timeout=20.0,
            # 语音是聊天轮次，不产生外部副作用；一次结果未知不该让助手永久哑掉，
            # 但要留痕，便于事后核对（日志 launch_after_unknown_continue）。
            after_unknown="continue",
        )
        answer = result.get("text")
        if (
            result.get("ok") is not True
            or result.get("generation_degraded")
            or not isinstance(answer, str)
            or not answer.strip()
            or self.key in answer
        ):
            state = result.get("model_request_state")
            status = result.get("provider_status")
            if state == "provider_rejected":
                raise BrainOutcomeError(
                    "brain_provider_rejected",
                    provider_status=status if type(status) is int else None,
                )
            raise BrainOutcomeError("brain_outcome_unknown")
        return answer

    async def __call__(self, text: str) -> str:
        async with self.lock:
            return await asyncio.to_thread(self._reply, text)


class TimelineBrain:
    """Reuse existing single-POST, voice-tagged timeline client if Web is running."""

    def __init__(self, url: str) -> None:
        from voice_live import TimelineClient

        self.client = TimelineClient(url)
        self.lock = asyncio.Lock()

    async def __call__(self, text: str) -> str:
        async with self.lock:
            return await asyncio.to_thread(self.client.reply, text)


def self_evaluate() -> dict[str, Any]:
    from datetime import UTC, datetime

    return {
        "module": "voice_gateway",
        "domain": "authenticated private-address multi-device BRAIN voice transport",
        "current_version": "1.0",
        "known_limitations": [
            "Utterances use transport gap or audio_end; clients supply wake/VAD/AEC.",
            "Barge-in discards stale speech, not already-submitted brain operations.",
            "Only this Mac's explicit tailnet IP is bound; external WSS termination is separate.",
            "Playback callbacks are observations, not physical hearing confirmation.",
            "Daemon and gateway share launcher lock; active daemon turns can return busy/unknown.",
            "Capability/KG registration and resident assembly require separate integration.",
        ],
        "improvement_directions": [
            "Add native VAD end markers, playback progress and daemon inbox routing."
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


async def run_cli(args: Any) -> None:
    import signal

    from inner_loop import MindStore
    from voice_ledger import VoiceLedger

    # BailianSpeech validates ~/.jarvis/dashscope.key on the first real vendor call.
    token = await asyncio.to_thread(read_gateway_token, args.token_file)
    root = await asyncio.to_thread(__import__("chat_launcher").prepare_root, args.data_dir)
    if args.timeline_url:
        brain: Any = TimelineBrain(args.timeline_url)
    else:
        import chat_launcher

        config = await asyncio.to_thread(chat_launcher.read_config, args.config, None)
        brain_key = await asyncio.to_thread(
            chat_launcher.read_key, key_file=args.brain_key, key_fd=None
        )
        root = await asyncio.to_thread(chat_launcher.prepare_root, args.data_dir)
        brain = LauncherBrain(config, brain_key, root)
    ledger = await asyncio.to_thread(lambda: VoiceLedger(MindStore(root)))
    speaker = None
    try:
        from voice_speaker import SpeakerVerifier, ensure_reference

        speaker = await asyncio.to_thread(SpeakerVerifier)
        if await asyncio.to_thread(ensure_reference, speaker):
            LOG.info("speaker_verification_ready")
        else:
            LOG.warning("speaker_verification_unavailable reason=no_reference")
            speaker = None
    except Exception as exc:
        LOG.warning("speaker_verification_unavailable error_type=%s", type(exc).__name__)
    app = VoiceGateway(
        build_speech(args.tts),
        brain,
        utterance_gap=args.utterance_gap,
        token=token,
        bind_hosts=configured_bind_hosts(),
        ledger=ledger,
        status_root=root,
        speaker=speaker,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        async with app.serve():
            LOG.info("listening hosts=%s port=%d", app.listening_hosts, PORT)
            print(f"Voice gateway listening on {app.listening_hosts} port {PORT}", flush=True)
            await stop.wait()
    finally:
        await app.close()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def main() -> int:
    import argparse
    import logging.handlers
    import queue

    parser = argparse.ArgumentParser(description=__doc__)
    home = Path.home() / ".jarvis"
    parser.add_argument("--token-file", type=Path, default=home / "voice-gateway.token")
    parser.add_argument("--config", type=Path, default=home / "config.json")
    parser.add_argument("--brain-key", type=Path, default=home / "api.key")
    parser.add_argument("--data-dir", type=Path, default=home / "loop-data")
    parser.add_argument(
        "--tts",
        choices=("volcengine", "bailian"),
        default=os.environ.get("JARVIS_TTS_PROVIDER", "volcengine"),
        help="TTS provider (ASR always uses Bailian)",
    )
    parser.add_argument(
        "--timeline-url", help="Optional existing loopback Web origin (not port 8765)"
    )
    parser.add_argument("--utterance-gap", type=float, default=0.5)
    parser.add_argument(
        "--log",
        type=Path,
        default=Path.home() / ".jarvis" / "voice-gateway.log",
    )
    args = parser.parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(args.log, maxBytes=2**20, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    records: queue.Queue[Any] = queue.Queue()
    LOG.addHandler(logging.handlers.QueueHandler(records))
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    listener = logging.handlers.QueueListener(records, handler)
    listener.start()
    try:
        asyncio.run(run_cli(args))
        return 0
    except Exception as exc:
        LOG.error("gateway_failed error_type=%s", type(exc).__name__)
        print(f"Voice gateway failed ({type(exc).__name__}); see log.")
        return 1
    finally:
        listener.stop()
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
