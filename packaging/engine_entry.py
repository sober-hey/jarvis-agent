#!/usr/bin/env python3
"""Offline JARVIS engine import check, using only the product interpreter.

Run: packaging/runtime/engine-venv/bin/python3 -I -S -B packaging/engine_entry.py --self-test
Optional --construct-test constructs and closes a real offline agent and database.
The immutable upstream bootstrap has no supported
externally-owned-runtime switch. ProductRuntimeAdapter therefore temporarily
adapts four real startup functions during import, restoring every alias after
import. This is explicitly NOT an unmodified SDK startup or an OS sandbox.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import inspect
import io
import json
import os
import re
import signal
import subprocess
import stat
import time
import sys
import sysconfig
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any, Literal, Protocol, cast

# Any is confined to heterogeneous JSON reports, never the vendor interface.
Report = dict[str, Any]


class ConnectionModule(Protocol):
    create_connection: object


class SessionStore(Protocol):
    def create_session(self, session_id: str, source: str) -> str: ...
    def get_session(self, session_id: str) -> dict[str, object] | None: ...
    def close(self) -> None: ...


class SessionFactory(Protocol):
    def __call__(self, db_path: Path) -> SessionStore: ...


class CompletionAPI(Protocol):
    def create(self, *, model: str, messages: list[dict[str, str]]) -> object: ...


class ChatAPI(Protocol):
    completions: CompletionAPI


class Client(Protocol):
    chat: ChatAPI

    def is_closed(self) -> bool: ...


class Agent(Protocol):
    tools: list[object]
    valid_tool_names: set[str]
    client: Client | None
    session_api_calls: int

    def close(self) -> None: ...


class AgentFactory(Protocol):
    def __call__(
        self,
        *,
        base_url: str,
        api_key: str,
        provider: str,
        api_mode: str,
        model: str,
        enabled_toolsets: list[str],
        quiet_mode: bool,
        skip_context_files: bool,
        skip_memory: bool,
        skip_background_review: bool,
        session_db: SessionStore,
        session_id: str,
        cwd: str,
        save_trajectories: bool,
        load_soul_identity: bool,
    ) -> Agent: ...


PROJECT = Path(__file__).resolve().parents[1]
VENDOR = PROJECT / "components/agent_engine/upstream"
VENV = PROJECT / "packaging/runtime/engine-venv"
PYTHON = VENV / "bin/python3"
RUNS = PROJECT / ".runs_01/b01-engine-construct-20261014"
COMMIT = "d97007c32d6340f7ba79ac1dfb5773cc00846448"
HOST = Path("/Volumes/扩展硬盘/hermes")


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def interpreter_check() -> None:
    if sys.version_info[:2] != (3, 14) or Path(sys.prefix).resolve() != VENV:
        raise RuntimeError(
            "Requires product Python 3.14 in packaging/runtime/engine-venv; no fallback"
        )
    if not sys.flags.isolated or not sys.flags.no_site or not sys.dont_write_bytecode:
        raise RuntimeError("Requires -I -S -B; site startup and .pth execution are forbidden")
    if inside(Path(sys.base_prefix).resolve(), HOST):
        raise RuntimeError("Host Hermes base interpreter is forbidden")


# Re-hashing 246 MB / 18k files on the external disk costs ~2.2 s per turn, which
# is a large share of a voice turn's latency. A verification receipt lets a turn
# skip the read-and-hash pass while still proving the tree matches the manifest:
# the receipt is only honoured when the manifest digest is unchanged, every file's
# (size, mtime_ns) is unchanged, and it is younger than VENDOR_RECEIPT_TTL.
# Any difference falls back to the full hash, so tampering that touches a file's
# metadata is always caught. The receipt lives in the 0700 data root, outside the
# vendor tree, and is refused if it is a symlink or not owned by this user.
VENDOR_RECEIPT_TTL = 3600.0
VENDOR_RECEIPT_NAME = "vendor-receipt.json"


def _tree_signature(manifest_sha: str) -> tuple[str, int]:
    signature = hashlib.sha256()
    signature.update(manifest_sha.encode())
    count = 0
    for path in sorted(VENDOR.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Vendor symbolic links are forbidden")
        if path.is_file():
            info = path.stat()
            signature.update(
                path.relative_to(VENDOR).as_posix().encode()
                + b"\0"
                + str(info.st_size).encode()
                + b"\0"
                + str(info.st_mtime_ns).encode()
                + b"\0"
            )
            count += 1
    return signature.hexdigest(), count


def _read_receipt(root: Path | None, signature: str, manifest_sha: str) -> Report | None:
    if root is None:
        return None
    receipt_path = root / VENDOR_RECEIPT_NAME
    try:
        info = receipt_path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        return None
    if stat.S_IMODE(info.st_mode) != 0o600:
        return None
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(receipt, dict):
        return None
    if receipt.get("signature") != signature or receipt.get("manifest_sha256") != manifest_sha:
        return None
    verified_at = receipt.get("verified_at")
    if not isinstance(verified_at, (int, float)) or time.time() - verified_at > VENDOR_RECEIPT_TTL:
        return None
    actual = receipt.get("actual")
    if not isinstance(actual, dict):
        return None
    return actual


def _write_receipt(root: Path | None, signature: str, manifest_sha: str, actual: Report) -> None:
    if root is None:
        return
    receipt_path = root / VENDOR_RECEIPT_NAME
    if receipt_path.is_symlink():
        return
    payload = json.dumps(
        {
            "signature": signature,
            "manifest_sha256": manifest_sha,
            "verified_at": time.time(),
            "actual": actual,
        }
    ).encode()
    fd, temporary = tempfile.mkstemp(prefix=".vendor-receipt-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, receipt_path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)


def verify_vendor(root: Path | None = None) -> Report:
    manifest_path = PROJECT / "packaging/VENDOR_MANIFEST.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if VENDOR.resolve() != VENDOR:
        raise RuntimeError("Vendor path may not be a symbolic link")
    signature, count = _tree_signature(manifest_sha)
    cached = _read_receipt(root, signature, manifest_sha)
    if cached is not None and cached.get("file_count") == count:
        if any(manifest.get(key) != cached.get(key) for key in ("commit", "tree_sha256", "file_count")):
            raise RuntimeError("Fixed vendor manifest mismatch; refusing import")
        return cast(Report, cached)
    digest = hashlib.sha256()
    for path in sorted(VENDOR.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Vendor symbolic links are forbidden")
        if path.is_file():
            digest.update(
                path.relative_to(VENDOR).as_posix().encode()
                + b"\0"
                + hashlib.sha256(path.read_bytes()).digest()
            )
    actual = {"commit": COMMIT, "tree_sha256": digest.hexdigest(), "file_count": count}
    if any(manifest.get(key) != value for key, value in actual.items()):
        raise RuntimeError("Fixed vendor manifest mismatch; refusing import")
    _write_receipt(root, signature, manifest_sha, actual)
    return actual


def environment(root: Path) -> dict[str, str]:
    # Do not inherit secrets, proxies, Python settings, or the host Hermes home.
    return {
        "HOME": str(root / "home"),
        "HERMES_HOME": str(root / "engine-home"),
        "JARVIS_HOME": str(root / "engine-home"),
        "HERMES_RUNTIME_DIR": str(root / "runtime"),
        "TMPDIR": str(root / "tmp"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "PATH": "/nonexistent",
        "LANG": "en_US.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "PIP_NO_INDEX": "1",
    }


class OfflinePolicy:
    """Fail closed before Python socket/exec actions; counts remain visible on failure."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.network_attempts = 0
        self.local_bind_attempts = 0
        self.network_callers: list[Report] = []
        self.process_attempts = 0
        self.process_callers: list[Report] = []
        self.host_access_attempts = 0
        self.outside_write_attempts = 0

    def audit(self, event: str, args: tuple[object, ...]) -> None:
        if event == "socket.bind":
            self.local_bind_attempts += 1
            raise PermissionError("Offline policy: local socket bind denied")
        if event in {
            "socket.connect",
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "socket.gethostbyaddr",
            "socket.sendto",
        }:
            self.network_attempts += 1
            import traceback

            self.network_callers.append(
                {
                    "event": event,
                    "frames": [
                        {"file": item.filename, "line": item.lineno, "function": item.name}
                        for item in traceback.extract_stack()[-18:]
                    ],
                }
            )
            raise PermissionError("Offline policy: network denied")
        if event in {
            "subprocess.Popen",
            "os.system",
            "os.posix_spawn",
            "os.exec",
            "os.fork",
            "os.forkpty",
        }:
            import traceback

            self.process_callers.append(
                {
                    "event": event,
                    "frames": [
                        {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                        for frame in traceback.extract_stack()[-18:]
                    ],
                }
            )
            self.process_attempts += 1
            raise PermissionError("Offline policy: child processes/installers denied")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(cast(str | bytes | os.PathLike[str], args[0]))).absolute()
            # resolve() has no audit open recursion and catches linked host imports.
            path = path.resolve()
            if inside(path, HOST):
                self.host_access_attempts += 1
                raise PermissionError("Offline policy: host Hermes access denied")
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
            )
            if writing and not inside(path, self.root) and path != Path(os.devnull):
                self.outside_write_attempts += 1
                raise PermissionError("Offline policy: writes outside private run denied")

    def counts(self) -> dict[str, int]:
        return {
            key: getattr(self, key)
            for key in (
                "network_attempts",
                "process_attempts",
                "host_access_attempts",
                "outside_write_attempts",
            )
        }


class ProductRuntimeAdapter:
    """Temporary explicit adaptation of upstream's mutable-install lifecycle.

    Real modules still execute. No synthetic modules, Nix stamps, fake argv,
    source transforms, or fallback source trees are used. All imported aliases
    of the four adapters are restored before returning to the caller.
    """

    def __init__(self) -> None:
        self.replacements: list[tuple[ModuleType, str, object, Callable[..., object]]] = []
        self.calls: list[str] = []

    def __enter__(self) -> ProductRuntimeAdapter:
        targets = (
            ("hermes_cli._early_recovery", "restore_interrupted_pull", False),
            ("hermes_cli._early_recovery", "recover_if_needed", False),
            ("hermes_cli.venv_sync", "prepare_launch", None),
            ("pm.environments", "activate_dependencies", None),
        )
        for name, attr, result in targets:
            module = importlib.import_module(name)
            original = getattr(module, attr)

            def adapted(
                *args: object,
                _name: str = name + "." + attr,
                _result: object = result,
                **kwargs: object,
            ) -> object:
                self.calls.append(_name)
                return _result

            self.replacements.append((module, attr, original, adapted))
            setattr(module, attr, adapted)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        for owner, attr, original, adapted in self.replacements:
            for module in list(sys.modules.values()):
                for key, value in list(vars(module).items()):
                    if value is adapted:
                        setattr(module, key, original)
            setattr(owner, attr, original)
        return False

    def remaining_aliases(self) -> list[str]:
        return [
            name + "." + key
            for name, module in list(sys.modules.items())
            for key, value in list(vars(module).items())
            if any(value is item[3] for item in self.replacements)
        ]


def module_sources() -> Report:
    files: dict[str, str] = {}
    for name, module in list(sys.modules.items()):
        value = getattr(module, "__file__", None)
        if value:
            files[name] = str(Path(value).resolve())
    return files


def construct_objects(
    root: Path,
    agent_factory: AgentFactory,
    db_factory: SessionFactory,
    report: Report,
    policy: OfflinePolicy,
    probe_model: bool,
) -> None:
    """Use real vendor constructors after restoring every lifecycle adapter alias."""
    report["scope"] = "real vendor object construction, close and persistent DB reopen"
    db_path = root / "engine-home" / "state.db"
    session_id = "b01-offline-persistence"
    # Fresh non-secret configuration derived from the vendor's config schema.
    # A fixed context pin avoids metadata probes; no live config or key is persisted.
    (root / "engine-home" / "config.yaml").write_text(
        "model:\n  default: b01-offline-test\n  provider: custom\n"
        "  base_url: http://offline.invalid/v1\n  context_length: 256000\n"
        "compression:\n  enabled: false\n"
    )
    db = db_factory(db_path=db_path)
    report["session_db_constructed"] = True
    agent: Agent | None = None
    try:
        if db.create_session(session_id, "b01-offline") != session_id:
            raise RuntimeError("Session create returned incorrect identity")
        agent = agent_factory(
            base_url="http://offline.invalid/v1",
            api_key="b01-in-memory-noncredential",
            provider="custom",
            api_mode="chat_completions",
            model="b01-offline-test",
            enabled_toolsets=[],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            skip_background_review=True,
            session_db=db,
            session_id=session_id,
            cwd=str(root),
            save_trajectories=False,
            load_soul_identity=False,
        )
        report["agent_constructed"] = True
        report["tool_count"] = len(agent.tools)
        if agent.tools or agent.valid_tool_names:
            raise RuntimeError("Offline construction unexpectedly enabled tools")
        client = agent.client
        if client is None:
            raise RuntimeError("Real client not constructed")
        report["model_calls"] = agent.session_api_calls
        if agent.session_api_calls or policy.network_attempts or policy.process_attempts:
            raise RuntimeError("Construction attempted network, model or installer execution")
        if probe_model:
            before = policy.network_attempts
            try:
                client.chat.completions.create(
                    model="b01-offline-test", messages=[{"role": "user", "content": "deny"}]
                )
            except Exception:
                report["model_request_denied"] = policy.network_attempts > before
                if not report["model_request_denied"]:
                    raise RuntimeError("Model denial did not reach network policy") from None
            else:
                raise RuntimeError("Model request unexpectedly succeeded")
        agent.close()
        agent.close()  # public lifecycle promises idempotence
        report["agent_closed"] = agent.client is None and client.is_closed()
        if not report["agent_closed"]:
            raise RuntimeError("Agent close retained its HTTP client")
    finally:
        if agent is not None:
            agent.close()
        db.close()
        report["session_db_closed"] = vars(db)["_conn"] is None
    reopened = db_factory(db_path=db_path)
    try:
        report["session_db_reopened"] = reopened is not db
        row = reopened.get_session(session_id)
        report["session_persisted"] = bool(
            row and row.get("id") == session_id and row.get("source") == "b01-offline"
        )
        if not report["session_persisted"]:
            raise RuntimeError("Persisted session missing or altered after reopening")
    finally:
        reopened.close()
        report["reopened_db_closed"] = vars(reopened)["_conn"] is None
    if not (report["session_db_closed"] and report["reopened_db_closed"]):
        raise RuntimeError("Session DB close retained writer connection")


def worker(
    root: Path, omit_dependencies: bool = False, construct: bool = False, probe_model: bool = False
) -> int:
    report: Report = {
        "ok": False,
        "scope": "real vendor imports and public signatures only",
        "agent_constructed": False,
        "session_db_constructed": False,
        "model_calls": 0,
        "sdk_initialization_unmodified": False,
        "bootstrap_policy": (
            "temporary ProductRuntimeAdapter; no upstream installation/update lifecycle"
        ),
        "environment_keys": sorted(os.environ),
    }
    policy = OfflinePolicy(root)
    adapter = None
    captured = io.StringIO()
    try:
        interpreter_check()
        expected = environment(root)
        # Only controlled paths/settings are reported, never inherited environment values.
        report["environment"] = {key: os.environ.get(key) for key in expected}
        if any(os.environ.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Private child environment mismatch")
        report["vendor"] = verify_vendor()
        report["interpreter"] = {
            "executable": sys.executable,
            "prefix": sys.prefix,
            "base_prefix": sys.base_prefix,
            "version": sys.version,
        }
        purelib = Path(sysconfig.get_path("purelib")).resolve()
        if not inside(purelib, VENV):
            raise RuntimeError("Site-packages escaped independent venv")
        # -S bypasses site.py and all .pth files. Add exactly the built dependency directory.
        if not omit_dependencies:
            sys.path.append(str(purelib))
        sys.path.insert(0, str(VENDOR))
        sys.addaudithook(policy.audit)
        import socket

        # Load the real dependency before bootstrap so its optimization has no
        # deferred import hook; restore both socket aliases after vendor import.
        urllib_connection = (
            cast(ConnectionModule, importlib.import_module("urllib3.util.connection"))
            if not omit_dependencies
            else None
        )
        urllib_create_connection = (
            urllib_connection.create_connection if urllib_connection else None
        )
        connection = socket.create_connection
        finders = list(sys.meta_path)
        try:
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                with ProductRuntimeAdapter() as adapter:
                    AIAgent = cast(AgentFactory, importlib.import_module("run_agent").AIAgent)
                    SessionDB = cast(
                        SessionFactory, importlib.import_module("hermes_state").SessionDB
                    )
                report["symbols"] = {
                    "AIAgent": {
                        "module": inspect.getfile(AIAgent),
                        "signature": str(inspect.signature(AIAgent)),
                    },
                    "SessionDB": {
                        "module": inspect.getfile(SessionDB),
                        "signature": str(inspect.signature(SessionDB)),
                    },
                }
        finally:
            # Bootstrap's HTTP optimization is irrelevant in an offline product process.
            # Remove that upstream hook and restore socket state, not the audit policy.
            socket.create_connection = connection
            if urllib_connection is not None:
                urllib_connection.create_connection = urllib_create_connection
            sys.meta_path[:] = [
                finder
                for finder in sys.meta_path
                if finder in finders or type(finder).__module__ != "hermes_bootstrap"
            ]
        report["bootstrap_network_hooks_remaining"] = (
            bool(getattr(socket.create_connection, "_hermes_happy_eyeballs", False))
            or bool(
                urllib_connection
                and getattr(urllib_connection.create_connection, "_hermes_happy_eyeballs", False)
            )
            or any(type(finder).__module__ == "hermes_bootstrap" for finder in sys.meta_path)
        )
        report["adapter_calls"] = adapter.calls if adapter else []
        report["adapter_aliases_remaining"] = adapter.remaining_aliases() if adapter else []
        if construct:
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                construct_objects(root, AIAgent, SessionDB, report, policy, probe_model)
        report["module_sources"] = module_sources()
        allowed = [VENDOR, VENV, Path(sys.base_prefix).resolve(), PROJECT / "packaging"]
        unexpected = {
            name: path
            for name, path in report["module_sources"].items()
            if not any(inside(Path(path), parent) for parent in allowed)
        }
        report["unexpected_module_sources"] = unexpected
        for symbol in report["symbols"].values():
            if not inside(Path(symbol["module"]).resolve(), VENDOR):
                raise RuntimeError("Required class did not originate in fixed vendor")
        if (
            unexpected
            or report["adapter_aliases_remaining"]
            or report["bootstrap_network_hooks_remaining"]
            or any(
                value
                for key, value in policy.counts().items()
                if not (
                    probe_model
                    and (
                        key == "network_attempts"
                        or (
                            key == "process_attempts"
                            and all(
                                any(
                                    frame["file"].endswith("/platform.py")
                                    for frame in attempt["frames"]
                                )
                                for attempt in policy.process_callers
                            )
                        )
                    )
                )
            )
        ):
            raise RuntimeError("Isolation or offline policy violation")
        report["ok"] = True
    except BaseException as exc:
        # Exception strings from upstream can contain configuration; report type and
        # missing module name only, not arbitrary provider or environment values.
        import traceback

        report["error_frames"] = [
            {"file": f.filename, "line": f.lineno, "function": f.name}
            for f in traceback.extract_tb(exc.__traceback__)
        ]
        report["error"] = {"type": type(exc).__name__, "missing_module": getattr(exc, "name", None)}
        if isinstance(exc, RuntimeError) and type(exc).__module__ == "builtins":
            report["error"]["detail"] = str(exc)
    finally:
        report["policy_counts"] = policy.counts()
        report["blocked_local_bind_attempts"] = policy.local_bind_attempts
        report["network_callers"] = policy.network_callers
        report["process_callers"] = policy.process_callers
        report["upstream_output_chars_suppressed"] = len(captured.getvalue())
        report.pop("environment", None) if not report.get("interpreter") else None
        report["module_sources"] = module_sources()
        report["host_module_sources"] = {
            name: path
            for name, path in report["module_sources"].items()
            if inside(Path(path), HOST)
        }
        print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


def self_test(
    timeout: float = 45,
    omit_dependencies: bool = False,
    construct: bool = False,
    probe_model: bool = False,
) -> Report:
    interpreter_check()
    RUNS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="offline-", dir=RUNS) as temporary:
        root = Path(temporary).resolve()
        env = environment(root)
        for key in (
            "HOME",
            "HERMES_HOME",
            "HERMES_RUNTIME_DIR",
            "TMPDIR",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
        ):
            Path(env[key]).mkdir(parents=True, exist_ok=True)
        command = [
            str(PYTHON),
            "-I",
            "-S",
            "-B",
            str(Path(__file__).resolve()),
            "--_worker",
            str(root),
        ]
        if omit_dependencies:
            command.append("--_omit-dependencies")
        if construct:
            command.append("--construct-test")
        if probe_model:
            command.append("--_probe-model")
        process = subprocess.Popen(
            command,
            env=env,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            return {
                "ok": False,
                "error": {"type": "TimeoutExpired"},
                "exit_code": -signal.SIGKILL,
                "command": command,
                "child_reaped": process.poll() is not None,
            }
        try:
            report: Report = cast(Report, json.loads(stdout))
        except ValueError:
            report = {
                "ok": False,
                "error": {"type": "InvalidWorkerReport"},
                "stdout_chars": len(stdout),
            }
        report.update(
            {
                "exit_code": process.returncode,
                "command": command,
                "stderr_chars_suppressed": len(stderr),
                "private_run_removed": True,
            }
        )
        report["ok"] = report["ok"] and process.returncode == 0
        return report


def self_evaluate() -> Report:
    """Evaluate fixed layout and sanitized environment policy without vendor IO."""
    from datetime import datetime, timezone

    # Pure environment builder: deliberately not self_test/worker/verify_vendor.
    candidate = environment(Path("/jarvis-evaluation-not-created"))
    checks = {
        "fixed_interpreter_layout": PYTHON == VENV / "bin/python3",
        "vendor_layout": VENDOR == PROJECT / "components/agent_engine/upstream",
        "pinned_commit": bool(re.fullmatch(r"[0-9a-f]{40}", COMMIT)),
        "minimal_environment": set(candidate)
        == {
            "HOME",
            "HERMES_HOME",
            "JARVIS_HOME",
            "HERMES_RUNTIME_DIR",
            "TMPDIR",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "PATH",
            "LANG",
            "PYTHONNOUSERSITE",
            "PYTHONDONTWRITEBYTECODE",
            "UV_OFFLINE",
            "UV_PYTHON_DOWNLOADS",
            "PIP_NO_INDEX",
        },
        "offline_environment": candidate.get("UV_OFFLINE") == "1"
        and candidate.get("PIP_NO_INDEX") == "1"
        and candidate.get("PATH") == "/nonexistent",
    }
    return {
        "module": "engine_entry",
        "domain": "Python embedded engine isolated startup offline lifecycle adapters",
        "current_version": "1.0.0",
        "known_limitations": [
            "Upstream startup needs temporary ProductRuntimeAdapter; not an unmodified SDK or OS sandbox.",
            "This policy evaluation does not import vendor code, verify its tree, construct agents or open databases.",
        ],
        "improvement_directions": [
            "Prefer a supported externally-owned runtime API when upstream provides one."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "upstream_commit": COMMIT,
        "sdk_initialization_unmodified": False,
        "checks": checks,
        "ok": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--construct-test", action="store_true")
    parser.add_argument("--_probe-model", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_worker", help=argparse.SUPPRESS)
    parser.add_argument("--_omit-dependencies", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._worker:
        return worker(
            Path(args._worker).resolve(),
            args._omit_dependencies,
            args.construct_test,
            args._probe_model,
        )
    if not (args.self_test or args.construct_test):
        parser.error("Only --self-test is supported; conversational engine is not enabled")
    try:
        report = self_test(construct=args.construct_test)
    except RuntimeError as exc:
        report = {"ok": False, "error": {"type": type(exc).__name__, "detail": str(exc)}}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
