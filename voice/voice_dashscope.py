"""Bailian REST speech adapter; no recording, playback or import-time I/O.

Credentials are read only from ~/.jarvis/dashscope.key (owner-only 0600).
ASR input: raw mono signed little-endian PCM16, 16 kHz (not WAV/MP3).
The PCM is wrapped in a WAV container before upload, so rate/width are explicit.
Network requests use the verified Beijing public endpoints; no workspace ID.
Streaming APIs are intentionally deferred. See docs/voice-docs/dashscope_probe.md.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import ssl
import stat
import time
import urllib.error
import urllib.request
import wave
from collections.abc import AsyncIterable, AsyncIterator
from http.client import HTTPException
from pathlib import Path
from typing import Any, cast

KEY_PATH = Path.home() / ".jarvis" / "dashscope.key"
ASR_ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)
TTS_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer"
ASR_MODEL = "qwen3-asr-flash"
TTS_MODEL = "cosyvoice-v3.5-flash"
VOICE_ID = "cosyvoice-v3.5-flash-jarvis-f7bec1631ddf4725841995ba9c711376"
# Emotion is a request parameter, not a property of the cloned voice: the provider
# honours parameters.instruction / rate / pitch (probe 2026-10-20: same text with a
# "cold" instruction produced 74971 B / 3.744 s versus 60571 B / 3.024 s baseline,
# byte-identical across repeats). Without these the voice reads flat.
# 配置文件与语音模块放在一起（voice/voice_config.json），不再放仓库根。
VOICE_CONFIG_PATH = Path(__file__).resolve().parent / "voice_config.json"
VOICE_PARAMETER_KEYS = ("rate", "pitch", "instruction")
MAX_INSTRUCTION_CHARS = 200
logger = logging.getLogger(__name__)


class DashScopeError(RuntimeError):
    """Sanitized failure; provider messages, URLs and credentials are not exposed."""

    def __init__(self, message: str, *, code: str = "provider_error") -> None:
        self.code = code
        super().__init__(message)


def _read_key() -> str:
    try:
        fd = os.open(KEY_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
                or info.st_size > 4096
            ):
                raise DashScopeError(
                    "key file must be owned by this user, regular and mode 0600", code="key_file"
                )
            key = handle.read(4097).strip()
            if not key or any(ch.isspace() for ch in key) or not key.isascii():
                raise DashScopeError(
                    "key file must contain one nonempty ASCII key", code="key_file"
                )
            return key
    except (OSError, UnicodeError):
        raise DashScopeError("cannot read ~/.jarvis/dashscope.key", code="key_file") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        # Never forward API credentials or follow audio redirects to a private host.
        return None


def _open(request: urllib.request.Request, timeout: float) -> Any:
    # A new opener avoids process-global settings; proxy auto-discovery is disabled.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(
        request, timeout=timeout
    )


def _provider_error(status: int, payload: dict[str, Any]) -> DashScopeError:
    # Inspect provider text only for classification; never expose it to callers/logs.
    marker = str(payload.get("code", "")).lower()
    message = str(payload.get("message", "")).lower()
    if status == 401 or "invalidapikey" in marker or "invalid_api_key" in marker:
        return DashScopeError("DashScope API key is invalid", code="invalid_key")
    if status == 402 or any(
        word in marker + message
        for word in (
            "quotaexhausted",
            "quota exhausted",
            "insufficient",
            "arrearage",
            "quota_exhausted",
            "quota exceeded",
            "outofquota",
            "freeallocatedquota",
            "accountinabnormal",
        )
    ):
        return DashScopeError("DashScope quota or balance exhausted", code="quota_exhausted")
    if status == 429 or "throttl" in marker or "ratelimit" in marker:
        return DashScopeError("DashScope rate limit exceeded", code="rate_limited")
    if status == 403 or "accessdenied" in marker:
        return DashScopeError("DashScope model access denied", code="permission_denied")
    if status in (408, 504) or "timeout" in marker:
        return DashScopeError("DashScope request timed out", code="timeout")
    if "invalidparameter" in marker or status in (400, 413, 422):
        return DashScopeError("DashScope rejected request parameters", code="invalid_parameter")
    return DashScopeError("DashScope provider request failed", code="provider_error")


def _fetch(request: urllib.request.Request, timeout: float, limit: int) -> bytes:
    try:
        with _open(request, timeout) as response:
            data: bytes = response.read(limit + 1)
        if len(data) > limit:
            raise DashScopeError("DashScope response exceeds byte limit", code="invalid_response")
        return data
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload: dict[str, Any] = {}
        try:
            obj: Any = json.loads(exc.read(65536))
            if isinstance(obj, dict):
                payload = cast(dict[str, Any], obj)
        except (ValueError, OSError):
            pass
        finally:
            exc.close()
        raise _provider_error(status, payload) from None
    except TimeoutError:
        raise DashScopeError("DashScope network request timed out", code="timeout") from None
    except urllib.error.URLError as exc:
        code = "timeout" if isinstance(exc.reason, TimeoutError) else "network_error"
        raise DashScopeError("DashScope network request failed (" + code + ")", code=code) from None
    except (OSError, ssl.SSLError, HTTPException):
        raise DashScopeError("DashScope network request failed", code="network_error") from None


def _post(endpoint: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Authorization": "Bearer " + _read_key(), "Content-Type": "application/json"},
    )
    try:
        result: Any = json.loads(_fetch(request, timeout, 2 * 1024 * 1024))
    except (ValueError, UnicodeError):
        raise DashScopeError("invalid provider JSON", code="invalid_response") from None
    if not isinstance(result, dict):
        raise DashScopeError("invalid provider response", code="invalid_response")
    result = cast(dict[str, Any], result)
    if result.get("code") and result["code"] not in ("200", 200, "Success"):
        raise _provider_error(200, result)
    return result


def _validate_timeout(timeout: object) -> None:
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")


def _pcm(value: object) -> bytes:
    if not isinstance(value, bytes) or not value or len(value) % 2:
        raise ValueError("audio must be nonempty, even-length raw PCM16 bytes")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError("text must be nonempty and at most 2000 characters; split long text")
    return value


def _text_length(value: object) -> int:
    return len(value) if isinstance(value, str) else 0


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return cast(dict[str, Any], value)


def _array(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("expected JSON array")
    return cast(list[Any], value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected string")
    return value


def _usage(result: dict[str, Any]) -> dict[str, Any]:
    usage: Any = result.get("usage", {})
    if not isinstance(usage, dict):
        raise DashScopeError("invalid provider usage", code="invalid_response")
    # Provider usage is returned intact, but only fixed numeric metrics enter logs.
    return cast(dict[str, Any], usage)


def _log_usage(usage: dict[str, Any]) -> str:
    metrics = {
        name: value
        for name, value in usage.items()
        if name
        in (
            "characters",
            "audio_tokens",
            "input_tokens",
            "output_tokens",
            "seconds",
            "total_tokens",
        )
        and isinstance(value, (int, float))
        and math.isfinite(value)
    }
    return json.dumps(metrics, separators=(",", ":"))


def asr_transcribe(pcm_bytes: bytes, *, timeout: float = 60.0) -> dict[str, Any]:
    """Recognize mono PCM16/16k; return text, emotion, language and provider usage."""
    start = time.monotonic()
    chars = 0
    usage: dict[str, Any] = {}
    status = "error"
    try:
        _validate_timeout(timeout)
        pcm_bytes = _pcm(pcm_bytes)
        if pcm_bytes.startswith((b"RIFF", b"ID3", b"fLaC", b"OggS")):
            raise ValueError("encoded audio/container supplied; raw PCM16 is required")
        if len(pcm_bytes) > 16000 * 2 * 180:
            raise ValueError("audio exceeds 180 seconds; split before transcription")
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm_bytes)
        encoded = base64.b64encode(audio.getvalue()).decode("ascii")
        result = _post(
            ASR_ENDPOINT,
            {
                "model": ASR_MODEL,
                "input": {
                    "messages": [
                        {"role": "user", "content": [{"audio": "data:audio/wav;base64," + encoded}]}
                    ]
                },
            },
            timeout,
        )
        try:
            output = _object(result["output"])
            choice = _object(_array(output["choices"])[0])
            message = _object(choice["message"])
            contents = [_object(part) for part in _array(message["content"])]
            if not any("text" in part for part in contents):
                raise ValueError("missing text content")
            text = "".join(_string(part["text"]) for part in contents if "text" in part)
            annotations = [_object(a) for a in _array(message.get("annotations", []))]
            info: dict[str, Any] = {}
            for annotation in annotations:
                if annotation.get("type") == "audio_info":
                    info = annotation
                    break
            usage = _usage(result)
            if (info.get("emotion") is not None and not isinstance(info["emotion"], str)) or (
                info.get("language") is not None and not isinstance(info["language"], str)
            ):
                raise ValueError("invalid annotations")
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            raise DashScopeError("invalid ASR response", code="invalid_response") from None
        chars = len(text)
        status = "ok"
        return {
            "text": text,
            "emotion": info.get("emotion"),
            "language": info.get("language"),
            "usage": usage,
        }
    finally:
        logger.info(
            "dashscope operation=asr model=%s elapsed_seconds=%.4f characters=%d "
            "status=%s usage=%s",
            ASR_MODEL,
            time.monotonic() - start,
            chars,
            status,
            _log_usage(usage),
        )


def _voice_parameters(instruction: str | None = None) -> dict[str, Any]:
    """Build provider `parameters` from voice_config.json, with a per-call override.

    Config is optional: a missing or malformed file yields no parameters (the
    provider then uses defaults) rather than failing a turn.
    """
    parameters: dict[str, Any] = {}
    parsed: Any = None
    try:
        parsed = json.loads(VOICE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        parsed = None
    if isinstance(parsed, dict):
        config = cast(dict[str, Any], parsed)
        for key in VOICE_PARAMETER_KEYS:
            value = config.get(key)
            if key == "instruction":
                if isinstance(value, str) and value.strip() and len(value) <= MAX_INSTRUCTION_CHARS:
                    parameters["instruction"] = value.strip()
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                number = float(value)
                if math.isfinite(number) and 0.5 <= number <= 2.0:
                    parameters[key] = number
    if instruction is not None:
        text = instruction.strip()
        if text and len(text) <= MAX_INSTRUCTION_CHARS:
            parameters["instruction"] = text
    return parameters


def tts_synthesize(
    text: str, *, timeout: float = 60.0, instruction: str | None = None
) -> dict[str, Any]:
    """Synthesize using the selected custom voice; return downloaded MP3 and usage."""
    from urllib.parse import urlsplit, urlunsplit

    start = time.monotonic()
    usage: dict[str, Any] = {}
    status = "error"
    try:
        _validate_timeout(timeout)
        text = _text(text)
        parameters = _voice_parameters(instruction)
        body: dict[str, Any] = {
            "model": TTS_MODEL,
            "input": {"text": text, "voice": VOICE_ID, "format": "mp3", "sample_rate": 24000},
        }
        if parameters:
            body["parameters"] = parameters
        result = _post(TTS_ENDPOINT, body, timeout)
        try:
            url = result["output"]["audio"]["url"]
            if not isinstance(url, str):
                raise ValueError("audio URL must be a string")
            parsed = urlsplit(url)
            if (
                parsed.scheme not in ("http", "https")
                or parsed.hostname != "dashscope-result-bj.oss-cn-beijing.aliyuncs.com"
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 80, 443)
            ):
                raise ValueError("untrusted audio URL")
            url = urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, ""))
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            raise DashScopeError(
                "missing or untrusted TTS audio URL", code="invalid_response"
            ) from None
        usage = _usage(result)
        # Separate GET has no Authorization header. Never log signed URLs.
        audio_bytes = _fetch(urllib.request.Request(url, method="GET"), timeout, 32 * 1024 * 1024)
        if not (
            audio_bytes.startswith(b"ID3")
            or (len(audio_bytes) >= 2 and audio_bytes[0] == 0xFF and audio_bytes[1] & 0xE0 == 0xE0)
        ):
            raise DashScopeError("empty or non-MP3 TTS audio", code="invalid_response")
        status = "ok"
        return {"audio_bytes": audio_bytes, "format": "mp3", "usage": usage}
    finally:
        logger.info(
            "dashscope operation=tts model=%s elapsed_seconds=%.4f characters=%d "
            "status=%s usage=%s",
            TTS_MODEL,
            time.monotonic() - start,
            _text_length(text),
            status,
            _log_usage(usage),
        )


def tts_stream_pcm(
    text_fragments: AsyncIterable[str], *, endpoint: str | None = None
) -> AsyncIterator[bytes]:
    """Stream raw PCM16 24 kHz audio for incremental text fragments.

    Delegates to voice_stream_tts (Bailian duplex WebSocket). Kept separate from
    tts_synthesize so callers that can play PCM get first-audio in about one
    second, while every existing device keeps the MP3 path. The caller owns
    cancellation: closing the returned iterator closes the vendor connection.
    """
    from voice_stream_tts import StreamingTTS

    options: dict[str, Any] = {}
    if endpoint is not None:
        options["endpoint"] = endpoint
    return StreamingTTS(**options).stream(text_fragments)


def self_evaluate() -> dict[str, Any]:
    """Offline declaration only; no request, credential read or playback here."""
    return {
        "module": "voice_dashscope",
        "domain": "Bailian ASR (qwen3-asr-flash) and MP3 TTS (cosyvoice-v3.5-flash) REST adapter",
        "current_version": "1.0.0",
        "known_limitations": [
            "ASR accepts raw mono PCM16 16 kHz only; no streaming, partial results or diarization.",
            "TTS returns MP3 bytes; synthesis success is not proof the audio was heard.",
            "The voice id is a fixed enrollment; no per-utterance voice selection or cloning.",
            "Credentials come only from the owner-only key file; no refresh or rotation logic.",
            "No resident caller, capability, mind provenance or KG projection is provided.",
        ],
        "improvement_directions": [
            "Streaming ASR/TTS with interruption once the vendor protocol is verified.",
            "Attach ASR/TTS observations to the voice ledger and mind provenance.",
            "Measure latency and error rates under real network conditions.",
        ],
        "last_evaluated_at": None,
    }
