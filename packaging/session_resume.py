"""Private internal task checkpoints; no new user-visible conversation timeline.

No scheduler is installed: the daemon must call auto_save_checkpoint(). This
module is not wired into Wiring. Checkpoints preserve context, not execution
receipts: resuming never runs pending work or retries an unknown side effect.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import product_home

if TYPE_CHECKING:
    from inner_loop import MindStore

Record = dict[str, Any]


def _session_id(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("Session ID must be a safe 1..128 character identifier")
    return value


def _context(value: Any) -> Record:
    if type(value) is not dict:
        raise ValueError("Context must be an object")
    value = cast(Record, value)
    required = {"current_task", "progress", "pending_items", "think_history"}
    if not required.issubset(value):
        raise ValueError("Context requires current_task, progress, pending_items and think_history")
    progress = value["progress"]
    if (
        type(value["current_task"]) is not str
        or type(progress) not in (int, float)
        or not math.isfinite(progress)
        or not 0 <= progress <= 1
        or type(value["pending_items"]) is not list
        or any(type(item) is not str for item in cast(list[Any], value["pending_items"]))
        or type(value["think_history"]) is not list
        or any(type(item) is not dict for item in cast(list[Any], value["think_history"]))
    ):
        raise ValueError("Invalid checkpoint context")

    def check(item: Any) -> None:
        if type(item) is dict:
            mapping = cast(Record, item)
            if any(type(key) is not str for key in mapping):
                raise ValueError("Context object keys must be strings")
            for child in mapping.values():
                check(child)
        elif type(item) is list:
            for child in cast(list[Any], item):
                check(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("Context must contain JSON values")

    try:
        check(value)
        snapshot = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("Context must contain finite JSON values") from exc
    snapshot["progress"] = float(progress)
    return snapshot


def _private_file(path: Path) -> None:
    product_home.safe_absolute(path)
    if path.exists():
        info = path.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("Checkpoint must be a private owned regular file (0600)")


def self_evaluate() -> dict[str, Any]:
    """Report checkpoint boundaries without reading or writing session state."""
    return {
        "module": "session_resume",
        "domain": "private context checkpoints atomic file replacement safe session resumption",
        "current_version": "private JSON context snapshots with fsync/replace and explicit resume",
        "known_limitations": [
            (
                "Checkpoints preserve context, not execution receipts; resume never "
                "executes or retries pending work."
            ),
            (
                "Automatic saving requires an owner call; constructing this store or "
                "exposing its read-only UI installs no checkpoint schedule."
            ),
            "The lock is per instance/thread, not a cross-process single-writer guarantee.",
            "Checkpoint schema has no version, retention policy or explicit payload-size limit.",
            (
                "The injected mind store is identity-checked but no checkpoint event "
                "is appended to it."
            ),
        ],
        "improvement_directions": [
            "Add versioned schemas, bounded payloads and explicit checkpoint retention controls.",
            "Design cross-process checkpoint ownership without weakening private-path validation.",
            (
                "Integrate owner-triggered saves and durable execution-receipt "
                "reconciliation separately."
            ),
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


class SessionResume:
    """Save and restore internal context without executing pending operations."""

    def __init__(self, data_root: Path, mind_store: MindStore) -> None:
        self.data_root = product_home.validate_root(Path(data_root))
        product_home.make_private(self.data_root)
        if Path(mind_store.path) != self.data_root / "mind.db":
            raise ValueError("MindStore must belong to the checkpoint data root")
        self.mind_store = mind_store
        self.sessions_dir = product_home.safe_absolute(self.data_root / "sessions")
        product_home.make_private(self.sessions_dir)
        self.current_session_id: str | None = None
        self.current_context: Record | None = None
        self._lock = threading.RLock()

    def save_state(self, session_id: str, context: Record) -> str:
        """Atomically replace a checkpoint; caller mutations cannot change it."""
        session_id = _session_id(session_id)
        snapshot = _context(context)
        with self._lock:
            path = self.sessions_dir / f"{session_id}.json"
            _private_file(path)
            state = {
                "session_id": session_id,
                "saved_at": datetime.now(UTC).isoformat(),
                "context": snapshot,
            }
            body = json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8")
            fd, name = tempfile.mkstemp(prefix=".checkpoint-", dir=self.sessions_dir)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(body)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
                directory_fd = os.open(self.sessions_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                temporary.unlink(missing_ok=True)
            self.current_session_id = session_id
            self.current_context = snapshot
        return session_id

    def _read(self, session_id: str) -> Record | None:
        path = self.sessions_dir / f"{_session_id(session_id)}.json"
        _private_file(path)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as source:
                state = json.load(source)
            if type(state) is not dict:
                raise ValueError("Checkpoint must be an object")
            state = cast(Record, state)
            if state.get("session_id") != session_id:
                raise ValueError("Checkpoint identity mismatch")
            saved_at = state.get("saved_at")
            if type(saved_at) is not str or datetime.fromisoformat(saved_at).tzinfo is None:
                raise ValueError("Checkpoint timestamp must include timezone")
            state["context"] = _context(state.get("context"))
            return state
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError(f"Invalid checkpoint: {session_id}") from exc

    def resume(self, session_id: str) -> Record:
        """Missing IDs return resumed=False; corrupt checkpoints fail explicitly."""
        with self._lock:
            state = self._read(session_id)
            if state is None:
                return {"session_id": session_id, "context": {}, "resumed": False}
            self.current_session_id = session_id
            self.current_context = _context(state["context"])
            return {"session_id": session_id, "context": state["context"], "resumed": True}

    def list_sessions(self, limit: int = 10) -> list[Record]:
        """Newest saved first. Corrupt/incomplete files are not resumable entries."""
        if type(limit) is not int or limit < 0:
            raise ValueError("Session limit must be a nonnegative integer")
        if not limit:
            return []
        sessions: list[Record] = []
        with self._lock:
            for path in self.sessions_dir.glob("*.json"):
                try:
                    state = self._read(path.stem)
                except ValueError:
                    continue
                if state is not None:
                    sessions.append(
                        {
                            "session_id": state["session_id"],
                            "saved_at": state["saved_at"],
                            "current_task": state["context"]["current_task"],
                            "progress": state["context"]["progress"],
                        }
                    )
        sessions.sort(key=lambda row: (row["saved_at"], row["session_id"]), reverse=True)
        return sessions[:limit]

    def auto_save_checkpoint(self) -> str | None:
        """Persist the active context; return None before save_state()/resume().

        The owner may update current_context between calls. For concurrent use,
        supply a fresh snapshot through save_state() instead of mutating it.
        Restart callers must explicitly resume an ID before automatic saving;
        no context is invented from arbitrary mind entries.
        """
        with self._lock:
            if self.current_session_id is None or self.current_context is None:
                return None
            return self.save_state(self.current_session_id, self.current_context)
