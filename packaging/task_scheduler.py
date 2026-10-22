"""Single-process threaded agent scheduler; not wired to daemon/inbox.

Call schedule() to drain runnable work, or schedule(block=False) for one tick.
Unknown dependencies remain blocked until submitted; failed dependencies fail
closed. There is no persistence/restart replay or automatic retry of side effects.
Running Python threads cannot be killed: timeout/cancel results retain their
physical worker slot until the executor returns, and late results are discarded.
"""

from __future__ import annotations

import heapq
import itertools
import json
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from agent_protocol import (
    EFFORTS,
    AgentEndpoint,
    ExecutorType,
    TaskMessage,
    TaskResult,
    deserialize,
    deserialize_result,
    serialize,
    serialize_result,
)
from model_registry import ModelRegistry
from task_executor import execute_task, select_model


def self_evaluate() -> dict[str, Any]:
    """Describe scheduling boundaries without creating pools or probing endpoints."""
    from datetime import UTC, datetime

    return {
        "module": "task_scheduler",
        "domain": "threaded agent DAG scheduling cancellation deadline reconciliation",
        "current_version": "single-process priority DAG scheduler with injectable agent endpoints",
        "known_limitations": [
            (
                "Queues and results are in memory; there is no persistent restart "
                "replay or daemon/inbox wiring here."
            ),
            (
                "Python worker threads cannot be killed; timeout/cancellation retain "
                "the slot until the worker returns."
            ),
            "Local scripts and CLI processes are trusted-caller execution, not an OS sandbox.",
            (
                "Remote HTTP is a protocol enum only; no remote endpoint is installed "
                "by this scheduler."
            ),
            (
                "Decomposition requests deepseek-flash explicitly; unavailable routes "
                "fail without automatic retries."
            ),
        ],
        "improvement_directions": [
            "Design durable queue and reconciliation records before enabling restart recovery.",
            (
                "Use isolated process workers with explicit cooperative cancellation "
                "and side-effect ownership."
            ),
            (
                "Make decomposition routing caller-configurable and validate endpoint "
                "capability compatibility."
            ),
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


def _finished(
    message: TaskMessage,
    status: str,
    started: float,
    *,
    output: str = "",
    error: str | None = None,
    model: str = "",
) -> TaskResult:
    return TaskResult(message.task_id, status, output, error, time.monotonic() - started, model)


class ModelApiExecutor(AgentEndpoint):
    """Use task_executor's credential-FD/chat_entry boundary, never a new SDK.

    Registry metadata does not contain API URLs. Supply validated full routes
    with register_model_route(), or use the existing product config if its model
    exactly matches the chosen ID. Never infer an endpoint from a provider label.
    """

    executor_type = ExecutorType.MODEL_API

    def __init__(self, data_root: Path, model_registry: ModelRegistry) -> None:
        self.data_root = Path(data_root)
        self.model_registry = model_registry
        self._routes: dict[str, tuple[dict[str, Any], str]] = {}
        self._lock = threading.RLock()

    def register_model_route(self, model_id: str, config: dict[str, Any], *, key_path: str) -> None:
        from product_config import validate_config

        if type(model_id) is not str or not model_id.strip():
            raise ValueError("model_id must be nonempty")
        if type(key_path) is not str or not key_path.strip():
            raise ValueError("key_path must be nonempty")
        config = dict(config)
        config.setdefault("reasoning_effort", None)
        validated = dict(validate_config(config))
        with self._lock:
            self._routes[model_id] = (validated, key_path)

    def resolve_route(self, message: TaskMessage) -> tuple[str, dict[str, Any], str]:
        from product_config import read_config, validate_config

        chosen = message.model_id
        if chosen is None:
            candidate = select_model(message.task_type, self.model_registry)
            chosen = str(candidate["id"]) if candidate else "deepseek-flash"
        entry = self.model_registry.get(chosen)
        with self._lock:
            route = self._routes.get(chosen)
        if route is None:
            # Product config is read lazily; initialization/submit never reads a key.
            config = dict(read_config().values)
            if config["model"] != chosen:
                raise ValueError("Selected model has no configured product route")
            key_path = entry.get("key_path") if entry else None
            if type(key_path) is not str or not key_path.strip():
                raise ValueError("Selected model has no configured key path")
        else:
            config, key_path = route
            config = dict(config)
        supported = entry.get("supported_efforts", []) if entry else ["low", "medium", "high"]
        # Wire protocol accepts max/ultra, but the current product transport does
        # not. Intersect first; do not send unsupported settings or invent aliases.
        allowed = [
            effort
            for effort in EFFORTS
            if effort in supported and effort in ("low", "medium", "high", "xhigh")
        ]
        if not allowed:
            raise ValueError("No reasoning effort supported by model and product transport")
        rank = EFFORTS.index(message.effort)
        config["reasoning_effort"] = min(
            allowed, key=lambda effort: (abs(EFFORTS.index(effort) - rank), EFFORTS.index(effort))
        )
        return chosen, dict(validate_config(config)), key_path

    def health_check(self) -> bool:
        try:
            _, _, key_path = self.resolve_route(TaskMessage())
            return Path(key_path).expanduser().is_file()
        except Exception:
            return False

    def execute(self, message: TaskMessage) -> TaskResult:
        started = time.monotonic()
        model = message.model_id or ""
        try:
            model, config, key_path = self.resolve_route(message)
            output = execute_task(message.description, config, key_path, self.data_root)
            if time.monotonic() - started > message.timeout_seconds:
                return _finished(
                    message,
                    "timeout",
                    started,
                    model=model,
                    error="Model task exceeded deadline; outcome requires reconciliation",
                )
            if type(output) is not str or not output.strip():
                return _finished(
                    message, "failed", started, model=model, error="Model returned no text"
                )
            if output.startswith("任务执行失败："):
                return _finished(message, "failed", started, model=model, error=output)
            # execute_task intentionally does not expose transport usage.
            return _finished(message, "completed", started, output=output, model=model)
        except Exception:
            return _finished(
                message,
                "failed",
                started,
                model=model,
                error="Model route unavailable or execution failed; no retry performed",
            )


class _ProcessExecutor(AgentEndpoint):
    """No shell; finite subprocess deadline. This is not a process sandbox."""

    def _execute_command(
        self, message: TaskMessage, command: list[str], *, cwd: Path, model: str
    ) -> TaskResult:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=message.timeout_seconds,
                check=False,
            )
            if completed.returncode:
                return _finished(
                    message,
                    "failed",
                    started,
                    output=completed.stdout,
                    model=model,
                    error=f"Process exited with status {completed.returncode}",
                )
            return _finished(message, "completed", started, output=completed.stdout, model=model)
        except subprocess.TimeoutExpired:
            return _finished(
                message,
                "timeout",
                started,
                model=model,
                error="Process exceeded deadline; child stopped; no retry performed",
            )
        except OSError:
            return _finished(
                message, "failed", started, model=model, error="Process could not be started"
            )


class CodexExecutor(_ProcessExecutor):
    """Use the requested Codex CLI contract. No retry on CLI errors/timeouts.

    An optional model fallback is used ONLY when the executable is absent;
    standalone executors without a fallback return failed in that case.
    Installed CLIs rejecting the requested flags fail visibly, never re-execute.
    """

    executor_type = ExecutorType.CODEX_CLI

    def __init__(self, data_root: Path, fallback: AgentEndpoint | None = None) -> None:
        self.data_root = Path(data_root)
        self.fallback = fallback

    def health_check(self) -> bool:
        return bool(shutil.which("codex")) or bool(self.fallback and self.fallback.health_check())

    def execute(self, message: TaskMessage) -> TaskResult:
        started = time.monotonic()
        binary = shutil.which("codex")
        if binary is None:
            if self.fallback is not None:
                return self.fallback.execute(message)
            return _finished(
                message, "failed", started, error="Codex CLI is not available", model="codex"
            )
        cwd = self.data_root
        cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self._execute_command(
            message,
            [binary, "--non-interactive", "--prompt", message.description],
            cwd=cwd,
            model="codex",
        )


class LocalScriptExecutor(_ProcessExecutor):
    """Run a trusted caller's Python script, not generated description text.

    metadata: executor_type='local_script', script_path, optional args (strings).
    Relative script paths resolve beneath data_root; the subprocess cwd is that
    root. Absolute paths are supported. The caller owns script authorization.
    """

    executor_type = ExecutorType.LOCAL_SCRIPT

    def __init__(self, data_root: Path, python_executable: str | None = None) -> None:
        self.data_root = Path(data_root)
        self.python_executable = python_executable or sys.executable

    def health_check(self) -> bool:
        return Path(self.python_executable).is_file()

    def execute(self, message: TaskMessage) -> TaskResult:
        started = time.monotonic()
        script = message.metadata.get("script_path")
        args = message.metadata.get("args", [])
        if (
            not isinstance(script, str)
            or not script.strip()
            or not isinstance(args, list)
            or any(not isinstance(arg, str) for arg in cast(list[object], args))
        ):
            return _finished(
                message,
                "failed",
                started,
                model="local-python",
                error="Local script requires script_path and a list of string args",
            )
        path = Path(script).expanduser()
        if not path.is_absolute():
            path = self.data_root / path
        if not path.is_file() or path.suffix != ".py":
            return _finished(
                message,
                "failed",
                started,
                model="local-python",
                error="Python script does not exist",
            )
        self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self._execute_command(
            message,
            [self.python_executable, str(path.resolve()), *args],
            cwd=self.data_root,
            model="local-python",
        )


class TaskScheduler:
    def __init__(
        self, data_root: Path, model_registry: ModelRegistry, max_concurrent: int = 4
    ) -> None:
        self.data_root = Path(data_root).expanduser()
        self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.model_registry = model_registry
        self._lock = threading.RLock()
        self._schedule_lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None
        self._active: dict[str, Future[TaskResult]] = {}
        self._started: dict[str, float] = {}
        self._ended: dict[str, float] = {}
        self._messages: dict[str, TaskMessage] = {}
        self._pending: dict[str, TaskMessage] = {}
        self._ready: list[tuple[int, int, str]] = []
        self._ready_ids: set[str] = set()
        self._sequence: dict[str, int] = {}
        self._counter = itertools.count()
        self._results: dict[str, TaskResult] = {}
        self._collected: deque[TaskResult] = deque()
        self._cancel_requested: set[str] = set()
        self._closed = False
        self._max_concurrent = 4
        self.max_concurrent = max_concurrent
        self.model_executor = ModelApiExecutor(self.data_root, model_registry)
        self.executors: dict[ExecutorType, AgentEndpoint] = {
            ExecutorType.MODEL_API: self.model_executor,
            ExecutorType.CODEX_CLI: CodexExecutor(self.data_root, self.model_executor),
            ExecutorType.LOCAL_SCRIPT: LocalScriptExecutor(self.data_root),
        }

    def register_model_route(self, model_id: str, config: dict[str, Any], *, key_path: str) -> None:
        self.model_executor.register_model_route(model_id, config, key_path=key_path)

    def decompose(self, complex_task: str) -> list[TaskMessage]:
        """Call deepseek-flash once, validate the whole DAG, do not auto-submit.

        Dependency aliases are optional unique id fields or zero-based indexes.
        Metadata/executor/script/key overrides from model output are NOT accepted.
        """
        if type(complex_task) is not str or not complex_task.strip():
            raise ValueError("complex_task must be nonempty")
        prompt = (
            "将以下复合任务拆解为可并行的独立子任务，"
            "每个子任务包括类型、描述、优先级。返回 JSON 数组。"
            "\n类型使用 code/writing/analysis/fast/general/research，优先级为 0-3。"
            "用 id 标识子任务，dependencies 数组引用前置任务的 id，没有依赖则为空数组。"
            "\n复合任务：\n" + complex_task
        )
        response = self.model_executor.execute(
            TaskMessage(
                description=prompt, task_type="fast", model_id="deepseek-flash", effort="low"
            )
        )
        if response.status != "completed":
            raise RuntimeError(response.error or "Task decomposition failed")
        text = response.output.strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            if lines[0].strip().lower() in ("```json", "```"):
                text = "\n".join(lines[1:-1])
        payload: object = json.loads(text)
        if not isinstance(payload, list):
            raise ValueError("Decomposition must be a JSON array")
        raw = cast(list[Any], payload)
        if not raw or len(raw) > 100:
            raise ValueError("Decomposition must be a nonempty JSON array with at most 100 tasks")
        messages: list[TaskMessage] = []
        aliases: dict[str, int] = {}
        for index, row in enumerate(raw):
            if not isinstance(row, dict):
                raise ValueError("Each subtask must be a JSON object")
            row = cast(dict[str, Any], row)
            alias = row.get("id", str(index))
            if not isinstance(alias, str) or not alias.strip() or alias in aliases:
                raise ValueError("Subtask IDs must be nonempty and unique")
            aliases[alias] = index
            message = TaskMessage(
                task_id=str(uuid4()),
                task_type=row.get("task_type", row.get("type", "general")),
                description=row.get("description", ""),
                priority=row.get("priority", 1),
                effort=row.get("effort", "medium"),
            )
            if not message.description.strip():
                raise ValueError("Subtask description must be nonempty")
            messages.append(message)
        for index, row in enumerate(raw):
            dependencies = row.get("dependencies", [])
            if not isinstance(dependencies, list):
                raise ValueError("dependencies must be an array")
            for dependency in cast(list[Any], dependencies):
                if type(dependency) is int and 0 <= dependency < len(messages):
                    target = dependency
                elif isinstance(dependency, str) and dependency in aliases:
                    target = aliases[dependency]
                else:
                    raise ValueError("Unknown subtask dependency")
                messages[index].dependencies.append(messages[target].task_id)
            messages[index].__post_init__()
        # A private graph probe avoids partial submission on malformed output.
        graph = {message.task_id: message.dependencies for message in messages}
        resolved: set[str] = set()
        while len(resolved) < len(messages):
            ready = {
                key for key, deps in graph.items() if key not in resolved and set(deps) <= resolved
            }
            if not ready:
                raise ValueError("Decomposition contains a dependency cycle")
            resolved.update(ready)
        return messages

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @max_concurrent.setter
    def max_concurrent(self, value: int) -> None:
        if type(value) is not int or value < 1:
            raise ValueError("max_concurrent must be a positive integer")
        with self._lock:
            if self._active:
                raise RuntimeError("Cannot resize while workers are active")
            if self._pool is not None:
                self._pool.shutdown(wait=True)
                self._pool = None
            self._max_concurrent = value

    def submit(self, message: TaskMessage) -> str:
        # Snapshot JSON boundaries so caller mutation cannot change queued work.
        message = deserialize(serialize(message))
        if not message.description.strip():
            raise ValueError("Task description must be nonempty")
        with self._lock:
            if self._closed:
                raise RuntimeError("Scheduler is closed")
            if message.task_id in self._messages:
                raise ValueError("Duplicate task_id")
            graph = {key: value.dependencies for key, value in self._messages.items()}
            graph[message.task_id] = message.dependencies
            visiting: set[str] = set()
            visited: set[str] = set()

            def visit(key: str) -> None:
                if key in visiting:
                    raise ValueError("Dependency cycle")
                if key in visited:
                    return
                visiting.add(key)
                for dependency in graph.get(key, []):
                    visit(dependency)
                visiting.remove(key)
                visited.add(key)

            visit(message.task_id)
            self._messages[message.task_id] = message
            self._pending[message.task_id] = message
            self._sequence[message.task_id] = next(self._counter)
            return message.task_id

    def _record(self, result: TaskResult) -> None:
        if result.task_id not in self._results:
            self._results[result.task_id] = deserialize_result(serialize_result(result))
            self._collected.append(deserialize_result(serialize_result(result)))

    def _reap(self) -> None:
        now = time.monotonic()
        for task_id, future in list(self._active.items()):
            message = self._messages[task_id]
            started = self._started.get(task_id)
            elapsed = self._ended.get(task_id, now) - started if started is not None else 0.0
            if future.done():
                try:
                    result = future.result()
                    if type(result) is not TaskResult or result.task_id != task_id:
                        raise ValueError("Executor returned a mismatched result")
                    result.__post_init__()
                except Exception:
                    result = TaskResult(
                        task_id, "failed", error="Executor failed or returned an invalid result"
                    )
                if task_id in self._cancel_requested:
                    result = TaskResult(
                        task_id,
                        "cancelled",
                        error="Cancellation requested; worker has stopped",
                        duration_seconds=elapsed,
                        model_used=result.model_used,
                    )
                elif elapsed > message.timeout_seconds:
                    result = TaskResult(
                        task_id,
                        "timeout",
                        error="Task deadline exceeded; no retry performed",
                        duration_seconds=elapsed,
                        model_used=result.model_used,
                    )
                self._record(result)
                del self._active[task_id]
                self._started.pop(task_id, None)
                self._ended.pop(task_id, None)
            elif elapsed >= message.timeout_seconds:
                self._record(
                    TaskResult(
                        task_id,
                        "timeout",
                        duration_seconds=elapsed,
                        error="Deadline exceeded; worker may still run; no retry performed",
                    )
                )

    def _promote(self) -> None:
        # Iterate to propagate failure transitively even with forward dependencies.
        changed = True
        while changed:
            changed = False
            for task_id, message in list(self._pending.items()):
                failed = [
                    dep
                    for dep in message.dependencies
                    if dep in self._results and self._results[dep].status != "completed"
                ]
                if failed:
                    self._record(
                        TaskResult(
                            task_id, "failed", error="Dependency did not complete successfully"
                        )
                    )
                    del self._pending[task_id]
                    self._ready_ids.discard(task_id)
                    changed = True
                elif task_id not in self._ready_ids and all(
                    dep in self._results and self._results[dep].status == "completed"
                    for dep in message.dependencies
                ):
                    heapq.heappush(
                        self._ready, (-message.priority, self._sequence[task_id], task_id)
                    )
                    self._ready_ids.add(task_id)

    def _run(self, message: TaskMessage) -> TaskResult:
        with self._lock:
            self._started[message.task_id] = time.monotonic()
        try:
            return self.execute(message)
        finally:
            with self._lock:
                self._ended[message.task_id] = time.monotonic()

    def schedule(self, *, block: bool = True) -> None:
        """Drain runnable work; return if only unresolved dependencies remain.

        Blocking mode owns dispatch, but submit/cancel/status/collect are safe
        from other threads. No scheduler thread is silently created.
        """
        if not self._schedule_lock.acquire(blocking=False):
            raise RuntimeError("Another schedule loop is already running")
        try:
            while True:
                with self._lock:
                    self._reap()
                    self._promote()
                    while (
                        not self._closed and self._ready and len(self._active) < self.max_concurrent
                    ):
                        _, _, task_id = heapq.heappop(self._ready)
                        self._ready_ids.discard(task_id)
                        message = self._pending.pop(task_id, None)
                        if message is None:
                            continue
                        if self._pool is None:
                            self._pool = ThreadPoolExecutor(
                                max_workers=self.max_concurrent, thread_name_prefix="jarvis-agent"
                            )
                        self._active[task_id] = self._pool.submit(self._run, message)
                    # Logically timed-out workers retain slots, but a blocking
                    # caller must still regain control at the task deadline.
                    futures = [
                        future
                        for task_id, future in self._active.items()
                        if task_id not in self._results
                    ]
                if not block or not futures:
                    return
                wait(futures, timeout=0.05, return_when=FIRST_COMPLETED)
        finally:
            self._schedule_lock.release()

    def execute(self, message: TaskMessage) -> TaskResult:
        """Select endpoint by task type, with explicit trusted metadata override.

        Set metadata.critic_review=true to request red-team review using the same
        configured model route. A rejected/unavailable review fails closed while
        retaining output for caller revision; no automatic repair/retry occurs.
        """
        started = time.monotonic()
        try:
            message = deserialize(serialize(message))
            explicit = message.metadata.get("executor_type")
            endpoint_type = (
                ExecutorType(explicit)
                if explicit is not None
                else ExecutorType.CODEX_CLI
                if message.task_type == "code"
                else ExecutorType.MODEL_API
            )
            endpoint = self.executors.get(endpoint_type)
            if endpoint is None:
                return _finished(
                    message, "failed", started, error="Executor type is not configured"
                )
            result = endpoint.execute(message)
            if (
                result.status == "completed"
                and message.metadata.get("critic_review", False) is True
            ):
                self._review(message, result)
            result.duration_seconds = time.monotonic() - started
            if result.duration_seconds > message.timeout_seconds:
                return _finished(
                    message,
                    "timeout",
                    started,
                    model=result.model_used,
                    error="Task exceeded deadline; no retry performed",
                )
            return result
        except Exception:
            return _finished(
                message,
                "failed",
                started,
                error="Invalid task or executor failure; no retry performed",
            )

    def _review(self, message: TaskMessage, result: TaskResult) -> None:
        from critic_agent import CriticAgent
        from product_config import read_key

        try:
            _, config, key_path = self.model_executor.resolve_route(message)
            key = read_key(key_file=Path(key_path).expanduser(), key_fd=None)
            report = CriticAgent().review(
                message.description,
                result.output,
                config=config,
                key=key,
                data_root=self.data_root / "critic-reviews",
            )
            result.critic_review = asdict(report)
            if not report.passed:
                result.status = "failed"
                result.error = "Critic rejected the result; caller revision required"
        except Exception:
            result.critic_review = {
                "passed": False,
                "reason": "Critic review unavailable",
                "suggestions": [],
                "iterations": 0,
            }
            result.status = "failed"
            result.error = "Requested critic review could not be completed"

    def collect_results(self) -> list[TaskResult]:
        """Drain each terminal result once; retain history for dependencies."""
        with self._lock:
            self._reap()
            self._promote()
            results = list(self._collected)
            self._collected.clear()
            return results

    def cancel(self, task_id: str) -> None:
        """Cancel queued work; running work is cooperative and never retried."""
        with self._lock:
            self._reap()
            if task_id not in self._messages:
                raise KeyError(task_id)
            if task_id in self._results:
                return
            if task_id in self._pending:
                del self._pending[task_id]
                self._ready_ids.discard(task_id)
                self._record(TaskResult(task_id, "cancelled", error="Cancelled before execution"))
            elif task_id in self._active:
                self._cancel_requested.add(task_id)
                self._active[task_id].cancel()
            self._promote()

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._reap()
            self._promote()
            counts = {
                state: sum(result.status == state for result in self._results.values())
                for state in ("completed", "failed", "timeout", "cancelled")
            }
            return {
                "pending": len(self._pending),
                "running": len(self._active),
                "ready": len(self._ready_ids),
                "blocked": len(self._pending) - len(self._ready_ids),
                "cancellation_requested": len(self._cancel_requested & self._active.keys()),
                "max_concurrent": self.max_concurrent,
                "total": len(self._messages),
                **counts,
            }

    def shutdown(self, *, wait: bool = True, cancel_pending: bool = True) -> None:
        """Release the thread pool. wait=False does not terminate running threads."""
        with self._lock:
            self._closed = True
            if cancel_pending:
                for task_id in list(self._pending):
                    self.cancel(task_id)
            pool = self._pool
        if pool is not None:
            pool.shutdown(wait=wait, cancel_futures=cancel_pending)
        with self._lock:
            self._reap()

    def __enter__(self) -> TaskScheduler:
        return self

    def __exit__(self, *args: object) -> None:
        self.shutdown()
