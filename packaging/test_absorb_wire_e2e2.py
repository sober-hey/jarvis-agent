"""Capture actual post-revocation context using real-LLM-produced acceptance stores.

Controlled local HTTP provider only: this does not test model understanding.
The original real-provider acceptance stores remain read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import chat_launcher
from product_memory import MemoryStore


def capture(source_root: Path, target: Path, output: Path) -> dict[str, Any]:
    assert source_root.parent == Path("/private/tmp")
    assert source_root.name.startswith("jarvis-acceptance-")
    assert target.parent == source_root
    target.mkdir(mode=0o700)
    engine = target / "engine-home"
    engine.mkdir(mode=0o700)
    original = source_root / "product"
    for relative in ("memory.db", "engine-home/state.db"):
        # SQLite backup gives an exact logical snapshot without modifying source.
        with sqlite3.connect((original / relative).as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(target / relative) as dst:
                src.backup(dst)
    saved = original / "timeline-session.json"
    if saved.exists():
        shutil.copy2(saved, target / saved.name)
    session = json.loads((source_root / "evidence/04-first-real-chat.json").read_text())[
        "session_id"
    ]
    assert session.startswith("jarvis-timeline-")
    with sqlite3.connect(engine / "state.db") as db:
        history = [r[0] for r in db.execute("SELECT content FROM messages WHERE role='assistant'")]
    assert any("纸鸢编号Z9" in text and "铜叶茶" in text for text in history)
    assert all(
        not any(token in fact["content"] for token in ("纸鸢编号Z9", "铜叶茶"))
        for fact in MemoryStore(target / "memory.db").list()
    )
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps(
                {
                    "id": "controlled-context-capture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fake-context-capture",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "受控 HTTP 上下文已捕获。"},
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        route = {
            "model": "fake-context-capture",
            "provider": "custom",
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "api_mode": "chat_completions",
            "context_length": 256000,
            "reasoning_effort": "high",
        }
        response = chat_launcher.launch(
            route,
            "FAKE-NONSECRET-CONTEXT-CAPTURE",
            "档案记载的居住城市、饮品偏好与收藏编号分别是什么？请逐项回答。",
            target,
            session,
            test_port=server.server_port,
            timeout=65,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert len(requests) == 1
    wire = json.dumps(requests[0], ensure_ascii=False)
    for retired in ("纸鸢编号Z9", "铜叶茶", "雾汀市"):
        assert retired not in wire, retired
    assert "银杏茶" in wire and "霁海市" in wire
    assert response["ok"] and not response["generation_degraded"]
    assert response["memory_sources"]
    result = {
        "passed": True,
        "scope": "controlled HTTP context capture from real-extracted stores",
        "real_model_calls": 0,
        "history_retained_old_values": True,
        "outgoing_context_excludes_revoked_values": True,
        "uses_same_continuous_timeline": True,
        "request_count": len(requests),
        "source_root": str(source_root),
        "requests": requests,
        "response": response,
    }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k not in {"requests", "response"}}))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    capture(
        args.root,
        args.root / "post-revocation-wire-copy",
        args.root / "evidence/12-real-stores-controlled-wire.json",
    )
