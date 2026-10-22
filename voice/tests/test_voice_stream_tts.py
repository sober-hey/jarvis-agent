"""Offline tests for the duplex TTS adapter; no vendor network calls."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from voice_stream_tts import (
    AuthenticationError,
    NetworkError,
    QuotaError,
    SentenceBuffer,
    StreamingTTS,
)
from websockets.asyncio.server import Server, ServerConnection, serve


class FakeDashScope:
    def __init__(self, mode: str = "normal") -> None:
        self.mode = mode
        self.received: list[dict[str, Any]] = []
        self.closed = asyncio.Event()
        self.server: Server | None = None

    async def handler(self, connection: ServerConnection) -> None:
        try:
            if self.mode == "network":
                await connection.close(code=1011, reason="fixture disconnect")
                return
            async for raw in connection:
                message = json.loads(raw)
                self.received.append(message)
                action = message["header"]["action"]
                task_id = message["header"]["task_id"]
                if action == "run-task":
                    await connection.send(
                        json.dumps({"header": {"event": "task-started", "task_id": task_id}})
                    )
                elif action == "continue-task":
                    if self.mode == "auth":
                        await self._fail(connection, task_id, "InvalidApiKey")
                        return
                    if self.mode == "quota":
                        await self._fail(connection, task_id, "QuotaExhausted")
                        return
                    text = message["payload"]["input"]["text"]
                    await connection.send(
                        json.dumps(
                            {
                                "header": {"event": "result-generated", "task_id": task_id},
                                "payload": {"output": {"type": "sentence-synthesis"}},
                            }
                        )
                    )
                    await connection.send(("pcm:" + text).encode())
                elif action == "finish-task":
                    await connection.send(
                        json.dumps({"header": {"event": "task-finished", "task_id": task_id}})
                    )
                    return
        finally:
            self.closed.set()

    async def _fail(self, connection: ServerConnection, task_id: str, code: str) -> None:
        await connection.send(
            json.dumps(
                {
                    "header": {
                        "event": "task-failed",
                        "task_id": task_id,
                        "error_code": code,
                        "error_message": code,
                    },
                    "payload": {},
                }
            )
        )

    async def start(self) -> str:
        self.server = await serve(self.handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


class StreamingTTSTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.key_dir = tempfile.TemporaryDirectory()
        self.key_path = Path(self.key_dir.name) / "dashscope.key"
        self.key_path.write_text("fixture-key\n", encoding="ascii")
        os.chmod(self.key_path, 0o600)

    def tearDown(self) -> None:
        self.key_dir.cleanup()

    def test_sentence_buffer_emits_strong_marks_and_comma_threshold(self) -> None:
        buffer = SentenceBuffer(comma_threshold=5)
        self.assertEqual(buffer.feed("第一句。第二"), ["第一句。"])
        self.assertEqual(buffer.feed("句？好！"), ["第二句？", "好！"])
        self.assertEqual(buffer.feed("甲乙，丙丁，"), ["甲乙，丙丁，"])
        self.assertEqual(buffer.finish(), [])

    async def test_stream_sends_segments_and_yields_pcm_early(self) -> None:
        fixture = FakeDashScope()
        endpoint = await fixture.start()
        try:

            async def fragments() -> AsyncIterator[str]:
                yield "你好，"
                yield "世界。第二句"

            tts = StreamingTTS(endpoint=endpoint, key_path=self.key_path, comma_threshold=20)
            chunks = [chunk async for chunk in tts.stream(fragments())]
            self.assertEqual(
                chunks,
                ["pcm:你好，世界。".encode(), "pcm:第二句".encode()],
            )
            run_message = fixture.received[0]
            self.assertEqual(run_message["header"]["streaming"], "duplex")
            self.assertEqual(run_message["payload"]["task"], "tts")
            self.assertEqual(run_message["payload"]["function"], "SpeechSynthesizer")
            self.assertEqual(run_message["payload"]["model"], "cosyvoice-v3.5-flash")
            self.assertEqual(
                run_message["payload"]["parameters"]["voice"],
                "cosyvoice-v3.5-flash-jarvis-f7bec1631ddf4725841995ba9c711376",
            )
            self.assertEqual(run_message["payload"]["parameters"]["format"], "pcm")
            self.assertEqual(
                [m["header"]["action"] for m in fixture.received],
                ["run-task", "continue-task", "continue-task", "finish-task"],
            )
        finally:
            await fixture.stop()

    async def test_cancel_stops_pending_input_and_closes_connection(self) -> None:
        fixture = FakeDashScope()
        endpoint = await fixture.start()
        input_started = asyncio.Event()
        release_input = asyncio.Event()

        async def fragments() -> AsyncIterator[str]:
            yield "第一句。"
            input_started.set()
            await release_input.wait()
            yield "不应发送。"

        tts = StreamingTTS(endpoint=endpoint, key_path=self.key_path)
        task = asyncio.create_task(self._collect(tts, fragments()))
        await asyncio.wait_for(input_started.wait(), timeout=2)
        tts.cancel()
        await asyncio.wait_for(task, timeout=2)
        self.assertEqual(
            [m["header"]["action"] for m in fixture.received], ["run-task", "continue-task"]
        )
        self.assertTrue(fixture.closed.is_set())
        release_input.set()
        await fixture.stop()

    async def test_authentication_failure_is_classified(self) -> None:
        await self._assert_error("auth", AuthenticationError)

    async def test_quota_failure_is_classified(self) -> None:
        await self._assert_error("quota", QuotaError)

    async def test_network_failure_is_classified(self) -> None:
        await self._assert_error("network", NetworkError)

    async def _assert_error(self, mode: str, expected: type[Exception]) -> None:
        fixture = FakeDashScope(mode)
        endpoint = await fixture.start()
        try:

            async def fragments() -> AsyncIterator[str]:
                yield "测试。"

            tts = StreamingTTS(endpoint=endpoint, key_path=self.key_path)
            with self.assertRaises(expected) as raised:
                _ = [chunk async for chunk in tts.stream(fragments())]
            self.assertIn(mode, str(raised.exception))
        finally:
            await fixture.stop()

    @staticmethod
    async def _collect(tts: StreamingTTS, fragments: AsyncIterator[str]) -> list[bytes]:
        return [chunk async for chunk in tts.stream(fragments)]


if __name__ == "__main__":
    unittest.main()
