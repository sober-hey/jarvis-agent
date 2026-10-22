"""Product engine behavior tests; stdlib-only runner, no vendor test environment.
Run: packaging/runtime/engine-venv/bin/python3 -I -S -B packaging/test_engine_entry.py
"""

import functools
import importlib.util
import json
import os
import subprocess
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import engine_entry as entry_types

ENTRY = Path(__file__).with_name("engine_entry.py")
spec = importlib.util.spec_from_file_location("jarvis_engine_entry", ENTRY)
if spec is None or spec.loader is None:
    raise RuntimeError("Entry module loader unavailable")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
engine = module
# This runner also works under -I -S, where sibling imports are unavailable.
_support_spec = importlib.util.spec_from_file_location(
    "packaging_evidence_support", ENTRY.with_name("evidence_support.py")
)
if _support_spec is None or _support_spec.loader is None:
    raise RuntimeError("Evidence support loader unavailable")
_support = importlib.util.module_from_spec(_support_spec)
_support_spec.loader.exec_module(_support)
if TYPE_CHECKING:
    engine = entry_types
engine.RUNS = _support.evidence_directory("engine-entry")


def _in_product_interpreter() -> bool:
    try:
        engine.interpreter_check()
    except RuntimeError:
        return False
    return True


def _run_in_product_interpreter(method: str) -> subprocess.CompletedProcess[str]:
    """Run one test method under the pinned product interpreter."""
    return subprocess.run(
        [
            str(engine.PYTHON),
            "-I",
            "-S",
            "-B",
            str(Path(__file__).resolve()),
            f"EngineEntryTests.{method}",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def product_interpreter(method: Callable[..., None]) -> Callable[..., None]:
    """Move the interpreter, never the assertion.

    Original intent: this module is a stdlib-only runner for
    packaging/runtime/engine-venv/bin/python3 (see the docstring), and every
    engine.self_test() call fails closed with RuntimeError unless sys.prefix is
    the product venv (engine_entry.interpreter_check). load_tests() below
    re-dispatched the whole class for host-interpreter discovery, but pytest
    never calls load_tests, so six cases died at engine_entry.py:113 without
    evaluating a single assertion. Wrapping each self_test case re-enters the
    product interpreter for that one method: the original body, with its
    original assertions, runs unchanged in the child, and the parent only
    requires that child to pass.
    """

    @functools.wraps(method)
    def wrapper(self: unittest.TestCase, *args: object, **kwargs: object) -> None:
        if _in_product_interpreter():
            return method(self, *args, **kwargs)
        result = _run_in_product_interpreter(method.__name__)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 1 test", result.stderr)
        self.assertIn("\nOK", result.stderr)
        self.assertNotIn("skipped", result.stderr)
        return None

    return cast(Callable[..., None], wrapper)


class EngineEntryTests(unittest.TestCase):
    @product_interpreter
    def test_real_vendor_imports_and_no_host_or_install(self) -> None:
        result = engine.self_test()
        (engine.RUNS / "self-test.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["agent_constructed"])
        self.assertFalse(result["session_db_constructed"])
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["host_module_sources"], {})
        self.assertEqual(result["unexpected_module_sources"], {})
        self.assertEqual(result["adapter_aliases_remaining"], [])
        self.assertFalse(result["bootstrap_network_hooks_remaining"])
        self.assertEqual(sum(result["policy_counts"].values()), 0)
        for name, relative in [("AIAgent", "run_agent.py"), ("SessionDB", "hermes_state.py")]:
            self.assertEqual(Path(result["symbols"][name]["module"]), engine.VENDOR / relative)
            self.assertTrue(result["symbols"][name]["signature"].startswith("("))
        self.assertFalse(result["sdk_initialization_unmodified"])
        self.assertTrue(result["private_run_removed"])
        self.assertFalse(Path(result["command"][-1]).exists())

    @product_interpreter
    def test_real_construct_close_and_persistent_reopen(self) -> None:
        result = engine.self_test(construct=True)
        (engine.RUNS / "construct-test.json").write_text(json.dumps(result, indent=2))
        self.assertTrue(result["ok"], result.get("error"))
        for key in (
            "agent_constructed",
            "agent_closed",
            "session_db_constructed",
            "session_db_closed",
            "session_db_reopened",
            "session_persisted",
        ):
            self.assertTrue(result[key], key)
        self.assertEqual(result["tool_count"], 0)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["policy_counts"]["process_attempts"], 0)
        self.assertEqual(result["policy_counts"]["network_attempts"], 0)
        self.assertFalse(result["sdk_initialization_unmodified"])

    @product_interpreter
    def test_model_request_is_denied_on_real_constructed_client(self) -> None:
        result = engine.self_test(construct=True, probe_model=True)
        (engine.RUNS / "model-denial.json").write_text(json.dumps(result, indent=2))
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result["model_request_denied"])
        self.assertGreater(result["policy_counts"]["network_attempts"], 0)
        self.assertEqual(result["model_calls"], 0)
        self.assertTrue(result["agent_closed"])
        self.assertTrue(result["session_persisted"])

    @product_interpreter
    def test_missing_dependency_exits_without_install_or_fallback(self) -> None:
        result = engine.self_test(omit_dependencies=True)
        (engine.RUNS / "missing-dependency.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        self.assertFalse(result["ok"])
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual(result["error"]["type"], "ModuleNotFoundError")
        self.assertTrue(result["error"]["missing_module"])
        self.assertEqual(result["host_module_sources"], {})
        self.assertEqual(sum(result["policy_counts"].values()), 0)

    @product_interpreter
    def test_inherited_host_environment_is_not_forwarded(self) -> None:
        prior = dict(os.environ)
        try:
            os.environ.update(
                {
                    "OPENAI_API_KEY": "nonsecret-test-sentinel",
                    "HTTPS_PROXY": "http://invalid.example",
                    "PYTHONPATH": str(engine.HOST),
                    "HERMES_HOME": str(engine.HOST / ".hermes"),
                }
            )
            result = engine.self_test()
        finally:
            os.environ.clear()
            os.environ.update(prior)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertNotIn("OPENAI_API_KEY", result["environment_keys"])
        self.assertNotIn("HTTPS_PROXY", result["environment_keys"])
        self.assertNotIn("PYTHONPATH", result["environment_keys"])
        self.assertNotIn("nonsecret-test-sentinel", json.dumps(result))
        self.assertNotEqual(result["environment"]["HERMES_HOME"], str(engine.HOST / ".hermes"))

    def test_policy_blocks_real_socket_and_installer_before_execution(self) -> None:
        # Separate process because Python audit hooks intentionally cannot be removed.
        code = """
import importlib.util,sys,json,socket,subprocess
from pathlib import Path
spec=importlib.util.spec_from_file_location('engine',sys.argv[1])
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
p=m.OfflinePolicy(Path(sys.argv[2])); sys.addaudithook(p.audit)
for action in (lambda: socket.create_connection(('127.0.0.1',9)),
               lambda: subprocess.run(['/usr/bin/false']),
               lambda: open(m.HOST/'never-read')):
    try: action()
    except PermissionError: pass
    else: raise AssertionError('policy failed open')
print(json.dumps(p.counts()))
"""
        result = subprocess.run(
            [str(engine.PYTHON), "-I", "-S", "-B", "-c", code, str(ENTRY), str(engine.RUNS)],
            env=engine.environment(engine.RUNS),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        counts = json.loads(result.stdout)
        self.assertGreater(counts["network_attempts"], 0)
        self.assertEqual(counts["process_attempts"], 1)
        self.assertEqual(counts["host_access_attempts"], 1)

    @product_interpreter
    def test_timeout_kills_and_reaps_private_process(self) -> None:
        result = engine.self_test(timeout=0.001)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "TimeoutExpired")
        self.assertTrue(result["child_reaped"])
        self.assertFalse(Path(result["command"][-1]).exists())

    def test_missing_isolation_flags_fail_closed(self) -> None:
        result = subprocess.run(
            [str(engine.PYTHON), "-B", str(ENTRY), "--self-test"],
            env=engine.environment(engine.RUNS),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Requires -I -S -B", json.loads(result.stdout)["error"]["detail"])


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    try:
        engine.interpreter_check()
    except RuntimeError:
        return unittest.TestSuite(
            isolated_case(name) for name in loader.getTestCaseNames(EngineEntryTests)
        )
    return tests


def isolated_case(method: str) -> unittest.TestCase:
    class IsolatedEngineCase(unittest.TestCase):
        """Run every original assertion under its required product interpreter."""

        def id(self) -> str:
            return f"{__name__}.EngineEntryTests.{method}"

        def runTest(self) -> None:
            result = _run_in_product_interpreter(method)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Ran 1 test", result.stderr)
            self.assertNotIn("skipped", result.stderr)

    return IsolatedEngineCase()


if __name__ == "__main__":
    unittest.main(verbosity=2)
