"""Lazy registry for local tools; route() is the validated/audited entry point.

Root checks constrain file adapters and command cwd, NOT arbitrary subprocess
file I/O or network access. execute is trusted argv execution, not a sandbox.
No directory, database or subprocess is created at router construction time.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, cast


def self_evaluate() -> dict[str, object]:
    """Describe this adapter without loading state or executing work."""
    from datetime import UTC, datetime

    return {
        "module": "tool_router",
        "domain": "validated lazy tool dispatch and private execution audit",
        "current_version": "schema-checked local adapter registry with explicit completed/denied/unknown outcomes",
        "known_limitations": [
            "Root checks constrain file adapters and command cwd, not arbitrary process file I/O or network; this is not an OS sandbox.",
            "The built-in schema validator implements a subset of JSON Schema; dynamic MCP schemas require jsonschema.",
            "Audit persistence is optional and best-effort: a failed MindStore append does not undo an executed effect.",
            "Exporting tool schemas does not install a resident model tool-call loop or a module-level list_tools API.",
        ],
        "improvement_directions": [
            "Separate tool admission policy from heuristic shell checks before granting untrusted callers execution.",
            "Expose audit persistence degradation and reconcile it without replaying tool effects.",
            "Validate adapter schema drift and add a deliberate resident caller before claiming autonomous tool execution.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


def _json(value: Any) -> bytes:
    def check(item: Any) -> None:
        if type(item) is dict:
            mapping = cast(dict[Any, Any], item)
            if any(type(key) is not str for key in mapping):
                raise ValueError("invalid_json")
            for child in mapping.values():
                check(child)
        elif type(item) is list:
            for child in cast(list[Any], item):
                check(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("invalid_json")

    check(value)
    return json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False).encode()


def _summary(value: Any) -> dict[str, Any]:
    try:
        body = _json(value)
        return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
    except Exception:
        return {"sha256": None, "bytes": None}


def _validate(value: Any, schema: dict[str, Any]) -> None:
    kind = schema.get("type")
    good = {
        "object": type(value) is dict,
        "array": type(value) is list,
        "string": type(value) is str,
        "integer": type(value) is int,
        "number": type(value) in (int, float) and math.isfinite(value),
        "boolean": type(value) is bool,
        "null": value is None,
    }
    if kind and not good.get(kind, False):
        raise ValueError("invalid_arguments")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("invalid_arguments")
    if kind == "object":
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])).issubset(value):
            raise ValueError("invalid_arguments")
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key])
            elif schema.get("additionalProperties") is False:
                raise ValueError("invalid_arguments")
            elif isinstance(schema.get("additionalProperties"), dict):
                _validate(item, schema["additionalProperties"])
    elif kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise ValueError("invalid_arguments")
        for item in value:
            _validate(item, schema.get("items", {}))
    elif kind == "string":
        if "\0" in value or len(value) < schema.get("minLength", 0):
            raise ValueError("invalid_arguments")
    elif kind in ("integer", "number"):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ValueError("invalid_arguments")


_TEXT = {"type": "string"}
_PATH = {"type": "string", "minLength": 1}
_BOOL = {"type": "boolean"}
_NONNEG = {"type": "integer", "minimum": 0}
_STRINGS = {"type": "array", "items": _PATH}
_ENV = {"type": "object", "additionalProperties": _TEXT}


class ToolRouter:
    """All stateful adapters are initialized only when their tool is invoked.

    registry maps names to name/description/parameters/risk/callable metadata.
    Risk is the highest relevant boundary, not an assertion of authorization.
    Pass an existing MindStore to persist tool_call rows via its append API.
    """

    def __init__(
        self,
        *,
        allowed_roots: list[Path] | None = None,
        data_root: Path | None = None,
        mind_store: Any = None,
        repo_root: Path | None = None,
    ):
        self.allowed_roots = [
            Path(p).expanduser()
            for p in (
                [Path("仓库根/"), Path.home() / ".jarvis/workspace"]
                if allowed_roots is None
                else allowed_roots
            )
        ]
        self.data_root = (
            Path(data_root).expanduser() if data_root is not None else Path.home() / ".jarvis"
        )
        self.repo_root = (
            Path(repo_root)
            if repo_root is not None
            else self.allowed_roots[0]
            if self.allowed_roots
            else Path.cwd()
        )
        self.mind_store = mind_store
        self._instances: dict[str, Any] = {}
        self.registry: dict[str, dict[str, Any]] = {}
        self._register_files()
        self._register_adapters()

    def _register_adapters(self) -> None:
        # Explicit schemas mirror the adapters' public APIs; no coercion/default
        # insertion is performed on model-supplied arguments.
        specs: list[tuple[str, str, str, str, dict[str, Any], list[str], str]] = [
            (
                "search",
                "search",
                "search",
                "read",
                {
                    "pattern": _TEXT,
                    "path": _PATH,
                    "file_glob": _TEXT,
                    "context_lines": _NONNEG,
                    "limit": _NONNEG,
                    "output_mode": {"type": "string", "enum": ["content", "files_only", "count"]},
                },
                ["pattern"],
                "search code using ripgrep without following symlinks",
            ),
            (
                "find_files",
                "search",
                "find_files",
                "read",
                {"pattern": _PATH, "path": _PATH, "limit": _NONNEG},
                ["pattern"],
                "find allowed files using ripgrep globs",
            ),
            (
                "count",
                "search",
                "count",
                "read",
                {"pattern": _TEXT, "path": _PATH, "file_glob": _TEXT},
                ["pattern"],
                "count matching lines in allowed files",
            ),
            (
                "git_status",
                "git",
                "status",
                "read",
                {"porcelain": _BOOL},
                [],
                "read my repository status",
            ),
            (
                "git_diff",
                "git",
                "diff",
                "read",
                {"staged": _BOOL, "path": _TEXT},
                [],
                "read a repository diff without external diff helpers",
            ),
            (
                "git_log",
                "git",
                "log",
                "read",
                {"count": _NONNEG, "format": _TEXT},
                [],
                "read recent repository commits",
            ),
            (
                "git_add",
                "git",
                "add",
                "exec",
                {"files": _STRINGS},
                ["files"],
                "stage allowed repository paths; Git filters may execute",
            ),
            (
                "git_commit",
                "git",
                "commit",
                "exec",
                {"message": _PATH, "files": _STRINGS},
                ["message"],
                "commit repository changes; configured hooks and signing may execute",
            ),
            (
                "git_branch",
                "git",
                "branch",
                "write",
                {"name": _TEXT, "create": _BOOL, "delete": _BOOL},
                [],
                "list, create or safely delete repository branches",
            ),
            (
                "git_checkout",
                "git",
                "checkout",
                "exec",
                {"ref": _PATH},
                ["ref"],
                "check out a repository reference; configured hooks may execute",
            ),
            (
                "execute",
                "shell",
                "execute",
                "exec",
                {"command": _PATH, "cwd": _TEXT, "timeout": _NONNEG, "env": _ENV},
                ["command"],
                "run trusted argv commands; allowed roots constrain cwd, not file I/O or network",
            ),
            (
                "execute_sandboxed",
                "shell",
                "execute_sandboxed",
                "exec",
                {"command": _PATH, "allowed_writes": _STRINGS, "timeout": _NONNEG},
                ["command"],
                "run with macOS write restrictions; reads and network remain allowed",
            ),
            (
                "check_safety",
                "shell",
                "check_safety",
                "read",
                {"command": _TEXT},
                ["command"],
                "check the best-effort destructive-command guard, not authorization",
            ),
            (
                "pip_install",
                "shell",
                "pip_install",
                "exec",
                {"package": _PATH, "venv_python": _TEXT},
                ["package"],
                "install a trusted package into an allowed venv; package code and network may run",
            ),
            (
                "mcp_list_servers",
                "mcp",
                "list_servers",
                "read",
                {},
                [],
                "list configured MCP stdio servers",
            ),
            (
                "mcp_list_tools",
                "mcp",
                "list_tools",
                "read",
                {"name": _PATH},
                ["name"],
                "list tools already discovered on a connected MCP server",
            ),
            (
                "mcp_add_server",
                "mcp",
                "add_server",
                "write",
                {
                    "name": _PATH,
                    "command": _PATH,
                    "args": {"type": "array", "items": _TEXT},
                    "env": _ENV,
                    "transport": {"type": "string", "enum": ["stdio"]},
                },
                ["name", "command"],
                "configure a safety-checked MCP stdio command without launching it",
            ),
            (
                "mcp_remove_server",
                "mcp",
                "remove_server",
                "exec",
                {"name": _PATH},
                ["name"],
                "remove an MCP definition and terminate its process if connected",
            ),
            (
                "mcp_connect",
                "mcp",
                "connect",
                "exec",
                {"name": _PATH},
                ["name"],
                "launch a configured local MCP command; user privileges and network access apply",
            ),
            (
                "mcp_call_tool",
                "mcp",
                "call_tool",
                "exec",
                {"server_name": _PATH, "tool_name": _PATH, "arguments": {"type": "object"}},
                ["server_name", "tool_name", "arguments"],
                "call an MCP tool once; it may execute code, write files or use network",
            ),
            (
                "mcp_disconnect",
                "mcp",
                "disconnect",
                "exec",
                {"name": _PATH},
                ["name"],
                "terminate one managed MCP process group",
            ),
            (
                "mcp_disconnect_all",
                "mcp",
                "disconnect_all",
                "exec",
                {},
                [],
                "terminate all managed MCP process groups",
            ),
            (
                "vision_capture_screen",
                "vision",
                "capture_screen",
                "exec",
                {},
                [],
                "explicitly capture my screen into private vision storage",
            ),
            (
                "vision_capture_region",
                "vision",
                "capture_region",
                "exec",
                {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "w": {"type": "integer", "minimum": 1},
                    "h": {"type": "integer", "minimum": 1},
                },
                ["x", "y", "w", "h"],
                "explicitly capture a screen region into private vision storage",
            ),
            (
                "vision_describe_image",
                "vision",
                "describe_image",
                "network",
                {"image_path": _PATH, "question": _PATH},
                ["image_path"],
                "send an allowed image to the configured product vision model",
            ),
            (
                "vision_add_to_think_context",
                "vision",
                "add_to_think_context",
                "write",
                {"image_path": _PATH, "description": _PATH, "question": _TEXT},
                ["image_path", "description"],
                "archive an allowed image and publish its description as observation",
            ),
            (
                "vision_list_recent_images",
                "vision",
                "list_recent_images",
                "write",
                {"days": {"type": "integer", "minimum": 0, "maximum": 365000}},
                [],
                "list registered recent images; lazy storage initialization may write",
            ),
            (
                "goal_set_goal",
                "goal",
                "set_goal",
                "write",
                {"goal": _PATH, "success_criteria": _STRINGS},
                ["goal"],
                "record an internal goal with caller-defined criteria",
            ),
            (
                "goal_get_status",
                "goal",
                "get_status",
                "write",
                {"goal_id": _PATH},
                ["goal_id"],
                "read goal status; lazy database initialization may write",
            ),
            (
                "goal_update_progress",
                "goal",
                "update_progress",
                "write",
                {"goal_id": _PATH, "progress": {"type": "number", "minimum": 0}, "note": _TEXT},
                ["goal_id", "progress"],
                "record caller-reported goal progress, not verified task success",
            ),
            (
                "goal_list_active_goals",
                "goal",
                "list_active_goals",
                "write",
                {},
                [],
                "list active internal goals; lazy database initialization may write",
            ),
            (
                "goal_complete",
                "goal",
                "complete",
                "write",
                {"goal_id": _PATH, "summary": _TEXT},
                ["goal_id"],
                "manually complete an internal goal and record the summary",
            ),
            (
                "session_save_state",
                "session",
                "save_state",
                "write",
                {
                    "session_id": _PATH,
                    "context": {
                        "type": "object",
                        "properties": {
                            "current_task": _TEXT,
                            "progress": {"type": "number", "minimum": 0, "maximum": 1},
                            "pending_items": {"type": "array", "items": _TEXT},
                            "think_history": {"type": "array", "items": {"type": "object"}},
                        },
                        "required": ["current_task", "progress", "pending_items", "think_history"],
                    },
                },
                ["session_id", "context"],
                "save an internal checkpoint without executing pending work",
            ),
            (
                "session_resume",
                "session",
                "resume",
                "write",
                {"session_id": _PATH},
                ["session_id"],
                "restore checkpoint context without retries; initialization may write",
            ),
            (
                "session_list_sessions",
                "session",
                "list_sessions",
                "write",
                {"limit": _NONNEG},
                [],
                "list saved checkpoints; lazy storage initialization may write",
            ),
            (
                "session_auto_save_checkpoint",
                "session",
                "auto_save_checkpoint",
                "write",
                {},
                [],
                "save the active checkpoint if one was explicitly established",
            ),
        ]
        for name, adapter, method, risk, properties, required, description in specs:
            self._add(
                name,
                "I " + description + ".",
                risk,
                properties,
                required,
                partial(self._call_adapter, adapter, method),
            )

    def _adapter(self, name: str) -> Any:
        if name not in self._instances:
            if name == "search":
                from code_search import CodeSearch

                instance = CodeSearch()
            elif name == "git":
                from git_ops import GitOps

                instance = GitOps(str(self._path(str(self.repo_root))))
            elif name == "shell":
                from shell_executor import ShellExecutor

                instance = ShellExecutor(
                    [self._path(str(p)) for p in self.allowed_roots if p.is_dir()]
                )
            elif name == "mcp":
                from mcp_bridge import MCPBridge

                instance = MCPBridge(self.data_root / "mcp_servers.json")
            elif name == "vision":
                from vision_input import VisionInput

                instance = VisionInput(self.data_root)
            else:
                from inner_loop import MindStore

                if self.mind_store is None:
                    self.mind_store = MindStore(self.data_root)
                if name == "goal":
                    from goal_tracker import GoalTracker

                    instance = GoalTracker(self.data_root, self.mind_store)
                elif name == "session":
                    from session_resume import SessionResume

                    instance = SessionResume(self.data_root, self.mind_store)
                else:
                    raise ValueError("unknown_adapter")
            self._instances[name] = instance
        return self._instances[name]

    def _call_adapter(self, adapter: str, method: str, **args: Any) -> Any:
        values = dict(args)
        if adapter == "search":
            values["path"] = str(self._path(values.get("path", ".")))
        elif adapter == "shell" and method == "execute":
            values["cwd"] = str(self._path(values.get("cwd") or str(self.repo_root)))
        elif adapter == "shell" and method == "execute_sandboxed":
            values["allowed_writes"] = [
                str(self._path(p)) for p in values.get("allowed_writes", [])
            ]
        elif adapter == "vision" and "image_path" in values:
            values["image_path"] = str(self._path(values["image_path"]))
        return getattr(self._adapter(adapter), method)(**values)

    def _add(
        self,
        name: str,
        description: str,
        risk: str,
        properties: dict[str, Any],
        required: list[str],
        function: Callable[..., Any],
    ) -> None:
        self.registry[name] = {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": deepcopy(properties),
                "required": required,
                "additionalProperties": False,
            },
            "risk": risk,
            "callable": function,
        }

    def _files(self) -> Any:
        if "files" not in self._instances:
            from file_ops import FileOps

            # Missing default roots are not implicitly created or authorized.
            self._instances["files"] = FileOps([p for p in self.allowed_roots if p.is_dir()])
        return self._instances["files"]

    def _path(self, raw: str) -> Path:
        target = self._files()._path(raw)
        for component in (target, *target.parents):
            if component.is_symlink():
                raise PermissionError("path_denied")
        return target

    def _file(self, method: str, **args: Any) -> Any:
        return getattr(self._files(), method)(**args)

    def _register_files(self) -> None:
        definitions: list[tuple[str, str, str, dict[str, Any], list[str]]] = [
            (
                "read_file",
                "I read UTF-8 files inside my allowed roots.",
                "read",
                {
                    "path": _PATH,
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": _NONNEG,
                },
                ["path"],
            ),
            (
                "write_file",
                "I atomically write UTF-8 files inside my allowed roots.",
                "write",
                {"path": _PATH, "content": _TEXT},
                ["path", "content"],
            ),
            (
                "edit_file",
                "I replace matching text in an allowed file.",
                "write",
                {"path": _PATH, "old_string": _PATH, "new_string": _TEXT, "replace_all": _BOOL},
                ["path", "old_string", "new_string"],
            ),
            (
                "delete_file",
                "I delete an allowed regular file.",
                "write",
                {"path": _PATH},
                ["path"],
            ),
            (
                "list_dir",
                "I list allowed directories without following symlinks.",
                "read",
                {"path": _PATH, "pattern": _PATH},
                ["path"],
            ),
            (
                "apply_patch",
                "I apply a V4A file patch inside my allowed roots.",
                "write",
                {"patch_text": _PATH},
                ["patch_text"],
            ),
        ]
        for name, description, risk, properties, required in definitions:
            self._add(
                name,
                description,
                risk,
                properties,
                required,
                partial(self._file, name),
            )

    def _safe_command(self, command: str) -> None:
        if not self._adapter("shell").check_safety(command).get("safe"):
            raise PermissionError("unsafe_command")

    def _precheck(self, name: str, args: dict[str, Any]) -> None:
        if name in {"read_file", "write_file", "edit_file", "delete_file", "list_dir"}:
            self._path(args["path"])
        if name in {"search", "find_files", "count"}:
            self._path(args.get("path", "."))
        if name.startswith("git_"):
            import os

            if any(
                key.startswith("GIT_") and key not in {"GIT_TERMINAL_PROMPT", "GIT_PAGER"}
                for key in os.environ
            ):
                raise PermissionError("git_environment_denied")
            repository = self._path(str(self.repo_root))
            if not repository.is_dir():
                raise ValueError("invalid_repository")
            # Never let git discover an ancestor repository or follow an
            # external worktree gitdir. Those could mutate outside the roots.
            dotgit = self._path(str(repository / ".git"))
            if not dotgit.is_dir():
                raise PermissionError("repository_boundary_denied")
            if name in {"git_add", "git_commit", "git_checkout", "git_branch"}:
                # Git's metadata writes are path based, not FileOps dir-fd
                # operations. Refuse redirected/shared metadata rather than
                # claiming the root allowlist is a kernel sandbox.
                for current, directories, files in os.walk(dotgit, followlinks=False):
                    if any((Path(current) / child).is_symlink() for child in directories + files):
                        raise PermissionError("git_metadata_denied")
                if (dotgit / "commondir").exists():
                    raise PermissionError("git_metadata_denied")
                config = (
                    self._adapter("git")
                    ._run("config", "--local", "--null", "--no-includes", "--list")
                    .stdout
                )
                for record in config.split("\0"):
                    key, _, value = record.partition("\n")
                    if key.lower().startswith(("include.", "includeif.")) or key.lower() in {
                        "core.worktree",
                        "extensions.worktreeconfig",
                    }:
                        raise PermissionError("git_configuration_denied")
            for raw in args.get("files", []) + ([args["path"]] if args.get("path") else []):
                value = Path(raw).expanduser()
                target = self._path(str(value if value.is_absolute() else repository / value))
                if not target.is_relative_to(repository):
                    raise PermissionError("repository_boundary_denied")
            if args.get("create") and args.get("delete"):
                raise ValueError("invalid_arguments")
            if name == "git_commit" and (not args["message"].strip() or args.get("files") == []):
                raise ValueError("invalid_arguments")
        if name in {"execute", "execute_sandboxed"}:
            self._safe_command(args["command"])
            directory = (
                self._path(args.get("cwd") or str(self.repo_root))
                if name == "execute"
                else self._path(str(Path.cwd()))
            )
            if not directory.is_dir():
                raise ValueError("invalid_cwd")
            for raw in args.get("allowed_writes", []):
                self._path(raw)
            for key in args.get("env", {}):
                if not key or "=" in key:
                    raise ValueError("invalid_arguments")
        if name == "pip_install":
            import sys

            python = Path(args.get("venv_python") or sys.executable).expanduser()
            if not python.is_absolute():
                python = self._path(str(self.repo_root)) / python
            self._path(str(python.parent))
            self._path(str(Path.cwd()))
            if (
                not args["package"].strip()
                or args["package"].startswith("-")
                or any(c in args["package"] for c in "\r\n")
            ):
                raise ValueError("invalid_arguments")
            import shlex

            self._safe_command(
                shlex.join([str(python), "-I", "-m", "pip", "install", "--", args["package"]])
            )
        if name.startswith(("mcp_", "goal_", "session_", "vision_")):
            # A constructor-owned product root is a separate storage grant.
            # It is not a grant to arbitrary filesystem tool arguments.
            import product_home

            product_home.safe_absolute(self.data_root)
            if name.startswith(("goal_", "session_", "vision_")):
                product_home.validate_root(self.data_root)
            for child in ("mind.db", "goals.db", "sessions", "vision", "mcp_servers.json"):
                product_home.safe_absolute(self.data_root / child)
        if name in {"mcp_add_server", "mcp_connect", "mcp_call_tool"}:
            import shlex

            config: Any
            if name == "mcp_add_server":
                config = args
            else:
                config = (
                    self._adapter("mcp")
                    ._load_config()["servers"]
                    .get(args.get("server_name", args.get("name")))
                )
                if not isinstance(config, dict):
                    raise PermissionError("mcp_command_unverifiable")
                config = cast(dict[str, Any], config)
                if not config.get("command"):
                    raise PermissionError("mcp_command_unverifiable")
            self._safe_command(shlex.join([config["command"], *config.get("args", [])]))
            if name == "mcp_call_tool":
                catalog = self._adapter("mcp").list_tools(args["server_name"])
                tool = next((item for item in catalog if item["name"] == args["tool_name"]), None)
                if tool is None:
                    raise PermissionError("mcp_tool_unverifiable")
                # Dynamic external schemas require a complete validator. If it
                # is unavailable, do not silently weaken validation.
                try:
                    import jsonschema
                except ImportError:
                    raise PermissionError("mcp_schema_unverifiable") from None
                jsonschema.validate(args["arguments"], tool["inputSchema"])
        if name.startswith("vision_") and "image_path" in args:
            self._path(args["image_path"])
        if name.startswith("session_") and "session_id" in args:
            from session_resume import _session_id  # pyright: ignore[reportPrivateUsage]

            _session_id(args["session_id"])
        if (
            name == "read_file"
            and args.get("end_line", 0)
            and args["end_line"] < args.get("start_line", 1)
        ):
            raise ValueError("invalid_arguments")
        if name == "apply_patch":
            for operation in self._files()._parse_patch(args["patch_text"]):
                self._path(operation.path)
                if operation.move is not None:
                    self._path(operation.move)

    def validate_arguments(self, tool_name: str, args: Any) -> None:
        """Validate shape without admitting an execution or writing an audit."""
        if tool_name not in self.registry or len(_json(args)) > 4 * 1024 * 1024:
            raise ValueError("invalid_arguments")
        _validate(args, self.registry[tool_name]["parameters"])

    def to_openai_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    key: deepcopy(item[key]) for key in ("name", "description", "parameters")
                },
            }
            for item in self.registry.values()
        ]

    def route(self, tool_name: str, args: Any) -> dict[str, Any]:
        started = time.monotonic()
        entry = self.registry.get(tool_name) if type(tool_name) is str else None
        audit: dict[str, Any] = {
            "category": "tool_call",
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(UTC).isoformat(),
            "tool_name": tool_name if entry else "<unknown>",
            "risk": entry["risk"] if entry else None,
            "args": _summary(args),
        }
        status: str = "failed"
        error: str | None = None
        result: Any = None
        invoked = False
        try:
            if entry is None:
                status, error = "unknown", "unknown_tool"
            else:
                body = _json(args)
                if len(body) > 4 * 1024 * 1024:
                    raise ValueError("invalid_arguments")
                _validate(args, entry["parameters"])
                self._precheck(tool_name, args)
                invoked = True
                result = entry["callable"](**args)
                if isinstance(result, dict):
                    result = cast(dict[str, Any], result)
                _json(result)
                status = "completed"
                if tool_name == "vision_describe_image" and result == "(视觉模型未配置)":
                    # The adapter conflates missing config and transport failure;
                    # do not claim success or safely repeat a possibly sent call.
                    status, error, result = "unknown", "vision_outcome_unavailable", None
                elif (
                    isinstance(result, dict) and "exit_code" in result and result["exit_code"] != 0
                ):
                    # A timeout/negative return cannot prove absence of a side
                    # effect. Preserve known nonzero exit, but never retry.
                    status = "unknown" if result["exit_code"] < 0 else "failed"
                    error = "execution_outcome_unknown" if status == "unknown" else "tool_failed"
                    result = cast(dict[str, Any], {"exit_code": result["exit_code"]})
                elif isinstance(result, dict) and (
                    cast(dict[str, Any], result).get("errors")
                    or cast(dict[str, Any], result).get("error")
                    or cast(dict[str, Any], result).get("success") is False
                    or cast(dict[str, Any], result).get("connected") is False
                ):
                    status, error, result = "unknown", "tool_failed", None
        except PermissionError:
            result = None
            status, error = (
                ("unknown", "execution_outcome_unknown")
                if invoked
                else ("denied", "security_denied")
            )
        except Exception:
            result = None
            status, error = (
                ("unknown", "execution_outcome_unknown")
                if invoked
                else ("failed", "invalid_arguments")
            )
        audit.update(
            status=status, result=_summary(result), duration_ms=(time.monotonic() - started) * 1000
        )
        if self.mind_store is not None:
            try:
                self.mind_store.append("tool_call", deepcopy(audit), source="tool-router")
                audit["persisted"] = True
            except Exception:
                audit["persisted"] = False
        answer: dict[str, Any] = {
            "ok": status == "completed",
            "status": status,
            "result": result,
            "audit": audit,
        }
        if error:
            answer["error"] = error
        return answer
