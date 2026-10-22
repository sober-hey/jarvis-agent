"""Authenticated iOS protocol acceptance over real private WebSockets."""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
from pathlib import Path

import pytest
import voice_gateway as vg
from inner_loop import MindStore
from voice_ledger import VoiceLedger
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


def async_test(function):
    def run():
        asyncio.run(function())

    run.__name__ = function.__name__
    return run


TOKEN = "fixture-gateway-token-not-a-model-key"
HEADERS = {"Authorization": "Bearer " + TOKEN}


def hello(identity="iphone-one", kind="iphone", **state):
    return {
        "type": "hello",
        "device_id": identity,
        "device_type": kind,
        "sample_rate": 16000,
        "channels": 1,
        "encoding": "pcm_s16le",
        "state": state,
    }


class Speech:
    async def transcribe(self, pcm):
        return {"text": "你好", "emotion": "happy", "asr_confidence": None}

    async def synthesize(self, text):
        yield b"ID3-fixture"


async def receive(ws):
    return json.loads(await asyncio.wait_for(ws.recv(), 3))


@async_test
async def test_authenticated_ios_hello_state_audio_text_ledger():
    calls = []

    async def reply(text):
        calls.append(text)
        return "答：" + text

    with tempfile.TemporaryDirectory(dir=Path.home(), prefix=".jarvis-gateway-") as folder:
        root = Path(folder) / "data"
        root.mkdir(mode=0o700)
        ledger = VoiceLedger(MindStore(root))
        app = vg.VoiceGateway(Speech(), reply, token=TOKEN, ledger=ledger, status_root=root)
        async with app.serve(port=0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with connect(url, proxy=None, additional_headers=HEADERS) as ws:
                await ws.send(json.dumps(hello(headphones_connected=True, headphones_worn=None)))
                assert (await receive(ws))["type"] == "selected"
                await ws.send(
                    json.dumps(
                        {
                            "type": "device_state",
                            "device_id": "iphone-one",
                            "state": {"screen_on": None, "face_down": None},
                        }
                    )
                )
                await ws.send(
                    json.dumps(
                        {
                            "type": "audio",
                            "format": "pcm_s16le",
                            "sample_rate": 16000,
                            "data": base64.b64encode(b"\x00\x01" * 320).decode(),
                        }
                    )
                )
                await ws.send('{"type":"audio_end"}')
                frames = [await receive(ws) for _ in range(4)]
                assert frames[0]["text"] == "你好"
                assert frames[1]["text"] == "答：你好"
                assert frames[-1]["final"] is True
                await ws.send(json.dumps({"type": "playback_started", "generation": 1}))
                await ws.send(json.dumps({"type": "playback_done", "generation": 1}))
                await ws.send(
                    json.dumps({"type": "text", "device_id": "iphone-one", "text": "intent"})
                )
                frames2 = [await receive(ws) for _ in range(4)]
                assert frames2[1]["text"] == "答：intent"
                await ws.send(
                    json.dumps({"type": "text", "device_id": "iphone-one", "text": "interrupt"})
                )
                assert (await receive(ws))["type"] == "interrupt"
                for _ in range(4):
                    await receive(ws)
                assert vg.read_gateway_status(root)["gateway_running"] is True
                assert vg.read_gateway_status(root)["last_call_at"]
        await app.close()
        turns = ledger.recent_voice_turns()
        assert len(turns) == 3
        first = turns[-1]
        assert first["user"]["payload"]["emotion"] == "happy"
        assert first["tts"][0]["payload"]["status"] == "success"
        assert first["playback"][0]["status"] == "ended"
        assert turns[1]["interrupts"]
        assert calls == ["你好", "intent", "interrupt"]
        assert vg.read_gateway_status(root)["gateway_running"] is False


@async_test
async def test_auth_required_before_upgrade_and_identity_binding():
    async def reply(text):
        return text

    app = vg.VoiceGateway(Speech(), reply, token=TOKEN)
    async with app.serve(port=0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        for headers in (None, {"Authorization": "Bearer wrong"}):
            with pytest.raises(InvalidStatus) as error:
                async with connect(url, proxy=None, additional_headers=headers):
                    pass
            assert error.value.response.status_code == 401
        async with connect(url, proxy=None, additional_headers=HEADERS) as ws:
            await ws.send(json.dumps(hello()))
            await receive(ws)
            await ws.send(json.dumps({"type": "device_state", "device_id": "another", "state": {}}))
            assert (await receive(ws))["code"] == "invalid_control"
    await app.close()


@async_test
async def test_multi_device_one_winner_and_stale_snapshot_suppression():
    calls = []

    async def reply(text):
        calls.append(text)
        return text

    app = vg.VoiceGateway(Speech(), reply, token=TOKEN, heartbeat_ttl=0.2)
    async with app.serve(port=0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with (
            connect(url, proxy=None, additional_headers=HEADERS) as mac,
            connect(url, proxy=None, additional_headers=HEADERS) as phone,
        ):
            await mac.send(json.dumps(hello("mac-one", "mac")))
            assert (await receive(mac))["type"] == "selected"
            await phone.send(json.dumps(hello()))
            assert (await receive(phone))["type"] == "selected"
            assert (await receive(mac))["type"] == "suppressed"
            await mac.send(
                json.dumps({"type": "text", "device_id": "mac-one", "text": "must not answer"})
            )
            assert (await receive(mac))["type"] == "suppressed"
            await phone.send(
                json.dumps({"type": "text", "device_id": "iphone-one", "text": "winner"})
            )
            for _ in range(4):
                await receive(phone)
            assert calls == ["winner"]
            await asyncio.sleep(0.25)
            await phone.send(
                json.dumps({"type": "text", "device_id": "iphone-one", "text": "stale"})
            )
            stale_frames = [await receive(phone), await receive(phone)]
            assert any(frame["type"] == "suppressed" for frame in stale_frames)
            assert calls == ["winner"]
    await app.close()


def test_no_wildcard_listener_or_unauthenticated_tailnet():
    async def reply(text):
        return text

    with pytest.raises(ValueError):
        vg.VoiceGateway(Speech(), reply, bind_hosts=("0.0.0.0",), token=TOKEN)
    with pytest.raises(ValueError):
        vg.VoiceGateway(Speech(), reply, bind_hosts=vg.BIND_HOSTS)


@async_test
async def test_state_heartbeat_does_not_split_pcm_or_advance_generation():
    calls = []

    class FrameSpeech(Speech):
        async def transcribe(self, pcm):
            assert pcm == b"\x00\x01" * 640
            return {"text": "whole utterance", "emotion": None}

    async def reply(text):
        calls.append(text)
        return text

    app = vg.VoiceGateway(FrameSpeech(), reply, token=TOKEN, utterance_gap=2)
    async with app.serve(port=0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with connect(url, proxy=None, additional_headers=HEADERS) as ws:
            await ws.send(json.dumps(hello()))
            assert (await receive(ws))["type"] == "selected"
            audio = {
                "type": "audio",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "data": base64.b64encode(b"\x00\x01" * 320).decode(),
            }
            await ws.send(json.dumps(audio))
            await ws.send(
                json.dumps(
                    {
                        "type": "device_state",
                        "device_id": "iphone-one",
                        "state": {"protected_data_available": False, "screen_on": None},
                    }
                )
            )
            await ws.send(json.dumps(audio))
            await ws.send('{"type":"audio_end"}')
            frames = [await receive(ws) for _ in range(4)]
            assert all(frame["generation"] == 1 for frame in frames)
            assert calls == ["whole utterance"]
    await app.close()


@async_test
async def test_duplicate_identity_and_explicit_ineligible_state():
    async def reply(text):
        raise AssertionError("ineligible device must not reach brain")

    app = vg.VoiceGateway(Speech(), reply, token=TOKEN)
    async with app.serve(port=0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with (
            connect(url, proxy=None, additional_headers=HEADERS) as one,
            connect(url, proxy=None, additional_headers=HEADERS) as two,
        ):
            await one.send(json.dumps(hello(face_down=True)))
            assert (await receive(one))["type"] == "suppressed"
            await two.send(json.dumps(hello()))
            assert (await receive(two))["code"] == "invalid_control"
            await one.send(
                json.dumps({"type": "text", "device_id": "iphone-one", "text": "blocked"})
            )
            assert (await receive(one))["type"] == "suppressed"
    await app.close()


@async_test
async def test_a_thinking_device_does_not_hold_the_voice_channel():
    """An owner that is only waiting on the brain must yield to another device.

    Otherwise the microphone stays on one device for the whole brain call and
    every other device is suppressed — the reported "it keeps occupying the
    voice channel" behaviour.
    """
    gate = asyncio.Event()
    started = asyncio.Event()

    async def reply(text):
        started.set()
        await gate.wait()
        return "答：" + text

    with tempfile.TemporaryDirectory(dir=Path.home(), prefix=".jarvis-gateway-") as folder:
        root = Path(folder) / "data"
        root.mkdir(mode=0o700)
        app = vg.VoiceGateway(Speech(), reply, token=TOKEN, status_root=root)
        async with app.serve(port=0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with connect(url, proxy=None, additional_headers=HEADERS) as mac:
                await mac.send(json.dumps(hello("mac-one", "mac")))
                assert (await receive(mac))["type"] == "selected"
                await mac.send(
                    json.dumps(
                        {
                            "type": "audio",
                            "format": "pcm_s16le",
                            "sample_rate": 16000,
                            "data": base64.b64encode(b"\x00\x01" * 320).decode(),
                        }
                    )
                )
                await mac.send('{"type":"audio_end"}')
                assert (await receive(mac))["text"] == "你好"  # transcript echo
                await asyncio.wait_for(started.wait(), 3)  # brain call in flight
                async with connect(url, proxy=None, additional_headers=HEADERS) as phone:
                    await phone.send(json.dumps(hello("iphone-one", "iphone")))
                    # The phone wins: the mac is thinking, not capturing.
                    assert (await receive(phone))["type"] == "selected"
                    assert (await receive(mac))["type"] == "suppressed"
                    gate.set()
        await app.close()
