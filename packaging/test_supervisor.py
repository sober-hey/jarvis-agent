"""Real-process lifecycle tests; all writes use disposable directories."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

ENTRY = Path(__file__).with_name("supervisor.py")
ADAPTER = Path(__file__).with_name("supervisor_components.py")


def eventually(predicate: Callable[[], bool], timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("condition did not become true")


class SupervisorTests(unittest.TestCase):
    root: Path
    process: subprocess.Popen[bytes] | None
    folder: tempfile.TemporaryDirectory[str]

    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory(prefix="jarvis-supervisor-test-")
        self.root = Path(self.folder.name)
        self.process = None

    def tearDown(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.folder.cleanup()

    def component(self, name: str = "inner-placeholder", code: str | None = None) -> dict[str, Any]:
        heartbeat = self.root / (name + ".heartbeat")
        command = [
            sys.executable,
            "-B",
            str(ADAPTER),
            "fake",
            "--heartbeat",
            str(heartbeat),
            "--interval",
            "0.03",
        ]
        if code is not None:
            command = [sys.executable, "-B", "-c", code]
        return {
            "name": name,
            "command": command,
            "health": {
                "kind": "heartbeat",
                "target": str(heartbeat),
                "timeout": 0.2,
                "interval": 0.03,
                "startup_grace": 0.3,
            },
            "restart": {
                "base_delay": 0.08,
                "max_delay": 60,
                "crash_limit": 4,
                "crash_window": 5,
                "stable_after": 2,
            },
            "stop_timeout": 0.2,
        }

    def start(self, components: list[dict[str, Any]]) -> None:
        config = self.root / "components.json"
        config.write_text(json.dumps({"components": components}))
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(ENTRY),
                "run",
                "--data-root",
                str(self.root),
                "--components",
                str(config),
                "--tick",
                "0.02",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def status(self) -> dict[str, Any]:
        try:
            return json.loads((self.root / "supervisor-status.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def events(self) -> list[dict[str, Any]]:
        path = self.root / "supervisor.log"
        return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []

    def test_default_components_replace_placeholder_with_resident_daemon(self) -> None:
        from daemon_wiring import component
        from supervisor_components import Component, Health, default_components

        config = self.root / "unopened-config.json"
        key_file = self.root / "unopened-key"
        components = default_components(self.root, config, key_file, 8765)
        self.assertEqual([spec.name for spec in components], ["web", "jarvis-daemon"])
        self.assertEqual(components[1], component(self.root, config=config, key_file=key_file))
        self.assertEqual(
            components[0],
            Component(
                "web",
                (
                    sys.executable,
                    "-B",
                    str(ENTRY.with_name("web_server.py")),
                    "--config",
                    str(config),
                    "--key-file",
                    str(key_file),
                    "--data-dir",
                    str(self.root),
                    "--port",
                    "8765",
                ),
                Health("http", "http://127.0.0.1:8765/api/health"),
            ),
        )
        acceptance = default_components(
            self.root, config, key_file, 8765, acceptance_mode=True, test_port=9999
        )
        self.assertEqual(
            acceptance[0].command,
            (*components[0].command, "--acceptance-mode", "--test-local-port", "9999"),
        )
        self.assertEqual(acceptance[1], components[1])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_default_component_imports_work_in_both_orders_in_fresh_processes(self) -> None:
        for first, second in (
            ("supervisor_components", "daemon_wiring"),
            ("daemon_wiring", "supervisor_components"),
        ):
            with self.subTest(first=first):
                code = (
                    f"import {first}; import {second}; "
                    "from pathlib import Path; "
                    "from supervisor_components import default_components; "
                    "specs=default_components("
                    "Path('/unopened'),Path('/config'),Path('/key'),8765); "
                    "assert [s.name for s in specs] == ['web','jarvis-daemon']"
                )
                result = subprocess.run(
                    [sys.executable, "-B", "-c", code],
                    cwd=ENTRY.parent,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_heartbeat_child_is_healthy_and_stops(self) -> None:
        self.assertTrue(ENTRY.exists(), "supervisor implementation is missing")
        self.start([self.component()])
        eventually(
            lambda: bool(
                self.status()
                .get("components", {})
                .get("inner-placeholder", {})
                .get("last_heartbeat")
            )
        )
        state = self.status()
        child = state["components"]["inner-placeholder"]
        self.assertGreater(child["pid"], 0)
        self.assertGreaterEqual(child["uptime"], 0)
        self.assertEqual(child["restart_count"], 0)
        assert self.process is not None
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=8), 0)
        self.assertEqual(self.status()["state"], "stopped")
        with self.assertRaises(ProcessLookupError):
            os.kill(child["pid"], 0)

    def test_crashes_back_off_exponentially_and_open_circuit(self) -> None:
        crashing = self.component(code="raise SystemExit(7)")
        crashing["restart"]["crash_window"] = 60
        crashing["restart"]["stable_after"] = 60
        self.start([crashing])
        eventually(
            lambda: (
                self.status().get("components", {}).get("inner-placeholder", {}).get("state")
                == "circuit_open"
            ),
            timeout=20,
        )
        events = self.events()
        delays = [row["delay"] for row in events if row["event"] == "restart_scheduled"]
        self.assertEqual(delays, [0.08, 0.16, 0.32])
        starts = [row for row in events if row["event"] == "component_started"]
        self.assertEqual(len(starts), 4)
        time.sleep(0.25)
        self.assertEqual(
            len([row for row in self.events() if row["event"] == "component_started"]), 4
        )
        self.assertIn(
            "process exited", self.status()["components"]["inner-placeholder"]["last_error"]
        )
        self.assertEqual(self.status()["components"]["inner-placeholder"]["restart_count"], 3)

    def test_missing_heartbeat_times_out_and_restarts(self) -> None:
        self.start([self.component(code="import time; time.sleep(30)")])
        eventually(
            lambda: any(
                row["event"] == "restart_scheduled" and "health timeout" in row["reason"]
                for row in self.events()
            )
        )
        eventually(
            lambda: (
                self.status()
                .get("components", {})
                .get("inner-placeholder", {})
                .get("restart_count", 0)
                >= 1
            )
        )

    def test_frozen_heartbeat_times_out(self) -> None:
        heartbeat = self.root / "inner-placeholder.heartbeat"
        code = (
            f"import pathlib,time; pathlib.Path({str(heartbeat)!r}).write_text('once'); "
            "time.sleep(30)"
        )
        self.start([self.component(code=code)])
        eventually(
            lambda: any(
                row["event"] == "restart_scheduled" and "health timeout" in row["reason"]
                for row in self.events()
            )
        )

    def test_previous_generation_heartbeat_is_rejected(self) -> None:
        heartbeat = self.root / "inner-placeholder.heartbeat"
        heartbeat.write_text("old generation")
        os.utime(heartbeat, (time.time() - 1, time.time() - 1))
        self.start([self.component(code="import time; time.sleep(30)")])
        eventually(lambda: any(row["event"] == "restart_scheduled" for row in self.events()))
        self.assertIsNone(self.status()["components"]["inner-placeholder"]["last_heartbeat"])

    def test_duplicate_start_is_rejected_and_status_is_nonblocking_readonly(self) -> None:
        self.start([self.component()])
        eventually(lambda: self.status().get("state") == "running")
        config = self.root / "components.json"
        duplicate = subprocess.run(
            [
                sys.executable,
                str(ENTRY),
                "run",
                "--data-root",
                str(self.root),
                "--components",
                str(config),
            ],
            capture_output=True,
            timeout=2,
        )
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn(b"AlreadyRunning", duplicate.stderr)
        result = subprocess.run(
            [sys.executable, str(ENTRY), "status", "--data-root", str(self.root)],
            capture_output=True,
            timeout=1,
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)["owner_alive"])
        from unittest.mock import patch

        import supervisor

        before = (self.root / "supervisor-status.json").stat().st_mtime_ns
        with (
            patch.object(
                supervisor.fcntl, "flock", side_effect=AssertionError("status took owner lock")
            ),
            patch.object(
                supervisor, "atomic_json", side_effect=AssertionError("status wrote state")
            ),
        ):
            self.assertTrue(supervisor.read_status(self.root)["owner_alive"])
        self.assertGreaterEqual((self.root / "supervisor-status.json").stat().st_mtime_ns, before)

    def test_status_on_absent_root_creates_nothing(self) -> None:
        import supervisor

        absent = self.root / "absent"
        self.assertEqual(supervisor.read_status(absent)["state"], "not_started")
        self.assertFalse(absent.exists())

    def test_sigterm_kills_term_ignoring_grandchild(self) -> None:
        pid_file = self.root / "grandchild.pid"
        code = (
            "import subprocess,sys,time,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import signal,time; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']); "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(60)"
        )
        self.start([self.component(code=code)])
        eventually(pid_file.exists)
        child = int(pid_file.read_text())
        time.sleep(0.1)
        assert self.process is not None
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=8), 0)

        def gone() -> bool:
            result = subprocess.run(
                ["/bin/ps", "-p", str(child), "-o", "stat="], capture_output=True, text=True
            )
            return not result.stdout.strip() or result.stdout.strip().startswith("Z")

        eventually(gone)
        import supervisor

        lock = supervisor.OwnerLock(self.root)
        lock.acquire()
        lock.release()

    def test_sigint_also_stops_cleanly(self) -> None:
        self.start([self.component()])
        eventually(lambda: self.status().get("state") == "running")
        assert self.process is not None
        self.process.send_signal(signal.SIGINT)
        self.assertEqual(self.process.wait(timeout=8), 0)
        self.assertEqual(self.status()["state"], "stopped")

    def test_partial_start_failure_reclaims_first_child(self) -> None:
        broken = self.component("broken")
        broken["command"] = ["/definitely-missing-jarvis-command"]
        self.start([self.component(), broken])
        assert self.process is not None
        self.assertEqual(self.process.wait(timeout=8), 1)
        starts = [row for row in self.events() if row["event"] == "component_started"]
        self.assertEqual(len(starts), 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(starts[0]["pid"], 0)
        self.assertEqual(self.status()["state"], "failed")

    def test_sigkill_child_is_restarted_with_new_pid(self) -> None:
        self.start([self.component()])
        eventually(
            lambda: (
                self.status().get("components", {}).get("inner-placeholder", {}).get("state")
                == "healthy"
            )
        )
        pid = self.status()["components"]["inner-placeholder"]["pid"]
        os.kill(pid, signal.SIGKILL)
        eventually(
            lambda: (
                self.status().get("components", {}).get("inner-placeholder", {}).get("state")
                == "healthy"
                and self.status()["components"]["inner-placeholder"]["pid"] != pid
            )
        )
        self.assertEqual(self.status()["components"]["inner-placeholder"]["restart_count"], 1)

    def test_identity_rejects_pid_reuse(self) -> None:
        from dataclasses import replace

        import supervisor

        identity = supervisor.process_table()[os.getpid()]
        self.assertTrue(supervisor.same_identity(identity, identity))
        self.assertFalse(
            supervisor.same_identity(identity, replace(identity, started="different start"))
        )
        self.assertFalse(
            supervisor.same_identity(identity, replace(identity, command_hash="different command"))
        )
        self.assertFalse(supervisor.same_identity(identity, None))

    def test_second_claim_refuses_verified_live_owner_even_if_lock_was_released(self) -> None:
        import supervisor

        first = supervisor.OwnerLock(self.root)
        first.acquire()
        first.release()
        second = supervisor.OwnerLock(self.root)
        with self.assertRaises(supervisor.AlreadyRunning):
            second.acquire()

    def test_partial_start_identity_failure_reclaims_spawn(self) -> None:
        from unittest.mock import patch

        import supervisor
        from supervisor_components import Component, Health

        runtime = supervisor.Runtime(
            Component(
                "injected",
                (sys.executable, "-c", "import time; time.sleep(60)"),
                Health("heartbeat", str(self.root / "beat")),
                stop_timeout=0.1,
            )
        )
        worker = supervisor.Supervisor(self.root, (runtime.spec,), tick=0.02)
        real_table = supervisor.process_table
        calls = 0

        def once() -> dict[int, supervisor.Identity]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("injected identity lookup failure")
            return real_table()

        with patch.object(supervisor, "process_table", side_effect=once):
            with self.assertRaisesRegex(RuntimeError, "injected identity"):
                worker.launch(runtime)
        self.assertIsNone(runtime.process)
        stopped = [row for row in self.events() if row["event"] == "component_stopped"]
        self.assertEqual(len(stopped), 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(stopped[0]["pid"], 0)

    def test_http_malformed_health_is_failure_not_supervisor_crash(self) -> None:
        import http.server
        import threading

        import supervisor_components as adapters

        class Broken(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"[]")

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Broken)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            health = adapters.Health("http", f"http://127.0.0.1:{server.server_port}/api/health")
            self.assertFalse(adapters.probe(health, time.time())[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_crash_after_leader_exit_reclaims_its_group(self) -> None:
        pid_file = self.root / "leader-exit-grandchild.pid"
        code = (
            "import subprocess,sys,pathlib,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(0.1)"
        )
        self.start([self.component(code=code)])
        eventually(pid_file.exists)
        pid = int(pid_file.read_text())
        eventually(lambda: any(row["event"] == "restart_scheduled" for row in self.events()))

        def gone() -> bool:
            result = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
            )
            return not result.stdout.strip() or result.stdout.strip().startswith("Z")

        eventually(gone)

    def test_heartbeat_timeout_is_measured_from_written_heartbeat(self) -> None:
        from unittest.mock import patch

        import supervisor
        from supervisor_components import Component, Health

        runtime = supervisor.Runtime(
            Component("once", (sys.executable,), Health("heartbeat", str(self.root / "once")))
        )
        worker = supervisor.Supervisor(self.root, (runtime.spec,))
        with (
            patch.object(supervisor.time, "monotonic", return_value=50),
            patch.object(supervisor.time, "time", return_value=100),
        ):
            worker.mark_healthy(runtime, 99.5)
        self.assertEqual(runtime.last_ok, 49.5)
        self.assertEqual(runtime.last_heartbeat, 99.5)
        self.assertEqual(runtime.state, "healthy")
        # Re-reading the same heartbeat must not renew the producer's timestamp.
        with (
            patch.object(supervisor.time, "monotonic", return_value=50.2),
            patch.object(supervisor.time, "time", return_value=100.2),
        ):
            worker.mark_healthy(runtime, 99.5)
        self.assertEqual(runtime.last_ok, 49.5)

    def test_restart_delay_caps_at_sixty_seconds(self) -> None:
        import supervisor
        from supervisor_components import Component, Health, Restart

        runtime = supervisor.Runtime(
            Component(
                "cap",
                (sys.executable,),
                Health("heartbeat", str(self.root / "beat")),
                Restart(base_delay=10, max_delay=60, crash_limit=100, stable_after=1000),
            )
        )
        runtime.started = time.monotonic()
        worker = supervisor.Supervisor(self.root, (runtime.spec,))
        for _ in range(5):
            worker.failed(runtime, "synthetic failure")
        self.assertEqual([row["delay"] for row in self.events()], [10, 20, 40, 60, 60])

    def test_stable_runtime_resets_crash_streak(self) -> None:
        import supervisor
        from supervisor_components import Component, Health, Restart

        runtime = supervisor.Runtime(
            Component(
                "stable",
                (sys.executable,),
                Health("heartbeat", str(self.root / "beat")),
                Restart(base_delay=1, stable_after=1),
            )
        )
        worker = supervisor.Supervisor(self.root, (runtime.spec,))
        runtime.streak = 9
        runtime.failures.extend([time.monotonic()] * 4)
        runtime.started = time.monotonic() - 2
        worker.failed(runtime, "after stable uptime")
        self.assertEqual(runtime.state, "backoff")
        self.assertEqual(self.events()[-1]["delay"], 1)

    def test_invalid_command_manifest_is_rejected_before_start(self) -> None:
        import supervisor_components as adapters

        component = self.component()
        component["command"] = "python -c pass"
        config = self.root / "bad.json"
        config.write_text(json.dumps({"components": [component]}))
        with self.assertRaises(ValueError):
            adapters.load_components(config)
        component["command"] = [sys.executable, 42]
        config.write_text(json.dumps({"components": [component]}))
        with self.assertRaises(ValueError):
            adapters.load_components(config)

    def test_lock_stale_pid_with_different_start_time_does_not_block(self) -> None:
        from dataclasses import asdict, replace

        import supervisor

        identity = replace(supervisor.process_table()[os.getpid()], started="stale PID generation")
        (self.root / "supervisor-owner.json").write_text(json.dumps(asdict(identity)))
        lock = supervisor.OwnerLock(self.root)
        lock.acquire()
        lock.release()

    def test_second_start_after_clean_shutdown_preserves_append_only_events(self) -> None:
        self.start([self.component()])
        eventually(lambda: self.status().get("state") == "running")
        assert self.process is not None
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=8), 0)
        original = (self.root / "supervisor.log").read_bytes()
        self.start([self.component()])
        eventually(lambda: self.status().get("state") == "running")
        self.assertTrue((self.root / "supervisor.log").read_bytes().startswith(original))

    def test_http_hang_is_bounded(self) -> None:
        import http.server
        import threading

        import supervisor_components as adapters

        class Hanging(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                time.sleep(0.5)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hanging)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            started = time.monotonic()
            health = adapters.Health(
                "http", f"http://127.0.0.1:{server.server_port}/", request_timeout=0.05
            )
            self.assertFalse(adapters.probe(health, time.time())[0])
            self.assertLess(time.monotonic() - started, 0.3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_descendant_spawned_during_grace_is_killed(self) -> None:
        pid_file = self.root / "late-grandchild.pid"
        code = (
            "import signal,subprocess,sys,pathlib,time\n"
            "def spawn(signum,frame):\n"
            " p=subprocess.Popen([sys.executable,'-c',"
            "'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])\n"
            f" pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))\n"
            " time.sleep(0.05)\n"
            " sys.exit(0)\n"
            "signal.signal(signal.SIGTERM,spawn)\n"
            "time.sleep(60)\n"
        )
        self.start([self.component(code=code)])
        eventually(lambda: self.status().get("state") == "running")
        time.sleep(0.08)
        assert self.process is not None
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=8), 0)
        self.assertTrue(pid_file.exists())
        pid = int(pid_file.read_text())

        def gone() -> bool:
            state = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
            ).stdout.strip()
            return not state or state.startswith("Z")

        try:
            eventually(gone, timeout=1)
        finally:
            if not gone():
                os.kill(pid, signal.SIGKILL)

    def test_sigterm_while_http_health_is_hung_stops(self) -> None:
        import http.server
        import threading

        class Hanging(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                time.sleep(0.6)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hanging)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            component = self.component(code="import time; time.sleep(60)")
            component["health"] = {
                "kind": "http",
                "target": f"http://127.0.0.1:{server.server_port}/",
                "timeout": 1,
                "startup_grace": 1,
                "interval": 0.02,
                "request_timeout": 0.1,
            }
            self.start([component])
            eventually(lambda: bool(self.status().get("components")))
            assert self.process is not None
            self.process.terminate()
            self.assertEqual(self.process.wait(timeout=3), 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_nested_descendant_session_is_cleaned(self) -> None:
        pid_file = self.root / "nested.pid"
        code = (
            "import subprocess,sys,time,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import signal,time; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'],"
            "start_new_session=True); "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(60)"
        )
        self.start([self.component(code=code)])
        eventually(pid_file.exists)
        pid = int(pid_file.read_text())
        time.sleep(0.1)
        assert self.process is not None
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=8), 0)

        def gone() -> bool:
            state = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
            ).stdout.strip()
            return not state or state.startswith("Z")

        try:
            eventually(gone)
        finally:
            if not gone():
                os.kill(pid, signal.SIGKILL)

    def test_reused_witness_pid_cannot_adopt_unrelated_children(self) -> None:
        from unittest.mock import Mock

        import supervisor
        from supervisor_components import Component, Health

        leader = supervisor.Identity(999990, 1, 999990, os.getuid(), "old", "leader")
        witnessed = supervisor.Identity(999991, 999990, 999991, os.getuid(), "old", "worker")
        reused = supervisor.Identity(999991, 1, 999991, os.getuid(), "new", "stranger")
        unrelated = supervisor.Identity(999992, 999991, 999991, os.getuid(), "new", "unrelated")
        runtime = supervisor.Runtime(
            Component("reuse", (sys.executable,), Health("heartbeat", str(self.root / "beat")))
        )
        runtime.process = Mock(pid=leader.pid)
        runtime.identity = leader
        runtime.witnessed[witnessed.pid] = witnessed
        worker = supervisor.Supervisor(self.root, (runtime.spec,))
        worker.observe(runtime, {reused.pid: reused, unrelated.pid: unrelated})
        self.assertNotIn(unrelated.pid, runtime.witnessed)


if __name__ == "__main__":
    unittest.main()
