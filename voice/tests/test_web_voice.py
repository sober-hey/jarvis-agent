"""Voice HTTP routes: real loopback requests, isolated stores, no paid calls."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import web_server as web
from test_chat_launcher import config


class WebVoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        # Product root guards deliberately reject the host Hermes scratch tree.
        self.folder = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".jarvis-web-voice-")
        self.root = web.launcher.prepare_root(Path(self.folder.name) / "data")
        self.server = web.DevServer(0, config(8123), "FAKE-test", self.root, 8123)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self) -> None:
        self.server.stop(timeout=3)
        self.worker.join(3)
        self.folder.cleanup()

    def request(
        self, method: str, path: str, body: object = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(
                method,
                path,
                json.dumps(body) if body is not None else None,
                headers
                if headers is not None
                else {"Origin": self.server.origin, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_playback_missing_ledger_is_explicitly_unavailable_without_model_call(self) -> None:
        with (
            patch.object(self.server, "get_voice_ledger", return_value=None, create=True),
            patch.object(web.launcher, "launch") as launch,
        ):
            self.assertEqual(
                self.request(
                    "POST",
                    "/api/voice/playback",
                    {
                        "turn_id": "turn-1",
                        "status": "started",
                        "assistant_id": "answer-1",
                        "device_id": "browser",
                    },
                ),
                (503, {"error": "voice_ledger_unavailable"}),
            )
            launch.assert_not_called()

    def seed(self, text: str = "测试转写") -> tuple[Any, str, str]:
        ledger = self.server.get_voice_ledger()
        user = ledger.record_user(
            text, device_id="browser", asr_confidence=None, audio_duration_ms=100
        )
        turn = user["payload"]["turn_id"]
        answer = ledger.record_assistant(turn, "测试回答", generation_ms=1)
        return ledger, turn, answer["id"]

    def start(self, turn: str, answer: str) -> dict[str, Any]:
        status, response = self.request(
            "POST",
            "/api/voice/playback",
            {"status": "started", "turn_id": turn, "assistant_id": answer, "device_id": "browser"},
        )
        self.assertEqual(status, 201, response)
        return response

    def test_playback_records_real_ledger_and_interrupt_does_not_cancel_tasks(self) -> None:
        ledger, turn, answer = self.seed()
        with (
            patch.object(web.launcher, "launch") as launch,
            patch.object(self.server, "chat_request") as chat,
        ):
            started = self.start(turn, answer)
            playback = started["playback_id"]
            status, interrupted = self.request(
                "POST",
                "/api/voice/playback",
                {
                    "status": "interrupted",
                    "turn_id": turn,
                    "playback_id": playback,
                    "interrupted_by": "user",
                    "device_id": "browser",
                },
            )
            self.assertEqual(status, 201, interrupted)
            projection = ledger.get_turn(turn)
            self.assertEqual(projection["playback"][0]["status"], "playing")
            self.assertIsNone(projection["playback"][0]["ended_at"])
            self.assertTrue(projection["playback"][0]["interrupted"])
            status, ended = self.request(
                "POST",
                "/api/voice/playback",
                {"status": "ended", "turn_id": turn, "playback_id": playback, "interrupted": True},
            )
            self.assertEqual(status, 201, ended)
            self.assertEqual(ledger.get_turn(turn)["playback"][0]["status"], "ended")
            launch.assert_not_called()
            chat.assert_not_called()
        timeline_status, timeline = self.request("GET", "/api/voice/timeline")
        self.assertEqual(timeline_status, 200)
        events = timeline["turns"][0]["events"]
        self.assertEqual(
            [event["payload"]["event"] for event in events],
            ["transcript", "response", "start", "interrupt", "end"],
        )
        # Fresh reader proves append persistence, not merely a shared cache.
        import voice_ledger

        reopened = voice_ledger.VoiceLedger(voice_ledger.MindStore(self.root))
        detached = reopened.get_turn(turn)
        assert detached is not None
        self.assertEqual(detached["events"], events)
        self.assertEqual(list(self.server.bindings.iterdir()), [])

    def test_timeline_is_bounded_newest_first_and_does_not_run_models(self) -> None:
        from datetime import UTC, datetime, timedelta

        ledger = self.server.get_voice_ledger()
        turns: list[str] = []
        timestamp = datetime(2026, 1, 1, tzinfo=UTC)
        for index in range(55):
            row = ledger.record_user(
                str(index),
                device_id="browser",
                asr_confidence=None,
                audio_duration_ms=1,
                timestamp=(timestamp + timedelta(seconds=index)).isoformat(),
            )
            turns.append(row["payload"]["turn_id"])
        with patch.object(web.launcher, "launch") as launch:
            status, result = self.request("GET", "/api/voice/timeline")
            self.assertEqual(status, 200)
            self.assertEqual(result["limit"], 50)
            self.assertEqual([turn["turn_id"] for turn in result["turns"]], turns[::-1][:50])
            status, result = self.request("GET", "/api/voice/timeline?limit=2")
            self.assertEqual(status, 200)
            self.assertEqual([turn["turn_id"] for turn in result["turns"]], turns[::-1][:2])
            launch.assert_not_called()

    def test_timeline_rejects_invalid_queries(self) -> None:
        for query in (
            "limit=0",
            "limit=-1",
            "limit=201",
            "limit=x",
            "limit=",
            "limit=1&limit=2",
            "other=1",
            "limit=1.5",
            "limit=2&other=3",
            "bad",
            "limit=999999",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", "/api/voice/timeline?" + query)[0], 400)
        for path in ("/api/voice/status?x=1", "/api/voice/stream?limit=2"):
            self.assertEqual(self.request("GET", path)[0], 400)

    def test_status_is_allowlisted_cached_and_unknown_until_connected(self) -> None:
        import urllib.request

        with (
            patch.object(urllib.request, "urlopen", side_effect=AssertionError("no cloud probes")),
            patch.object(web.launcher, "launch") as launch,
        ):
            status, result = self.request("GET", "/api/voice/status")
            self.assertEqual(status, 200)
            self.assertIsNone(result["gateway_running"])
            self.assertIsNone(result["bailian_key_valid"])
            self.assertIsNone(result["last_call_at"])
            self.assertEqual(result["status_source"], "not_connected")
            observed = {
                "gateway_running": True,
                "bailian_key_valid": False,
                "last_call_at": "2026-10-20T12:00:00+00:00",
                "api_key": "DO_NOT_EXPOSE",
            }
            self.server.voice_status_reader = lambda: observed
            status, result = self.request("GET", "/api/voice/status")
            self.assertEqual(status, 200)
            self.assertIs(result["gateway_running"], True)
            self.assertIs(result["bailian_key_valid"], False)
            self.assertEqual(result["last_call_at"], observed["last_call_at"])
            self.assertNotIn("DO_NOT_EXPOSE", json.dumps(result))
            launch.assert_not_called()
        self.server.voice_status_reader = lambda: {"gateway_running": "True"}
        self.assertEqual(
            self.request("GET", "/api/voice/status"),
            (503, {"error": "voice_subsystem_unavailable"}),
        )

    def test_voice_routes_use_existing_origin_and_host_guards(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        paths = ("/api/voice/status", "/api/voice/timeline", "/api/voice/stream")
        for headers in (
            {"Host": "evil.example"},
            {"Origin": "http://evil.example"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
        ):
            for path in paths:
                self.assertEqual(self.request("GET", path, headers=headers)[0], 403)
            self.assertEqual(self.request("POST", "/api/voice/playback", {}, headers)[0], 403)
        self.assertEqual(self.request("POST", "/api/voice/playback", {}, {})[0], 403)
        self.assertEqual(self.request("GET", "/api/voice/status", headers={})[0], 200)

    def test_playback_invalid_fields_and_cross_turn_refs_do_not_append(self) -> None:
        ledger, turn, answer = self.seed()
        _, other, other_answer = self.seed("另一个转写")
        base = {
            "status": "started",
            "turn_id": turn,
            "assistant_id": answer,
            "device_id": "browser",
        }
        initial = len(ledger.get_turn(turn)["events"])
        invalid_bodies: tuple[object, ...] = (
            [],
            {},
            {**base, "status": "bad"},
            {**base, "turn_id": ""},
            {**base, "device_id": "x" * 201},
            {**base, "text": "not_allowed"},
            {**base, "assistant_id": other_answer},
            {**base, "turn_id": other},
            {**base, "timestamp": "2026-01-01"},
            {"status": "ended", "turn_id": turn, "playback_id": "missing"},
            {"status": "ended", "turn_id": turn, "playback_id": "missing", "interrupted": "true"},
        )
        for body in invalid_bodies:
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/voice/playback", body)[0], 400)
        self.assertEqual(len(ledger.get_turn(turn)["events"]), initial)
        self.assertEqual(
            self.request("POST", "/api/voice/playback", base, {"Origin": self.server.origin})[0],
            415,
        )
        self.assertEqual(
            self.request(
                "POST",
                "/api/voice/playback",
                base,
                {
                    "Origin": self.server.origin,
                    "Content-Type": "application/json",
                    "Content-Length": "8193",
                },
            )[0],
            413,
        )

    def test_duplicate_json_and_http_headers_rejected(self) -> None:
        for duplicate in ("Content-Length", "Content-Type", "Origin", "Host", "Sec-Fetch-Site"):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            try:
                connection.putrequest("POST", "/api/voice/playback", skip_host=True)
                headers = {
                    "Host": self.server.authority,
                    "Origin": self.server.origin,
                    "Content-Type": "application/json",
                    "Content-Length": "2",
                    "Sec-Fetch-Site": "same-origin",
                }
                for key, value in headers.items():
                    connection.putheader(key, value)
                connection.putheader(duplicate, headers[duplicate])
                connection.endheaders(b"{}")
                response = connection.getresponse()
                self.assertIn(response.status, (400, 403, 415))
                response.read()
            finally:
                connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(
                "POST",
                "/api/voice/playback",
                '{"status":"started","status":"ended"}',
                {"Origin": self.server.origin, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            connection.close()

    def test_deep_json_body_rejected_as_json_error(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(
                "POST",
                "/api/voice/playback",
                "[" * 4000 + "0" + "]" * 4000,
                {"Origin": self.server.origin, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.read()), {"error": "invalid_request"})
        finally:
            connection.close()

    def test_stream_cursor_rejects_sqlite_overflow(self) -> None:
        self.assertEqual(
            self.request(
                "GET", "/api/voice/stream", headers={"Last-Event-ID": "99999999999999999999"}
            ),
            (400, {"error": "invalid_event_id"}),
        )

    def test_stream_backlog_includes_old_turn_late_responses(self) -> None:
        ledger, old_turn, _ = self.seed()
        initial = ledger.get_turn(old_turn)["events"][-1]["seq"]
        expected: list[int] = []
        for index in range(105):
            row = ledger.record_user(
                str(index), device_id="browser", asr_confidence=None, audio_duration_ms=1
            )
            expected.append(ledger.store.get(row["id"])["seq"])
        late = ledger.record_assistant(old_turn, "晚到的回答", generation_ms=1)
        expected.append(ledger.store.get(late["id"])["seq"])
        first = self.server.voice_transcript_events(ledger, initial)
        second = self.server.voice_transcript_events(ledger, first[-1]["seq"])
        self.assertEqual([row["seq"] for row in first + second], expected)
        self.assertEqual(second[-1]["payload"]["text"], "晚到的回答")

    def test_unavailable_and_backend_failures_are_sanitized(self) -> None:
        with patch.object(self.server, "get_voice_ledger", return_value=None):
            for path in ("/api/voice/timeline", "/api/voice/stream"):
                self.assertEqual(
                    self.request("GET", path), (503, {"error": "voice_ledger_unavailable"})
                )
        with patch.object(self.server, "get_voice_ledger", side_effect=OSError("secret-key")):
            status, value = self.request("GET", "/api/voice/timeline")
            self.assertEqual(status, 503)
            self.assertNotIn("secret-key", json.dumps(value))

    def test_stream_transmits_committed_transcript_updates_and_drains_on_stop(self) -> None:
        ledger, _, _ = self.seed()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("GET", "/api/voice/stream")
        response = connection.getresponse()
        try:
            self.assertEqual(response.status, 200)
            self.assertTrue(response.getheader("Content-Type", "").startswith("text/event-stream"))
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

            def event() -> dict[str, Any]:
                lines: list[str] = []
                while True:
                    line = response.readline().decode().strip()
                    if not line:
                        if any(value.startswith("data: ") for value in lines):
                            return json.loads(
                                next(value[6:] for value in lines if value.startswith("data: "))
                            )
                        lines = []
                    else:
                        lines.append(line)

            self.assertEqual(event()["text"], "测试转写")
            self.assertEqual(event()["role"], "assistant")
            ledger.record_user(
                "后续实时转写", device_id="browser", asr_confidence=None, audio_duration_ms=20
            )
            self.assertEqual(event()["text"], "后续实时转写")
            self.assertEqual(self.request("GET", "/api/health")[0], 200)
            self.assertTrue(self.server.stop(timeout=2))
            self.assertEqual(self.server.responses_inflight, 0)
            self.assertEqual(response.read(), b"")
        finally:
            response.close()
            connection.close()

    def test_stream_reconnect_filter_invalid_ids_and_slot_bound(self) -> None:
        ledger, turn, _ = self.seed()
        rows = ledger.get_turn(turn)["events"]
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(
                "GET", "/api/voice/stream", headers={"Last-Event-ID": str(rows[0]["seq"])}
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            line = ""
            while not line.startswith("data:"):
                line = response.readline().decode()
            self.assertEqual(json.loads(line[5:])["role"], "assistant")
            response.close()
        finally:
            connection.close()
        self.assertEqual(
            self.request(
                "GET", "/api/voice/stream", headers={"Last-Event-ID": "id: injected\u0000"}
            )[0],
            400,
        )
        # A held quota causes an explicit rejection, not another unbounded thread.
        self.server.avatar_stop.set()
        with self.server.condition:
            self.assertTrue(
                self.server.condition.wait_for(
                    lambda: self.server.responses_inflight == 0, timeout=2
                )
            )
        self.server.avatar_stop.clear()
        for _ in range(4):
            self.assertTrue(self.server.voice_slots.acquire(blocking=False))
        try:
            self.assertEqual(
                self.request("GET", "/api/voice/stream"), (503, {"error": "voice_stream_busy"})
            )
        finally:
            for _ in range(4):
                self.server.voice_slots.release()


if __name__ == "__main__":
    unittest.main()
