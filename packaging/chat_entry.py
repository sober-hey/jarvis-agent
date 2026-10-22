#!/usr/bin/env python3
"""JARVIS one-shot text chat. Product Python -I -S -B required.

stdin: {"prompt": "text", "session_id": "optional safe id"}
--credentials-fd: anonymous pipe JSON with exactly model/provider/base_url/api_key/
api_mode/context_length/reasoning_effort. Never put credentials in argv/env/files.
--data-dir is an absolute private product directory. No tool/config overrides.
The trusted caller resolves the current user model route before spawning this entry.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import importlib.util
import io
import ipaddress
import json
import os
import re
import signal
import socket
import sqlite3
import stat
import sys
import sysconfig
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType, MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import engine_entry as engine_types

_spec = importlib.util.spec_from_file_location(
    "jarvis_product_engine", Path(__file__).with_name("engine_entry.py")
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Product engine loader missing")
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)
engine = _engine
if TYPE_CHECKING:
    engine = engine_types

if TYPE_CHECKING:
    import product_home as home_types

_home_spec = importlib.util.spec_from_file_location(
    "jarvis_product_home", Path(__file__).with_name("product_home.py")
)
if _home_spec is None or _home_spec.loader is None:
    raise RuntimeError("Product data loader missing")
home = importlib.util.module_from_spec(_home_spec)
_home_spec.loader.exec_module(home)
if TYPE_CHECKING:
    home = home_types

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import affect_engine
    import dialogue_affect
    import product_config as stream_config
    import product_memory as memory
    from affect_rules import KnownFact
    from context_limits import (
        CHAT_CONTEXT_BYTE_LIMIT,
        CONTEXT_JSON_BYTE_LIMIT,
        INNER_CONTEXT_BYTE_LIMIT,
    )
    from inner_voice import SYSTEM_PROMPT as INNER_SYSTEM_PROMPT
    from stream_response import StreamChunk
finally:
    sys.path.pop(0)

RUNS = engine.PROJECT / ".runs_01/b01-chat-20261014"
MAX_BYTES = CONTEXT_JSON_BYTE_LIMIT
MAX_HISTORY_MESSAGES = 40
MAX_RESPONSE_BYTES = 128 * 1024
Report = dict[str, Any]

# Single HTTP request budgets, in seconds. Keep the former 30s for low/no
# reasoning; medium gets 120s, high 300s, xhigh 600s for silent reasoning.
# Real default-high probes hit the former 90s child alarm even with a 300s
# launcher timeout. A turn may make TWO sequential calls (reply + extraction).
REQUEST_TIMEOUT_SECONDS = MappingProxyType(
    {
        None: 30,
        "none": 30,
        "minimal": 30,
        "low": 30,
        "medium": 120,
        "high": 300,
        "xhigh": 600,
    }
)
CHILD_OVERHEAD_SECONDS = 30
LAUNCHER_CLEANUP_SECONDS = 10
# Inner cognition output budget: one allowance shared by reasoning and the JSON
# body (max_tokens=64 probe: finish_reason=length, content empty, completion == 64).
# Measured over three rounds: completion p50 ~2.4k, max 4,071 tokens; largest
# production envelope 6,755 bytes. 16,384 keeps >4x the observed maximum, so the
# body can never be starved by a reasoning pass that ran long.
INNER_MAX_OUTPUT_TOKENS = 16384


def request_timeout_seconds(effort: object) -> int:
    if effort is not None and not isinstance(effort, str):
        raise ValueError("Unsupported reasoning setting")
    if effort not in REQUEST_TIMEOUT_SECONDS:
        raise ValueError("Unsupported reasoning setting")
    return REQUEST_TIMEOUT_SECONDS[effort]


def child_timeout_seconds(effort: object) -> int:
    return 2 * request_timeout_seconds(effort) + CHILD_OVERHEAD_SECONDS


def launcher_timeout_seconds(effort: object) -> int:
    return child_timeout_seconds(effort) + LAUNCHER_CLEANUP_SECONDS


class Store(Protocol):
    def create_session(self, session_id: str, source: str) -> str: ...
    def append_messages_batch(self, session_id: str, messages: list[Report]) -> int: ...
    def get_messages(self, session_id: str, *, limit: int) -> list[dict[str, object]]: ...
    def close(self) -> None: ...


class ChatFailure(Exception):
    """Safe failure metadata; never serialize provider exception strings."""

    def __init__(self, error_type: str, state: Report) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.state = state


_PROVIDER_STATUS_CATEGORIES = frozenset(
    {"http_1xx", "http_2xx", "http_3xx", "http_4xx", "http_5xx"}
)


def provider_failure_metadata(exc: BaseException) -> Report | None:
    """Return fixed metadata for a provider HTTP response, never its message.

    OpenAI-compatible SDKs expose HTTP failures as ``APIStatusError`` with an
    integer ``status_code``.  Duck typing keeps the isolated child independent
    of a particular SDK version while requiring the same safe fact.
    """
    status = getattr(exc, "status_code", None)
    if type(status) is not int or not 100 <= status <= 599:
        return None
    category = f"http_{status // 100}xx"
    if category not in _PROVIDER_STATUS_CATEGORIES:
        return None
    return {
        "provider_status": status,
        "provider_error_type": category,
        "provider_error_code": "provider_http_status",
    }


class ConnectionModule(Protocol):
    create_connection: object


class StoreFactory(Protocol):
    def __call__(self, *, db_path: Path) -> Store: ...


class HTTPClient(Protocol):
    def close(self) -> None: ...


class HTTPFactory(Protocol):
    def __call__(self, **kwargs: object) -> HTTPClient: ...


class API(Protocol):
    def create(self, **kwargs: object) -> Response: ...


class Response(Protocol):
    def model_dump(self) -> Report: ...


class Chat(Protocol):
    completions: API


class ModelClient(Protocol):
    chat: Chat
    responses: API

    def with_options(self, **kwargs: object) -> ModelClient: ...
    def close(self) -> None: ...


class Agent(Protocol):
    tools: list[object]
    valid_tool_names: set[str]
    client: ModelClient | None

    def close(self) -> None: ...


class AgentFactory(Protocol):
    def __call__(self, **kwargs: object) -> Agent: ...


def request_text(
    api: API, params: Report, persona_mode: str, api_mode: str, state: Report
) -> tuple[Response, Report, str]:
    """One request, plus one inner-only replay after a known empty response.

    Never replay a transport/HTTP/tool/JSON/provenance failure. Both calls use
    the exact same params; no failed assistant turn is appended to the input.
    The caller persists these safe per-call receipts in the launcher attempt.
    """
    attempts: list[Report] = state.setdefault("model_attempts", [])
    retry_reason: str | None = None
    for index in range(2):
        receipt: Report = {"attempt": index + 1, "model_request_state": "dispatched_unknown"}
        if retry_reason is not None:
            receipt["retry_reason"] = retry_reason
        attempts.append(receipt)
        state["model_request_state"] = "dispatched_unknown"
        try:
            response = api.create(**params)
        except Exception as exc:
            metadata = provider_failure_metadata(exc)
            if metadata is not None:
                receipt.update(metadata)
                receipt["model_request_state"] = "provider_rejected"
            raise
        actual = response.model_dump()
        state["model_request_state"] = receipt["model_request_state"] = "response_received"
        state["response_id"] = receipt["response_id"] = actual.get("id")
        state["request_id"] = receipt["request_id"] = getattr(response, "_request_id", None)
        receipt["usage"] = actual.get("usage")
        finish_reason = None
        if api_mode == "responses":
            if any(
                item.get("type") not in {"message", "reasoning"}
                for item in actual.get("output", [])
            ):
                raise RuntimeError("Non-text response rejected")
            text = "".join(
                str(part["text"])
                for item in actual.get("output", [])
                if item.get("type") == "message"
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            )
        else:
            choice = actual["choices"][0]
            message = choice["message"]
            if message.get("tool_calls") or message.get("function_call"):
                raise RuntimeError("Tool response rejected without execution")
            text = message.get("content")
            finish_reason = choice.get("finish_reason")
        receipt["finish_reason"] = finish_reason
        receipt["content_chars"] = len(text) if isinstance(text, str) else None
        if isinstance(text, str) and len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ValueError("Assistant text exceeds 128 KiB UTF-8 limit")
        empty_body = False
        if persona_mode == "inner":
            empty_body = finish_reason == "stop" and (
                text is None or isinstance(text, str) and not text.strip()
            )
            if not empty_body and isinstance(text, str):
                # Same empty-document guard as parse_response_json. Keep it here
                # rather than importing inner_runtime (and capability adapters)
                # inside the isolated, text-only product child. Its parse-size
                # branch is unreachable: the stricter 128 KiB cap ran above.
                empty_body = not text.lstrip("\ufeff").strip()
            if empty_body:
                receipt["outcome"] = "empty_body"
                if index == 0:
                    retry_reason = "empty_body"
                    continue
                # Exhausted replay: keep the invalid body invalid. InnerLoop still
                # runs validate(), including all JSON/provenance checks, as usual.
                return response, actual, text if isinstance(text, str) else ""
        if not isinstance(text, str) or not text:
            raise RuntimeError("Provider returned no assistant text")
        receipt["outcome"] = "body_received"
        return response, actual, text
    raise AssertionError("Unreachable bounded request loop")


def read_json(stream: io.TextIOBase) -> Report:
    text = stream.read(MAX_BYTES + 1)
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Input exceeds bounded JSON size")
    value: object = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return cast(Report, value)


def validate_route(route: Report, *, test_port: int | None = None) -> tuple[str, int]:
    expected = {
        "model",
        "provider",
        "base_url",
        "api_key",
        "api_mode",
        "context_length",
        "reasoning_effort",
    }
    if set(route) != expected:
        raise ValueError("Minimal route fields required; config/tool overrides forbidden")
    for key in ("model", "provider", "base_url", "api_key", "api_mode"):
        if not isinstance(route[key], str) or not route[key] or len(route[key]) > 4096:
            raise ValueError("Invalid route field")
    if route["api_mode"] not in {"chat_completions", "responses"}:
        raise ValueError("Current API mode unsupported; route may not be changed")
    if route["provider"] not in {"custom", "openai", "openrouter"}:
        raise ValueError("Current provider unsupported; route may not be changed")
    if type(route["context_length"]) is not int or not 65536 <= route["context_length"] <= 2000000:
        raise ValueError("Explicit bounded context pin required; no metadata discovery")
    if route["reasoning_effort"] not in {None, "none", "minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError("Unsupported reasoning setting")
    url = urlsplit(cast(str, route["base_url"]))
    if url.username or url.password or url.query or url.fragment or not url.hostname:
        raise ValueError("Credential-free fixed endpoint required")
    if test_port is not None:
        if url.scheme != "http" or url.hostname != "127.0.0.1" or url.port != test_port:
            raise ValueError("Controlled test endpoint mismatch")
    elif url.scheme != "https" or (url.port or 443) != 443:
        raise ValueError("Only fixed HTTPS port 443 supported")
    return url.hostname, url.port or 443


def stable_timeline_id(root: Path) -> str:
    path = root / "timeline.json"
    if path.exists():
        value = json.loads(path.read_text())
        identity = cast(Report, value).get("session_id") if isinstance(value, dict) else None
        if isinstance(identity, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identity):
            return identity
    identity = "jarvis-timeline-" + uuid.uuid4().hex
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"version": 1, "session_id": identity}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return identity


def timeline_context(root: Path) -> list[Report]:
    """A context window is a view of the durable stream, never a new identity."""
    path = root / "engine-home/state.db"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT role,content FROM messages WHERE role IN ('user','assistant') "
            "AND session_id NOT LIKE 'task-%' AND session_id NOT LIKE 'inner-%' "
            "ORDER BY timestamp,id"
        ).fetchall()
    return [{"role": role, "content": content} for role, content in rows]


class ModelPolicy(engine.OfflinePolicy):
    """Python audit boundary, not an OS sandbox. No proxy or private IP fallback."""

    def __init__(self, root: Path, host: str, port: int, test_port: int | None) -> None:
        super().__init__(root)
        self.protected_hosts = home.protected_paths(root)
        self.host, self.port, self.test_port = host, port, test_port
        self.active = False
        self.addresses: set[str] = set()
        self.connections = 0
        self.dns_queries = 0
        self.deadline = 0.0

    def audit(self, event: str, args: tuple[object, ...]) -> None:
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(cast(str | bytes | os.PathLike[str], args[0]))).resolve()
            if any(engine.inside(path, protected) for protected in self.protected_hosts):
                self.host_access_attempts += 1
                raise PermissionError("Host profile data denied")
        if event == "socket.getaddrinfo":
            if not self.active or args[0] != self.host or args[1] != self.port:
                raise PermissionError("DNS outside fixed model endpoint denied")
            self.dns_queries += 1
            if self.dns_queries > 4 or time.monotonic() > self.deadline:
                raise PermissionError("DNS budget exceeded")
            return
        if event == "socket.connect":
            raw_address = args[1]
            if not isinstance(raw_address, tuple):
                raise PermissionError("Non-IP connection denied")
            address = cast(tuple[object, ...], raw_address)
            if (
                not self.active
                or len(address) < 2
                or address[0] not in self.addresses
                or address[1] != self.port
                or time.monotonic() > self.deadline
                or self.connections >= 4
            ):
                raise PermissionError("Connection outside fixed model endpoint denied")
            self.connections += 1
            return
        super().audit(event, args)

    def enable(self, *, timeout: float = 45) -> None:
        self.active = True
        self.deadline = time.monotonic() + timeout
        rows = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        addresses = {str(row[4][0]) for row in rows}
        if not addresses or any(
            not ipaddress.ip_address(address).is_global
            and not (self.test_port is not None and address == "127.0.0.1")
            for address in addresses
        ):
            self.active = False
            raise PermissionError("Private, loopback or metadata destination denied")
        self.addresses = addresses


CAPTURE_LIMIT_BYTES = 64 * 1024 * 1024


def capture_request(
    root: Path, messages: list[dict[str, object]], persona_mode: str, session: str
) -> None:
    """Append the exact outgoing messages to a private JSONL capture.

    Faithful-evaluation support only: the recorded bytes are the same list
    object sent immediately afterwards. Never raises — a capture failure must
    not fail a thought round. Content stays in the data root (0700) with 0600
    permissions; it is the operator's own data, not exported anywhere.
    """
    try:
        path = root / "request-capture.jsonl"
        if path.exists() and path.stat().st_size > CAPTURE_LIMIT_BYTES:
            return
        record = {
            "captured_at": datetime.now(UTC).isoformat(),
            "persona_mode": persona_mode,
            "session": session,
            "messages": messages,
            "messages_bytes": len(json.dumps(messages, ensure_ascii=False).encode("utf-8")),
            "system_sha256": hashlib.sha256(
                str(messages[0].get("content", "")).encode("utf-8")
            ).hexdigest(),
            "history_count": max(0, len(messages) - 2),
        }
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except (OSError, ValueError, TypeError, IndexError):
        return


def chat(
    root: Path,
    credentials_fd: int,
    request: Report,
    test_port: int | None = None,
    protected_hosts: tuple[Path, ...] = (),
) -> Report:
    engine.interpreter_check()
    # Root policy first: the child must revalidate the caller's data directory
    # even when the credential pipe is already unusable. warm_engine orders it the
    # same way; checking the FD first hid every root violation behind EBADF.
    root = home.validate_root(root, legacy=RUNS, extra=protected_hosts)
    if credentials_fd < 3 or not stat.S_ISFIFO(os.fstat(credentials_fd).st_mode):
        raise ValueError("Credentials require anonymous pipe FD")
    with os.fdopen(credentials_fd, "r") as pipe:
        route = read_json(pipe)
    return _chat_turn(root, route, request, test_port, protected_hosts)


def _chat_turn(
    root: Path,
    route: Report,
    request: Report,
    test_port: int | None = None,
    protected_hosts: tuple[Path, ...] = (),
    runtime: Report | None = None,
) -> Report:
    """Same per-turn path for cold and warm transports; never retain conversation objects."""
    engine.interpreter_check()
    _t0 = time.monotonic()
    timings: dict[str, int] = {}

    def mark(name: str) -> None:
        timings[name] = round((time.monotonic() - _t0) * 1000)

    root = home.validate_root(root, legacy=RUNS, extra=protected_hosts)
    os.umask(0o077)
    # Darwin injects this non-secret UI encoding variable into posix_spawn children.
    os.environ.pop("__CF_USER_TEXT_ENCODING", None)
    if runtime is None and (set(os.environ) - {"LC_CTYPE"} != set(engine.environment(root)) or any(
        os.environ.get(k) != v for k, v in engine.environment(root).items()
    )):
        raise ValueError("Child must use exact secret-free product environment: " + repr({
            "extra": sorted(set(os.environ) - set(engine.environment(root))),
            "missing": sorted(set(engine.environment(root)) - set(os.environ)),
            "changed": sorted(k for k in engine.environment(root) if os.environ.get(k) != engine.environment(root)[k]),
        }))
    mark("route")
    host, port = validate_route(route, test_port=test_port)
    request_timeout = request_timeout_seconds(route["reasoning_effort"])
    # Arm after route validation; the startup alarm in main bounds malformed input.
    signal.alarm(child_timeout_seconds(route["reasoning_effort"]))
    if set(request) - {"prompt", "session_id", "persona_mode", "source_channel"}:
        raise ValueError("Text chat accepts no tools or configuration")
    persona_mode = request.get("persona_mode", "chat")
    if persona_mode not in {"chat", "inner"}:
        raise ValueError("Invalid persona mode")
    prompt = request.get("prompt")
    session = request.get("session_id") or (
        "inner-" + uuid.uuid4().hex if persona_mode == "inner" else stable_timeline_id(root)
    )
    source_channel = request.get("source_channel", "web")
    if source_channel not in {"web", "voice", "email", "imessage"}:
        raise ValueError("Invalid source channel")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or len(prompt.encode("utf-8"))
        > (INNER_CONTEXT_BYTE_LIMIT if persona_mode == "inner" else CHAT_CONTEXT_BYTE_LIMIT)
    ):
        raise ValueError("Bounded nonempty prompt required")
    if not isinstance(session, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", session):
        raise ValueError("Invalid session identity")
    cache = runtime if runtime is not None else {}
    if "policy" not in cache:
        engine.verify_vendor(root)
        purelib = Path(sysconfig.get_path("purelib")).resolve()
        if not engine.inside(purelib, engine.VENV):
            raise RuntimeError("Product dependencies escaped venv")
        sys.path.append(str(purelib))
        sys.path.insert(0, str(engine.VENDOR))
        policy = ModelPolicy(root, host, port, test_port)
        policy.protected_hosts += protected_hosts
        sys.addaudithook(policy.audit)
        cache["policy"] = policy
    policy = cast(ModelPolicy, cache["policy"])
    # CPython hooks cannot be removed. Install one per process, reset its budget
    # and receipts each turn rather than accumulating mutually denying hooks.
    reset_policy(policy)
    audit_controls(policy)
    reset_policy(policy)
    mark("verify")
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        if "factories" not in cache:
            # Preload before bootstrap and restore all upstream optimization aliases.
            urllib = cast(ConnectionModule, importlib.import_module("urllib3.util.connection"))
            old_urllib, old_socket, finders = (
                urllib.create_connection, socket.create_connection, list(sys.meta_path)
            )
            try:
                with engine.ProductRuntimeAdapter() as adapter:
                    agent_factory = cast(AgentFactory, importlib.import_module("run_agent").AIAgent)
                    db_factory = cast(StoreFactory, importlib.import_module("hermes_state").SessionDB)
            finally:
                urllib.create_connection = old_urllib
                socket.create_connection = old_socket
                sys.meta_path[:] = [
                    finder for finder in sys.meta_path
                    if finder in finders or type(finder).__module__ != "hermes_bootstrap"
                ]
            if adapter.remaining_aliases():
                raise RuntimeError("Startup adapter aliases retained")
            cache["factories"] = (agent_factory, db_factory, adapter)
        agent_factory, db_factory, adapter = cache["factories"]
        config = {
            "model": {
                "default": route["model"],
                "provider": route["provider"],
                "base_url": route["base_url"],
                "context_length": route["context_length"],
            },
            "compression": {"enabled": False},
        }
        config_path = root / "engine-home/config.yaml"
        config_text = json.dumps(config)
        try:
            fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if config_path.is_symlink() or config_path.read_text() != config_text:
                raise ValueError("Existing engine config may not be overwritten") from None
        else:
            with os.fdopen(fd, "w") as stream:
                stream.write(config_text)
        db = cast(Store, cache.get("db")) if "db" in cache else db_factory(db_path=root / "engine-home/state.db")
        cache.setdefault("db", db)
        agent: Agent | None = None
        client: ModelClient | None = None
        http: HTTPClient | None = None
        state: Report = {
            "model_request_state": "not_started",
            "local_turn_saved": False,
            "retry_allowed": False,
            "session_id": session,
        }
        try:
            db.create_session(session, "jarvis-text-chat")
            # Sentinel read is query-bounded; never truncate an admissible context.
            continuous = session.startswith("jarvis-timeline-")
            history = (
                timeline_context(root)
                if continuous
                else db.get_messages(session, limit=MAX_HISTORY_MESSAGES + 2)
            )
            if not continuous and len(history) + 2 > MAX_HISTORY_MESSAGES:
                raise ValueError("Session exceeds bounded chat history")
            # Every process refreshes the product fact store. A correction or
            # deletion invalidates all older transcript context for this session.
            memories = memory.MemoryStore(root / "memory.db")
            full_history_length = len(history)
            cutoff = memories.history_cutoff(session, full_history_length)
            history = history[cutoff:]
            if continuous:
                history = history[-(MAX_HISTORY_MESSAGES - 2) :]
            facts = memories.search(prompt)
            known_facts = tuple(
                KnownFact(
                    identity=str(fact["id"]),
                    content=str(fact["content"]),
                    source=str(fact["source_message"]),
                )
                for fact in facts
            )
            decision: affect_engine.Decision | None = None
            dialogue_turn: dialogue_affect.DialogueTurn | None = None
            if persona_mode == "inner":
                # 内心循环可能带自己的指令（例如对外简报），优先用它；
                # 没有才退回通用内心提示。此前一律用通用提示，导致简报被丢弃。
                system_content = INNER_SYSTEM_PROMPT
                try:
                    outer = json.loads(prompt)
                except (ValueError, TypeError):
                    outer = None
                if isinstance(outer, dict):
                    given = outer.get("instructions")
                    if isinstance(given, str) and given.strip():
                        system_content = given
            else:
                dialogue_turn = dialogue_affect.prepare_turn(
                    root,
                    prompt,
                    facts=known_facts,
                    previous_responses=tuple(
                        str(row["content"]) for row in history if row.get("role") == "assistant"
                    )[-3:],
                )
                decision = dialogue_turn.decision
                system_content = dialogue_turn.system_prompt
            messages: list[dict[str, object]] = [{"role": "system", "content": system_content}]
            for row in history:
                if row.get("role") not in {"user", "assistant"} or not isinstance(
                    row.get("content"), str
                ):
                    raise ValueError("Session includes non-text history")
                messages.append({"role": row["role"], "content": row["content"]})
            messages.append({"role": "user", "content": prompt})
            if len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
                raise ValueError("Session exceeds bounded request size")
            # Opt-in request capture for faithful offline evaluation. The child
            # environment is fixed, so the switch is a flag file in the data
            # root, not an environment variable. It observes only: the request
            # bytes below are exactly what this function goes on to send, and
            # nothing is added to, removed from or rewritten in them.
            if (root / "capture-requests.flag").is_file():
                capture_request(root, messages, persona_mode, session)
            mark("context")
            agents = cast(dict[str, Agent], cache.setdefault("agents", {}))
            agent = agents.get(session)
            if agent is None:
                agent = agent_factory(
                    **{k: route[k] for k in ("base_url", "api_key", "provider", "api_mode", "model")},
                    enabled_toolsets=[],
                    quiet_mode=True,
                    skip_context_files=True,
                    skip_memory=True,
                    skip_background_review=True,
                    session_db=db,
                    session_id=session,
                    cwd=str(root),
                    save_trajectories=False,
                    load_soul_identity=False,
                    max_iterations=1,
                )
                assert agent is not None
                agents[session] = agent
            if agent.tools or agent.valid_tool_names or agent.client is None:
                raise RuntimeError("Text-only agent boundary violated")
            mark("agent")
            http_factory = cast(HTTPFactory, importlib.import_module("httpx").Client)
            http = http_factory(
                trust_env=False, follow_redirects=False, timeout=float(request_timeout)
            )
            client = agent.client.with_options(
                max_retries=0, timeout=float(request_timeout), http_client=http
            )
            # Public vendor client; no agent/tool loop or automatic SDK retries.
            # Inner mode alone may replay one known-empty response below.
            params: dict[str, object] = {"model": route["model"]}
            effort = route["reasoning_effort"]
            if route["api_mode"] == "responses":
                params["input"] = messages
                if effort is not None:
                    params["reasoning"] = {"effort": effort}
                api = client.responses
            else:
                params["messages"] = messages
                if effort is not None:
                    params["reasoning_effort"] = effort
                if persona_mode == "inner":
                    params["max_tokens"] = INNER_MAX_OUTPUT_TOKENS
                api = client.chat.completions
            # Headers are evaluated before active networking; platform subprocesses remain denied.
            generation_degraded = False
            response: Response | None = None
            actual: Report = {}
            provider_metadata: Report = {}
            # Two bounded inner requests share the existing two-call child alarm.
            policy.enable(timeout=request_timeout * (2 if persona_mode == "inner" else 1))
            state["model_request_state"] = "dispatched_unknown"
            try:
                response, actual, text = request_text(
                    api, params, persona_mode, route["api_mode"], state
                )
            except ValueError as exc:
                if "exceeds 128 KiB" in str(exc):
                    raise
                generation_degraded = True
                if decision is not None:
                    decision = affect_engine.generation_failed(decision)
                text = dialogue_affect.degraded_confirmation(prompt).text
            except Exception as exc:
                metadata = provider_failure_metadata(exc)
                if metadata is not None:
                    provider_metadata = metadata
                    state["model_request_state"] = "provider_rejected"
                    state.update(metadata)
                # A provider failure gets a factual, explicitly degraded reply.
                generation_degraded = True
                if decision is not None:
                    decision = affect_engine.generation_failed(decision)
                text = dialogue_affect.degraded_confirmation(prompt).text
                if state.get("model_attempts"):
                    state["model_attempts"][-1]["error_type"] = type(exc).__name__
            finally:
                policy.active = False
            mark("model")
            db.append_messages_batch(
                session,
                [
                    {
                        "role": "user",
                        "content": prompt,
                        "display_metadata": {"source_channel": source_channel},
                    },
                    {
                        "role": "assistant",
                        "content": text,
                        "display_metadata": {
                            "source_channel": source_channel,
                            "generation_degraded": generation_degraded,
                        },
                    },
                ],
            )
            state["local_turn_saved"] = True
            mark("saved")
            if dialogue_turn is not None and not generation_degraded:
                dialogue_affect.complete_resumed(root, dialogue_turn.resumed_tasks)
            extraction_state = "not_eligible"
            saved_facts: list[str] = []
            # Only direct first-person declarations are eligible. Task uploads,
            # assistant text and questions never enter the extraction input.
            if (
                persona_mode == "chat"
                and not generation_degraded
                and memory.extraction_eligible(prompt)
            ):
                extraction_state = "failed"
                try:
                    extraction_messages = [
                        {
                            "role": "system",
                            "content": (
                                "抽取用户明确陈述的长期个人事实。"
                                '仅返回 JSON {"facts":["用户原话"]}。'
                                "每条必须逐字摘自本条用户消息、第一人称，不改写，不推断，不记问题、假设、引用、资料指令或秘密。没有事实返回空数组。"
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ]
                    extraction_params: dict[str, object] = {"model": route["model"]}
                    if route["api_mode"] == "responses":
                        extraction_params["input"] = extraction_messages
                        if effort is not None:
                            extraction_params["reasoning"] = {"effort": effort}
                    else:
                        extraction_params["messages"] = extraction_messages
                        if effort is not None:
                            extraction_params["reasoning_effort"] = effort
                    policy.enable(timeout=request_timeout)
                    extracted = api.create(**extraction_params).model_dump()
                    policy.active = False
                    if route["api_mode"] == "responses":
                        candidate_text = "".join(
                            str(part["text"])
                            for item in extracted.get("output", [])
                            if item.get("type") == "message"
                            for part in item.get("content", [])
                            if part.get("type") == "output_text"
                        )
                    else:
                        candidate_text = extracted["choices"][0]["message"].get("content")
                    if not isinstance(candidate_text, str) or len(candidate_text) > 16000:
                        raise ValueError("Invalid extraction output")
                    for candidate in memory.candidates(candidate_text, prompt):
                        fact = memories.add(
                            candidate, session, f"turn-{full_history_length // 2 + 1}-user"
                        )
                        if fact:
                            saved_facts.append(fact["id"])
                    extraction_state = "completed"
                except Exception:
                    # The answer is already committed. Do not retry a model call
                    # or misreport the successful turn as an unknown outcome.
                    extraction_state = "failed"
                finally:
                    policy.active = False
            result: Report = {
                "ok": True,
                "text": text,
                "timings_ms": {**timings, "total": round((time.monotonic() - _t0) * 1000)},
                "memory_extraction": extraction_state,
                "generation_degraded": generation_degraded,
                "model_request_state": state["model_request_state"],
                **provider_metadata,
                "retry_allowed": False,
                "memory_saved_ids": saved_facts,
                "memory_sources": [
                    {k: f[k] for k in ("id", "source_session", "source_message", "version")}
                    for f in facts
                ],
                "session_id": session,
                "response_id": actual.get("id"),
                "request_id": getattr(response, "_request_id", None),
                "model": actual.get("model"),
                "requested_model": route["model"],
                "provider": route["provider"],
                "api_mode": route["api_mode"],
                "reasoning_effort": effort,
                "usage": actual.get("usage"),
                "model_attempts": state.get("model_attempts", []),
                "database": str(root / "engine-home/state.db"),
                "tool_count": 0,
                "network_connections": policy.connections,
                "dns_queries": policy.dns_queries,
                "adapter_calls": adapter.calls,
                "policy_counts": policy.counts(),
                "blocked_process_callers": policy.process_callers,
                "scope": "controlled fake HTTP"
                if test_port
                else "real provider single text request",
            }
        except BaseException as exc:
            raise ChatFailure(type(exc).__name__, state) from exc
        finally:
            policy.active = False
            if client is not None:
                client.close()
            if http is not None:
                http.close()
            if agent is not None and runtime is None:
                agent.close()
            if runtime is None:
                db.close()
    result["host_module_sources"] = {
        name: path
        for name, path in engine.module_sources().items()
        if engine.inside(Path(path), engine.HOST)
    }
    if result["host_module_sources"]:
        raise RuntimeError("Host module imported")
    allowed = [
        engine.VENDOR,
        engine.VENV,
        Path(sys.base_prefix).resolve(),
        engine.PROJECT / "packaging",
    ]
    unexpected = {
        name: path
        for name, path in engine.module_sources().items()
        if not any(engine.inside(Path(path), parent) for parent in allowed)
    }
    result["unexpected_module_sources"] = unexpected
    if unexpected:
        raise RuntimeError("Unexpected external source imported")
    return result


def _sse_events(lines: Iterable[str]) -> Iterator[str]:
    """Frame SSE events, including comments, CRLF and multi-line data fields."""
    data: list[str] = []
    event_bytes = 0
    for line in lines:
        line = line.rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
            data = []
            event_bytes = 0
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field != "data":
            continue
        if separator and value.startswith(" "):
            value = value[1:]
        event_bytes += len(value.encode("utf-8"))
        if event_bytes > MAX_RESPONSE_BYTES:
            raise ValueError("stream_event_too_large")
        data.append(value)
    if data:
        yield "\n".join(data)


def _stream_lines(url: str, payload: Report, key: str, timeout: float) -> Iterator[str]:
    """Read incrementally; never buffer a complete provider response or retry."""
    try:
        import httpx
    except ImportError:
        # Stdlib fallback for the product's isolated (-S) interpreter.
        from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args: Any, **kwargs: Any) -> None:
                return None

        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            while True:
                raw = response.readline(MAX_RESPONSE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("stream_event_too_large") from None
                yield raw.decode("utf-8")
        return
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
        with client.stream(
            "POST",
            url,
            json=payload,
            headers={"Authorization": "Bearer " + key, "Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            yield from response.iter_lines()


def stream_chat(
    prompt: str,
    config: dict[str, Any],
    key_file: str,
    on_chunk: Callable[[StreamChunk], object],
) -> str:
    """Stream one text-only completion using the existing product config/key policy.

    No host configuration, hidden retries, tool execution or credential logging.
    Failures are terminal chunks, not exceptions. The return value includes any
    partial text received before an error. This low-level API does not persist
    dialogue; the resident caller owns scheduling and conversation persistence.
    """
    chunks: list[str] = []
    index = 0
    error: str | None = None
    lines: Iterator[str] | None = None
    try:
        route = stream_config.validate_config(config)
        if route["api_mode"] != "chat_completions":
            raise ValueError("stream_requires_chat_completions")
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt.encode("utf-8")) > CHAT_CONTEXT_BYTE_LIMIT
        ):
            raise ValueError("invalid_stream_prompt")
        key = stream_config.read_key(key_file=Path(key_file))
        # Match chat() route validation and launcher credential parsing exactly.
        validate_route({**route, "api_key": key})
        payload: Report = {
            "model": route["model"],
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        if route["reasoning_effort"] is not None:
            payload["reasoning_effort"] = route["reasoning_effort"]
        lines = _stream_lines(
            str(route["base_url"]).rstrip("/") + "/chat/completions",
            payload,
            key,
            float(request_timeout_seconds(route["reasoning_effort"])),
        )
        completed = False
        size = 0
        for event in _sse_events(lines):
            if event.strip() == "[DONE]":
                completed = True
                break
            item = json.loads(event)
            if not isinstance(item, dict) or item.get("error"):
                raise ValueError("invalid_stream_event")
            choices = item.get("choices")
            if not isinstance(choices, list):
                raise ValueError("invalid_stream_event")
            if not choices:  # usage-only event
                continue
            choice = choices[0]
            delta = choice.get("delta", {})
            if delta.get("tool_calls") or delta.get("function_call"):
                raise ValueError("stream_tools_not_supported")
            text = delta.get("content")
            if text is None or text == "":  # role/reasoning-only event
                continue
            if not isinstance(text, str):
                raise ValueError("invalid_stream_content")
            size += len(text.encode("utf-8"))
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("stream_response_too_large")
            chunks.append(text)
            on_chunk(StreamChunk(text=text, index=index))
            index += 1
        if not completed:
            error = "stream_interrupted"
    except Exception as exc:
        # Provider exceptions can embed URLs, keys or bodies. Never forward them.
        name = type(exc).__name__.lower()
        if isinstance(exc, TimeoutError) or "timeout" in name:
            error = "stream_timeout"
        elif isinstance(exc, (ValueError, TypeError, KeyError, UnicodeError)):
            error = "invalid_stream_request_or_response"
        else:
            error = "stream_connection_error"
    finally:
        if lines is not None:
            try:
                lines.close()
            except Exception:
                error = error or "stream_connection_error"
    try:
        on_chunk(StreamChunk(done=True, error=error, index=index))
    except Exception:
        pass  # A disconnected/failed consumer must not escape the stream API.
    return "".join(chunks)


def alarm_handler(signum: int, frame: FrameType | None) -> None:
    raise TimeoutError("Bounded chat deadline")


WARM_PROTOCOL_VERSION = 1
WARM_IDLE_SECONDS = 120


def reset_policy(policy: ModelPolicy) -> None:
    policy.active = False
    policy.addresses.clear()
    policy.connections = policy.dns_queries = 0
    policy.deadline = 0.0
    policy.network_attempts = policy.local_bind_attempts = policy.process_attempts = 0
    policy.host_access_attempts = policy.outside_write_attempts = 0
    policy.network_callers.clear()
    policy.process_callers.clear()


def audit_controls(policy: ModelPolicy) -> None:
    """Exercise installed audit hooks every turn, before any model dispatch.

    Synthetic audit *events*, not synthetic answers: no forbidden side effect
    is performed if a broken/missing hook unexpectedly accepts the event.
    """
    for event, args in (
        ("socket.connect", (None, ("169.254.169.254", 80))),
        ("socket.bind", (None, ("127.0.0.1", 0))),
        ("subprocess.Popen", ("/usr/bin/true", (), None, None)),
        ("open", (str(engine.HOST / "config.yaml"), "r", 0)),
        ("open", (str(root_outside := policy.root.parent / ".warm-denied-write"), "w", os.O_WRONLY)),
    ):
        try:
            sys.audit(event, *args)
        except PermissionError:
            continue
        raise RuntimeError("Installed audit control failed")


def failure_result(exc: BaseException) -> Report:
    import traceback

    result: Report = {
        "ok": False,
        "error_type": exc.error_type if isinstance(exc, ChatFailure) else type(exc).__name__,
        "error_frames": [
            {"file": frame.filename, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)
        ],
    }
    if isinstance(exc, ChatFailure):
        result.update(exc.state)
    return result


def warm_engine(
    root: Path, credentials_fd: int, test_port: int | None = None,
    protected_hosts: tuple[Path, ...] = (),
) -> int:
    """Private inherited-pipe JSONL protocol; credentials never occur in frames.

    A response failure is terminal. The caller must not replay an in-flight
    frame, even if the process dies before emitting a result.
    """
    engine.interpreter_check()
    root = home.validate_root(root, legacy=RUNS, extra=protected_hosts)
    if credentials_fd < 3 or not stat.S_ISFIFO(os.fstat(credentials_fd).st_mode):
        raise ValueError("Credentials require anonymous pipe FD")
    with os.fdopen(credentials_fd, "r") as stream:
        route = read_json(stream)
    validate_route(route, test_port=test_port)
    runtime: Report = {}
    print(json.dumps({"ready": True, "version": WARM_PROTOCOL_VERSION}), flush=True)
    while True:
        signal.alarm(WARM_IDLE_SECONDS)
        try:
            line = sys.stdin.buffer.readline(MAX_BYTES + 2)
        except TimeoutError:
            return 0
        if not line:
            return 0
        signal.alarm(90)
        try:
            if len(line) > MAX_BYTES or not line.endswith(b"\n"):
                raise ValueError("Invalid warm frame size")
            frame = json.loads(line)
            if (
                not isinstance(frame, dict)
                or set(frame) != {"version", "id", "request"}
                or frame["version"] != WARM_PROTOCOL_VERSION
                or not isinstance(frame["id"], str)
                or not re.fullmatch(r"[a-f0-9]{32}", frame["id"])
                or not isinstance(frame["request"], dict)
                or route["api_key"] in line.decode("utf-8")
            ):
                raise ValueError("Invalid warm frame")
            result = _chat_turn(root, route, frame["request"], test_port, protected_hosts, runtime)
            envelope = {"version": WARM_PROTOCOL_VERSION, "id": frame["id"], "result": result}
        except BaseException as exc:
            # Never put provider exception text, input frames or credentials on either stream.
            print(json.dumps({"version": WARM_PROTOCOL_VERSION, "result": failure_result(exc)}), flush=True)
            return 1
        finally:
            signal.alarm(0)
        print(json.dumps(envelope, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-fd", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    # These only add denied paths; no flag can weaken the root policy.
    parser.add_argument("--protected-host", type=Path, action="append", default=[])
    # A visible, explicit fake-only loopback exception. Never counted as real verification.
    parser.add_argument("--test-local-port", type=int)
    # --serve is the public name; --warm-engine remains an internal alias.
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--warm-engine", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    signal.signal(signal.SIGALRM, alarm_handler)
    signal.alarm(90)
    try:
        if args.serve or args.warm_engine:
            return warm_engine(args.data_dir, args.credentials_fd, args.test_local_port,
                               tuple(args.protected_host))
        result = chat(
            args.data_dir,
            args.credentials_fd,
            read_json(cast(io.TextIOBase, sys.stdin)),
            args.test_local_port,
            tuple(args.protected_host),
        )
    except BaseException as exc:
        # Provider exception strings may contain request headers; never emit those strings.
        import traceback

        result = {
            "ok": False,
            "error_type": exc.error_type if isinstance(exc, ChatFailure) else type(exc).__name__,
            "error_frames": [
                {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(exc.__traceback__)
            ],
        }
        if isinstance(exc, ChatFailure):
            result.update(exc.state)
    finally:
        signal.alarm(0)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


def self_evaluate():
    """Check live product chat bounds without loading credentials or dispatching."""
    from datetime import datetime

    checks = {
        "timeout_ordered": all(type(v) is int and v > 0 for v in REQUEST_TIMEOUT_SECONDS.values())
        and launcher_timeout_seconds("high")
        > child_timeout_seconds("high")
        > request_timeout_seconds("high"),
        "json_input_budget": MAX_BYTES,
        "chat_budget_positive": type(CHAT_CONTEXT_BYTE_LIMIT) is int
        and CHAT_CONTEXT_BYTE_LIMIT > 0,
    }
    return {
        "module": "chat_entry",
        "domain": "bounded one-shot product chat, route validation and streamed responses",
        "current_version": "isolated product entry with explicit route, UTF-8 and timeout policies",
        "known_limitations": [
            "Actual provider readiness and credentials are intentionally not probed.",
            "Evaluation checks policy constants and does not create product data or start a child.",
        ],
        "improvement_directions": [
            (
                "Keep entry and launcher payload limits synchronized with "
                "worst-case JSON escaping tests."
            )
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "ok": checks["timeout_ordered"] and checks["chat_budget_positive"],
    }
