"""No sockets, processes, models, stores or historical evidence in hook tests."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime
import importlib
import inspect
import json
from types import ModuleType
from typing import Any, cast
import unittest
from unittest.mock import patch

MODULES = ("avatar_signals", "avatar_stream", "web_server", "verify_avatar_web")
REQUIRED = {
    "module",
    "domain",
    "current_version",
    "known_limitations",
    "improvement_directions",
    "last_evaluated_at",
}


def load(name: str) -> ModuleType:
    return importlib.import_module(name)


class AvatarHookContractTests(unittest.TestCase):
    def test_every_hook_is_module_level_no_argument_and_serializable(self):
        for name in MODULES:
            with self.subTest(module=name):
                hook = getattr(load(name), "self_evaluate", None)
                self.assertTrue(callable(hook), name)
                assert callable(hook)
                self.assertEqual(list(inspect.signature(hook).parameters), [])
                before = datetime.now().astimezone()
                result = cast(dict[str, Any], hook())
                after = datetime.now().astimezone()
                self.assertLessEqual(REQUIRED, result.keys())
                self.assertEqual(result["module"], name)
                for key in ("domain", "current_version"):
                    self.assertIsInstance(result[key], str)
                    self.assertTrue(result[key].strip())
                for key in ("known_limitations", "improvement_directions"):
                    self.assertIsInstance(result[key], list)
                    self.assertTrue(result[key])
                    self.assertTrue(all(isinstance(v, str) and v.strip() for v in result[key]))
                evaluated = datetime.fromisoformat(result["last_evaluated_at"])
                self.assertIsNotNone(evaluated.tzinfo)
                self.assertLessEqual(before, evaluated)
                self.assertLessEqual(evaluated, after)
                self.assertFalse(result["runtime_verified"])
                json.dumps(result, allow_nan=False)

    def test_hooks_do_not_read_or_write_files_open_db_spawn_or_bind(self):
        modules = [load(name) for name in MODULES]
        targets = (
            "builtins.open",
            "io.open",
            "os.open",
            "sqlite3.connect",
            "socket.socket",
            "subprocess.Popen",
            "threading.Thread.start",
            "os.mkdir",
            "os.unlink",
            "os.rename",
            "os.replace",
        )
        with ExitStack() as stack:
            guards = [
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
                for target in targets
            ]
            for module in modules:
                with self.subTest(module=module.__name__):
                    module.self_evaluate()
            for guard in guards:
                guard.assert_not_called()

    def test_registry_source_gate_recognizes_owned_hooks(self):
        from integration_registry import REGISTRY

        for name in MODULES:
            with self.subTest(module=name):
                self.assertTrue(REGISTRY._has_self_evaluate(REGISTRY.get(name)))

    def test_reports_return_detached_containers(self):
        for name in MODULES:
            with self.subTest(module=name):
                module = load(name)
                first = module.self_evaluate()
                first["known_limitations"].append("MUTATED")
                self.assertNotIn("MUTATED", module.self_evaluate()["known_limitations"])


class AvatarSignalsHookTests(unittest.TestCase):
    def test_probe_uses_real_generated_channels_and_bounds(self):
        module = load("avatar_signals")
        original = module.build_signals
        with patch.object(module, "build_signals", wraps=original) as build:
            result = module.self_evaluate()
        self.assertGreaterEqual(build.call_count, 2)
        probe = result["offline_probe"]
        self.assertEqual(probe["channel_types"], ["expression", "gaze", "idle", "posture"])
        self.assertGreater(probe["event_count"], 0)
        self.assertTrue(probe["within_declared_ranges"])
        self.assertTrue(probe["invalid_sample_rate_rejected"])

    def test_probe_detects_changed_signal_output_not_static_success(self):
        module = load("avatar_signals")
        original = module.build_signals

        def out_of_bounds(*args: Any, **kwargs: Any):
            values = original(*args, **kwargs)
            values[0]["parameters"]["care"] = 99.0
            return values

        with patch.object(module, "build_signals", side_effect=out_of_bounds):
            self.assertFalse(module.self_evaluate()["offline_probe"]["within_declared_ranges"])

    def test_report_uses_live_sampling_limits(self):
        module = load("avatar_signals")
        with patch.object(module, "MAX_SEGMENTS", 17):
            self.assertEqual(module.self_evaluate()["limits"]["max_segments"], 17)


class AvatarStreamHookTests(unittest.TestCase):
    def test_callback_probe_exercises_clamp_and_user_ignore(self):
        result = load("avatar_stream").self_evaluate()
        self.assertEqual(result["offline_probe"]["assistant_level_after_clamp"], 1.0)
        self.assertTrue(result["offline_probe"]["user_level_ignored"])
        self.assertTrue(result["offline_probe"]["nonfinite_level_rejected"])

    def test_live_stream_limits_are_not_cached(self):
        module = load("avatar_stream")
        with patch.object(module, "PUSH_INTERVAL", 0.4), patch.object(module, "MAX_CONNECTIONS", 7):
            result = module.self_evaluate()["limits"]
        self.assertEqual(result["push_interval_seconds"], 0.4)
        self.assertEqual(result["max_connections"], 7)

    def test_bad_callback_is_visible_not_reported_success(self):
        module = load("avatar_stream")
        with patch.object(module.AvatarSource, "on_audio_level", return_value=None):
            probe = module.self_evaluate()["offline_probe"]
        self.assertEqual(probe["assistant_level_after_clamp"], 0.0)
        self.assertFalse(probe["nonfinite_level_rejected"])


class WebServerHookTests(unittest.TestCase):
    def test_real_id_and_header_authorization_probes(self):
        probe = load("web_server").self_evaluate()["offline_probe"]
        for key in (
            "valid_uuid_accepted",
            "invalid_uuid_rejected",
            "same_origin_accepted",
            "cross_origin_rejected",
            "wrong_host_rejected",
        ):
            self.assertTrue(probe[key], key)

    def test_validator_regression_is_observable(self):
        module = load("web_server")
        with patch.object(module, "valid_id", return_value=True):
            self.assertFalse(module.self_evaluate()["offline_probe"]["invalid_uuid_rejected"])

    def test_auth_regression_is_observable(self):
        module = load("web_server")
        with patch.object(module.Handler, "authorized", return_value=True):
            result = module.self_evaluate()["offline_probe"]
        self.assertFalse(result["cross_origin_rejected"])
        self.assertFalse(result["wrong_host_rejected"])

    def test_static_routes_use_live_allowlist_and_detached_copy(self):
        module = load("web_server")
        with patch.object(module, "STATIC", {"/test.js": ("test.js", "text/javascript")}):
            result = module.self_evaluate()
        self.assertEqual(result["static_routes"], ["/test.js"])
        self.assertIn("loopback", " ".join(result["known_limitations"]).lower())


class VerifyAvatarWebHookTests(unittest.TestCase):
    def test_import_does_not_import_test_config_or_allocate_evidence(self):
        import builtins

        original = builtins.__import__

        def guarded(name: str, *args: Any, **kwargs: Any):
            if name == "test_chat_launcher":
                raise AssertionError("test config import allocates evidence")
            return original(name, *args, **kwargs)

        module = load("verify_avatar_web")
        with patch("builtins.__import__", side_effect=guarded):
            importlib.reload(module)

    def test_dependency_inspection_does_not_construct_runtime(self):
        module = load("verify_avatar_web")
        with (
            patch.object(module, "DevServer", side_effect=AssertionError("server")) as server,
            patch.object(module, "AffectEngine", side_effect=AssertionError("affect")) as affect,
            patch.object(module, "MindStore", side_effect=AssertionError("mind")) as mind,
        ):
            result = module.self_evaluate()
        for constructor in (server, affect, mind):
            constructor.assert_not_called()
        self.assertTrue(result["entrypoint_callable"])
        self.assertFalse(result["runtime_verified"])

    def test_missing_dependency_is_reported_as_missing(self):
        module = load("verify_avatar_web")
        with patch.object(module, "MindStore", None):
            result = module.self_evaluate()
        self.assertFalse(result["dependency_callables"]["MindStore"])

    def test_fixture_is_explicitly_not_resident_or_browser_verification(self):
        result = load("verify_avatar_web").self_evaluate()
        self.assertEqual(result["scope"], "offline fixture dependency inspection only")
        self.assertTrue(result["fixture_only"])
        self.assertIn("browser", " ".join(result["known_limitations"]).lower())


if __name__ == "__main__":
    unittest.main()
