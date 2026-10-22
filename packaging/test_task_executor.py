"""Task executor acceptance tests; no live credentials/providers."""

from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import task_executor as protocol


class ClassificationTests(unittest.TestCase):
    def test_keyword_types_and_specialist_precedence(self):
        executor = importlib.import_module("task_executor")
        cases = {
            "帮我写一个 Python 函数": "code",
            "Please debug this JAVASCRIPT code": "code",
            "给我写一封邮件": "writing",
            "Rewrite this paragraph": "writing",
            "分析这两组数据": "analysis",
            "Compare these options": "analysis",
            "一句话回答": "fast",
            "A quick answer please": "fast",
            "帮我生成一张图片": "image",
            "Draw a cat": "image",
            "快速写一个 Python 函数": "code",
            "今天怎么样": "general",
            "withdraw my request": "general",
            "": "general",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(executor.classify_task(text), expected)


class SelectionTests(unittest.TestCase):
    def test_filters_keys_types_and_orders_quality(self):
        import task_executor as executor
        from model_registry import ModelRegistry

        with tempfile.TemporaryDirectory(
            dir=Path(os.environ["TMPDIR"]).resolve(), prefix=".jarvis-task-executor-test-"
        ) as root:
            registry = ModelRegistry(Path(root))
            self.assertIsNone(executor.select_model("code", registry))
            for identity, types, score, has_key in (
                ("keyless", ["code"], 1.0, 0),
                ("wrong-type", ["writing"], 0.99, 1),
                ("lower", ["code"], 0.7, 1),
                ("winner", ["code", "analysis"], 0.95, 1),
                ("unknown-score", ["code"], None, 1),
            ):
                registry.upsert(
                    identity,
                    provider="custom",
                    name=identity,
                    task_types=types,
                    quality_score=score,
                    has_key=has_key,
                )
            self.assertEqual(executor.select_model("code", registry)["id"], "winner")
            self.assertIsNone(executor.select_model("image", registry))
            self.assertEqual(executor.select_model("fast", registry)["id"], "deepseek-flash")
            registry.upsert("winner", has_key=0)
            self.assertEqual(executor.select_model("code", registry)["id"], "lower")


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        import task_executor

        self.executor = task_executor
        self.temporary = tempfile.TemporaryDirectory(
            dir=Path(os.environ["TMPDIR"]).resolve(), prefix=".jarvis-task-executor-test-"
        )
        self.folder = Path(self.temporary.name)
        self.key = self.folder / "key"
        self.key.write_text("FAKE-task-executor-key\n")
        self.key.chmod(0o600)
        self.config = dict(
            model="fixture-model",
            provider="custom",
            base_url="https://example.com/v1",
            api_mode="chat_completions",
            context_length=256000,
        )
        # Product credential admission correctly rejects Hermes scratch. Inject
        # only the fake leaf, never weaken its path guard or read a live key.
        from unittest.mock import patch

        import chat_launcher

        self.key_reader = patch.object(chat_launcher, "read_key", side_effect=self._fake_key)
        self.key_reader.start()
        self.addCleanup(self.key_reader.stop)

    def _fake_key(self, *, key_file, key_fd):
        self.assertEqual(key_file, self.key)
        self.assertIsNone(key_fd)
        self.assertEqual(self.key.read_text(), "FAKE-task-executor-key\n")
        return "FAKE-task-executor-key"

    def tearDown(self):
        self.temporary.cleanup()

    def test_missing_key_returns_error_without_launch(self):
        from unittest.mock import patch

        import chat_launcher

        with patch.object(chat_launcher, "launch") as launch:
            output = self.executor.execute_task(
                "hello", self.config, str(self.folder / "missing"), self.folder / "data"
            )
        self.assertIn("密钥", output)
        launch.assert_not_called()

    def test_prompt_config_and_imessage_forwarded_and_routes_isolated(self):
        from unittest.mock import patch

        import chat_launcher

        with patch.object(
            chat_launcher, "launch", return_value={"ok": True, "text": "result"}
        ) as launch:
            for model in ("fixture-model", "other-model"):
                config = {**self.config, "model": model}
                original = dict(config)
                output = self.executor.execute_task(
                    "Exact task prompt", config, str(self.key), self.folder / "data"
                )
                self.assertEqual(output, "result")
                self.assertEqual(config, original)
                args = launch.call_args.args
                self.assertEqual(args[0], {**config, "reasoning_effort": None})
                self.assertEqual(args[1], "FAKE-task-executor-key")
                self.assertEqual(args[2], "Exact task prompt")
                self.assertEqual(launch.call_args.kwargs["source_channel"], "imessage")
            self.assertNotEqual(launch.call_args_list[0].args[3], launch.call_args_list[1].args[3])

    def test_failures_are_not_retried_and_exception_secrets_are_suppressed(self):
        from unittest.mock import patch

        import chat_launcher

        with patch.object(
            chat_launcher, "launch", side_effect=RuntimeError("FAKE-task-executor-key")
        ) as launch:
            result = self.executor.execute_task(
                "hello", self.config, str(self.key), self.folder / "data"
            )
        self.assertNotIn("FAKE-task-executor-key", result)
        self.assertIn("失败", result)
        self.assertIn("重试", result)
        launch.assert_called_once()

    def test_provider_errors_and_invalid_routes_are_not_success(self):
        from unittest.mock import patch

        import chat_launcher

        with patch.object(
            chat_launcher, "launch", return_value={"ok": False, "text": "not success"}
        ):
            result = self.executor.execute_task(
                "hello", self.config, str(self.key), self.folder / "data"
            )
            self.assertNotEqual(result, "not success")
        with patch.object(
            chat_launcher,
            "launch",
            return_value={"ok": True, "generation_degraded": True, "text": "degraded reply"},
        ):
            result = self.executor.execute_task(
                "hello", self.config, str(self.key), self.folder / "data"
            )
            self.assertIn("失败", result)
        with patch.object(chat_launcher, "launch") as launch:
            for config in (
                {**self.config, "provider": "deepseek"},
                {**self.config, "api_key": "forbidden"},
            ):
                result = self.executor.execute_task(
                    "hello", config, str(self.key), self.folder / "data"
                )
                self.assertIn("配置", result)
            launch.assert_not_called()

    def test_empty_or_oversize_input_never_reads_credentials_or_launches(self):
        import chat_launcher
        from context_limits import CHAT_CONTEXT_BYTE_LIMIT

        with (
            patch.object(chat_launcher, "read_key") as reader,
            patch.object(chat_launcher, "launch") as launch,
        ):
            for text in (" ", "中" * CHAT_CONTEXT_BYTE_LIMIT):
                result = self.executor.execute_task(text, self.config, str(self.key), self.folder)
                self.assertIn("失败", result)
            reader.assert_not_called()
            launch.assert_not_called()

    def test_empty_degraded_and_secret_outputs_never_return_as_success(self):
        import chat_launcher

        for response in (
            {"ok": True, "text": " "},
            {"ok": True, "text": 1},
            {"ok": True, "text": "FAKE-task-executor-key"},
            {"ok": True, "generation_degraded": True, "text": "not completed"},
        ):
            with (
                self.subTest(response=response),
                patch.object(chat_launcher, "launch", return_value=response) as launch,
            ):
                result = self.executor.execute_task(
                    "hello", self.config, str(self.key), self.folder
                )
                self.assertIn("失败", result)
                self.assertNotIn("FAKE-task-executor-key", result)
                launch.assert_called_once()

    def test_launcher_error_is_safe_and_never_retried(self):
        import chat_launcher

        for diagnostic in ("unknown attempt fixture-1", "FAKE-task-executor-key"):
            with (
                self.subTest(diagnostic=diagnostic),
                patch.object(
                    chat_launcher, "launch", side_effect=chat_launcher.LauncherError(diagnostic)
                ) as launch,
            ):
                result = self.executor.execute_task(
                    "hello", self.config, str(self.key), self.folder
                )
                self.assertIn("失败", result)
                self.assertNotIn("FAKE-task-executor-key", result)
                launch.assert_called_once()


class SelfEvaluationTests(unittest.TestCase):
    def test_self_evaluate_contract(self):
        hook = getattr(protocol, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-level self_evaluate is required")
        before = datetime.now(UTC)
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
        self.assertEqual(report["module"], "task_executor")
        for field in ("domain", "current_version"):
            self.assertIsInstance(report[field], str)
            self.assertTrue(report[field].strip())
        for field in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[field], list)
            self.assertTrue(report[field])
            self.assertTrue(all(isinstance(item, str) and item.strip() for item in report[field]))
        stamp = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.utcoffset(), UTC.utcoffset(stamp))
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, datetime.now(UTC))
        json.dumps(report, allow_nan=False)

    def test_self_evaluate_is_fresh_and_has_no_execution_or_file_effects(self):
        hook = getattr(protocol, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-level self_evaluate is required")
        with (
            patch("builtins.open", side_effect=AssertionError("unexpected file access")),
            patch.object(
                protocol, "execute_task", side_effect=AssertionError("unexpected protocol use")
            ),
        ):
            first = hook()
            first["known_limitations"].append("caller-only")
            first["improvement_directions"].clear()
            second = hook()
        self.assertNotIn("caller-only", second["known_limitations"])
        self.assertTrue(second["improvement_directions"])


class AdmissionTests(unittest.TestCase):
    def test_registry_detects_real_module_hook(self):
        from integration_registry import REGISTRY

        self.assertTrue(REGISTRY._has_self_evaluate(REGISTRY.get("task_executor")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
