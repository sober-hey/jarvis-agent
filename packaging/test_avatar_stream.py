"""Real loopback SSE tests; no provider/model/credential access."""

from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

import web_server as web
from test_chat_launcher import config


class AvatarStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory(prefix=".jarvis-avatar-", dir=Path.home())
        self.root = web.launcher.prepare_root(Path(self.folder.name) / "data")
        self.server = web.DevServer(0, config(8000), "FAKE-avatar", self.root, 8000)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connections: list[http.client.HTTPConnection] = []

    def tearDown(self) -> None:
        for connection in self.connections:
            connection.close()
        self.server.stop(timeout=3)
        self.thread.join(3)
        self.folder.cleanup()

    def request(self, headers: dict[str, str] | None = None, method: str = "GET") -> Any:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.connections.append(connection)
        connection.request(method, "/api/avatar/stream", headers=headers or {})
        return connection.getresponse()

    def frame(self, response: Any) -> dict[str, Any]:
        event: dict[str, str] = {}
        while True:
            line = response.readline().decode().strip()
            if not line:
                if "data" in event:
                    payload = json.loads(event["data"])
                    self.assertEqual(event["event"], "avatar")
                    self.assertEqual(int(event["id"]), payload["sequence"])
                    return payload
                continue
            if ": " in line:
                name, value = line.split(": ", 1)
                event[name] = value

    def test_format_neutral_degraded_and_read_only(self) -> None:
        response = self.request()
        self.assertEqual(response.status, 200, "avatar SSE must exist")
        self.assertTrue(response.headers["Content-Type"].startswith("text/event-stream"))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
        frame = self.frame(response)
        self.assertEqual(frame["schema_version"], 1)
        self.assertTrue(frame["degraded"])
        self.assertIn("affect", frame["missing_sources"])
        self.assertIn("voice_level", frame["missing_sources"])
        self.assertEqual(frame["activity"], "idle")
        self.assertLess(abs(frame["timestamp"] - time.time()), 5)
        self.assertTrue(all(v == 0 for c in frame["channels"].values() for v in c.values()))
        self.assertFalse((self.root / "affect.db").exists())
        self.assertFalse((self.root / "mind.db").exists())
        response.close()
        rejected = self.request({"Origin": self.server.origin}, "POST")
        self.assertEqual(rejected.status, 404)
        rejected.read()

    def test_security_boundaries(self) -> None:
        for headers in (
            {"Host": "evil.example"},
            {"Origin": "http://evil.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            with self.subTest(headers=headers):
                response = self.request(headers)
                self.assertEqual(response.status, 403)
                response.read()

    def test_frequency_and_sequence(self) -> None:
        response = self.request({"Origin": self.server.origin, "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status, 200, "avatar SSE must exist")
        frames = [self.frame(response) for _ in range(6)]
        stamps = [frame["timestamp"] for frame in frames]
        self.assertGreaterEqual(stamps[-1] - stamps[0], 0.99)
        self.assertTrue(all(b - a >= 0.199 for a, b in zip(stamps, stamps[1:], strict=False)))
        sequences = [frame["sequence"] for frame in frames]
        self.assertEqual(sequences, sorted(set(sequences)))

    def test_connections_capped_and_released(self) -> None:
        streams = [self.request() for _ in range(4)]
        self.assertEqual([r.status for r in streams], [200] * 4)
        rejected = self.request()
        self.assertEqual(rejected.status, 503)
        rejected.read()
        for response in streams:
            response.close()
        deadline = time.monotonic() + 3
        while self.server.avatar_connections and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.server.avatar_connections, 0)
        accepted = self.request()
        self.assertEqual(accepted.status, 200)

    def test_real_affect_changes_and_voice_callback(self) -> None:
        from affect_engine import AffectEngine

        engine = AffectEngine(self.root)
        response = self.request()
        self.assertEqual(response.status, 200, "avatar SSE must exist")
        before = self.frame(response)
        engine.handle("胸痛", is_task=False)
        database = self.root / "affect.db"
        digest = hashlib.sha256(database.read_bytes()).hexdigest()
        self.server.avatar_voice_level("assistant", 0.8)
        deadline = time.monotonic() + 3
        after = self.frame(response)
        while after["action"] != "priority" and time.monotonic() < deadline:
            after = self.frame(response)
        self.assertGreater(
            after["channels"]["expression"]["care"], before["channels"]["expression"]["care"]
        )
        self.assertEqual(after["action"], "priority")
        self.assertEqual(after["activity"], "speaking")
        self.assertEqual(after["channels"]["mouth"]["open"], 0.8)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), digest)
        self.assertNotIn("胸痛", json.dumps(after, ensure_ascii=False))
        self.server.avatar_voice_level("assistant", 0)
        time.sleep(0.25)
        self.assertEqual(self.frame(response)["channels"]["mouth"]["open"], 0)

    def test_read_projection_freshness_and_real_intent(self) -> None:
        from affect_engine import AffectEngine
        from inner_loop import MindStore

        AffectEngine(self.root)
        store = MindStore(self.root)
        store.append("action", {"state": "intent", "private": "DO_NOT_EXPOSE"}, source="test")
        heartbeat = self.root / "daemon.heartbeat"
        heartbeat.write_text(json.dumps({"wall_time": time.time(), "phase": "think"}))
        frame = self.server.avatar_source.frame()
        self.assertEqual(frame["activity"], "thinking")
        self.assertEqual(frame["missing_sources"], ["voice_level"])
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(frame))
        self.server.avatar_voice_level("assistant", 1.5)
        self.assertFalse(self.server.avatar_source.frame()["degraded"])
        self.assertEqual(self.server.avatar_source.frame()["channels"]["mouth"]["open"], 1)
        self.server.avatar_voice_level("user", 0.1)
        self.assertEqual(self.server.avatar_source.frame()["channels"]["mouth"]["open"], 1)
        time.sleep(0.8)
        expired = self.server.avatar_source.frame()
        self.assertEqual(expired["channels"]["mouth"]["open"], 0)
        self.assertIn("voice_level", expired["missing_sources"])
        heartbeat.write_text(json.dumps({"wall_time": time.time() - 1000, "phase": "reflection"}))
        self.assertEqual(self.server.avatar_source.frame()["activity"], "thinking")
        heartbeat.write_text(json.dumps({"wall_time": time.time() - 2200, "phase": "think"}))
        self.assertEqual(self.server.avatar_source.frame()["activity"], "idle")
        heartbeat.write_text(json.dumps({"wall_time": time.time() - 700, "phase": "idle"}))
        self.assertEqual(self.server.avatar_source.frame()["activity"], "idle")
        with self.assertRaises(ValueError):
            self.server.avatar_voice_level("assistant", float("nan"))
        # Malformed source must degrade, without leaking or rewriting it.
        database = self.root / "affect.db"
        import sqlite3

        with sqlite3.connect(database) as db:
            db.execute("UPDATE snapshot SET state=?", ('{"private":"DO_NOT_EXPOSE"}',))
        original = database.read_bytes()
        degraded = self.server.avatar_source.frame()
        self.assertIn("affect", degraded["missing_sources"])
        self.assertEqual(database.read_bytes(), original)
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(degraded))

    def test_shutdown_closes_stream(self) -> None:
        response = self.request()
        self.assertEqual(response.status, 200, "avatar SSE must exist")
        self.frame(response)
        self.server.stop(timeout=3)
        self.thread.join(3)
        self.assertEqual(response.read(), b"")


if __name__ == "__main__":
    unittest.main()
