"""Offline adapter tests: fake credentials, mocked child, real local pipes only."""

from pathlib import Path
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import inner_model


class ModelBehaviorTests(unittest.TestCase):
    def model(self):
        model = object.__new__(inner_model.ProductModel)
        model.root = Path("/fixture/product-root")
        model.key = "FAKE-UNIT-KEY-NOT-PRIVATE"
        model.binding = SimpleNamespace(
            values={"reasoning_effort": "low", "model": "fixture-model"}
        )
        return model

    def test_failure_names_are_sanitized(self):
        for invalid in ("ValueError: private", "含私密", "../error", "", None, "x" * 101):
            with self.subTest(value=invalid):
                self.assertEqual(
                    str(inner_model.ProductRequestFailure(invalid)), "UnknownProductError"
                )
        self.assertEqual(str(inner_model.ProductRequestFailure("TimeoutError")), "TimeoutError")

    def test_unsafe_prompt_is_rejected_before_any_pipe_or_child(self):
        model = self.model()
        for prompt in ("prefix " + model.key, "汉" * inner_model.INNER_CONTEXT_BYTE_LIMIT):
            with (
                self.subTest(kind="secret" if model.key in prompt else "bytes"),
                patch.object(inner_model.os, "pipe") as pipe,
                patch.object(inner_model.subprocess, "Popen") as spawn,
            ):
                with self.assertRaisesRegex(ValueError, "Unsafe or oversized prompt"):
                    model(prompt)
                pipe.assert_not_called()
                spawn.assert_not_called()

    def invoke(self, stdout, *, stderr="", returncode=0, first_error=None):
        model = self.model()
        process = Mock(returncode=returncode)
        process.communicate.side_effect = [first_error or (stdout, stderr), ("", "")]
        captured = {}

        def spawn(command, **kwargs):
            fd = kwargs["pass_fds"][0]
            captured["fd"] = fd
            captured["credentials"] = json.loads(os.read(fd, 4096))
            captured["command"] = command
            captured["kwargs"] = kwargs
            return process

        launcher = inner_model.launcher
        with (
            patch.object(launcher.chat, "validate_route") as validate,
            patch.object(launcher, "binding_summary", return_value={"binding_version": 1}),
            patch.object(launcher.chat.home, "protected_paths", return_value=[]),
            patch.object(launcher.chat.engine, "environment", return_value={}),
            patch.object(launcher.chat, "launcher_timeout_seconds", return_value=3),
            patch.object(launcher, "kill_group") as kill,
            patch.object(inner_model.subprocess, "Popen", side_effect=spawn),
        ):
            try:
                result = model("unit prompt")
            finally:
                kill.assert_called_once_with(process)
                self.assertEqual(process.communicate.call_count, 2)
                with self.assertRaises(OSError):
                    os.fstat(captured["fd"])
        validate.assert_called_once()
        payload = json.loads(process.communicate.call_args_list[0].args[0])
        self.assertEqual(payload, {"prompt": "unit prompt", "persona_mode": "inner"})
        self.assertEqual(captured["credentials"]["route"]["api_key"], model.key)
        self.assertNotIn(model.key, " ".join(captured["command"]))
        self.assertEqual(captured["kwargs"]["env"], {})
        return result

    def test_success_projects_only_allowlisted_envelope_fields(self):
        result = self.invoke(
            json.dumps(
                {
                    "ok": True,
                    "text": "fixture answer",
                    "usage": {"input_tokens": 1},
                    "untrusted_extra": "dropped",
                }
            )
        )
        self.assertEqual(result["text"], "fixture answer")
        self.assertEqual(result["usage"], {"input_tokens": 1})
        self.assertNotIn("untrusted_extra", result)
        self.assertEqual(
            set(result),
            {
                "text",
                "usage",
                "model_attempts",
                "response_id",
                "request_id",
                "model",
                "requested_model",
                "provider",
                "api_mode",
                "reasoning_effort",
                "memory_extraction",
            },
        )

    def test_secret_output_is_suppressed_and_cleanup_runs(self):
        for stdout, stderr in [(self.model().key, ""), ("{}", self.model().key)]:
            with (
                self.subTest(channel="stdout" if not stderr else "stderr"),
                self.assertRaisesRegex(ValueError, "Unsafe model output suppressed"),
            ):
                self.invoke(stdout, stderr=stderr)

    def test_bad_envelopes_and_product_errors(self):
        with self.assertRaisesRegex(ValueError, "Invalid model envelope"):
            self.invoke("[]")
        with self.assertRaisesRegex(inner_model.ProductRequestFailure, "TimeoutError"):
            self.invoke(json.dumps({"ok": False, "error_type": "TimeoutError"}), returncode=1)
        with self.assertRaisesRegex(inner_model.ProductRequestFailure, "UnknownProductError"):
            self.invoke(json.dumps({"ok": False, "error_type": "private diagnostic"}))

    def test_timeout_still_kills_child_and_closes_fd(self):
        import subprocess

        with self.assertRaises(subprocess.TimeoutExpired):
            self.invoke("", first_error=subprocess.TimeoutExpired("fixture", 3))


class ModelEvaluationTests(unittest.TestCase):
    def test_self_evaluate_is_detached_metadata_without_io_or_instance(self):
        import builtins
        from datetime import datetime
        from unittest.mock import patch
        import inner_model as module

        self.assertTrue(callable(getattr(module, "self_evaluate", None)))
        with (
            patch.object(builtins, "open", side_effect=AssertionError("no files")),
            patch.object(Path, "home", side_effect=AssertionError("no user root")),
            patch("inner_model.subprocess.Popen", side_effect=AssertionError("no process")),
        ):
            result = module.self_evaluate()
            self.assertEqual(module.ProductModel.self_evaluate()["module"], "inner_model")
        self.assertEqual(
            set(result),
            {
                "module",
                "domain",
                "current_version",
                "known_limitations",
                "improvement_directions",
                "last_evaluated_at",
            },
        )
        self.assertEqual(result["module"], "inner_model")
        for key in ("domain", "current_version"):
            self.assertIsInstance(result[key], str)
            self.assertTrue(result[key].strip())
        for key in ("known_limitations", "improvement_directions"):
            self.assertTrue(result[key])
            self.assertTrue(all(isinstance(item, str) and item.strip() for item in result[key]))
        self.assertEqual(
            datetime.fromisoformat(result["last_evaluated_at"]).utcoffset().total_seconds(), 0
        )
        result["known_limitations"].append("caller mutation")
        self.assertNotIn("caller mutation", module.self_evaluate()["known_limitations"])
