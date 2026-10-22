"""Guarded, single-device XiaoAI cloud TTS. Importing performs no I/O.

The retained LX05 transport is host-specific; no native microphone, custom
voice, interruption, dialogue brain or continuous-listening capability implied.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

TOOLS = Path("/Volumes/扩展硬盘/hermes/.hermes/tools")
PYTHON = TOOLS / "xiaoai-venv/bin/python"
SAY = TOOLS / "xiaoai_say.sh"
DEVICE_ID = "d547528a-bdbc-4784-ac8d-ba20ba99e5fa"
TOKEN_PATH = Path.home() / ".mi.token"
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


class XiaoAIError(RuntimeError):
    """Safe diagnostic; never expose a cloud response, token or spoken text."""

    def __init__(self, code: str, message: str, *, status: str = "failed"):
        super().__init__(message)
        self.code = code
        self.status = status


def _environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in (*PROXY_KEYS, "MI_PASS", "MI_USER")
    }


def _failure(raw: str) -> XiaoAIError:
    lowered = raw.lower()
    if any(word in lowered for word in ("401", "403", "auth", "login", "token", "credential")):
        return XiaoAIError("credentials_invalid", "小米凭据缺失或失效，需要在本机终端手动重新登录")
    if "offline" in lowered:
        return XiaoAIError("device_offline", "小爱音箱离线")
    if "volume" in lowered or "音量" in raw:
        return XiaoAIError("volume_unverified", "无法确认音量不超过25，已拒绝播报")
    return XiaoAIError("service_unavailable", "小米云服务不可用或请求失败", status="unknown")


def _run_bounded(argv: list[str], *, timeout: float, **kwargs: Any) -> Any:
    """Bound the shell AND its SDK descendants, not just the direct child."""
    import signal

    kwargs.pop("capture_output", None)
    kwargs.pop("check", None)
    process = subprocess.Popen(
        argv, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


class XiaoAIVoice:
    """Synchronous API; run in a worker thread from an async gateway."""

    def __init__(self, *, runner: Any = _run_bounded):
        self._runner = runner

    def speak(
        self, text: object, *, context: dict[str, object] | None = None, now: Any = None
    ) -> dict[str, object]:
        """Submit once, never retry uncertain TTS; success is not heard confirmation."""
        from device_routing import xiaoai_output_allowed

        if not isinstance(text, str) or not text.strip() or len(text) > 2000 or "\x00" in text:
            raise ValueError("TTS text must contain 1..2000 characters without NUL")
        snapshot = self._context() if context is None else context
        if not xiaoai_output_allowed(snapshot, now=now):
            raise XiaoAIError(
                "presence_blocked", "未确认在家且在卧室/客厅，或处于23:00–08:00静默时段"
            )
        if not self.is_available():
            raise XiaoAIError("device_offline", "小爱音箱离线")
        try:
            result = self._runner(
                ["/bin/bash", str(SAY), text],
                capture_output=True,
                text=True,
                timeout=220,
                check=False,
                env=_environment(),
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise XiaoAIError(
                "service_unavailable", "小爱播报超时，结果未知，禁止自动重发", status="unknown"
            ) from None
        except OSError:
            raise XiaoAIError("transport_missing", "本机小爱安全播报入口不可用") from None
        if result.returncode:
            raise _failure(result.stderr)
        # Existing wrapper's exit code is not sufficient: require cloud TTS ack
        # plus its independent post-write volume witness.
        import re

        match = re.search(r"volume readback=(\d+) \(<=25\)", result.stderr)
        if match is None or not 0 <= int(match.group(1)) <= 25 or result.stdout.strip() != "true":
            raise XiaoAIError(
                "volume_unverified", "安全回执不完整，不能确认播报已提交", status="unknown"
            )
        logging.getLogger(__name__).info("xiaoai_tts_submitted device=%s heard=false", DEVICE_ID)
        return {"device_id": DEVICE_ID, "status": "submitted", "heard": False}

    def _context(self) -> dict[str, object]:
        """GPS cannot resolve rooms. Until a room sensor is wired, default is silent."""
        try:
            result = self._runner(
                [sys.executable, str(TOOLS.parent / "scripts/presence.py"), "--json"],
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
                env=_environment(),
                stdin=subprocess.DEVNULL,
            )
            value: Any = json.loads(result.stdout) if result.returncode == 0 else {}
            return cast(dict[str, object], value) if isinstance(value, dict) else {}
        except (OSError, subprocess.SubprocessError, ValueError):
            return {}

    def is_available(self) -> bool:
        """False means the exact bound LX05 is absent/offline; infrastructure raises."""
        try:
            result = self._runner(
                [str(PYTHON), str(Path(__file__).resolve()), "--probe"],
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
                env=_environment(),
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise XiaoAIError("service_unavailable", "小米云在线查询超时") from None
        except OSError:
            raise XiaoAIError("transport_missing", "本机小爱云通道解释器不可用") from None
        if result.returncode:
            raise _failure(result.stderr)
        try:
            value: Any = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError
            online = cast(dict[str, Any], value).get("online")
            if type(online) is not bool:
                raise ValueError
            return online
        except (ValueError, TypeError):
            raise XiaoAIError("service_unavailable", "小米云在线查询返回无效数据") from None


def speak(
    text: object, *, context: dict[str, object] | None = None, now: Any = None
) -> dict[str, object]:
    return XiaoAIVoice().speak(text, context=context, now=now)


def is_available() -> bool:
    return XiaoAIVoice().is_available()


async def _probe() -> bool:
    # Keep secrets inside the bounded child, never read them into a tool response.
    import importlib
    import stat

    sdk = importlib.import_module("miservice")
    biohttp = importlib.import_module("miservice.biohttp")

    if TOKEN_PATH.is_symlink() or not TOKEN_PATH.is_file():
        raise PermissionError("token missing")
    if stat.S_IMODE(TOKEN_PATH.stat().st_mode) & 0o077:
        raise PermissionError("token permission invalid")
    try:
        raw: Any = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError
        token = cast(dict[str, Any], raw)
        if not token.get("userId") or not token.get("micoapi"):
            raise ValueError
    except (ValueError, TypeError):
        raise PermissionError("token invalid") from None

    async def no_login(_sid: str) -> bool:
        raise PermissionError("credentials invalid; manual login required")

    async with biohttp.ClientSession() as session:
        account = sdk.MiAccount(session, "", "", token_store=None)
        account.token = token
        account.login = no_login  # Never silently log in or delete the host token.
        devices: Any = await sdk.MiNAService(account).device_list()
        if not isinstance(devices, list):
            raise RuntimeError("invalid device response")
        for item in cast(list[Any], devices):
            if isinstance(item, dict):
                device = cast(dict[str, Any], item)
                if device.get("deviceID") == DEVICE_ID and device.get("presence") == "online":
                    return True
        return False


def self_evaluate() -> dict[str, Any]:
    """Offline declaration only; no cloud call, token read or playback here."""
    return {
        "module": "voice_xiaoai",
        "domain": "guarded single-device XiaoAI cloud TTS submission",
        "current_version": "1.0.0",
        "known_limitations": [
            "One hardcoded LX05 device id; no discovery or multi-device arbitration here.",
            "Cloud submission is not proof the speech was heard or completed playback.",
            "No microphone, dialogue brain, interruption or continuous listening.",
            "Requires the host-specific helper scripts and an existing ~/.mi.token session.",
            "No resident caller, capability, mind provenance or KG projection is provided.",
        ],
        "improvement_directions": [
            "Confirm delivery with a real playback observation instead of submission status.",
            "Replace the fixed helper path and device id with verified per-host configuration.",
            "Attach voice observations to the ledger and mind provenance.",
        ],
        "last_evaluated_at": None,
    }


def _main() -> int:
    import asyncio

    logging.disable(logging.CRITICAL)  # SDK auth failures can include cloud payloads.
    try:
        print(json.dumps({"online": asyncio.run(asyncio.wait_for(_probe(), timeout=25))}))
        return 0
    except Exception as error:
        print(_failure(str(error)).code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    if sys.argv[1:] != ["--probe"]:
        raise SystemExit("Only --probe is supported; use guarded speak() from JARVIS")
    raise SystemExit(_main())
