"""MCP bridge tests with disposable local stdio servers; no credentials."""

import importlib.util
import json
import sys
import tempfile
import unittest
import os
from pathlib import Path


FIXTURE_SERVER = "\"\"\"Local executable protocol fixture, not a third-party MCP integration.\"\"\"\nimport json\nimport os\nimport sys\nimport time\n\ninitialized = False\n\ndef reply(message):\n    print(json.dumps(message), flush=True)\n\nfor line in sys.stdin:\n    request = json.loads(line)\n    method = request.get('method')\n    request_id = request.get('id')\n    params = request.get('params', {})\n    if method == 'notifications/initialized':\n        initialized = True\n        continue\n    if method == 'initialize':\n        reply({'jsonrpc': '2.0', 'id': request_id, 'result': {\n            'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},\n            'serverInfo': {'name': 'fixture', 'version': '1.0'}}})\n    elif method == 'tools/list':\n        assert initialized, 'client omitted initialized notification'\n        reply({'jsonrpc': '2.0', 'method': 'notifications/message', 'params': {'level': 'info'}})\n        reply({'jsonrpc': '2.0', 'id': request_id, 'result': {'tools': [\n            {'name': 'echo', 'description': 'Return arguments', 'inputSchema': {'type': 'object'}}]}})\n    elif method == 'tools/call':\n        args = params['arguments']\n        if args.get('pause'):\n            time.sleep(args['pause'])\n        if args.get('rpc_error'):\n            reply({'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32602, 'message': 'invalid parameters'}})\n        elif args.get('tool_error'):\n            reply({'jsonrpc': '2.0', 'id': request_id, 'result': {'isError': True, 'content': [{'type': 'text', 'text': 'fixture failure'}]}})\n        else:\n            reply({'jsonrpc': '2.0', 'id': request_id, 'result': {\n                'content': [{'type': 'text', 'text': json.dumps(args)}],\n                'env': os.getenv('MCP_FIXTURE_ENV'), 'pid': os.getpid()}})\n"


def _fixture_server(root):
    path = Path(root) / "fixture_server.py"
    path.write_text(FIXTURE_SERVER, encoding="utf-8")
    return str(path)


EDGE_SERVER = "\"\"\"Exercise protocol edge cases using an actual local subprocess.\"\"\"\nimport json\nimport os\nimport signal\nimport sys\nimport time\n\nmode = sys.argv[1]\nif mode == 'ignore-term':\n    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n\n\ndef reply(message):\n    text = json.dumps(message) + '\\n'\n    if mode == 'split':\n        os.write(1, text[:9].encode())\n        time.sleep(0.005)\n        os.write(1, text[9:].encode())\n    else:\n        print(text, end='', flush=True)\n\n\ninitialized = False\nfor line in sys.stdin:\n    req = json.loads(line)\n    method = req.get('method')\n    rid = req.get('id')\n    if method == 'notifications/initialized':\n        initialized = True\n        continue\n    if method == 'initialize':\n        if mode == 'bad-json':\n            print('not-json', flush=True)\n            continue\n        if mode == 'bad-version':\n            version = 'unsupported'\n        else:\n            version = '2024-11-05'\n        reply({'jsonrpc': '2.0', 'id': rid, 'result': {\n            'protocolVersion': version, 'capabilities': {'tools': {}},\n            'serverInfo': {'name': 'fixture', 'version': '1.0'}}})\n    elif method == 'tools/list':\n        assert initialized\n        if mode == 'stderr':\n            os.write(2, b'e' * 200000)\n        if mode == 'ping':\n            reply({'jsonrpc': '2.0', 'id': 'server-ping', 'method': 'ping'})\n            pong = json.loads(next(sys.stdin))\n            assert pong == {'jsonrpc': '2.0', 'id': 'server-ping', 'result': {}}\n            reply({'jsonrpc': '2.0', 'id': rid - 1, 'result': {'stale': True}})\n        first = not req['params'].get('cursor')\n        tool = {'name': 'echo' if first else 'second', 'inputSchema': {'type': 'object'}}\n        if mode == 'bad-tool':\n            tool['inputSchema'] = []\n        result = {'tools': [tool]}\n        if mode in ('pages', 'cursor-loop') and first:\n            result['nextCursor'] = 'next'\n        if mode == 'cursor-loop' and not first:\n            result['tools'] = []\n            result['nextCursor'] = 'next'\n        if mode == 'read-block':\n            result = {'tools': [tool]}\n        reply({'jsonrpc': '2.0', 'id': rid, 'result': result})\n        if mode == 'read-block':\n            time.sleep(120)\n    elif method == 'tools/call':\n        if mode == 'timeout':\n            time.sleep(120)\n        reply({'jsonrpc': '2.0', 'id': rid, 'result': {'pid': os.getpid(), 'arguments': req['params']['arguments']}})\n        if mode == 'exit':\n            sys.exit(0)\n"


def _edge_server(root):
    path = Path(root) / "edge_server.py"
    path.write_text(EDGE_SERVER, encoding="utf-8")
    return str(path)


class ConfigurationTests(unittest.TestCase):
    def test_server_configuration_round_trip(self):
        self.assertIsNotNone(importlib.util.find_spec("mcp_bridge"), "MCP bridge module must exist")
        from mcp_bridge import MCPBridge

        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as root:
            config = Path(root) / "nested" / "mcp_servers.json"
            bridge = MCPBridge(config)
            self.assertEqual(bridge.list_servers(), [])
            self.assertFalse(config.exists(), "construction must not write config")
            self.assertEqual(
                bridge.add_server("test", "echo", args=["hello"]), {"name": "test", "added": True}
            )
            expected = [{"name": "test", "connected": False, "tools_count": 0}]
            self.assertEqual(bridge.list_servers(), expected)
            self.assertEqual(MCPBridge(config).list_servers(), expected)
            self.assertEqual(
                json.loads(config.read_text())["servers"]["test"],
                {"command": "echo", "args": ["hello"], "env": {}, "transport": "stdio"},
            )
            self.assertEqual(bridge.remove_server("test"), {"name": "test", "removed": True})
            self.assertEqual(bridge.remove_server("test"), {"name": "test", "removed": False})
            self.assertEqual(MCPBridge(config).list_servers(), [])


class LifecycleTests(unittest.TestCase):
    def test_real_stdio_discovery_call_and_shutdown(self):
        from mcp_bridge import MCPBridge
        import os

        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as root:
            bridge = MCPBridge(Path(root) / "servers.json")
            bridge.add_server(
                "fixture",
                sys.executable,
                args=["-B", _fixture_server(root)],
                env={"MCP_FIXTURE_ENV": "verified"},
            )
            self.assertTrue(callable(getattr(bridge, "connect", None)), "connect must exist")
            try:
                connected = bridge.connect("fixture")
                self.assertTrue(connected["connected"], connected)
                self.assertEqual(connected["error"], "")
                self.assertEqual(connected["tools"][0]["name"], "echo")
                self.assertEqual(
                    bridge.list_servers(),
                    [{"name": "fixture", "connected": True, "tools_count": 1}],
                )
                self.assertEqual(bridge.list_tools("fixture"), connected["tools"])
                result = bridge.call_tool("fixture", "echo", {"message": "中文"})
                self.assertEqual(result["error"], "", result)
                self.assertEqual(result["result"]["env"], "verified")
                self.assertEqual(
                    json.loads(result["result"]["content"][0]["text"]), {"message": "中文"}
                )
                pid = result["result"]["pid"]
                self.assertEqual(bridge.connect("fixture")["tools"], connected["tools"])
                self.assertEqual(
                    bridge.disconnect("fixture"), {"name": "fixture", "disconnected": True}
                )
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
                self.assertEqual(bridge.list_tools("fixture"), [])
                self.assertFalse(bridge.list_servers()[0]["connected"])
            finally:
                bridge.disconnect_all()

    def test_replace_and_remove_disconnect_live_servers(self):
        from mcp_bridge import MCPBridge
        import os

        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as root:
            bridge = MCPBridge(Path(root) / "servers.json")
            args = ["-B", _fixture_server(root)]
            try:
                for operation in ("replace", "remove"):
                    bridge.add_server("fixture", sys.executable, args=args)
                    self.assertTrue(bridge.connect("fixture")["connected"])
                    pid = bridge.call_tool("fixture", "echo", {})["result"]["pid"]
                    if operation == "replace":
                        bridge.add_server("fixture", "echo", args=["replacement"])
                    else:
                        bridge.remove_server("fixture")
                    self.assertEqual(bridge.list_tools("fixture"), [])
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
            finally:
                bridge.disconnect_all()

    def test_errors_and_tools_are_detached(self):
        from mcp_bridge import MCPBridge

        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as root:
            bridge = MCPBridge(Path(root) / "servers.json")
            self.assertFalse(bridge.connect("missing")["connected"])
            self.assertTrue(bridge.call_tool("missing", "echo", {})["error"])
            self.assertFalse(bridge.disconnect("missing")["disconnected"])
            bridge.add_server("remote", "unused", transport="sse")
            self.assertIn("stdio", bridge.connect("remote")["error"])
            bridge.add_server("missing-command", "/definitely/nonexistent/mcp-command")
            self.assertTrue(bridge.connect("missing-command")["error"])
            bridge.add_server("invalid", "echo", args=["not-json"])
            self.assertFalse(bridge.connect("invalid")["connected"])
            bridge.add_server("fixture", sys.executable, args=["-B", _fixture_server(root)])
            try:
                connected = bridge.connect("fixture")
                self.assertTrue(connected["connected"])
                connected["tools"][0]["name"] = "mutated"
                tools = bridge.list_tools("fixture")
                self.assertEqual(tools[0]["name"], "echo")
                tools[0]["inputSchema"]["type"] = "bad"
                self.assertEqual(bridge.list_tools("fixture")[0]["inputSchema"]["type"], "object")
                self.assertIn("Unknown", bridge.call_tool("fixture", "unknown", {})["error"])
                self.assertTrue(bridge.call_tool("fixture", "echo", [])["error"])
                self.assertTrue(
                    bridge.call_tool("fixture", "echo", {"not-json": object()})["error"]
                )
                self.assertIn(
                    "invalid parameters",
                    bridge.call_tool("fixture", "echo", {"rpc_error": True})["error"],
                )
                failed = bridge.call_tool("fixture", "echo", {"tool_error": True})
                self.assertTrue(failed["error"])
                self.assertTrue(failed["result"]["isError"])
                self.assertEqual(bridge.call_tool("fixture", "echo", {})["error"], "")
            finally:
                bridge.disconnect_all()


class EdgeCases(unittest.TestCase):
    def setUp(self):
        from mcp_bridge import MCPBridge

        self.root = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.bridge = MCPBridge(Path(self.root.name) / "servers.json")
        self.addCleanup(self.root.cleanup)
        self.addCleanup(self.bridge.disconnect_all)

    def connect_mode(self, mode):
        self.bridge.add_server(
            mode, sys.executable, args=["-B", _edge_server(self.root.name), mode]
        )
        return self.bridge.connect(mode)

    def test_pagination_split_io_ping_and_stderr(self):
        for mode in ("pages", "split", "ping", "stderr"):
            with self.subTest(mode=mode):
                connected = self.connect_mode(mode)
                self.assertTrue(connected["connected"], connected)
                self.assertEqual(len(connected["tools"]), 2 if mode == "pages" else 1)
                self.assertEqual(connected["tools"][0]["description"], "")
                self.assertFalse(self.bridge.call_tool(mode, "echo", {})["error"])
        self.assertEqual(len(self.bridge.disconnect_all()["servers"]), 4)
        self.assertTrue(all(not row["connected"] for row in self.bridge.list_servers()))

    def test_bad_protocol_responses_reject_and_cleanup(self):
        for mode in ("bad-json", "bad-version", "cursor-loop", "bad-tool"):
            with self.subTest(mode=mode):
                connected = self.connect_mode(mode)
                self.assertFalse(connected["connected"], connected)
                self.assertTrue(connected["error"])
                self.assertEqual(self.bridge.list_tools(mode), [])
        self.assertEqual(self.bridge.disconnect_all()["servers"], [])

    def test_kill_escalation(self):
        import os
        import time

        self.assertTrue(self.connect_mode("ignore-term")["connected"])
        pid = self.bridge.call_tool("ignore-term", "echo", {})["result"]["pid"]
        start = time.monotonic()
        self.assertTrue(self.bridge.disconnect("ignore-term")["disconnected"])
        self.assertLess(time.monotonic() - start, 3)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_process_exit_updates_status(self):
        import time

        self.assertTrue(self.connect_mode("exit")["connected"])
        self.assertFalse(self.bridge.call_tool("exit", "echo", {})["error"])
        deadline = time.monotonic() + 1
        while self.bridge.list_servers()[0]["connected"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.bridge.list_servers()[0]["connected"])
        self.assertEqual(self.bridge.list_tools("exit"), [])

    def test_config_validation_preserves_corrupt_original(self):
        self.bridge.config_path.write_text("{bad-json")
        with self.assertRaises(ValueError):
            self.bridge.add_server("valid", "echo")
        self.assertEqual(self.bridge.config_path.read_text(), "{bad-json")
        self.bridge.config_path.unlink()
        for kwargs in (
            {"name": "", "command": "echo"},
            {"name": "bad", "command": ""},
            {"name": "bad", "command": "echo", "args": "wrong"},
            {"name": "bad", "command": "echo", "env": {"key": 1}},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.bridge.add_server(**kwargs)
        self.assertFalse(self.bridge.config_path.exists())
        self.bridge.add_server("valid", "echo")
        self.assertEqual(self.bridge.config_path.stat().st_mode & 0o777, 0o600)

    def test_read_timeout_real_30_seconds(self):
        import time

        self.assertTrue(self.connect_mode("timeout")["connected"])
        start = time.monotonic()
        result = self.bridge.call_tool("timeout", "echo", {})
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 29)
        self.assertLess(elapsed, 34)
        self.assertIn("timed out", result["error"])
        self.assertIn("not retried", result["error"])
        self.assertFalse(self.bridge.list_servers()[0]["connected"])
        print(f"READ_TIMEOUT elapsed={elapsed:.3f}s error={result['error']}")

    def test_write_backpressure_timeout_real_30_seconds(self):
        import time

        self.assertTrue(self.connect_mode("read-block")["connected"])
        start = time.monotonic()
        result = self.bridge.call_tool("read-block", "echo", {"large": "x" * 1_000_000})
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 29)
        self.assertLess(elapsed, 34)
        self.assertIn("timed out", result["error"])
        self.assertFalse(self.bridge.list_servers()[0]["connected"])
        print(f"WRITE_TIMEOUT elapsed={elapsed:.3f}s error={result['error']}")


class SelfEvaluationTests(unittest.TestCase):
    def test_module_hook_is_offline_and_describes_real_limits(self):
        import builtins
        from contextlib import ExitStack
        from datetime import UTC, datetime, timedelta
        from unittest.mock import patch
        import mcp_bridge as module

        hook = getattr(module, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-level self_evaluate is required")
        before = datetime.now(UTC)
        # No construction, data/config inspection, process spawn or directory probe.
        with ExitStack() as stack:
            for target in (
                "builtins.open",
                "pathlib.Path.open",
                "pathlib.Path.read_text",
                "pathlib.Path.read_bytes",
                "os.open",
                "os.stat",
                "os.listdir",
                "os.scandir",
                "subprocess.Popen",
                "subprocess.run",
            ):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            stack.enter_context(
                patch.object(module, "MCPBridge", side_effect=AssertionError("constructor"))
            )
            report = hook()
            another = hook()
        self.assertEqual(
            set(report),
            {
                "module",
                "domain",
                "current_version",
                "known_limitations",
                "improvement_directions",
                "last_evaluated_at",
            },
        )
        self.assertEqual(report["module"], "mcp_bridge")
        for name in ("domain", "current_version"):
            self.assertIsInstance(report[name], str)
            self.assertTrue(report[name].strip())
        for name in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[name], list)
            self.assertTrue(report[name])
            self.assertTrue(all(isinstance(s, str) and s.strip() for s in report[name]))
        stamp = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.utcoffset(), timedelta(0))
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, datetime.now(UTC))
        report["known_limitations"].append("caller mutation")
        self.assertNotIn("caller mutation", another["known_limitations"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
