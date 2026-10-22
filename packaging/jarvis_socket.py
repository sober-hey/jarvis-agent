"""Private Unix-socket ingress and read-only mind queries for the resident daemon.

Wire format: one JSON object per connection (optionally newline terminated), then
one newline-terminated JSON response. Chat waits at most 30 seconds; a timeout
never cancels/retries an admitted request. Wiring processes durable chat in its
existing scheduler thread, not in the socket thread or a second model session.
Importing this module does not bind sockets or create databases.
"""

from __future__ import annotations

import errno
import json
import math
import os
import socket
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

UTC = timezone.utc
from pathlib import Path
from typing import Any, Optional, Union

from stream_response import StreamChunk

StreamHandler = Callable[[str, str, Callable[[StreamChunk], None]], str]

DEFAULT_SOCKET = Path("/tmp/jarvis.sock")
DEFAULT_DB = Path.home() / ".jarvis/loop-data/mind.db"
QUERIES = (
    "24thoughts",
    "status",
    "beliefs",
    "location",
    "health",
    "actions",
    "goals",
    "sessions",
    "evolution",
    "tools",
    "skills",
    "memory",
)
QUERY_ALIASES = {"goal": "goals"}
MAX_REQUEST_BYTES = 65536
MAX_CHAT_BYTES = 16000


class JarvisSocketServer:
    """Bounded local server. Call start(), and stop() when its owner shuts down.

    The daemon must call process_pending() from its own model/scheduler thread.
    chat_handler receives (text, stable_request_id), and must return real text.
    Each request is marked dispatched before that callback: uncertain outcomes
    stay uncertain across restart instead of being automatically submitted again.
    """

    def __init__(
        self,
        wiring: Any = None,
        *,
        db_path: Optional[Union[Path, str]] = None,
        inbox_path: Optional[Union[Path, str]] = None,
        socket_path: Union[Path, str] = DEFAULT_SOCKET,
        chat_handler: Optional[Callable[[str, str], str]] = None,
        stream_handler: Optional[StreamHandler] = None,
        stream_config: Optional[dict[str, Any]] = None,
        stream_key_file: Optional[Union[Path, str]] = None,
        notify: Optional[Callable[[], None]] = None,
        chat_timeout: float = 30,
        max_clients: int = 16,
    ) -> None:
        if not math.isfinite(chat_timeout) or not 0 < chat_timeout <= 30 or max_clients < 1:
            raise ValueError("chat_timeout must be in (0,30]; max_clients must be positive")
        self.db_path = Path(db_path or (wiring.mind.store.path if wiring else DEFAULT_DB))
        self.inbox_path = Path(
            inbox_path or (wiring.path if wiring else self.db_path.with_name("daemon.db"))
        )
        self.socket_path = Path(socket_path)
        self.chat_handler = chat_handler
        self.stream_handler = stream_handler
        if (stream_config is None) != (stream_key_file is None):
            raise ValueError("stream config and key file must be supplied together")
        if self.stream_handler is None and stream_config is not None:
            # Server-owned route only; clients cannot supply an endpoint or key path.
            route = dict(stream_config)
            key_path = str(stream_key_file)

            def provider_stream(
                text: str, identity: str, callback: Callable[[StreamChunk], None]
            ) -> str:
                from chat_entry import stream_chat

                return stream_chat(text, route, key_path, callback)

            self.stream_handler = provider_stream
        self.notify = notify
        self.chat_timeout = chat_timeout
        self.started_at = time.monotonic()
        self.thread: Optional[threading.Thread] = None
        self._socket: Optional[socket.socket] = None
        self._identity: Optional[tuple[int, int]] = None
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(max_clients)
        self._processing = threading.Lock()
        self._lifecycle = threading.Lock()

    @contextmanager
    def _inbox(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.inbox_path, timeout=1)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _prepare_inbox(self) -> None:
        # Never use the mind DB as a queue, nor replace an existing inbox schema.
        if self.inbox_path == self.db_path:
            raise ValueError("inbox and mind databases must be separate")
        fd = os.open(self.inbox_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        if stat.S_IMODE(self.inbox_path.stat().st_mode) != 0o600:
            raise ValueError("inbox must have mode 0600")
        with self._inbox() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inbox(
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, delivered INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS socket_replies(
                    request_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                    reply TEXT, error TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS socket_stream_chunks(
                    request_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL, done INTEGER NOT NULL, error TEXT,
                    PRIMARY KEY(request_id,chunk_index));
            """)

    def _remove_stale(self) -> None:
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError("refusing to replace a non-owned or non-socket path")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            try:
                probe.connect(str(self.socket_path))
            except OSError as exc:
                if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                    raise
            else:
                raise OSError(errno.EADDRINUSE, "JARVIS socket is already active")
        current = self.socket_path.lstat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError("socket path changed while checking liveness")
        self.socket_path.unlink()

    def start(self) -> JarvisSocketServer:
        with self._lifecycle:
            if self.thread and self.thread.is_alive():
                return self
            self._prepare_inbox()
            self._remove_stale()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                # Bind under an unguessable name in the same directory, chmod
                # before publication, then link without overwriting another owner.
                temporary = self.socket_path.with_name(".jv-" + uuid.uuid4().hex[:12])
                listener.bind(str(temporary))
                try:
                    temporary.chmod(0o600)
                    listener.listen(16)
                    os.link(temporary, self.socket_path, follow_symlinks=False)
                finally:
                    temporary.unlink(missing_ok=True)
                info = self.socket_path.lstat()
                self._identity = info.st_dev, info.st_ino
                listener.settimeout(0.2)
            except BaseException:
                listener.close()
                raise
            self._socket = listener
            self._stop.clear()
            self.thread = threading.Thread(target=self._serve, name="jarvis-socket", daemon=True)
            self.thread.start()
        return self

    def _unlink_owned(self) -> None:
        try:
            info = self.socket_path.lstat()
            if self._identity == (info.st_dev, info.st_ino):
                self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def stop(self) -> None:
        with self._lifecycle:
            self._stop.set()
            if self._socket:
                self._socket.close()
            if self.thread and self.thread is not threading.current_thread():
                self.thread.join(timeout=2)
            self._unlink_owned()
            self._socket = None

    def _serve(self) -> None:
        listener = self._socket
        assert listener is not None
        while not self._stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                connection.close()
                continue
            threading.Thread(
                target=self._client, args=(connection,), name="jarvis-socket-client", daemon=True
            ).start()

    @staticmethod
    def _read_request(connection: socket.socket) -> dict[str, Any]:
        deadline = time.monotonic() + 5
        data = bytearray()
        while len(data) <= MAX_REQUEST_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("request_timeout")
            connection.settimeout(remaining)
            chunk = connection.recv(min(8192, MAX_REQUEST_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_REQUEST_BYTES:
                raise ValueError("request_too_large")
            framed = b"\n" in data
            raw = bytes(data).split(b"\n", 1)[0] if framed else bytes(data)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                if framed:
                    raise ValueError("invalid_json") from None
                continue
            if not isinstance(value, dict):
                raise ValueError("request_must_be_object")
            return value
        raise ValueError("invalid_json")

    def _client(self, connection: socket.socket) -> None:
        try:
            with connection:
                try:
                    request = self._read_request(connection)
                    if request.get("type") == "stream_chat":
                        for chunk in self._stream_request(request):
                            connection.settimeout(5)
                            connection.sendall(
                                (json.dumps(chunk, ensure_ascii=False) + "\n").encode("utf-8")
                            )
                        return
                    result = self.handle_request(request)
                except (ValueError, UnicodeError, TimeoutError):
                    result = {"error": "invalid_request"}
                except (sqlite3.Error, OSError):
                    result = {"error": "storage_unavailable"}
                except Exception:
                    result = {"error": "internal_error"}
                connection.settimeout(5)
                connection.sendall((json.dumps(result, ensure_ascii=False) + "\n").encode())
        except OSError:
            pass  # Caller disconnected; admitted chat is not retried or cancelled.
        finally:
            self._slots.release()

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        kind = request.get("type")
        if isinstance(kind, str) and (kind in QUERIES or kind in QUERY_ALIASES):
            return self.query(QUERY_ALIASES.get(kind, kind))
        if kind == "query":
            name = request.get("query")
            if isinstance(name, str):
                name = QUERY_ALIASES.get(name, name)
            if not isinstance(name, str) or name not in QUERIES:
                return {"error": "unknown_query"}
            return self.query(name)
        if kind == "chat":
            text = request.get("text")
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text.encode("utf-8")) > MAX_CHAT_BYTES
            ):
                return {"error": "invalid_text"}
            return self._chat(text)
        return {"error": "unknown_type"}

    @staticmethod
    def _read_rows(
        path: Path, sql: str, params: tuple[Any, ...] = ()
    ) -> tuple[list[dict[str, Any]], str]:
        """Read existing stores only; never run writer constructors/migrations."""
        if not path.is_file():
            return [], "missing"
        try:
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                return [dict(row) for row in db.execute(sql, params)], "available"
            finally:
                db.close()
        except (sqlite3.Error, OSError):
            return [], "unavailable"

    @staticmethod
    def _decode_fields(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
        for row in rows:
            for field in fields:
                if isinstance(row.get(field), str):
                    try:
                        row[field] = json.loads(row[field])
                    except ValueError:
                        pass  # A malformed optional field must not hide other records.
        return rows

    @staticmethod
    def _listing(rows: list[dict[str, Any]], status: str, empty_message: str) -> dict[str, Any]:
        message = "" if rows else empty_message
        if status == "unavailable":
            message = "数据暂时不可读取，请稍后再试。"
        return {"entries": rows, "status": status, "message": message}

    def _query_goals(self) -> dict[str, Any]:
        rows, status = self._read_rows(
            self.db_path.parent / "goals.db",
            "SELECT * FROM goals WHERE completed=0 ORDER BY created_at DESC,goal_id DESC LIMIT 50",
        )
        self._decode_fields(rows, ("success_criteria", "notes"))
        return self._listing(rows, status, "暂无活跃目标；目标记录尚未建立或当前没有进行中的目标。")

    def _query_sessions(self) -> dict[str, Any]:
        directory = self.db_path.parent / "sessions"
        if not directory.is_dir():
            return self._listing([], "missing", "暂无可恢复的会话检查点。")
        rows = []
        skipped = 0
        try:
            paths = list(directory.glob("*.json"))
        except OSError:
            return self._listing([], "unavailable", "暂无可恢复的会话检查点。")
        for path in paths:
            try:
                # Do not follow checkpoint symlinks or read unbounded context files.
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "r", encoding="utf-8") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError("not a checkpoint file")
                    body = source.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise ValueError("checkpoint too large")
                state = json.loads(body)
                context = state["context"]
                saved_at = state["saved_at"]
                stamp = datetime.fromisoformat(saved_at.replace("Z", "+00:00"))
                if (
                    state["session_id"] != path.stem
                    or stamp.tzinfo is None
                    or not isinstance(context["current_task"], str)
                    or type(context["progress"]) not in (float, int)
                    or not math.isfinite(context["progress"])
                    or not 0 <= context["progress"] <= 1
                    or not isinstance(context["pending_items"], list)
                    or any(not isinstance(v, str) for v in context["pending_items"])
                    or not isinstance(context["think_history"], list)
                    or any(not isinstance(v, dict) for v in context["think_history"])
                ):
                    raise ValueError("invalid checkpoint")
                rows.append(
                    {
                        "session_id": state["session_id"],
                        "saved_at": saved_at,
                        "current_task": context["current_task"],
                        "progress": context["progress"],
                        "_stamp": stamp.timestamp(),
                    }
                )
            except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
                skipped += 1
        rows.sort(key=lambda row: (row["_stamp"], row["session_id"]), reverse=True)
        rows = rows[:10]
        for row in rows:
            del row["_stamp"]
        result = self._listing(rows, "available", "暂无可恢复的会话检查点。")
        if skipped:
            result["skipped_invalid"] = skipped
        return result

    def _query_evolution(self) -> dict[str, Any]:
        rows, status = self._read_rows(
            self.db_path,
            "SELECT * FROM entries WHERE category IN ('evolution_proposal','evolution_cycle') "
            "ORDER BY julianday(created_at) DESC,created_at DESC,rowid DESC LIMIT 20",
        )
        self._decode_fields(rows, ("payload", "refs"))
        return self._listing(rows, status, "暂无自进化提案或周期记录。")

    def _query_tools(self) -> dict[str, Any]:
        # Metadata only: never instantiate a router or invoke any tool.
        import importlib

        descriptors = None
        source = "capabilities"
        try:
            router = importlib.import_module("tool_router")
            listing = getattr(router, "list_tools", None)
            if callable(listing):
                descriptors = listing()
                source = "tool_router"
        except Exception:
            pass  # An absent/optional router is not required to inspect capabilities.
        if descriptors is None:
            try:
                capabilities = importlib.import_module("capabilities")
                descriptors = capabilities.catalog()
            except Exception:
                return self._listing([], "unavailable", "工具目录尚未就绪。")
        if not isinstance(descriptors, (list, tuple)):
            return self._listing([], "unavailable", "工具目录尚未就绪。")
        rows = []
        for item in descriptors:
            if not isinstance(item, dict):
                continue
            call = item.get("call")
            identity = item.get("id") or (call.get("name") if isinstance(call, dict) else None)
            name = identity or item.get("name")
            if not isinstance(name, str) or not name:
                continue
            rows.append(
                {
                    "name": name,
                    "label": item.get("name", name),
                    "description": item.get("description_zh")
                    or item.get("provides")
                    or item.get("persona_description")
                    or item.get("description")
                    or item.get("name", name),
                    "available": item.get("available"),
                }
            )
        result = self._listing(rows, "available", "暂无可用工具描述。")
        result["source"] = source
        return result

    def _query_skills(self) -> dict[str, Any]:
        root = self.db_path.parent
        # Standalone registry's real default is ~/.jarvis/skills/registry.db.
        # Isolated data roots never fall through into the user's production registry.
        paths = [root / "skill_registry.db", root / "skills" / "registry.db"]
        if root == DEFAULT_DB.parent:
            paths.append(root.parent / "skills" / "registry.db")
        path = next((path for path in paths if path.is_file()), paths[0])
        rows, status = self._read_rows(
            path,
            "SELECT name,version,manifest FROM skill_versions "
            "WHERE active=1 AND deleted=0 ORDER BY name LIMIT 100",
        )
        for row in rows:
            try:
                manifest = json.loads(row.pop("manifest"))
                if isinstance(manifest, dict):
                    for field in ("description", "tags", "dependencies"):
                        if field in manifest:
                            row[field] = manifest[field]
            except (ValueError, TypeError):
                row["description"] = "技能说明暂时不可读取"
        return self._listing(rows, status, "暂无已安装技能。")

    def _query_memory(self) -> dict[str, Any]:
        root = self.db_path.parent
        metrics = (
            (
                "high_confidence_facts",
                "sir_profile.db",
                "SELECT count(*) AS count FROM facts WHERE confidence>=0.8",
            ),
            (
                "active_hypotheses",
                "hypotheses.db",
                "SELECT count(*) AS count FROM hypotheses WHERE status IN ('pending','confirmed')",
            ),
            ("kg_nodes", "knowledge_graph.db", "SELECT count(*) AS count FROM nodes"),
            ("kg_edges", "knowledge_graph.db", "SELECT count(*) AS count FROM edges"),
        )
        result: dict[str, Any] = {"confidence_threshold": 0.8}
        sources = {}
        for key, filename, sql in metrics:
            rows, status = self._read_rows(root / filename, sql)
            # Missing/unreadable is unknown, not a fabricated zero count.
            result[key] = rows[0]["count"] if rows else None
            sources[key] = status
        result["sources"] = sources
        result["message"] = (
            "部分记忆数据尚未建立或暂时不可读取；未知数量显示为 null。"
            if any(s != "available" for s in sources.values())
            else ""
        )
        return result

    def query(self, name: str) -> dict[str, Any]:
        name = QUERY_ALIASES.get(name, name)
        if name not in QUERIES:
            raise ValueError("unknown_query")
        handlers = {
            "goals": self._query_goals,
            "sessions": self._query_sessions,
            "evolution": self._query_evolution,
            "tools": self._query_tools,
            "skills": self._query_skills,
            "memory": self._query_memory,
        }
        if name in handlers:
            return handlers[name]()
        # URI read-only prevents an unavailable/mistyped mind DB being created.
        db = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            if name == "status":
                count = db.execute("SELECT count(*) FROM entries").fetchone()[0]
                latest = db.execute(
                    "SELECT created_at FROM entries ORDER BY julianday(created_at) DESC, "
                    "created_at DESC, rowid DESC LIMIT 1"
                ).fetchone()
                return {
                    "pid": os.getpid(),
                    "uptime_seconds": time.monotonic() - self.started_at,
                    "entry_count": count,
                    "latest_entry_at": latest[0] if latest else None,
                }
            where, params, limit = "", (), ""
            if name == "24thoughts":
                where = (
                    "category IN ('thought','knowledge_gap','observation','reflection','task') "
                    "AND julianday(created_at)>=julianday('now','-1 day') "
                    "AND julianday(created_at)<=julianday('now')"
                )
                limit = " LIMIT 50"
            elif name == "beliefs":
                where = "source=?"
                params = ("self_report",)
            elif name == "location":
                where, params, limit = "category=?", ("location",), " LIMIT 1"
            elif name == "health":
                where, params, limit = "category=?", ("health",), " LIMIT 5"
            elif name == "actions":
                where, limit = "category IN ('task','action')", " LIMIT 10"
            rows = db.execute(
                "SELECT * FROM entries WHERE "
                + where
                + " ORDER BY julianday(created_at) DESC, created_at DESC, rowid DESC"
                + limit,
                params,
            ).fetchall()
            entries = []
            for row in rows:
                item = dict(row)
                for field in ("payload", "refs"):
                    if isinstance(item.get(field), str):
                        try:
                            item[field] = json.loads(item[field])
                        except ValueError:
                            pass  # Preserve stored data instead of inventing a replacement.
                entries.append(item)
            return (
                {"entry": entries[0] if entries else None}
                if name == "location"
                else {"entries": entries}
            )
        finally:
            db.close()

    def _enqueue_chat(self, text: str, *, streaming: bool = False) -> str:
        identity = "socket:" + uuid.uuid4().hex
        stamp = datetime.now(UTC).isoformat()
        with self._inbox() as db:
            db.execute(
                "INSERT INTO inbox(id,payload,delivered) VALUES(?,?,0)",
                (
                    identity,
                    json.dumps(
                        {
                            "source": "socket",
                            "text": text,
                            "timestamp": stamp,
                            "streaming": streaming,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            db.execute(
                "INSERT INTO socket_replies VALUES(?,'pending',NULL,NULL,?)", (identity, stamp)
            )
        if self.notify:
            self.notify()
        return identity

    def _stream_request(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """NDJSON deltas; model execution stays on process_pending's scheduler."""
        index = 0
        identity: Optional[str] = None
        try:
            text = request.get("text", request.get("prompt"))
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text.encode("utf-8")) > MAX_CHAT_BYTES
            ):
                raise ValueError("invalid_text")
            if self.stream_handler is None:
                yield {"text": "", "done": True, "error": "stream_chat_unavailable", "index": 0}
                return
            identity = self._enqueue_chat(text, streaming=True)
            deadline = time.monotonic() + self.chat_timeout
            while True:
                with self._inbox() as db:
                    rows = db.execute(
                        "SELECT chunk_index,text,done,error FROM socket_stream_chunks "
                        "WHERE request_id=? AND chunk_index>=? ORDER BY chunk_index LIMIT 256",
                        (identity, index),
                    ).fetchall()
                    state = db.execute(
                        "SELECT state,error FROM socket_replies WHERE request_id=?", (identity,)
                    ).fetchone()
                for seq, content, done, error in rows:
                    index = seq + 1
                    yield {
                        "text": content,
                        "done": bool(done),
                        "error": error,
                        "index": seq,
                        "request_id": identity,
                    }
                    if done:
                        return
                if rows:
                    continue  # Drain already committed chunks before checking timeout.
                if state and state[0] == "unknown":
                    error = state[1] or "chat_outcome_unknown"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.wait(min(0.02, remaining)):
                    error = "reply_timeout"
                    break
        except ValueError:
            error = "invalid_text"
        except Exception:
            error = "stream_transport_error"
        yield {
            "text": "",
            "done": True,
            "error": error,
            "index": index,
            "request_id": identity,
            "retryable": False,
        }

    def _chat(self, text: str) -> dict[str, Any]:
        if self.chat_handler is None:
            return {"error": "chat_unavailable"}
        identity = self._enqueue_chat(text)
        deadline = time.monotonic() + self.chat_timeout
        while True:
            with self._inbox() as db:
                row = db.execute(
                    "SELECT state,reply,error FROM socket_replies WHERE request_id=?", (identity,)
                ).fetchone()
            if row and row[0] == "completed":
                return {"reply": row[1], "request_id": identity}
            if row and row[0] == "unknown":
                return {"error": row[2] or "chat_outcome_unknown", "request_id": identity}
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._stop.wait(min(0.05, remaining)):
                return {
                    "error": "reply_timeout",
                    "request_id": identity,
                    "state": row[0] if row else "unknown",
                    "retryable": False,
                }

    def has_pending(self) -> bool:
        with self._inbox() as db:
            return (
                db.execute("SELECT 1 FROM socket_replies WHERE state='pending' LIMIT 1").fetchone()
                is not None
            )

    def _process_stream(self, text: str, identity: str) -> str:
        if self.stream_handler is None:
            raise ValueError("stream_chat_unavailable")
        index = 0
        terminal: Optional[StreamChunk] = None
        total_bytes = 0
        parts: list[str] = []

        def emit(chunk: StreamChunk) -> None:
            nonlocal index, terminal, total_bytes
            if terminal is not None:
                return
            if not isinstance(chunk, StreamChunk) or not isinstance(chunk.text, str):
                raise ValueError("invalid_stream_chunk")
            total_bytes += len(chunk.text.encode("utf-8"))
            if total_bytes > 128 * 1024 or index >= 32768:
                raise ValueError("stream_response_too_large")
            if chunk.text:
                parts.append(chunk.text)
            if chunk.done:
                # Hold completion until the provider callback returns successfully.
                terminal = chunk
                if not chunk.text:
                    return
            with self._inbox() as db:
                db.execute(
                    "INSERT INTO socket_stream_chunks VALUES(?,?,?,0,NULL)",
                    (identity, index, chunk.text),
                )
            index += 1

        self.stream_handler(text, identity, emit)
        if terminal is None:
            raise ValueError("stream_interrupted")
        # Store terminal and state atomically, preserving partial error output.
        reply = "".join(parts)
        with self._inbox() as db:
            db.execute(
                "INSERT INTO socket_stream_chunks VALUES(?,?,?,1,?)",
                (identity, index, "", terminal.error),
            )
            db.execute(
                "UPDATE socket_replies SET state=?,reply=?,error=? WHERE request_id=?",
                ("unknown" if terminal.error else "completed", reply, terminal.error, identity),
            )
            if terminal.error is None:
                db.execute("UPDATE inbox SET delivered=1 WHERE id=?", (identity,))
        return reply

    def process_pending(self, *, limit: int = 1) -> int:
        """Run only from Wiring's existing scheduler. Never repeat claimed work."""
        if (
            self.chat_handler is None and self.stream_handler is None
        ) or not self._processing.acquire(blocking=False):
            return 0
        processed = 0
        try:
            for _ in range(max(0, limit)):
                if self._stop.is_set():
                    break
                with self._inbox() as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute(
                        "SELECT i.id,i.payload FROM inbox i JOIN socket_replies r "
                        "ON r.request_id=i.id WHERE i.delivered=0 AND r.state='pending' "
                        "ORDER BY i.rowid LIMIT 1"
                    ).fetchone()
                    if not row:
                        break
                    identity, payload = row
                    db.execute("UPDATE inbox SET delivered=2 WHERE id=?", (identity,))
                    db.execute(
                        "UPDATE socket_replies SET state='dispatched_unknown' WHERE request_id=?",
                        (identity,),
                    )
                try:
                    request = json.loads(payload)
                    if request.get("streaming"):
                        self._process_stream(request["text"], identity)
                        processed += 1
                        continue
                    if self.chat_handler is None:
                        raise ValueError("chat_unavailable")
                    reply = self.chat_handler(request["text"], identity)
                    if not isinstance(reply, str) or not reply.strip():
                        raise ValueError("empty_reply")
                except Exception:
                    with self._inbox() as db:
                        db.execute(
                            "UPDATE socket_replies SET state='unknown',error="
                            "'chat_outcome_unknown' WHERE request_id=?",
                            (identity,),
                        )
                    continue
                with self._inbox() as db:
                    db.execute(
                        "UPDATE socket_replies SET state='completed',reply=? WHERE request_id=?",
                        (reply, identity),
                    )
                    db.execute("UPDATE inbox SET delivered=1 WHERE id=?", (identity,))
                processed += 1
        finally:
            self._processing.release()
        return processed


def self_evaluate():
    """Probe the configured protocol bounds without opening sockets or databases."""
    from datetime import datetime, timezone

    checks = {
        "request_budget_positive": type(MAX_REQUEST_BYTES) is int and MAX_REQUEST_BYTES > 0,
        "chat_budget_within_request": type(MAX_CHAT_BYTES) is int
        and 0 < MAX_CHAT_BYTES <= MAX_REQUEST_BYTES,
        "server_lifecycle": "explicit_start_stop"
        if callable(getattr(JarvisSocketServer, "start", None))
        and callable(getattr(JarvisSocketServer, "stop", None))
        else "incomplete",
    }
    return {
        "module": "jarvis_socket",
        "domain": "local Unix socket chat and query transport",
        "current_version": "bounded NDJSON requests with explicit server lifecycle and uncertain-outcome persistence",
        "known_limitations": [
            "Requires daemon-owned scheduler processing for chat work.",
            "Evaluation checks public configuration and methods; it does not start or connect to the daemon.",
        ],
        "improvement_directions": [
            "Add a read-only readiness endpoint reflecting active server lifecycle and handler availability."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "ok": all(v is True for v in checks.values()),
    }
