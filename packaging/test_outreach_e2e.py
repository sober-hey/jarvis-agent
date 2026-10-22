"""Offline integration through the real mind/router/timeline and loopback SSE."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import daemon_wiring
from jarvis_daemon import CST
from test_chat_launcher import config
from test_web_server import web


class ProactiveE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="jarvis-acceptance-outreach-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.root.mkdir(mode=0o700)
        self.now = datetime(2026, 10, 16, 12, tzinfo=CST).timestamp()
        self.calls: list[dict[str, Any]] = []

    def model(self, prompt: str) -> dict[str, Any]:
        context = json.loads(prompt)
        self.calls.append(context)
        if "outreach_intent" in context:
            return {
                "text": json.dumps({"text": "FAKE:" + context["outreach_intent"]["id"]}),
                "generation_degraded": False,
            }
        event = next(e for e in context["events"] if e["source"] == "offline-evidence")
        return {
            "text": json.dumps(
                {
                    "thoughts": [
                        {
                            "id": "t1",
                            "kind": "inference",
                            "text": "synthetic concern",
                            "refs": [event["id"]],
                            "question": None,
                        }
                    ],
                    "resolved_questions": [],
                    "outreach_intents": [
                        {
                            "reason": "synthetic factual change",
                            "evidence": [event["id"]],
                            "urgency": "normal",
                            "needs_response": False,
                        }
                    ],
                }
            )
        }

    def wiring(self) -> daemon_wiring.Wiring:
        return daemon_wiring.Wiring(self.root, self.model, fixture=True, clock=lambda: self.now)

    def seed(self, wiring: daemon_wiring.Wiring) -> None:
        wiring.mind.ingest("tool_result", {"synthetic": True}, source="offline-evidence")

    def test_mind_intent_reaches_one_timeline_draft_and_no_external_transport(self) -> None:
        wiring = self.wiring()
        self.seed(wiring)
        with (
            patch("outreach.send_email", side_effect=AssertionError("external transport")),
            patch.object(daemon_wiring.launcher, "read_key", side_effect=AssertionError("key")),
            patch.object(daemon_wiring.launcher, "launch", side_effect=AssertionError("model")),
        ):
            self.assertTrue(wiring.think())
            intents = wiring.mind.store.recent("outreach_intent", 10)
            self.assertEqual(len(intents), 1, "mind must emit a durable structured intent")
            wiring.contact()
            wiring.dispatch()
        rows = daemon_wiring.launcher.read_timeline(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_channel"], "jarvis_proactive")
        self.assertFalse(rows[0]["generation_degraded"])
        self.assertTrue(rows[0]["evidence"])
        self.assertFalse(rows[0]["needs_response"])
        from email import policy
        from email.parser import BytesParser

        drafts = list((self.root / "outbox").glob("*.eml"))
        self.assertEqual(len(drafts), 1)
        mail = BytesParser(policy=policy.default).parsebytes(drafts[0].read_bytes())
        self.assertEqual(mail["X-JARVIS-Outbox-Only"], "yes")
        self.assertEqual(mail.get_content().strip(), rows[0]["content"].strip())
        self.assertEqual(len(self.calls), 2)
        restarted = self.wiring()
        restarted.contact()
        restarted.dispatch()
        self.assertEqual(daemon_wiring.launcher.read_timeline(self.root), rows)
        self.assertEqual(len(list((self.root / "outbox").glob("*.eml"))), 1)
        self.assertEqual(len(self.calls), 2)

    def test_timeline_publish_crash_replays_without_duplicate(self) -> None:
        wiring = self.wiring()
        self.seed(wiring)
        wiring.think()
        wiring.contact()
        with patch.object(wiring, "publish_deliveries", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                wiring.dispatch()
        self.assertEqual(daemon_wiring.launcher.read_timeline(self.root), [])
        restarted = self.wiring()
        restarted.dispatch()
        self.assertEqual(len(daemon_wiring.launcher.read_timeline(self.root)), 1)
        with restarted.connect() as db:
            db.execute("DELETE FROM once_keys WHERE key LIKE 'published:%'")
        restarted.publish_deliveries()
        self.assertEqual(len(daemon_wiring.launcher.read_timeline(self.root)), 1)

    def test_crash_before_mind_intent_commit_recovers_response_without_model_retry(self) -> None:
        wiring = self.wiring()
        self.seed(wiring)
        original = wiring.mind.store.append

        def crash(
            category: str, payload: dict[str, Any], *, source: str, refs: list[str] | None = None
        ) -> dict[str, Any]:
            if category == "outreach_intent":
                raise KeyboardInterrupt
            return original(category, payload, source=source, refs=refs)

        with patch.object(wiring.mind.store, "append", new=crash):
            with self.assertRaises(KeyboardInterrupt):
                wiring.think()
        self.assertEqual(wiring.mind.store.recent("outreach_intent", 10), [])
        restarted = self.wiring()
        restarted.contact()
        restarted.dispatch()
        self.assertEqual(len(daemon_wiring.launcher.read_timeline(self.root)), 1)
        self.assertEqual(len(self.calls), 2, "recovery must not repeat the thought model call")

    def test_model_failure_is_marked_in_timeline_and_draft(self) -> None:
        wiring = self.wiring()
        self.seed(wiring)
        wiring.think()
        wiring.contact()

        def failed(_prompt: str) -> dict[str, Any]:
            raise OSError("fake failure")

        wiring.raw_model = failed
        wiring.dispatch()
        rows = daemon_wiring.launcher.read_timeline(self.root)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["generation_degraded"])
        self.assertEqual(rows[0]["content"], rows[0]["reason"])
        draft = next((self.root / "outbox").glob("*.eml")).read_text()
        self.assertIn("X-JARVIS-Generation-Degraded: yes", draft)

    def test_local_sse_streams_authoritative_timeline_and_rejects_cross_origin(self) -> None:
        server = web.DevServer(0, config(8000), "FAKE-test", self.root, 8000)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=8)
        try:
            connection.request("GET", "/api/timeline/events", headers={"Origin": server.origin})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.getheader("Content-Type", ""))
            wiring = self.wiring()
            self.seed(wiring)
            wiring.think()
            wiring.contact()
            wiring.dispatch()
            streamed = None
            for _ in range(30):
                line = response.fp.readline().decode()  # type: ignore[union-attr]
                if line.startswith("data:"):
                    payload = json.loads(line.removeprefix("data:"))
                    if payload["entries"]:
                        streamed = payload
                        break
            self.assertIsNotNone(streamed)
            assert streamed is not None
            self.assertEqual(streamed["timeline_id"], wiring.timeline_id)
            self.assertEqual(streamed["entries"], daemon_wiring.launcher.read_timeline(self.root))
            other = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                other.request(
                    "GET", "/api/timeline/events", headers={"Origin": "https://evil.invalid"}
                )
                denied = other.getresponse()
                self.assertEqual(denied.status, 403)
                denied.read()
            finally:
                other.close()
        finally:
            connection.close()
            server.stop(timeout=3)
            thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_response_seeking_intent_cannot_bypass_question_review(self) -> None:
        from inner_runtime import validate

        value: dict[str, Any] = {
            "thoughts": [
                {"id": "t1", "kind": "idle", "text": "synthetic", "refs": [], "question": None}
            ],
            "resolved_questions": [],
            "outreach_intents": [
                {
                    "reason": "synthetic ask",
                    "evidence": ["source"],
                    "urgency": "normal",
                    "needs_response": True,
                }
            ],
        }
        with self.assertRaises(ValueError):
            validate(json.dumps(value), {"source"}, [], [])

    def test_presence_writer_closes_sqlite_connection(self) -> None:
        import sqlite3

        server = web.DevServer(0, config(8000), "FAKE-test", self.root, 8000)
        original = sqlite3.connect
        connections: list[sqlite3.Connection] = []

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            db = original(*args, **kwargs)
            connections.append(db)
            return db

        try:
            with patch.object(sqlite3, "connect", side_effect=connect):
                server.note_presence()
            self.assertEqual(len(connections), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                connections[0].execute("SELECT 1")
        finally:
            for db in connections:
                db.close()
            server.server_close()

    def test_loopback_provider_public_tick_to_timeline_sse_and_draft(self) -> None:
        import smtplib
        from email import policy
        from email.parser import BytesParser
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from jarvis_daemon import Daemon

        author = self.model
        requests: list[dict[str, Any]] = []

        class Provider(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                generated = author(request["messages"][-1]["content"])
                data = json.dumps(
                    {
                        "id": "fake-outreach",
                        "model": "fake-test-model",
                        "object": "chat.completion",
                        "created": 1,
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": generated["text"]},
                            }
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                pass

        provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        worker = threading.Thread(target=provider.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 3)
        self.addCleanup(provider.server_close)
        self.addCleanup(provider.shutdown)
        root = daemon_wiring.launcher.prepare_root(self.root)
        timeline = daemon_wiring.launcher.timeline_identity(root)
        model = daemon_wiring.StableModel(
            root,
            config(provider.server_port),
            "FAKE-NONSECRET",
            timeline,
            test_port=provider.server_port,
        )
        wiring = daemon_wiring.Wiring(root, model, fixture=False, clock=lambda: self.now)
        wiring.adapters = []  # Explicitly no real owner sensors or source probes.
        self.seed(wiring)
        with (
            patch.object(smtplib, "SMTP", side_effect=AssertionError("SMTP forbidden")) as smtp,
            patch.object(smtplib, "SMTP_SSL", side_effect=AssertionError("SMTP forbidden")) as ssl,
            patch("outreach.send_email", side_effect=AssertionError("external send")) as send,
            patch.object(daemon_wiring.launcher, "read_key", side_effect=AssertionError("key")),
        ):
            Daemon(wiring, clock=lambda: self.now).tick()
        smtp.assert_not_called()
        ssl.assert_not_called()
        send.assert_not_called()
        rows = daemon_wiring.launcher.read_timeline(root)
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_channel"], "jarvis_proactive")
        self.assertFalse(rows[0]["generation_degraded"])
        drafts = list((root / "outbox").glob("*.eml"))
        self.assertEqual(len(drafts), 1)
        draft = BytesParser(policy=policy.default).parsebytes(drafts[0].read_bytes())
        self.assertEqual(draft["X-JARVIS-Outbox-Only"], "yes")
        self.assertEqual(draft.get_content().strip(), rows[0]["content"])
        server = web.DevServer(
            0, config(provider.server_port), "FAKE-NONSECRET", root, provider.server_port
        )
        stream_worker = threading.Thread(target=server.serve_forever, daemon=True)
        stream_worker.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("GET", "/api/timeline/events")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            lines: list[str] = []
            for _ in range(10):
                line = response.readline().decode()
                lines.append(line)
                if line.startswith("data: "):
                    break
            payload = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
            self.assertEqual(payload["timeline_id"], timeline)
            self.assertEqual(payload["entries"], rows)
        finally:
            connection.close()
            server.stop(timeout=3)
            stream_worker.join(3)
        restarted = daemon_wiring.Wiring(root, model, fixture=False, clock=lambda: self.now)
        restarted.contact()
        restarted.dispatch()
        self.assertEqual(daemon_wiring.launcher.read_timeline(root), rows)
        self.assertEqual(len(requests), 2)
        self.acceptance = {
            "timeline": rows,
            "requests": requests,
            "sse_lines": lines,
            "draft": drafts[0].read_text(),
            "real_external_sends": 0,
            "restart_no_duplicate": True,
            "public_tick": True,
        }

    def test_reviewed_question_evidence_resolves_and_reaches_author(self) -> None:
        wiring = self.wiring()
        self.seed(wiring)
        source = wiring.mind.store.recent("event", 1)[0]
        wiring.mind.state(
            "scheduler:synthetic-question",
            "synthetic reviewed question",
            "ask_sir",
            refs=[source["id"]],
            detail="synthetic missing information",
        )
        wiring.contact()
        wiring.dispatch()
        rows = daemon_wiring.launcher.read_timeline(self.root)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["needs_response"])
        self.assertFalse(rows[0]["generation_degraded"])
        self.assertEqual(len(self.calls), 1)
        evidence = self.calls[0]["evidence"]
        self.assertEqual(evidence[0]["category"], "question_state")
        self.assertEqual(evidence[0]["refs"], [source["id"]])
        self.assertEqual(evidence[0]["payload"]["question_id"], "scheduler:synthetic-question")

    def test_belief_provenance_reaches_outward_model_with_values_and_source(self) -> None:
        wiring = self.wiring()
        belief = wiring.mind.store.append_belief(
            belief_id="synthetic-belief",
            subject="synthetic Sir",
            attribute="synthetic status",
            value="synthetic recorded value",
            source="self_report",
            confidence=0.6,
            last_verified=datetime.fromtimestamp(self.now, CST).isoformat(),
            importance=0.8,
        )
        observed: list[dict[str, Any]] = []

        def author(prompt: str) -> dict[str, Any]:
            observed.append(json.loads(prompt))
            return {"text": json.dumps({"text": "synthetic authored statement"})}

        wiring.raw_model = author
        result = wiring.render_outreach(
            {
                "id": "belief-intent",
                "reason": "synthetic reason",
                "urgency": "normal",
                "needs_response": False,
                "evidence": [belief["belief_id"]],
            }
        )
        self.assertFalse(result["generation_degraded"])
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["evidence"], [belief])

    def test_missing_or_deleted_evidence_does_not_reach_outward_model(self) -> None:
        wiring = self.wiring()
        fact = wiring.mind.memories.add("synthetic remembered fact", "fake", "fake")
        wiring.mind.memories.delete(fact["id"])
        with patch.object(wiring, "model", side_effect=AssertionError("author must not run")):
            for identity in ("missing-source", "memory:" + fact["id"]):
                with self.subTest(identity=identity):
                    with self.assertRaises(ValueError):
                        wiring.render_outreach(
                            {
                                "id": "stale",
                                "reason": "synthetic",
                                "urgency": "normal",
                                "needs_response": False,
                                "evidence": [identity],
                            }
                        )

    def test_intent_requires_real_provenance(self) -> None:
        from inner_runtime import validate

        value: dict[str, Any] = {
            "thoughts": [
                {"id": "t1", "kind": "idle", "text": "synthetic", "refs": [], "question": None}
            ],
            "resolved_questions": [],
            "outreach_intents": [
                {
                    "reason": "synthetic",
                    "evidence": ["invented"],
                    "urgency": "emergency",
                    "needs_response": False,
                }
            ],
        }
        with self.assertRaises(ValueError):
            validate(json.dumps(value), {"source"}, [], [])


if __name__ == "__main__":
    unittest.main()
