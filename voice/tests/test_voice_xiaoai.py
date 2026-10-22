"""XiaoAI contract tests: subprocess/network boundaries are mocked."""

from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest


def voice_module():
    assert importlib.util.find_spec("voice_xiaoai") is not None, "XiaoAI output adapter missing"
    return importlib.import_module("voice_xiaoai")


def test_online_probe_is_read_only_and_checks_exact_device():
    voice = voice_module()
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"online": true}', ""))
    adapter = voice.XiaoAIVoice(runner=runner)
    assert adapter.is_available() is True
    argv = runner.call_args.args[0]
    assert argv[-1] == "--probe"
    assert "xiaoai_say.sh" not in " ".join(argv)
    assert runner.call_args.kwargs["env"].get("HTTPS_PROXY") is None
    assert runner.call_args.kwargs["env"].get("MI_PASS") is None


def test_offline_probe_returns_false():
    voice = voice_module()
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"online": false}', ""))
    assert voice.XiaoAIVoice(runner=runner).is_available() is False


def test_presence_and_room_fail_closed_before_network():
    voice = voice_module()
    now = datetime(2026, 10, 20, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    for changes in (
        {"state": "away"},
        {"stale": True},
        {"room": None},
        {"room_observed_at": "2026-10-20T13:00:00+08:00"},
        {"last_point_ts": "2026-10-20T09:00:00+08:00"},
    ):
        runner = Mock()
        with pytest.raises(voice.XiaoAIError) as caught:
            voice.XiaoAIVoice(runner=runner).speak(
                "消息", context={**home_context(), **changes}, now=now
            )
        assert caught.value.code == "presence_blocked"
        runner.assert_not_called()


@pytest.mark.parametrize("text", ["", " ", "x" * 2001, "abc\x00def", 12])
def test_invalid_text_makes_no_request(text):
    voice = voice_module()
    runner = Mock()
    with pytest.raises(ValueError):
        voice.XiaoAIVoice(runner=runner).speak(text)
    runner.assert_not_called()


@pytest.mark.parametrize(
    "stderr,code",
    [
        ("401 unauthorized secret-payload", "credentials_invalid"),
        ("token missing", "credentials_invalid"),
        ("device offline", "device_offline"),
        ("volume failed", "volume_unverified"),
        ("502 secret-payload", "service_unavailable"),
    ],
)
def test_transport_failures_are_distinct_and_sanitized(stderr, code):
    voice = voice_module()
    runner = Mock(return_value=subprocess.CompletedProcess([], 1, "", stderr))
    with pytest.raises(voice.XiaoAIError) as caught:
        voice.XiaoAIVoice(runner=runner).is_available()
    assert caught.value.code == code
    assert "secret-payload" not in str(caught.value)
    assert runner.call_count == 1


def test_offline_speaker_does_not_submit_tts():
    voice = voice_module()
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"online": false}', ""))
    now = datetime(2026, 10, 20, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    with pytest.raises(voice.XiaoAIError) as caught:
        voice.XiaoAIVoice(runner=runner).speak("消息", context=home_context(), now=now)
    assert caught.value.code == "device_offline"
    assert runner.call_count == 1


@pytest.mark.parametrize(
    "stdout,stderr",
    [
        ("true", ""),
        ("false", "volume readback=20 (<=25)"),
        ("true", "volume readback=50 (<=25)"),
    ],
)
def test_incomplete_ack_does_not_claim_submitted(stdout, stderr):
    voice = voice_module()
    runner = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, '{"online": true}', ""),
            subprocess.CompletedProcess([], 0, stdout, stderr),
        ]
    )
    now = datetime(2026, 10, 20, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    with pytest.raises(voice.XiaoAIError) as caught:
        voice.XiaoAIVoice(runner=runner).speak("消息", context=home_context(), now=now)
    assert caught.value.code == "volume_unverified"
    assert caught.value.status == "unknown"
    assert runner.call_count == 2


def test_tts_timeout_is_unknown_without_retry():
    voice = voice_module()
    runner = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, '{"online": true}', ""),
            subprocess.TimeoutExpired("wrapper", 220),
        ]
    )
    now = datetime(2026, 10, 20, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    with pytest.raises(voice.XiaoAIError) as caught:
        voice.XiaoAIVoice(runner=runner).speak("消息", context=home_context(), now=now)
    assert caught.value.status == "unknown"
    assert runner.call_count == 2


@pytest.mark.parametrize("body", ["{}", '{"online": "yes"}', "[]", "not-json"])
def test_malformed_online_response_is_not_offline_or_success(body):
    voice = voice_module()
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, body, ""))
    with pytest.raises(voice.XiaoAIError) as caught:
        voice.XiaoAIVoice(runner=runner).is_available()
    assert caught.value.code == "service_unavailable"


def test_subprocess_transport_has_group_timeout_cleanup():
    voice = voice_module()
    assert hasattr(voice, "_run_bounded"), "whole wrapper process group must be bounded"
    from unittest.mock import patch

    process = Mock(pid=123)
    process.communicate.side_effect = [subprocess.TimeoutExpired("probe", 1), ("", "")]
    with patch.object(voice.subprocess, "Popen", return_value=process) as popen:
        with patch.object(voice.os, "killpg") as killpg:
            with pytest.raises(subprocess.TimeoutExpired):
                voice._run_bounded(
                    ["test"],
                    capture_output=True,
                    text=True,
                    timeout=1,
                    check=False,
                    env={},
                    stdin=subprocess.DEVNULL,
                )
    assert popen.call_args.kwargs["start_new_session"] is True
    killpg.assert_called_once()
    assert process.communicate.call_count == 2


def test_probe_uses_exact_bound_device_and_never_relogs(monkeypatch, tmp_path):
    import asyncio
    import types
    from unittest.mock import AsyncMock

    voice = voice_module()
    token = tmp_path / "fake-token"
    token.write_text('{"userId":"test-account","micoapi":["test","test"]}')
    token.chmod(0o600)
    monkeypatch.setattr(voice, "TOKEN_PATH", token)
    service = Mock()
    service.device_list = AsyncMock(
        return_value=[
            {"deviceID": "another-speaker", "presence": "online"},
            {"deviceID": voice.DEVICE_ID, "presence": "offline"},
        ]
    )
    account = Mock()
    account_type = Mock(return_value=account)
    session = Mock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setitem(
        __import__("sys").modules,
        "miservice",
        types.SimpleNamespace(MiAccount=account_type, MiNAService=Mock(return_value=service)),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "miservice.biohttp",
        types.SimpleNamespace(ClientSession=Mock(return_value=session)),
    )
    assert asyncio.run(voice._probe()) is False
    account_type.assert_called_once_with(session, "", "", token_store=None)
    with pytest.raises(PermissionError):
        asyncio.run(account.login("micoapi"))
    assert token.is_file()
    service.device_list.return_value = [{"deviceID": voice.DEVICE_ID, "presence": "online"}]
    assert asyncio.run(voice._probe()) is True
    service.device_list.return_value = {"devices": []}
    with pytest.raises(RuntimeError):
        asyncio.run(voice._probe())
    token.chmod(0o644)
    with pytest.raises(PermissionError):
        asyncio.run(voice._probe())


def test_route_excludes_xiaoai_without_fresh_room_evidence():
    import device_routing

    assert "xiaoai_context" in device_routing.DeviceContext.__dataclass_fields__
    now = datetime(2026, 10, 20, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    devices = (
        device_routing.DeviceState("speaker", "xiaoai"),
        device_routing.DeviceState("phone", "iphone"),
    )
    state = device_routing.DeviceContext(devices=devices, tailscale_online=True, mac_active=True)
    assert device_routing.select_output_device(state, now=now).device_id == "phone"
    from dataclasses import replace

    state = replace(state, xiaoai_context=home_context())
    assert device_routing.select_output_device(state, now=now).device_id == "speaker"
    assert (
        device_routing.select_output_device(
            replace(state, tailscale_online=False, mac_active=False), now=now
        ).device_id
        == "speaker"
    )
    assert (
        device_routing.select_output_device(
            replace(state, xiaoai_context={**home_context(), "room": "厨房"}), now=now
        ).device_id
        == "phone"
    )


def home_context():
    return {
        "state": "home",
        "stale": False,
        "pipeline_ok": True,
        "last_point_ts": "2026-10-20T12:00:00+08:00",
        "room": "卧室",
        "room_observed_at": "2026-10-20T12:00:00+08:00",
    }


def test_speak_uses_existing_safe_wrapper_and_never_claims_heard():
    voice = voice_module()
    assert hasattr(voice.XiaoAIVoice, "speak"), "guarded speak missing"
    runner = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, '{"online": true}', ""),
            subprocess.CompletedProcess([], 0, "true", "xiaoai_say: volume readback=20 (<=25)"),
        ]
    )
    now = datetime(2026, 10, 20, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = voice.XiaoAIVoice(runner=runner).speak("测试消息", context=home_context(), now=now)
    assert result == {"device_id": voice.DEVICE_ID, "status": "submitted", "heard": False}
    assert runner.call_args.args[0] == ["/bin/bash", str(voice.SAY), "测试消息"]
    assert "credentials_invalid" not in json.dumps(result)


def test_routing_checks_home_and_actual_room_not_merely_home():
    import device_routing

    assert hasattr(device_routing, "xiaoai_output_allowed"), "XiaoAI output policy missing"
    now = datetime(2026, 10, 20, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert device_routing.xiaoai_output_allowed(home_context(), now=now)
    for change in (
        {"state": "away"},
        {"stale": True},
        {"room": "厨房"},
        {"room": None},
        {"pipeline_ok": False},
        {"room_observed_at": "2026-10-20T11:00:00+08:00"},
    ):
        assert not device_routing.xiaoai_output_allowed({**home_context(), **change}, now=now)
    assert not device_routing.xiaoai_output_allowed(home_context(), now=now.replace(hour=23))
