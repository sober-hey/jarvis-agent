"""Resident gateway security, listener and brain error contracts (no cloud)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import voice_gateway as vg

TOKEN = "fixture-service-token-not-a-cloud-key"


def test_token_file_requires_exact_private_owned_regular_file(tmp_path):
    token_file = tmp_path / "gateway.token"
    token_file.write_text(TOKEN + "\n")
    token_file.chmod(0o600)
    assert vg.read_gateway_token(token_file) == TOKEN
    for mode in (0o644, 0o400, 0o700, 0o660):
        token_file.chmod(mode)
        with pytest.raises(PermissionError):
            vg.read_gateway_token(token_file)
    token_file.chmod(0o600)
    with patch.object(vg.os, "getuid", return_value=os.getuid() + 1):
        with pytest.raises(PermissionError):
            vg.read_gateway_token(token_file)
    link = tmp_path / "link"
    link.symlink_to(token_file)
    with pytest.raises((PermissionError, OSError)):
        vg.read_gateway_token(link)
    token_file.write_text(TOKEN + "\nsecond-line")
    with pytest.raises(ValueError):
        vg.read_gateway_token(token_file)


def test_en0_address_discovery_is_explicit_and_safe():
    with patch.object(vg.subprocess, "run", return_value=SimpleNamespace(stdout="192.168.1.5\n")):
        assert vg.configured_bind_hosts() == ("127.0.0.1", "100.64.0.1", "192.168.1.5")
    for address in ("0.0.0.0", "8.8.8.8", "not-an-address", ""):
        with patch.object(vg.subprocess, "run", return_value=SimpleNamespace(stdout=address)):
            assert vg.configured_bind_hosts() == (vg.HOST, vg.TAILSCALE_HOST)


def test_lan_requires_token_and_arbitrary_private_hosts_rejected():
    async def reply(text):
        return text

    with patch.object(vg, "en0_address", return_value="192.168.1.5"):
        with pytest.raises(ValueError):
            vg.VoiceGateway(None, reply, bind_hosts=("192.168.1.5",))
        vg.VoiceGateway(None, reply, bind_hosts=("192.168.1.5",), token=TOKEN)
        with pytest.raises(ValueError):
            vg.VoiceGateway(None, reply, bind_hosts=("192.168.99.99",), token=TOKEN)


def test_unavailable_optional_address_keeps_loopback_running():
    import asyncio

    async def check():
        async def reply(text):
            return text

        with patch.object(vg, "en0_address", return_value=vg.LAN_HOST):
            app = vg.VoiceGateway(None, reply, token=TOKEN, bind_hosts=(vg.HOST, vg.LAN_HOST))
        original_serve = vg.serve

        async def fake_serve(handler, host, port, **kwargs):
            if host == vg.LAN_HOST:
                raise OSError("fixture unavailable")
            return await original_serve(handler, host, port, **kwargs)

        with patch.object(vg, "serve", side_effect=fake_serve):
            async with app.serve(port=0) as server:
                assert server.sockets[0].getsockname()[0] == vg.HOST
                assert app.listening_hosts == (vg.HOST,)
        await app.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "state,status,code",
    [
        ("provider_rejected", 402, "brain_provider_rejected"),
        ("dispatched_unknown", None, "brain_outcome_unknown"),
    ],
)
def test_brain_result_error_reaches_socket_without_retry_or_tts(state, status, code):
    import asyncio
    import json

    from websockets.asyncio.client import connect

    class Speech:
        async def synthesize(self, text):
            raise AssertionError("must not synthesize a degraded answer")
            yield b""

    async def check():
        brain = vg.LauncherBrain({}, "FAKE-KEY", Path("/unopened-private-data"))
        response = {
            "ok": True,
            "text": "degraded",
            "generation_degraded": True,
            "model_request_state": state,
            "provider_status": status,
        }
        with (
            patch("chat_launcher.launch", return_value=response) as launch,
            patch("chat_launcher.timeline_identity", return_value="timeline-fixture"),
        ):
            app = vg.VoiceGateway(Speech(), brain)
            async with app.serve(port=0) as server:
                url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                async with connect(url, proxy=None) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "hello",
                                "device_id": "test",
                                "device_type": "mac",
                                "sample_rate": 16000,
                                "channels": 1,
                                "encoding": "pcm_s16le",
                                "state": {},
                            }
                        )
                    )
                    assert json.loads(await ws.recv())["type"] == "selected"
                    await ws.send(
                        json.dumps({"type": "text", "device_id": "test", "text": "hello"})
                    )
                    assert json.loads(await ws.recv())["role"] == "user"
                    error = json.loads(await asyncio.wait_for(ws.recv(), 3))
                    assert error["code"] == code
                    if state == "provider_rejected":
                        assert error["provider_status"] == 402
                    else:
                        assert "provider_status" not in error
            await app.close()
            assert launch.call_count == 1
            with pytest.raises(
                RuntimeError, match="brain_provider_rejected" if status else "brain_outcome_unknown"
            ):
                brain._reply("second independent fixture")

    asyncio.run(check())
