"""Isolated checks and explicit fake-only launcher integration."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import config_launcher as entry
import product_config as config
from evidence_support import evidence_directory

ENTRY = Path(__file__).with_name("config_launcher.py")
EVIDENCE = evidence_directory("config-launcher")
KEY = "FAKE-config-launcher-test-only"


def values(port: int | None = None) -> dict[str, object]:
    return {
        "model": "fake-model",
        "provider": "custom",
        "base_url": f"http://127.0.0.1:{port}/v1" if port else "https://example.invalid/v1",
        "api_mode": "chat_completions",
        "context_length": 256000,
        "reasoning_effort": "high",
    }


class EntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=EVIDENCE)
        self.folder = Path(self.temp.name)
        self.path = self.folder / "config.json"
        self.root = self.folder / "data"
        self.root.mkdir(mode=0o700)
        self.key = self.folder / "fake.key"
        self.key.write_text(KEY)
        self.key.chmod(0o600)
        config.write_config(self.path, values())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ENTRY.parents[1] / ".venv/bin/python"), "-B", str(ENTRY), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_prompt_arguments_use_utf8_byte_boundary(self) -> None:
        from context_limits import CHAT_CONTEXT_BYTE_LIMIT

        checked = entry.check_route(self.path, self.root, key_file=self.key)
        for character in ("x", "界", "😀", "\0"):
            width = len(character.encode("utf-8"))
            exact = character * (CHAT_CONTEXT_BYTE_LIMIT // width)
            exact += "x" * (CHAT_CONTEXT_BYTE_LIMIT % width)
            with self.subTest(character=repr(character)):
                arguments = entry.launcher_arguments(
                    checked,
                    expected_sha256=checked.binding.config_sha256,
                    key_fd=3,
                    attempt_id="boundary",
                    prompt=exact,
                )
                self.assertEqual(arguments[arguments.index("--prompt") + 1], exact)
                with self.assertRaises(config.ConfigError):
                    entry.launcher_arguments(
                        checked,
                        expected_sha256=checked.binding.config_sha256,
                        key_fd=3,
                        attempt_id="overflow",
                        prompt=exact + "x",
                    )

    def test_check_readonly_and_binding(self) -> None:
        before = sorted(str(p) for p in self.folder.rglob("*"))
        with patch.object(entry.home, "PROJECT", self.folder / "other-project"):
            result = entry.check_route(self.path, self.root, key_file=self.key)
        self.assertEqual(result.binding.config_sha256, config.read_config(self.path).sha256)
        self.assertEqual(result.binding.data_root_sha256, config.sha256_text(str(self.root)))
        self.assertNotIn(KEY, json.dumps(result.summary()))
        self.assertEqual(before, sorted(str(p) for p in self.folder.rglob("*")))
        self.assertNotIn("chat_launcher", entry.__dict__)

    def test_cli_missing_readonly_and_suppressed_unknown_secret(self) -> None:
        absent = self.folder / "absent" / "config.json"
        process = self.cli(
            "--check",
            "--config",
            str(absent),
            "--data-dir",
            str(self.root),
            "--key-file",
            str(self.key),
        )
        self.assertEqual(process.returncode, 1)
        self.assertFalse(absent.parent.exists())
        self.assertNotIn(KEY, process.stdout + process.stderr)
        bad = self.cli("--api-key", KEY)
        self.assertNotIn(KEY, bad.stdout + bad.stderr)
        self.assertEqual(json.loads(self.cli("version").stdout)["version"], entry.VERSION)

    def test_root_and_key_policy(self) -> None:
        with patch.object(entry.home, "PROJECT", self.folder / "other-project"):
            for mode in (0o755, 0o777):
                self.root.chmod(mode)
                with self.assertRaises(config.ConfigError):
                    entry.check_route(self.path, self.root, key_file=self.key)
            self.root.chmod(0o700)
            self.key.chmod(0o644)
            with self.assertRaises(config.ConfigError):
                entry.check_route(self.path, self.root, key_file=self.key)
        with self.assertRaises(config.ConfigError):
            entry.check_route(self.path, self.root, key_file=self.key)
        with self.assertRaises(config.ConfigError):
            entry.check_route(self.path, Path("relative"), key_file=self.key)

    def test_fd_and_expected_conflict(self) -> None:
        read, write = os.pipe()
        os.write(write, KEY.encode())
        os.close(write)
        try:
            with patch.object(entry.home, "PROJECT", self.folder / "other-project"):
                checked = entry.check_route(self.path, self.root, key_fd=read)
                route = values()
                route["model"] = "changed-model"
                config.write_config(self.path, route, expected_sha256=checked.binding.config_sha256)
                with self.assertRaises(config.ConfigConflictError):
                    entry.launcher_arguments(
                        checked,
                        expected_sha256=checked.binding.config_sha256,
                        key_fd=read,
                        attempt_id="attempt-one",
                        prompt="test",
                    )
        finally:
            os.close(read)

    def test_no_host_config_parse_and_fake_only_boundary(self) -> None:
        host = self.folder / "host"
        host.mkdir(mode=0o700)
        poison = host / "config.yaml"
        poison.write_text("INVALID_HOST_CONFIGURATION: [")
        original = Path.read_text
        reads: list[Path] = []

        def guarded_read(path: Path, *args: object, **kwargs: object) -> str:
            reads.append(path)
            if host == path or host in path.parents:
                raise AssertionError("host config read")
            return original(path, *args, **kwargs)  # type: ignore[arg-type]

        with (
            patch.dict(os.environ, {"HERMES_HOME": str(host)}),
            patch.object(entry.home, "PROJECT", self.folder / "other-project"),
            patch.object(Path, "read_text", guarded_read),
        ):
            checked = entry.check_route(self.path, self.root, key_file=self.key)
            with self.assertRaises(config.ConfigSecurityError):
                entry.test_adapter(checked, key_file=self.key, attempt_id="test", prompt="test")
        self.assertEqual(reads, [self.path])
        self.assertFalse((host / "state.db").exists())

    def test_missing_source_and_normal_loopback_are_rejected(self) -> None:
        with patch.object(entry.home, "PROJECT", self.folder / "other-project"):
            with self.assertRaises(config.CredentialError):
                entry.check_route(self.path, self.root)
            fake = self.folder / "fake-only.json"
            config.write_config(fake, values(9123), test_port=9123)
            with self.assertRaises(config.ConfigError):
                entry.check_route(fake, self.root, key_file=self.key)

    def test_fake_http_adapter_and_duplicate_attempt(self) -> None:
        requests: list[dict[str, object]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                requests.append(json.loads(body))
                payload = json.dumps(
                    {
                        "id": "fake-config-response",
                        "model": "fake-model",
                        "object": "chat.completion",
                        "created": 1,
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "CONFIG_ADAPTER_OK"},
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

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # Current consumer forbids source-tree data roots; transient fixture
            # outside the source is deleted in this context, never user data.
            with tempfile.TemporaryDirectory(
                dir=Path.home(), prefix=".jarvis-config-test-"
            ) as name:
                root = Path(name)
                self.path = self.folder / "fake-config.json"
                config.write_config(
                    self.path,
                    values(server.server_port),
                    test_port=server.server_port,
                )
                process = self.cli(
                    "check",
                    "--config",
                    str(self.path),
                    "--data-dir",
                    str(root),
                    "--key-file",
                    str(self.key),
                    "--test-local-port",
                    str(server.server_port),
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                checked = entry.check_route(
                    self.path, root, key_file=self.key, test_port=server.server_port
                )
                with patch.dict(
                    os.environ,
                    {
                        "OPENAI_API_KEY": KEY,
                        "PYTHONPATH": "bad-parent",
                        "HERMES_HOME": str(self.folder / "unread-host"),
                    },
                ):
                    real_run = subprocess.run
                    commands: list[list[str]] = []
                    environments: list[dict[str, str]] = []

                    def record_run(
                        args: list[str], **kwargs: object
                    ) -> subprocess.CompletedProcess[str]:
                        commands.append(args)
                        environments.append(dict(kwargs["env"]))  # type: ignore[arg-type]
                        return real_run(args, **kwargs)  # type: ignore[arg-type]

                    with patch.object(entry.subprocess, "run", side_effect=record_run):
                        result = entry.test_adapter(
                            checked,
                            key_file=self.key,
                            attempt_id="one-attempt",
                            prompt="ordinary prompt",
                        )
                    self.assertEqual(result["text"], "CONFIG_ADAPTER_OK")
                    with self.assertRaises(config.ConfigError):
                        entry.test_adapter(
                            checked,
                            key_file=self.key,
                            attempt_id="one-attempt",
                            prompt="ordinary prompt",
                        )
                self.assertEqual(len(requests), 1)
                self.assertNotIn(
                    KEY, json.dumps(result) + json.dumps(commands) + json.dumps(environments)
                )
                self.assertTrue(
                    all(
                        "OPENAI_API_KEY" not in env
                        and "PYTHONPATH" not in env
                        and "HERMES_HOME" not in env
                        for env in environments
                    )
                )
                for file in root.rglob("*"):
                    if file.is_file():
                        self.assertNotIn(KEY.encode(), file.read_bytes())
                self.assertNotIn(KEY, self.path.read_text())
                (EVIDENCE / "fake-http.json").write_text(
                    json.dumps(
                        {
                            "request_count": len(requests),
                            "result": result,
                            "duplicate_rejected": True,
                            "secret_absent_from_argv_env_output_config_data": True,
                        },
                        indent=2,
                    )
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
