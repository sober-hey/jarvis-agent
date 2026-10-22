"""Read-only owned resource evaluation; never opens the real Messages DB."""

from __future__ import annotations

import importlib
import sys
import unittest
from unittest.mock import Mock, patch

from test_vision import assert_evaluation


class IMessageInputTests(unittest.TestCase):
    def test_owned_hook_reports_external_ingress_without_claiming_wiring(self):
        module = importlib.import_module("imessage_input")
        report = assert_evaluation(self, module.self_evaluate, "imessage_input")
        text = " ".join(report["known_limitations"])
        self.assertIn("daemon_wiring", text)
        self.assertIn("未接入", text)
        self.assertIn("附件", text)
        self.assertIn("KG", text)
        self.assertIn("只读", report["current_version"])

    def test_import_does_not_start_daemon_or_read_messages(self):
        with (
            patch("sqlite3.connect", side_effect=AssertionError("private DB read")),
            patch("subprocess.run", side_effect=AssertionError("external action")),
            patch.dict(sys.modules, {"daemon_wiring": None}),
        ):
            module = importlib.import_module("imessage_input")
            importlib.reload(module)
        self.assertNotIn("daemon_wiring", module.__dict__)
        self.assertEqual(module.self_evaluate()["module"], "imessage_input")


class ExistingIMessageIngressTests(unittest.TestCase):
    """Exercise existing owner against synthetic DBs, without starting Wiring."""

    def setUp(self):
        import os
        import sqlite3
        import tempfile
        from contextlib import closing, contextmanager
        from pathlib import Path
        from types import SimpleNamespace
        import daemon_wiring

        self.temp = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"], prefix="imessage-input-")
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "fixture-messages.db"
        self.ledger = Path(self.temp.name) / "fixture-inbox.db"
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("CREATE TABLE handle(id TEXT)")
            db.executemany("INSERT INTO handle VALUES(?)", [("fixture-sender",), ("other-sender",)])
            db.execute(
                "CREATE TABLE message(text TEXT,date INTEGER,handle_id INTEGER,account TEXT,is_from_me INTEGER,service TEXT)"
            )
        with closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute("CREATE TABLE once_keys(key TEXT PRIMARY KEY)")
            db.execute("CREATE TABLE inbox(id TEXT PRIMARY KEY,payload TEXT,delivered INTEGER)")

        @contextmanager
        def connect():
            with closing(sqlite3.connect(self.ledger)) as db, db:
                yield db

        self.obj = SimpleNamespace(fixture=False, connect=connect, log=Mock())
        self.poll = lambda: daemon_wiring.Wiring._poll_imessage(self.obj)
        for name, value in [
            ("IMESSAGE_DB", self.source),
            ("IMESSAGE_ACCOUNT", "fixture-account"),
            ("IMESSAGE_SIR", "fixture-sender"),
        ]:
            mocked = patch.object(daemon_wiring, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def seed(self, text, *, handle=1, account="fixture-account", mine=0, service="iMessage"):
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute(
                "INSERT INTO message VALUES(?,?,?,?,?,?)",
                (text, 1000000000, handle, account, mine, service),
            )

    def inbox(self):
        import json
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(self.ledger)) as db:
            return [
                (row[0], json.loads(row[1]))
                for row in db.execute("SELECT id,payload FROM inbox ORDER BY id")
            ]

    def test_first_enable_latest_then_ordered_drain_and_replay_dedupe(self):
        import hashlib

        self.seed("stale instruction")
        self.seed("latest message")
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.assertEqual(self.poll(), 1)
        self.assertEqual(self.inbox()[0][1]["text"], "latest message")
        self.assertEqual(self.poll(), 0)
        self.seed("new A")
        self.seed("new B")
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.assertEqual(self.poll(), 2)
        self.assertEqual(
            [row[1]["text"] for row in self.inbox()], ["latest message", "new A", "new B"]
        )
        self.assertEqual(self.poll(), 0)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        from datetime import datetime

        self.assertTrue(
            all(
                datetime.fromisoformat(row[1]["timestamp"]).utcoffset() is not None
                for row in self.inbox()
            )
        )

    def test_account_sender_direction_service_and_empty_text_filters(self):
        self.seed("accepted")
        self.seed("wrong sender", handle=2)
        self.seed("wrong account", account="other-account")
        self.seed("outgoing", mine=1)
        self.seed("SMS", service="SMS")
        self.seed(None)
        self.seed("   ")
        self.assertEqual(self.poll(), 1)
        self.assertEqual([r[1]["text"] for r in self.inbox()], ["accepted"])
        self.assertEqual(self.poll(), 0)

    def test_fixture_mode_and_schema_failure_do_not_ingest(self):
        import sqlite3
        from contextlib import closing

        self.obj.fixture = True
        with patch(
            "sqlite3.connect", side_effect=AssertionError("fixture must never read mailbox")
        ):
            self.assertEqual(self.poll(), 0)
        self.obj.fixture = False
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("DROP TABLE message")
        self.assertEqual(self.poll(), 0)
        self.obj.log.assert_called_once_with("imessage_poll_error", error_type="OperationalError")
        self.assertEqual(self.inbox(), [])


if __name__ == "__main__":
    unittest.main()
