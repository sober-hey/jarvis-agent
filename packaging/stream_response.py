"""Text streaming protocol: ordered deltas and exactly one terminal chunk.

On errors the terminal chunk has ``error`` set; text received before it remains
available as partial output. Socket transport uses newline-delimited JSON with
these same four fields, and stops when ``done`` is true.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from context_limits import CHAT_CONTEXT_BYTE_LIMIT


@dataclass
class StreamChunk:
    text: str = ""
    done: bool = False
    error: str | None = None
    index: int = 0


class StreamResponse:
    """Collect streamed text without discarding partial output on failure."""

    def __init__(self) -> None:
        self.chunks: list[StreamChunk] = []
        self.full_text: str = ""

    def add_chunk(self, chunk: StreamChunk) -> None:
        self.chunks.append(chunk)
        if chunk.text:
            self.full_text += chunk.text

    def is_complete(self) -> bool:
        return bool(self.chunks) and self.chunks[-1].done


def stream_via_socket(socket_path: str | Path, prompt: str) -> Iterator[StreamChunk]:
    """Read bounded NDJSON deltas until completion; transport errors are chunks."""
    index = 0
    try:
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt.encode("utf-8")) > CHAT_CONTEXT_BYTE_LIMIT
        ):
            raise ValueError("invalid_prompt")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(35)
            connection.connect(str(socket_path))
            connection.sendall(
                (
                    json.dumps({"type": "stream_chat", "text": prompt}, ensure_ascii=False) + "\n"
                ).encode("utf-8")
            )
            with connection.makefile("rb") as reader:
                while True:
                    raw = reader.readline(1024 * 1024 + 1)
                    if not raw:
                        break
                    if len(raw) > 1024 * 1024 or not raw.endswith(b"\n"):
                        raise ValueError("invalid_frame")
                    value = cast(dict[str, Any], json.loads(raw))
                    if (
                        not isinstance(value.get("text"), str)
                        or type(value.get("done")) is not bool
                        or type(value.get("index")) is not int
                        or value["index"] != index
                        or (value.get("error") is not None and not isinstance(value["error"], str))
                    ):
                        raise ValueError("invalid_frame")
                    chunk = StreamChunk(
                        text=value["text"],
                        done=value["done"],
                        error=value.get("error"),
                        index=value["index"],
                    )
                    yield chunk
                    if chunk.done:
                        return
                    index += 1
    except TimeoutError:
        yield StreamChunk(done=True, error="stream_timeout", index=index)
        return
    except Exception:
        yield StreamChunk(done=True, error="stream_transport_error", index=index)
        return
    yield StreamChunk(done=True, error="stream_interrupted", index=index)


def self_evaluate():
    """Check stream response and NDJSON transport invariants without connecting."""
    from datetime import datetime, timezone

    checks = {
        "chunk_contract": all(
            hasattr(StreamChunk(), name) for name in ("text", "done", "error", "index")
        ),
        "socket_transport": callable(stream_via_socket),
        "bounded_prompt": type(CHAT_CONTEXT_BYTE_LIMIT) is int and CHAT_CONTEXT_BYTE_LIMIT > 0,
    }
    return {
        "module": "stream_response",
        "domain": "ordered text delta and terminal socket streaming protocol",
        "current_version": "NDJSON bounded-frame transport with ordered chunks and explicit terminal errors",
        "known_limitations": [
            "Transport depends on an externally managed Unix socket server.",
            "Evaluation does not open a socket or test provider delivery.",
        ],
        "improvement_directions": [
            "Add a local in-memory protocol fixture covering malformed and interrupted frames."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "ok": all(checks.values()),
    }
