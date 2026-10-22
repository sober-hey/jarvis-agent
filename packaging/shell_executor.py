"""Synchronous argv-based command execution (not a PTY/unified_exec session)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NotRequired, TypedDict


def self_evaluate() -> dict[str, object]:
    """Describe execution boundaries without launching commands or probing hosts."""
    from datetime import UTC, datetime

    return {
        "module": "shell_executor",
        "domain": "synchronous argv execution with optional macOS write sandbox and venv pip",
        "current_version": "shell=False subprocess runner with timeout and Seatbelt write restrictions",
        "known_limitations": [
            "allowed_roots constrains cwd only; the argv safety guard cannot authorize arbitrary scripts or interpreters.",
            "Write sandbox requires macOS sandbox-exec and intentionally permits reads and network access; it is not secret isolation.",
            "Captured output is unbounded and timeout cleanup does not manage an entire descendant process group.",
            "pip_install executes trusted package code with normal user rights outside the write sandbox.",
        ],
        "improvement_directions": [
            "Bound stdout/stderr capture and add process-group-aware deadline cleanup.",
            "Define explicit read/network/resource policies and fail-closed platform backends before extending sandbox guarantees.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


class ExecutionResult(TypedDict):
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float


class SafetyResult(TypedDict):
    safe: bool
    reason: NotRequired[str]


class InstallResult(TypedDict):
    success: bool
    output: str


class ShellExecutor:
    """Execute argv without an implicit shell; roots constrain cwd, not file I/O."""

    def __init__(self, allowed_roots: list[Path] | None = None, default_timeout: int = 120) -> None:
        if default_timeout <= 0:
            raise ValueError("default_timeout must be positive")
        self.default_timeout = default_timeout
        self.allowed_roots = (
            None if allowed_roots is None else tuple(self._directory(p) for p in allowed_roots)
        )

    @staticmethod
    def _path(value: str | Path) -> Path:
        """Reject traversal and symlinks before canonicalizing a policy path."""
        raw = os.fspath(value)
        if not raw or "\x00" in raw or ".." in raw.split(os.sep):
            raise ValueError("invalid path")
        path = Path(os.path.abspath(os.path.expanduser(raw)))
        for component in (path, *path.parents):
            if component.is_symlink():
                raise ValueError(f"symlinks are not allowed: {component}")
        return path.resolve(strict=False)

    @classmethod
    def _directory(cls, value: str | Path) -> Path:
        path = cls._path(value)
        if not path.is_dir():
            raise ValueError(f"not a directory: {path}")
        return path

    def _cwd(self, cwd: str = "") -> Path:
        path = self._directory(cwd or os.getcwd())
        if self.allowed_roots is not None and not any(
            path.is_relative_to(root) for root in self.allowed_roots
        ):
            raise ValueError("working directory is outside allowed_roots")
        return path

    @staticmethod
    def _result(
        start: float, stdout: str = "", stderr: str = "", code: int = -1
    ) -> ExecutionResult:
        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": code,
            "duration_ms": (time.monotonic() - start) * 1000.0,
        }

    def _run(
        self, argv: list[str], cwd: Path, timeout: int, env: dict[str, str] | None, start: float
    ) -> ExecutionResult:
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                timeout=timeout,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                close_fds=True,
            )
            return self._result(start, completed.stdout, completed.stderr, completed.returncode)
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            return self._result(start, output, "timeout")
        except FileNotFoundError as exc:
            return self._result(start, stderr=str(exc), code=127)
        except PermissionError as exc:
            return self._result(start, stderr=str(exc), code=126)
        except (OSError, TypeError, ValueError) as exc:
            return self._result(start, stderr=str(exc))

    def execute(
        self, command: str, cwd: str = "", timeout: int = 0, env: dict[str, str] | None = None
    ) -> ExecutionResult:
        """Run a trusted command; operators, pipes and expansions are literal argv.

        This method is not a filesystem sandbox. Use execute_sandboxed for
        kernel-enforced write restrictions. Environment overrides are merged.
        """
        start = time.monotonic()
        try:
            safety = self.check_safety(command)
            if not safety["safe"]:
                return self._result(start, stderr=safety.get("reason", "unsafe command"))
            argv = shlex.split(command)
            if not argv:
                raise ValueError("empty command")
            limit = timeout or self.default_timeout
            if limit <= 0:
                raise ValueError("timeout must be positive")
            directory = self._cwd(cwd)
            child_env = None if env is None else {**os.environ, **env}
        except (OSError, TypeError, ValueError) as exc:
            return self._result(start, stderr=str(exc))
        return self._run(argv, directory, limit, child_env, start)

    def execute_sandboxed(
        self, command: str, allowed_writes: list[str] | None = None, timeout: int = 60
    ) -> ExecutionResult:
        """Run in a disposable workspace with writable targets symlinked by name.

        Relative allowed_writes resolve against the caller's cwd. Their basename
        is the workspace alias; ambiguous duplicate names are rejected. Existing
        directories allow descendants; files/missing leaves allow that exact path.
        The disposable workspace is writable and discarded afterwards. Only listed
        host paths are writable. Reads/network stay allowed. On macOS Seatbelt
        enforces writes (symlinks alone do NOT isolate); other OSes fail closed.
        This is not secret isolation, resource isolation or a PTY session.
        """
        start = time.monotonic()
        try:
            safety = self.check_safety(command)
            if not safety["safe"]:
                return self._result(start, stderr=safety.get("reason", "unsafe command"))
            argv = shlex.split(command)
            if not argv:
                raise ValueError("empty command")
            limit = timeout or self.default_timeout
            if limit <= 0:
                raise ValueError("timeout must be positive")
            self._cwd()
            targets: list[Path] = []
            names: set[str] = set()
            for value in allowed_writes or []:
                target = self._path(value)
                self._cwd(str(target if target.is_dir() else target.parent))
                if target.exists() and not (target.is_dir() or target.is_file()):
                    raise ValueError("writable targets must be regular files or directories")
                if target == Path("/") or not target.name or target.name in names:
                    raise ValueError("root target or duplicate sandbox alias")
                names.add(target.name)
                targets.append(target)
            if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
                return self._result(
                    start, stderr="sandbox unavailable: macOS sandbox-exec required"
                )
            with tempfile.TemporaryDirectory(prefix="jarvis-shell-") as temporary:
                workspace = Path(temporary).resolve()
                for target in targets:
                    (workspace / target.name).symlink_to(
                        target, target_is_directory=target.is_dir()
                    )
                rules = [f"(subpath {json.dumps(str(workspace), ensure_ascii=False)})"]
                for target in targets:
                    kind = "subpath" if target.is_dir() else "literal"
                    rules.append(f"({kind} {json.dumps(str(target), ensure_ascii=False)})")
                profile = "\n".join(
                    [
                        "(version 1)",
                        "(allow default)",
                        "(deny file-write*)",
                        "(allow file-write* " + " ".join(rules) + ")",
                    ]
                )
                child_env = {
                    **os.environ,
                    "HOME": str(workspace),
                    "TMPDIR": str(workspace),
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
                sandbox_argv = ["/usr/bin/sandbox-exec", "-p", profile, *argv]
                return self._run(sandbox_argv, workspace, limit, child_env, start)
        except (OSError, TypeError, ValueError) as exc:
            return self._result(start, stderr=str(exc))

    def pip_install(self, package: str, venv_python: str = "") -> InstallResult:
        """Install one requirement/wheel using a verified venv's pip.

        Installation runs package code with normal user rights, not in the write
        sandbox. Call only for trusted packages. No implicit venv creation or
        ensurepip bootstrapping; an unavailable pip is reported as failure.
        """
        start = time.monotonic()
        try:
            if type(package) is not str or not package.strip() or package.startswith("-"):
                raise ValueError("package must be one requirement, not pip options")
            if "\x00" in package or "\n" in package or "\r" in package:
                raise ValueError("invalid package requirement")
            # Do not resolve the interpreter symlink: venv identity depends on
            # the invoked bin/python path, not on its base interpreter target.
            python = os.path.abspath(os.path.expanduser(venv_python or sys.executable))
            directory = self._cwd()
            # Ignore pip config and inherited redirect flags (PIP_TARGET etc.).
            child_env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("PIP_", "PYTHON"))
            }
            child_env["PIP_CONFIG_FILE"] = os.devnull
            probe = self._run(
                [python, "-I", "-c", "import sys; print(int(sys.prefix != sys.base_prefix))"],
                directory,
                self.default_timeout,
                child_env,
                start,
            )
            if probe["exit_code"] != 0 or probe["stdout"].strip() != "1":
                return {"success": False, "output": probe["stderr"] or "interpreter is not a venv"}
            result = self._run(
                [
                    python,
                    "-I",
                    "-m",
                    "pip",
                    "--disable-pip-version-check",
                    "--no-input",
                    "install",
                    "--no-user",
                    "--no-cache-dir",
                    "--",
                    package,
                ],
                directory,
                self.default_timeout,
                child_env,
                start,
            )
            return {
                "success": result["exit_code"] == 0,
                "output": result["stdout"] + result["stderr"],
            }
        except (OSError, TypeError, ValueError) as exc:
            return {"success": False, "output": str(exc)}

    def check_safety(self, command: str) -> SafetyResult:
        """Best-effort destructive-command guard, NOT an authorization boundary.

        Arbitrary interpreters, scripts, aliases and network clients cannot be
        proven safe by inspecting argv. Kernel write policy is independent.
        """
        try:
            return self._check_command(command, 0)
        except (TypeError, ValueError) as exc:
            return {"safe": False, "reason": f"invalid command: {exc}"}

    def _check_command(self, command: str, depth: int) -> SafetyResult:
        if depth > 8:
            return {"safe": False, "reason": "command nesting too deep"}
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        segment: list[str] = []
        for token in [*lexer, ";"]:
            if token and all(char in ";&|\n" for char in token):
                if segment:
                    result = self._check_argv(segment, depth)
                    if not result["safe"]:
                        return result
                    segment = []
            else:
                segment.append(token)
        if not command.strip():
            return {"safe": False, "reason": "empty command"}
        return {"safe": True}

    def _check_argv(self, argv: list[str], depth: int) -> SafetyResult:
        name = Path(argv[0]).name
        args = argv[1:]
        if name in {"sudo", "doas"}:
            return {"safe": False, "reason": "requires sudo"}
        if name in {"env", "command", "exec", "nohup"}:
            while args and (args[0].startswith("-") or "=" in args[0]):
                if args[0] in {"-u", "--unset", "-C", "--chdir"}:
                    args = args[2:]
                else:
                    args = args[1:]
            return self._check_argv(args, depth) if args else {"safe": True}
        if name in {"sh", "bash", "zsh", "dash", "ksh", "fish"}:
            for index, arg in enumerate(args):
                if arg.startswith("-") and "c" in arg[1:] and index + 1 < len(args):
                    return self._check_command(args[index + 1], depth + 1)
        dangerous = {
            "dd",
            "shutdown",
            "reboot",
            "halt",
            "poweroff",
            "init",
            "telinit",
            "mkswap",
            "fdisk",
            "sfdisk",
            "parted",
            "newfs",
            "wipefs",
        }
        if name in dangerous or name.startswith(("mkfs", "newfs_")):
            return {"safe": False, "reason": f"dangerous command: {name}"}
        if name == "diskutil" and any(
            arg.lower().startswith(("erase", "partition", "secureerase", "reset")) for arg in args
        ):
            return {"safe": False, "reason": "destructive disk operation"}
        if name == "rm":
            flags = "".join(
                arg[1:] for arg in args if arg.startswith("-") and not arg.startswith("--")
            )
            recursive = "r" in flags.lower() or "--recursive" in args
            force = "f" in flags or "--force" in args
            targets = [arg for arg in args if not arg.startswith("-")]
            if (recursive and force) or any(os.path.abspath(target) == "/" for target in targets):
                return {"safe": False, "reason": "dangerous recursive removal or root target"}
        return {"safe": True}
