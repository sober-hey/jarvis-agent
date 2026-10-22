"""Private loopback document tasks, durable receipts and bounded workers."""

from __future__ import annotations

import base64
import binascii
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import file_task_chain as chain

Record = dict[str, Any]


def self_evaluate() -> dict[str, object]:
    """Describe this adapter without loading state or executing work."""
    from datetime import UTC, datetime

    return {
        "module": "web_tasks",
        "domain": "loopback document upload bounded worker lifecycle and verified downloads",
        "current_version": "single-active-worker TaskService over durable semantic file chains",
        "known_limitations": [
            "Only one task worker can be active per TaskService instance; there is no durable worker queue or automatic startup resume.",
            "Worker exceptions are swallowed by run(); persisted chain status remains authoritative and no structured exception is exposed by the service.",
            "stop waits for workers but cannot forcibly cancel a blocked generator thread.",
            "Uploads accept at most one MiB of TXT/Markdown or text-layer PDF; OCR and non-document workflows are unsupported.",
        ],
        "improvement_directions": [
            "Publish sanitized structured worker diagnostics without inventing successful completion.",
            "Add a durable pending-work queue and receipt-based startup reconciliation before promising automatic recovery.",
            "Use cooperative generator cancellation or isolated workers before claiming bounded stop latency.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


class TaskService:
    def __init__(
        self, root: Path, generate: Callable[[str, str], str], lookup: Callable[[str], str | None]
    ) -> None:
        self.root, self.generate, self.lookup = root, generate, lookup
        self.active: set[str] = set()
        self.condition = threading.Condition()
        self.stopping = False

    def upload(self, payload: Record) -> Record:
        if set(payload) != {
            "request_key",
            "filename",
            "content_base64",
            "processing",
            "instruction",
        }:
            raise ValueError("invalid upload fields")
        if not all(isinstance(value, str) for value in payload.values()):
            raise ValueError("string fields required")
        name = payload["filename"]
        suffix = Path(name).suffix.lower()
        if not re.fullmatch(r"[^/\\\x00-\x1f]{1,180}", name) or suffix not in {
            ".txt",
            ".md",
            ".pdf",
        }:
            raise ValueError("invalid filename")
        key = payload["request_key"]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise ValueError("invalid request key")
        try:
            blob = base64.b64decode(payload["content_base64"], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("invalid base64") from None
        if not 0 < len(blob) <= chain.files.MAX_INPUT:
            raise ValueError("file exceeds one MiB")
        # Preflight before retaining upload: reject unsupported PDF/OCR explicitly.
        chain.semantic_text(blob, suffix)
        token = chain.files.digest(key.encode())
        source = self.root / "uploads" / (token + suffix)
        with self.condition:
            with (
                chain.files.root_handle(self.root) as root,
                chain.files.directory(root, "uploads") as dest,
            ):
                try:
                    previous = chain.files.read(dest, source.name, chain.files.MAX_INPUT)
                except FileNotFoundError:
                    chain.files.atomic(dest, source.name, blob, replace=False)
                else:
                    if previous != blob:
                        raise ValueError("request key upload conflict")
            record = chain.create_semantic(
                self.root, source, payload["instruction"], key, payload["processing"]
            )
            return self.public(record)

    @staticmethod
    def public(record: Record) -> Record:
        return {
            key: record[key]
            for key in (
                "task_id",
                "status",
                "progress",
                "stage",
                "processing",
                "output_sha256",
                "error",
            )
            if key in record
        }

    def query(self, identity: str) -> Record:
        record = chain.query(self.root, identity)
        if record["schema_version"] != 2:
            raise ValueError("not a web semantic task")
        view = self.public(record)
        with self.condition:
            view["active"] = identity in self.active
        return view

    def list(self) -> Record:
        tasks: list[Record] = []
        directory = self.root / "file-chains"
        if directory.exists():
            for entry in sorted(directory.iterdir()):
                if entry.is_symlink() or not re.fullmatch(r"chain-[0-9a-f]{64}", entry.name):
                    continue
                try:
                    tasks.append(self.query(entry.name))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        return {"tasks": tasks}

    def resume(self, identity: str) -> Record:
        self.query(identity)
        with self.condition:
            if self.stopping:
                raise ValueError("server stopping")
            if identity not in self.active:
                if self.active:
                    raise ValueError("task worker busy; try resume later")
                self.active.add(identity)
                threading.Thread(target=self.run, args=(identity,), daemon=True).start()
        return self.query(identity)

    def run(self, identity: str) -> None:
        try:
            chain.resume_semantic(self.root, identity, generate=self.generate, lookup=self.lookup)
        except Exception:
            pass  # Durable chain/launcher state remains authoritative, not a fake success.
        finally:
            with self.condition:
                self.active.discard(identity)
                self.condition.notify_all()

    def download(self, identity: str) -> bytes:
        record = chain.query(self.root, identity)
        if record["schema_version"] != 2 or record["status"] != "completed":
            raise ValueError("task not completed")
        with chain.files.root_handle(self.root) as root, chain.workspace(root, identity) as work:
            blob = chain.files.read(work, "candidate.md", 8 * chain.files.MAX_INPUT)
        if chain.files.digest(blob) != record["output_sha256"]:
            raise ValueError("output changed")
        return blob

    def stop(self, timeout: float) -> bool:
        with self.condition:
            self.stopping = True
            return self.condition.wait_for(lambda: not self.active, timeout=max(0, timeout))
