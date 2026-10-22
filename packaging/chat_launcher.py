#!/usr/bin/env python3
"""JARVIS dedicated one-shot chat launcher; no host Hermes config/resolver.

--config is a JSON object containing exactly model, provider, base_url, api_mode,
context_length, reasoning_effort. Credentials come only from --key-file (0600,
owned regular file without symlink components) or --key-fd (including stdin 0).
--test-local-port explicitly enables a loopback fake service; use only FAKE- keys.
Data defaults to $JARVIS_HOME/sessions or ~/.jarvis/sessions; --data-dir accepts
an absolute user-owned 0700 dedicated root. The legacy chat run subtree remains
available for development. Omitted --session reuses the saved session.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import selectors
import threading
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from context_limits import CHAT_CONTEXT_BYTE_LIMIT, INNER_CONTEXT_BYTE_LIMIT
finally:
    sys.path.pop(0)

if TYPE_CHECKING:
    import chat_entry as chat_types
    import product_config as config_types

_spec = importlib.util.spec_from_file_location(
    "jarvis_launcher_chat", Path(__file__).with_name("chat_entry.py")
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Product entry missing")
chat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chat)
if TYPE_CHECKING:
    chat = chat_types

_config_spec = importlib.util.spec_from_file_location(
    "jarvis_launcher_config", Path(__file__).with_name("product_config.py")
)
if _config_spec is None or _config_spec.loader is None:
    raise RuntimeError("Product configuration missing")
product = importlib.util.module_from_spec(_config_spec)
_config_spec.loader.exec_module(product)
if TYPE_CHECKING:
    product = config_types

FIELDS = product.CONFIG_FIELDS
Report = dict[str, Any]


class LauncherError(Exception):
    """Fixed user-facing messages only; never include untrusted diagnostics."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise LauncherError("参数无效；请用 --help 查看用法，密钥不能放在参数中。")


def safe_path(path: Path) -> Path:
    path = path.absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise LauncherError("路径不能包含符号链接。")
    return path


def read_config(path: Path, test_port: int | None) -> Report:
    try:
        if not path.is_absolute():
            raise product.ConfigSecurityError("absolute config required")
        return dict(product.read_config(path, test_port=test_port).values)
    except (product.ConfigError, OSError, TypeError):
        raise LauncherError(
            "自有配置无效；须为专用 0600 配置及 0700 父目录，普通端点仅 HTTPS 443。"
        ) from None


def read_key(*, key_file: Path | None, key_fd: int | None) -> str:
    try:
        return product.read_key(key_file=key_file, key_fd=key_fd)
    except (product.ConfigError, OSError, ValueError):
        raise LauncherError("凭据无效；须为专用 0600 文件或匿名管道 FD。") from None


def binding_summary(binding: config_types.RouteBinding) -> Report:
    return {
        "binding_version": binding.version,
        "config_sha256": binding.config_sha256,
        "data_root": str(binding.data_root),
        "data_root_sha256": binding.data_root_sha256,
        "binding_sha256": binding.binding_sha256,
    }


def checked_binding(config: Report, root: Path, test_port: int | None) -> config_types.RouteBinding:
    try:
        chat.home.validate_root(root)
        return product.bind_route(config, root, test_port=test_port)
    except (product.ConfigError, OSError, ValueError, TypeError):
        raise LauncherError("配置或数据根绑定无效，未启动引擎。") from None


def prepare_root(path: Path | None) -> Path:
    try:
        return chat.home.prepare_root(path, legacy=chat.RUNS)
    except (ValueError, OSError):
        raise LauncherError(
            "数据目录须为当前用户拥有的绝对 0700 专用目录，父目录不可共享；"
            "禁止符号链接、源码树和宿主 Hermes 路径。"
        ) from None


def kill_group(process: subprocess.Popen[str]) -> None:
    """尽力收掉整组；拿不到权限时退回单进程，别让清理本身抛异常。

    killpg 要求 pid 同时是进程组号：子进程没开 start_new_session 时它属于
    父进程的组，killpg 会打到父进程组（被内核以 EPERM 拦住）。这里吞掉 EPERM
    再退化成 kill(pid)，至少不让已超时的子进程留在数据根里继续跑。
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
        return
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, value: Report) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".attempt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


UNRESOLVED_STATES = ("started", "unknown")


def unresolved_attempts(root: Path) -> list[dict[str, Any]]:
    """数据根上结果未知的调用：既不能重发，也不该被静默忘掉。"""
    directory = root / "launcher-attempts"
    if not directory.is_dir():
        return []
    pending: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            pending.append({"attempt_id": path.stem, "state": "unreadable", "path": str(path)})
            continue
        if isinstance(record, dict) and record.get("state") in UNRESOLVED_STATES:
            pending.append(
                {
                    "attempt_id": record.get("attempt_id", path.stem),
                    "state": record.get("state"),
                    "path": str(path),
                }
            )
    return pending


def resolve_attempt(root: Path, attempt_id: str, note: str) -> Path:
    """人工核对后标记已处理，解除新发送的阻塞。不重发、不伪造结果。"""
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", attempt_id or ""):
        raise LauncherError("attempt ID 无效。")
    path = attempt_path(root, attempt_id)
    try:
        record = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        raise LauncherError("占位记录不可读，无法标记。") from None
    if not isinstance(record, dict):
        raise LauncherError("占位记录格式无效。")
    record["state"] = "resolved"
    record["resolution"] = {"note": note[:500], "at": time.time()}
    atomic_json(path, record)
    return path


def attempt_path(root: Path, attempt_id: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", attempt_id):
        raise LauncherError("attempt ID 无效。")
    return safe_path(root / "launcher-attempts" / (attempt_id + ".json"))


def query_attempt(root: Path, attempt_id: str) -> Report:
    path = attempt_path(root, attempt_id)
    try:
        if path.stat().st_size > 262144:
            raise ValueError("oversize")
        value: object = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("invalid")
        record = cast(Report, value)
        if record.get("attempt_id") != attempt_id:
            raise ValueError("identity")
        return record
    except Exception:
        raise LauncherError("占位记录不可读；结果未知，禁止重发此 ID。") from None


def timeline_identity(root: Path) -> str:
    """Return the stable technical context segment backing the public timeline."""
    path = safe_path(root / "timeline.json")
    if path.exists():
        value = json.loads(path.read_text())
        identity = cast(Report, value).get("session_id") if isinstance(value, dict) else None
        if isinstance(identity, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identity):
            return identity
        raise LauncherError("时间线标识记录不可读。")
    identity = "jarvis-timeline-" + uuid.uuid4().hex
    atomic_json(path, {"version": 1, "session_id": identity})
    legacy = safe_path(root / "launcher-session.json")
    if legacy.exists():
        try:
            old = json.loads(legacy.read_text())
            old_id = cast(Report, old).get("session_id") if isinstance(old, dict) else None
        except (OSError, ValueError, TypeError):
            old_id = None
        if isinstance(old_id, str):
            atomic_json(
                safe_path(root / "timeline-migration.json"),
                {
                    "version": 1,
                    "from_session_id": old_id,
                    "to_timeline_id": identity,
                    "status": "read-only-compatible",
                },
            )
    return identity


def _row_channel(raw: object, session_id: object, timeline_id: str) -> str:
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            value = None
        if isinstance(value, dict):
            channel = cast(Report, value).get("source_channel")
            if isinstance(channel, str) and channel in {
                "web",
                "voice",
                "email",
                "imessage",
                "jarvis_proactive",
            }:
                return channel
    return "web" if session_id == timeline_id else "legacy"


def _outreach_metadata(raw: object) -> Report:
    if not isinstance(raw, str):
        return {}
    try:
        metadata = cast(Report, json.loads(raw))
        if metadata.get("source_channel") != "jarvis_proactive":
            return {}
        return {
            key: metadata[key]
            for key in (
                "intent_id",
                "reason",
                "evidence",
                "urgency",
                "needs_response",
                "generation_degraded",
                "outreach_delivery_key",
                "delivery_channel",
            )
            if key in metadata
        }
    except (ValueError, TypeError, AttributeError):
        return {}


def read_timeline(root: Path) -> list[dict[str, Any]]:
    """Read legacy and current transcript rows in insertion/time order, without rewriting them."""
    import sqlite3

    database = root / "engine-home/state.db"
    if not database.exists():
        return []
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        required = {"id", "session_id", "role", "content", "timestamp"}
        if not required <= columns:
            return []
        metadata = "display_metadata" if "display_metadata" in columns else "NULL"
        rows = db.execute(
            "SELECT id,session_id,role,content,timestamp," + metadata + " FROM messages "
            "WHERE role IN ('user','assistant') "
            "AND session_id NOT LIKE 'task-%' AND session_id NOT LIKE 'inner-%' "
            "ORDER BY timestamp,id"
        ).fetchall()
    timeline_id = timeline_identity(root)
    return [
        {
            "id": row[0],
            "role": row[2],
            "content": row[3],
            "timestamp": row[4],
            "source_channel": _row_channel(row[5], row[1], timeline_id),
            **_outreach_metadata(row[5]),
        }
        for row in rows
    ]


def launch(
    config: Report,
    key: str,
    prompt: str,
    root: Path,
    session: str | None,
    *,
    test_port: int | None = None,
    timeout: float | None = None,
    attempt_id: str | None = None,
    expected_sha256: str | None = None,
    expected_binding_sha256: str | None = None,
    persona_mode: str = "chat",
    source_channel: str = "web",
    lock_wait_timeout: float = 0.0,
    after_unknown: str = "refuse",
) -> Report:
    """Local at-most-one launch per ID; unknown outcomes require reconciliation.

    Each deliberate new prompt gets a new UUID, including identical text. To
    re-enter/query one attempt, retain its ID and data directory. This flock is
    launcher-local protection, not an OS sandbox or a K0 execution protocol.
    lock_wait_timeout only waits before reservation, never retries a model call.
    """
    if (
        type(lock_wait_timeout) not in (int, float)
        or not math.isfinite(lock_wait_timeout)
        or not 0 <= lock_wait_timeout <= 120
    ):
        raise LauncherError("数据根锁等待时间必须为 0 至 120 秒的有限数值。")
    if persona_mode not in {"chat", "inner"}:
        raise LauncherError("人格模式无效。")
    if key in str(root) or key in prompt or (session is not None and key in session):
        raise LauncherError("凭据不得出现在提示词、目录或会话 ID 中。")
    if source_channel not in {"web", "voice", "email", "imessage"}:
        raise LauncherError("来源通道无效。")
    if after_unknown not in {"refuse", "continue"}:
        raise LauncherError("after_unknown 只接受 refuse 或 continue。")
    if key in json.dumps(config, ensure_ascii=False):
        raise LauncherError("凭据不得出现在非敏感模型配置中。")
    if chat.home.overlap(root, chat.home.PROJECT):
        raise LauncherError("正常产品发送禁止使用源码或历史 RUNS 数据根。")
    root = prepare_root(root)
    binding = checked_binding(config, root, test_port)
    config = dict(binding.values)
    if timeout is None:
        timeout = chat.launcher_timeout_seconds(config["reasoning_effort"])
    summary = binding_summary(binding)
    if (expected_sha256 is not None and expected_sha256 != binding.config_sha256) or (
        expected_binding_sha256 is not None and expected_binding_sha256 != binding.binding_sha256
    ):
        raise LauncherError("配置或数据根绑定冲突，未启动引擎。")
    pending_unknown = unresolved_attempts(root) if attempt_id is None else []
    if pending_unknown and after_unknown == "continue":
        _LOG.warning(
            "launch_after_unknown_continue root=%s pending=%d ids=%s",
            root, len(pending_unknown),
            ",".join(item["attempt_id"] for item in pending_unknown[:5]),
        )
    if attempt_id is None and after_unknown == "refuse":
        pending = pending_unknown
        if pending:
            ids = "、".join(item["attempt_id"] for item in pending[:5])
            raise LauncherError(
                f"数据根有 {len(pending)} 个结果未知的调用（{ids}）；"
                "先核对并 resolve-attempt，或显式声明 after_unknown='continue'。"
            )
    identity = attempt_id if attempt_id is not None else uuid.uuid4().hex
    if key in identity:
        raise LauncherError("凭据不得出现在 attempt ID 中。")
    path = attempt_path(root, identity)
    directory = path.parent
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise LauncherError("占位目录须为当前用户拥有的 0700 目录。")
    sync_directory(root)
    lock_fd: int | None = None
    try:
        lock_fd = os.open(
            safe_path(root / "launcher-session.lock"),
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        # Hold the same root lock used by migration before publishing started.
        deadline = time.monotonic() + lock_wait_timeout
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                # Only lock contention is retryable, before any attempt or child.
                time.sleep(min(0.05, remaining))
    except BaseException:
        if lock_fd is not None:
            os.close(lock_fd)
        raise LauncherError("已有模型请求或迁移占用数据根，未启动引擎。") from None
    try:
        # Waiting may span permission or model migrations. Revalidate under lock;
        # the existing _launch_once engine pin check still runs before dispatch.
        binding = checked_binding(config, root, test_port)
        summary = binding_summary(binding)
    except BaseException:
        os.close(lock_fd)
        raise
    # O_EXCL is the authority: existence of even partial/corrupt data blocks reuse.
    record: Report = {
        "attempt_id": identity,
        "state": "started",
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "session_sha256": hashlib.sha256((session or "saved-session").encode()).hexdigest(),
        "source_channel": source_channel,
        **summary,
    }
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        try:
            previous = query_attempt(root, identity)
            if any(previous.get(name) != value for name, value in summary.items()):
                raise LauncherError(
                    "配置或数据根绑定冲突，此 attempt 禁止重发；未启动引擎。"
                ) from None
            raise LauncherError(
                f"此 attempt 已占位；结果查询：--query-attempt {identity} "
                f"--data-dir {root}。started/unknown 均为结果未知；新发送须选择新 attempt。"
            ) from None
        finally:
            os.close(lock_fd)
    except OSError:
        os.close(lock_fd)
        raise LauncherError("占位创建失败，未启动引擎；保留该 ID 禁止重发。") from None
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(directory)
    except BaseException:
        os.close(lock_fd)
        raise LauncherError("占位持久化失败，未启动引擎；保留该 ID 禁止重发。") from None
    try:
        # Entire saved-session read / request / persistence is serialized.
        if session is None:
            saved = safe_path(root / "launcher-session.json")
            if saved.exists():
                if saved.stat().st_size > 4096:
                    raise ValueError("oversize session")
                selected = json.loads(saved.read_text()).get("session_id")
                if not isinstance(selected, str):
                    raise ValueError("invalid session")
                session = selected
        record["session_sha256"] = hashlib.sha256((session or "new-session").encode()).hexdigest()
        # A second durable checkpoint covers metadata and state-write failure.
        atomic_json(path, record)
        if persona_mode == "inner":
            result = _launch_once(
                config,
                key,
                prompt,
                root,
                session,
                test_port=test_port,
                timeout=timeout,
                persona_mode="inner",
                **({"source_channel": source_channel} if source_channel != "web" else {}),
            )
        else:
            result = _launch_once(
                config,
                key,
                prompt,
                root,
                session,
                test_port=test_port,
                timeout=timeout,
                **({"source_channel": source_channel} if source_channel != "web" else {}),
            )
        result.update(summary)
        result["attempt_id"] = identity
        result["attempt_path"] = str(path)
        record["state"] = "completed"
        record["result"] = result
        atomic_json(path, record)
        return result
    except BaseException as exc:
        record.pop("result", None)
        record["state"] = "unknown"
        try:
            atomic_json(path, record)
        except BaseException:
            pass  # Existing started or completed checkpoint still refuses resend.
        reason = str(exc) if isinstance(exc, LauncherError) else "引擎或本地保存异常。"
        raise LauncherError(
            f"{reason} 结果未知，禁止自动重发；attempt={identity}；记录={path}。"
        ) from None
    finally:
        os.close(lock_fd)


_LOG = logging.getLogger(__name__)
_PRODUCT_ENTRY = Path(__file__).with_name("chat_entry.py")
_PRODUCT_PYTHON = chat.engine.PYTHON
_WARM_POOL: dict[tuple[str, str], _WarmChild] = {}
_POOL_LOCK = threading.RLock()


class WarmUnavailable(Exception):
    """Startup failed before request dispatch; cold fallback may be safe."""


class _WarmChild:
    def __init__(self, command: list[str], blob: bytes, root: Path, key: str, timeout: float):
        read_fd, write_fd = os.pipe()
        self.process: subprocess.Popen[str] | None = None
        self.buffer = bytearray()
        try:
            os.write(write_fd, blob)
            os.close(write_fd)
            write_fd = -1
            command = list(command)
            command[command.index("--credentials-fd") + 1] = str(read_fd)
            self.process = subprocess.Popen(
                command + ["--serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                env=chat.engine.environment(root), cwd=root, pass_fds=(read_fd,),
                start_new_session=True,
            )
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
            ready = self.exchange(None, min(timeout, 90), key)
            if ready != {"ready": True, "version": chat.WARM_PROTOCOL_VERSION}:
                _LOG.warning("warm_engine_ready_invalid %s", ready.get("error_type", "invalid") if isinstance(ready, dict) else type(ready).__name__)
                raise WarmUnavailable()
        except BaseException:
            self.close()
            raise
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def close(self) -> None:
        process = self.process
        if process is not None:
            kill_group(process)
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def exchange(self, payload: bytes | None, timeout: float, key: str) -> Report:
        process = self.process
        assert process is not None and process.stdin and process.stdout and process.stderr
        deadline = time.monotonic() + timeout
        stderr = bytearray()
        offset = 0
        with selectors.DefaultSelector() as selected:
            selected.register(process.stdout, selectors.EVENT_READ, "out")
            selected.register(process.stderr, selectors.EVENT_READ, "err")
            if payload is not None:
                selected.register(process.stdin, selectors.EVENT_WRITE, "in")
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LauncherError("对话超时；热引擎进程组已终止。")
                events = selected.select(remaining)
                if not events:
                    raise LauncherError("对话超时；热引擎进程组已终止。")
                for event, _ in events:
                    if event.data == "in":
                        assert payload is not None
                        offset += os.write(process.stdin.fileno(), payload[offset:offset + 8192])
                        if offset == len(payload):
                            selected.unregister(process.stdin)
                        continue
                    chunk = os.read(event.fd, 8192)
                    if not chunk:
                        if event.data == "out":
                            raise LauncherError("热引擎已退出，结果未知，禁止自动重发。")
                        selected.unregister(event.fileobj)
                        continue
                    target = self.buffer if event.data == "out" else stderr
                    target.extend(chunk)
                    if len(target) > 262144 or key.encode() in target:
                        raise LauncherError("引擎输出不安全，已抑制输出。")
                if b"\n" in self.buffer:
                    line, _, extra = self.buffer.partition(b"\n")
                    self.buffer.clear()
                    if extra or stderr:
                        raise LauncherError("热引擎协议输出无效，诊断已抑制。")
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeError):
                        raise LauncherError("热引擎协议输出无效，诊断已抑制。") from None
                    if not isinstance(value, dict):
                        raise LauncherError("热引擎回包必须为对象。")
                    return cast(Report, value)


def close_warm_engines() -> None:
    with _POOL_LOCK:
        children = list(_WARM_POOL.values())
        _WARM_POOL.clear()
    for child in children:
        child.close()


atexit.register(close_warm_engines)


def _warm_exchange(
    command: list[str], blob: bytes, binding: config_types.RouteBinding,
    root: Path, key: str, request: Report, timeout: float,
) -> str:
    pool_key = (str(root), binding.binding_sha256)
    # A credential rotation also retires the child without sending a second secret
    # envelope to a running engine. The digest is memory-only, never logged.
    fingerprint = hashlib.sha256(key.encode()).digest()
    with _POOL_LOCK:
        for identity, previous in list(_WARM_POOL.items()):
            if identity[0] == str(root) and (
                identity != pool_key or getattr(previous, "fingerprint", None) != fingerprint
            ):
                previous.close()
                del _WARM_POOL[identity]
        child = _WARM_POOL.get(pool_key)
        if child is not None and child.process is not None and child.process.poll() is not None:
            child.close()
            del _WARM_POOL[pool_key]
            child = None
        if child is None:
            try:
                child = _WarmChild(command, blob, root, key, timeout)
            except Exception as exc:
                _LOG.warning("warm_engine_start_failed cold_fallback before_dispatch %s", type(exc).__name__)
                raise WarmUnavailable() from None
            child.fingerprint = fingerprint
            _WARM_POOL[pool_key] = child
    identity = uuid.uuid4().hex
    payload = (json.dumps({"version": chat.WARM_PROTOCOL_VERSION, "id": identity,
                           "request": request}, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        if len(payload) > chat.MAX_BYTES:
            # A valid legacy payload may not fit the additional warm envelope.
            # No bytes have been sent, so the unchanged cold path is safe.
            raise WarmUnavailable()
        frame = child.exchange(payload, timeout, key)
        if set(frame) != {"version", "id", "result"} or (
            frame["version"] != chat.WARM_PROTOCOL_VERSION or frame["id"] != identity
        ) or not isinstance(frame["result"], dict):
            raise LauncherError("热引擎回包无效；结果未知，禁止自动重发。")
        return json.dumps(frame["result"], ensure_ascii=False)
    except BaseException:
        with _POOL_LOCK:
            _WARM_POOL.pop(pool_key, None)
        child.close()
        _LOG.warning("warm_engine_turn_failed unknown_not_replayed next_attempt_cold")
        raise


def _launch_once(
    config: Report,
    key: str,
    prompt: str,
    root: Path,
    session: str | None,
    *,
    test_port: int | None = None,
    timeout: float | None = None,
    persona_mode: str = "chat",
    source_channel: str = "web",
) -> Report:
    if persona_mode not in {"chat", "inner"}:
        raise LauncherError("人格模式无效。")
    prompt_limit = INNER_CONTEXT_BYTE_LIMIT if persona_mode == "inner" else CHAT_CONTEXT_BYTE_LIMIT
    if not prompt.strip() or len(prompt.encode("utf-8")) > prompt_limit:
        raise LauncherError(f"请输入不超过 {prompt_limit} UTF-8 字节的普通文本。")
    if key in prompt or key in str(root) or (session is not None and key in session):
        raise LauncherError("凭据不得出现在提示词、目录或会话 ID 中。")
    if source_channel not in {"web", "voice", "email", "imessage"}:
        raise LauncherError("来源通道无效。")
    if key in json.dumps(config, ensure_ascii=False):
        raise LauncherError("凭据不得出现在非敏感模型配置中。")
    if test_port is not None and not key.startswith("FAKE-"):
        raise LauncherError("模拟服务模式仅接受明确标注 FAKE- 的凭据。")
    request: Report = {"prompt": prompt}
    if persona_mode == "inner":
        request["persona_mode"] = "inner"
    if source_channel != "web":
        request["source_channel"] = source_channel
    session_path = safe_path(root / "launcher-session.json")
    if session is None and session_path.exists():
        if session_path.stat().st_size > 4096:
            raise LauncherError("保存的会话记录超过大小限制。")
        saved = json.loads(session_path.read_text())
        session = saved.get("session_id")
        if not isinstance(session, str):
            raise LauncherError("保存的会话记录无效。")

    if session is not None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", session):
            raise LauncherError("会话 ID 无效。")
        request["session_id"] = session
    binding = checked_binding(config, root, test_port)
    engine_config = safe_path(root / "engine-home/config.yaml")
    expected_engine = json.dumps(
        {
            "model": {
                "default": config["model"],
                "provider": config["provider"],
                "base_url": config["base_url"],
                "context_length": config["context_length"],
            },
            "compression": {"enabled": False},
        }
    )
    if engine_config.exists() and (
        engine_config.stat().st_size > 65536 or engine_config.read_text() != expected_engine
    ):
        raise LauncherError("配置与已有数据根冲突，未启动引擎。")
    route = {**dict(binding.values), "api_key": key}
    try:
        chat.validate_route(route, test_port=test_port)
    except (ValueError, TypeError):
        raise LauncherError("模型路由无效。") from None
    blob = json.dumps({"route": route, "binding": binding_summary(binding)}).encode()
    if len(blob) > 4096:
        raise LauncherError("凭据管道载荷超过原子写入限制。")
    read_fd, write_fd = os.pipe()
    process: subprocess.Popen[str] | None = None
    command = [
        str(chat.engine.PYTHON),
        "-I",
        "-S",
        "-B",
        "-c",
        CHILD_BOOTSTRAP,
        str(Path(__file__).with_name("chat_entry.py")),
        "--credentials-fd",
        str(read_fd),
        "--data-dir",
        str(root),
    ]
    for protected in chat.home.protected_paths(root):
        command.extend(["--protected-host", str(protected)])
    if test_port is not None:
        command.extend(["--test-local-port", str(test_port)])
    try:
        deadline = (chat.launcher_timeout_seconds(config["reasoning_effort"])
                    if timeout is None else timeout)
        stdout: str | None = None
        stderr = ""
        if Path(__file__).with_name("chat_entry.py") == _PRODUCT_ENTRY and (
            chat.engine.PYTHON == _PRODUCT_PYTHON
        ):
            try:
                stdout = _warm_exchange(command, blob, binding, root, key, request, deadline)
            except WarmUnavailable:
                _LOG.warning("warm_engine_unavailable cold_fallback before_dispatch")
        if stdout is None:
            os.write(write_fd, blob)
            os.close(write_fd)
            write_fd = -1
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", env=chat.engine.environment(root), cwd=root,
                pass_fds=(read_fd,), start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(
                    json.dumps(request, ensure_ascii=False), timeout=deadline
                )
            except subprocess.TimeoutExpired:
                kill_group(process)
                process.communicate(timeout=5)
                raise LauncherError("对话超时；引擎进程组已终止。") from None
        # Never forward arbitrary subprocess stdout/stderr or provider exceptions.
        if len(stdout.encode()) > 262144 or key in stdout or key in stderr:
            raise LauncherError("引擎输出不安全，已抑制输出。")
        try:
            raw_result: object = json.loads(stdout)
        except (ValueError, TypeError):
            raise LauncherError("引擎未返回有效 JSON；诊断内容已抑制。") from None
        if not isinstance(raw_result, dict):
            raise LauncherError("引擎回包必须为 JSON 对象。")
        result = cast(Report, raw_result)
        if (process is not None and process.returncode != 0) or result.get("ok") is not True or result.get("error_type"):
            raise LauncherError("引擎对话失败；诊断内容已抑制。")
        text, identity = result.get("text"), result.get("session_id")
        if (
            not isinstance(text, str)
            or not text
            or key in text
            or not isinstance(identity, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identity)
            or (session is not None and session != identity)
            or key in json.dumps(result, ensure_ascii=False)
        ):
            raise LauncherError("引擎回包字段无效或包含凭据，已抑制输出。")
        # Only a safe session ID is persisted; no key, prompt, or diagnostics.
        fd, temporary = tempfile.mkstemp(prefix=".session-", dir=root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"session_id": identity}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, session_path)
            sync_directory(root)
        finally:
            Path(temporary).unlink(missing_ok=True)
        public = {
            "ok": True,
            "text": text,
            "session_id": identity,
            "data_dir": str(root),
            "mode": "explicit fake HTTP" if test_port else "provider",
            "memory_extraction": result.get("memory_extraction", "unknown"),
            "generation_degraded": result.get("generation_degraded", False),
            "model_request_state": result.get("model_request_state", "unknown"),
            **{
                key: result[key]
                for key in (
                    "provider_status",
                    "provider_error_type",
                    "provider_error_code",
                )
                if key in result
            },
            "timings_ms": result.get("timings_ms") if isinstance(result.get("timings_ms"), dict) else {},
            "memory_saved_ids": result.get("memory_saved_ids", []),
            "memory_sources": result.get("memory_sources", []),
        }
        if key in json.dumps(public, ensure_ascii=False):
            raise LauncherError("公开回包包含凭据，已抑制输出。")
        return public
    finally:
        if process is not None:
            kill_group(process)
            process.wait(timeout=5)
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


# Fixed bootstrap validates the snapshot in the SAME isolated product child.
# The config file is never reread; only validated values cross the anonymous FD.
CHILD_BOOTSTRAP = r"""
import importlib.util,json,os,runpy,sys
from pathlib import Path
entry=Path(sys.argv[1]); args=sys.argv[2:]
fd=int(args[args.index('--credentials-fd')+1])
root=Path(args[args.index('--data-dir')+1])
port=int(args[args.index('--test-local-port')+1]) if '--test-local-port' in args else None
with os.fdopen(fd) as stream:
    envelope=json.loads(stream.read(4097))
spec=importlib.util.spec_from_file_location('jarvis_verified_config',CONFIG_MODULE)
assert spec and spec.loader
pc=importlib.util.module_from_spec(spec);spec.loader.exec_module(pc)
route=envelope['route']; summary=envelope['binding']
values=pc.validate_config({k:v for k,v in route.items() if k!='api_key'},test_port=port)
config_sha=pc.sha256_config(values); data_sha=pc.sha256_text(str(root))
binding_sha=pc.sha256_text(json.dumps({
    'version':pc.BINDING_VERSION,'config_sha256':config_sha,'data_root_sha256':data_sha
},sort_keys=True,separators=(',',':')))
if summary!={
    'binding_version':pc.BINDING_VERSION,'config_sha256':config_sha,'data_root':str(root),
    'data_root_sha256':data_sha,'binding_sha256':binding_sha
}:
    raise ValueError('binding mismatch')
read,write=os.pipe()
os.write(write,json.dumps(route).encode());os.close(write)
args[args.index('--credentials-fd')+1]=str(read)
sys.argv=[str(entry),*args]
runpy.run_path(str(entry),run_name='__main__')
""".replace("CONFIG_MODULE", repr(str(Path(__file__).with_name("product_config.py"))))


def main() -> int:
    parser = Parser(description=__doc__)
    parser.add_argument("--config", type=Path)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--key-file", type=Path)
    source.add_argument("--key-fd", type=int)
    parser.add_argument("--prompt")
    parser.add_argument(
        "--attempt-id", help="Explicit stable ID for re-entry; omission means new send"
    )
    parser.add_argument("--query-attempt", help="Read saved result without starting an engine")
    parser.add_argument("--session")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--test-local-port", type=int)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--expected-binding-sha256")
    try:
        args = parser.parse_args()
        if args.query_attempt:
            if args.data_dir is None:
                raise LauncherError("查询须明确指定已有数据根。")
            result = query_attempt(chat.home.validate_root(args.data_dir), args.query_attempt)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.config is None or args.prompt is None:
            raise LauncherError("发送须指定配置、凭据和提示词；省略 attempt ID 表示主动新发送。")
        config = read_config(args.config, args.test_local_port)
        key = read_key(key_file=args.key_file, key_fd=args.key_fd)
        root = prepare_root(args.data_dir)
        result = launch(
            config,
            key,
            args.prompt,
            root,
            args.session,
            test_port=args.test_local_port,
            attempt_id=args.attempt_id,
            expected_sha256=args.expected_sha256,
            expected_binding_sha256=args.expected_binding_sha256,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except LauncherError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
    except KeyboardInterrupt:
        print('{"ok": false, "error": "对话已中止，引擎进程组已清理。"}', file=sys.stderr)
    except Exception:
        print(
            '{"ok": false, "error": "启动失败；请检查配置、凭据文件及独立引擎。"}', file=sys.stderr
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


def self_evaluate():
    """Inspect launcher policy constants without resolving keys or spawning."""
    from datetime import datetime

    checks = {
        "chat_input_budget": CHAT_CONTEXT_BYTE_LIMIT,
        "inner_input_budget": INNER_CONTEXT_BYTE_LIMIT,
        "credential_sources_explicit": callable(read_key) and callable(read_config),
        "no_spawn_during_evaluation": callable(launch),
    }
    return {
        "module": "chat_launcher",
        "domain": "secure isolated product chat launch and credential handoff",
        "current_version": "minimal fixed config and key sources with supervised child launch",
        "known_limitations": [
            "Evaluation does not validate real credentials, configs or child-process availability."
        ],
        "improvement_directions": [
            "Report launch readiness from explicit operator checks without reading secrets."
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "ok": checks["chat_input_budget"] > 0
        and checks["inner_input_budget"] > 0
        and checks["credential_sources_explicit"],
    }
