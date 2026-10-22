"""Incremental CosyVoice TTS over the Bailian duplex WebSocket protocol.

The public entry point is :meth:`StreamingTTS.stream`: it accepts an async
iterator of text fragments and yields raw PCM16 audio chunks as soon as the
service sends them. PCM is used instead of MP3 because it can be played
without waiting for a complete compressed frame or decoding a file.

It is a library, not a service: voice_dashscope exposes it as tts_stream_pcm()
and the voice gateway keeps using the REST MP3 path until every client can play
raw PCM. It reads the DashScope key only from the owner-only key file and never
logs credentials.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import stat
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from typing import Any, cast

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

DEFAULT_ENDPOINT = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"
DEFAULT_KEY_PATH = Path.home() / ".jarvis" / "dashscope.key"
MODEL = "cosyvoice-v3.5-flash"
VOICE = "cosyvoice-v3.5-flash-jarvis-f7bec1631ddf4725841995ba9c711376"


class StreamingTTSError(RuntimeError):
    """Base class for sanitized streaming TTS failures."""

    code = "streaming_tts_error"


class AuthenticationError(StreamingTTSError):
    """The API key was rejected during the handshake or task."""

    code = "authentication"


class QuotaError(StreamingTTSError):
    """The provider rejected the request because of quota or balance."""

    code = "quota"


class NetworkError(StreamingTTSError):
    """The WebSocket could not be established or was lost unexpectedly."""

    code = "network"


class ProviderError(StreamingTTSError):
    """The provider rejected a task for a reason other than auth or quota."""

    code = "provider"


class SentenceBuffer:
    """Release complete sentences from text fragments without losing text."""

    def __init__(self, *, comma_threshold: int = 18) -> None:
        if comma_threshold < 1:
            raise ValueError("comma_threshold must be positive")
        self.comma_threshold = comma_threshold
        self._pending = ""

    def feed(self, fragment: str) -> list[str]:
        """Append text and return segments ready for synthesis."""
        ready: list[str] = []
        for char in fragment:
            self._pending += char
            if char in "。？！.!?\n" or (
                char in "，," and len(self._pending) >= self.comma_threshold
            ):
                ready.append(self._pending)
                self._pending = ""
        return ready

    def finish(self) -> list[str]:
        """Flush the final incomplete segment at end-of-input."""
        if not self._pending:
            return []
        pending, self._pending = self._pending, ""
        return [pending]


def _read_key(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="ascii") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
                or info.st_size > 4096
            ):
                raise AuthenticationError("DashScope key file must be owner-only mode 0600")
            key = handle.read(4097).strip()
    except (OSError, UnicodeError):
        raise AuthenticationError("DashScope key file cannot be read") from None
    if not key or any(char.isspace() for char in key) or not key.isascii():
        raise AuthenticationError("DashScope key file is invalid")
    return key


def _provider_failure(header: dict[str, Any]) -> StreamingTTSError:
    code = str(header.get("error_code", "")).lower()
    message = str(header.get("error_message", "")).lower()
    marker = code + " " + message
    if any(word in marker for word in ("401", "403", "apikey", "api_key", "unauthor")):
        return AuthenticationError("authentication failed (authentication)")
    if any(word in marker for word in ("quota", "balance", "arrearage", "insufficient")):
        return QuotaError("quota or balance exhausted (quota)")
    return ProviderError("DashScope rejected the TTS task (provider)")


def _network_error(exc: BaseException) -> NetworkError:
    del exc
    return NetworkError("DashScope WebSocket connection failed (network)")


class StreamingTTS:
    """One cancellable duplex TTS stream.

    An instance supports one active ``stream`` call at a time. ``cancel`` is
    synchronous so an interrupt handler can call it immediately; it schedules
    the socket close on the current event loop and prevents further sends.
    """

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        key_path: Path = DEFAULT_KEY_PATH,
        model: str = MODEL,
        voice: str = VOICE,
        sample_rate: int = 24_000,
        rate: float = 1.0,
        pitch: float = 1.0,
        comma_threshold: int = 18,
        connect_timeout: float = 10.0,
        task_timeout: float | None = 90.0,
    ) -> None:
        if not endpoint.startswith(("ws://", "wss://")):
            raise ValueError("endpoint must use ws:// or wss://")
        if sample_rate <= 0:
            raise ValueError("sample_rate must be a positive integer")
        for name, value in (("rate", rate), ("pitch", pitch)):
            if isinstance(value, bool) or not math.isfinite(value) or not 0.5 <= value <= 2.0:
                raise ValueError(f"{name} must be finite and between 0.5 and 2.0")
        if connect_timeout <= 0 or not math.isfinite(connect_timeout):
            raise ValueError("connect_timeout must be finite and positive")
        if task_timeout is not None and (task_timeout <= 0 or not math.isfinite(task_timeout)):
            raise ValueError("task_timeout must be finite and positive")
        self.endpoint = endpoint
        self.key_path = key_path
        self.model = model
        self.voice = voice
        self.sample_rate = sample_rate
        self.rate = rate
        self.pitch = pitch
        self.comma_threshold = comma_threshold
        self.connect_timeout = connect_timeout
        self.task_timeout = task_timeout
        self._socket: ClientConnection | None = None
        self._cancelled = asyncio.Event()
        self._cancel_task: asyncio.Task[None] | None = None
        self._active = False

    def cancel(self) -> None:
        """Stop sending and close the current WebSocket as soon as possible."""
        self._cancelled.set()
        socket = self._socket
        if socket is not None and self._cancel_task is None:
            self._cancel_task = asyncio.create_task(socket.close())

    async def stream(self, fragments: AsyncIterable[str]) -> AsyncIterator[bytes]:
        """Yield raw PCM audio for progressively supplied text fragments."""
        if self._active:
            raise RuntimeError("a TTS stream is already active")
        self._active = True
        self._cancelled = asyncio.Event()
        self._cancel_task = None
        socket: ClientConnection | None = None
        try:
            async with asyncio.timeout(self.task_timeout):
                key = _read_key(self.key_path)
            socket = await self._open(key)
            self._socket = socket
            if self._cancelled.is_set():
                await socket.close()
                return
            task_id = str(uuid.uuid4())
            await socket.send(json.dumps(self._run_task(task_id), ensure_ascii=False))
            await self._wait_for_started(socket, task_id)
            queue: asyncio.Queue[object] = asyncio.Queue(maxsize=32)
            runner = asyncio.create_task(self._run_io(socket, task_id, fragments, queue))
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, BaseException):
                        raise item
                    if not isinstance(item, bytes):
                        raise ProviderError("invalid audio frame from DashScope (provider)")
                    yield item
                await runner
            finally:
                if not runner.done():
                    runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
        except StreamingTTSError:
            raise
        except asyncio.CancelledError:
            self.cancel()
            raise
        except (ConnectionClosed, OSError, InvalidStatus) as exc:
            raise _network_error(exc) from None
        finally:
            self._socket = None
            self._active = False
            if socket is not None:
                await socket.close()
            if self._cancel_task is not None:
                await asyncio.gather(self._cancel_task, return_exceptions=True)
                self._cancel_task = None

    async def _open(self, key: str) -> ClientConnection:
        try:
            return await connect(
                self.endpoint,
                additional_headers={"Authorization": "Bearer " + key},
                open_timeout=self.connect_timeout,
                proxy=None,
                max_size=16 * 1024 * 1024,
            )
        except InvalidStatus as exc:
            response: Any = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if status in (401, 403):
                raise AuthenticationError("authentication failed (authentication)") from None
            if status == 402:
                raise QuotaError("quota or balance exhausted (quota)") from None
            raise _network_error(exc) from None
        except (OSError, TimeoutError) as exc:
            raise _network_error(exc) from None

    def _run_task(self, task_id: str) -> dict[str, Any]:
        return {
            "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": self.model,
                "parameters": {
                    "text_type": "PlainText",
                    "voice": self.voice,
                    "format": "pcm",
                    "sample_rate": self.sample_rate,
                    "volume": 50,
                    "rate": self.rate,
                    "pitch": self.pitch,
                    "enable_ssml": False,
                },
                "input": {},
            },
        }

    async def _wait_for_started(self, socket: ClientConnection, task_id: str) -> None:
        try:
            raw = await socket.recv()
        except (ConnectionClosed, OSError, TimeoutError) as exc:
            raise _network_error(exc) from None
        if not isinstance(raw, str):
            raise ProviderError("invalid task-started response (provider)")
        event = self._json_event(raw, task_id)
        if event.get("event") == "task-failed":
            raise _provider_failure(event)
        if event.get("event") != "task-started":
            raise ProviderError("DashScope did not start the TTS task (provider)")

    async def _run_io(
        self,
        socket: ClientConnection,
        task_id: str,
        fragments: AsyncIterable[str],
        queue: asyncio.Queue[object],
    ) -> None:
        sender = asyncio.create_task(self._send_text(socket, task_id, fragments))
        receiver = asyncio.create_task(self._receive_audio(socket, task_id, queue))
        try:
            done, _ = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
            first = done.pop()
            if first is sender:
                error = first.exception()
                if error is not None:
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
                    if not self._cancelled.is_set():
                        await queue.put(error)
                    return
                await receiver
            else:
                error = first.exception()
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                if error is not None and not self._cancelled.is_set():
                    await queue.put(error)
        except asyncio.CancelledError:
            sender.cancel()
            receiver.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
            raise
        except (StreamingTTSError, ConnectionClosed, OSError, TimeoutError) as exc:
            sender.cancel()
            receiver.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
            if not self._cancelled.is_set():
                await queue.put(exc if isinstance(exc, StreamingTTSError) else _network_error(exc))
        finally:
            await queue.put(None)

    async def _send_text(
        self,
        socket: ClientConnection,
        task_id: str,
        fragments: AsyncIterable[str],
    ) -> None:
        buffer = SentenceBuffer(comma_threshold=self.comma_threshold)
        total = 0
        async for fragment in fragments:
            if self._cancelled.is_set():
                return
            for segment in buffer.feed(fragment):
                total = await self._send_segment(socket, task_id, segment, total)
        if self._cancelled.is_set():
            return
        for segment in buffer.finish():
            total = await self._send_segment(socket, task_id, segment, total)
        if not self._cancelled.is_set():
            await socket.send(json.dumps(self._command("finish-task", task_id, {})))

    async def _send_segment(
        self, socket: ClientConnection, task_id: str, segment: str, total: int
    ) -> int:
        if self._cancelled.is_set():
            return total
        if not segment:
            return total
        if len(segment) > 20_000:
            raise ProviderError("text segment exceeds provider limit (provider)")
        if total + len(segment) > 200_000:
            raise ProviderError("cumulative text exceeds provider limit (provider)")
        await socket.send(json.dumps(self._command("continue-task", task_id, {"text": segment})))
        return total + len(segment)

    async def _receive_audio(
        self, socket: ClientConnection, task_id: str, queue: asyncio.Queue[object]
    ) -> None:
        try:
            async for raw in socket:
                if isinstance(raw, bytes):
                    await queue.put(raw)
                    continue
                event = self._json_event(raw, task_id)
                event_type = event.get("event")
                if event_type == "task-failed":
                    raise _provider_failure(event)
                if event_type == "task-finished":
                    return
        except (ConnectionClosed, OSError, TimeoutError) as exc:
            if not self._cancelled.is_set():
                raise _network_error(exc) from None

    @staticmethod
    def _json_event(raw: str, task_id: str) -> dict[str, Any]:
        try:
            parsed: Any = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError
            parsed_dict: dict[str, Any] = cast(dict[str, Any], parsed)
            header_value: Any = parsed_dict.get("header")
            if not isinstance(header_value, dict):
                raise ValueError
            header = cast(dict[str, Any], header_value)
            if header.get("task_id") not in (None, task_id):
                raise ValueError
            return header
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ProviderError("invalid DashScope event (provider)") from None

    @staticmethod
    def _command(action: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "continue-task":
            payload = {"input": payload}
        else:
            payload = {"input": payload}
        return {
            "header": {"action": action, "task_id": task_id, "streaming": "duplex"},
            "payload": payload,
        }


# Keep the public name explicit for future registry integration without changing
# the current gateway or integration registry.
__all__ = [
    "AuthenticationError",
    "NetworkError",
    "ProviderError",
    "QuotaError",
    "SentenceBuffer",
    "StreamingTTS",
    "StreamingTTSError",
]


def self_evaluate() -> dict[str, object]:
    """Offline declaration only; no socket, credential read or playback here."""
    return {
        "module": "voice_stream_tts",
        "domain": "Bailian CosyVoice duplex streaming TTS (run-task/continue-task/finish-task)",
        "current_version": "1.0.0",
        "known_limitations": [
            "Emits raw PCM16 24 kHz mono; the caller owns framing, playback and any MP3 wrapping.",
            "No product caller yet: voice_gateway still synthesizes via the REST MP3 adapter.",
            "Synthesis success is not proof the audio was heard on a device.",
            "Interruption closes the vendor connection; already-emitted audio cannot be recalled.",
            "Credentials come only from the owner-only key file; no rotation logic.",
        ],
        "improvement_directions": [
            "Feed sentence-level text from a streaming brain so speech starts "
            "before the answer ends.",
            "Report first-chunk and total latency into the voice ledger.",
        ],
        "last_evaluated_at": None,
    }
