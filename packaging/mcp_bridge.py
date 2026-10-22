"""Synchronous MCP stdio client for explicitly configured local servers.

This module is standalone and has no import-time process or filesystem effects.
Configured commands execute with the current user's permissions, not in a sandbox.
Only stdio is supported; remote transports and OAuth are deliberately out of scope.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, cast


def self_evaluate() -> dict[str, Any]:
    """Describe protocol boundaries without reading config or connecting servers."""
    from datetime import UTC, datetime

    return {
        "module": "mcp_bridge",
        "domain": "local MCP stdio JSON-RPC server configuration discovery and tool calls",
        "current_version": "serialized stdio client for MCP 2024-11-05 with pagination and no side-effect retries",
        "known_limitations": [
            "Only stdio transport and protocol version 2024-11-05 are supported; remote transports and OAuth are absent.",
            "Configured servers inherit user permissions and environment; the bridge is not a sandbox or credential broker.",
            "Configuration replacement is atomic but separate processes have no shared writer lock; one bridge serializes all calls.",
            "A timed-out tool may already have acted; closing the connection cannot verify or roll back that action.",
        ],
        "improvement_directions": [
            "Add explicit protocol negotiation and separately authorized remote transport support.",
            "Coordinate cross-process configuration writes and expose durable unknown-outcome receipts without automatic retries.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


def _object(value: Any, error: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(error)
    return cast(dict[str, Any], value)


class _StdioConnection:
    """One serialized JSON-lines stream; nonblocking I/O bounds writes too."""

    def __init__(self, command: str, args: list[str], env: dict[str, str]):
        self.process = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, **env},
            bufsize=0,
            start_new_session=True,
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        self.writer = self.process.stdin.fileno()
        self.reader = self.process.stdout.fileno()
        os.set_blocking(self.writer, False)
        os.set_blocking(self.reader, False)
        self.buffer = bytearray()
        self.next_id = 0
        self.tools: list[dict[str, Any]] = []

    def _send(self, message: dict[str, Any], deadline: float) -> None:
        payload = memoryview(
            (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        )
        while payload:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.writer], [], remaining)[1]:
                raise TimeoutError("MCP request timed out")
            try:
                count = os.write(self.writer, payload)
            except BlockingIOError:
                continue
            if count == 0:
                raise ConnectionError("MCP server closed stdin")
            payload = payload[count:]

    def notify(self, method: str, deadline: float) -> None:
        self._send({"jsonrpc": "2.0", "method": method}, deadline)

    def request(self, method: str, params: dict[str, Any], deadline: float) -> Any:
        self.next_id += 1
        request_id = self.next_id
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            deadline,
        )
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("MCP request timed out")
            if b"\n" not in self.buffer:
                remaining = max(0, deadline - time.monotonic())
                if not select.select([self.reader], [], [], remaining)[0]:
                    raise TimeoutError("MCP request timed out")
                try:
                    chunk = os.read(self.reader, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    raise ConnectionError("MCP server closed stdout")
                self.buffer.extend(chunk)
                if len(self.buffer) > 16 * 1024 * 1024:
                    raise ValueError("MCP response exceeds 16 MiB limit")
                continue
            line, _, remainder = self.buffer.partition(b"\n")
            self.buffer = remainder
            if not line.strip():
                continue
            message = _object(json.loads(line), "Invalid MCP JSON-RPC message")
            if message.get("jsonrpc") != "2.0":
                raise ValueError("Invalid MCP JSON-RPC message")
            if "method" in message:
                if "id" in message:
                    response: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
                    if message["method"] == "ping":
                        response["result"] = {}
                    else:
                        response["error"] = {"code": -32601, "message": "Method not supported"}
                    self._send(response, deadline)
                continue  # Notifications do not consume a response ID.
            if type(message.get("id")) is not int or message["id"] != request_id:
                continue
            if "error" in message:
                error = message["error"]
                if isinstance(error, dict):
                    error = cast(dict[str, Any], error)
                    raise _RPCError(f"{error.get('code', '')}: {error.get('message', 'MCP error')}")
                raise _RPCError(str(error))
            if "result" not in message:
                raise ValueError("MCP response has neither result nor error")
            return message["result"]

    def close(self) -> None:
        # Own a fresh process group so child helpers cannot retain the pipes.
        # macOS can deny group signals for a system binary that already exited.
        def send(sig: signal.Signals) -> None:
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                if self.process.poll() is None:
                    self.process.send_signal(sig)

        try:
            send(signal.SIGTERM)
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        finally:
            try:
                send(signal.SIGKILL)
                self.process.wait()
            finally:
                for stream in (self.process.stdin, self.process.stdout):
                    if stream is not None:
                        stream.close()


class _RPCError(RuntimeError):
    """Server-reported JSON-RPC error; the transport may remain usable."""


class MCPBridge:
    """Persist server definitions and manage their MCP connections.

    Configuration writes are atomic and private (0600). Each operation reloads
    configuration, so edits by another bridge are visible. Calls on one bridge
    are serialized; concurrent writers in separate processes need coordination.
    """

    def __init__(self, config_path: Path | None = None):
        self.config_path = (
            Path(config_path).expanduser()
            if config_path is not None
            else Path.home() / ".jarvis" / "mcp_servers.json"
        )
        self._lock = threading.RLock()
        self._connections: dict[str, _StdioConnection] = {}

    def _load_config(self) -> dict[str, Any]:
        try:
            with self.config_path.open(encoding="utf-8") as stream:
                config = _object(json.load(stream), "MCP configuration must be an object")
        except FileNotFoundError:
            return {"servers": {}}
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ValueError(f"Invalid MCP configuration: {exc}") from exc
        servers = _object(config.get("servers"), "MCP configuration must contain a servers object")
        for name, value in servers.items():
            server = _object(value, f"Invalid MCP server definition: {name}")
            self._validate_server(
                name,
                server.get("command"),
                server.get("args", []),
                server.get("env", {}),
                server.get("transport", "stdio"),
            )
        return config

    @staticmethod
    def _validate_server(
        name: Any,
        command: Any,
        args: Any,
        env: Any,
        transport: Any,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Server name must be a nonempty string")
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise ValueError("Server command must be a nonempty string without NUL")
        if not isinstance(args, list) or any(
            not isinstance(arg, str) or "\0" in arg for arg in cast(list[Any], args)
        ):
            raise ValueError("Server args must be a list of strings without NUL")
        if not isinstance(env, dict) or any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(value, str)
            or "\0" in value
            for key, value in cast(dict[Any, Any], env).items()
        ):
            raise ValueError("Server env must map valid environment names to strings")
        if not isinstance(transport, str) or not transport:
            raise ValueError("Server transport must be a nonempty string")

    def _save_config(self, config: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.config_path.parent,
                prefix=f".{self.config_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(config, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _is_connected(self, name: str) -> bool:
        connection = self._connections.get(name)
        if connection is None:
            return False
        if connection.process.poll() is None:
            return True
        self.disconnect(name)
        return False

    def list_servers(self) -> list[dict[str, Any]]:
        """Return configured servers, connection state and discovered counts."""
        with self._lock:
            result: list[dict[str, Any]] = []
            for name in self._load_config()["servers"]:
                connected = self._is_connected(name)
                result.append(
                    {
                        "name": name,
                        "connected": connected,
                        "tools_count": len(self._connections[name].tools) if connected else 0,
                    }
                )
            return result

    def connect(self, name: str) -> dict[str, Any]:
        """Initialize a local server and cache all pages of its tool catalog."""
        with self._lock:
            connection = None
            try:
                config = self._load_config()["servers"].get(name)
                if config is None:
                    raise ValueError(f"Unknown MCP server: {name}")
                if config.get("transport", "stdio") != "stdio":
                    raise ValueError("Unsupported MCP transport; only stdio is supported")
                if self._is_connected(name):
                    return {
                        "name": name,
                        "connected": True,
                        "tools": deepcopy(self._connections[name].tools),
                        "error": "",
                    }
                connection = _StdioConnection(
                    config["command"],
                    config.get("args", []),
                    config.get("env", {}),
                )
                deadline = time.monotonic() + 30.0
                initialized = connection.request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "JARVIS", "version": "1.0"},
                    },
                    deadline,
                )
                initialized = _object(initialized, "Invalid MCP initialize response")
                if initialized.get("protocolVersion") != "2024-11-05":
                    raise ValueError("Unsupported MCP protocol version")
                connection.notify("notifications/initialized", deadline)
                cursor = None
                cursors: set[str] = set()
                tools: list[dict[str, Any]] = []
                tool_names: set[str] = set()
                while True:
                    params = {} if cursor is None else {"cursor": cursor}
                    page = connection.request("tools/list", params, deadline)
                    page = _object(page, "Invalid MCP tools/list response")
                    if not isinstance(page.get("tools"), list):
                        raise ValueError("Invalid MCP tools/list response")
                    for value in page["tools"]:
                        tool = _object(value, "Invalid MCP tool definition")
                        if (
                            not isinstance(tool.get("name"), str)
                            or not tool["name"]
                            or not isinstance(tool.get("description", ""), str)
                            or not isinstance(tool.get("inputSchema"), dict)
                        ):
                            raise ValueError("Invalid MCP tool definition")
                        if tool["name"] in tool_names:
                            raise ValueError("Duplicate MCP tool name")
                        tool_names.add(tool["name"])
                        tools.append(
                            {
                                "name": tool["name"],
                                "description": tool.get("description", ""),
                                "inputSchema": deepcopy(tool["inputSchema"]),
                            }
                        )
                    cursor = page.get("nextCursor")
                    if cursor is None:
                        break
                    if not isinstance(cursor, str) or cursor in cursors:
                        raise ValueError("Invalid or repeated MCP tools/list cursor")
                    cursors.add(cursor)
                connection.tools = tools
                self._connections[name] = connection
                return {"name": name, "connected": True, "tools": deepcopy(tools), "error": ""}
            except (OSError, ValueError, RuntimeError, TypeError) as exc:
                if connection is not None:
                    connection.close()
                return {"name": name, "connected": False, "tools": [], "error": str(exc)}

    def list_tools(self, name: str) -> list[dict[str, Any]]:
        """Return a detached copy of the connected server's discovered tools."""
        with self._lock:
            if not self._is_connected(name):
                return []
            return deepcopy(self._connections[name].tools)

    def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Call once, with a 30-second I/O deadline; never retry side effects.

        A timeout may mean the server already performed the action. The bridge
        closes that connection, reports an unknown outcome, and never resends.
        """
        with self._lock:
            if not self._is_connected(server_name):
                return {"result": None, "error": f"MCP server is not connected: {server_name}"}
            connection = self._connections[server_name]
            if not isinstance(cast(Any, arguments), dict):
                return {"result": None, "error": "Tool arguments must be an object"}
            if not any(tool["name"] == tool_name for tool in connection.tools):
                return {"result": None, "error": f"Unknown MCP tool: {tool_name}"}
            try:
                params = {"name": tool_name, "arguments": arguments}
                # Validate before sending so malformed arguments leave the stream usable.
                json.dumps(params, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (ValueError, TypeError, UnicodeError) as exc:
                return {"result": None, "error": f"Invalid tool arguments: {exc}"}
            try:
                result = connection.request("tools/call", params, time.monotonic() + 30.0)
                if isinstance(result, dict) and cast(dict[str, Any], result).get("isError"):
                    return {"result": result, "error": "MCP tool reported an execution error"}
                return {"result": result, "error": ""}
            except _RPCError as exc:
                return {"result": None, "error": str(exc)}
            except (OSError, ValueError, RuntimeError, TypeError) as exc:
                self.disconnect(server_name)
                return {
                    "result": None,
                    "error": f"{exc}; tool outcome may be unknown; request was not retried",
                }

    def disconnect(self, name: str) -> dict[str, Any]:
        """Terminate and reap the server process and its process group."""
        with self._lock:
            connection = self._connections.pop(name, None)
            if connection is not None:
                connection.close()
            return {"name": name, "disconnected": connection is not None}

    def disconnect_all(self) -> dict[str, Any]:
        """Close every live connection, retaining all server definitions."""
        with self._lock:
            names = list(self._connections)
            for name in names:
                self.disconnect(name)
            return {"disconnected": True, "servers": names}

    def add_server(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        transport: str = "stdio",
    ) -> dict[str, Any]:
        """Add or replace a definition; never start a process implicitly."""
        args = [] if args is None else args
        env = {} if env is None else env
        self._validate_server(name, command, args, env, transport)
        with self._lock:
            config = self._load_config()
            config["servers"][name] = {
                "command": command,
                "args": list(args),
                "env": dict(env),
                "transport": transport,
            }
            self._save_config(config)
            self.disconnect(name)
            return {"name": name, "added": True}

    def remove_server(self, name: str) -> dict[str, Any]:
        """Remove a definition, returning false when it was absent."""
        with self._lock:
            config = self._load_config()
            removed = name in config["servers"]
            if removed:
                del config["servers"][name]
                self._save_config(config)
                self.disconnect(name)
            return {"name": name, "removed": removed}
