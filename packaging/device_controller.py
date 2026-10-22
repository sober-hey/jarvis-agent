"""Unified, explicit-registration device control; importing never controls devices.

Device configs stay private. Xiaomi uses a miservice token JSON file at
~/.jarvis/xiaomi.key (not a password); install miservice in the engine runtime
before cloud use. No login, device discovery or command is run at startup.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import math
import stat
import subprocess
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


def _timeout(config: Mapping[str, Any]) -> float:
    value = float(config.get("timeout", 10))
    if not math.isfinite(value) or not 0 < value <= 60:
        raise ValueError("timeout must be between 0 and 60 seconds")
    return value


class XiaomiExecutor:
    """MIoT cloud adapter using miservice's signing/encryption implementation.

    config: did, key_path?, region?, timeout?, commands. A command is
    {kind: property, siid: 2, piid: 1, value: true} or
    {kind: action, siid: 3, aiid: 1, args: []}. Property values may instead
    come from params['value']. get_property is read-only. Speaker actions
    are deliberately blocked: audio must go through the presence/volume guard.
    """

    def __init__(self, config: Mapping[str, Any]):
        self.config = deepcopy(dict(config))

    def _token(self) -> dict[str, Any]:
        path = Path(self.config.get("key_path", "~/.jarvis/xiaomi.key")).expanduser()
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("Xiaomi token must be a private regular file (0600)")
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Xiaomi token must be a JSON object")
        token = cast(dict[str, Any], raw)
        if not token.get("userId") or not token.get("xiaomiio"):
            raise ValueError("Xiaomi token missing authenticated xiaomiio service")
        return token

    def configured(self) -> bool:
        try:
            self._token()
            return bool(self.config.get("did"))
        except (OSError, ValueError, TypeError):
            return False

    async def _request(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        token = self._token()
        try:
            sdk = importlib.import_module("miservice")
            biohttp = importlib.import_module("miservice.biohttp")
        except ImportError:
            return {"ok": False, "error": "dependency_missing", "message": "需要安装 miservice"}

        async def no_login(_sid: str) -> bool:
            raise PermissionError("Expired token: manual authentication required")

        async with biohttp.ClientSession() as session:
            # No password, OTP, token writes, relogin or discovery at initialization.
            account = sdk.MiAccount(session, "", "", token_store=None)
            account.token = token
            account.login = no_login
            service = sdk.MiIOService(account, region=self.config.get("region"))
            did = str(self.config["did"])
            if command == "_health":
                devices = await service.device_list("full")
                online = any(
                    str(d.get("did")) == did and d.get("isOnline") is True for d in devices
                )
                return {"ok": online, "verified": True}
            if command == "get_property":
                value = await service.miot_get_prop(did, (int(params["siid"]), int(params["piid"])))
                return {"ok": value is not None, "data": value, "verified": value is not None}
            action = self.config.get("commands", {}).get(command)
            if not isinstance(action, Mapping):
                return {"ok": False, "error": "unknown_command"}
            if self.config.get("device_type") == "speaker" or self.config.get(
                "model", ""
            ).startswith("xiaomi.wifispeaker"):
                return {"ok": False, "error": "speaker_guard_required"}
            action = cast(Mapping[str, Any], action)
            siid = int(action["siid"])
            if action.get("kind", "property") == "property":
                piid = int(action["piid"])
                value = action["value"] if "value" in action else params["value"]
                code = await service.miot_set_prop(did, (siid, piid), value)
                if type(code) is not int or code != 0:
                    return {"ok": False, "error": "device_rejected", "code": code}
                actual = await service.miot_get_prop(did, (siid, piid))
                matches = actual is not None and actual == value
                return {
                    "ok": matches,
                    "verified": matches,
                    "data": actual,
                    **({} if matches else {"error": "verification_failed"}),
                }
            if action.get("kind") == "action":
                code = await service.miot_action(
                    did,
                    (siid, int(action["aiid"])),
                    list(action.get("args", params.get("args", []))),
                )
                accepted = type(code) is int and code == 0
                return {
                    "ok": accepted,
                    "verified": False,
                    "status": "accepted" if accepted else "failed",
                    "code": code,
                }
            return {"ok": False, "error": "invalid_command_config"}

    def execute(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.configured():
            return {
                "ok": False,
                "error": "not_configured",
                "message": "小米云尚未配置：需要 ~/.jarvis/xiaomi.key 和设备 DID",
            }
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            return {"ok": False, "error": "async_context", "message": "请在线程中调用同步设备接口"}

        async def bounded() -> dict[str, Any]:
            return await asyncio.wait_for(
                self._request(command, params), timeout=_timeout(self.config)
            )

        try:
            return asyncio.run(bounded())
        except TimeoutError:
            return {"ok": False, "error": "timeout", "status": "unknown"}
        except (KeyError, ValueError, TypeError):
            return {"ok": False, "error": "invalid_request"}
        except Exception:
            return {"ok": False, "error": "cloud_request_failed", "status": "unknown"}

    def health_check(self) -> bool:
        return bool(self.execute("_health", {}).get("ok"))


class AppleScriptExecutor:
    """Only registered scripts run; params are JSON argv, never script source.

    config: commands: {command: AppleScript source}, health_script? (read-only,
    return true). Scripts receive one JSON argument via 'on run argv'.
    """

    def __init__(self, config: Mapping[str, Any]):
        self.config = deepcopy(dict(config))

    def configured(self) -> bool:
        return bool(self.config.get("commands")) and Path("/usr/bin/osascript").is_file()

    def _run(self, script: Any, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(script, str) or not script.strip():
            return {"ok": False, "error": "invalid_command_config"}
        try:
            completed = subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    script,
                    "--",
                    json.dumps(params, ensure_ascii=False, allow_nan=False),
                ],
                capture_output=True,
                text=True,
                timeout=_timeout(self.config),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout", "status": "unknown"}
        if completed.returncode:
            return {"ok": False, "error": "osascript_failed", "returncode": completed.returncode}
        return {
            "ok": True,
            "data": completed.stdout.strip(),
            "verified": False,
            "status": "accepted",
        }

    def execute(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        script = self.config.get("commands", {}).get(command)
        if script is None:
            return {"ok": False, "error": "unknown_command"}
        return self._run(script, params)

    def health_check(self) -> bool:
        # Never use a control command as a health probe.
        script = self.config.get("health_script")
        if script is None:
            return False
        result = self._run(script, {})
        return bool(result.get("ok") and str(result.get("data")).lower() == "true")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        # Never forward device credentials or commands to another target.
        return None


class HTTPExecutor:
    """config: base_url, commands: {name: {method, path}}, headers?, timeout?,
    health_path? (read-only GET). Paths must stay on the registered origin;
    execute params become a JSON body or GET query. No proxy or auto retry.
    """

    def __init__(self, config: Mapping[str, Any]):
        self.config = deepcopy(dict(config))

    def _url(self, path: Any) -> str:
        base = str(self.config.get("base_url", ""))
        parsed = urlsplit(base)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Invalid device base_url")
        if not isinstance(path, str) or urlsplit(path).scheme or path.startswith("//"):
            raise ValueError("Command path must be relative to registered origin")
        url = urljoin(base.rstrip("/") + "/", path)
        if urlsplit(url).netloc != parsed.netloc:
            raise ValueError("Cross-origin device command")
        return url

    def configured(self) -> bool:
        try:
            self._url("")
            return bool(self.config.get("commands"))
        except (ValueError, TypeError):
            return False

    def _request(self, spec: Mapping[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        from urllib.parse import urlencode

        url = self._url(spec.get("path", ""))
        method = str(spec.get("method", "POST")).upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            return {"ok": False, "error": "invalid_method"}
        headers = dict(self.config.get("headers", {}))
        body = None
        if method == "GET":
            if params:
                url += ("&" if "?" in url else "?") + urlencode(params, doseq=True)
        else:
            body = json.dumps(params, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(
                request, timeout=_timeout(self.config)
            ) as response:
                payload = response.read(1_048_577)
                if len(payload) > 1_048_576:
                    return {"ok": False, "error": "response_too_large", "status": "unknown"}
                text = payload.decode("utf-8")
                try:
                    data = json.loads(text) if text else None
                except ValueError:
                    data = text
                # HTTP success acknowledges a request, not physical state.
                return {
                    "ok": 200 <= response.status < 300,
                    "http_status": response.status,
                    "data": data,
                    "verified": False,
                    "status": "accepted",
                }
        except Exception:
            return {"ok": False, "error": "http_request_failed", "status": "unknown"}

    def execute(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.configured():
            return {"ok": False, "error": "not_configured"}
        spec = self.config["commands"].get(command)
        if not isinstance(spec, Mapping):
            return {"ok": False, "error": "unknown_command"}
        return self._request(cast(Mapping[str, Any], spec), params)

    def health_check(self) -> bool:
        path = self.config.get("health_path")
        return bool(
            path is not None and self._request({"method": "GET", "path": path}, {}).get("ok")
        )


class DeviceController:
    """Ordered in-memory registry. Re-registering an ID preserves its position."""

    def __init__(self) -> None:
        self._devices: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._executors = {
            "xiaomi": XiaomiExecutor,
            "applescript": AppleScriptExecutor,
            "http": HTTPExecutor,
        }

    def self_evaluate(self) -> dict[str, Any]:
        """Describe adapter gaps without probing or controlling any device."""
        from datetime import UTC, datetime

        return {
            "module": "device_controller",
            "domain": "smart home IoT device control python",
            "current_version": "ordered device registry with Xiaomi, AppleScript and HTTP adapters",
            "known_limitations": [
                "小米 miservice 尚未安装，真实云控制未验证",
                "HTTP 执行器无认证层",
            ],
            "improvement_directions": ["HA（Home Assistant）中间件对接", "MQTT 协议支持"],
            "last_evaluated_at": datetime.now(UTC).isoformat(),
        }

    def register_device(self, device_id: Any, name: Any, executor_type: str, config: Any) -> None:
        if (
            not isinstance(device_id, str)
            or not device_id.strip()
            or not isinstance(name, str)
            or not name.strip()
        ):
            raise ValueError("device_id and name must be non-empty strings")
        if executor_type not in self._executors:
            raise ValueError("Unknown executor type")
        if not isinstance(config, Mapping):
            raise ValueError("config must be a mapping")
        self._devices[device_id] = {
            "device_id": device_id,
            "name": name,
            "executor_type": executor_type,
            "executor": self._executors[executor_type](cast(Mapping[str, Any], config)),
        }

    def list_devices(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for device in self._devices.values():
            try:
                configured = device["executor"].configured()
            except (OSError, ValueError, TypeError):
                configured = False
            result.append(
                {
                    "device_id": device["device_id"],
                    "name": device["name"],
                    "executor_type": device["executor_type"],
                    "configured": configured,
                    "commands": list(device["executor"].config.get("commands", {})),
                }
            )
        return result

    def execute(self, device_id: str, command: Any, params: Any = None) -> dict[str, Any]:
        device = self._devices.get(device_id)
        if device is None:
            return {"ok": False, "device_id": device_id, "error": "unknown_device"}
        if (
            not isinstance(command, str)
            or not command
            or (params is not None and not isinstance(params, Mapping))
        ):
            return {"ok": False, "device_id": device_id, "error": "invalid_request"}
        try:
            result = device["executor"].execute(
                command, deepcopy(dict(cast(Mapping[str, Any], params or {})))
            )
        except Exception:
            # Device libraries can embed credentials in errors; do not return them.
            result = {"ok": False, "error": "execution_failed"}
        return {**result, "device_id": device_id}

    def health_check(self, device_id: str) -> bool:
        device = self._devices.get(device_id)
        if device is None:
            return False
        try:
            return bool(device["executor"].health_check())
        except Exception:
            return False


_DEFAULT_CONTROLLER = DeviceController()


def get_default_controller() -> DeviceController:
    """Process-wide registry shared by direct registrations and capabilities."""
    return _DEFAULT_CONTROLLER


class DeviceCapability:
    """Capability descriptor and dispatcher; list works without any cloud login."""

    descriptor: dict[str, Any] = {
        "id": "device_control",
        "name": "智能设备控制",
        "persona_description": "我能控制你家里的智能设备，比如关灯、调温度、播放音乐、控制音答",
        "provides": (
            "列出已注册设备、检查连通性，并通过小米云、AppleScript 或 HTTP "
            "执行已注册的命令；未配置时明确说明。"
        ),
        "call": {"name": "device_control", "arguments": {"action": "list_devices"}},
        "costs_money": False,
        "security_sensitive": True,
        "precision": "device_command",
        "freshness": "on_demand_live",
        "implementation": "device_control",
        "available": True,
    }

    def __init__(self, controller: DeviceController | None = None):
        self.controller = controller if controller is not None else get_default_controller()

    def execute(self, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        args = dict(arguments or {})
        if set(args) - {"action", "device_id", "command", "params"}:
            raise ValueError("Unknown device control argument")
        action = args.get("action", "list_devices")
        if action == "list_devices":
            rows = self.controller.list_devices()
            xiaomi = [d for d in rows if d["executor_type"] == "xiaomi"]
            configured = bool(xiaomi) and all(d["configured"] for d in xiaomi)
            return {
                "ok": True,
                "status": "available" if rows else "empty",
                "devices": rows,
                "xiaomi_configured": configured,
                "message": "小米云已配置"
                if configured
                else "小米云未配置；仍可查看设备列表并使用已配置的 AppleScript/HTTP 设备",
            }
        device_id = args.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            return {"ok": False, "error": "device_id_required"}
        if action == "health_check":
            return {
                "ok": True,
                "device_id": device_id,
                "healthy": self.controller.health_check(device_id),
            }
        if action == "execute":
            command = args.get("command")
            if not isinstance(command, str) or not command:
                return {"ok": False, "error": "command_required"}
            return self.controller.execute(device_id, command, args.get("params"))
        return {"ok": False, "error": "unknown_action"}
