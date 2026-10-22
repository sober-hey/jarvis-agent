"""Isolated acceptance tests; never writes existing project files."""

import importlib.util
import sys
import unittest
import os
from pathlib import Path

SOURCE = Path(__file__).with_name("shell_executor.py")


class ShellExecutorTests(unittest.TestCase):
    def executor(self, **kwargs):
        self.assertTrue(SOURCE.is_file(), "shell_executor.py is not implemented")
        spec = importlib.util.spec_from_file_location("shell_executor_under_test", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.ShellExecutor(**kwargs)

    def test_execute_echo(self):
        executor = self.executor(allowed_roots=[SOURCE.parent.parent])
        result = executor.execute("echo hello", timeout=5)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].strip(), "hello")
        self.assertEqual(result["stderr"], "")
        self.assertIsInstance(result["duration_ms"], float)

    def test_safety(self):
        executor = self.executor()
        check = getattr(executor, "check_safety", None)
        self.assertTrue(callable(check), "check_safety is not implemented")
        for command in [
            "rm -rf /",
            "rm -fr -- /",
            "/bin/rm -r -f //",
            "dd if=/dev/zero of=/dev/disk0",
            "mkfs.ext4 /dev/x",
            "shutdown -h now",
            "env X=y sudo ls",
            'sh -c "rm -rf /"',
        ]:
            self.assertFalse(check(command)["safe"], command)
        self.assertEqual(check("sudo ls")["reason"], "requires sudo")
        self.assertEqual(check("ls -la"), {"safe": True})
        self.assertEqual(check("echo sudo"), {"safe": True})
        self.assertEqual(check('python3 -c "print(1+1)"'), {"safe": True})
        result = executor.execute("sudo echo no")
        self.assertEqual(result["exit_code"], -1)
        self.assertEqual(result["stderr"], "requires sudo")

    def test_sandbox_only_allowed_host_writes(self):
        import shlex
        import tempfile

        executor = self.executor()
        sandboxed = getattr(executor, "execute_sandboxed", None)
        self.assertTrue(callable(sandboxed), "execute_sandboxed is not implemented")
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory).resolve()
            allowed = root / "allowed"
            allowed.mkdir()
            outside = root / "outside"
            outside.write_text("original")
            command = shlex.join(
                [
                    sys.executable,
                    "-c",
                    'from pathlib import Path; Path("allowed/yes").write_text("ok")',
                ]
            )
            result = sandboxed(command, [str(allowed)])
            self.assertEqual(result["exit_code"], 0, result)
            self.assertEqual((allowed / "yes").read_text(), "ok")
            # Negative control: the same command CAN write without the sandbox.
            command = shlex.join(
                [
                    sys.executable,
                    "-c",
                    f'from pathlib import Path; Path({str(outside)!r}).write_text("changed")',
                ]
            )
            self.assertEqual(executor.execute(command)["exit_code"], 0)
            outside.write_text("original")
            denied = sandboxed(command, [str(allowed)])
            self.assertNotEqual(denied["exit_code"], 0, denied)
            self.assertEqual(outside.read_text(), "original")
            (allowed / "escape").symlink_to(outside)
            command = shlex.join(
                [
                    sys.executable,
                    "-c",
                    'from pathlib import Path; Path("allowed/escape").write_text("escaped")',
                ]
            )
            self.assertNotEqual(sandboxed(command, [str(allowed)])["exit_code"], 0)
            self.assertEqual(outside.read_text(), "original")

    def test_pip_installs_offline_wheel_in_temporary_venv(self):
        import shlex
        import subprocess
        import tempfile
        import venv
        import zipfile

        executor = self.executor()
        install = getattr(executor, "pip_install", None)
        self.assertTrue(callable(install), "pip_install is not implemented")
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory).resolve()
            venv.EnvBuilder(with_pip=True).create(root / "venv")
            python = root / "venv/bin/python3"
            wheel = root / "shell_executor_probe-0.0.1-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("shell_executor_probe.py", 'VALUE = "installed"\n')
                archive.writestr(
                    "shell_executor_probe-0.0.1.dist-info/METADATA",
                    "Metadata-Version: 2.1\nName: shell-executor-probe\nVersion: 0.0.1\n",
                )
                archive.writestr(
                    "shell_executor_probe-0.0.1.dist-info/WHEEL",
                    "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                )
                archive.writestr("shell_executor_probe-0.0.1.dist-info/RECORD", "")
            result = install(str(wheel), str(python))
            self.assertTrue(result["success"], result)
            readback = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import shell_executor_probe; print(shell_executor_probe.VALUE)",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(readback.returncode, 0, readback.stderr)
            self.assertEqual(readback.stdout.strip(), "installed")
            self.assertFalse(install("--user", str(python))["success"])

    def test_execute_errors_timeout_env_and_no_implicit_shell(self):
        import shlex

        executor = self.executor(default_timeout=1)
        timeout = executor.execute(
            shlex.join(
                [sys.executable, "-c", 'import time; print("before", flush=True); time.sleep(4)']
            )
        )
        self.assertEqual(timeout["exit_code"], -1)
        self.assertEqual(timeout["stderr"], "timeout")
        self.assertIn("before", timeout["stdout"])
        result = executor.execute(
            shlex.join(
                [sys.executable, "-c", 'import os; print(os.environ["SHELL_EXECUTOR_TEST"])']
            ),
            env={"SHELL_EXECUTOR_TEST": "override"},
        )
        self.assertEqual(result["stdout"].strip(), "override")
        literal = executor.execute("echo hello && echo injected")
        self.assertEqual(literal["stdout"].strip(), "hello && echo injected")
        self.assertEqual(executor.execute("missing-shell-executor-test-command")["exit_code"], 127)
        self.assertEqual(executor.execute('echo "unterminated')["exit_code"], -1)
        exit_result = executor.execute(
            shlex.join(
                [sys.executable, "-c", 'import sys; print("error", file=sys.stderr); sys.exit(3)']
            )
        )
        self.assertEqual(exit_result["exit_code"], 3)
        self.assertEqual(exit_result["stderr"].strip(), "error")
        restricted = self.executor(allowed_roots=[SOURCE.parent])
        self.assertEqual(
            restricted.execute("echo denied", cwd=str(SOURCE.parent.parent))["exit_code"], -1
        )
        self.assertEqual(self.executor(allowed_roots=[]).execute("echo denied")["exit_code"], -1)

    def test_sandbox_file_grant_and_read_only_network(self):
        import shlex
        import tempfile
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        executor = self.executor()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"network-ok")

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            code = f'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:{server.server_port}", timeout=3).read().decode())'
            result = executor.execute_sandboxed(shlex.join([sys.executable, "-c", code]))
            self.assertEqual(result["exit_code"], 0, result)
            self.assertEqual(result["stdout"].strip(), "network-ok")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            root = Path(temporary).resolve()
            target = root / "newfile"
            command = shlex.join(
                [
                    sys.executable,
                    "-c",
                    'from pathlib import Path; Path("newfile").write_text("written")',
                ]
            )
            self.assertEqual(executor.execute_sandboxed(command, [str(target)])["exit_code"], 0)
            self.assertEqual(target.read_text(), "written")
            denied = executor.execute_sandboxed(
                shlex.join(
                    [
                        sys.executable,
                        "-c",
                        f'from pathlib import Path; Path({str(root / "sibling")!r}).write_text("bad")',
                    ]
                ),
                [str(target)],
            )
            self.assertNotEqual(denied["exit_code"], 0)
            self.assertFalse((root / "sibling").exists())

    def test_sandbox_missing_backend_and_path_policy_fail_closed(self):
        import tempfile
        from unittest.mock import patch

        executor = self.executor()
        module = sys.modules.get(executor.__class__.__module__)
        # Method globals identify this isolated import without patching product files.
        with patch.dict(
            executor.execute_sandboxed.__globals__,
            sys=type("Platform", (), {"platform": "linux"})(),
        ):
            unavailable = executor.execute_sandboxed("echo must-not-run")
        self.assertEqual(unavailable["exit_code"], -1)
        self.assertIn("sandbox unavailable", unavailable["stderr"])
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            root = Path(temporary).resolve()
            allowed = root / "allowed"
            allowed.mkdir()
            alias = root / "alias"
            alias.symlink_to(allowed)
            restricted = self.executor(allowed_roots=[allowed])
            sibling = root / "allowed-sibling"
            sibling.mkdir()
            self.assertEqual(restricted.execute("echo denied", cwd=str(sibling))["exit_code"], -1)
            self.assertEqual(restricted.execute("echo denied", cwd=str(alias))["exit_code"], -1)
            self.assertEqual(
                executor.execute_sandboxed("echo denied", [str(alias)])["exit_code"], -1
            )
            self.assertEqual(
                executor.execute_sandboxed("echo denied", [str(allowed), str(allowed)])[
                    "exit_code"
                ],
                -1,
            )


class SelfEvaluationTests(unittest.TestCase):
    def test_module_hook_is_offline_and_describes_real_limits(self):
        import builtins
        from contextlib import ExitStack
        from datetime import UTC, datetime, timedelta
        from unittest.mock import patch
        import shell_executor as module

        hook = getattr(module, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-level self_evaluate is required")
        before = datetime.now(UTC)
        # No construction, data/config inspection, process spawn or directory probe.
        with ExitStack() as stack:
            for target in (
                "builtins.open",
                "pathlib.Path.open",
                "pathlib.Path.read_text",
                "pathlib.Path.read_bytes",
                "os.open",
                "os.stat",
                "os.listdir",
                "os.scandir",
                "subprocess.Popen",
                "subprocess.run",
            ):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            stack.enter_context(
                patch.object(module, "ShellExecutor", side_effect=AssertionError("constructor"))
            )
            report = hook()
            another = hook()
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
        self.assertEqual(report["module"], "shell_executor")
        for name in ("domain", "current_version"):
            self.assertIsInstance(report[name], str)
            self.assertTrue(report[name].strip())
        for name in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[name], list)
            self.assertTrue(report[name])
            self.assertTrue(all(isinstance(s, str) and s.strip() for s in report[name]))
        stamp = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.utcoffset(), timedelta(0))
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, datetime.now(UTC))
        report["known_limitations"].append("caller mutation")
        self.assertNotIn("caller mutation", another["known_limitations"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
