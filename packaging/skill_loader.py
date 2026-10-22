"""Runtime skill loading, capability-gated APIs and OS-enforced worker sandbox.

Untrusted skill modules run in separate macOS Seatbelt processes. Other platforms
fail closed instead of claiming Python-level restrictions are a security sandbox.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import UTC, datetime
from queue import Empty, Queue
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

if __package__:
    from .skill_manifest import SkillCapability, SkillManifest, validate_name
    from .skill_registry import SkillRegistry
else:
    from skill_manifest import SkillCapability, SkillManifest, validate_name
    from skill_registry import SkillRegistry

RUNTIME_ENABLED = True
_MAX_ZIP = 50_000_000
_MAX_FILE = 5_000_000
_MAX_MEMBERS = 256


def self_evaluate() -> dict[str, object]:
    """Public implementation metadata, never a scan of installed/private skills."""
    return {
        "module": "skill_loader",
        "domain": "runtime skill discovery loading capability dispatch and process sandboxing",
        "current_version": "manifest-driven loader with macOS Seatbelt workers and allowlisted parent APIs",
        "known_limitations": [
            "Execution fails closed without macOS Seatbelt; no other OS sandbox backend is implemented.",
            "scan keeps one manifest per skill name in filesystem iteration order, losing other installed versions.",
            "Manifest checksums validate metadata consistency, not skill.py implementation bytes or authorship.",
            "The Seatbelt profile allows default reads; it is not a confidential-data read allowlist.",
        ],
        "improvement_directions": [
            "Retain all installed versions and select the active version with strict SemVer ordering.",
            "Add separate implementation integrity verification and explicit trusted package provenance.",
            "Test a least-privilege read policy and equivalent fail-closed sandbox backends on other platforms.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


class SkillExecutionError(RuntimeError):
    """A worker or its API call failed."""


def _quote(path: Path) -> str:
    return '"' + str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _profile(skill_dir: Path, sandbox_dir: Path, caps: set[SkillCapability]) -> str:
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(deny network*)",
        "(deny process-exec)",
        "(deny process-fork)",
    ]
    if SkillCapability.FILE_WRITE in caps:
        lines.append(f"(allow file-write* (subpath {_quote(sandbox_dir)}))")
    if SkillCapability.NETWORK in caps:
        lines += ["(allow network-outbound)", "(allow network-inbound)"]
    if SkillCapability.PROCESS_EXEC in caps:
        lines += ["(allow process-exec)", "(allow process-fork)"]
    lines.append(f"(allow file-read* (subpath {_quote(skill_dir)}))")
    return "\n".join(lines)


# The process applies Seatbelt before importing the skill. Parent runs the worker
# with a narrowly configured profile via DYLD_INSERT_LIBRARIES-independent
# bootstrap; sandbox_init is called before dynamic skill import.
class _ApiProxy:
    def __init__(self, send: Callable[..., object], prefix: str = ""):
        self._send, self._prefix = send, prefix

    def __getattr__(self, name: str) -> _ApiProxy:
        method = f"{self._prefix}.{name}" if self._prefix else name
        return _ApiProxy(self._send, method)

    def __call__(self, *args: object, **kwargs: object) -> object:
        return self._send(self._prefix, *args, **kwargs)


_WORKER = r"""import ctypes, importlib.util, json, os, sys
path, profile = sys.argv[1:3]
lib=ctypes.CDLL('/usr/lib/libsandbox.dylib')
lib.sandbox_init.argtypes=[ctypes.c_char_p,ctypes.c_uint64,ctypes.POINTER(ctypes.c_char_p)]
err=ctypes.c_char_p()
if lib.sandbox_init(profile.encode(),0,ctypes.byref(err)) != 0:
 print(json.dumps({'type':'fatal','error':'sandbox initialization failed'}),flush=True);sys.exit(70)
spec=importlib.util.spec_from_file_location('_jarvis_skill',path)
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module
class Api:
 def __init__(self,name): self.name=name
 def __getattr__(self, name): return Api(self.name+'.'+name)
 def __call__(self,*args,**kwargs):
  print(json.dumps({'type':'api','id':current[0],'method':self.name,'args':args,'kwargs':kwargs},ensure_ascii=False,default=str),flush=True)
  response=json.loads(sys.stdin.readline())
  if response.get('error'): raise PermissionError(response['error'])
  return response.get('value')
class Context:
 def __getattr__(self,name): return Api(name)
current=['']
try:
 spec.loader.exec_module(module);module.context=Context()
 if callable(getattr(module,'setup',None)): module.setup()
 print(json.dumps({'type':'ready'}),flush=True)
 for line in sys.stdin:
  try:
   request=json.loads(line);current[0]=request['id']; action=request['action']
   if action=='__shutdown__': break
   fn=getattr(module,action,None)
   if not callable(fn): raise AttributeError('skill function not found')
   result=fn(*request.get('args',[]),**request.get('kwargs',{}))
   print(json.dumps({'type':'result','id':current[0],'value':result},ensure_ascii=False,default=str),flush=True)
  except BaseException as exc:
   print(json.dumps({'type':'result','id':current[0],'error':type(exc).__name__+': '+str(exc)},ensure_ascii=False),flush=True)
except BaseException as exc:
 print(json.dumps({'type':'fatal','error':type(exc).__name__+': '+str(exc)},ensure_ascii=False),flush=True)
"""


class SkillLoader:
    """Scan/load installed skill packages, match triggers, discover and install.

    Runtime paths are ``root/<name>/<version>/{manifest.json,skill.py}``.
    The SQLite registry decides which version is active. Capability policy is
    enforced by Seatbelt plus a capability-filtered parent API dispatcher.
    """

    @staticmethod
    def self_evaluate() -> dict[str, object]:
        """Expose the same metadata without constructing a loader or opening paths."""
        return self_evaluate()

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        registry: SkillRegistry | None = None,
        context: object = None,
        logger: Callable[..., object] | None = None,
        search_fn: Callable[..., object] | None = None,
        evaluator: object = None,
        timeout: float = 60.0,
    ):
        self.root = Path(root or Path.home() / ".jarvis" / "skills").expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.sandbox_root = Path.home() / ".jarvis" / "sandbox"
        self.sandbox_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.sandbox_root, 0o700)
        self.registry = registry or SkillRegistry()
        self._owns_registry = registry is None
        self.context, self._logger = context, logger
        self.search_fn, self.evaluator = search_fn, evaluator
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self._workers: dict[str, subprocess.Popen[str]] = {}
        self._outputs: dict[str, Queue[str]] = {}
        self._manifests: dict[str, SkillManifest] = {}
        self._lock = threading.RLock()
        if sys.platform == "darwin":
            try:
                ctypes.CDLL("/usr/lib/libsandbox.dylib")
                self._sandbox_available = True
            except OSError:
                self._sandbox_available = False
        else:
            self._sandbox_available = False

    def _directory(self, name: str, version: str) -> Path:
        validate_name(name)
        path = self.root / name / version
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError("skill directory escapes root or is a symlink")
        return path

    def _read(self, directory: Path) -> SkillManifest:
        mp = directory / "manifest.json"
        cp = directory / "skill.py"
        for path in (mp, cp):
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"{path.name} must be a regular file")
        manifest = SkillManifest.from_dict(
            json.loads(mp.read_text(encoding="utf-8")), require_checksum=True
        )
        if manifest.name not in (directory.name, directory.parent.name):
            raise ValueError("manifest name/path mismatch")
        return manifest

    def scan(self) -> list[SkillManifest]:
        """Scan both flat and versioned package layouts without executing code."""
        found: dict[str, SkillManifest] = {}
        for skill_dir in self.root.iterdir():
            if not skill_dir.is_dir() or skill_dir.is_symlink():
                continue
            if (skill_dir / "manifest.json").exists():
                item = self._read(skill_dir)
                found[item.name] = item
            else:
                for version_dir in skill_dir.iterdir():
                    if (
                        version_dir.is_dir()
                        and not version_dir.is_symlink()
                        and (version_dir / "manifest.json").exists()
                    ):
                        item = self._read(version_dir)
                        found[item.name] = item
        return [found[name] for name in sorted(found)]

    def load(self, name: str) -> SkillManifest:
        """Read and authenticate manifest, reconcile registry, cache it."""
        manifests = self.scan()
        matching = [m for m in manifests if m.name == name]
        if not matching:
            raise KeyError(name)
        # Register only the version represented by the active registry, or install
        # an unregistered initial package. Never silently downgrade an active entry.
        try:
            active = self.registry.get(name)
        except KeyError:
            active = None
        candidate = next((m for m in matching if active and m.version == active.version), None)
        if candidate is None:
            candidate = max(
                matching,
                key=lambda m: tuple(int(x) for x in m.version.split("-")[0].split(".")[:3]),
            )
            if active is None:
                self.registry.install(candidate)
            else:
                if candidate.version != active.version:
                    if __package__:
                        from .skill_manifest import compare as semver_compare
                    else:
                        from skill_manifest import compare as semver_compare
                    if semver_compare(candidate.version, active.version) <= 0:
                        raise ValueError("disk version is not a valid upgrade")
                    self.registry.upgrade(candidate)
        if candidate.checksum != self.registry.get(name).checksum:
            raise ValueError("disk manifest checksum differs from registry")
        self._manifests[name] = candidate
        return candidate

    def _api(
        self, manifest: SkillManifest, method: str, args: list[object], kwargs: dict[str, object]
    ) -> object:
        if method.startswith("mind_store."):
            operation = method.partition(".")[2]
            if SkillCapability.READ_ONLY not in manifest.capabilities:
                raise PermissionError("mind_store requires READ_ONLY")
            if operation not in {
                "recent",
                "beliefs",
                "search",
                "get",
                "iter_entries",
                "latest_seq",
            }:
                raise PermissionError("MindStore method is not read-only/allowlisted")
            store = getattr(self.context, "mind_store", None)
            target = getattr(store, operation, None) if store is not None else None
            if not callable(target):
                raise RuntimeError(f"MindStore {operation} unavailable")
            return target(*args, **kwargs)
        required = {
            "log": SkillCapability.READ_ONLY,
            "search": SkillCapability.NETWORK,
            "outreach": SkillCapability.NETWORK,
            "emit_event": SkillCapability.FILE_WRITE,
            "model_call": SkillCapability.MODEL_CALL,
        }
        cap = required.get(method)
        if cap is None or cap not in manifest.capabilities:
            raise PermissionError(f"{method} requires declared capability {cap}")
        if method == "log":
            if self._logger:
                return self._logger(*args, **kwargs)
            import logging

            level, message = args[:2]
            getattr(logging.getLogger("jarvis.skill"), str(level).upper(), logging.info)(message)
            return None
        if method == "search":
            if self.search_fn:
                return self.search_fn(*args, **kwargs)
            if __package__:
                from .search import search
            else:
                from search import search
            return search(*args, **kwargs)
        target = getattr(self.context, method, None)
        if not callable(target):
            raise RuntimeError(f"{method} unavailable")
        return target(*args, **kwargs)

    def _start(self, manifest: SkillManifest) -> subprocess.Popen[str]:
        if not self._sandbox_available:
            raise RuntimeError("OS-enforced skill sandbox unavailable; execution refused")
        directory = self._directory(manifest.name, manifest.version).resolve()
        sandbox_dir = (self.sandbox_root / manifest.name).resolve()
        sandbox_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(sandbox_dir, 0o700)
        profile = _profile(directory, sandbox_dir, manifest.capabilities)
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-u", "-c", _WORKER, str(directory / "skill.py"), profile],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            close_fds=True,
        )
        assert process.stdout
        stdout = process.stdout
        ready_queue: Queue[str] = Queue()

        def pump_ready() -> None:
            while True:
                item = stdout.readline()
                ready_queue.put(item)
                if not item:
                    return

        threading.Thread(target=pump_ready, daemon=True).start()
        deadline = time.monotonic() + self.timeout
        try:
            while time.monotonic() < deadline:
                message = json.loads(
                    ready_queue.get(timeout=max(0.01, deadline - time.monotonic()))
                )
                if message.get("type") == "ready":
                    self._workers[manifest.name] = process
                    self._outputs[manifest.name] = ready_queue
                    return process
                if message.get("type") == "api":
                    try:
                        value = self._api(
                            manifest,
                            message["method"],
                            message.get("args", []),
                            message.get("kwargs", {}),
                        )
                        response = {"value": value}
                    except Exception as exc:
                        response = {"error": f"{type(exc).__name__}: {exc}"}
                    assert process.stdin
                    process.stdin.write(
                        json.dumps(response, ensure_ascii=False, default=str) + "\n"
                    )
                    process.stdin.flush()
                    continue
                raise SkillExecutionError(message.get("error", "worker failed during setup"))
            raise TimeoutError("skill setup exceeded deadline")
        except Exception:
            process.kill()
            raise

        self._workers[manifest.name] = process
        self._outputs[manifest.name] = ready_queue
        return process

    def execute(self, name: str, function: str = "run", *args: object, **kwargs: object) -> object:
        with self._lock:
            manifest = self._manifests.get(name) or self.load(name)
            proc = self._workers.get(name)
            if proc is None or proc.poll() is not None:
                proc = self._start(manifest)
            assert proc.stdin and proc.stdout
            request_id = uuid.uuid4().hex
            proc.stdin.write(
                json.dumps(
                    {"id": request_id, "action": function, "args": args, "kwargs": kwargs},
                    ensure_ascii=False,
                )
                + "\n"
            )
            proc.stdin.flush()
            deadline = time.monotonic() + self.timeout
            output = self._outputs[name]
            while time.monotonic() < deadline:
                try:
                    line = output.get(timeout=max(0.01, deadline - time.monotonic()))
                except Empty:
                    break
                if not line:
                    stderr = proc.stderr.read()[-1000:] if proc.stderr else ""
                    raise SkillExecutionError(f"worker exited: {stderr}")
                msg = json.loads(line)
                if msg.get("type") == "api":
                    try:
                        value = self._api(
                            manifest, msg["method"], msg.get("args", []), msg.get("kwargs", {})
                        )
                        response = {"value": value}
                    except Exception as exc:
                        response = {"error": f"{type(exc).__name__}: {exc}"}
                    proc.stdin.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
                    proc.stdin.flush()
                elif msg.get("type") == "result" and msg.get("id") == request_id:
                    if "error" in msg:
                        raise SkillExecutionError(msg["error"])
                    return msg.get("value")
                else:
                    raise SkillExecutionError("unexpected worker response")
            proc.kill()
            self._workers.pop(name, None)
            raise TimeoutError("skill execution exceeded deadline")

    def match(self, input_text: str) -> list[SkillManifest]:
        return [m for m in self.registry.list_installed() if m.matches(input_text)]

    def discover(self, query: str) -> list[dict[str, object]]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be nonempty text")
        if self.search_fn:
            results = self.search_fn(query, max_results=10)
        else:
            if __package__:
                from .search import search
            else:
                from search import search
            results = search(query, max_results=10)
        output = []
        for item in (results or [])[:10]:
            url = item.get("url") or item.get("href")
            if (
                not isinstance(url, str)
                or urlsplit(url).scheme != "https"
                or urlsplit(url).netloc != "github.com"
            ):
                continue
            candidate: dict[str, object] = {
                "url": url,
                "title": item.get("title"),
                "snippet": item.get("snippet") or item.get("body"),
            }
            if self.evaluator:
                repo = "https://github.com/" + "/".join(
                    urlsplit(url).path.strip("/").split("/")[:2]
                )
                try:
                    eco = self.evaluator.eco_score(repo)
                    candidate["eco_score"] = eco
                    content = None
                    if self.evaluator.should_review_content([eco]):
                        content = self.evaluator.review_content(
                            repo, str(candidate["snippet"] or "")
                        )
                        candidate["content_score"] = content
                    candidate["final_score"] = self.evaluator.final_score(eco, content)
                except Exception as exc:
                    candidate["evaluation_error"] = type(exc).__name__
            output.append(candidate)
        return output

    def install_from_url(self, url: str) -> SkillManifest:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("URL must use HTTPS")
        with urlopen(
            Request(url, headers={"User-Agent": "JARVIS-SkillLoader"}), timeout=20
        ) as response:
            blob = response.read(_MAX_ZIP + 1)
        if len(blob) > _MAX_ZIP:
            raise ValueError("zip exceeds size limit")
        with tempfile.TemporaryDirectory(prefix="jarvis-skill-") as tmp:
            zip_path = Path(tmp) / "package.zip"
            zip_path.write_bytes(blob)
            stage = Path(tmp) / "stage"
            stage.mkdir(mode=0o700)
            with zipfile.ZipFile(zip_path) as archive:
                members = archive.infolist()
                if len(members) > _MAX_MEMBERS:
                    raise ValueError("too many archive files")
                total = 0
                names = set()
                for item in members:
                    member = PurePosixPath(item.filename)
                    mode = item.external_attr >> 16
                    if member.is_absolute() or ".." in member.parts or stat.S_ISLNK(mode):
                        raise ValueError("unsafe zip path")
                    if item.file_size > _MAX_FILE:
                        raise ValueError("zip member too large")
                    total += item.file_size
                    if total > _MAX_ZIP:
                        raise ValueError("expanded zip too large")
                    names.add(member.as_posix())
                    destination = stage.joinpath(*member.parts)
                    if item.is_dir():
                        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        destination.write_bytes(archive.read(item))
                if not {"manifest.json", "skill.py"} <= names:
                    raise ValueError("zip must contain root manifest.json and skill.py")
            manifest = SkillManifest.from_dict(
                json.loads((stage / "manifest.json").read_text()), require_checksum=True
            )
            if not (stage / "skill.py").is_file() or (stage / "skill.py").is_symlink():
                raise ValueError("skill.py must be regular file")
            dest = self._directory(manifest.name, manifest.version)
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if dest.exists():
                raise FileExistsError(dest)
            shutil.copytree(stage, dest, symlinks=False)
            try:
                try:
                    old = self.registry.get(manifest.name)
                except KeyError:
                    old = None
                if old is None:
                    self.registry.install(manifest)
                else:
                    self.registry.upgrade(manifest)
            except Exception:
                shutil.rmtree(dest)
                raise
            self._manifests[manifest.name] = manifest
            return manifest

    def close(self) -> None:
        with self._lock:
            for proc in self._workers.values():
                if proc.poll() is None:
                    try:
                        assert proc.stdin
                        proc.stdin.write('{"id":"stop","action":"__shutdown__"}\n')
                        proc.stdin.flush()
                        proc.wait(timeout=1)
                    except Exception:
                        proc.kill()
            self._workers.clear()
            if self._owns_registry:
                self.registry.close()

    def __enter__(self) -> SkillLoader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
