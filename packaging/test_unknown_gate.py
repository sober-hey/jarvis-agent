"""Unknown operations remain at-most-once without freezing fresh cognition."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from daemon_wiring import FixtureModel, StableModel, Wiring


class UnknownGateTests(unittest.TestCase):
    def setUp(self) -> None:
        # Ignore caller TMPDIR: host scratch and macOS aliases fail the product guard.
        self.tmp = tempfile.TemporaryDirectory(
            prefix="jarvis-fix-tests-unknown-", dir="/private/tmp"
        )
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "data"
        self.now = 1792137600.0
        self.wall = 1000.0
        self.wiring = self.make()

    def make(self) -> Wiring:
        import chat_launcher

        root = chat_launcher.prepare_root(self.root)
        return Wiring(root, FixtureModel(), fixture=True, clock=lambda: self.now)

    def test_new_readonly_call_survives_unknown(self) -> None:
        def unknown(prompt: str) -> dict[str, Any]:
            raise RuntimeError("ModelOutcomeUnknown")

        self.wiring.raw_model = unknown
        with self.assertRaises(RuntimeError):
            self.wiring.model("{}", "thought", call_identity="first")
        self.now += 1000

        def fresh(prompt: str) -> dict[str, Any]:
            return {"text": "fresh"}

        self.wiring.raw_model = fresh
        self.assertEqual(
            self.wiring.model("{}", "thought", call_identity="second")["text"], "fresh"
        )
        with self.assertRaises(RuntimeError):
            self.wiring.model("{}", "thought", call_identity="first")
        self.assertEqual(self.wiring.used(), 2)

    def test_receipt_reconciliation_and_abandonment(self) -> None:
        from daemon_wiring import UNKNOWN_ABANDON_SECONDS

        def unknown(prompt: str) -> dict[str, Any]:
            raise RuntimeError("ModelOutcomeUnknown")

        self.wiring.raw_model = unknown
        with self.assertRaises(RuntimeError):
            self.wiring.model("{}", "thought", call_identity="recover")
        directory = self.root / "launcher-attempts"
        directory.mkdir(exist_ok=True, mode=0o700)
        (directory / "recover.json").write_text(
            json.dumps(
                {
                    "attempt_id": "recover",
                    "state": "completed",
                    "result": {"ok": True, "text": "recovered"},
                }
            )
        )
        self.wiring.reconcile_model_calls()
        with self.wiring.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM budget").fetchone()[0], "completed")
            db.execute(
                "INSERT INTO budget(day,phase,state,call_identity,dispatched_at) "
                "VALUES(?, 'thought','dispatched_unknown','lost',?)",
                (self.wiring.day(), 0),
            )
        with patch("daemon_wiring.time.time", return_value=UNKNOWN_ABANDON_SECONDS + 1):
            self.wiring.reconcile_model_calls()
        with self.wiring.connect() as db:
            self.assertEqual(
                db.execute("SELECT state FROM budget WHERE call_identity='lost'").fetchone()[0],
                "unknown_abandoned",
            )
        self.assertEqual(self.wiring.used(), 2)

    def test_sideeffect_unknown_blocks_only_same_phase(self) -> None:
        with self.wiring.connect() as db:
            db.execute(
                "INSERT INTO budget(day,phase,state) VALUES(?,'task','dispatched_unknown')",
                (self.wiring.day(),),
            )
        self.assertFalse(self.wiring.budget_status("task")["allowed"])
        self.assertTrue(self.wiring.budget_status("thought")["allowed"])
        self.assertTrue(self.wiring.budget_status("other_action")["allowed"])

    def test_failure_backoff_caps_and_survives_restart(self) -> None:
        from daemon_wiring import MODEL_FAILURE_BACKOFF_MAX_SECONDS

        def failed(prompt: str) -> dict[str, Any]:
            raise OSError("offline")

        self.wiring.raw_model = failed
        delays: list[float] = []
        for index in range(12):
            with self.assertRaises(OSError):
                self.wiring.model("{}", "thought", call_identity=f"fail-{index}")
            delay = self.wiring.budget_status()["failure_wait_seconds"]
            delays.append(delay)
            self.now += delay + 20
        self.assertEqual(delays, sorted(delays))
        self.assertEqual(delays[:5], [30.0, 60.0, 120.0, 240.0, 300.0])
        self.assertEqual(delays[-1], MODEL_FAILURE_BACKOFF_MAX_SECONDS)
        self.now -= delays[-1] + 20
        restarted = self.make()
        self.assertEqual(restarted.budget_status()["failure_wait_seconds"], delays[-1])
        self.assertFalse(restarted.think())

    def test_unknown_identity_never_resends_after_restart_and_abandonment(self) -> None:
        from daemon_wiring import UNKNOWN_ABANDON_SECONDS

        sent: list[str] = []

        def unknown(prompt: str) -> dict[str, Any]:
            sent.append(prompt)
            raise RuntimeError("ModelOutcomeUnknown")

        self.wiring.raw_model = unknown
        with self.assertRaises(RuntimeError):
            self.wiring.model("{}", "thought", call_identity="never-again")
        with self.wiring.connect() as db:
            db.execute("UPDATE budget SET dispatched_at=0")
        self.now += 1000
        restarted = self.make()
        restarted.raw_model = unknown
        with patch("daemon_wiring.time.time", return_value=UNKNOWN_ABANDON_SECONDS + 1):
            with self.assertRaisesRegex(RuntimeError, "ModelCallAlreadyAttempted"):
                restarted.model("{}", "thought", call_identity="never-again")
        self.assertEqual(len(sent), 1)
        self.assertEqual(restarted.used(), 1)
        with restarted.connect() as db:
            self.assertEqual(
                db.execute("SELECT state FROM budget").fetchone()[0], "unknown_abandoned"
            )

    def test_backoff_survives_fresh_python_process(self) -> None:
        import subprocess
        import sys

        def failed(prompt: str) -> dict[str, Any]:
            raise OSError("offline")

        self.wiring.raw_model = failed
        with self.assertRaises(OSError):
            self.wiring.model("{}", "thought")
        script = (
            "import json; from pathlib import Path; "
            "from daemon_wiring import Wiring, FixtureModel; "
            f"w=Wiring(Path({str(self.root)!r}),FixtureModel(),fixture=True,"
            f"clock=lambda:{self.now!r}); "
            "print(json.dumps(w.budget_status()))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=20, check=True
        )
        status = json.loads(result.stdout)
        self.assertEqual(status["failure_wait_seconds"], 30)
        self.assertFalse(status["allowed"])

    def test_identity_survives_boundary_context_bounding(self) -> None:
        seen: list[str | None] = []
        from daemon_wiring import model_call_identity

        def model(prompt: str) -> dict[str, Any]:
            seen.append(model_call_identity(prompt))
            return {"text": "ok"}

        self.wiring.raw_model = model
        self.wiring.model(
            json.dumps({"instructions": "x" * 10520}), "thought", call_identity="fixed-id"
        )
        self.assertEqual(seen, ["fixed-id"])

    def test_registering_child_during_reconcile_does_not_abandon(self) -> None:
        import os

        import daemon_wiring
        from daemon_wiring import UNKNOWN_ABANDON_SECONDS

        with self.wiring.connect() as db:
            db.execute(
                "INSERT INTO budget(day,phase,state,call_identity,dispatched_at) "
                "VALUES(?,'task','dispatched_unknown','registering',0)",
                (self.wiring.day(),),
            )

        def receipt(root: Path, identity: str) -> dict[str, Any]:
            with self.wiring.connect() as db:
                db.execute(
                    "UPDATE budget SET child_pid=? WHERE call_identity=?", (os.getpid(), identity)
                )
            return {}

        with (
            patch.object(daemon_wiring.launcher, "query_attempt", side_effect=receipt),
            patch("daemon_wiring.time.time", return_value=UNKNOWN_ABANDON_SECONDS + 1),
        ):
            self.wiring.reconcile_model_calls()
        with self.wiring.connect() as db:
            self.assertEqual(
                db.execute("SELECT state FROM budget").fetchone()[0], "dispatched_unknown"
            )

    def test_reconciled_success_resets_failure_streak(self) -> None:
        self.wiring.record_model_failure()
        call = self.wiring.mind.store.append(
            "model_call", {"context": {"_daemon_call_identity": "done"}}, source="test"
        )
        self.wiring.mind.store.append(
            "model_response", {"call_id": call["id"], "text": "local"}, source="test"
        )
        with self.wiring.connect() as db:
            db.execute(
                "INSERT INTO budget(day,phase,state,call_identity,dispatched_at) "
                "VALUES(?,'thought','dispatched_unknown','done',0)",
                (self.wiring.day(),),
            )
        self.wiring.reconcile_model_calls()
        self.assertEqual(self.wiring.budget_status()["failure_wait_seconds"], 0)

    def test_seventeenth_unknown_then_three_virtual_hours(self) -> None:
        base = FixtureModel()
        identities: list[str] = []

        def model(prompt: str) -> dict[str, Any]:
            identities.append(json.loads(prompt)["_daemon_call_identity"])
            if len(identities) == 17:
                raise RuntimeError("ModelOutcomeUnknown")
            return base(prompt)

        self.wiring.raw_model = model
        for _ in range(17):
            self.wiring.think()
            self.now += 30
        self.assertEqual(len(identities), 17)
        per_hour: list[int] = []
        for _ in range(3):
            before = self.wiring.mind.store.count("thought")
            for _ in range(120):
                self.wiring.think()
                self.now += 30
            per_hour.append(self.wiring.mind.store.count("thought") - before)
        self.assertTrue(all(value > 0 for value in per_hour), per_hour)
        self.assertGreater(len(identities), 17)
        self.assertEqual(len(identities), len(set(identities)))
        with self.wiring.connect() as db:
            states = dict(db.execute("SELECT state,count(*) FROM budget GROUP BY state"))
        print(
            json.dumps(
                {
                    "scenario": "unknown_at_17_three_virtual_hours",
                    "thoughts_per_hour": per_hour,
                    "model_dispatches": len(identities),
                    "states": states,
                }
            ),
            flush=True,
        )

    def test_live_child_is_not_abandoned_and_mind_reply_reconciles(self) -> None:
        import os

        from daemon_wiring import UNKNOWN_ABANDON_SECONDS

        with self.wiring.connect() as db:
            db.execute(
                "INSERT INTO budget(day,phase,state,call_identity,dispatched_at,child_pid) "
                "VALUES(?,'thought','dispatched_unknown','live',0,?)",
                (self.wiring.day(), os.getpid()),
            )
        with patch("daemon_wiring.time.time", return_value=UNKNOWN_ABANDON_SECONDS + 1):
            self.wiring.reconcile_model_calls()
        with self.wiring.connect() as db:
            self.assertEqual(
                db.execute("SELECT state FROM budget").fetchone()[0], "dispatched_unknown"
            )
        call = self.wiring.mind.store.append(
            "model_call", {"context": {"_daemon_call_identity": "live"}}, source="test"
        )
        self.wiring.mind.store.append(
            "model_response", {"call_id": call["id"], "text": "local"}, source="test"
        )
        self.wiring.reconcile_model_calls()
        with self.wiring.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM budget").fetchone()[0], "completed")
        self.assertEqual(self.wiring.used(), 1)

    def test_degraded_received_reply_is_known_failure(self) -> None:
        import daemon_wiring

        stable = StableModel(self.root, {}, "FAKE-private", self.wiring.timeline_id)
        with patch.object(
            daemon_wiring.launcher,
            "launch",
            return_value={
                "ok": True,
                "generation_degraded": True,
                "model_request_state": "response_received",
            },
        ):
            self.wiring.raw_model = stable
            with self.assertRaises(RuntimeError):
                self.wiring.model("{}", "thought")
        with self.wiring.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM budget").fetchone()[0], "failed")


if __name__ == "__main__":
    unittest.main()
