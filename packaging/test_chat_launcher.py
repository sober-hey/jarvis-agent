"""Dedicated config launcher: fake HTTP, safe credentials and process cleanup."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from evidence_support import evidence_directory

if TYPE_CHECKING:
    import chat_launcher as launcher_types

ENTRY = Path(__file__).with_name("chat_launcher.py")
_spec = importlib.util.spec_from_file_location("launcher_under_test", ENTRY)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Launcher missing")
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)
if TYPE_CHECKING:
    launcher = launcher_types

EVIDENCE = evidence_directory("chat-launcher")
FAKE_KEY = "FAKE-ONLY-NONSECRET-TEST-KEY"


def config(port: int) -> dict[str, Any]:
    return {
        "model": "fake-test-model",
        "provider": "custom",
        "base_url": f"http://127.0.0.1:{port}/v1",
        "api_mode": "chat_completions",
        "context_length": 256000,
        "reasoning_effort": "high",
    }


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".jarvis-launcher-test-")
        self.folder = Path(self.temp.name)
        self.key = self.folder / "fake-key.txt"
        self.key.write_text(FAKE_KEY + "\n")
        self.key.chmod(0o600)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_lock_wait_acquires_once_after_release_without_early_attempt(self) -> None:
        import fcntl

        root = launcher.prepare_root(self.folder / "voice-lock-wait")
        held = os.open(root / "launcher-session.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finished = threading.Event()
        results: list[dict[str, Any]] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                results.append(
                    launcher.launch(
                        config(8000),
                        FAKE_KEY,
                        "voice",
                        root,
                        "timeline",
                        test_port=8000,
                        source_channel="voice",
                        attempt_id="wait-once",
                        lock_wait_timeout=2.0,
                    )
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        with patch.object(
            launcher, "_launch_once", return_value={"ok": True, "text": "ok"}
        ) as send:
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertFalse(finished.wait(0.15), errors)
                self.assertFalse((root / "launcher-attempts/wait-once.json").exists())
                send.assert_not_called()
            finally:
                os.close(held)
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            send.assert_called_once()
            self.assertEqual(launcher.query_attempt(root, "wait-once")["state"], "completed")

    def test_lock_wait_deadline_has_no_attempt_or_dispatch_and_closes_fd(self) -> None:
        import fcntl

        root = launcher.prepare_root(self.folder / "voice-lock-deadline")
        held = os.open(root / "launcher-session.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        closed: list[int] = []
        real_close = os.close

        def close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        try:
            with (
                patch.object(launcher, "_launch_once") as send,
                patch.object(launcher.os, "close", side_effect=close),
            ):
                started = time.monotonic()
                with self.assertRaisesRegex(launcher.LauncherError, "占用数据根"):
                    launcher.launch(
                        config(8000),
                        FAKE_KEY,
                        "voice",
                        root,
                        None,
                        test_port=8000,
                        attempt_id="expired",
                        lock_wait_timeout=0.1,
                    )
                self.assertGreaterEqual(time.monotonic() - started, 0.1)
                self.assertLess(time.monotonic() - started, 1.0)
                send.assert_not_called()
                self.assertFalse((root / "launcher-attempts/expired.json").exists())
                self.assertGreaterEqual(len(closed), 2)  # sync-directory and lock descriptors
        finally:
            os.close(held)

    def test_lock_wait_invalid_values_do_not_open_root(self) -> None:
        for invalid in (-1.0, 121.0, float("nan"), float("inf"), True):
            with self.subTest(value=invalid), patch.object(launcher, "prepare_root") as prepare:
                with self.assertRaisesRegex(launcher.LauncherError, "等待时间"):
                    launcher.launch(
                        config(8000),
                        FAKE_KEY,
                        "voice",
                        self.folder / "unopened",
                        None,
                        test_port=8000,
                        lock_wait_timeout=invalid,
                    )
                prepare.assert_not_called()

    def test_lock_wait_nonbusy_error_does_not_poll_or_dispatch(self) -> None:
        root = launcher.prepare_root(self.folder / "voice-lock-io-error")
        with (
            patch.object(launcher.fcntl, "flock", side_effect=PermissionError("fixture")) as flock,
            patch.object(launcher.time, "sleep") as sleep,
            patch.object(launcher, "_launch_once") as send,
        ):
            with self.assertRaisesRegex(launcher.LauncherError, "未启动引擎"):
                launcher.launch(
                    config(8000),
                    FAKE_KEY,
                    "voice",
                    root,
                    None,
                    test_port=8000,
                    attempt_id="io-error",
                    lock_wait_timeout=1.0,
                )
            flock.assert_called_once()
            sleep.assert_not_called()
            send.assert_not_called()
            self.assertFalse((root / "launcher-attempts/io-error.json").exists())

    def test_lock_wait_revalidates_permissions_before_reservation(self) -> None:
        root = launcher.prepare_root(self.folder / "voice-lock-revalidate")
        real_flock = launcher.fcntl.flock

        def acquired(fd: int, operation: int) -> None:
            real_flock(fd, operation)
            root.chmod(0o755)

        try:
            with (
                patch.object(launcher.fcntl, "flock", side_effect=acquired),
                patch.object(launcher, "_launch_once") as send,
            ):
                with self.assertRaises(launcher.LauncherError):
                    launcher.launch(
                        config(8000),
                        FAKE_KEY,
                        "voice",
                        root,
                        None,
                        test_port=8000,
                        attempt_id="permissions",
                        lock_wait_timeout=1.0,
                    )
                send.assert_not_called()
                self.assertFalse((root / "launcher-attempts/permissions.json").exists())
        finally:
            root.chmod(0o700)
        # The revalidation failure must not leak ownership of the root lock.
        with patch.object(launcher, "_launch_once", return_value={"ok": True, "text": "ok"}):
            self.assertTrue(
                launcher.launch(
                    config(8000),
                    FAKE_KEY,
                    "voice",
                    root,
                    None,
                    test_port=8000,
                    attempt_id="after-permissions",
                )["ok"]
            )

    def test_lock_wait_model_failure_stays_unknown_and_is_never_replayed(self) -> None:
        root = launcher.prepare_root(self.folder / "voice-lock-model-error")
        with patch.object(launcher, "_launch_once", side_effect=RuntimeError("fixture")) as send:
            for _ in range(2):
                with self.assertRaises(launcher.LauncherError):
                    launcher.launch(
                        config(8000),
                        FAKE_KEY,
                        "voice",
                        root,
                        None,
                        test_port=8000,
                        attempt_id="unknown-voice",
                        lock_wait_timeout=1.0,
                    )
            send.assert_called_once()
        self.assertEqual(launcher.query_attempt(root, "unknown-voice")["state"], "unknown")

    def test_product_config_permissions_rejected_before_child(self) -> None:
        path = self.folder / "product.json"
        path.write_text(json.dumps(config(8000)))
        path.chmod(0o644)
        with patch.object(launcher.subprocess, "Popen") as child:
            with self.assertRaises(launcher.LauncherError):
                launcher.read_config(path, 8000)
            child.assert_not_called()

    def test_product_binding_reserved_and_conflict_never_spawns(self) -> None:
        root = launcher.prepare_root(self.folder / "bound-data")
        with patch.object(
            launcher, "_launch_once", return_value={"ok": True, "text": "ok", "session_id": "s"}
        ):
            result = launcher.launch(
                config(8000), FAKE_KEY, "test", root, None, test_port=8000, attempt_id="bound"
            )
        record = launcher.query_attempt(root, "bound")
        self.assertEqual(record.get("data_root"), str(root))
        self.assertEqual(record["config_sha256"], result["config_sha256"])
        self.assertEqual(record["binding_sha256"], result["binding_sha256"])
        changed = {**config(8000), "model": "changed"}
        with patch.object(launcher.subprocess, "Popen") as child:
            with self.assertRaisesRegex(launcher.LauncherError, "配置.*冲突"):
                launcher.launch(
                    changed, FAKE_KEY, "test", root, None, test_port=8000, attempt_id="bound"
                )
            child.assert_not_called()

    def test_prompt_budget_is_utf8_inclusive_and_persona_specific(self) -> None:
        from context_limits import CHAT_CONTEXT_BYTE_LIMIT, INNER_CONTEXT_BYTE_LIMIT

        root = launcher.prepare_root(self.folder / "prompt-boundary")
        for mode, limit in (("chat", CHAT_CONTEXT_BYTE_LIMIT), ("inner", INNER_CONTEXT_BYTE_LIMIT)):
            for character in ("x", "界", "😀", "\0"):
                width = len(character.encode("utf-8"))
                exact = character * (limit // width) + "x" * (limit % width)
                with (
                    self.subTest(mode=mode, character=repr(character)),
                    patch.object(
                        launcher.subprocess, "Popen", side_effect=RuntimeError("accepted boundary")
                    ) as child,
                ):
                    with self.assertRaisesRegex(launcher.LauncherError, "结果未知"):
                        launcher.launch(
                            config(8000),
                            FAKE_KEY,
                            exact,
                            root,
                            None,
                            test_port=8000,
                            persona_mode=mode,
                        )
                    # 一次 attempt 内部允许热引擎失败后回落冷启动（两次 Popen），
                    # 但「结果未知」之后不得再派生任何新进程。
                    before = child.call_count
                    with self.assertRaises(launcher.LauncherError):
                        launcher.launch(
                            config(8000),
                            FAKE_KEY,
                            exact + "x",
                            root,
                            None,
                            test_port=8000,
                            persona_mode=mode,
                        )
                    self.assertEqual(child.call_count, before)

    def test_inner_worst_escaped_boundary_crosses_real_launcher_and_engine(self) -> None:
        from context_limits import INNER_CONTEXT_BYTE_LIMIT
        from inner_voice import SYSTEM_PROMPT

        requests: list[dict[str, Any]] = []

        class Provider(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                payload = json.dumps(
                    {
                        "id": "fake-boundary",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "fake-test-model",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "ok"},
                            }
                        ],
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Provider)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        root = launcher.prepare_root(self.folder / "worst-escaped-inner")
        prompt = "\0" * INNER_CONTEXT_BYTE_LIMIT
        try:
            result = launcher.launch(
                config(server.server_port),
                FAKE_KEY,
                prompt,
                root,
                "s" * 80,
                test_port=server.server_port,
                persona_mode="inner",
                source_channel="imessage",
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["text"], "ok")
            self.assertEqual(len(requests), 1)
            self.assertEqual(
                requests[0]["messages"],
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            with patch.object(launcher.subprocess, "Popen") as child:
                with self.assertRaises(launcher.LauncherError):
                    launcher.launch(
                        config(server.server_port),
                        FAKE_KEY,
                        prompt + "x",
                        root,
                        "t" * 80,
                        test_port=server.server_port,
                        persona_mode="inner",
                    )
                child.assert_not_called()
            self.assertEqual(len(requests), 1)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)

    def test_launch_forwards_inner_persona(self) -> None:
        root = launcher.prepare_root(self.folder / "inner-forwarding")
        with patch.object(
            launcher, "_launch_once", return_value={"ok": True, "text": "ok", "session_id": "s"}
        ) as send:
            launcher.launch(
                config(8000), FAKE_KEY, "test", root, None, test_port=8000, persona_mode="inner"
            )
        send.assert_called_once_with(
            config(8000),
            FAKE_KEY,
            "test",
            root,
            None,
            test_port=8000,
            timeout=launcher.chat.launcher_timeout_seconds("high"),
            persona_mode="inner",
        )

    def test_launch_default_preserves_legacy_call_signature(self) -> None:
        root = launcher.prepare_root(self.folder / "default-forwarding")
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                options: dict[str, Any] = {"persona_mode": "chat"} if explicit else {}
                with patch.object(
                    launcher,
                    "_launch_once",
                    return_value={"ok": True, "text": "ok", "session_id": "s"},
                ) as send:
                    launcher.launch(
                        config(8000), FAKE_KEY, "test", root, None, test_port=8000, **options
                    )
                send.assert_called_once_with(
                    config(8000),
                    FAKE_KEY,
                    "test",
                    root,
                    None,
                    test_port=8000,
                    timeout=launcher.chat.launcher_timeout_seconds("high"),
                )

    def test_launch_uses_effort_budget_and_preserves_explicit_timeout(self) -> None:
        for effort in (None, "none", "minimal", "low", "medium", "high", "xhigh"):
            for override in (None, 0.5, 300.0):
                with self.subTest(effort=effort, override=override):
                    root = launcher.prepare_root(self.folder / uuid.uuid4().hex)
                    route = {**config(8000), "reasoning_effort": effort}
                    options: dict[str, Any] = {} if override is None else {"timeout": override}
                    with patch.object(
                        launcher,
                        "_launch_once",
                        return_value={"ok": True, "text": "ok", "session_id": "s"},
                    ) as send:
                        launcher.launch(
                            route, FAKE_KEY, "test", root, None, test_port=8000, **options
                        )
                    expected = launcher.chat.launcher_timeout_seconds(effort)
                    self.assertEqual(
                        send.call_args.kwargs["timeout"],
                        expected if override is None else override,
                    )

    def test_launch_once_persona_stdin_and_fd_only_credentials(self) -> None:
        stub = self.folder / "chat_entry.py"
        stub.write_text(
            "import json,os,sys\n"
            "fd=int(sys.argv[sys.argv.index('--credentials-fd')+1])\n"
            "with os.fdopen(fd) as stream: route=json.load(stream)\n"
            "assert route['api_key']=='FAKE-ONLY-NONSECRET-TEST-KEY'\n"
            "request=json.load(sys.stdin)\n"
            "print(json.dumps({'ok':True,'text':json.dumps(request),'session_id':'s'}))\n"
        )
        for mode in (None, "chat", "inner"):
            with self.subTest(mode=mode):
                root = launcher.prepare_root(self.folder / ("stdin-" + str(mode)))
                options: dict[str, Any] = {} if mode is None else {"persona_mode": mode}
                real_spawn = launcher.subprocess.Popen
                with (
                    patch.object(launcher, "__file__", str(self.folder / "chat_launcher.py")),
                    patch.object(launcher.subprocess, "Popen", wraps=real_spawn) as spawn,
                ):
                    result = launcher.launch(
                        config(8000), FAKE_KEY, "test", root, "s", test_port=8000, **options
                    )
                expected = {"prompt": "test", "session_id": "s"}
                if mode == "inner":
                    expected["persona_mode"] = "inner"
                self.assertEqual(json.loads(result["text"]), expected)
                self.assertNotIn(FAKE_KEY, json.dumps(spawn.call_args.args))
                self.assertNotIn(FAKE_KEY, json.dumps(spawn.call_args.kwargs["env"]))
                self.assertEqual(len(spawn.call_args.kwargs["pass_fds"]), 1)
                for file in root.rglob("*"):
                    if file.is_file():
                        self.assertNotIn(FAKE_KEY.encode(), file.read_bytes(), str(file))

    def test_invalid_persona_rejected_before_attempt_or_child(self) -> None:
        root = launcher.prepare_root(self.folder / "invalid-persona")
        with patch.object(launcher.subprocess, "Popen") as child:
            with self.assertRaises(launcher.LauncherError):
                launcher.launch(
                    config(8000),
                    FAKE_KEY,
                    "test",
                    root,
                    None,
                    test_port=8000,
                    persona_mode="invalid",
                )
            child.assert_not_called()
        self.assertFalse((root / "launcher-attempts").exists())

    def test_timeline_identity_is_stable_across_restart(self) -> None:
        root = launcher.prepare_root(self.folder / "timeline-data")
        first = launcher.timeline_identity(root)
        restarted = launcher.timeline_identity(launcher.prepare_root(root))
        self.assertEqual(first, restarted)
        self.assertEqual(first, launcher.timeline_identity(root))

    def test_legacy_session_rows_remain_readable_in_one_timeline(self) -> None:
        import sqlite3

        root = launcher.prepare_root(self.folder / "legacy-timeline")
        database = root / "engine-home/state.db"
        with sqlite3.connect(database) as db:
            db.executescript("""
                CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT,
                    role TEXT, content TEXT, timestamp REAL);
            """)
            db.executemany(
                "INSERT INTO messages VALUES(?,?,?,?,?)",
                [
                    (1, "legacy-a", "user", "older", 1000.0),
                    (2, "legacy-b", "assistant", "newer", 2000.0),
                ],
            )
        rows = launcher.read_timeline(root)
        self.assertEqual([row["content"] for row in rows], ["older", "newer"])
        self.assertEqual({row["source_channel"] for row in rows}, {"legacy"})
        self.assertEqual([row["timestamp"] for row in rows], [1000.0, 2000.0])

    def test_attempt_protocol_interface(self) -> None:
        import inspect

        self.assertIn("attempt_id", inspect.signature(launcher.launch).parameters)

    def test_expected_snapshot_and_binding_conflicts_never_spawn(self) -> None:
        root = launcher.prepare_root(self.folder / "conflict-root")
        for expected in ({"expected_sha256": "0" * 64}, {"expected_binding_sha256": "0" * 64}):
            with (
                self.subTest(expected=expected),
                patch.object(launcher.subprocess, "Popen") as child,
            ):
                with self.assertRaisesRegex(launcher.LauncherError, "配置.*冲突"):
                    launcher.launch(
                        config(8000),
                        FAKE_KEY,
                        "test",
                        root,
                        None,
                        test_port=8000,
                        expected_sha256=expected.get("expected_sha256"),
                        expected_binding_sha256=expected.get("expected_binding_sha256"),
                    )
                child.assert_not_called()
                self.assertFalse((root / "launcher-attempts").exists())

    def test_existing_engine_config_conflict_never_spawns(self) -> None:
        root = launcher.prepare_root(self.folder / "existing-root")
        (root / "engine-home/config.yaml").write_text('{"old_model":true}')
        with patch.object(launcher.subprocess, "Popen") as child:
            with self.assertRaisesRegex(launcher.LauncherError, "配置.*冲突"):
                launcher.launch(
                    config(8000),
                    FAKE_KEY,
                    "test",
                    root,
                    None,
                    test_port=8000,
                    attempt_id="changed-root",
                )
            child.assert_not_called()

    def test_invalid_product_root_never_spawns(self) -> None:
        root = launcher.prepare_root(self.folder / "invalid-root")
        root.chmod(0o755)
        with patch.object(launcher.subprocess, "Popen") as child:
            with self.assertRaises(launcher.LauncherError):
                launcher.launch(config(8000), FAKE_KEY, "test", root, None, test_port=8000)
            child.assert_not_called()

    def test_host_config_is_never_read(self) -> None:
        path = self.folder / "product-route.json"
        launcher.product.write_config(path, config(8000), test_port=8000)
        host = self.folder / "host"
        host.mkdir(mode=0o700)
        (host / "config.yaml").write_text("poison: [")
        original = Path.read_text
        reads: list[Path] = []

        def guarded_read(target: Path, *args: Any, **kwargs: Any) -> str:
            reads.append(target)
            self.assertFalse(target == host or host in target.parents)
            return original(target, *args, **kwargs)

        with (
            patch.dict(os.environ, {"HERMES_HOME": str(host)}),
            patch.object(Path, "read_text", guarded_read),
        ):
            self.assertEqual(launcher.read_config(path, 8000), config(8000))
        self.assertEqual(reads, [path])

    def test_safe_key_file_and_pipe_and_rejected_permissions_links_owner(self) -> None:
        self.assertEqual(launcher.read_key(key_file=self.key, key_fd=None), FAKE_KEY)
        self.key.chmod(0o644)
        with self.assertRaises(launcher.LauncherError):
            launcher.read_key(key_file=self.key, key_fd=None)
        self.key.chmod(0o600)
        link = self.folder / "linked-key"
        link.symlink_to(self.key)
        with self.assertRaises(launcher.LauncherError):
            launcher.read_key(key_file=link, key_fd=None)
        with patch.object(launcher.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(launcher.LauncherError):
                launcher.read_key(key_file=self.key, key_fd=None)
        hard = self.folder / "hard-key"
        os.link(self.key, hard)
        with self.assertRaises(launcher.LauncherError):
            launcher.read_key(key_file=self.key, key_fd=None)
        read, write = os.pipe()
        os.write(write, FAKE_KEY.encode())
        os.close(write)
        try:
            self.assertEqual(launcher.read_key(key_file=None, key_fd=read), FAKE_KEY)
        finally:
            os.close(read)

    def test_config_has_no_key_and_loopback_requires_explicit_test_mode(self) -> None:
        path = self.folder / "jarvis.fake.json"
        launcher.product.write_config(path, config(8000), test_port=8000)
        self.assertEqual(launcher.read_config(path, 8000), config(8000))
        with self.assertRaises(launcher.LauncherError):
            launcher.read_config(path, None)
        for field in ("api_key", "tools", "hermes_config"):
            value = config(8000)
            value[field] = FAKE_KEY
            path.write_text(json.dumps(value))
            with self.assertRaises(launcher.LauncherError):
                launcher.read_config(path, 8000)

    def test_actual_cli_fake_http_reuses_saved_session_and_fd_zero(self) -> None:
        requests: list[dict[str, Any]] = []
        authorization: list[str | None] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                authorization.append(self.headers.get("Authorization"))
                payload = json.dumps(
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
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = launcher.prepare_root(
            getattr(self, "data_root", self.folder / ("data-" + uuid.uuid4().hex))
        )
        try:
            path = self.folder / "jarvis.fake.json"
            launcher.product.write_config(
                path, config(server.server_port), test_port=server.server_port
            )
            cmd = [
                str(ENTRY.parents[1] / ".venv/bin/python"),
                "-B",
                str(ENTRY),
                "--config",
                str(path),
                "--prompt",
                "普通文本 test",
                "--data-dir",
                str(root),
                "--test-local-port",
                str(server.server_port),
            ]
            if hasattr(self, "data_root") and self.data_root is None:
                index = cmd.index("--data-dir")
                del cmd[index : index + 2]
            first = subprocess.run(
                cmd + ["--key-file", str(self.key)], text=True, capture_output=True, timeout=65
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            first_result = json.loads(first.stdout)
            second = subprocess.run(
                cmd + ["--key-fd", "0"], input=FAKE_KEY, text=True, capture_output=True, timeout=65
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            second_result = json.loads(second.stdout)
            self.assertEqual(first_result["session_id"], second_result["session_id"])
            self.assertEqual(first_result["text"], "FAKE_HTTP_OK")
            self.assertEqual(second_result["mode"], "explicit fake HTTP")
            self.assertNotEqual(first_result["attempt_id"], second_result["attempt_id"])
            query = subprocess.run(
                [
                    str(ENTRY.parents[1] / ".venv/bin/python"),
                    "-B",
                    str(ENTRY),
                    "--query-attempt",
                    first_result["attempt_id"],
                    "--data-dir",
                    str(root),
                ],
                text=True,
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(query.returncode, 0, query.stderr)
            self.assertEqual(json.loads(query.stdout)["state"], "completed")
            duplicate = subprocess.run(
                cmd + ["--key-file", str(self.key), "--attempt-id", first_result["attempt_id"]],
                text=True,
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(duplicate.returncode, 1)
            self.assertEqual([len(r["messages"]) for r in requests], [2, 4])
            self.assertEqual(
                [[message["role"] for message in r["messages"]] for r in requests],
                [["system", "user"], ["system", "user", "assistant", "user"]],
            )
            for request in requests:
                self.assertIn("Sir", request["messages"][0]["content"])
            self.assertEqual(authorization, ["Bearer " + FAKE_KEY] * 2)
            self.assertTrue(all("tools" not in r for r in requests))
            self.assertTrue(all(r["reasoning_effort"] == "high" for r in requests))
            self.assertNotIn(FAKE_KEY, first.stdout + first.stderr + second.stdout + second.stderr)
            self.assertEqual(
                json.loads((root / "launcher-session.json").read_text()),
                {"session_id": first_result["session_id"]},
            )
            for file in root.rglob("*"):
                if file.is_file():
                    self.assertNotIn(FAKE_KEY.encode(), file.read_bytes(), str(file))
            (EVIDENCE / (self._testMethodName + "-fake-http.json")).write_text(
                json.dumps(
                    {
                        "mode": "explicit fake HTTP only; no real provider or real credentials",
                        "first": first_result,
                        "second": second_result,
                        "message_counts": [len(r["messages"]) for r in requests],
                        "credential_absent_from_output_and_data": True,
                    },
                    indent=2,
                )
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            import shutil

            shutil.rmtree(root)

    def test_default_product_home_actual_cli_restart_and_query(self) -> None:
        self.data_root = None
        with patch.dict(os.environ, {"JARVIS_HOME": str(self.folder / "product-home")}):
            self.test_actual_cli_fake_http_reuses_saved_session_and_fd_zero()

    def test_legacy_root_actual_cli_restart_and_query(self) -> None:
        # Historical RUNS fixture migrated to a dedicated product root/config.
        self.data_root = self.folder / "migrated-product-fixture"
        self.test_actual_cli_fake_http_reuses_saved_session_and_fd_zero()

    def test_cli_unknown_key_argument_is_not_echoed(self) -> None:
        process = subprocess.run(
            [str(ENTRY.parents[1] / ".venv/bin/python"), "-B", str(ENTRY), "--api-key", FAKE_KEY],
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 1)
        self.assertNotIn(FAKE_KEY, process.stdout + process.stderr)

    def test_child_failures_and_secret_output_are_suppressed(self) -> None:
        root = launcher.prepare_root(self.folder / ("data-" + uuid.uuid4().hex))
        try:
            stub = self.folder / "stub.py"
            for source in (
                'print("not-json")',
                'print(\'{"ok":false,"error_type":"Failure"}\')',
                'print(\'{"ok":true,"text":"ok","session_id":"s"}\'); raise SystemExit(3)',
                f"print({FAKE_KEY!r})",
                f"print('{json.dumps({'ok': True, 'text': FAKE_KEY, 'session_id': 's'})}')",
            ):
                stub.write_text(source)
                with (
                    patch.object(
                        launcher.chat.engine, "PYTHON", ENTRY.parents[1] / ".venv/bin/python"
                    ),
                    patch.object(launcher, "__file__", str(stub.with_name("chat_launcher.py"))),
                ):
                    stub.rename(stub.with_name("chat_entry.py"))
                    with self.assertRaises(launcher.LauncherError) as caught:
                        launcher.launch(config(8000), FAKE_KEY, "test", root, None, test_port=8000)
                    self.assertNotIn(FAKE_KEY, str(caught.exception))
                    stub.with_name("chat_entry.py").rename(stub)
            self.assertFalse((root / "launcher-session.json").exists())
        finally:
            import shutil

            shutil.rmtree(root)

    def test_attempt_bad_json_after_send_blocks_second_call(self) -> None:
        import shutil

        root = launcher.prepare_root(self.folder / ("data-" + uuid.uuid4().hex))
        counter = root / "simulated-send-counter"
        stub = self.folder / "chat_entry.py"
        stub.write_text(
            f"from pathlib import Path\np=Path({str(counter)!r})\n"
            "with p.open('a') as f: f.write('sent\\n')\nprint('broken JSON')\n"
        )
        try:
            with patch.object(launcher, "__file__", str(self.folder / "chat_launcher.py")):
                for _ in range(2):
                    with self.assertRaises(launcher.LauncherError):
                        launcher.launch(
                            config(8000),
                            FAKE_KEY,
                            "sensitive prompt",
                            root,
                            None,
                            test_port=8000,
                            attempt_id="bad-json",
                        )
            self.assertEqual(counter.read_text().splitlines(), ["sent"])
            record = launcher.query_attempt(root, "bad-json")
            self.assertEqual(record["state"], "unknown")
            self.assertNotIn("sensitive prompt", json.dumps(record))
            self.assertNotIn(FAKE_KEY, json.dumps(record))
        finally:
            shutil.rmtree(root)

    def test_attempt_persistence_failures_never_start_child(self) -> None:
        import shutil

        for target in ("fsync-file", "fsync-parent", "atomic_json"):
            root = launcher.prepare_root(self.folder / ("data-" + uuid.uuid4().hex))
            try:
                owner = launcher.os if target.startswith("fsync") else launcher
                operation = "fsync" if target.startswith("fsync") else target
                with (
                    patch.object(
                        owner,
                        operation,
                        side_effect=(
                            [None] * (1 if target == "fsync-file" else 2)
                            + [OSError("simulated disk fault")]
                        )
                        if target.startswith("fsync")
                        else OSError("simulated disk fault"),
                    ),
                    patch.object(launcher.subprocess, "Popen") as child,
                ):
                    with self.assertRaises(launcher.LauncherError):
                        launcher.launch(
                            config(8000),
                            FAKE_KEY,
                            "test",
                            root,
                            None,
                            test_port=8000,
                            attempt_id="disk-fault",
                        )
                    child.assert_not_called()
            finally:
                shutil.rmtree(root)

    def test_attempt_multiprocess_one_winner_and_crash_reentry(self) -> None:
        import shutil

        root = launcher.prepare_root(self.folder / ("data-" + uuid.uuid4().hex))
        counter = root / "simulated-send-counter"
        stub = self.folder / "chat_entry.py"
        stub.write_text(
            "import os,time\n"
            f"fd=os.open({str(counter)!r},os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)\n"
            "os.write(fd,b'sent\\n');os.close(fd)\ntime.sleep(0.4)\nprint('broken JSON')\n"
        )
        driver = self.folder / "driver.py"
        driver.write_text(
            "import importlib.util,sys\n"
            f"s=importlib.util.spec_from_file_location('l',{str(ENTRY)!r})\n"
            "assert s and s.loader\nl=importlib.util.module_from_spec(s);s.loader.exec_module(l)\n"
            f"l.__file__={str(self.folder / 'chat_launcher.py')!r}\n"
            "try:\n"
            f" l.launch({config(8000)!r},{FAKE_KEY!r},'test',l.Path({str(root)!r}),None,"
            "test_port=8000,attempt_id=sys.argv[1])\n"
            "except l.LauncherError: pass\n"
        )
        processes: list[subprocess.Popen[str]] = []
        try:
            for _ in range(8):
                processes.append(
                    subprocess.Popen(
                        [str(ENTRY.parents[1] / ".venv/bin/python"), "-B", str(driver), "race"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            for process in processes:
                out, err = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, out + err)
            self.assertEqual(counter.read_text().splitlines(), ["sent"])
            self.assertEqual(launcher.query_attempt(root, "race")["state"], "unknown")
            # A hard launcher crash leaves the durable started marker authoritative.
            stub.write_text(
                "import os,signal\n"
                f"fd=os.open({str(counter)!r},os.O_WRONLY|os.O_APPEND)\n"
                "os.write(fd,b'sent\\n');os.close(fd)\n"
                "os.kill(os.getppid(),signal.SIGKILL)\n"
            )
            crashed = subprocess.run(
                [str(ENTRY.parents[1] / ".venv/bin/python"), "-B", str(driver), "crash"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(crashed.returncode, -signal.SIGKILL)
            self.assertEqual(counter.read_text().splitlines(), ["sent", "sent"])
            self.assertEqual(launcher.query_attempt(root, "crash")["state"], "started")
            with patch.object(launcher.subprocess, "Popen") as child:
                with self.assertRaises(launcher.LauncherError):
                    launcher.launch(
                        config(8000),
                        FAKE_KEY,
                        "test",
                        root,
                        None,
                        test_port=8000,
                        attempt_id="crash",
                    )
                child.assert_not_called()
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            shutil.rmtree(root)

    def test_actual_http_db_failure_is_unknown_and_never_resent(self) -> None:
        import shutil
        import sqlite3

        count: list[int] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                count.append(1)
                payload = json.dumps(
                    {
                        "id": "fake-db-failure",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "fake-test-model",
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
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = launcher.prepare_root(self.folder / ("data-" + uuid.uuid4().hex))
        try:
            route_path = self.folder / "snapshot.json"
            snapshot = launcher.product.write_config(
                route_path, config(server.server_port), test_port=server.server_port
            )
            route = launcher.read_config(route_path, server.server_port)
            real_spawn = launcher.subprocess.Popen
            commands: list[list[str]] = []
            environments: list[dict[str, str]] = []

            def mutate_and_spawn(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
                commands.append(command)
                environments.append(kwargs["env"])
                launcher.product.write_config(
                    route_path,
                    {**route, "model": "changed-after-validation"},
                    expected_sha256=snapshot.sha256,
                    test_port=server.server_port,
                )
                return real_spawn(command, **kwargs)

            with patch.object(launcher.subprocess, "Popen", side_effect=mutate_and_spawn):
                first = launcher.launch(
                    route, FAKE_KEY, "baseline", root, None, test_port=server.server_port
                )
            self.assertEqual(first["config_sha256"], snapshot.sha256)
            self.assertNotIn(FAKE_KEY, json.dumps(commands) + json.dumps(environments))
            self.assertNotIn(str(route_path), json.dumps(commands))
            self.assertEqual(
                launcher.product.read_config(route_path, test_port=server.server_port).values[
                    "model"
                ],
                "changed-after-validation",
            )
            self.assertEqual(
                json.loads((root / "engine-home/config.yaml").read_text())["model"]["default"],
                "fake-test-model",
            )
            database = root / "engine-home/state.db"
            self.assertTrue(database.is_file())
            self.assertTrue((root / "memory.db").is_file())
            with sqlite3.connect(database.as_uri() + "?mode=rw", uri=True) as db:
                db.execute(
                    "CREATE TRIGGER reject_assistant BEFORE INSERT ON messages "
                    "WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT,'fake fault'); END"
                )
            for _ in range(2):
                with self.assertRaises(launcher.LauncherError):
                    launcher.launch(
                        route,
                        FAKE_KEY,
                        "database fault",
                        root,
                        first["session_id"],
                        test_port=server.server_port,
                        attempt_id="db-fault",
                    )
            self.assertEqual(len(count), 2)  # baseline + exactly one failed local save
            self.assertEqual(launcher.query_attempt(root, "db-fault")["state"], "unknown")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            shutil.rmtree(root)

    def test_session_lock_blocks_another_attempt_before_send(self) -> None:
        import fcntl
        import shutil

        root = launcher.prepare_root(self.folder / ("data-" + uuid.uuid4().hex))
        fd = os.open(root / "launcher-session.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(launcher.subprocess, "Popen") as child:
                with self.assertRaises(launcher.LauncherError):
                    launcher.launch(
                        config(8000),
                        FAKE_KEY,
                        "test",
                        root,
                        None,
                        test_port=8000,
                        attempt_id="session-busy",
                    )
                child.assert_not_called()
        finally:
            os.close(fd)
            shutil.rmtree(root)

    def test_attempt_completed_query_and_fresh_ids_allow_identical_prompts(self) -> None:
        import shutil

        root = launcher.prepare_root(self.folder / ("data-" + uuid.uuid4().hex))
        try:
            with patch.object(
                launcher,
                "_launch_once",
                side_effect=[
                    {"ok": True, "text": "FAKE-RESULT", "session_id": "s"},
                    {"ok": True, "text": "FAKE-RESULT", "session_id": "s"},
                ],
            ) as send:
                first = launcher.launch(config(8000), FAKE_KEY, "same", root, None, test_port=8000)
                second = launcher.launch(config(8000), FAKE_KEY, "same", root, None, test_port=8000)
                self.assertNotEqual(first["attempt_id"], second["attempt_id"])
                self.assertEqual(send.call_count, 2)
                with self.assertRaises(launcher.LauncherError):
                    launcher.launch(
                        config(8000),
                        FAKE_KEY,
                        "same",
                        root,
                        None,
                        test_port=8000,
                        attempt_id=first["attempt_id"],
                    )
                self.assertEqual(send.call_count, 2)
                self.assertEqual(
                    launcher.query_attempt(root, first["attempt_id"])["state"], "completed"
                )
        finally:
            shutil.rmtree(root)

    def test_timeout_kills_child_and_grandchild_with_clean_environment(self) -> None:
        root = launcher.prepare_root(self.folder / ("data-" + uuid.uuid4().hex))
        pidfile = root / "pids.json"
        stub = self.folder / "timeout.py"
        interpreter = ENTRY.parents[1] / ".venv/bin/python"
        stub.write_text(
            "import os,json,subprocess,time\n"
            f"child=subprocess.Popen([{str(interpreter)!r},'-c','import time;time.sleep(30)'])\n"
            f"open({str(pidfile)!r},'w').write(json.dumps([os.getpid(),child.pid,dict(os.environ)]))\n"
            "time.sleep(30)\n"
        )
        try:
            with (
                patch.object(launcher.chat.engine, "PYTHON", interpreter),
                patch.object(launcher, "__file__", str(stub.with_name("chat_launcher.py"))),
                patch.dict(os.environ, {"OPENAI_API_KEY": "FAKE-parent-key", "PYTHONPATH": "/bad"}),
            ):
                stub.rename(stub.with_name("chat_entry.py"))
                with self.assertRaisesRegex(launcher.LauncherError, "超时"):
                    launcher.launch(
                        config(8000),
                        FAKE_KEY,
                        "test",
                        root,
                        None,
                        test_port=8000,
                        timeout=0.5,
                        attempt_id="timeout",
                    )
                self.assertEqual(launcher.query_attempt(root, "timeout")["state"], "unknown")
                with patch.object(launcher.subprocess, "Popen") as child:
                    with self.assertRaises(launcher.LauncherError):
                        launcher.launch(
                            config(8000),
                            FAKE_KEY,
                            "test",
                            root,
                            None,
                            test_port=8000,
                            attempt_id="timeout",
                        )
                    child.assert_not_called()
            pids = json.loads(pidfile.read_text())
            self.assertNotIn("OPENAI_API_KEY", pids[2])
            self.assertNotIn("PYTHONPATH", pids[2])
            self.assertEqual(pids[2]["HERMES_HOME"], str(root / "engine-home"))
            for pid in pids[:2]:
                for _ in range(30):
                    probe = subprocess.run(
                        ["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
                    )
                    if not probe.stdout.strip() or probe.stdout.strip().startswith("Z"):
                        break
                    time.sleep(0.05)
                else:
                    os.kill(pid, signal.SIGKILL)
                    self.fail("process group descendant survived")
        finally:
            import shutil

            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
