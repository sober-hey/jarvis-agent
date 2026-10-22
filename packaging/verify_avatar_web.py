"""Offline browser acceptance fixture. Private stdin controls only, no HTTP writes.

Never use this fixture as the resident product entrypoint. It exercises the real
web DevServer and real AffectEngine against disposable state without a model.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from affect_engine import AffectEngine
from inner_loop import MindStore
from web_server import DevServer, launcher


def main() -> None:
    # Importing test configuration allocates evidence: keep it behind the explicit
    # fixture entrypoint, never the read-only module evaluation/import boundary.
    from test_chat_launcher import config

    root = launcher.prepare_root(Path(sys.argv[1]) / "data")
    affect = AffectEngine(root)
    MindStore(root)
    heartbeat = root / "daemon.heartbeat"
    heartbeat.write_text(json.dumps({"wall_time": time.time(), "phase": "idle"}))
    server = DevServer(0, config(8000), "FAKE-avatar-acceptance", root, 8000)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    print(json.dumps({"url": server.origin, "port": server.server_port}), flush=True)
    try:
        for line in sys.stdin:
            command = json.loads(line)["command"]
            if command == "priority":
                affect.handle("胸痛", is_task=False)
                heartbeat.write_text(json.dumps({"wall_time": time.time(), "phase": "think"}))
                server.avatar_voice_level("assistant", 0.85)
            elif command == "silence":
                server.avatar_voice_level("assistant", 0)
            elif command == "stop":
                break
            else:
                raise ValueError("unknown fixture command")
            print(json.dumps({"ack": command}), flush=True)
    finally:
        clean = server.stop(timeout=5)
        worker.join(3)
        print(json.dumps({"clean": clean, "connections": server.avatar_connections}), flush=True)


def self_evaluate() -> dict[str, object]:
    """Inspect fixture dependencies without running its server/state entrypoint."""
    from datetime import datetime, timezone

    return {
        "module": "verify_avatar_web",
        "domain": "disposable browser avatar acceptance fixture dependency inspection",
        "current_version": "1",
        "version_scope": "offline evaluation contract, not browser acceptance results",
        "known_limitations": [
            "Fixture only: not a resident product entrypoint or daemon.",
            "Dependency inspection does not execute browser, HTTP, affect or voice acceptance.",
            "Explicit main() creates disposable state and starts loopback HTTP; this hook never calls it.",
        ],
        "improvement_directions": [
            "Keep browser acceptance evidence isolated and check cleanup in the explicit fixture runner."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_verified": False,
        "scope": "offline fixture dependency inspection only",
        "fixture_only": True,
        "entrypoint_callable": callable(main),
        "dependency_callables": {
            "AffectEngine": callable(AffectEngine),
            "MindStore": callable(MindStore),
            "DevServer": callable(DevServer),
        },
    }


if __name__ == "__main__":
    main()
