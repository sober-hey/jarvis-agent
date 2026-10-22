"""Meaningful offline policy tests and controlled local HTTP product-entry test."""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

from inner_voice import SYSTEM_PROMPT

if TYPE_CHECKING:
    import chat_entry as chat_types

ENTRY = Path(__file__).with_name("chat_entry.py")
spec = importlib.util.spec_from_file_location("jarvis_chat", ENTRY)
if spec is None or spec.loader is None:
    raise RuntimeError("Entry loader missing")
chat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chat)
if TYPE_CHECKING:
    chat = chat_types


def route(port: int) -> dict[str, Any]:
    return {
        "model": "fake-test-model",
        "provider": "custom",
        "base_url": f"http://127.0.0.1:{port}/v1",
        "api_key": "FAKE-NONSECRET",
        "api_mode": "chat_completions",
        "context_length": 256000,
        "reasoning_effort": "high",
    }


def context_gate_literals(source: str) -> list[int]:
    """Find numeric text-size gates, not audio rates, token pins or response caps."""
    tree = ast.parse(source)
    # Fact-extraction eligibility is a classifier, not an accepted-request gate.
    # Its short-text heuristic must not be confused with a dispatch budget.
    eligibility_nodes = {
        id(part)
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        and function.name == "extraction_eligible"
        for part in ast.walk(function)
    }
    lines: list[int] = []
    for node in ast.walk(tree):
        if id(node) in eligibility_nodes:
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if (
                any(
                    isinstance(target, ast.Name)
                    and "CONTEXT" in target.id.upper()
                    and any(word in target.id.upper() for word in ("LIMIT", "MAX", "BYTES"))
                    for target in targets
                )
                and node.value is not None
            ):
                if any(
                    isinstance(part, ast.Constant) and type(part.value) is int
                    for part in ast.walk(node.value)
                ):
                    lines.append(node.lineno)
        if not isinstance(node, ast.Compare):
            continue
        lengths = [
            part
            for part in ast.walk(node)
            if isinstance(part, ast.Call)
            and isinstance(part.func, ast.Name)
            and part.func.id == "len"
            and part.args
            and any(
                isinstance(name, ast.Name)
                and name.id in {"prompt", "context", "messages", "message"}
                for name in ast.walk(part.args[0])
            )
        ]
        if lengths and any(
            isinstance(part, ast.Constant) and type(part.value) is int and part.value > 1
            for part in ast.walk(node)
        ):
            lines.append(node.lineno)
    return sorted(set(lines))


class ContextLimitTests(unittest.TestCase):
    def test_shared_budget_exists_and_fits_escaped_inner_wire(self) -> None:
        spec = importlib.util.find_spec("context_limits")
        self.assertIsNotNone(spec, "shared context_limits module is missing")
        from context_limits import (
            CHAT_CONTEXT_BYTE_LIMIT,
            CONTEXT_JSON_BYTE_LIMIT,
            INNER_CONTEXT_BYTE_LIMIT,
        )

        self.assertLessEqual(INNER_CONTEXT_BYTE_LIMIT, CHAT_CONTEXT_BYTE_LIMIT)
        self.assertEqual(chat.MAX_BYTES, CONTEXT_JSON_BYTE_LIMIT)
        # NUL is the worst JSON expansion: one UTF-8 byte becomes six wire bytes.
        prompt = "\0" * INNER_CONTEXT_BYTE_LIMIT
        request = {
            "prompt": prompt,
            "persona_mode": "inner",
            "session_id": "s" * 80,
            "source_channel": "imessage",
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        self.assertLessEqual(len(json.dumps(request, ensure_ascii=False).encode("utf-8")), chat.MAX_BYTES)
        self.assertLessEqual(len(json.dumps(messages, ensure_ascii=False).encode("utf-8")), chat.MAX_BYTES)

    def test_scanner_detects_known_context_gate_shapes(self) -> None:
        for source in (
            "if len(prompt) > 64000: pass",
            "if len(prompt.encode('utf-8')) > 60000: pass",
            "if len(prompt) > (64000 if persona_mode == 'inner' else 16000): pass",
            "CONTEXT_BYTE_LIMIT = 60000",
            "if len(json.dumps(messages).encode()) > 65536: pass",
            "if len(prompt.encode('utf-16-le')) // 2 > 16000: pass",
        ):
            with self.subTest(source=source):
                self.assertTrue(context_gate_literals(source))
        self.assertEqual(context_gate_literals("SAMPLE_RATE = 16000"), [])
        self.assertEqual(
            context_gate_literals("def extraction_eligible(prompt): return len(prompt) <= 2000"), []
        )

    def test_packaging_context_gates_have_no_local_numeric_budget(self) -> None:
        base = ENTRY.parent
        violations: list[str] = []
        for directory, folders, files in os.walk(base):
            folders[:] = [
                name
                for name in folders
                if name not in {"runtime", "__pycache__", "vendor", ".venv", "tests"}
            ]
            for name in files:
                if (
                    not name.endswith(".py")
                    or name.startswith("test_")
                    or name == "context_limits.py"
                ):
                    continue
                path = Path(directory) / name
                violations.extend(
                    f"{path.relative_to(base)}:{line}"
                    for line in context_gate_literals(path.read_text())
                )
        self.assertEqual(violations, [], "context size gates must import shared budgets")

    def test_owned_context_gates_have_no_local_numeric_budget(self) -> None:
        for name in ("inner_model.py", "jarvis_daemon.py", "daemon_wiring.py", "chat_entry.py"):
            with self.subTest(name=name):
                self.assertEqual(context_gate_literals(ENTRY.with_name(name).read_text()), [])

    def test_product_model_boundary_is_utf8_and_inclusive(self) -> None:
        from types import SimpleNamespace
        from typing import cast
        from unittest.mock import patch

        import inner_model
        from context_limits import INNER_CONTEXT_BYTE_LIMIT

        model = object.__new__(inner_model.ProductModel)
        model.key = "FAKE-CONTEXT-KEY"
        model.binding = cast(Any, SimpleNamespace(values={}))
        for character in ("x", "界", "😀"):
            width = len(character.encode("utf-8"))
            prompt = character * (INNER_CONTEXT_BYTE_LIMIT // width)
            prompt += "x" * (INNER_CONTEXT_BYTE_LIMIT - len(prompt.encode("utf-8")))
            with (
                self.subTest(character=character),
                patch.object(
                    inner_model.launcher.chat,
                    "validate_route",
                    side_effect=RuntimeError("accepted at route boundary"),
                ) as validate,
            ):
                with self.assertRaisesRegex(RuntimeError, "accepted at route boundary"):
                    model(prompt)
                with self.assertRaisesRegex(ValueError, "oversized"):
                    model(prompt + "x")
                self.assertEqual(validate.call_count, 1)

    def test_product_model_rejects_utf8_overflow_before_spawn(self) -> None:
        from types import SimpleNamespace
        from typing import cast
        from unittest.mock import patch

        import inner_model

        model = object.__new__(inner_model.ProductModel)
        model.key = "FAKE-CONTEXT-KEY"
        model.binding = cast(Any, SimpleNamespace(values={}))
        # Below the former character ceiling, above even the former byte ceiling.
        with patch.object(
            inner_model.launcher.chat,
            "validate_route",
            side_effect=AssertionError("guard bypassed"),
        ) as validate:
            with self.assertRaisesRegex(ValueError, "oversized"):
                model("界" * 22000)
        validate.assert_not_called()


class ChatTests(unittest.TestCase):
    def test_persona_contains_only_values_and_initial_trait_numbers(self) -> None:
        import ast
        import re

        persona = ENTRY.with_name("JARVIS_PERSONA.md").read_text()
        direction = ENTRY.parents[1] / "docs/HANDOFF_DIRECTION.md"
        line = next(
            row
            for row in direction.read_text().splitlines()
            if row.startswith("5. 核心价值 6 条：")
        )
        values = line.split("：", 1)[1].removesuffix("。").split("；")
        self.assertEqual(len(values), 6)
        for value in values:
            self.assertIn(value, persona)
        self.assertNotIn("Sir，我不知道", persona)
        self.assertNotRegex(persona, r"[“「『\"]([^”」』\"]+)[”」』\"]")
        for phrase in ("一两句足够", "先给结论", "没有工具执行能力", "回答", "例如"):
            self.assertNotIn(phrase, persona)
        tree = ast.parse(ENTRY.with_name("affect_engine.py").read_text())
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "TRAIT_BASELINES"
                for target in node.targets
            )
        )
        self.assertIsInstance(assignment.value, ast.Call)
        assert isinstance(assignment.value, ast.Call)
        baselines = ast.literal_eval(assignment.value.args[0])
        numeric_rows = dict(re.findall(r"\|\s*([a-z_]+)\s*\|\s*([0-9.]+)\s*\|", persona))
        self.assertEqual({key: float(value) for key, value in numeric_rows.items()}, baselines)

    def test_minimal_route_rejects_tools_configs_and_local_endpoint(self) -> None:
        for key in ("tools", "config", "request_overrides", "proxy", "command"):
            value = route(8000)
            value[key] = []
            with self.assertRaises(ValueError):
                chat.validate_route(value, test_port=8000)
        with self.assertRaises(ValueError):
            chat.validate_route(route(8000))
        value = route(8000)
        value["provider"] = "anthropic"
        with self.assertRaises(ValueError):
            chat.validate_route(value, test_port=8000)

    def test_reasoning_budgets_cover_two_requests_and_cleanup(self) -> None:
        low = chat.request_timeout_seconds("low")
        medium = chat.request_timeout_seconds("medium")
        high = chat.request_timeout_seconds("high")
        xhigh = chat.request_timeout_seconds("xhigh")
        self.assertEqual(low, 30)
        self.assertGreater(medium, low)
        self.assertGreaterEqual(high, 300)
        self.assertGreater(xhigh, high)
        for effort in (None, "none", "minimal", "low", "medium", "high", "xhigh"):
            request = chat.request_timeout_seconds(effort)
            child = chat.child_timeout_seconds(effort)
            outer = chat.launcher_timeout_seconds(effort)
            self.assertGreaterEqual(child, request * 2 + 30)
            self.assertGreaterEqual(outer, child + 10)
        with self.assertRaises(ValueError):
            chat.request_timeout_seconds("unsupported")

    def test_network_policy_deadline_accepts_high_reasoning_budget(self) -> None:
        from unittest.mock import patch

        policy = chat.ModelPolicy(chat.RUNS, "model.example", 443, None)
        rows = [(2, 1, 6, "", ("1.1.1.1", 443))]
        with (
            patch.object(chat.time, "monotonic", return_value=100.0),
            patch.object(chat.socket, "getaddrinfo", return_value=rows),
        ):
            policy.enable(timeout=300)
        self.assertGreaterEqual(policy.deadline, 400.0)

    def test_bounded_input_and_non_object_rejected(self) -> None:
        for text in ("[]", '"x"', " " * (chat.MAX_BYTES + 1)):
            with self.assertRaises(ValueError):
                chat.read_json(io.StringIO(text))

    def test_policy_denies_metadata_scans_bind_and_process(self) -> None:
        policy = chat.ModelPolicy(chat.RUNS, "model.example", 443, None)
        for event, args in (
            ("socket.getaddrinfo", ("169.254.169.254", 80)),
            ("socket.connect", (None, ("127.0.0.1", 443))),
            ("socket.bind", (None, ("127.0.0.1", 9000))),
            ("subprocess.Popen", ("/usr/bin/true",)),
            ("open", (str(chat.engine.HOST / "config.yaml"), "r", 0)),
        ):
            with self.assertRaises(PermissionError):
                policy.audit(event, args)
        policy.active = True
        policy.addresses = {"1.1.1.1"}
        policy.deadline = float("inf")
        for _ in range(4):
            policy.audit("socket.connect", (None, ("1.1.1.1", 443)))
        with self.assertRaises(PermissionError):
            policy.audit("socket.connect", (None, ("1.1.1.1", 443)))

    def test_real_entry_fake_http_single_request_and_persistent_text(self) -> None:
        requests: list[dict[str, object]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                requests.append(json.loads(body))
                payload = json.dumps(
                    {
                        "id": "fake-controlled-response",
                        "model": "fake-test-model",
                        "object": "chat.completion",
                        "created": 1,
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "FAKE_HTTP_NONCE_8ed"},
                            }
                        ],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 6, "total_tokens": 17},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("x-request-id", "fake-controlled-request")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(dir=Path.home(), prefix="fake-") as directory:
                root = Path(directory)
                env = chat.engine.environment(root)
                for key in (
                    "HOME",
                    "HERMES_HOME",
                    "HERMES_RUNTIME_DIR",
                    "TMPDIR",
                    "XDG_CACHE_HOME",
                    "XDG_CONFIG_HOME",
                ):
                    Path(env[key]).mkdir(parents=True, exist_ok=True, mode=0o700)
                read_fd, write_fd = os.pipe()
                os.write(write_fd, json.dumps(route(server.server_port)).encode())
                os.close(write_fd)
                try:
                    process = subprocess.run(
                        [
                            str(chat.engine.PYTHON),
                            "-I",
                            "-S",
                            "-B",
                            str(ENTRY),
                            "--credentials-fd",
                            str(read_fd),
                            "--data-dir",
                            str(root),
                            "--test-local-port",
                            str(server.server_port),
                        ],
                        input=json.dumps(
                            {"prompt": "Return FAKE_HTTP_NONCE_8ed", "session_id": "fake-session"}
                        ),
                        env=env,
                        cwd=root,
                        pass_fds=(read_fd,),
                        capture_output=True,
                        text=True,
                        timeout=65,
                    )
                finally:
                    os.close(read_fd)
                result = json.loads(process.stdout)

                self.assertEqual(process.returncode, 0, result)
                self.assertEqual(result["text"], "FAKE_HTTP_NONCE_8ed")
                self.assertEqual(result["response_id"], "fake-controlled-response")
                self.assertEqual(result["request_id"], "fake-controlled-request")
                self.assertEqual(result["scope"], "controlled fake HTTP")
                self.assertEqual(result["host_module_sources"], {})
                self.assertEqual(len(requests), 1)
                self.assertNotIn("tools", requests[0])
                self.assertEqual(requests[0]["reasoning_effort"], "high")
                self.assertTrue(Path(result["database"]).is_file())
                import sqlite3

                with sqlite3.connect(result["database"]) as connection:
                    rows = connection.execute(
                        "SELECT role,content FROM messages WHERE session_id=? ORDER BY id",
                        ("fake-session",),
                    ).fetchall()
                self.assertEqual(
                    rows,
                    [("user", "Return FAKE_HTTP_NONCE_8ed"), ("assistant", "FAKE_HTTP_NONCE_8ed")],
                )
                self.assertNotIn("FAKE-NONSECRET", process.stdout + process.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class TurnTests(unittest.TestCase):
    """Exercise the real entry and SessionDB; only the HTTP provider is fake."""

    def setUp(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.text = "baseline-assistant"
        self.response_texts: list[str] = []
        self.status = 200
        self.response_delay = 0.0
        self.effort = "high"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                owner.requests.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                payload = json.dumps(
                    {
                        "id": "fake-response",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "fake-test-model",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": owner.response_texts.pop(0)
                                    if owner.response_texts
                                    else owner.text,
                                },
                            }
                        ],
                    }
                ).encode()
                time.sleep(owner.response_delay)
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("x-request-id", "fake-request")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.directory = tempfile.TemporaryDirectory(dir=Path.home(), prefix="turn-")
        self.root = Path(self.directory.name)
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def launch(
        self,
        prompt: str = "next-user",
        mode: str = "chat_completions",
        *,
        persona_mode: str | None = None,
        session_id: str = "turn-session",
        source_channel: str = "web",
    ) -> dict[str, Any]:
        env = chat.engine.environment(self.root)
        for key in (
            "HOME",
            "HERMES_HOME",
            "HERMES_RUNTIME_DIR",
            "TMPDIR",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
        ):
            Path(env[key]).mkdir(parents=True, exist_ok=True, mode=0o700)
        read_fd, write_fd = os.pipe()
        value = route(self.server.server_port)
        value["api_mode"] = mode
        value["reasoning_effort"] = self.effort
        request = {"prompt": prompt, "session_id": session_id, "source_channel": source_channel}
        if persona_mode is not None:
            request["persona_mode"] = persona_mode
        os.write(write_fd, json.dumps(value).encode())
        os.close(write_fd)
        try:
            process = subprocess.run(
                [
                    str(chat.engine.PYTHON),
                    "-I",
                    "-S",
                    "-B",
                    str(ENTRY),
                    "--credentials-fd",
                    str(read_fd),
                    "--data-dir",
                    str(self.root),
                    "--test-local-port",
                    str(self.server.server_port),
                ],
                input=json.dumps(request),
                env=env,
                cwd=self.root,
                pass_fds=(read_fd,),
                capture_output=True,
                text=True,
                timeout=chat.launcher_timeout_seconds(self.effort),
            )
        finally:
            os.close(read_fd)
        self.assertNotIn("FAKE-NONSECRET", process.stdout + process.stderr)
        result: dict[str, Any] = json.loads(process.stdout)
        self.assertEqual(process.returncode, 0 if result["ok"] else 1)
        return result

    def rows(self) -> list[tuple[Any, ...]]:
        import sqlite3

        with sqlite3.connect(self.root / "engine-home/state.db") as db:
            return db.execute("SELECT * FROM messages ORDER BY id").fetchall()

    def test_inner_utf8_boundary_is_inclusive_and_overflow_never_calls_provider(self) -> None:
        from context_limits import INNER_CONTEXT_BYTE_LIMIT

        for character in ("x", "界", "😀", "\0"):
            with self.subTest(character=repr(character)):
                width = len(character.encode("utf-8"))
                exact = character * (INNER_CONTEXT_BYTE_LIMIT // width)
                exact += "x" * (INNER_CONTEXT_BYTE_LIMIT - len(exact.encode("utf-8")))
                before = len(self.requests)
                result = self.launch(exact, persona_mode="inner", session_id=f"boundary-{before}")
                self.assertTrue(result["ok"], result)
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.requests[-1]["messages"][-1]["content"], exact)
                result = self.launch(
                    exact + "x", persona_mode="inner", session_id=f"overflow-{before}"
                )
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["error_type"], "ValueError")
                self.assertEqual(len(self.requests), before + 1)

    def test_inner_empty_body_replays_identical_request_once_and_exposes_receipts(self) -> None:
        from inner_runtime import validate

        good = json.dumps(
            {
                "thoughts": [
                    {"id": "t1", "kind": "idle", "text": "待续", "refs": [], "question": None}
                ],
                "resolved_questions": [],
            }
        )
        self.response_texts = ["", good]
        result = self.launch("frozen-context", persona_mode="inner")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["generation_degraded"])
        self.assertEqual(result["text"], good)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0], self.requests[1])
        self.assertEqual(self.requests[0]["max_tokens"], chat.INNER_MAX_OUTPUT_TOKENS)
        self.assertEqual(len(result["model_attempts"]), 2)
        self.assertEqual(result["model_attempts"][1]["retry_reason"], "empty_body")
        validate(result["text"], set(), [], [])
        self.assertEqual(len(self.rows()), 2)

    def test_inner_two_empty_bodies_stop_and_remain_invalid(self) -> None:
        from inner_runtime import validate

        self.text = ""
        result = self.launch("frozen-context", persona_mode="inner")
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(result["model_attempts"]), 2)
        with self.assertRaisesRegex(ValueError, "Model output empty or exceeds bounded parse size"):
            validate(result["text"], set(), [], [])

    def test_chat_empty_body_never_replays(self) -> None:
        self.text = ""
        result = self.launch("frozen-context", persona_mode="chat")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["generation_degraded"])
        self.assertEqual(len(self.requests), 1)

    def test_inner_duplicate_keys_and_outreach_provenance_never_replay(self) -> None:
        from inner_runtime import validate

        bodies = [
            '{"question":null,"question":{}}',
            json.dumps(
                {
                    "thoughts": [],
                    "outreach_intents": [
                        {
                            "reason": "事实",
                            "evidence": ["t1"],
                            "urgency": "normal",
                            "needs_response": False,
                        }
                    ],
                }
            ),
        ]
        for index, body in enumerate(bodies):
            self.text = body
            before = len(self.requests)
            result = self.launch(
                "frozen-context", persona_mode="inner", session_id=f"invalid-{index}"
            )
            self.assertEqual(len(self.requests), before + 1)
            self.assertEqual(result["text"], body)
            with self.assertRaises(ValueError):
                validate(result["text"], set(), [], [])

    def test_chat_utf8_boundary_is_inclusive_and_not_character_based(self) -> None:
        from context_limits import CHAT_CONTEXT_BYTE_LIMIT

        exact = "界" * (CHAT_CONTEXT_BYTE_LIMIT // 3) + "x" * (CHAT_CONTEXT_BYTE_LIMIT % 3)
        result = self.launch(exact)
        self.assertTrue(result["ok"], result)
        before = len(self.requests)
        rows = self.rows()
        result = self.launch(exact + "x")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error_type"], "ValueError")
        self.assertEqual(self.rows(), rows)
        self.assertEqual(len(self.requests), before)

    def test_inner_system_prompt_and_no_extraction_for_eligible_input(self) -> None:
        import sqlite3

        prompt = "我喜欢喝绿茶。"
        self.assertTrue(chat.memory.extraction_eligible(prompt))
        memories = chat.memory.MemoryStore(self.root / "memory.db")
        existing = memories.add("我喜欢喝红茶。", "seed-session", "seed-user")
        self.assertIsNotNone(existing)
        facts = memories.search(prompt)
        self.assertEqual(len(facts), 1)
        self.text = json.dumps({"facts": [prompt]}, ensure_ascii=False)

        result = self.launch(prompt, persona_mode="inner")

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.requests), 1, "inner turns must never call extraction")
        self.assertEqual(
            self.requests[0]["messages"],
            [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                },
                {"role": "user", "content": prompt},
            ],
        )
        self.assertEqual(result["memory_extraction"], "not_eligible")
        self.assertEqual(result["memory_saved_ids"], [])
        self.assertEqual(len(result["memory_sources"]), 1)
        self.assertEqual(len(self.rows()), 2)
        with sqlite3.connect(self.root / "memory.db") as db:
            self.assertEqual(
                db.execute("SELECT content FROM facts").fetchall(), [("我喜欢喝红茶。",)]
            )

    def assert_chat_persona_and_extraction(self, persona_mode: str | None) -> None:
        prompt = "我喜欢喝绿茶。"
        self.text = json.dumps({"facts": [prompt]}, ensure_ascii=False)
        result = self.launch(prompt, persona_mode=persona_mode)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.requests), 2)
        system = self.requests[0]["messages"][0]
        self.assertEqual(system["role"], "system")
        self.assertIn("当前感受", system["content"])
        self.assertIn("照办缘由", system["content"])
        self.assertNotRegex(system["content"], r"warmth|patience|sarcasm|0\.60|0\.70")
        self.assertEqual(self.requests[1]["messages"][-1], {"role": "user", "content": prompt})
        self.assertEqual(result["memory_extraction"], "completed")
        self.assertEqual(len(result["memory_saved_ids"]), 1)
        self.assertEqual(len(self.rows()), 2)

    def test_affect_decision_and_known_fact_reach_fake_model_across_restart(self) -> None:
        store = chat.memory.MemoryStore(self.root / "memory.db")
        store.add("我住在天水。", "timeline", "direct-sir")
        self.text = "unique-live-answer"
        result = self.launch("我住哪？")
        self.assertEqual(result["text"], self.text)
        system = self.requests[-1]["messages"][0]["content"]
        self.assertIn("我住在天水。", system)
        self.assertIn("随后", system)
        self.assertNotIn("answer_fact", system)
        self.assertNotIn("tease_answer", system)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue((self.root / "affect.db").is_file())
        self.assertTrue(self.launch("你个废物垃圾蠢货")["ok"])
        self.assertTrue(self.launch("你个废物垃圾蠢货")["ok"])
        self.assertTrue(self.launch("我胸口疼")["ok"])
        system = self.requests[-1]["messages"][0]["content"]
        self.assertIn("立即", system)
        self.assertIn("关心压过不快", system)
        self.assertNotRegex(system, r"irritation|grudge|priority|0\.[0-9]")
        import sqlite3

        with sqlite3.connect(self.root / "affect.db") as connection:
            self.assertGreaterEqual(
                connection.execute("SELECT count(*) FROM events").fetchone()[0], 4
            )

    def test_default_chat_system_prompt_and_extraction_unchanged(self) -> None:
        self.assert_chat_persona_and_extraction(None)

    def test_explicit_chat_system_prompt_and_extraction_unchanged(self) -> None:
        self.assert_chat_persona_and_extraction("chat")

    def test_context_window_preserves_memory_emotion_and_pending_tasks(self) -> None:
        import sqlite3

        import affect_engine

        self.seed_history(42)
        memories = chat.memory.MemoryStore(self.root / "memory.db")
        fact = memories.add("我喜欢喝红茶。", "legacy-source", "seed-user")
        affect = affect_engine.AffectEngine(self.root, clock=lambda: 1000.0)
        affect.handle("你个废物垃圾蠢货")
        with sqlite3.connect(affect.path) as connection:
            connection.execute(
                "INSERT INTO todos(created_at,task,recovery) VALUES(?,?,?)",
                (1000.0, "pending-boundary-check", "preserve"),
            )
        before = affect.path.read_bytes()
        identity = "jarvis-timeline-window-test"
        result = self.launch("红茶怎么样", session_id=identity)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["session_id"], identity)
        self.assertEqual(len(self.rows()), 44, "context trimming cannot delete history")
        self.assertLessEqual(len(self.requests[-1]["messages"]), 40)
        self.assertEqual(memories.list()[0]["id"], fact["id"])
        self.assertNotEqual(affect.path.read_bytes(), before)
        with sqlite3.connect(affect.path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM todos WHERE task='pending-boundary-check'"
                ).fetchone()[0],
                1,
            )
        self.assertTrue(self.launch("继续", session_id=identity)["ok"])
        self.assertEqual(len(self.rows()), 46)
        with sqlite3.connect(affect.path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM todos WHERE task='pending-boundary-check'"
                ).fetchone()[0],
                1,
            )

    def test_source_channel_is_preserved_in_message_metadata(self) -> None:
        import sqlite3

        self.assertTrue(self.launch("channel-check", source_channel="voice")["ok"])
        with sqlite3.connect(self.root / "engine-home/state.db") as db:
            metadata = [
                row[0]
                for row in db.execute(
                    "SELECT display_metadata FROM messages ORDER BY id"
                ).fetchall()
            ]
        self.assertEqual(
            [json.loads(value)["source_channel"] for value in metadata],
            ["voice", "voice"],
        )

    def test_assistant_insert_failure_rolls_back_pair_without_model_retry(self) -> None:
        import sqlite3

        self.assertTrue(self.launch("original-user")["ok"])
        before = self.rows()
        with sqlite3.connect(self.root / "engine-home/state.db") as db:
            db.execute(
                "CREATE TRIGGER deny_assistant BEFORE INSERT ON messages "
                "WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT,'deny'); END"
            )
        result = self.launch()
        self.assertEqual(self.rows(), before, "assistant failure must add zero transcript rows")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "IntegrityError")
        self.assertEqual(result["model_request_state"], "response_received")
        self.assertFalse(result["local_turn_saved"])
        self.assertFalse(result["retry_allowed"])
        self.assertEqual(result["response_id"], "fake-response")
        self.assertEqual(result["request_id"], "fake-request")
        self.assertEqual(len(self.requests), 2)

    def seed_history(self, count: int) -> None:
        import sqlite3

        self.assertTrue(self.launch("original-user")["ok"])
        with sqlite3.connect(self.root / "engine-home/state.db") as db:
            for index in range(2, count):
                db.execute(
                    "INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
                    (
                        "turn-session",
                        "user" if index % 2 == 0 else "assistant",
                        f"history-{index}",
                        float(index),
                    ),
                )

    def test_history_preserved_at_38_and_rejected_before_growth_at_39_40_42(self) -> None:
        self.seed_history(38)
        self.assertTrue(self.launch()["ok"])
        self.assertEqual(len(self.rows()), 40)
        self.assertEqual(len(self.requests[-1]["messages"]), 40)
        self.assertEqual(
            [message["role"] for message in self.requests[-1]["messages"]],
            ["system"] + ["user", "assistant"] * 19 + ["user"],
        )
        self.assertIn("Sir", self.requests[-1]["messages"][0]["content"])
        self.assertEqual(self.requests[-1]["messages"][1]["content"], "original-user")
        before = self.rows()
        requests = len(self.requests)
        result = self.launch()
        self.assertFalse(result["ok"], "40-row history cannot grow to 42")
        self.assertEqual(self.rows(), before)
        self.assertEqual(len(self.requests), requests)
        import sqlite3

        with sqlite3.connect(self.root / "engine-home/state.db") as db:
            db.execute("DELETE FROM messages WHERE id=(SELECT MAX(id) FROM messages)")
        before = self.rows()
        self.assertEqual(len(before), 39)
        self.assertFalse(self.launch()["ok"])
        self.assertEqual(self.rows(), before)
        self.assertEqual(len(self.requests), requests)
        with sqlite3.connect(self.root / "engine-home/state.db") as db:
            for index in range(3):
                db.execute(
                    "INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
                    ("turn-session", "user", f"overflow-{index}", float(index)),
                )
        before = self.rows()
        self.assertEqual(len(before), 42)
        self.assertFalse(self.launch()["ok"])
        self.assertEqual(self.rows(), before)
        self.assertEqual(len(self.requests), requests)

    def test_utf8_response_limit_rejects_oversize_without_saving_or_retry(self) -> None:
        self.assertTrue(self.launch("original-user")["ok"])
        before = self.rows()
        self.text = "é" * 65536 + "x"
        result = self.launch()
        self.assertFalse(result["ok"], "131073 UTF-8 bytes must be rejected")
        self.assertEqual(self.rows(), before)
        self.assertEqual(result["model_request_state"], "response_received")
        self.assertFalse(result["local_turn_saved"])
        self.assertFalse(result["retry_allowed"])
        self.assertEqual(len(self.requests), 2)
        self.text = "é" * 65536
        result = self.launch()
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["text"].encode("utf-8")), 131072)
        self.assertEqual(len(self.rows()), 4)

    def test_high_reasoning_waits_beyond_old_http_and_child_deadlines(self) -> None:
        self.response_delay = 92.0
        start = time.monotonic()
        result = self.launch("delayed-budget-check")
        self.assertGreater(time.monotonic() - start, 90)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.rows()), 2)

    def test_http_402_is_provider_rejected_with_safe_status(self) -> None:
        self.status = 402
        result = self.launch("provider-402")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["generation_degraded"])
        self.assertEqual(result["model_request_state"], "provider_rejected")
        self.assertEqual(result["provider_status"], 402)
        self.assertEqual(result["provider_error_type"], "http_4xx")
        self.assertNotIn("Insufficient", json.dumps(result))

    def test_http_429_is_provider_rejected_with_safe_status(self) -> None:
        self.status = 429
        result = self.launch("provider-429")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["generation_degraded"])
        self.assertEqual(result["model_request_state"], "provider_rejected")
        self.assertEqual(result["provider_status"], 429)
        self.assertEqual(result["provider_error_type"], "http_4xx")

    def test_real_timeout_remains_dispatched_unknown(self) -> None:
        self.effort = "none"
        self.response_delay = 31.0
        result = self.launch("provider-timeout")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["generation_degraded"])
        self.assertEqual(result["model_request_state"], "dispatched_unknown")
        self.assertNotIn("provider_status", result)
        self.assertEqual(len(self.requests), 1)


class ProviderRejectionClassificationTests(unittest.TestCase):
    def test_http_402_and_429_are_known_rejections_and_timeout_is_unknown(self) -> None:
        class FakeAPIStatusError(Exception):
            def __init__(self, status_code: int) -> None:
                super().__init__("provider body contains a secret and must not escape")
                self.status_code = status_code

        for status in (402, 429):
            with self.subTest(status=status):
                metadata = chat.provider_failure_metadata(FakeAPIStatusError(status))
                self.assertEqual(
                    metadata,
                    {
                        "provider_status": status,
                        "provider_error_type": "http_4xx",
                        "provider_error_code": "provider_http_status",
                    },
                )
                state = {"model_request_state": "dispatched_unknown"}
                state.update(metadata or {})
                state["model_request_state"] = "provider_rejected"
                self.assertEqual(state["model_request_state"], "provider_rejected")
                self.assertTrue(status in (402, 429))

        self.assertIsNone(chat.provider_failure_metadata(TimeoutError("deadline")))

    def test_non_http_exception_never_exports_provider_text(self) -> None:
        class FakeError(Exception):
            pass

        metadata = chat.provider_failure_metadata(
            FakeError("https://secret.invalid/token=redacted")
        )
        self.assertIsNone(metadata)

    def test_timeout_keeps_dispatched_unknown_state(self) -> None:
        result = {
            "model_request_state": "dispatched_unknown",
            "generation_degraded": True,
            "retry_allowed": False,
        }
        metadata = chat.provider_failure_metadata(TimeoutError("deadline"))
        if metadata is not None:
            result.update(metadata)
            result["model_request_state"] = "provider_rejected"
        self.assertEqual(result["model_request_state"], "dispatched_unknown")
        self.assertNotIn("provider_status", result)


class RequestCaptureTests(unittest.TestCase):
    """The capture is observation only: same bytes, private file, never fatal."""

    def test_capture_records_exact_messages_and_is_bounded(self):
        import json as _json
        import stat
        import tempfile
        from pathlib import Path

        from chat_entry import capture_request

        with tempfile.TemporaryDirectory(prefix="jarvis-capture-", dir=Path.home()) as folder:
            root = Path(folder)
            root.chmod(0o700)
            messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
            snapshot = _json.dumps(messages)
            capture_request(root, messages, "inner", "jarvis-timeline-x")
            path = root / "request-capture.jsonl"
            self.assertEqual(_json.dumps(messages), snapshot)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            record = _json.loads(path.read_text().strip())
            self.assertEqual(record["messages"], messages)
            self.assertEqual(record["messages_bytes"], len(snapshot.encode()))
            self.assertEqual(record["history_count"], 0)
            self.assertEqual(len(record["system_sha256"]), 64)

    def test_capture_never_raises_on_unwritable_root(self):
        from pathlib import Path

        from chat_entry import capture_request

        capture_request(
            Path("/nonexistent-root-for-capture"), [{"role": "system", "content": ""}], "inner", "s"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
