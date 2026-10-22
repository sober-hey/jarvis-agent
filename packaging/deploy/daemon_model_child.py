"""Launcher child shim: inner requests read the timeline but never publish JSON turns."""

from __future__ import annotations

import json
import os
import re
import types
from pathlib import Path
from typing import Any, cast

import chat_entry


class InternalImport:
    def __init__(self) -> None:
        self.real = chat_entry.importlib

    def import_module(self, name: str) -> Any:
        module = self.real.import_module(name)
        if name != "hermes_state":
            return module
        factory = module.SessionDB

        class ReadTimelineStore:
            def __init__(self, **kwargs: Any) -> None:
                self.store = factory(**kwargs)

            def __getattr__(self, attr: str) -> Any:
                return getattr(self.store, attr)

            def append_messages_batch(self, session_id: str, messages: list[dict[str, Any]]) -> int:
                # Internal prompts/results already live in mind, reflection and
                # daemon attempt logs. Public history must contain actual dialogue.
                return 0

        return types.SimpleNamespace(SessionDB=ReadTimelineStore)


def internal_chat(
    root: Path,
    credentials_fd: int,
    request: dict[str, Any],
    test_port: int | None = None,
    protected_hosts: tuple[Path, ...] = (),
) -> dict[str, Any]:
    # Keep one timeline and the existing no-public-JSON adapter. Only the
    # system channel changes for the explicit outward-generation phase; otherwise
    # the original inner voice would forbid addressing Sir even when user JSON asks.
    try:
        prompt = request.get("prompt", "")
        context = json.loads(prompt[prompt.index("{") :])
    except (ValueError, TypeError):
        context = None
    if isinstance(context, dict):
        identity = cast(dict[str, Any], context).get("_daemon_call_identity")
        if isinstance(identity, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identity):
            # Register before any request so the parent can distinguish a still
            # running child from an orphaned reservation after restart.
            import sqlite3

            with sqlite3.connect(root / "daemon.db", timeout=2) as db:
                updated = db.execute(
                    "UPDATE budget SET child_pid=? WHERE call_identity=? "
                    "AND state='dispatched_unknown' AND child_pid IS NULL",
                    (os.getpid(), identity),
                ).rowcount
                if updated != 1:
                    raise RuntimeError("ModelCallAlreadyAttempted")
    if isinstance(context, dict) and "outreach_intent" in context:
        instruction = cast(dict[str, Any], context).get("instructions")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("Missing outward instruction")
        chat_entry.INNER_SYSTEM_PROMPT = instruction
    return original_chat(root, credentials_fd, request, test_port, protected_hosts)


original_chat = chat_entry.chat

if __name__ == "__main__":
    chat_entry.chat = internal_chat
    cast(dict[str, Any], vars(chat_entry))["importlib"] = InternalImport()
    raise SystemExit(chat_entry.main())
