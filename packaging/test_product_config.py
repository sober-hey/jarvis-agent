"""Tests for the JARVIS-owned non-secret route and credential boundary."""

from __future__ import annotations

import importlib.util
import json
import os
import pwd
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "product_config_under_test", HERE / "product_config.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("product_config.py is missing")
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)


ROUTE: dict[str, object] = {
    "model": "fake-test-model",
    "provider": "custom",
    "base_url": "https://api.example.test/v1",
    "api_mode": "chat_completions",
    "context_length": 256000,
    "reasoning_effort": "high",
}
FAKE_KEY = "FAKE-ONLY-NONSECRET-TEST-KEY"


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="jarvis-config-test-", dir=Path.home())
        self.base = Path(self.temp.name)
        self.config_path = self.base / "home" / "config.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_default_path_and_check_do_not_create_real_or_temp_home(self) -> None:
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        real_product_home = real_home / ".jarvis"
        before = real_product_home.exists()
        isolated_home = self.base / "fake-home"
        with patch.dict(os.environ, {"HOME": str(isolated_home)}, clear=False):
            self.assertEqual(config.default_config_path(), isolated_home / ".jarvis/config.json")
            self.assertEqual(config.check(self.config_path), {"ok": False, "error": "missing"})
        self.assertEqual(real_product_home.exists(), before)
        self.assertFalse(isolated_home.exists())
        self.assertFalse(self.config_path.parent.exists())

    def test_write_read_and_optimistic_conflict_are_atomic(self) -> None:
        first = config.write_config(self.config_path, ROUTE)
        self.assertEqual(first.values, ROUTE)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config_path.stat().st_uid, os.getuid())
        loaded = config.read_config(self.config_path)
        self.assertEqual(loaded.values, ROUTE)
        with self.assertRaises(config.ConfigConflictError):
            config.write_config(self.config_path, {**ROUTE, "model": "changed"})
        with self.assertRaises(config.ConfigConflictError):
            config.write_config(
                self.config_path,
                {**ROUTE, "model": "changed"},
                expected_sha256="0" * 64,
            )
        changed = config.write_config(
            self.config_path,
            {**ROUTE, "model": "changed"},
            expected_sha256=first.sha256,
        )
        self.assertNotEqual(changed.sha256, first.sha256)
        self.assertEqual(config.read_config(self.config_path).values["model"], "changed")

    def test_schema_has_only_six_non_secret_fields(self) -> None:
        self.assertEqual(config.CONFIG_FIELDS, frozenset(ROUTE))
        extras: tuple[dict[str, object], ...] = (
            {"api_key": FAKE_KEY},
            {"tools": []},
            {"hermes_config": "x"},
        )
        for extra in extras:
            with self.subTest(extra=extra), self.assertRaises(config.ConfigError):
                config.validate_config({**ROUTE, **extra})
        with self.assertRaises(config.ConfigError):
            config.validate_config({key: value for key, value in ROUTE.items() if key != "model"})
        self.config_path.parent.mkdir(mode=0o700)
        self.config_path.write_text(json.dumps({**ROUTE, "api_key": FAKE_KEY}), encoding="utf-8")
        self.config_path.chmod(0o600)
        with self.assertRaises(config.ConfigError):
            config.read_config(self.config_path)
        self.assertNotIn(FAKE_KEY, json.dumps(config.check(self.config_path)))

    def test_paths_permissions_owner_and_host_hermes_are_rejected(self) -> None:
        self.config_path.parent.mkdir(mode=0o700)
        self.config_path.write_text(json.dumps(ROUTE), encoding="utf-8")
        self.config_path.chmod(0o644)
        with self.assertRaises(config.ConfigSecurityError):
            config.read_config(self.config_path)
        self.config_path.chmod(0o600)
        link = self.base / "config-link.json"
        link.symlink_to(self.config_path)
        with self.assertRaises(config.ConfigSecurityError):
            config.read_config(link)
        with self.assertRaises(config.ConfigSecurityError):
            config.read_config(Path("/Volumes/扩展硬盘/hermes/config.json"))
        with patch.dict(os.environ, {"HERMES_HOME": str(self.base / "host-hermes")}):
            with self.assertRaises(config.ConfigSecurityError):
                config.read_config(self.base / "host-hermes/config.json")
        with patch.object(config.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(config.ConfigSecurityError):
                config.read_config(self.config_path)

    def test_route_endpoint_policy_and_binding_are_explicit_and_immutable(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.validate_config({**ROUTE, "base_url": "http://127.0.0.1:8123/v1"})
        test_route = {**ROUTE, "base_url": "http://127.0.0.1:8123/v1"}
        self.assertEqual(config.validate_config(test_route, test_port=8123), test_route)
        with self.assertRaises(config.ConfigError):
            config.validate_config(test_route, test_port=8124)
        data_root = self.base / "data"
        data_root.mkdir(mode=0o700)
        bound = config.bind_route(ROUTE, data_root)
        self.assertEqual(bound.version, config.BINDING_VERSION)
        self.assertEqual(bound.config_sha256, config.sha256_config(ROUTE))
        self.assertEqual(bound.data_root, data_root)
        self.assertEqual(bound.data_root_sha256, config.sha256_text(str(data_root)))
        self.assertEqual(len(bound.binding_sha256), 64)
        with self.assertRaises(TypeError):
            bound.values["model"] = "mutated"  # type: ignore[index]
        with self.assertRaises(config.ConfigSecurityError):
            config.bind_route(ROUTE, self.base / "missing-data")
        data_root.chmod(0o755)
        with self.assertRaises(config.ConfigSecurityError):
            config.bind_route(ROUTE, data_root)

    def test_key_requires_dedicated_0600_file_or_pipe_fd(self) -> None:
        key_file = self.base / "key"
        key_file.write_text(FAKE_KEY + "\n", encoding="utf-8")
        key_file.chmod(0o600)
        self.assertEqual(config.read_key(key_file=key_file), FAKE_KEY)
        key_file.chmod(0o644)
        with self.assertRaises(config.CredentialError):
            config.read_key(key_file=key_file)
        key_file.chmod(0o600)
        link = self.base / "key-link"
        link.symlink_to(key_file)
        with self.assertRaises(config.CredentialError):
            config.read_key(key_file=link)
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, FAKE_KEY.encode())
            os.close(write_fd)
            write_fd = -1
            self.assertEqual(config.read_key(key_fd=read_fd), FAKE_KEY)
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
        with self.assertRaises(config.CredentialError):
            config.read_key(key_file=key_file, key_fd=0)

    def test_real_launcher_fake_http_uses_generated_config_and_deduplicates_attempt(self) -> None:
        requests: list[dict[str, Any]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps(
                    {
                        "id": "fake-response",
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
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        data_root = self.base / "data"
        data_root.mkdir(mode=0o700)
        route = {**ROUTE, "base_url": f"http://127.0.0.1:{server.server_port}/v1"}
        self.config_path.parent.mkdir(mode=0o700)
        config.write_config(self.config_path, route, test_port=server.server_port)
        key_file = self.base / "key"
        key_file.write_text(FAKE_KEY, encoding="utf-8")
        key_file.chmod(0o600)
        python = HERE.parent / ".venv/bin/python"
        launcher = HERE / "chat_launcher.py"
        command = [
            str(python),
            "-B",
            str(launcher),
            "--config",
            str(self.config_path),
            "--key-file",
            str(key_file),
            "--prompt",
            "hello",
            "--data-dir",
            str(data_root),
            "--test-local-port",
            str(server.server_port),
            "--attempt-id",
            "stable-attempt",
        ]
        try:
            first = subprocess.run(command, text=True, capture_output=True, timeout=65)
            self.assertEqual(first.returncode, 0, first.stderr)
            result = json.loads(first.stdout)
            self.assertEqual(result["text"], "FAKE_HTTP_OK")
            duplicate = subprocess.run(command, text=True, capture_output=True, timeout=5)
            self.assertEqual(duplicate.returncode, 1)
            self.assertEqual(len(requests), 1)
            query = subprocess.run(
                [
                    str(python),
                    "-B",
                    str(launcher),
                    "--query-attempt",
                    "stable-attempt",
                    "--data-dir",
                    str(data_root),
                ],
                text=True,
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(query.returncode, 0, query.stderr)
            self.assertEqual(json.loads(query.stdout)["state"], "completed")
            self.assertNotIn(FAKE_KEY, first.stdout + first.stderr + duplicate.stderr)
            self.assertNotIn(FAKE_KEY, self.config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                config.bind_route(
                    config.read_config(self.config_path, test_port=server.server_port).values,
                    data_root,
                    test_port=server.server_port,
                ).version,
                1,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_cli_check_is_read_only_and_no_network_or_model(self) -> None:
        config.write_config(self.config_path, ROUTE)
        before = sorted(str(path.relative_to(self.base)) for path in self.base.rglob("*"))
        result = subprocess.run(
            [
                str(HERE.parent / ".venv/bin/python"),
                "-B",
                str(HERE / "product_config.py"),
                "check",
                "--config",
                str(self.config_path),
            ],
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["ok"], True)
        after = sorted(str(path.relative_to(self.base)) for path in self.base.rglob("*"))
        self.assertEqual(after, before)


class SelfEvaluationTests(unittest.TestCase):
    def test_owned_hook_returns_honest_schema_without_private_io(self):
        import importlib
        import json
        from datetime import datetime, timezone
        from unittest.mock import patch

        module = importlib.import_module("product_config")
        hook = getattr(module, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-owned self_evaluate is missing")
        before = datetime.now(timezone.utc)
        with (
            patch("builtins.open", side_effect=AssertionError("private read")),
            patch("pathlib.Path.open", side_effect=AssertionError("private read")),
            patch("sqlite3.connect", side_effect=AssertionError("database access")),
            patch("subprocess.Popen", side_effect=AssertionError("external process")),
            patch("socket.socket.connect", side_effect=AssertionError("network")),
        ):
            report = hook()
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
        self.assertEqual(report["module"], "product_config")
        self.assertIn("six-field", report["current_version"])
        for field in ("domain", "current_version"):
            self.assertGreater(len(report[field]), 10)
        for field in ("known_limitations", "improvement_directions"):
            self.assertTrue(report[field])
            self.assertTrue(all(isinstance(item, str) and len(item) > 15 for item in report[field]))
        evaluated = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(evaluated.utcoffset())
        self.assertGreaterEqual(evaluated, before)
        self.assertLessEqual(evaluated, datetime.now(timezone.utc))
        json.dumps(report)

    def test_reports_own_mutable_lists(self):
        import importlib

        module = importlib.import_module("product_config")
        hook = getattr(module, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-owned self_evaluate is missing")
        report = hook()
        report["known_limitations"].clear()
        report["improvement_directions"].clear()
        fresh = hook()
        self.assertTrue(fresh["known_limitations"])
        self.assertTrue(fresh["improvement_directions"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
