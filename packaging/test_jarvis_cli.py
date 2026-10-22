"""CLI configuration contracts using real product validators and disposable roots."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import selectors
import signal
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import product_config as config
import product_home as home
from evidence_support import evidence_directory

if TYPE_CHECKING:
    from . import jarvis
else:
    import jarvis

EVIDENCE = evidence_directory("jarvis-cli")


class ConfigCliTests(unittest.TestCase):
    def setUp(self) -> None:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=EVIDENCE)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.root = self.folder / "data"
        self.root.mkdir(mode=0o700)
        self.path = self.folder / "config.json"

    def invoke(self, *args: str) -> tuple[int, dict[str, object], str]:
        output, error = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
            patch.object(home, "PROJECT", self.folder / "other-source"),
        ):
            try:
                code = jarvis.main(list(args))
            except SystemExit as exc:
                code = int(exc.code or 0)
        text = output.getvalue() + error.getvalue()
        try:
            value: object = json.loads(text)
        except json.JSONDecodeError:
            value = {}
        self.assertIsInstance(value, dict)
        return code, value, text  # type: ignore[return-value]

    def init_args(self, *extra: str) -> list[str]:
        return [
            "config",
            "init",
            "--config",
            str(self.path),
            "--data-dir",
            str(self.root),
            "--model",
            "example-model",
            "--provider",
            "custom",
            "--base-url",
            "https://example.invalid/v1",
            "--api-mode",
            "chat_completions",
            "--context-length",
            "256000",
            "--reasoning-effort",
            "high",
            *extra,
        ]

    def test_init_and_check_binding(self) -> None:
        code, result, _ = self.invoke(*self.init_args())
        self.assertEqual(code, 0, result)
        snapshot = config.read_config(self.path)
        self.assertEqual(set(snapshot.values), config.CONFIG_FIELDS)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        code, checked, _ = self.invoke(
            "config", "check", "--config", str(self.path), "--data-dir", str(self.root)
        )
        self.assertEqual(code, 0, checked)
        self.assertEqual(result, checked)
        self.assertEqual(checked["config_sha256"], snapshot.sha256)
        self.assertEqual(
            checked["binding_sha256"], config.bind_route(snapshot.values, self.root).binding_sha256
        )

    def test_malformed_config_types_have_fixed_error(self) -> None:
        self.assertEqual(self.invoke(*self.init_args())[0], 0)
        values = dict(config.read_config(self.path).values)
        for field in ("provider", "reasoning_effort"):
            broken = {**values, field: ["FAKE-sensitive"]}
            self.path.write_text(json.dumps(broken))
            code, value, text = self.invoke(
                "config", "check", "--config", str(self.path), "--data-dir", str(self.root)
            )
            self.assertEqual((code, value), (2, {"ok": False}))
            self.assertNotIn("FAKE-sensitive", text)

    def test_lock_links_and_permissions_rejected_before_mutation(self) -> None:
        victim = self.folder / "victim"
        victim.write_text("FAKE-victim")
        victim.chmod(0o600)
        lock = self.folder / ".config.json.lock"
        os.link(victim, lock)
        self.assertEqual(self.invoke(*self.init_args())[0], 2)
        self.assertEqual(victim.read_text(), "FAKE-victim")
        lock.unlink()
        lock.symlink_to(victim)
        self.assertEqual(self.invoke(*self.init_args())[0], 2)
        lock.unlink()
        lock.write_text("")
        lock.chmod(0o644)
        self.assertEqual(self.invoke(*self.init_args())[0], 2)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o644)

    def test_overwrite_requires_exact_expected_sha(self) -> None:
        self.assertEqual(self.invoke(*self.init_args())[0], 0)
        before = self.path.read_bytes()
        for extra in ([], ["--expected-sha256", "0" * 64]):
            self.assertEqual(self.invoke(*self.init_args(*extra))[0], 2)
            self.assertEqual(before, self.path.read_bytes())
        digest = config.read_config(self.path).sha256
        self.assertEqual(self.invoke(*self.init_args("--expected-sha256", digest))[0], 0)

    def test_unknown_secret_and_abbreviated_flags_are_suppressed(self) -> None:
        for flag in ("--api-key", "--secret", "--unknown", "--mod"):
            code, value, text = self.invoke(*self.init_args(flag, "FAKE-secret-do-not-echo"))
            self.assertEqual((code, value), (2, {"ok": False}))
            self.assertNotIn("FAKE-secret", text)
            self.assertFalse(self.path.exists())

    def test_missing_check_no_creation_network_or_process(self) -> None:
        self.path = self.folder / "absent/config.json"
        before = sorted(str(p) for p in self.folder.rglob("*"))
        with (
            patch("socket.socket", side_effect=AssertionError("network")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("process")),
        ):
            self.assertEqual(
                self.invoke(
                    "config", "check", "--config", str(self.path), "--data-dir", str(self.root)
                )[0],
                2,
            )
        self.assertEqual(before, sorted(str(p) for p in self.folder.rglob("*")))

    def test_binding_changes_with_config_and_root(self) -> None:
        first = self.invoke(*self.init_args())[1]
        args = self.init_args("--expected-sha256", str(first["config_sha256"]))
        args[args.index("example-model")] = "changed-model"
        changed = self.invoke(*args)[1]
        self.assertNotEqual(first["binding_sha256"], changed["binding_sha256"])
        other = self.folder / "other-data"
        other.mkdir(mode=0o700)
        moved = self.invoke(
            "config", "check", "--config", str(self.path), "--data-dir", str(other)
        )[1]
        self.assertNotEqual(changed["binding_sha256"], moved["binding_sha256"])

    def test_config_and_root_permissions_links_and_host_paths(self) -> None:
        self.assertEqual(self.invoke(*self.init_args())[0], 0)
        for mode in (0o644, 0o666):
            self.path.chmod(mode)
            self.assertEqual(
                self.invoke(
                    "config", "check", "--config", str(self.path), "--data-dir", str(self.root)
                )[0],
                2,
            )
        self.path.chmod(0o600)
        alias = self.folder / "alias"
        alias.symlink_to(self.path)
        self.assertEqual(
            self.invoke("config", "check", "--config", str(alias), "--data-dir", str(self.root))[0],
            2,
        )
        alias.unlink()
        os.link(self.path, alias)
        self.assertEqual(self.invoke(*self.init_args())[0], 2)
        alias.unlink()
        self.root.chmod(0o755)
        self.assertEqual(self.invoke(*self.init_args())[0], 2)
        self.root.chmod(0o700)
        for bad in (Path("relative"), Path.home() / ".hermes/config.json"):
            self.assertEqual(
                self.invoke("config", "check", "--config", str(bad), "--data-dir", str(self.root))[
                    0
                ],
                2,
            )
        values = dict(config.read_config(self.path).values)
        values["api_key"] = "FAKE-secret-no-output"
        self.path.write_text(json.dumps(values))
        code, _, text = self.invoke(
            "config", "check", "--config", str(self.path), "--data-dir", str(self.root)
        )
        self.assertEqual(code, 2)
        self.assertNotIn("FAKE-secret", text)

    def test_key_check_permissions_length_links_owner_and_no_echo(self) -> None:
        key = self.folder / "test.key"
        key.write_text("FAKE-dedicated-secret\n")
        key.chmod(0o600)
        args = ["config", "key-check", "--key-file", str(key)]
        self.assertEqual(self.invoke(*args)[:2], (0, {"ok": True, "key_file": str(key)}))
        with patch.object(config.os, "getuid", return_value=os.getuid() + 1):
            self.assertEqual(self.invoke(*args)[0], 2)
        key.chmod(0o644)
        self.assertEqual(self.invoke(*args)[0], 2)
        key.chmod(0o600)
        alias = self.folder / "key-alias"
        os.link(key, alias)
        self.assertEqual(self.invoke(*args)[0], 2)
        alias.unlink()
        alias.symlink_to(key)
        self.assertEqual(self.invoke("config", "key-check", "--key-file", str(alias))[0], 2)
        for value in ("short", "FAKE-line\nsecond", "x" * 4097):
            key.write_text(value)
            code, _, text = self.invoke(*args)
            self.assertEqual(code, 2)
            self.assertNotIn(value, text)

    def test_loopback_requires_explicit_test_port(self) -> None:
        args = self.init_args()
        for endpoint in ("https://127.0.0.1/v1", "https://localhost/v1", "https://[::1]/v1"):
            args[args.index("https://example.invalid/v1")] = endpoint
            self.assertEqual(self.invoke(*args)[0], 2)
            args[args.index(endpoint)] = "https://example.invalid/v1"
        args[args.index("https://example.invalid/v1")] = "http://127.0.0.1:8123/v1"
        self.assertEqual(self.invoke(*args)[0], 2)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.invoke(*args, "--test-port", "8123")[0], 0)
        self.assertEqual(
            self.invoke(
                "config", "check", "--config", str(self.path), "--data-dir", str(self.root)
            )[0],
            2,
        )

    def test_default_home_not_created_and_legacy_version_preserved(self) -> None:
        default = self.folder / "user"
        default.mkdir(mode=0o700)
        with patch.object(Path, "home", return_value=default):
            self.assertEqual(self.invoke("config", "init")[0], 2)
            self.assertFalse((default / ".jarvis").exists())
        code, value, _ = self.invoke("version", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(value["version"], jarvis.VERSION)


class ChatCliTests(unittest.TestCase):
    def setUp(self) -> None:
        # The real launcher rejects source-tree and host-Hermes data roots.
        self.temp = tempfile.TemporaryDirectory(
            dir=jarvis.PROJECT.parent, prefix=".jarvis-cli-test-"
        )
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.root = self.folder / "data"
        self.root.mkdir(mode=0o700)
        self.path = self.folder / "config.json"
        self.key = self.folder / "fake.key"
        self.key.write_text("FAKE-CLI-DEDICATED-KEY\n")
        self.key.chmod(0o600)
        self.requests: list[dict[str, Any]] = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        requests, entered, release = self.requests, self.entered, self.release

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                entered.set()
                release.wait(90)
                payload = json.dumps(
                    {
                        "id": "fake-cli-response",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "fake-cli-model",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "CLI_CHAT_OK"},
                            }
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.snapshot = config.write_config(
            self.path,
            {
                "model": "fake-cli-model",
                "provider": "custom",
                "base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
                "api_mode": "chat_completions",
                "context_length": 256000,
                "reasoning_effort": "high",
            },
            test_port=self.server.server_port,
        )

    def stop_server(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def run_cli(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(jarvis.PROJECT / ".venv/bin/python"), "-B", str(Path(jarvis.__file__)), *args],
            capture_output=True,
            text=True,
            timeout=120,
            **kwargs,
        )

    def chat_args(self, *extra: str) -> list[str]:
        return [
            "chat",
            "--config",
            str(self.path),
            "--data-dir",
            str(self.root),
            "--prompt",
            "你好 Sir",
            "--test-port",
            str(self.server.server_port),
            *extra,
        ]

    def test_fd_session_query_replay_and_expected_conflicts(self) -> None:
        first = self.run_cli(
            *self.chat_args("--key-fd", "0", "--attempt-id", "fd-zero"),
            input="FAKE-CLI-DEDICATED-KEY",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        initial = json.loads(first.stdout)
        read, write = os.pipe()
        os.write(write, b"FAKE-CLI-DEDICATED-KEY")
        os.close(write)
        try:
            second = self.run_cli(
                *self.chat_args(
                    "--key-fd",
                    str(read),
                    "--attempt-id",
                    "fd-extra",
                    "--session",
                    initial["session_id"],
                ),
                pass_fds=(read,),
            )
        finally:
            os.close(read)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["session_id"], initial["session_id"])
        third = self.run_cli(*self.chat_args("--key-file", str(self.key)))
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(json.loads(third.stdout)["session_id"], initial["session_id"])
        for attempt in ("fd-zero", "fd-extra"):
            query = self.run_cli(
                "query-attempt", "--data-dir", str(self.root), "--attempt-id", attempt
            )
            self.assertEqual(query.returncode, 0, query.stderr)
            self.assertEqual(json.loads(query.stdout)["state"], "completed")
        for extra in (
            ["--attempt-id", "fd-zero"],
            ["--expected-sha256", "0" * 64, "--attempt-id", "config-conflict"],
            ["--expected-binding-sha256", "0" * 64, "--attempt-id", "binding-conflict"],
        ):
            result = self.run_cli(*self.chat_args("--key-file", str(self.key), *extra))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertEqual(json.loads(result.stderr), {"ok": False})
        self.assertEqual([len(r["messages"]) for r in self.requests], [2, 4, 6])
        for index, request in enumerate(self.requests):
            self.assertEqual(
                [message["role"] for message in request["messages"]],
                ["system"] + ["user", "assistant"] * index + ["user"],
            )
            self.assertIn("Sir", request["messages"][0]["content"])
        self.assertTrue(all("tools" not in request for request in self.requests))
        for attempt in ("config-conflict", "binding-conflict"):
            self.assertFalse((self.root / "launcher-attempts" / (attempt + ".json")).exists())
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"FAKE-CLI-DEDICATED-KEY", path.read_bytes())
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / "integration.json").write_text(
            json.dumps(
                {
                    "mode": "fake HTTP only",
                    "first": initial,
                    "message_counts": [len(r["messages"]) for r in self.requests],
                    "key_fd_zero_and_extra": True,
                    "saved_session_reused": True,
                    "replay_and_expected_conflicts_rejected": True,
                    "key_absent_from_data": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    def test_errors_never_echo_and_no_default_root(self) -> None:
        defaults = self.folder / "unused-default"
        for args in (
            ["chat"],
            self.chat_args("--key-file", str(self.key), "--api-key", "FAKE-argv-secret"),
            self.chat_args("--key-file", str(self.key), "--key-fd", "0"),
            self.chat_args("--key-fd", "-1"),
            ["query-attempt", "--attempt-id", "missing"],
        ):
            with patch.dict(os.environ, {"JARVIS_HOME": str(defaults)}):
                result = self.run_cli(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stderr), {"ok": False})
            self.assertNotIn("FAKE-argv-secret", result.stdout + result.stderr)
            self.assertFalse(defaults.exists())
        self.path.chmod(0o644)
        result = self.run_cli(*self.chat_args("--key-file", str(self.key)))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr), {"ok": False})
        self.assertEqual(self.requests, [])
        self.assertFalse((self.root / "engine-home").exists())

    def test_transport_group_environment_argv_and_exit_code(self) -> None:
        with (
            patch.object(jarvis.subprocess, "Popen") as spawn,
            patch.dict(os.environ, {"OPENAI_API_KEY": "FAKE-env-secret", "PYTHONPATH": "/bad"}),
            contextlib.redirect_stderr(io.StringIO()) as error,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            child = spawn.return_value
            child.communicate.return_value = ('{"ok":true}', None)
            child.returncode = 7
            self.assertEqual(jarvis.main(self.chat_args("--key-fd", "19")), 7)
            self.assertEqual(json.loads(error.getvalue()), {"ok": False})
            self.assertEqual(output.getvalue(), "")
            command = spawn.call_args.args[0]
            options = spawn.call_args.kwargs
            self.assertTrue(options["start_new_session"])
            self.assertEqual(options["pass_fds"], (19,))
            self.assertIn("--test-local-port", command)
            self.assertNotIn("FAKE-env-secret", json.dumps(command) + json.dumps(options["env"]))
            self.assertNotIn("PYTHONPATH", options["env"])
            child.communicate.return_value = ('{"ok":true}', None)
            child.returncode = 0
            self.assertEqual(jarvis.main(self.chat_args("--key-file", str(self.key))), 0)
            self.assertEqual(output.getvalue(), '{"ok":true}')

    def test_real_cli_chat_key_file_reaches_sessiondb(self) -> None:
        result = self.run_cli(*self.chat_args("--key-file", str(self.key), "--attempt-id", "first"))
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["text"], "CLI_CHAT_OK")
        self.assertEqual(value["attempt_id"], "first")
        self.assertEqual(len(self.requests), 1)
        with sqlite3.connect(self.root / "engine-home/state.db") as db:
            rows = db.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
        self.assertEqual(rows, [("user", "你好 Sir"), ("assistant", "CLI_CHAT_OK")])
        self.assertNotIn("FAKE-CLI-DEDICATED-KEY", result.stdout + result.stderr)

    def web_args(self, *extra: str) -> list[str]:
        return [
            "web",
            "--config",
            str(self.path),
            "--data-dir",
            str(self.root),
            "--test-port",
            str(self.server.server_port),
            *extra,
        ]

    def start_web(self, *extra: str, **kwargs: Any) -> tuple[subprocess.Popen[str], dict[str, Any]]:
        process = subprocess.Popen(
            [
                str(jarvis.PROJECT / ".venv/bin/python"),
                "-B",
                str(Path(jarvis.__file__)),
                *self.web_args(*extra),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            **kwargs,
        )

        def cleanup() -> None:
            self.release.set()
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

        self.addCleanup(cleanup)
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=90), "startup URL must stream before exit")
            line = process.stdout.readline()
        self.assertTrue(line, "web command must emit a startup URL")
        banner = json.loads(line)
        self.assertRegex(banner["url"], r"^http://127\.0\.0\.1:[1-9][0-9]{0,4}$")
        self.assertIsNone(process.poll(), "web must stay in the foreground")
        return process, banner

    def web_call(self, origin: str, method: str, path: str, body: object = None) -> tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", int(origin.rsplit(":", 1)[1]), timeout=90)
        try:
            conn.request(
                method,
                path,
                json.dumps(body) if body is not None else None,
                {"Origin": origin, "Content-Type": "application/json"},
            )
            response = conn.getresponse()
            blob = response.read()
            value = json.loads(blob) if path.startswith("/api/") else blob.decode()
            return response.status, value
        finally:
            conn.close()

    def test_web_real_cli_streams_url_chat_sqlite_replay_and_shutdown(self) -> None:
        process, banner = self.start_web("--key-file", str(self.key))
        origin = banner["url"]
        self.assertEqual(self.web_call(origin, "GET", "/api/health")[1]["status"], "ok")
        status, page = self.web_call(origin, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("JARVIS", page)
        payload = {"prompt": "你好 Sir", "attempt_id": str(uuid.uuid4())}
        status, first = self.web_call(origin, "POST", "/api/chat", payload)
        self.assertEqual(status, 200)
        self.assertEqual(first["text"], "CLI_CHAT_OK")
        self.assertEqual(self.web_call(origin, "POST", "/api/chat", payload), (200, first))
        self.assertEqual(len(self.requests), 1)
        with sqlite3.connect(self.root / "engine-home/state.db") as db:
            rows = db.execute("SELECT role,content FROM messages ORDER BY id").fetchall()
        self.assertEqual(rows, [("user", "你好 Sir"), ("assistant", "CLI_CHAT_OK")])
        # A durable unknown binding is never automatically sent again.
        unknown = {"prompt": payload["prompt"], "attempt_id": str(uuid.uuid4())}
        binding = self.root / "web-bindings" / (unknown["attempt_id"] + ".json")
        original = self.root / "web-bindings" / (payload["attempt_id"] + ".json")
        binding.write_bytes(original.read_bytes())
        binding.chmod(0o600)
        self.assertEqual(
            self.web_call(origin, "GET", "/api/attempts/" + unknown["attempt_id"]),
            (200, {"state": "unknown"}),
        )
        self.assertEqual(
            self.web_call(origin, "POST", "/api/chat", unknown), (202, {"state": "unknown"})
        )
        self.assertEqual(len(self.requests), 1)
        process.terminate()
        out, err = process.communicate(timeout=90)
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(out + err, "")
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", int(origin.rsplit(":", 1)[1]))), 0)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"FAKE-CLI-DEDICATED-KEY", path.read_bytes())
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        with (EVIDENCE / "unified-web-integration.json").open("x") as stream:
            json.dump(
                {
                    "banner": banner,
                    "result": first,
                    "sqlite_messages": rows,
                    "provider_requests": len(self.requests),
                    "shutdown_exit": process.returncode,
                },
                stream,
                ensure_ascii=False,
                indent=2,
            )

    def test_web_inherited_fd_and_stdin_fd_shutdown(self) -> None:
        for use_stdin, signum in ((False, signal.SIGINT), (True, signal.SIGTERM)):
            with self.subTest(stdin=use_stdin):
                read, write = os.pipe()
                os.write(write, b"FAKE-CLI-DEDICATED-KEY")
                os.close(write)
                try:
                    process, banner = self.start_web(
                        "--key-fd",
                        "0" if use_stdin else str(read),
                        **({"stdin": read} if use_stdin else {"pass_fds": (read,)}),
                    )
                finally:
                    os.close(read)
                self.assertEqual(self.web_call(banner["url"], "GET", "/api/health")[0], 200)
                process.send_signal(signum)
                out, err = process.communicate(timeout=90)
                self.assertEqual(process.returncode, 0, err)
                self.assertEqual(out + err, "")
        self.assertEqual(self.requests, [])

    def test_web_sigterm_drains_active_engine_and_preserves_attempt(self) -> None:
        self.release.clear()
        process, banner = self.start_web("--key-file", str(self.key), "--port", "0")
        payload = {"prompt": "pending", "attempt_id": str(uuid.uuid4())}
        result: list[tuple[int, Any]] = []
        errors: list[Exception] = []

        def admitted_request() -> None:
            try:
                result.append(self.web_call(banner["url"], "POST", "/api/chat", payload))
            except Exception as exc:
                errors.append(exc)

        request = threading.Thread(target=admitted_request, daemon=True)
        request.start()
        self.assertTrue(self.entered.wait(90))
        # Observe the independent owned engine while the provider is blocked.
        table = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,pgid=,command="],
            capture_output=True,
            text=True,
            timeout=90,
        ).stdout
        engine_ids = [
            int(line.split(None, 3)[0])
            for line in table.splitlines()
            if str(self.root) in line and "--credentials-fd" in line
        ]
        self.assertTrue(engine_ids, "real isolated engine must be running")
        process.terminate()
        time.sleep(0.3)
        self.assertIsNone(process.poll(), "shutdown should allow an admitted request to settle")
        self.release.set()
        out, err = process.communicate(timeout=90)
        request.join(timeout=90)
        self.assertFalse(request.is_alive())
        self.assertEqual(errors, [], "admitted response must finish before shutdown")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], 200)
        self.assertEqual(result[0][1]["text"], "CLI_CHAT_OK")
        self.assertEqual(result[0][1]["attempt_id"], payload["attempt_id"])
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(out + err, "")
        record = json.loads(
            (self.root / "launcher-attempts" / (payload["attempt_id"] + ".json")).read_text()
        )
        self.assertEqual(record["state"], "completed")
        self.assertEqual(len(self.requests), 1)
        for pid in engine_ids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        with socket.socket() as probe:
            self.assertNotEqual(
                probe.connect_ex(("127.0.0.1", int(banner["url"].rsplit(":", 1)[1]))), 0
            )

    def test_web_stdout_guard_and_owned_group_timeout(self) -> None:
        real_spawn = subprocess.Popen
        banner = {"url": "http://127.0.0.1:12345", "scope": "loopback development only"}
        cases = (
            (json.dumps(banner) + "\n", 0, False, True),
            (json.dumps(banner) + "\nFAKE-untrusted-secret\n", 0, False, True),
            ("FAKE-untrusted-secret\n", 0, False, False),
            (
                '{"url":"http://0.0.0.0:1234","scope":"loopback development only"}\n',
                0,
                False,
                False,
            ),
            (json.dumps({**banner, "extra": "FAKE-untrusted-secret"}) + "\n", 0, False, False),
            ("x" * 5000, 0, False, False),
            ("\xff", 0, False, False),
            (json.dumps(banner) + '\n{"state":"unknown","retry_allowed":false}\n', 2, False, True),
            (json.dumps(banner) + "\n", 7, False, True),
            ("FAKE-untrusted-secret\n", 0, True, False),
        )
        sentinel = real_spawn(["/bin/sleep", "90"], start_new_session=True)
        try:
            for text, exit_code, ignore_stop, expect_banner in cases:
                spawned: list[subprocess.Popen[bytes]] = []

                def spawn(
                    command: list[str],
                    *,
                    text: str = text,
                    exit_code: int = exit_code,
                    ignore_stop: bool = ignore_stop,
                    spawned: list[subprocess.Popen[bytes]] = spawned,
                    **options: Any,
                ) -> subprocess.Popen[bytes]:
                    self.assertEqual(Path(command[3]).name, "web_server.py")
                    self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
                    self.assertEqual(command[command.index("--port") + 1], "65535")
                    self.assertEqual(options["pass_fds"], (19,))
                    self.assertTrue(options["start_new_session"])
                    self.assertNotIn("FAKE-env-secret", json.dumps(options["env"]))
                    self.assertNotIn("PYTHONPATH", options["env"])
                    stub = (
                        "import os,signal,time; "
                        + ("signal.signal(signal.SIGINT,signal.SIG_IGN); " if ignore_stop else "")
                        + f"os.write(1,{text.encode()!r}); "
                        + ("time.sleep(90)" if ignore_stop else f"raise SystemExit({exit_code})")
                    )
                    child = real_spawn(
                        [str(jarvis.PROJECT / ".venv/bin/python"), "-c", stub],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    spawned.append(child)
                    return child

                output, error = io.StringIO(), io.StringIO()
                with (
                    patch.object(jarvis.subprocess, "Popen", side_effect=spawn),
                    patch.object(jarvis, "WEB_SHUTDOWN_TIMEOUT", 0.2),
                    patch.dict(
                        os.environ, {"OPENAI_API_KEY": "FAKE-env-secret", "PYTHONPATH": "/bad"}
                    ),
                    patch.object(jarvis.os, "killpg", wraps=os.killpg) as kill,
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(error),
                ):
                    code = jarvis.main(self.web_args("--key-fd", "19", "--port", "65535"))
                self.assertTrue(all(call.args[0] == spawned[0].pid for call in kill.call_args_list))
                self.assertIsNotNone(spawned[0].poll())
                self.assertIsNone(sentinel.poll())
                self.assertNotIn("FAKE-untrusted-secret", output.getvalue() + error.getvalue())
                self.assertEqual(bool(output.getvalue()), expect_banner)
                if text == json.dumps(banner) + "\n" and exit_code == 0 and not ignore_stop:
                    self.assertEqual((code, error.getvalue()), (0, ""))
                else:
                    self.assertNotEqual(code, 0)
                    self.assertEqual(json.loads(error.getvalue()), {"ok": False})
                    if exit_code == 7:
                        self.assertEqual(code, 7)
                if ignore_stop:
                    self.assertEqual(
                        [call.args[1] for call in kill.call_args_list],
                        [signal.SIGINT, signal.SIGKILL],
                    )
        finally:
            sentinel.terminate()
            sentinel.wait(timeout=90)

    def test_web_shared_security_checks_are_unchanged(self) -> None:
        args = self.web_args("--key-file", str(self.key))
        for target, mode in ((self.path, 0o644), (self.key, 0o644), (self.root, 0o755)):
            original = target.stat().st_mode & 0o777
            target.chmod(mode)
            try:
                result = self.run_cli(*args)
                self.assertEqual(result.stdout, "")
                self.assertEqual(json.loads(result.stderr), {"ok": False})
            finally:
                target.chmod(original)
        self.key.write_text("NONFAKE-DEDICATED-TEST")
        result = self.run_cli(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr), {"ok": False})
        alias = self.folder / "linked.key"
        alias.symlink_to(self.key)
        result = self.run_cli(*self.web_args("--key-file", str(alias)))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr), {"ok": False})
        self.assertFalse((self.root / "engine-home").exists())
        self.assertEqual(self.requests, [])

    def test_web_errors_are_fixed_and_never_select_default_home(self) -> None:
        defaults = self.folder / "unused-default"
        valid = self.web_args("--key-file", str(self.key))
        for args in (
            ["web"],
            [v for i, v in enumerate(valid) if i not in (3, 4)],
            self.web_args("--key-file", "relative.key"),
            self.web_args("--key-file", str(self.folder / "absent.key")),
            self.web_args("--key-fd", "-1"),
            self.web_args("--key-file", str(self.key), "--host", "0.0.0.0"),
            *(
                self.web_args("--key-file", str(self.key), flag, "FAKE-no-echo")
                for flag in ("--api-key", "--secret", "--unknown", "--conf")
            ),
            *(
                self.web_args("--key-file", str(self.key), "--port", port)
                for port in ("-1", "65536", "bad")
            ),
            *(
                self.web_args("--key-file", str(self.key), "--test-port", port)
                for port in ("0", "65536", "bad")
            ),
        ):
            with self.subTest(args=args), patch.dict(os.environ, {"JARVIS_HOME": str(defaults)}):
                result = self.run_cli(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(json.loads(result.stderr), {"ok": False})
                self.assertNotIn("FAKE-no-echo", result.stderr)
                self.assertFalse(defaults.exists())
        self.assertFalse((self.folder / "absent.key").exists())
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
