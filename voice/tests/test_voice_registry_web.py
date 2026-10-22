"""Voice admission and Web assembly checks; no credentials or resident starts."""

from __future__ import annotations

import ast
import http.client
import inspect
import json
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
import voice_gateway
import web_server as web
from integration_registry import REGISTRY
from test_chat_launcher import config

VOICE_MODULES = {
    "voice_dashscope",
    "voice_gateway",
    "voice_ledger",
    "voice_live",
    "voice_port",
    "voice_speaker",
    "voice_stream_tts",
    "voice_volcengine",
    "voice_xiaoai",
}
VOICE_ROOT = Path(__file__).resolve().parent.parent
PACKAGING_ROOT = VOICE_ROOT.parent / "packaging"


def _source_of(identity: str) -> Path:
    """语音模块已隔离到 voice/；device_routing 与 voice_xiaoai 仍留在 packaging/。"""
    for root in (VOICE_ROOT, PACKAGING_ROOT):
        candidate = root / (identity + ".py")
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(identity)


def test_voice_modules_have_explicit_owned_registrations() -> None:
    expected = {
        "device_routing": "test_device_routing.py",
        "voice_dashscope": "test_voice_dashscope.py",
        "voice_gateway": "test_voice_gateway.py",
        "voice_ledger": "test_voice_ledger.py",
        "voice_xiaoai": "test_voice_xiaoai.py",
    }
    registered = {component.component_id for component in REGISTRY.list()}
    assert set(expected) <= registered
    for identity, test in expected.items():
        component = REGISTRY.get(identity)
        # 注册表里语音模块记 "voice/x.py"，仍留在 packaging/ 的记裸名。
        expected_path = f"voice/{identity}.py" if identity in VOICE_MODULES else f"{identity}.py"
        assert component.source_paths == (expected_path,), identity
        # 语音模块的测试路径带 voice/tests/ 前缀。
        expected_test = f"voice/tests/{test}" if identity in VOICE_MODULES else test
        assert expected_test in component.wiring.tests, identity
        assert not component.core
        assert component.pending
        assert "runtime_wiring" in component.wiring.pending_fields
        assert component.wiring.self_evaluate == REGISTRY._has_self_evaluate(component)  # pyright: ignore[reportPrivateUsage]
        test_file = Path(__file__).parent / test
        assert test_file.is_file() or (PACKAGING_ROOT / test).is_file(), test


def test_voice_dependencies_are_real_imports_not_admission_shortcuts() -> None:
    for identity in (
        "device_routing",
        "voice_dashscope",
        "voice_gateway",
        "voice_ledger",
        "voice_xiaoai",
    ):
        source = _source_of(identity).read_text()
        tree = ast.parse(source)
        imports = set[str]()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        assert set(REGISTRY.get(identity).wiring.reads_from) <= imports
    assert "voice_gateway" in REGISTRY.get("web_server").wiring.reads_from
    assert "voice_ledger" in REGISTRY.get("web_server").wiring.reads_from


@pytest.fixture
def web_app() -> Iterator[web.DevServer]:
    # Existing product root guards reject Hermes-owned scratch trees.
    with tempfile.TemporaryDirectory(dir=Path.home(), prefix=".jarvis-voice-registry-") as folder:
        root = web.launcher.prepare_root(Path(folder) / "data")
        server = web.DevServer(0, config(8123), "FAKE-test", root, 8123)
        try:
            yield server
        finally:
            server.server_close()


def test_default_voice_status_reader_uses_web_data_root(web_app: web.DevServer) -> None:
    observed = {
        "gateway_running": True,
        "bailian_key_valid": False,
        "last_call_at": "2026-10-20T10:00:00+00:00",
        "api_key": "PRIVATE_DO_NOT_EXPOSE",
    }
    with (
        patch.object(
            voice_gateway, "read_gateway_status", return_value=observed, create=True
        ) as read,
        patch.object(web.launcher, "read_key", side_effect=AssertionError("no credential read")),
    ):
        assert callable(web_app.voice_status_reader)
        result = web_app.voice_status_reader()
        read.assert_called_once_with(web_app.root)
        assert isinstance(result, dict)
        assert result["gateway_running"] is True


def test_web_default_does_not_take_gateway_or_outreach_port() -> None:
    assert web.DEFAULT_WEB_PORT == 8767
    assert inspect.signature(web.create_server).parameters["port"].default == web.DEFAULT_WEB_PORT


@pytest.mark.parametrize("argument, expected", [(None, 8767), (0, 0), (8765, 8765)])
def test_cli_uses_default_without_rewriting_explicit_ports(
    argument: int | None, expected: int
) -> None:
    argv = ["web_server.py", "--config", "/not-opened.json", "--key-file", "/not-opened.key"]
    if argument is not None:
        argv.extend(["--port", str(argument)])
    with (
        patch.object(sys, "argv", argv),
        patch.object(web, "create_server", side_effect=KeyboardInterrupt) as create,
    ):
        assert web.main() == 0
        assert create.call_args.kwargs["port"] == expected


def test_explicit_web_port_and_origin_remain_unchanged(web_app: web.DevServer) -> None:
    # Explicit 0 continues to mean ephemeral; it is never rewritten to 8767.
    assert web_app.server_port > 0
    assert web_app.origin == f"http://127.0.0.1:{web_app.server_port}"
    # Reuse a kernel-selected free port rather than assuming the next port is free.
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    explicit = web.DevServer(port, config(8123), "FAKE-test", web_app.root, 8123)
    try:
        assert explicit.server_port == port
        assert explicit.origin == f"http://127.0.0.1:{explicit.server_port}"
    finally:
        explicit.server_close()


def _request_status(server: web.DevServer) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request("GET", "/api/voice/status", headers={"Origin": server.origin})
        response = connection.getresponse()
        return response.status, cast(dict[str, Any], json.loads(response.read()))
    finally:
        connection.close()


def test_default_status_http_allowlists_reader_and_rereads_each_request(
    web_app: web.DevServer,
) -> None:
    worker = threading.Thread(target=web_app.serve_forever, daemon=True)
    worker.start()
    observed = {
        "gateway_running": True,
        "bailian_key_valid": None,
        "last_call_at": None,
        "pid": 12345,
        "api_key": "PRIVATE_DO_NOT_EXPOSE",
    }
    try:
        with patch.object(
            voice_gateway, "read_gateway_status", return_value=observed, create=True
        ) as read:
            status, result = _request_status(web_app)
            assert status == 200
            assert result == {
                "gateway_running": True,
                "bailian_key_valid": None,
                "last_call_at": None,
                "status_source": "runtime_snapshot",
                "ledger_available": True,
            }
            observed["gateway_running"] = False
            assert _request_status(web_app)[1]["gateway_running"] is False
            assert read.call_args_list == [((web_app.root,),), ((web_app.root,),)]
    finally:
        web_app.stop(timeout=3)
        worker.join(3)


def test_default_reader_failure_is_explicit_unavailability(web_app: web.DevServer) -> None:
    worker = threading.Thread(target=web_app.serve_forever, daemon=True)
    worker.start()
    try:
        with patch.object(
            voice_gateway,
            "read_gateway_status",
            side_effect=PermissionError("private"),
            create=True,
        ):
            assert _request_status(web_app) == (503, {"error": "voice_subsystem_unavailable"})
    finally:
        web_app.stop(timeout=3)
        worker.join(3)


def test_live_reader_matches_snapshot_for_same_root(web_app: web.DevServer) -> None:
    """Use the parent's real private-file reader when that writer has landed."""
    reader = getattr(voice_gateway, "read_gateway_status", None)
    if reader is None:
        pytest.skip("Parent gateway status reader has not landed yet")
    expected = reader(web_app.root)
    result = web_app.voice_status()
    for name in ("gateway_running", "bailian_key_valid", "last_call_at"):
        assert result[name] == expected.get(name)
