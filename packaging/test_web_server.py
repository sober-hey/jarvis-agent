"""Loopback HTTP integration; providers are explicit FAKE HTTP only."""

from __future__ import annotations

import http.client
import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from evidence_support import evidence_directory

if TYPE_CHECKING:
    import web_server as web_types

ENTRY = Path(__file__).with_name("web_server.py")
spec = importlib.util.spec_from_file_location("web_under_test", ENTRY)
web: Any = None
if spec is not None and spec.loader is not None and ENTRY.exists():
    web = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(web)
if TYPE_CHECKING:
    web = web_types
EVIDENCE = evidence_directory("web-server")


class WebTests(unittest.TestCase):
    def test_chat_prompt_budget_is_utf8_inclusive_before_dispatch(self) -> None:
        from context_limits import CHAT_CONTEXT_BYTE_LIMIT
        from test_chat_launcher import config

        with tempfile.TemporaryDirectory(dir=Path.home(), prefix=".jarvis-web-budget-") as folder:
            root = web.launcher.prepare_root(Path(folder) / "data")
            server = web.DevServer(0, config(8000), "FAKE-test", root, 8000)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with patch.object(server, "chat_request", return_value=(200, {"ok": True})) as send:
                    for index, character in enumerate(("x", "界", "😀"), 1):
                        width = len(character.encode("utf-8"))
                        exact = character * (CHAT_CONTEXT_BYTE_LIMIT // width)
                        exact += "x" * (CHAT_CONTEXT_BYTE_LIMIT % width)
                        for suffix, expected in (("", 200), ("x", 400)):
                            with self.subTest(character=repr(character), overflow=bool(suffix)):
                                connection = http.client.HTTPConnection(
                                    "127.0.0.1", server.server_port, timeout=10
                                )
                                try:
                                    connection.request(
                                        "POST",
                                        "/api/chat",
                                        json.dumps(
                                            {
                                                "prompt": exact + suffix,
                                                "attempt_id": "65a70552-1e70-4c43-82a0-520ab8cf10d2",
                                            },
                                            ensure_ascii=False,
                                        ).encode("utf-8"),
                                        {
                                            "Origin": server.origin,
                                            "Content-Type": "application/json",
                                        },
                                    )
                                    response = connection.getresponse()
                                    response.read()
                                    self.assertEqual(response.status, expected)
                                finally:
                                    connection.close()
                        self.assertEqual(send.call_count, index)
            finally:
                server.stop(timeout=3)
                worker.join(3)

    def test_timeline_endpoint_reuses_identity_after_server_restart(self) -> None:
        from test_chat_launcher import config

        with tempfile.TemporaryDirectory(dir=Path.home(), prefix=".jarvis-timeline-") as folder:
            root = web.launcher.prepare_root(Path(folder) / "data")
            server = web.DevServer(0, config(8000), "FAKE-test", root, 8000)
            first = getattr(server, "timeline_id", None)
            server.server_close()
            restarted = web.DevServer(0, config(8000), "FAKE-test", root, 8000)
            try:
                self.assertIsInstance(first, str, "server must own a durable timeline identity")
                self.assertEqual(first, getattr(restarted, "timeline_id", None))
            finally:
                restarted.server_close()

    def test_real_chat_is_persisted_and_same_identity_does_not_resend(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        requests: list[dict[str, Any]] = []
        entered, release = threading.Event(), threading.Event()

        class Provider(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                if requests[-1]["messages"][-1]["content"] == "delayed":
                    entered.set()
                    release.wait(15)
                payload = json.dumps(
                    {
                        "id": "fake",
                        "model": "fake-test-model",
                        "object": "chat.completion",
                        "created": 1,
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "FAKE_HTTP_OK"},
                            }
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        worker = threading.Thread(target=provider.serve_forever, daemon=True)
        worker.start()
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".jarvis-web-config-test-", dir=Path.home()
        ) as folder:
            config = Path(folder) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "model": "fake-test-model",
                        "provider": "custom",
                        "base_url": f"http://127.0.0.1:{provider.server_port}/v1",
                        "api_mode": "chat_completions",
                        "context_length": 256000,
                        "reasoning_effort": "high",
                    }
                )
            )
            key = Path(folder) / "key"
            key.write_text("FAKE-NONSECRET-TEST")
            key.chmod(0o600)
            config.chmod(0o600)
            root = web.launcher.prepare_root(Path(folder) / "data")
            server = web.create_server(
                config_file=config,
                key_file=key,
                data_dir=root,
                test_local_port=provider.server_port,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"

            def call(method: str, path: str, body: object = None) -> tuple[int, Any]:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
                conn.request(
                    method,
                    path,
                    json.dumps(body) if body is not None else None,
                    {"Origin": origin, "Content-Type": "application/json"},
                )
                response = conn.getresponse()
                status, value = response.status, json.loads(response.read())
                conn.close()
                return status, value

            try:
                payload = {
                    "prompt": "普通消息",
                    "attempt_id": "e1371101-5613-4b6a-8d7c-859660e73274",
                }
                with patch.object(web.launcher, "launch", wraps=web.launcher.launch) as launch:
                    status, first = call("POST", "/api/chat", payload)
                self.assertEqual(
                    launch.call_args.kwargs["expected_sha256"], server.route_binding.config_sha256
                )
                self.assertEqual(
                    launch.call_args.kwargs["expected_binding_sha256"],
                    server.route_binding.binding_sha256,
                )
                self.assertEqual(status, 200)
                self.assertEqual(first["text"], "FAKE_HTTP_OK")
                self.assertEqual(
                    set(first), {"text", "session_id", "attempt_id", "generation_degraded"}
                )
                record = web.launcher.query_attempt(root, payload["attempt_id"])
                summary = web.launcher.binding_summary(
                    web.launcher.product.bind_route(
                        server.config, root, test_port=provider.server_port
                    )
                )
                self.assertTrue(all(record.get(name) == value for name, value in summary.items()))
                self.assertEqual(call("POST", "/api/chat", payload), (200, first))
                self.assertEqual(
                    call("GET", "/api/attempts/" + payload["attempt_id"]),
                    (200, {"state": "completed", "result": first}),
                )
                self.assertEqual(
                    call("POST", "/api/chat", {**payload, "prompt": "changed"})[0], 409
                )
                self.assertEqual(len(requests), 1)
                # Corrupt any binding field in a completed receipt: still reject,
                # with no new subprocess or provider side effect.
                receipt = web.launcher.attempt_path(root, payload["attempt_id"])
                for name in summary:
                    web.launcher.atomic_json(receipt, {**record, name: "tampered"})
                    with patch.object(web.launcher.subprocess, "Popen") as popen:
                        self.assertEqual(call("POST", "/api/chat", payload)[0], 409)
                        popen.assert_not_called()
                web.launcher.atomic_json(receipt, record)
                import sqlite3

                database = root / "engine-home/state.db"
                self.assertTrue(database.is_file())
                self.assertTrue((root / "memory.db").is_file())
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                    rows = db.execute(
                        "SELECT role,content FROM messages WHERE session_id=? ORDER BY id",
                        (first["session_id"],),
                    ).fetchall()
                self.assertEqual(rows, [("user", "普通消息"), ("assistant", "FAKE_HTTP_OK")])
                delayed = {
                    "prompt": "delayed",
                    "attempt_id": "78bfbbcf-6c3e-4f07-9ca9-72127d4041fe",
                    "session_id": first["session_id"],
                }
                lost = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
                lost.request(
                    "POST",
                    "/api/chat",
                    json.dumps(delayed),
                    {"Origin": origin, "Content-Type": "application/json"},
                )
                self.assertTrue(entered.wait(12), "real launcher must reach fake provider")
                lost.close()
                self.assertEqual(call("GET", "/api/health")[0], 200)
                self.assertEqual(call("POST", "/api/chat", delayed)[1]["state"], "started")
                self.assertEqual(
                    call("GET", "/api/attempts/" + delayed["attempt_id"])[1]["state"], "started"
                )
                release.set()
                with server.condition:
                    self.assertTrue(
                        server.condition.wait_for(lambda: server.inflight == 0, timeout=12)
                    )
                finished = call("GET", "/api/attempts/" + delayed["attempt_id"])[1]
                self.assertEqual(finished["state"], "completed")
                self.assertEqual(call("POST", "/api/chat", delayed), (200, finished["result"]))
                self.assertEqual(len(requests), 2)
                self.assertEqual([len(request["messages"]) for request in requests], [2, 4])
                self.assertEqual(
                    [[message["role"] for message in request["messages"]] for request in requests],
                    [["system", "user"], ["system", "user", "assistant", "user"]],
                )
                for request in requests:
                    self.assertIn("Sir", request["messages"][0]["content"])
                # Isolated fault injection only: launcher exception must be suppressed
                # and the binding must continue to forbid retransmission.
                fault = {"prompt": "fault", "attempt_id": "547f1f60-b36b-4d59-bfc5-5e1819a20d61"}
                with patch.object(
                    web.launcher, "launch", side_effect=RuntimeError("secret-provider-diagnostic")
                ):
                    self.assertEqual(call("POST", "/api/chat", fault), (202, {"state": "unknown"}))
                self.assertEqual(call("POST", "/api/chat", fault), (202, {"state": "unknown"}))
                self.assertEqual(len(requests), 2)
                # Real provider stays blocked while stop reaches its bounded deadline.
                entered.clear()
                release.clear()
                stopping = {**delayed, "attempt_id": "074c511f-a8a4-418a-a210-eb98947295cc"}
                waiting = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
                waiting.request(
                    "POST",
                    "/api/chat",
                    json.dumps(stopping),
                    {"Origin": origin, "Content-Type": "application/json"},
                )
                self.assertTrue(entered.wait(12))
                # Semaphore saturation rejects a new ID before any durable binding.
                self.assertTrue(server.slots.acquire(blocking=False))
                busy = {"prompt": "busy", "attempt_id": "d7bc65d1-4d04-4099-b8ea-a402e997b8f5"}
                self.assertEqual(call("POST", "/api/chat", busy), (503, {"error": "busy"}))
                self.assertFalse((server.bindings / (busy["attempt_id"] + ".json")).exists())
                server.slots.release()
                self.assertFalse(server.stop(timeout=0.01))
                self.assertEqual(server.view(stopping["attempt_id"]), {"state": "unknown"})
                release.set()
                with server.condition:
                    self.assertTrue(
                        server.condition.wait_for(lambda: server.inflight == 0, timeout=12)
                    )
                waiting.close()
                self.assertEqual(server.view(stopping["attempt_id"])["state"], "completed")
                # Restart on the same root must replay the durable original result.
                server = web.create_server(
                    config_file=config,
                    key_file=key,
                    data_dir=root,
                    test_local_port=provider.server_port,
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                origin = f"http://127.0.0.1:{server.server_port}"
                self.assertEqual(call("POST", "/api/chat", payload), (200, first))
                self.assertEqual(
                    call("POST", "/api/chat", {**payload, "session_id": first["session_id"]})[0],
                    409,
                )
                saved_model = server.config["model"]
                server.config["model"] = "different-model"
                with patch.object(web.launcher.subprocess, "Popen") as popen:
                    self.assertEqual(call("POST", "/api/chat", payload)[0], 409)
                    self.assertEqual(
                        call(
                            "POST",
                            "/api/chat",
                            {**payload, "attempt_id": "1f39ec84-7daf-4e0a-8e41-c1744738b6e0"},
                        )[0],
                        409,
                    )
                    popen.assert_not_called()
                server.config["model"] = saved_model
                with patch.object(web.launcher.subprocess, "Popen") as popen:
                    saved_root = server.root
                    server.root = Path(folder) / "changed-data"
                    server.root.mkdir(mode=0o700)
                    self.assertEqual(call("POST", "/api/chat", payload)[0], 409)
                    server.root = saved_root
                    popen.assert_not_called()
                for item in root.rglob("*"):
                    if item.is_file():
                        self.assertNotIn(b"FAKE-NONSECRET-TEST", item.read_bytes())
                self.assertEqual(len(requests), 3)
                (EVIDENCE / "integration.json").write_text(
                    json.dumps(
                        {
                            "provider": "explicit fake HTTP only",
                            "provider_request_count": len(requests),
                            "session_message_counts": [len(r["messages"]) for r in requests],
                            "sqlite_messages_verified": rows,
                            "disconnect_result": finished,
                            "real_launcher_and_sessiondb": True,
                            "fault_injection": "one explicit launcher exception",
                        },
                        indent=2,
                    )
                )
            finally:
                release.set()
                server.stop(timeout=3)
                thread.join(2)
                provider.shutdown()
                provider.server_close()
                worker.join(2)
                import shutil

                shutil.rmtree(root)

    def test_startup_credentials_and_routes_are_server_owned(self) -> None:
        import os

        EVIDENCE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".jarvis-web-config-test-", dir=Path.home()
        ) as folder:
            config = Path(folder) / "config.json"
            route = {
                "model": "fake-test-model",
                "provider": "custom",
                "base_url": "http://127.0.0.1:8123/v1",
                "api_mode": "chat_completions",
                "context_length": 256000,
                "reasoning_effort": "high",
            }
            config.write_text(json.dumps(route))
            key = Path(folder) / "key"
            key.write_text("FAKE-NONSECRET-TEST")
            key.chmod(0o600)
            config.chmod(0o600)
            root = web.launcher.prepare_root(Path(folder) / "data")
            try:
                with self.assertRaises(web.launcher.LauncherError):
                    web.create_server(config_file=config, key_file=key, data_dir=root)
                key.chmod(0o644)
                with self.assertRaises(web.launcher.LauncherError):
                    web.create_server(
                        config_file=config, key_file=key, data_dir=root, test_local_port=8123
                    )
                key.chmod(0o600)
                key.write_text("NONFAKE-NONSECRET-TEST")
                with self.assertRaises(ValueError):
                    web.create_server(
                        config_file=config, key_file=key, data_dir=root, test_local_port=8123
                    )
                config.write_text(json.dumps({**route, "api_key": "disallowed"}))
                with self.assertRaises(web.launcher.LauncherError):
                    web.create_server(
                        config_file=config, key_file=key, data_dir=root, test_local_port=8123
                    )
                config.write_text(json.dumps(route))
                read, write = os.pipe()
                os.write(write, b"FAKE-NONSECRET-TEST")
                os.close(write)
                try:
                    server = web.create_server(
                        config_file=config, key_fd=read, data_dir=root, test_local_port=8123
                    )
                    server.server_close()
                finally:
                    os.close(read)
                # Exercise the real command-line service and SIGTERM cleanup.
                import subprocess

                key.write_text("FAKE-NONSECRET-TEST")
                process = subprocess.Popen(
                    [
                        str(ENTRY.parents[1] / ".venv/bin/python"),
                        "-B",
                        str(ENTRY),
                        "--config",
                        str(config),
                        "--key-file",
                        str(key),
                        "--data-dir",
                        str(root),
                        "--test-local-port",
                        "8123",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    self.assertIsNotNone(process.stdout)
                    assert process.stdout is not None
                    banner = json.loads(process.stdout.readline())
                    self.assertTrue(banner["url"].startswith("http://127.0.0.1:"))
                    process.terminate()
                    out, err = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, err)
                    self.assertNotIn("FAKE-NONSECRET-TEST", out + err)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)
            finally:
                import shutil

                shutil.rmtree(root)

    def test_health_requires_exact_origin_host_and_static_is_allowlisted(self) -> None:
        self.assertIsNotNone(web, "web backend must exist")
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".jarvis-web-config-test-", dir=Path.home()
        ) as folder:
            config = Path(folder) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "model": "fake-test-model",
                        "provider": "custom",
                        "base_url": "http://127.0.0.1:8123/v1",
                        "api_mode": "chat_completions",
                        "context_length": 256000,
                        "reasoning_effort": "high",
                    }
                )
            )
            key = Path(folder) / "key"
            key.write_text("FAKE-NONSECRET-TEST")
            key.chmod(0o600)
            config.chmod(0o600)
            root = web.launcher.prepare_root(Path(folder) / "data")
            server = web.create_server(
                config_file=config, key_file=key, data_dir=root, test_local_port=8123
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                origin = f"http://127.0.0.1:{server.server_port}"
                conn.request("GET", "/api/health", headers={"Origin": origin})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["status"], "ok")
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
                conn.close()

                def request(
                    method: str,
                    path: str,
                    headers: dict[str, str] | None = None,
                    body: str | bytes | None = None,
                ) -> tuple[int, bytes]:
                    c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    c.request(method, path, body, headers or {})
                    r = c.getresponse()
                    result = r.status, r.read()
                    c.close()
                    return result

                # Browser same-origin GET normally omits Origin. Read-only local
                # clients may also omit Fetch Metadata; neither is public auth.
                for headers in ({}, {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}):
                    for path in (
                        "/api/health",
                        "/api/attempts/e1371101-5613-4b6a-8d7c-859660e73274",
                    ):
                        self.assertEqual(request("GET", path, headers)[0], 200)
                for headers in (
                    {"Sec-Fetch-Site": "cross-site"},
                    {"Sec-Fetch-Site": "same-site"},
                    {"Sec-Fetch-Site": "invalid"},
                    {"Origin": "http://evil.example"},
                    {"Host": "evil.example"},
                    {"Origin": origin, "Host": "evil.example"},
                    {"Origin": origin.replace("127.0.0.1", "localhost")},
                    {"Host": f"localhost:{server.server_port}"},
                    {"Origin": origin + "/"},
                    {"Origin": origin, "Sec-Fetch-Site": "cross-site"},
                    {"Origin": origin, "Sec-Fetch-Site": "same-site"},
                ):
                    for path in (
                        "/api/health",
                        "/api/attempts/e1371101-5613-4b6a-8d7c-859660e73274",
                        "/",
                        "/app.js",
                    ):
                        self.assertEqual(request("GET", path, headers)[0], 403)
                    self.assertEqual(request("POST", "/api/chat", headers, "{}")[0], 403)
                self.assertEqual(request("POST", "/api/chat", {}, "{}")[0], 403)
                for method in ("GET", "POST"):
                    for name, value in (
                        ("Host", server.authority),
                        ("Origin", origin),
                        ("Sec-Fetch-Site", "same-origin"),
                    ):
                        c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                        c.putrequest(
                            method,
                            "/api/health" if method == "GET" else "/api/chat",
                            skip_host=True,
                        )
                        for header, content in (
                            ("Host", server.authority),
                            ("Origin", origin),
                            ("Sec-Fetch-Site", "same-origin"),
                        ):
                            c.putheader(header, content)
                        c.putheader(name, value)
                        c.putheader("Content-Length", "0")
                        c.endheaders()
                        r = c.getresponse()
                        self.assertEqual(r.status, 403)
                        r.read()
                        c.close()
                for path in ("/", "/index.html", "/styles.css", "/app.js"):
                    self.assertEqual(request("GET", path)[0], 200)
                for path in (
                    "/../AGENTS.md",
                    "/%2e%2e/AGENTS.md",
                    "/verify_web.mjs",
                    "/app.js?x=1",
                ):
                    self.assertEqual(request("GET", path)[0], 404)
                self.assertEqual(
                    request("OPTIONS", "/api/chat", {"Origin": "http://evil.example"})[0], 403
                )
                api = {"Origin": origin, "Content-Type": "application/json"}
                self.assertEqual(request("POST", "/api/chat", {"Origin": origin}, "{}")[0], 415)
                for length in ("-1", "abc", "65537", "999999"):
                    self.assertIn(
                        request("POST", "/api/chat", {**api, "Content-Length": length})[0],
                        (400, 413),
                    )
                for body in (
                    "[]",
                    '{"prompt":"p","attempt_id":"invalid"}',
                    '{"prompt":"p","attempt_id":"e1371101-5613-4b6a-8d7c-859660e73274","api_key":"bad"}',
                    '{"prompt":"p","prompt":"q","attempt_id":"e1371101-5613-4b6a-8d7c-859660e73274"}',
                    '{"prompt":"p","attempt_id":"e1371101-5613-4b6a-8d7c-859660e73274","session_id":"../../x"}',
                ):
                    self.assertEqual(request("POST", "/api/chat", api, body)[0], 400)
                self.assertEqual(
                    json.loads(
                        request("GET", "/api/attempts/e1371101-5613-4b6a-8d7c-859660e73274", api)[1]
                    ),
                    {"state": "unknown"},
                )
                self.assertEqual(server.inflight, 0)
                self.assertEqual(list(server.bindings.iterdir()), [])
                for host in ("0.0.0.0", "localhost", "::1"):
                    with self.assertRaises(ValueError):
                        web.create_server(
                            config_file=config,
                            key_file=key,
                            data_dir=root,
                            host=host,
                            test_local_port=8123,
                        )
                with self.assertRaises(OSError):
                    web.create_server(
                        config_file=config,
                        key_file=key,
                        data_dir=root,
                        port=server.server_port,
                        test_local_port=8123,
                    )
            finally:
                server.stop(timeout=2)
                thread.join(2)
                import shutil

                shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
