"""K4-004 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

Standalone fresh-exec experiment, never wired to Worker or self-evolution.
Same UID is NOT an OS sandbox: manifests are declarations, not syscall filters.
Only trusted offline fixtures may opt in; hostile generated code requires an
approved OS/container boundary. No ApprovalGate/AutonomyController bypass grant.
"""

from __future__ import annotations

import importlib
import inspect
import json
import math
import os
import resource
import runpy
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar, Protocol, cast

from jarvis.skill_manifest import SkillCapability, SkillManifest

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Trusted fixture policy; None explicitly disables the memory limit."""

    timeout_s: float = 10.0
    max_memory_mb: int | None = 256
    allowed_capabilities: set[SkillCapability] = field(default_factory=set[SkillCapability])
    capture_output: bool = True

    def __post_init__(self) -> None:
        if (
            type(self.timeout_s) not in (int, float)
            or not math.isfinite(self.timeout_s)
            or self.timeout_s <= 0
        ):
            raise ValueError("timeout_s must be positive and finite")
        if self.max_memory_mb is not None and (
            type(self.max_memory_mb) is not int or not 0 < self.max_memory_mb < 2**30
        ):
            raise ValueError("max_memory_mb must be a positive integer or None")
        if type(self.allowed_capabilities) is not set or any(
            type(value) is not SkillCapability for value in self.allowed_capabilities
        ):
            raise ValueError("allowed_capabilities must be a set of SkillCapability")
        if type(self.capture_output) is not bool:
            raise ValueError("capture_output must be bool")
        object.__setattr__(self, "allowed_capabilities", set(self.allowed_capabilities))


@dataclass(frozen=True, slots=True)
class RunResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    return_code: int | None = None
    wall_time_s: float = 0.0
    memory_mb: float | None = None
    error: str | None = None


class ManifestSource(Protocol):
    def get(self, name: str) -> SkillManifest | None: ...


class IsolatedRunner:
    """Explicit fixture-only executor; capability grants must be trusted inputs.

    Handlers must be importable module-level functions, args JSON-compatible
    positional values. No pickle/fork or in-process thread execution is used.
    Function return values are discarded: success means normal process exit.
    """

    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(
        self, registry: ManifestSource | None = None, *, draft_fixture: bool = False
    ) -> None:
        if draft_fixture is not True:
            raise ValueError("explicit offline draft_fixture=True required")
        self._registry = registry

    def run(
        self,
        skill_name: str,
        handler_fn: Callable[..., object],
        args: Sequence[object],
        config: RunConfig,
    ) -> RunResult:
        start = time.monotonic()
        try:
            config = replace(config)
            if self._registry is None:
                raise PermissionError("manifest registry is required")
            manifest = self._registry.get(skill_name)
            if manifest is None:
                raise PermissionError("unknown skill")
            manifest = replace(manifest)
            if manifest.name != skill_name:
                raise PermissionError("manifest name mismatch")
            denied = manifest.capabilities - config.allowed_capabilities
            if denied:
                raise PermissionError("capabilities denied: " + ", ".join(sorted(denied)))
            if (
                not inspect.isfunction(handler_fn)
                or "<locals>" in handler_fn.__qualname__
                or inspect.iscoroutinefunction(handler_fn)
                or inspect.isgeneratorfunction(handler_fn)
                or inspect.isasyncgenfunction(handler_fn)
            ):
                raise ValueError("handler must be a synchronous importable module-level function")
            module = sys.modules.get(handler_fn.__module__)
            source = inspect.getsourcefile(handler_fn)
            if module is None or source is None or handler_fn.__module__ == "__main__":
                raise ValueError("handler must have an importable module")
            if getattr(module, handler_fn.__name__, None) is not handler_fn:
                raise ValueError("handler must be an unambiguous module-level function")
            root = Path(source).resolve().parents[len(handler_fn.__module__.split(".")) - 1]
            payload: dict[str, object] = {
                "mode": "handler",
                "module": handler_fn.__module__,
                "name": handler_fn.__name__,
                "root": str(root),
                "args": list(args),
            }
            return self._execute(payload, config, start)
        except Exception as exc:
            return RunResult(False, wall_time_s=time.monotonic() - start, error=str(exc))

    def run_subprocess(self, script_path: str | Path, config: RunConfig) -> RunResult:
        """Run a caller-trusted Python fixture, not a shell command or skill grant."""
        start = time.monotonic()
        try:
            config = replace(config)
            if SkillCapability.PROCESS_EXEC not in config.allowed_capabilities:
                raise PermissionError("PROCESS_EXEC is required for raw script execution")
            path = Path(script_path).resolve(strict=True)
            if not path.is_file():
                raise ValueError("script_path must name a regular Python file")
            return self._execute({"mode": "script", "path": str(path)}, config, start)
        except Exception as exc:
            return RunResult(False, wall_time_s=time.monotonic() - start, error=str(exc))

    @staticmethod
    def _execute(payload: dict[str, object], config: RunConfig, start: float) -> RunResult:
        root = str(Path(__file__).resolve().parents[1])
        bootstrap = (
            f"import sys;sys.path.insert(0,{root!r});"
            "from jarvis.isolated_runner import _worker;_worker()"
        )
        with tempfile.TemporaryDirectory(prefix="jarvis-run-", dir=os.environ.get("TMPDIR")) as tmp:
            status = Path(tmp) / "status.json"
            payload["status"] = str(status)
            payload["max_memory_mb"] = config.max_memory_mb
            request = Path(tmp) / "request.json"
            serialized = json.dumps(payload, allow_nan=False).encode("utf-8")
            if len(serialized) > 1024**2:
                raise ValueError("request exceeds 1 MiB limit")
            request.write_bytes(serialized)
            process = subprocess.Popen(
                [sys.executable, "-I", "-c", bootstrap, str(request)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=tmp,
                env={"PATH": os.defpath, "LANG": "C.UTF-8", "TMPDIR": tmp},
                start_new_session=True,
                close_fds=True,
            )
            stdout, stderr, failure = _collect(process, config, start)
            if status.exists() and status.stat().st_size > 65536:
                return RunResult(
                    False,
                    stdout,
                    stderr,
                    process.returncode,
                    time.monotonic() - start,
                    error="worker metadata exceeds 64 KiB limit",
                )
            metadata: dict[str, object] = json.loads(status.read_text()) if status.exists() else {}
            memory = metadata.get("memory_mb")
            error = metadata.get("error", "worker exited without a result")
            success = (
                failure is None and process.returncode == 0 and metadata.get("success") is True
            )
            return RunResult(
                success,
                stdout,
                stderr,
                process.returncode,
                time.monotonic() - start,
                float(cast(float | int, memory)) if type(memory) in (float, int) else None,
                failure or (None if success else str(error)),
            )


def _collect(
    process: subprocess.Popen[bytes], config: RunConfig, start: float
) -> tuple[str, str, str | None]:
    """Bound captured bytes while draining both streams without reader threads."""
    buffers = [bytearray(), bytearray()]
    total, limit = 0, 1024**2
    failure = None
    assert process.stdout is not None and process.stderr is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, 0)
        selector.register(process.stderr, selectors.EVENT_READ, 1)
        try:
            while selector.get_map() or process.poll() is None:
                if time.monotonic() - start >= config.timeout_s:
                    failure = "timeout exceeded"
                    break
                if process.poll() is not None:
                    _kill_group(process)
                for key, _ in selector.select(timeout=0.01):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = max(0, limit - total)
                    if config.capture_output:
                        buffers[cast(int, key.data)].extend(chunk[:remaining])
                    total += len(chunk)
                    if total > limit:
                        failure = "output limit exceeded (1 MiB)"
                        break
                if failure:
                    break
        finally:
            _kill_group(process)
            process.wait(timeout=2)
            process.stdout.close()
            process.stderr.close()
    return (
        buffers[0].decode("utf-8", errors="replace"),
        buffers[1].decode("utf-8", errors="replace"),
        failure,
    )


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    # start_new_session makes exactly this owned PID the process-group ID.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _worker() -> None:  # pyright: ignore[reportUnusedFunction] - fresh-exec bootstrap target
    """Internal fresh-interpreter entrypoint; never called in the main process."""
    payload = json.loads(Path(sys.argv[1]).read_text())
    status = Path(payload["status"])
    success, error, code = False, None, 1
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        memory_mb = payload["max_memory_mb"]
        if memory_mb is not None:
            bound = memory_mb * 1024**2
            try:
                _, hard = resource.getrlimit(resource.RLIMIT_AS)
                if hard != resource.RLIM_INFINITY:
                    bound = min(bound, hard)
                resource.setrlimit(resource.RLIMIT_AS, (bound, bound))
            except (ValueError, OSError) as exc:
                raise RuntimeError(f"memory limit unavailable: {exc}") from exc
        if payload["mode"] == "script":
            runpy.run_path(payload["path"], run_name="__main__")
        else:
            sys.path.insert(0, payload["root"])
            module = importlib.import_module(payload["module"])
            getattr(module, payload["name"])(*payload["args"])
        success, code = True, 0
    except SystemExit as exc:
        code = exc.code if type(exc.code) is int else (0 if exc.code is None else 1)
        success = code == 0
        error = None if success else f"SystemExit: {exc.code}"
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        memory = rss / (1024**2 if sys.platform == "darwin" else 1024)
        status.write_text(json.dumps({"success": success, "error": error, "memory_mb": memory}))
    raise SystemExit(code)
