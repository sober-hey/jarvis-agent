"""HTTP document task behavior, controlled generator (no real model)."""

from __future__ import annotations

import base64
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

import web_server


class WebTaskTests(unittest.TestCase):
    def test_upload_process_download_and_refresh(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".jarvis-task-test-", dir=Path.home()) as d:
            folder = Path(d)
            cfg = folder / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "model": "fake",
                        "provider": "custom",
                        "base_url": "http://127.0.0.1:57431/v1",
                        "api_mode": "chat_completions",
                        "context_length": 256000,
                        "reasoning_effort": "high",
                    }
                )
            )
            cfg.chmod(0o600)
            key = folder / "key"
            key.write_text("FAKE-task-key")
            key.chmod(0o600)
            server = web_server.create_server(
                config_file=cfg, key_file=key, data_dir=folder / "data", test_local_port=57431
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            calls: list[str] = []

            def generate(prompt: str, identity: str) -> str:
                calls.append(identity)
                return "# 摘要\n\n项目预算为42元。"

            server.tasks.generate = generate

            def call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                conn.request(
                    method,
                    path,
                    None if body is None else json.dumps(body),
                    {"Origin": server.origin, "Content-Type": "application/json"},
                )
                r = conn.getresponse()
                raw = r.read()
                value = (
                    json.loads(raw)
                    if "application/json" in r.getheader("Content-Type", "")
                    else raw
                )
                conn.close()
                return r.status, value

            try:
                body = {
                    "request_key": "upload-01",
                    "filename": "notes.txt",
                    "content_base64": base64.b64encode("项目预算42元。".encode()).decode(),
                    "processing": "summary",
                    "instruction": "用中文摘要",
                }
                status, record = call("POST", "/api/tasks", body)
                self.assertEqual(status, 201)
                task_id = record["task_id"]
                self.assertEqual(call("POST", "/api/tasks/" + task_id + "/resume", {})[0], 202)
                for _ in range(100):
                    record = call("GET", "/api/tasks/" + task_id)[1]
                    if record["status"] == "completed":
                        break
                    time.sleep(0.02)
                self.assertEqual(record["status"], "completed")
                self.assertEqual(record["progress"], 100)
                self.assertEqual(call("GET", "/api/tasks")[1]["tasks"][0]["task_id"], task_id)
                self.assertEqual(
                    call("GET", "/api/tasks/" + task_id + "/download")[1],
                    "# 摘要\n\n项目预算为42元。".encode(),
                )
                call("POST", "/api/tasks/" + task_id + "/resume", {})
                time.sleep(0.05)
                self.assertEqual(len(calls), 1)
                self.assertEqual(
                    call("POST", "/api/tasks", {**body, "filename": "../evil.txt"})[0], 400
                )
                self.assertEqual(
                    call("POST", "/api/tasks", {**body, "content_base64": "!"})[0], 400
                )
            finally:
                server.stop(5)
                thread.join(2)


class SelfEvaluationTests(unittest.TestCase):
    def test_offline_module_evaluation_describes_real_scope(self):
        import importlib
        import json
        from datetime import datetime
        from unittest.mock import patch

        module = importlib.import_module("web_tasks")
        hook = getattr(module, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-level self_evaluate is required")
        with (
            patch("pathlib.Path.open", side_effect=AssertionError("unexpected file IO")),
            patch("pathlib.Path.mkdir", side_effect=AssertionError("unexpected mkdir")),
            patch("subprocess.Popen", side_effect=AssertionError("unexpected process")),
        ):
            report = hook()
        self.assertEqual(report["module"], "web_tasks")
        self.assertTrue(report["domain"].strip())
        self.assertTrue(report["current_version"].strip())
        for key in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[key], list)
            self.assertTrue(report[key])
            self.assertTrue(all(isinstance(value, str) and value.strip() for value in report[key]))
        self.assertIn("worker", " ".join(report["known_limitations"]).lower())
        self.assertIsNotNone(datetime.fromisoformat(report["last_evaluated_at"]).utcoffset())
        report["known_limitations"].clear()
        self.assertTrue(hook()["known_limitations"])
        json.dumps(report)


if __name__ == "__main__":
    unittest.main()
