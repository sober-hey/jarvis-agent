"""Real task execution checks; model transport is mocked, tools are not."""

import copy
import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import product_config
import task_api as task
from tool_router import ToolRouter


class RealTaskTests(unittest.TestCase):
    def setUp(self):
        # Data roots cannot overlap Hermes or source. No system temporary path.
        self.temp = tempfile.TemporaryDirectory(prefix="jarvis-task-real-", dir="/Users/sober")
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent / "data"
        self.root.mkdir(mode=0o700)
        self.workspace = self.parent / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.input = self.root / "input.txt"
        self.input.write_text("task source material")
        self.input.chmod(0o600)
        self.config = self.parent / "config.json"
        product_config.write_config(
            self.config,
            {
                "model": "mock-planner",
                "provider": "custom",
                "base_url": "https://example.invalid/v1",
                "api_mode": "chat_completions",
                "context_length": 256000,
                "reasoning_effort": "low",
            },
        )
        self.key = self.parent / "api.key"
        self.key.write_text("FAKE-private-task-key")
        self.key.chmod(0o600)
        self.router = ToolRouter(
            allowed_roots=[self.workspace], data_root=self.root, repo_root=self.workspace
        )

    def submit(self, steps=None, **kw):
        return task.submit(
            config_path=self.config,
            data_dir=self.root,
            input_path=self.input,
            instruction=json.dumps({"steps": steps})
            if steps is not None
            else "perform the requested work",
            key_file=self.key,
            router=self.router,
            **kw,
        )

    def test_real_write_read_publish_and_readonly_replay(self):
        target = self.workspace / "output.txt"
        steps = [
            {"tool": "write_file", "args": {"path": str(target), "content": "real execution\n"}},
            {"tool": "read_file", "args": {"path": str(target)}},
        ]
        result = self.submit(steps, publish=True, request_key="real-file")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(target.read_text(), "real execution\n")
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(result["steps"][1]["result"]["result"]["content"], target.read_text())
        self.assertTrue(
            all(s["result"]["audit"]["category"] == "tool_call" for s in result["steps"])
        )
        self.assertEqual(task.query(self.root, result["task_id"]), result)
        with (
            patch.object(task, "atomic", wraps=task.atomic) as atomic,
            patch.object(self.router, "route", wraps=self.router.route) as route,
        ):
            again = self.submit(steps, publish=True, request_key="real-file")
            self.assertEqual(result, again)
            self.assertEqual(atomic.call_count, 0)
            self.assertEqual(route.call_count, 0)
        published = self.root / result["published_relpath"]
        self.assertEqual(
            hashlib.sha256(published.read_bytes()).hexdigest(), result["output_sha256"]
        )

    def test_real_git_status_and_shell_echo(self):
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        result = self.submit(
            [
                {"tool": "git_status", "args": {}},
                {
                    "tool": "execute",
                    "args": {"command": "echo task-real", "cwd": str(self.workspace)},
                },
            ]
        )
        self.assertEqual(result["status"], "awaiting_approval", result)
        self.assertEqual(result["steps"][1]["result"]["result"]["stdout"].strip(), "task-real")
        self.assertTrue(result["steps"][0]["result"]["ok"])
        self.assertFalse((self.root / "published").exists())

    def test_dangerous_command_denied_and_later_step_not_run(self):
        target = self.workspace / "never.txt"
        result = self.submit(
            [
                {"tool": "execute", "args": {"command": "rm -rf /", "cwd": str(self.workspace)}},
                {"tool": "write_file", "args": {"path": str(target), "content": "bad"}},
            ]
        )
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["steps"][0]["result"]["status"], "denied")
        self.assertFalse(target.exists())
        self.assertIsNone(result["steps"][1]["attempt"])

    def test_cancel_in_flight_prevents_following_tool_and_publish(self):
        entered, release = threading.Event(), threading.Event()
        target = self.workspace / "cancelled.txt"
        steps = [
            {"tool": "execute", "args": {"command": "echo first", "cwd": str(self.workspace)}},
            {"tool": "write_file", "args": {"path": str(target), "content": "bad"}},
        ]
        original = self.router.route
        values, failures = [], []

        def paused(name, args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timeout")
            return original(name, args)

        def worker():
            try:
                values.append(self.submit(steps, publish=True, request_key="cancel-real"))
            except Exception as exc:
                failures.append(exc)

        with patch.object(self.router, "route", side_effect=paused):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                task_id = "task-" + task.digest(b"cancel-real")
                self.assertTrue(task.cancel(self.root, task_id)["cancel_requested"])
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures)
        self.assertTrue(values[0]["cancel_requested"])
        self.assertEqual(values[0]["status"], "failed")
        self.assertFalse(target.exists())
        self.assertFalse((self.root / "published").exists())

    def test_unknown_after_effect_is_not_retried(self):
        target = self.workspace / "once.txt"
        steps = [{"tool": "write_file", "args": {"path": str(target), "content": "once"}}]

        def uncertain(name, args):
            self.router._files().write_file(**args)
            raise OSError("private diagnostic must not escape")

        with patch.object(self.router, "route", side_effect=uncertain) as route:
            result = self.submit(steps, request_key="unknown-real")
            self.assertEqual(result["status"], "unknown", result)
            again = self.submit(steps, request_key="unknown-real")
            self.assertEqual(again["status"], "unknown")
            self.assertEqual(route.call_count, 1)
        self.assertTrue(result["reconciliation_required"])
        self.assertEqual(target.read_text(), "once")
        self.assertNotIn("private diagnostic", json.dumps(result))

    def test_mock_model_plan_executes_real_tool_once(self):
        target = self.workspace / "model.txt"
        response = {
            "ok": True,
            "text": json.dumps(
                {
                    "steps": [
                        {
                            "tool": "write_file",
                            "args": {"path": str(target), "content": "model-planned"},
                        }
                    ]
                }
            ),
        }
        with patch("chat_launcher.launch", return_value=response) as model:
            result = self.submit(request_key="model-real")
            again = self.submit(publish=True, request_key="model-real")
        self.assertEqual(result["status"], "awaiting_approval", result)
        self.assertEqual(again["status"], "completed", again)
        self.assertEqual(target.read_text(), "model-planned")
        self.assertEqual(model.call_count, 1)
        call = model.call_args
        self.assertEqual(call.kwargs["persona_mode"], "inner")
        self.assertIn("write_file", call.args[2])
        from context_limits import INNER_CONTEXT_BYTE_LIMIT

        self.assertLessEqual(len(call.args[2].encode()), INNER_CONTEXT_BYTE_LIMIT)
        self.assertEqual(call.args[1], "FAKE-private-task-key")
        self.assertNotIn("FAKE-private-task-key", json.dumps(result))

    def test_model_unknown_never_reissued(self):
        with patch("chat_launcher.launch", side_effect=RuntimeError("secret error")) as model:
            result = self.submit(request_key="model-unknown")
            again = self.submit(request_key="model-unknown")
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(again["status"], "unknown")
        self.assertEqual(model.call_count, 1)
        self.assertNotIn("secret error", json.dumps(result))

    def test_entire_plan_validated_before_any_side_effect(self):
        target = self.workspace / "badplan.txt"
        with self.assertRaises(ValueError):
            self.submit(
                [
                    {"tool": "write_file", "args": {"path": str(target), "content": "bad"}},
                    {"tool": "missing", "args": {}},
                ]
            )
        self.assertFalse(target.exists())
        with self.assertRaises(ValueError):
            self.submit([{"tool": "execute", "args": {"command": "echo x"}}] * 9)
        self.assertFalse((self.root / "tasks").exists())

    def test_completed_step_receipt_tampering_is_rejected(self):
        steps = [
            {"tool": "execute", "args": {"command": "echo verified", "cwd": str(self.workspace)}}
        ]
        result = self.submit(steps, publish=True, request_key="tamper-real")
        path = self.root / "tasks" / (result["task_id"] + ".json")
        value = copy.deepcopy(result)
        value["steps"][0]["result"]["result"]["stdout"] = "forged"
        path.write_text(json.dumps(value))
        with patch.object(task, "atomic", wraps=task.atomic) as writer:
            with self.assertRaises(ValueError):
                self.submit(steps, publish=True, request_key="tamper-real")
            self.assertEqual(writer.call_count, 0)

    def test_bad_model_plan_is_terminal_and_does_not_execute(self):
        target = self.workspace / "model-bad.txt"
        response = {
            "ok": True,
            "text": json.dumps(
                {
                    "steps": [
                        {"tool": "write_file", "args": {"path": str(target), "content": "bad"}},
                        {"tool": "missing", "args": {}},
                    ]
                }
            ),
        }
        with patch("chat_launcher.launch", return_value=response) as model:
            result = self.submit(request_key="bad-model")
            again = self.submit(request_key="bad-model")
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(again["error"], "invalid_model_plan")
        self.assertFalse(target.exists())
        self.assertEqual(model.call_count, 1)

    def test_oversized_prompt_fails_before_model_admission(self):
        self.input.write_text("x" * 20000)
        with patch("chat_launcher.launch") as model:
            result = self.submit(request_key="large-model-context")
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["error"], "model_context_limit")
        self.assertNotIn("model_operation", result)
        self.assertEqual(model.call_count, 0)

    def test_credential_in_tool_output_is_not_persisted_or_retried(self):
        steps = [
            {"tool": "execute", "args": {"command": "echo innocent", "cwd": str(self.workspace)}}
        ]
        original = self.router.route

        def secret_result(name, args):
            result = original(name, args)
            result["result"]["stdout"] = self.key.read_text()
            return result

        with patch.object(self.router, "route", side_effect=secret_result) as route:
            result = self.submit(steps, request_key="credential-output")
            again = self.submit(steps, request_key="credential-output")
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(again["status"], "unknown")
        self.assertEqual(route.call_count, 1)
        for path in (self.root / "tasks").glob("*.json"):
            self.assertNotIn(self.key.read_text(), path.read_text())

    def test_structured_plan_needs_no_key_and_cli_runs_real_tool(self):
        packaging = Path(__file__).resolve().parents[1]
        target = self.workspace / "cli-real.txt"
        instruction = json.dumps(
            [
                {
                    "tool_name": "write_file",
                    "arguments": {"path": str(target), "content": "cli executed"},
                }
            ]
        )
        process = subprocess.run(
            [
                # Absolute product paths: the CLI must not depend on the caller's cwd.
                str(packaging / "runtime/engine-venv/bin/python3"),
                str(packaging / "jarvis.py"),
                "task",
                "submit",
                "--config",
                str(self.config),
                "--data-dir",
                str(self.root),
                "--input",
                str(self.input),
                "--instruction",
                instruction,
                "--allowed-root",
                str(self.workspace),
                "--publish",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["provider"], "tool-plan-v1")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(target.read_text(), "cli executed")

    def test_json_escaped_credential_in_plan_is_rejected_before_persistence(self):
        target = self.workspace / "escaped-secret.txt"
        raw = json.dumps(
            {
                "steps": [
                    {
                        "tool": "write_file",
                        "args": {"path": str(target), "content": self.key.read_text()},
                    }
                ]
            }
        )
        escaped = raw.replace("FAKE", r"\u0046AKE")
        self.assertNotIn(self.key.read_text(), escaped)
        with patch("chat_launcher.launch", return_value={"ok": True, "text": escaped}):
            result = self.submit(request_key="escaped-model-credential")
        self.assertEqual(result["status"], "failed", result)
        self.assertFalse(target.exists())
        with self.assertRaises(ValueError):
            task.submit(
                config_path=self.config,
                data_dir=self.root,
                input_path=self.input,
                instruction=escaped,
                key_file=self.key,
                router=self.router,
                request_key="escaped-structured-credential",
            )
        for path in (self.root / "tasks").glob("*.json"):
            self.assertNotIn(self.key.read_text(), path.read_text())


if __name__ == "__main__":
    unittest.main()
