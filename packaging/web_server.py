#!/usr/bin/env python3
"""Loopback-only development HTTP adapter; no production authentication claim.

Dedicated JSON config and server-side file/FD credentials only. A validated
startup route/data-root binding is retained for every attempt. No CORS or public bind.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import signal
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

# The product launcher runs with -I: load only our pinned sibling modules,
# never borrow an ambient cwd/PYTHONPATH. Remove the path after their import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import avatar_stream
    import product_memory as memory
    import web_tasks

    # 语音子系统已隔离到仓库根 voice/ 目录（见 voice/README.md）。
    # HTTP 适配器必须能在没有语音 SDK 的环境下工作，所以这里仍按可选依赖处理。
    _VOICE_DIR = Path(__file__).resolve().parent.parent / "voice"
    if str(_VOICE_DIR) not in sys.path:
        sys.path.append(str(_VOICE_DIR))
    try:
        import voice_ledger as _voice_ledger_module
    except ModuleNotFoundError as _voice_import_error:
        if _voice_import_error.name != "voice_ledger":
            raise
        _voice_ledger_module = None
    from context_limits import CHAT_CONTEXT_BYTE_LIMIT
finally:
    sys.path.pop(0)

if TYPE_CHECKING:
    import chat_launcher as launcher_types

_spec = importlib.util.spec_from_file_location(
    "jarvis_web_launcher", Path(__file__).with_name("chat_launcher.py")
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Launcher missing")
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)
if TYPE_CHECKING:
    launcher = launcher_types

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)
# Voice WebSocket owns 8765; the existing outreach HTTP server uses 8766.
# Explicit caller ports (including 0 for an ephemeral test port) remain unchanged.
DEFAULT_WEB_PORT = 8767
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/avatar.js": ("avatar.js", "text/javascript; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/tasks.js": ("tasks.js", "text/javascript; charset=utf-8"),
    "/memory.js": ("memory.js", "text/javascript; charset=utf-8"),
}


class DevServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(
        self, port: int, config: dict[str, Any], key: str, root: Path, test_port: int | None
    ) -> None:
        self.config, self.key, self.root, self.test_port = config, key, root, test_port
        self.route_binding = launcher.checked_binding(config, root, test_port)
        self.binding_summary = launcher.binding_summary(self.route_binding)
        self.timeline_id = launcher.timeline_identity(root)
        self.condition = threading.Condition()
        self.inflight = 0
        self.responses_inflight = 0
        self.slots = threading.BoundedSemaphore(2)
        self.active: set[str] = set()
        self.stopping = False
        self.avatar_source = avatar_stream.AvatarSource(root)
        self.avatar_stop = threading.Event()
        self.avatar_slots = threading.BoundedSemaphore(avatar_stream.MAX_CONNECTIONS)
        self.avatar_connections = 0
        self.voice_slots = threading.BoundedSemaphore(4)
        self.voice_ledger: Any = None
        # Default reads gateway observations for this same data root. Assembly
        # may replace it; a GET never probes cloud credentials or starts services.
        self.voice_status_reader: Any = self._read_voice_status
        self.bindings = launcher.safe_path(root / "web-bindings")
        self.bindings.mkdir(mode=0o700, exist_ok=True)
        if self.bindings.stat().st_mode & 0o777 != 0o700:
            raise ValueError("Unsafe binding directory")
        self.memories = memory.MemoryStore(root / "memory.db")
        self.tasks = web_tasks.TaskService(root, self.generate_task, self.lookup_task)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.authority = f"127.0.0.1:{self.server_port}"

    def get_voice_ledger(self) -> Any:
        """Share daemon's mind.db, with no model/provider startup side effect."""
        with self.condition:
            if self.voice_ledger is None and _voice_ledger_module is not None:
                self.voice_ledger = _voice_ledger_module.VoiceLedger(
                    _voice_ledger_module.MindStore(self.root)
                )
            # TODO: retain explicit 503 until voice_ledger is installed.
            return self.voice_ledger

    def voice_transcript_events(
        self, ledger: Any, after_seq: int, *, snapshot: bool = False
    ) -> list[dict[str, Any]]:
        """Read bounded committed events forward, including late old-turn replies.

        Initial connection snapshots the last 50 transcripts. A reconnect cursor
        then reads forward in batches, so high traffic and late callbacks cannot
        disappear merely because their turns fell outside the timeline window.
        """
        with ledger.store.connection() as db:
            if not snapshot:
                rows = db.execute(
                    "SELECT seq,category,payload FROM entries WHERE seq>? "
                    "AND category IN ('voice_user','voice_assistant') "
                    "AND json_extract(payload,'$.event') IN ('transcript','response') "
                    "ORDER BY seq LIMIT 100",
                    (after_seq,),
                ).fetchall()
            else:
                rows = list(
                    reversed(
                        db.execute(
                            "SELECT seq,category,payload FROM entries "
                            "WHERE category IN ('voice_user','voice_assistant') "
                            "AND json_extract(payload,'$.event') IN ('transcript','response') "
                            "ORDER BY seq DESC LIMIT 50"
                        ).fetchall()
                    )
                )
        return [
            {"seq": row["seq"], "category": row["category"], "payload": json.loads(row["payload"])}
            for row in rows
        ]

    def _read_voice_status(self) -> object:
        """Read the gateway's private observations, not fabricated Web defaults."""
        # The CLI runs under -I; 语音网关在仓库根 voice/ 下，与这里同级。
        voice_dir = Path(__file__).resolve().parent.parent / "voice"
        sys.path.insert(0, str(voice_dir))
        try:
            from voice_gateway import read_gateway_status
        finally:
            sys.path.pop(0)
        return read_gateway_status(self.root)

    def voice_status(self) -> dict[str, Any]:
        """Read cached runtime observations, never infer remote credential validity.

        Default reader delegates to voice_gateway.read_gateway_status(root).
        Assembly may replace it with a snapshot callback. Unknown is JSON null,
        not a fabricated successful key check or a port scan.
        """
        status: dict[str, Any] = {
            "gateway_running": None,
            "bailian_key_valid": None,
            "last_call_at": None,
            "status_source": "not_connected",
            "ledger_available": _voice_ledger_module is not None or self.voice_ledger is not None,
        }
        if self.voice_status_reader is not None:
            raw = self.voice_status_reader()
            if not isinstance(raw, dict):
                raise ValueError("Invalid voice status snapshot")
            snapshot = cast(dict[str, Any], raw)
            if not snapshot:
                return status  # No gateway heartbeat has ever been observed.
            for name in ("gateway_running", "bailian_key_valid"):
                value = snapshot.get(name)
                if value is not None and type(value) is not bool:
                    raise ValueError("Invalid voice status flag")
                status[name] = value
            last = snapshot.get("last_call_at")
            if last is not None:
                from datetime import datetime

                if not isinstance(last, str) or len(last) > 80:
                    raise ValueError("Invalid last call time")
                parsed = datetime.fromisoformat(last)
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError("Voice call time must have a timezone")
            status["last_call_at"] = last
            status["status_source"] = "runtime_snapshot"
        return status

    def avatar_voice_level(self, speaker: str, level: float) -> None:
        """Optional voice_port BrainSink callback; not exposed over HTTP."""
        self.avatar_source.on_audio_level(speaker, level)

    def server_close(self) -> None:
        self.avatar_stop.set()
        super().server_close()

    def note_presence(self) -> None:
        import sqlite3

        db = sqlite3.connect(self.root / "web-presence.db")
        try:
            with db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS presence("
                    "singleton INTEGER PRIMARY KEY CHECK(singleton=1), seen_at REAL)"
                )
                db.execute("INSERT OR REPLACE INTO presence VALUES(1,?)", (time.time(),))
        finally:
            db.close()

    def generate_task(self, prompt: str, identity: str) -> str:
        launcher.launch(
            dict(self.route_binding.values),
            self.key,
            prompt,
            self.root,
            "task-" + identity.removeprefix("semantic-"),
            test_port=self.test_port,
            attempt_id=identity,
            expected_sha256=self.route_binding.config_sha256,
            expected_binding_sha256=self.route_binding.binding_sha256,
        )
        result = self.lookup_task(identity)
        if result is None:
            raise ValueError("model outcome unknown")
        return result

    def lookup_task(self, identity: str) -> str | None:
        try:
            record = launcher.query_attempt(self.root, identity)
        except launcher.LauncherError:
            return None
        if any(record.get(name) != value for name, value in self.binding_summary.items()):
            raise ValueError("configuration binding conflict")
        if record.get("state") != "completed":
            return None
        return self.public(record.get("result"), identity)["text"]

    def stop(self, timeout: float = 70) -> bool:
        with self.condition:
            self.stopping = True
        self.avatar_stop.set()
        self.shutdown()
        deadline = time.monotonic() + max(0, timeout)
        with self.condition:
            while (
                self.inflight or self.responses_inflight or self.avatar_connections
            ) and time.monotonic() < deadline:
                self.condition.wait(max(0, deadline - time.monotonic()))
            clean = not (self.inflight or self.responses_inflight or self.avatar_connections)
            if not clean:
                # Do not kill a request whose provider outcome may be unknown.
                # A durable shutdown marker blocks future POST replay; GET can
                # still show a later completed launcher result after restart.
                for identity in self.active:
                    launcher.atomic_json(
                        self.bindings / (identity + ".shutdown"), {"state": "unknown"}
                    )
        clean = self.tasks.stop(timeout=timeout) and clean
        self.server_close()
        return clean

    def view(self, identity: str) -> dict[str, Any]:
        path = launcher.attempt_path(self.root, identity)
        if not path.exists():
            return {"state": "unknown"}
        try:
            record = launcher.query_attempt(self.root, identity)
            if record.get("state") == "completed":
                return {"state": "completed", "result": self.public(record.get("result"), identity)}
            if (self.bindings / (identity + ".shutdown")).exists():
                return {"state": "unknown"}
            return {"state": "started" if record.get("state") == "started" else "unknown"}
        except Exception:
            return {"state": "unknown"}

    def public(self, raw: object, identity: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("Invalid result")
        result_data = cast(dict[str, Any], raw)
        text, session = result_data.get("text"), result_data.get("session_id")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text.encode()) > 131072
            or not isinstance(session, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", session)
            or result_data.get("attempt_id") != identity
        ):
            raise ValueError("Invalid result")
        result = {
            "text": text,
            "session_id": session,
            "attempt_id": identity,
            "generation_degraded": result_data.get("generation_degraded", False),
        }
        if self.key in json.dumps(result):
            raise ValueError("Unsafe result")
        return result

    def chat_request(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        identity, prompt = payload["attempt_id"], payload["prompt"]
        session = payload.get("session_id")
        try:
            current = launcher.checked_binding(self.config, self.root, self.test_port)
            if launcher.binding_summary(current) != self.binding_summary:
                return 409, {"error": "configuration_binding_conflict"}
        except launcher.LauncherError:
            return 409, {"error": "configuration_binding_conflict"}
        binding = {
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "session_sha256": hashlib.sha256(json.dumps(session).encode()).hexdigest(),
            "source_channel": payload.get("source_channel", "web"),
            **self.binding_summary,
        }
        path = launcher.safe_path(self.bindings / (identity + ".json"))
        with self.condition:
            if path.exists():
                try:
                    if path.stat().st_size > 4096:
                        return 202, {"state": "unknown"}
                    old = json.loads(path.read_text())
                    if old.get("binding") != binding:
                        return 409, {"error": "attempt_payload_conflict"}
                except Exception:
                    return 202, {"state": "unknown"}
                view = self.view(identity)
                if view["state"] == "completed":
                    try:
                        record = launcher.query_attempt(self.root, identity)
                        selected = old.get("session_id")
                        expected = {
                            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                            "session_sha256": hashlib.sha256(selected.encode()).hexdigest(),
                            "source_channel": old.get("source_channel", "web"),
                            **self.binding_summary,
                        }
                        if any(record.get(name) != value for name, value in expected.items()):
                            return 409, {"error": "attempt_record_conflict"}
                    except Exception:
                        return 202, {"state": "unknown"}
                return (200, view["result"]) if view["state"] == "completed" else (202, view)
            # IDs created by another entry have no trusted original HTTP binding.
            if launcher.attempt_path(self.root, identity).exists():
                return 409, {"error": "attempt_binding_unavailable"}
            if self.stopping or not self.slots.acquire(blocking=False):
                return 503, {"error": "busy"}
            selected = session or "web-" + uuid.uuid4().hex
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump(
                        {
                            "binding": binding,
                            "session_id": selected,
                            "source_channel": payload.get("source_channel", "web"),
                        },
                        stream,
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                launcher.sync_directory(self.bindings)
            except FileExistsError:
                self.slots.release()
                return 202, {"state": "unknown"}
            except Exception:
                self.slots.release()
                raise
            self.inflight += 1
            self.active.add(identity)
        try:
            launcher.launch(
                dict(self.route_binding.values),
                self.key,
                prompt,
                self.root,
                selected,
                test_port=self.test_port,
                attempt_id=identity,
                source_channel=payload.get("source_channel", "web"),
                expected_sha256=self.route_binding.config_sha256,
                expected_binding_sha256=self.route_binding.binding_sha256,
            )
            view = self.view(identity)
            return (200, view["result"]) if view["state"] == "completed" else (202, view)
        except Exception:
            return 202, self.view(identity)
        finally:
            with self.condition:
                self.inflight -= 1
                self.slots.release()
                self.active.discard(identity)
                self.condition.notify_all()


class Handler(BaseHTTPRequestHandler):
    @property
    def dev(self) -> DevServer:
        return cast(DevServer, self.server)

    def log_message(self, format: str, *args: object) -> None:
        pass  # No prompt, request target, credentials or arbitrary diagnostics.

    def send(
        self, status: int, value: object, mime: str = "application/json; charset=utf-8"
    ) -> None:
        blob = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        for name, content in (
            ("Content-Type", mime),
            ("Content-Length", str(len(blob))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Security-Policy", CSP),
            ("Connection", "close"),
        ):
            self.send_header(name, content)
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(blob)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self, *, read_only: bool = False) -> bool:
        if self.headers.get_all("Host") != [self.dev.authority]:
            return False
        origins = self.headers.get_all("Origin")
        sites = self.headers.get_all("Sec-Fetch-Site")
        # GET fetches normally omit Origin. Missing browser metadata permits
        # local read-only clients; this is not authentication or a public API.
        return (origins == [self.dev.origin] or (read_only and origins is None)) and (
            sites is None or sites in (["same-origin"], ["none"])
        )

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def do_POST(self) -> None:
        if self.path == "/api/voice/playback":
            if not self.authorized():
                self.send(403, {"error": "local_origin_required"})
                return
            self.voice_playback_post()
            return
        if not self.authorized():
            self.send(403, {"error": "local_origin_required"})
            return
        if self.path == "/api/tasks" or re.fullmatch(
            r"/api/tasks/chain-[0-9a-f]{64}/resume", self.path
        ):
            self.task_post()
            return
        if re.fullmatch(r"/api/memories/[0-9a-f]{32}/(correct|delete)", self.path):
            self.memory_post()
            return
        if self.path not in {"/api/chat", "/api/timeline/chat"}:
            self.send(404, {"error": "not_found"})
            return
        if self.headers.get_all("Content-Type") != ["application/json"]:
            self.send(415, {"error": "json_required"})
            return
        lengths = self.headers.get_all("Content-Length")
        if (
            self.headers.get("Transfer-Encoding") is not None
            or not lengths
            or len(lengths) != 1
            or not re.fullmatch(r"[0-9]{1,6}", lengths[0])
        ):
            self.send(400, {"error": "invalid_length"})
            return
        length = int(lengths[0])
        if not 1 <= length <= 65536:
            self.send(413, {"error": "body_too_large"})
            return
        try:
            blob = self.rfile.read(length)
            if len(blob) != length:
                raise ValueError("Incomplete")

            def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for name, value in pairs:
                    if name in result:
                        raise ValueError("Duplicate")
                    result[name] = value
                return result

            raw_payload: object = json.loads(blob.decode("utf-8"), object_pairs_hook=unique)
            if not isinstance(raw_payload, dict):
                raise ValueError("Object")
            payload = cast(dict[str, Any], raw_payload)
            if not {"prompt", "attempt_id"} <= set(payload) or set(payload) - {
                "prompt",
                "attempt_id",
                "session_id",
                "source_channel",
            }:
                raise ValueError("Fields")
            prompt, identity = payload["prompt"], payload["attempt_id"]
            if (
                not isinstance(prompt, str)
                or not prompt.strip()
                or len(prompt.encode("utf-8")) > CHAT_CONTEXT_BYTE_LIMIT
                or not valid_id(identity)
            ):
                raise ValueError("Input")
            if "session_id" in payload and (
                not isinstance(payload["session_id"], str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", payload["session_id"])
            ):
                raise ValueError("Session")
            if payload.get("source_channel", "web") not in {"web", "voice", "email", "imessage"}:
                raise ValueError("Channel")
        except (TimeoutError, ValueError, UnicodeError):
            self.send(400, {"error": "invalid_request"})
            return
        # Drain the admitted HTTP response too, not only the model operation.
        # chat_request releases its model slot before this handler sends bytes.
        with self.dev.condition:
            admitted = not self.dev.stopping
            if admitted:
                self.dev.responses_inflight += 1
        if not admitted:
            self.send(503, {"error": "busy"})
            return
        try:
            if self.path == "/api/timeline/chat":
                if "session_id" in payload:
                    self.send(400, {"error": "timeline_identity_is_server_owned"})
                    return
                payload["session_id"] = self.dev.timeline_id
            status, result = self.dev.chat_request(payload)
            self.send(status, result)
        except Exception:
            self.send(202, {"state": "unknown"})
        finally:
            with self.dev.condition:
                self.dev.responses_inflight -= 1
                self.dev.condition.notify_all()

    def voice_playback_post(self) -> None:
        """Append a client observation only; never call chat or cancel work.

        started: turn_id, assistant_id, device_id
        ended: turn_id, playback_id, optional interrupted (bool)
        interrupted: turn_id, playback_id, interrupted_by, optional device_id
        All variants accept an optional timezone-aware timestamp.
        """
        if self.headers.get_all("Content-Type") != ["application/json"]:
            self.send(415, {"error": "json_required"})
            return
        lengths = self.headers.get_all("Content-Length")
        if (
            self.headers.get("Transfer-Encoding") is not None
            or not lengths
            or len(lengths) != 1
            or not re.fullmatch(r"[0-9]{1,5}", lengths[0])
        ):
            self.send(400, {"error": "invalid_length"})
            return
        length = int(lengths[0])
        if not 1 <= length <= 8192:
            self.send(413, {"error": "body_too_large"})
            return
        try:
            blob = self.rfile.read(length)
            if len(blob) != length:
                raise ValueError("Incomplete body")

            def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate field")
                    result[key] = value
                return result

            value = json.loads(blob, object_pairs_hook=unique)
            if not isinstance(value, dict):
                raise ValueError("Object required")
            payload = cast(dict[str, Any], value)
            state = payload.get("status")
            fields: dict[str, tuple[set[str], set[str]]] = {
                "started": ({"turn_id", "assistant_id", "device_id"}, set()),
                "ended": ({"turn_id", "playback_id"}, {"interrupted"}),
                "interrupted": (
                    {"turn_id", "playback_id", "interrupted_by"},
                    {"device_id", "interrupting_turn_id"},
                ),
            }
            if not isinstance(state, str) or state not in fields:
                raise ValueError("Unknown playback status")
            required, optional = fields[state]
            if not required <= payload.keys() or payload.keys() - (
                required | optional | {"status", "timestamp"}
            ):
                raise ValueError("Invalid playback fields")
            for name, item in payload.items():
                if name == "interrupted":
                    if type(item) is not bool:
                        raise ValueError("Boolean required")
                elif not isinstance(item, str) or not item.strip() or len(item) > 200:
                    raise ValueError("Invalid playback string")
        except (ValueError, UnicodeError, TimeoutError, RecursionError):
            self.send(400, {"error": "invalid_request"})
            return
        # Reuse existing response accounting to drain ledger appends on shutdown.
        with self.dev.condition:
            admitted = not self.dev.stopping
            if admitted:
                self.dev.responses_inflight += 1
        if not admitted:
            self.send(503, {"error": "busy"})
            return
        try:
            ledger = self.dev.get_voice_ledger()
            if ledger is None:
                self.send(503, {"error": "voice_ledger_unavailable"})
                return
            stamp = {"timestamp": payload["timestamp"]} if "timestamp" in payload else {}
            if state == "started":
                event = ledger.record_playback_start(
                    payload["turn_id"],
                    payload["assistant_id"],
                    device_id=payload["device_id"],
                    **stamp,
                )
                playback_id = event["id"]
            elif state == "ended":
                event = ledger.record_playback_end(
                    payload["turn_id"],
                    payload["playback_id"],
                    interrupted=payload.get("interrupted", False),
                    **stamp,
                )
                playback_id = payload["playback_id"]
            else:
                extra = {
                    name: payload[name]
                    for name in ("device_id", "interrupting_turn_id")
                    if name in payload
                }
                event = ledger.record_interrupt(
                    payload["turn_id"],
                    target_id=payload["playback_id"],
                    interrupted_by=payload["interrupted_by"],
                    **extra,
                    **stamp,
                )
                playback_id = payload["playback_id"]
            self.send(201, {"event": event, "playback_id": playback_id})
        except (ValueError, TypeError, KeyError):
            self.send(400, {"error": "invalid_playback_reference"})
        except Exception:
            # Do not echo SQL/provider diagnostics or retry an uncertain append.
            self.send(503, {"error": "voice_ledger_unavailable"})
        finally:
            with self.dev.condition:
                self.dev.responses_inflight -= 1
                self.dev.condition.notify_all()

    def voice_get(self) -> None:
        from urllib.parse import parse_qs, urlsplit

        try:
            target = urlsplit(self.path)
            query = parse_qs(target.query, keep_blank_values=True, strict_parsing=True)
            if target.fragment or target.path != "/api/voice/timeline" and query:
                raise ValueError("Unexpected query")
            if query.keys() - {"limit"}:
                raise ValueError("Unknown query")
            values = query.get("limit", ["50"])
            if len(values) != 1 or not re.fullmatch(r"[0-9]{1,3}", values[0]):
                raise ValueError("Invalid limit")
            limit = int(values[0])
            if not 1 <= limit <= 200:
                raise ValueError("Invalid limit")
        except ValueError:
            self.send(400, {"error": "invalid_request"})
            return
        try:
            if target.path == "/api/voice/status":
                self.send(200, self.dev.voice_status())
                return
            ledger = self.dev.get_voice_ledger()
            if ledger is None:
                self.send(503, {"error": "voice_ledger_unavailable"})
                return
            if target.path == "/api/voice/stream":
                self.voice_stream(ledger)
            else:
                self.send(200, {"turns": ledger.recent_voice_turns(limit=limit), "limit": limit})
        except Exception:
            self.send(503, {"error": "voice_subsystem_unavailable"})

    def voice_stream(self, ledger: Any) -> None:
        """Poll committed final transcripts; no extra model, ASR or thread bus.

        Snapshot on connect, then ordered idempotent sequence IDs. ASR partials
        are not persisted by VoiceLedger and are not invented here. The gateway
        must commit its transcript callbacks to that same ledger at assembly.
        """
        ids = self.headers.get_all("Last-Event-ID")
        if ids is not None and (
            len(ids) != 1
            or re.fullmatch(r"[0-9]{1,19}", ids[0]) is None
            or int(ids[0]) > 9223372036854775807
        ):
            self.send(400, {"error": "invalid_event_id"})
            return
        with self.dev.condition:
            admitted = not self.dev.stopping and self.dev.voice_slots.acquire(blocking=False)
            if admitted:
                self.dev.responses_inflight += 1
        if not admitted:
            self.send(503, {"error": "voice_stream_busy"})
            return
        headers_sent = False
        try:
            previous = int(ids[0]) if ids else 0
            # Read before sending headers: backend failures remain JSON 503s.
            events = self.dev.voice_transcript_events(ledger, previous, snapshot=ids is None)
            headers_sent = True
            self.send_response(200)
            for name, content in (
                ("Content-Type", "text/event-stream; charset=utf-8"),
                ("Cache-Control", "no-store"),
                ("X-Accel-Buffering", "no"),
                ("X-Content-Type-Options", "nosniff"),
                ("Content-Security-Policy", CSP),
                ("Connection", "close"),
            ):
                self.send_header(name, content)
            self.end_headers()
            self.close_connection = True
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            deadline = time.monotonic() + 30
            heartbeat = time.monotonic()
            while not self.dev.avatar_stop.is_set() and time.monotonic() < deadline:
                for event in events:
                    if event["seq"] <= previous:
                        continue
                    payload = json.dumps(
                        {
                            "turn_id": event["payload"]["turn_id"],
                            "role": "user" if event["category"] == "voice_user" else "assistant",
                            "text": event["payload"]["text"],
                            "final": True,
                            "timestamp": event["payload"]["timestamp"],
                        },
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                    self.wfile.write(
                        f"id: {event['seq']}\nevent: transcript\ndata: {payload}\n\n".encode()
                    )
                    self.wfile.flush()
                    previous = event["seq"]
                if time.monotonic() - heartbeat >= 5:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    heartbeat = time.monotonic()
                if self.dev.avatar_stop.wait(0.1):
                    break
                events = self.dev.voice_transcript_events(ledger, previous)
        except Exception:
            # After headers, close the stream rather than writing a second HTTP
            # response. Client reconnect is bounded, never a model resend.
            if not headers_sent:
                self.send(503, {"error": "voice_subsystem_unavailable"})
            return
        finally:
            with self.dev.condition:
                self.dev.responses_inflight -= 1
                self.dev.voice_slots.release()
                self.dev.condition.notify_all()

    def memory_post(self) -> None:
        if self.headers.get_all("Content-Type") != ["application/json"]:
            self.send(415, {"error": "json_required"})
            return
        lengths = self.headers.get_all("Content-Length")
        if (
            self.headers.get("Transfer-Encoding") is not None
            or not lengths
            or len(lengths) != 1
            or not re.fullmatch(r"[0-9]{1,5}", lengths[0])
        ):
            self.send(400, {"error": "invalid_length"})
            return
        length = int(lengths[0])
        if not 1 <= length <= 8192:
            self.send(413, {"error": "body_too_large"})
            return
        lock_fd: int | None = None
        try:
            raw: object = json.loads(self.rfile.read(length))
            if not isinstance(raw, dict):
                raise ValueError("Object required")
            payload = cast(dict[str, Any], raw)
            identity, action = self.path.split("/")[3:5]
            if set(payload) != ({"content"} if action == "correct" else set()):
                raise ValueError("Invalid memory edit")
            # Refuse edits while any model process holds old context; once the
            # lock is ours there are no stale in-flight replies or extraction.
            lock_fd = os.open(
                launcher.safe_path(self.dev.root / "launcher-session.lock"),
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.send(
                    409,
                    {"error": "conversation_busy", "detail": "请等待当前对话完成后再修改记忆。"},
                )
                return
            if action == "correct":
                if not isinstance(payload["content"], str):
                    raise ValueError("Invalid content")
                row = self.dev.memories.correct(identity, payload["content"])
                self.send(200, {"memory": row})
            else:
                self.dev.memories.delete(identity)
                self.send(200, {"deleted": identity})
        except KeyError:
            self.send(404, {"error": "memory_not_found"})
        except (ValueError, TypeError, OSError, TimeoutError, memory.sqlite3.Error):
            self.send(400, {"error": "memory_edit_rejected"})
        finally:
            if lock_fd is not None:
                os.close(lock_fd)

    def task_post(self) -> None:
        if self.headers.get_all("Content-Type") != ["application/json"]:
            self.send(415, {"error": "json_required"})
            return
        lengths = self.headers.get_all("Content-Length")
        if (
            self.headers.get("Transfer-Encoding") is not None
            or not lengths
            or len(lengths) != 1
            or not re.fullmatch(r"[0-9]{1,7}", lengths[0])
        ):
            self.send(400, {"error": "invalid_length"})
            return
        length = int(lengths[0])
        if not 1 <= length <= 1500000:
            self.send(413, {"error": "body_too_large"})
            return
        try:
            blob = self.rfile.read(length)
            if len(blob) != length:
                raise ValueError("incomplete body")

            def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for name, value in pairs:
                    if name in result:
                        raise ValueError("duplicate field")
                    result[name] = value
                return result

            value = json.loads(blob, object_pairs_hook=unique)
            if not isinstance(value, dict):
                raise ValueError("object required")
            if self.path == "/api/tasks":
                self.send(201, self.dev.tasks.upload(cast(dict[str, Any], value)))
            else:
                if value:
                    raise ValueError("empty resume required")
                self.send(202, self.dev.tasks.resume(self.path.split("/")[3]))
        except FileNotFoundError:
            self.send(404, {"error": "task_not_found"})
        except (TimeoutError, OSError, ValueError, UnicodeError, TypeError, KeyError):
            self.send(
                400,
                {
                    "error": "task_rejected",
                    "detail": (
                        "请检查文件格式、大小、任务类型；PDF须有文字层和提取依赖。"
                        "未知模型结果只能查询恢复，不会自动重发。"
                    ),
                },
            )

    def task_get(self) -> None:
        try:
            if self.path == "/api/tasks":
                self.send(200, self.dev.tasks.list())
                return
            match = re.fullmatch(r"/api/tasks/(chain-[0-9a-f]{64})(/download)?", self.path)
            if match is None:
                self.send(404, {"error": "not_found"})
                return
            if match[2]:
                self.send(200, self.dev.tasks.download(match[1]), "text/markdown; charset=utf-8")
            else:
                self.send(200, self.dev.tasks.query(match[1]))
        except FileNotFoundError:
            self.send(404, {"error": "task_not_found"})
        except (OSError, ValueError, KeyError, TypeError):
            self.send(409, {"error": "task_not_ready_or_changed"})

    def do_OPTIONS(self) -> None:
        self.send(403, {"error": "cors_disabled"})

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.send(code, {"error": "invalid_http_request"})

    def avatar_get(self) -> None:
        # Slot acquisition and stop admission are atomic with lifecycle shutdown.
        with self.dev.condition:
            admitted = not self.dev.stopping and self.dev.avatar_slots.acquire(blocking=False)
            if admitted:
                self.dev.avatar_connections += 1
        if not admitted:
            self.send(503, {"error": "avatar_stream_busy"})
            return
        try:
            self.send_response(200)
            for name, value in (
                ("Content-Type", "text/event-stream; charset=utf-8"),
                ("Cache-Control", "no-store"),
                ("X-Accel-Buffering", "no"),
                ("X-Content-Type-Options", "nosniff"),
                ("Content-Security-Policy", CSP),
                ("Connection", "close"),
            ):
                self.send_header(name, value)
            self.end_headers()
            self.close_connection = True
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            deadline = time.monotonic() + avatar_stream.STREAM_SECONDS
            while not self.dev.avatar_stop.is_set() and time.monotonic() < deadline:
                with self.dev.condition:
                    thinking = bool(self.dev.inflight)
                frame = self.dev.avatar_source.frame(thinking=thinking)
                payload = json.dumps(frame, allow_nan=False, separators=(",", ":"))
                self.wfile.write(
                    f"id: {frame['sequence']}\nevent: avatar\ndata: {payload}\n\n".encode()
                )
                self.wfile.flush()
                # Delay measured after write: slow readers cannot cause bursts.
                self.dev.avatar_stop.wait(avatar_stream.PUSH_INTERVAL)
        except (OSError, TimeoutError):
            pass
        finally:
            with self.dev.condition:
                self.dev.avatar_connections -= 1
                self.dev.avatar_slots.release()
                self.dev.condition.notify_all()

    def timeline_events(self) -> None:
        # Stream the authoritative transcript, not outreach's parallel delivery
        # ledger. Reconnect starts with a snapshot so a lost event cannot lose text.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        previous: str | None = None
        last_presence = 0.0
        try:
            while not self.dev.stopping:
                if time.monotonic() - last_presence >= 5:
                    self.dev.note_presence()
                    last_presence = time.monotonic()
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                rows = launcher.read_timeline(self.dev.root)
                payload = json.dumps(
                    {"timeline_id": self.dev.timeline_id, "entries": rows}, ensure_ascii=False
                )
                digest = hashlib.sha256(payload.encode()).hexdigest()
                if digest != previous:
                    self.wfile.write(f"id: {digest}\nevent: timeline\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                    previous = digest
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] in {
            "/api/voice/timeline",
            "/api/voice/status",
            "/api/voice/stream",
        }:
            if not self.authorized(read_only=True):
                self.send(403, {"error": "local_origin_required"})
                return
            self.voice_get()
            return
        if not self.authorized(read_only=True):
            self.send(403, {"error": "local_origin_required"})
            return
        if self.path == "/api/avatar/stream":
            self.avatar_get()
            return
        if self.path == "/api/tasks" or self.path.startswith("/api/tasks/"):
            self.task_get()
            return
        if self.path == "/api/timeline/events":
            self.timeline_events()
            return
        if self.path == "/api/timeline":
            rows = launcher.read_timeline(self.dev.root)
            self.send(200, {"timeline_id": self.dev.timeline_id, "entries": rows})
            return
        if self.path == "/api/memories":
            self.send(200, {"memories": self.dev.memories.list()})
            return
        if self.path.startswith("/api/"):
            if self.path == "/api/health":
                self.send(
                    200, {"status": "ok", "scope": "loopback development", "model_verified": False}
                )
            elif self.path.startswith("/api/attempts/") and valid_id(
                self.path.removeprefix("/api/attempts/")
            ):
                self.send(200, self.dev.view(self.path.removeprefix("/api/attempts/")))
            else:
                self.send(404, {"error": "not_found"})
        elif self.path in STATIC and self.headers.get_all("Host") == [self.dev.authority]:
            name, mime = STATIC[self.path]
            target = Path(__file__).parents[1] / "apps/web" / name
            self.send(200, target.read_bytes(), mime)
        else:
            self.send(404, {"error": "not_found"})


def valid_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", value
        )
        is not None
    )


def create_server(
    *,
    config_file: Path,
    key_file: Path | None = None,
    key_fd: int | None = None,
    data_dir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_WEB_PORT,
    test_local_port: int | None = None,
    acceptance_mode: bool = False,
) -> DevServer:
    if acceptance_mode:
        if data_dir is None:
            raise ValueError("Acceptance requires an explicit temporary data directory")
        # Canonical root is passed unchanged through normal launcher/child checks.
        data_dir = launcher.chat.home.validate_root(data_dir, validation_mode="acceptance")
    if host != "127.0.0.1" or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("Only explicit 127.0.0.1 binding is supported")
    config = launcher.read_config(config_file, test_local_port)
    key = launcher.read_key(key_file=key_file, key_fd=key_fd)
    if test_local_port is not None and not key.startswith("FAKE-"):
        raise ValueError("Explicit test mode requires FAKE credentials")
    return DevServer(port, config, key, launcher.prepare_root(data_dir), test_local_port)


def self_evaluate() -> dict[str, Any]:
    """Inspect live declarations and pure guards; never construct DevServer."""
    from datetime import datetime, timezone
    from email.message import Message
    from types import SimpleNamespace

    authority = "127.0.0.1:8123"
    origin = "http://" + authority

    def authorized(host: str, request_origin: str) -> bool:
        headers = Message()
        headers["Host"] = host
        headers["Origin"] = request_origin
        headers["Sec-Fetch-Site"] = "same-origin"
        request = SimpleNamespace(
            headers=headers, dev=SimpleNamespace(authority=authority, origin=origin)
        )
        return Handler.authorized(request)  # type: ignore[arg-type]

    return {
        "module": "web_server",
        "domain": "loopback HTTP chat timeline memory task and avatar development adapter",
        "current_version": "1",
        "version_scope": "offline evaluation contract, not a deployed release",
        "known_limitations": [
            "Loopback development adapter has no production authentication claim.",
            "Pure guard checks do not verify HTTP/SSE delivery or running server state.",
            "No config, credentials, model availability or production stores are inspected.",
        ],
        "improvement_directions": [
            "Keep isolated HTTP/SSE acceptance distinct from guard checks and runtime deployment."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_verified": False,
        "scope": "in-memory request guard probes and route declarations only",
        "static_routes": sorted(STATIC),
        "chat_context_byte_limit": CHAT_CONTEXT_BYTE_LIMIT,
        "content_security_policy": CSP,
        "offline_probe": {
            "valid_uuid_accepted": valid_id("65a70552-1e70-4c43-82a0-520ab8cf10d2"),
            "invalid_uuid_rejected": not valid_id("not-an-attempt-id"),
            "same_origin_accepted": authorized(authority, origin),
            "cross_origin_rejected": not authorized(authority, "http://other.invalid"),
            "wrong_host_rejected": not authorized("other.invalid", origin),
        },
    }


def main() -> int:
    parser = launcher.Parser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--key-file", type=Path)
    source.add_argument("--key-fd", type=int)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_WEB_PORT,
        help=f"Loopback Web port (default {DEFAULT_WEB_PORT}; explicit 0 is ephemeral)",
    )
    parser.add_argument("--test-local-port", type=int)
    parser.add_argument("--acceptance-mode", action="store_true")
    server: DevServer | None = None
    thread: threading.Thread | None = None
    clean = True

    def terminate(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        args = parser.parse_args()
        server = create_server(
            config_file=args.config,
            key_file=args.key_file,
            key_fd=args.key_fd,
            data_dir=args.data_dir,
            host=args.host,
            port=args.port,
            test_local_port=args.test_local_port,
            acceptance_mode=args.acceptance_mode,
        )
        print(json.dumps({"url": server.origin, "scope": "loopback development only"}), flush=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        pass
    except Exception:
        print('{"error":"startup_failed"}', flush=True)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
        if server is not None and thread is not None:
            clean = server.stop(timeout=70)
            thread.join(2)
            if not clean:
                print('{"state":"unknown","retry_allowed":false}', flush=True)
    return 0 if clean else 2


if __name__ == "__main__":
    raise SystemExit(main())
