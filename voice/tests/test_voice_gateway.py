"""Gateway acceptance uses real loopback WebSockets; providers are injected."""

from __future__ import annotations

import asyncio
import base64
import importlib
import importlib.util
import json
import unittest

from websockets.asyncio.client import connect


def audio(data: bytes = b"\x00\x01" * 160) -> str:
    return json.dumps(
        {
            "type": "audio",
            "data": base64.b64encode(data).decode(),
            "format": "pcm_s16le",
            "sample_rate": 16000,
        }
    )


class Speech:
    async def transcribe(self, pcm: bytes) -> str:
        return "heard-" + str(len(pcm))

    async def synthesize(self, text: str):
        yield b"ID3" + text.encode()


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_websocket_pcm_to_brain_text_and_mp3(self):
        self.assertIsNotNone(importlib.util.find_spec("voice_gateway"), "voice gateway is missing")
        gateway = importlib.import_module("voice_gateway")
        received = []

        async def reply(text):
            received.append(text)
            return "brain-" + text

        app = gateway.VoiceGateway(Speech(), reply, utterance_gap=0.02)
        async with app.serve(port=0) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}", proxy=None) as ws:
                await ws.send(audio())
                user = json.loads(await asyncio.wait_for(ws.recv(), 2))
                answer = json.loads(await asyncio.wait_for(ws.recv(), 2))
                chunk = json.loads(await asyncio.wait_for(ws.recv(), 2))
                end = json.loads(await asyncio.wait_for(ws.recv(), 2))
                self.assertEqual(user["role"], "user")
                self.assertEqual(answer["text"], "brain-heard-320")
                self.assertEqual(chunk["format"], "mp3")
                self.assertEqual(base64.b64decode(chunk["data"]), b"ID3brain-heard-320")
                self.assertTrue(end["final"])
                self.assertEqual(received, ["heard-320"])
        await app.close()

    async def test_chunked_audio_is_one_utterance(self):
        import voice_gateway as vg

        got = []

        async def reply(text):
            got.append(text)
            return "ok"

        app = vg.VoiceGateway(Speech(), reply, utterance_gap=0.02)
        async with app.serve(port=0) as server:
            async with connect(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None
            ) as ws:
                await ws.send(audio())
                await ws.send(audio())
                await ws.send(json.dumps({"type": "audio_end"}))
                messages = [json.loads(await asyncio.wait_for(ws.recv(), 2)) for _ in range(4)]
                self.assertEqual(messages[0]["text"], "heard-640")
                self.assertEqual(got, ["heard-640"])
        await app.close()

    async def test_interrupt_after_completed_synthesis_flushes_client_buffer(self):
        import voice_gateway as vg

        async def reply(text):
            return text

        app = vg.VoiceGateway(Speech(), reply, utterance_gap=0.01)
        async with app.serve(port=0) as server:
            async with connect(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None
            ) as ws:
                await ws.send(audio())
                for _ in range(4):
                    await asyncio.wait_for(ws.recv(), 2)
                await ws.send(audio())
                self.assertEqual(
                    json.loads(await asyncio.wait_for(ws.recv(), 2)), {"type": "interrupt"}
                )
        await app.close()

    async def test_interrupt_stops_old_audio_but_preserves_other_client(self):
        import voice_gateway as vg

        release = asyncio.Event()
        stopped = asyncio.Event()

        class SlowSpeech(Speech):
            async def synthesize(self, text):
                yield b"first"
                try:
                    await release.wait()
                    yield b"stale"
                finally:
                    stopped.set()

        async def reply(text):
            return text

        app = vg.VoiceGateway(SlowSpeech(), reply, utterance_gap=0.01)
        async with app.serve(port=0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with connect(url, proxy=None) as one, connect(url, proxy=None) as two:
                for ws in (one, two):
                    await ws.send(audio())
                    for _ in range(3):
                        await asyncio.wait_for(ws.recv(), 2)
                await one.send(audio())
                self.assertEqual(
                    json.loads(await asyncio.wait_for(one.recv(), 2))["type"], "interrupt"
                )
                await asyncio.wait_for(stopped.wait(), 2)
                release.set()
                # The second client's output remains intact, not globally cancelled.
                second = json.loads(await asyncio.wait_for(two.recv(), 2))
                self.assertEqual(base64.b64decode(second["data"]), b"stale")
        await app.close()

    async def test_barge_in_does_not_cancel_or_repeat_submitted_brain(self):
        import voice_gateway as vg

        calls = []
        started, release = asyncio.Event(), asyncio.Event()

        async def reply(text):
            calls.append(text)
            started.set()
            if len(calls) == 1:
                await release.wait()
            return text

        app = vg.VoiceGateway(Speech(), reply, utterance_gap=0.01)
        async with app.serve(port=0) as server:
            async with connect(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None
            ) as ws:
                await ws.send(audio())
                await asyncio.wait_for(started.wait(), 2)
                await ws.recv()  # first user transcript
                await ws.send(audio(b"\x01\x00" * 320))
                await asyncio.sleep(0.03)
                release.set()
                messages = [json.loads(await asyncio.wait_for(ws.recv(), 2)) for _ in range(4)]
                self.assertTrue(all(message.get("generation") == 2 for message in messages))
                self.assertEqual(calls, ["heard-320", "heard-640"])
        await app.close()

    async def test_disconnect_preserves_submitted_brain_and_cleanup(self):
        import voice_gateway as vg

        started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def reply(text):
            started.set()
            await release.wait()
            completed.set()
            return text

        app = vg.VoiceGateway(Speech(), reply, utterance_gap=0.01)
        async with app.serve(port=0) as server:
            async with connect(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None
            ) as ws:
                await ws.send(audio())
                await asyncio.wait_for(started.wait(), 2)
            release.set()
            await asyncio.wait_for(completed.wait(), 2)
        await app.close()
        self.assertEqual(app.brain_calls, set())
        self.assertEqual(app.clients, set())

    async def test_invalid_input_safe_error_no_provider_or_brain_calls(self):
        import voice_gateway as vg

        calls = []

        async def reply(text):
            calls.append(text)
            return text

        app = vg.VoiceGateway(Speech(), reply, utterance_gap=0.01)
        invalid = [
            "[1]",
            "{",
            b"binary",
            json.dumps({"type": "audio", "format": "wav"}),
            audio().replace("16000", "true"),
            audio().replace("16000", "16000.0"),
            audio().replace("pcm_s16le", "mp3"),
            json.dumps(
                {"type": "audio", "format": "pcm_s16le", "sample_rate": 16000, "data": "%%%"}
            ),
            audio(b"x"),
        ]
        async with app.serve(port=0) as server:
            async with connect(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None
            ) as ws:
                for raw in invalid:
                    await ws.send(raw)
                    self.assertEqual(
                        json.loads(await asyncio.wait_for(ws.recv(), 2)),
                        {"type": "error", "code": "invalid_audio"},
                    )
                self.assertEqual(calls, [])
        await app.close()

    async def test_browser_origin_rejected(self):
        import voice_gateway as vg
        from websockets.exceptions import InvalidStatus

        async def reply(text):
            return text

        app = vg.VoiceGateway(Speech(), reply)
        async with app.serve(port=0) as server:
            self.assertEqual(server.sockets[0].getsockname()[0], "127.0.0.1")
            with self.assertRaises(InvalidStatus):
                async with connect(
                    f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                    proxy=None,
                    origin="https://untrusted.example",
                ):
                    pass
        await app.close()

    async def test_playback_ack_avoids_spurious_interrupt(self):
        import voice_gateway as vg

        async def reply(text):
            return text

        app = vg.VoiceGateway(Speech(), reply, utterance_gap=0.01)
        async with app.serve(port=0) as server:
            async with connect(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None
            ) as ws:
                await ws.send(audio())
                for _ in range(4):
                    await asyncio.wait_for(ws.recv(), 2)
                await ws.send(json.dumps({"type": "playback_done"}))
                await ws.send(audio())
                first = json.loads(await asyncio.wait_for(ws.recv(), 2))
                self.assertEqual(first["type"], "text")
        await app.close()

    async def test_launcher_brain_reuses_entry_voice_channel_and_shared_timeline(self):
        import threading
        from pathlib import Path
        from unittest.mock import patch

        import voice_gateway as vg

        event_thread = threading.get_ident()
        captured = []

        def launch(*args, **kwargs):
            captured.append((args, kwargs, threading.get_ident()))
            return {"ok": True, "text": "brain-result", "generation_degraded": False}

        root = Path("/unopened-private-data")
        brain = vg.LauncherBrain({"model": "fixture"}, "FAKE-KEY", root)
        with (
            patch("chat_launcher.launch", side_effect=launch),
            patch("chat_launcher.timeline_identity", return_value="timeline-fixture"),
        ):
            self.assertEqual(await brain("transcript"), "brain-result")
        args, kwargs, actual_thread = captured[0]
        self.assertEqual(args[2:], ("transcript", root, "timeline-fixture"))
        self.assertEqual(kwargs["source_channel"], "voice")
        self.assertEqual(kwargs["lock_wait_timeout"], 20.0)
        self.assertNotEqual(actual_thread, event_thread)
        self.assertTrue(kwargs["attempt_id"])

    async def test_launcher_brain_degraded_result_is_not_spoken_or_retried(self):
        from pathlib import Path
        from unittest.mock import patch

        import voice_gateway as vg

        brain = vg.LauncherBrain({}, "FAKE-KEY", Path("/unopened-private-data"))
        with (
            patch(
                "chat_launcher.launch",
                return_value={"ok": True, "text": "degraded", "generation_degraded": True},
            ) as launch,
            patch("chat_launcher.timeline_identity", return_value="timeline-fixture"),
        ):
            with self.assertRaisesRegex(RuntimeError, "brain_outcome_unknown"):
                await brain("transcript")
            self.assertEqual(launch.call_count, 1)

    async def test_launcher_error_logs_safe_message_and_raise_site(self):
        import chat_launcher
        import voice_gateway as vg

        async def reply(text):
            raise chat_launcher.LauncherError("已有模型请求或迁移占用数据根，未启动引擎。")

        app = vg.VoiceGateway(Speech(), reply, utterance_gap=0.01)
        with self.assertLogs(vg.LOG, level="WARNING") as captured:
            async with app.serve(port=0) as server:
                async with connect(
                    f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None
                ) as ws:
                    await ws.send(audio())
                    for _ in range(2):
                        raw = await asyncio.wait_for(ws.recv(), 2)
                    self.assertEqual(json.loads(raw), {"type": "error", "code": "turn_failed"})
        await app.close()
        diagnostic = "\n".join(captured.output)
        self.assertIn("已有模型请求或迁移占用数据根", diagnostic)
        self.assertIn("test_voice_gateway.py", diagnostic)
        self.assertIn("LauncherError", diagnostic)

    async def test_provider_error_is_not_echoed_on_socket(self):
        import voice_gateway as vg

        class BrokenSpeech(Speech):
            async def transcribe(self, pcm):
                raise RuntimeError("PRIVATE-provider-value")

        async def reply(text):
            self.fail("brain called after ASR failure")

        app = vg.VoiceGateway(BrokenSpeech(), reply, utterance_gap=0.01)
        async with app.serve(port=0) as server:
            async with connect(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None
            ) as ws:
                await ws.send(audio())
                raw = await asyncio.wait_for(ws.recv(), 2)
                self.assertNotIn("PRIVATE", raw)
                self.assertEqual(json.loads(raw), {"type": "error", "code": "turn_failed"})
        await app.close()

    async def test_stale_playback_ack_cannot_clear_current_generation(self):
        import voice_gateway as vg

        async def reply(text):
            return text

        app = vg.VoiceGateway(Speech(), reply, utterance_gap=0.01)
        async with app.serve(port=0) as server:
            async with connect(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None
            ) as ws:
                await ws.send(audio())
                for _ in range(4):
                    await asyncio.wait_for(ws.recv(), 2)
                await ws.send(json.dumps({"type": "playback_done", "generation": 0}))
                await ws.send(audio())
                self.assertEqual(
                    json.loads(await asyncio.wait_for(ws.recv(), 2)), {"type": "interrupt"}
                )
        await app.close()

    async def test_client_limit_is_bounded_and_other_client_survives(self):
        import voice_gateway as vg
        from websockets.exceptions import ConnectionClosedError

        async def reply(text):
            return text

        app = vg.VoiceGateway(Speech(), reply, max_clients=1, utterance_gap=0.01)
        async with app.serve(port=0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with connect(url, proxy=None) as one, connect(url, proxy=None) as two:
                with self.assertRaises(ConnectionClosedError):
                    await two.recv()
                self.assertEqual(two.close_code, 1013)
                await one.send(audio())
                first = json.loads(await asyncio.wait_for(one.recv(), 2))
                self.assertEqual(first["role"], "user")
        await app.close()

    async def test_oversized_utterance_is_rejected_without_interruption(self):
        import voice_gateway as vg

        async def reply(text):
            self.fail("oversized audio must not call brain")

        app = vg.VoiceGateway(Speech(), reply, utterance_gap=1)
        async with app.serve(port=0) as server:
            async with connect(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None
            ) as ws:
                await ws.send(audio(b"\x00\x01" * (vg.MAX_AUDIO_BYTES // 2)))
                await ws.send(audio())
                self.assertEqual(
                    json.loads(await asyncio.wait_for(ws.recv(), 2)),
                    {"type": "error", "code": "invalid_audio"},
                )
        await app.close()

    async def test_stream_failure_falls_back_once_without_new_brain_call(self):
        import voice_gateway as vg

        calls = {"stream": 0, "fallback": 0}

        class Stream:
            def cancel(self):
                pass

            async def stream(self, fragments):
                calls["stream"] += 1
                raise vg.StreamingTTSError("quota")
                yield b""

        class Provider(Speech):
            def create_stream(self):
                return Stream()

            async def synthesize(self, text):
                calls["fallback"] += 1
                yield b"legacy-mp3"

        async def reply(text):
            return "回退验证。"

        app = vg.VoiceGateway(Provider(), reply)
        async with app.serve(port=0) as server:
            async with connect(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "device_id": "fallback-test",
                            "device_type": "mac",
                            "sample_rate": 16000,
                            "channels": 1,
                            "encoding": "pcm_s16le",
                            "stream_pcm": True,
                            "state": {},
                        }
                    )
                )
                await ws.recv()
                await ws.send(
                    json.dumps({"type": "text", "device_id": "fallback-test", "text": "go"})
                )
                seen = []
                while True:
                    item = json.loads(await asyncio.wait_for(ws.recv(), 2))
                    seen.append(item)
                    if item.get("type") == "audio" and item.get("final"):
                        break
                self.assertEqual(calls, {"stream": 1, "fallback": 1})
                self.assertEqual(
                    [m["format"] for m in seen if m.get("type") == "audio"],
                    ["mp3", "mp3"],
                )
        await app.close()

    async def test_new_audio_cancels_stream_without_final_audio(self):
        import voice_gateway as vg

        started = asyncio.Event()
        cancelled = asyncio.Event()

        class Stream:
            def cancel(self):
                cancelled.set()

            async def stream(self, fragments):
                async for _ in fragments:
                    started.set()
                    await asyncio.Event().wait()
                yield b"never"

        class Provider(Speech):
            def create_stream(self):
                return Stream()

            async def synthesize(self, text):
                raise AssertionError("cancelled stream must not fallback")
                yield b""

        async def reply(text):
            return "打断。"

        app = vg.VoiceGateway(Provider(), reply)
        async with app.serve(port=0) as server:
            async with connect(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "device_id": "cancel-test",
                            "device_type": "mac",
                            "sample_rate": 16000,
                            "channels": 1,
                            "encoding": "pcm_s16le",
                            "stream_pcm": True,
                            "state": {},
                        }
                    )
                )
                await ws.recv()
                await ws.send(
                    json.dumps({"type": "text", "device_id": "cancel-test", "text": "go"})
                )
                await asyncio.wait_for(started.wait(), 2)
                await ws.send(json.dumps({"type": "audio_commit", "device_id": "cancel-test"}))
                await asyncio.wait_for(cancelled.wait(), 2)
                await asyncio.sleep(0.1)
                self.assertTrue(cancelled.is_set())
        await app.close()


class DashScopeTests(unittest.IsolatedAsyncioTestCase):
    async def run_provider(self, task, *, fail=False, wrong_id=False):
        import voice_gateway as vg
        from websockets.asyncio.server import serve

        seen = []

        async def vendor(ws):
            start = json.loads(await ws.recv())
            seen.append(start)
            tid = start["header"]["task_id"]

            async def event(name, payload=None):
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "task_id": "wrong" if wrong_id else tid,
                                "event": name,
                                "error_message": "PRIVATE-provider-string",
                            },
                            "payload": payload or {},
                        }
                    )
                )

            await event("task-failed" if fail else "task-started")
            if fail or wrong_id:
                return
            if task == "asr":
                chunk = await ws.recv()
                self.assertEqual(chunk, b"\x00\x01" * 160)
                seen.append(json.loads(await ws.recv()))
                await event(
                    "result-generated",
                    {"output": {"sentence": {"text": "partial", "sentence_end": False}}},
                )
                await event(
                    "result-generated",
                    {"output": {"sentence": {"text": "final", "sentence_end": True}}},
                )
            else:
                seen.append(json.loads(await ws.recv()))
                seen.append(json.loads(await ws.recv()))
                await ws.send(b"ID3-provider")
            await event("task-finished")

        async with serve(vendor, "127.0.0.1", 0) as server:

            def connector(url, **kwargs):
                kwargs.pop("additional_headers")
                return connect(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", **kwargs)

            provider = vg.DashScopeSpeech("FAKE-KEY", connector=connector)
            if task == "asr":
                result = await provider.transcribe(b"\x00\x01" * 160)
            else:
                result = b"".join([chunk async for chunk in provider.synthesize("reply")])
        return result, seen

    async def test_asr_official_wire_protocol(self):
        result, seen = await self.run_provider("asr")
        self.assertEqual(result, "final")
        self.assertEqual(seen[0]["payload"]["model"], "fun-asr-realtime")
        self.assertEqual(seen[1]["header"]["action"], "finish-task")

    async def test_tts_official_wire_protocol_and_exact_voice(self):
        result, seen = await self.run_provider("tts")
        self.assertEqual(result, b"ID3-provider")
        self.assertEqual(
            seen[0]["payload"]["parameters"]["voice"],
            "cosyvoice-v3.5-flash-jarvis-f7bec1631ddf4725841995ba9c711376",
        )
        self.assertEqual(seen[1]["header"]["action"], "continue-task")
        self.assertEqual(seen[2]["header"]["action"], "finish-task")

    async def test_provider_failure_sanitized(self):
        with self.assertRaisesRegex(RuntimeError, "^provider_task_failed$"):
            await self.run_provider("asr", fail=True)

    async def test_task_id_mismatch_refused(self):
        with self.assertRaisesRegex(RuntimeError, "^provider_task_mismatch$"):
            await self.run_provider("tts", wrong_id=True)


if __name__ == "__main__":
    unittest.main()
