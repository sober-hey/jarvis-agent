"""K1-003 DRAFT: OpenAI-compatible Chat Completions transport.

NOT_APPROVED / NOT_FROZEN; not wired to packaging or production. The caller
supplies ProviderConfig, or explicitly named environment variables via from_env.
One HTTP request per invocation: no retry, fallback, tools or task-state writes.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import math
import os
import time
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import Any, cast
from urllib.parse import urlsplit

from jarvis.model_provider import (
    AuthError,
    ContentFilterError,
    ModelChunk,
    ModelError,
    ModelProvider,
    ModelResponse,
    ModelUsage,
    ProviderConfig,
    RateLimitError,
    TimeoutError,
)

_MAX_EVENT_BYTES = 1_048_576


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None  # Never forward a bearer token to a redirected destination.


def _urlopen(request: urllib.request.Request, *, timeout: float) -> Any:
    # Do not implicitly trust host proxy environment variables or follow redirects.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(request, timeout=timeout)


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelError("Invalid Chat Completions response")
    return cast(dict[str, Any], value)


def _usage(payload: dict[str, Any]) -> ModelUsage:
    raw = payload.get("usage")
    usage = {} if raw is None else _object(raw)
    values: list[int | None] = []
    for name in ("prompt_tokens", "completion_tokens"):
        value = usage.get(name)
        if value is not None and (type(value) is not int or value < 0):
            raise ModelError("Invalid provider token usage")
        values.append(value)
    return ModelUsage(tokens_in=values[0], tokens_out=values[1])


def _choice(payload: dict[str, Any], *, streaming: bool = False) -> dict[str, Any] | None:
    if "error" in payload:
        raise ModelError("Provider returned an error envelope")
    choices: Any = payload.get("choices")
    if streaming and choices == []:
        return None
    if not isinstance(choices, list):
        raise ModelError("Exactly one Chat Completions choice is required")
    entries = cast(list[object], choices)
    if len(entries) != 1:
        raise ModelError("Exactly one Chat Completions choice is required")
    choice = _object(entries[0])
    if choice.get("index", 0) != 0:
        raise ModelError("Unexpected choice index")
    if choice.get("finish_reason") == "content_filter":
        raise ContentFilterError("Provider filtered the content")
    return choice


def _text(message: dict[str, Any], *, streaming: bool = False) -> str:
    if message.get("tool_calls") or message.get("function_call"):
        raise ModelError("Tool calls are unsupported by this text adapter")
    content = message.get("content")
    if streaming and content is None:
        return ""
    if not isinstance(content, str):
        raise ModelError("Provider returned non-text content")
    return content


def _finish(choice: dict[str, Any]) -> str | None:
    reason = choice.get("finish_reason")
    if reason is not None and (not isinstance(reason, str) or not reason):
        raise ModelError("Invalid finish reason")
    return reason


def _normalize(error: Exception) -> ModelError:
    if isinstance(error, urllib.error.HTTPError):
        code = error.code
        error.close()
        if code == 429:
            return RateLimitError("Provider rate limit (HTTP 429)")
        if code == 401:
            return AuthError("Provider authentication failed (HTTP 401)")
        return ModelError(f"Provider HTTP failure ({code})")
    if isinstance(error, builtins.TimeoutError) or (
        isinstance(error, urllib.error.URLError) and isinstance(error.reason, builtins.TimeoutError)
    ):
        return TimeoutError("Provider request timed out; remote outcome may be unknown")
    return ModelError("Provider transport or response failure")


class OpenAIAdapter(ModelProvider):
    """Draft text-only adapter; urllib work runs off-loop in asynchronous APIs.

    A timeout/cancellation never proves the remote request did not happen.
    Blocking urllib operations remain bounded by the socket timeout; this draft
    does not claim hard-deadline or instantaneous worker-thread cancellation.
    """

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @classmethod
    def from_env(
        cls,
        *,
        base_url_env: str,
        api_key_env: str,
        model_id: str,
        max_tokens: int,
        temperature: float,
    ) -> OpenAIAdapter:
        """Resolve the endpoint from an explicit env name; keep only a key reference."""
        base_url = os.environ.get(base_url_env)
        if not base_url:
            raise ValueError("Missing provider endpoint environment variable")
        return cls(ProviderConfig(base_url, api_key_env, model_id, max_tokens, temperature))

    def _request(
        self, messages: Sequence[Mapping[str, object]], streaming: bool, options: dict[str, object]
    ) -> tuple[urllib.request.Request, float]:
        unsupported = options.keys() - {
            "model",
            "max_output_tokens",
            "temperature",
            "timeout_seconds",
        }
        if unsupported:
            raise ValueError("Unsupported adapter options")
        parts = urlsplit(self.config.base_url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Provider base_url must be an HTTPS endpoint without credentials")
        key = os.environ.get(self.config.api_key_env)
        if not key or any(c in key for c in "\r\n"):
            raise AuthError("Missing or invalid API key environment variable")
        timeout = options.get("timeout_seconds", 60.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout_seconds must be a positive finite number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        model = options.get("model", self.config.model_id)
        tokens = options.get("max_output_tokens", self.config.max_tokens)
        temperature = options.get("temperature", self.config.temperature)
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a nonempty string")
        if type(tokens) is not int or tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ValueError("temperature must be finite and between 0 and 2")
        if not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError("temperature must be finite and between 0 and 2")
        body = {
            "model": model,
            "messages": list(messages),
            "max_tokens": options.get("max_output_tokens", self.config.max_tokens),
            "temperature": options.get("temperature", self.config.temperature),
            "stream": streaming,
        }
        if streaming:
            body["stream_options"] = {"include_usage": True}
        endpoint = self.config.base_url.rstrip("/") + "/chat/completions"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if streaming else "application/json",
            },
            method="POST",
        )
        return request, float(timeout)

    def chat(self, messages: Sequence[Mapping[str, object]], **kwargs: object) -> ModelResponse:
        request, timeout = self._request(messages, False, kwargs)
        started = time.monotonic()
        try:
            with _urlopen(request, timeout=timeout) as response:
                raw = response.read(_MAX_EVENT_BYTES + 1)
            if len(raw) > _MAX_EVENT_BYTES:
                raise ModelError("Provider response exceeds draft byte limit")
            payload = _object(json.loads(raw))
            choice = _choice(payload)
            assert choice is not None
            model = payload.get("model", self.config.model_id)
            if not isinstance(model, str) or not model:
                raise ModelError("Invalid provider model ID")
            reason = _finish(choice)
            if reason is None:
                raise ModelError("Missing finish reason")
            return ModelResponse(
                content=_text(_object(choice.get("message"))),
                usage=_usage(payload),
                latency_ms=(time.monotonic() - started) * 1000,
                model_id=model,
                finish_reason=reason,
            )
        except ModelError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise _normalize(exc) from None

    async def stream_chat(
        self, messages: Sequence[Mapping[str, object]], **kwargs: object
    ) -> AsyncGenerator[ModelChunk, None]:
        request, timeout = self._request(messages, True, kwargs)
        started = time.monotonic()
        response = None
        opening = asyncio.create_task(asyncio.to_thread(_urlopen, request, timeout=timeout))
        try:
            try:
                response = await asyncio.shield(opening)
            except asyncio.CancelledError:
                # A cancelled open must not leave an eventual socket unowned.
                opening.add_done_callback(self._close_opened)
                raise
            data: list[str] = []
            size = 0
            model = self.config.model_id
            finish_reason: str | None = None
            usage: ModelUsage | None = None
            while True:
                line = await asyncio.to_thread(response.readline, _MAX_EVENT_BYTES + 1)
                if not line:
                    raise ModelError("Incomplete SSE stream: missing [DONE]")
                size += len(line)
                if size > _MAX_EVENT_BYTES:
                    raise ModelError("SSE event exceeds draft byte limit")
                text = line.decode("utf-8").rstrip("\r\n")
                if text:
                    if text.startswith("data:"):
                        value = text[5:]
                        data.append(value[1:] if value.startswith(" ") else value)
                    continue
                event = "\n".join(data)
                data, size = [], 0
                if not event:
                    continue
                if event == "[DONE]":
                    if finish_reason is None:
                        raise ModelError("Incomplete SSE stream: missing finish reason")
                    yield ModelChunk(
                        content="",
                        model_id=model,
                        usage=usage,
                        latency_ms=(time.monotonic() - started) * 1000,
                        finish_reason=finish_reason,
                    )
                    return
                payload = _object(json.loads(event))
                model = payload.get("model", model)
                if not isinstance(model, str) or not model:
                    raise ModelError("Invalid provider model ID")
                if payload.get("usage") is not None:
                    usage = _usage(payload)
                choice = _choice(payload, streaming=True)
                if choice is None:
                    continue
                if finish_reason is not None:
                    raise ModelError("Unexpected SSE choice after finish")
                content = _text(_object(choice.get("delta", {})), streaming=True)
                finish_reason = _finish(choice)
                if content:
                    yield ModelChunk(content=content, model_id=model)
        except ModelError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise _normalize(exc) from None
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _close_opened(task: asyncio.Task[Any]) -> None:
        if not task.cancelled() and task.exception() is None:
            task.result().close()
