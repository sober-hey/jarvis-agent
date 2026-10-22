"""火山 seed-tts-2.0 双向流式 TTS — JARVIS 网关适配层。

协议：v3 二进制帧（4 字节头 + event i32 + session_id 长度前缀 + gzip payload）。
鉴权：X-Api-Key (UUID 格式 API Key)，无需 HMAC 签名。
音色：默认 zh_female_shuangkuaisisi_uranus_bigtts（爽快思思 2.0，seed-tts-2.0 版本）。

延迟实测（2026-10-21）：首包 700-850ms，总耗时 1150-1500ms，音频 ~30KB/句。

公共接口与 BailianSpeech / DashScopeSpeech 等价：
  synthesize(text) -> AsyncIterator[bytes]  # MP3 分块
  不提供 create_stream()：网关 getattr 拿不到就直接走 MP3 分块路径。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import struct
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
_RESOURCE = "seed-tts-2.0"
DEFAULT_VOICE = "zh_female_shuangkuaisisi_uranus_bigtts"
DEFAULT_KEY_ENV = "VOLCENGINE_API_KEY"
DEFAULT_KEY_PATH = Path.home() / ".jarvis" / "volcengine.key"

# 协议常量
_MT_CLIENT_FULL = 0x01
_MT_SERVER_FULL = 0x09
_MT_SERVER_AUDIO = 0x0B
_MT_SERVER_ERROR = 0x0F
_FL_WITH_EVENT = 0x04
_COMP_NONE = 0x00
_COMP_GZIP = 0x01


# ---------------------------------------------------------------------------
# 帧构建 / 解析
# ---------------------------------------------------------------------------


def _build(msg_type: int, event: int, payload: bytes, session: str | None = None) -> bytes:
    header = bytes([0x11, (msg_type << 4) | _FL_WITH_EVENT, 0x11, 0x00])
    body = struct.pack(">i", event)
    if session:
        s = session.encode()
        body += struct.pack(">I", len(s)) + s
    packed = gzip.compress(payload)
    body += struct.pack(">I", len(packed)) + packed
    return header + body


def _parse(msg: bytes | str) -> tuple[int, int | None, bytes]:
    """返回 (msg_type, event, payload)"""
    if isinstance(msg, str):
        msg = msg.encode()
    if len(msg) < 4:
        return -1, None, b""
    b1 = msg[1]
    mt, fl = b1 >> 4, b1 & 0x0F
    comp = msg[2] & 0x0F
    pos = 4
    ev: int | None = None
    if fl & _FL_WITH_EVENT:
        ev = struct.unpack(">i", msg[pos : pos + 4])[0]
        pos += 4
    # 跳过服务端 session_id（4 字节长度前缀 + 内容，最多 64 字节）
    if mt in (_MT_SERVER_FULL, _MT_SERVER_AUDIO, _MT_SERVER_ERROR) and len(msg) >= pos + 4:
        sid_len = struct.unpack(">I", msg[pos : pos + 4])[0]
        if 0 < sid_len <= 64 and len(msg) >= pos + 4 + sid_len + 4:
            pos += 4 + sid_len
    if len(msg) < pos + 4:
        return mt, ev, b""
    size = struct.unpack(">I", msg[pos : pos + 4])[0]
    data = msg[pos + 4 : pos + 4 + size]
    if comp == _COMP_GZIP and data:
        try:
            data = gzip.decompress(data)
        except Exception:
            pass
    return mt, ev, data


# ---------------------------------------------------------------------------
# 凭据读取
# ---------------------------------------------------------------------------


def _read_key() -> str:
    """优先从环境变量，次从 key 文件读取 API Key。"""
    key = os.environ.get(DEFAULT_KEY_ENV, "").strip()
    if key:
        return key
    if DEFAULT_KEY_PATH.exists():
        key = DEFAULT_KEY_PATH.read_text("ascii").strip()
        if key:
            return key
    raise RuntimeError(
        f"Volcengine API key not found. "
        f"Set {DEFAULT_KEY_ENV} env var or write to {DEFAULT_KEY_PATH}"
    )


def write_key(api_key: str) -> None:
    """将 API Key 写入 key 文件（仅 owner 可读）。"""
    DEFAULT_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_KEY_PATH.write_text(api_key.strip() + "\n", encoding="ascii")
    DEFAULT_KEY_PATH.chmod(0o600)


# ---------------------------------------------------------------------------
# 核心流式适配器
# ---------------------------------------------------------------------------


class VolcengineTTS:
    """火山 seed-tts-2.0 TTS 适配器，接口与 BailianSpeech 兼容。

    用法：
        tts = VolcengineTTS()
        async for chunk in tts.synthesize("Sir，网关已连接。"):
            # chunk 是 MP3 字节
            ...
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        voice: str = DEFAULT_VOICE,
        resource: str = _RESOURCE,
        sample_rate: int = 24000,
        speech_rate: int = 0,
        audio_format: str = "mp3",
    ) -> None:
        self._api_key = api_key  # None 则运行时读取
        self.voice = voice
        self.resource = resource
        self.sample_rate = sample_rate
        self.speech_rate = speech_rate
        self.audio_format = audio_format

    def _key(self) -> str:
        return self._api_key or _read_key()

    async def _stream_raw(self, text: str) -> AsyncIterator[bytes]:
        """建连、握手、合成，逐块 yield MP3 音频字节。"""
        import websockets  # lazy import，避免启动时依赖

        key = self._key()
        headers = {
            "X-Api-Key": key,
            "X-Api-Resource-Id": self.resource,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

        try:
            # proxy=None: Volcengine is a domestic endpoint; websockets>=15 would
            # otherwise pick up the system SOCKS proxy under launchd and fail
            # with "connecting through a SOCKS proxy requires python-socks".
            async with websockets.connect(
                _URL, additional_headers=headers, max_size=16 * 1024 * 1024, proxy=None
            ) as ws:
                # StartConnection (event=1)
                await ws.send(_build(_MT_CLIENT_FULL, 1, b"{}"))
                mt, ev, _ = _parse(await asyncio.wait_for(ws.recv(), 10))
                if ev != 50:
                    raise RuntimeError(f"StartConnection failed ev={ev}")

                sid = str(uuid.uuid4())
                cfg = {
                    "event": 100,
                    "namespace": "BidirectionalTTS",
                    "req_params": {
                        "speaker": self.voice,
                        "audio_params": {
                            "format": self.audio_format,
                            "sample_rate": self.sample_rate,
                        },
                        "speech_rate": self.speech_rate,
                    },
                }
                await ws.send(_build(_MT_CLIENT_FULL, 100, json.dumps(cfg).encode(), session=sid))
                mt, ev, data = _parse(await asyncio.wait_for(ws.recv(), 10))
                if ev != 150:
                    raise RuntimeError(f"StartSession failed ev={ev} data={data[:200]}")

                task = {
                    "event": 200,
                    "namespace": "BidirectionalTTS",
                    "req_params": {"text": text},
                }
                await ws.send(_build(_MT_CLIENT_FULL, 200, json.dumps(task).encode(), session=sid))
                await ws.send(_build(_MT_CLIENT_FULL, 102, b"{}", session=sid))

                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 30)
                    except TimeoutError:
                        LOG.warning("volcengine_tts: recv timeout")
                        return
                    mt, ev, data = _parse(raw)
                    if mt == _MT_SERVER_AUDIO and data:
                        yield data
                    elif mt == _MT_SERVER_ERROR:
                        raise RuntimeError(f"Volcengine TTS error: {data[:300]}")
                    elif mt == _MT_SERVER_FULL and ev == 152:  # SessionFinished
                        return

        except Exception as exc:
            LOG.error("volcengine_tts_error type=%s msg=%s", type(exc).__name__, str(exc)[:200])
            raise

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """分块 yield MP3 字节（与 BailianSpeech.synthesize 接口一致）。

        文本超过 500 字按句切分后串行合成，保证顺序。
        """
        MAX = 500
        if len(text) <= MAX:
            async for chunk in self._stream_raw(text):
                yield chunk
            return

        # 简单切分：按句号/问号/叹号
        import re

        parts = re.split(r"(?<=[。！？!?.])", text)
        buf = ""
        for part in parts:
            buf += part
            if len(buf) >= MAX:
                async for chunk in self._stream_raw(buf):
                    yield chunk
                buf = ""
        if buf:
            async for chunk in self._stream_raw(buf):
                yield chunk


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------


async def _self_test() -> dict[str, Any]:
    import time

    text = "Sir，网关连接正常，火山语音已经接通。"
    t0 = time.perf_counter()
    ttfb: float | None = None
    audio = bytearray()
    tts = VolcengineTTS()
    async for chunk in tts.synthesize(text):
        if ttfb is None:
            ttfb = (time.perf_counter() - t0) * 1000
        audio.extend(chunk)
    total = (time.perf_counter() - t0) * 1000
    return {
        "ok": bool(audio),
        "ttfb_ms": round(ttfb or 0),
        "total_ms": round(total),
        "audio_bytes": len(audio),
    }


if __name__ == "__main__":
    import asyncio

    result = asyncio.run(_self_test())
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["ok"] else 1)


def self_evaluate() -> dict[str, Any]:
    """Offline declaration only; never opens the socket, reads the key or plays audio."""
    return {
        "module": "voice_volcengine",
        "domain": "Volcengine seed-tts-2.0 bidirectional streaming MP3 TTS adapter",
        "current_version": "1.0.0",
        "known_limitations": [
            "Latency figures (first chunk 700-850 ms) come from one session on 2026-10-21, "
            "not a sustained measurement.",
            "MP3 chunks only; no duplex PCM path, so interruption granularity is the chunk.",
            "The voice id is a fixed enrollment; no per-utterance voice choice or cloning.",
            "The API key comes from the owner-only key file or environment; no rotation logic.",
            "No resident caller, mind provenance or KG projection is provided.",
        ],
        "improvement_directions": [
            "Measure latency and error rates across sessions before claiming targets.",
            "Attach synthesis/playback observations to the voice ledger.",
        ],
        "last_evaluated_at": None,
    }
