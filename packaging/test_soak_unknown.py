"""Offline, durable soak call admission: no model/config/credential access."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch


def runner() -> ModuleType:
    path = Path(__file__).parent / "soak" / "run_soak.py"
    spec = importlib.util.spec_from_file_location("soak_unknown_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SoakUnknownTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="soak-unknown-", dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runner = runner()
        self.sent: list[str] = []
        self.now = 1000.0

    def good(self, prompt: str) -> dict[str, Any]:
        self.sent.append(prompt)
        return {"text": "offline-result"}

    def unknown(self, prompt: str) -> dict[str, Any]:
        self.sent.append(prompt)
        raise RuntimeError("ModelOutcomeUnknown")

    def rows(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.root / "daemon.db")) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM soak_model_calls ORDER BY id")]

    def test_unknown_does_not_globally_block_new_readonly_thought(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
            self.runner.budgeted_call(self.root, 10, self.unknown, "first")
        try:
            result = runner().budgeted_call(self.root, 10, self.good, "next")
        except RuntimeError as exc:
            self.fail(f"distinct readonly thought was blocked by previous unknown: {exc}")
        self.assertEqual(result, {"text": "offline-result"})
        self.assertEqual(self.sent, ["first", "next"])
        self.assertEqual([row["state"] for row in self.rows()], ["dispatched_unknown", "completed"])

    def call(self, identity: str, *, phase: str = "thought", **kwargs: Any) -> dict[str, Any]:
        return self.runner.budgeted_call(
            self.root,
            100,
            self.good,
            "request-" + identity,
            phase=phase,
            call_identity=identity,
            clock=lambda: self.now,
            **kwargs,
        )

    def make_unknown(self, identity: str, phase: str = "thought") -> None:
        with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
            self.runner.budgeted_call(
                self.root,
                100,
                self.unknown,
                "request-" + identity,
                phase=phase,
                call_identity=identity,
                clock=lambda: self.now,
            )

    def test_same_unknown_identity_never_resends_after_restart_or_abandonment(self) -> None:
        self.make_unknown("stable")
        self.runner = runner()
        self.now += 1800
        with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
            self.call("stable")
        self.assertEqual(self.sent, ["request-stable"])
        self.assertEqual(self.rows()[0]["state"], "unknown_abandoned")

    def test_exact_readonly_whitelist_admits_distinct_calls_during_unknown(self) -> None:
        readonly = (
            "thought",
            "question",
            "question_review",
            "relevance",
            "reflection",
            "outreach",
            "digest",
        )
        for phase in readonly:
            with self.subTest(phase=phase):
                self.make_unknown(phase + "-one", phase)
                self.call(phase + "-two", phase=phase)
        self.assertEqual(len(self.sent), 2 * len(readonly))

    def test_live_child_prevents_soak_abandonment(self) -> None:
        import os

        self.make_unknown("active", "task")
        with closing(sqlite3.connect(self.root / "daemon.db")) as db, db:
            db.execute("CREATE TABLE budget(call_identity TEXT, child_pid INTEGER)")
            db.execute("INSERT INTO budget VALUES(?,?)", ("active", os.getpid()))
        self.now += 3600
        with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
            self.call("next", phase="task")
        self.assertEqual(self.rows()[0]["state"], "dispatched_unknown")

    def test_soak_abandonment_atomically_prevents_late_child_registration(self) -> None:
        self.make_unknown("late-child", "task")
        with closing(sqlite3.connect(self.root / "daemon.db")) as db, db:
            db.execute("CREATE TABLE budget(call_identity TEXT,child_pid INTEGER,state TEXT)")
            db.execute("INSERT INTO budget VALUES('late-child',NULL,'dispatched_unknown')")
        self.now += 3600
        self.call("next-task", phase="task")
        with closing(sqlite3.connect(self.root / "daemon.db")) as db, db:
            updated = db.execute(
                "UPDATE budget SET child_pid=123 WHERE call_identity='late-child' "
                "AND state='dispatched_unknown' AND child_pid IS NULL"
            ).rowcount
        self.assertEqual(updated, 0)

    def test_unknown_side_effect_blocks_only_same_phase(self) -> None:
        self.make_unknown("task-one", "task")
        with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
            self.call("task-two", phase="task")
        self.call("fresh-thought")
        self.call("other-side-effect", phase="publish")
        self.assertEqual(
            self.sent, ["request-task-one", "request-fresh-thought", "request-other-side-effect"]
        )
        self.assertEqual(len(self.rows()), 3)

    def test_unlisted_phases_fail_closed_not_keyword_classified(self) -> None:
        for phase in ("task", "readonly_task", "thought_publish", "", "THOUGHT"):
            with self.subTest(phase=phase):
                self.make_unknown("first-" + phase, phase)
                with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
                    self.call("second-" + phase, phase=phase)

    def test_abandon_at_exact_wall_deadline_and_keep_budget_spent(self) -> None:
        self.make_unknown("old-task", "task")
        self.now += 1799.999
        with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
            self.call("new-task", phase="task")
        self.now += 0.001
        self.call("new-task", phase="task")
        self.assertEqual([row["state"] for row in self.rows()], ["unknown_abandoned", "completed"])
        with self.assertRaisesRegex(RuntimeError, "SoakTotalCallLimit"):
            self.runner.budgeted_call(
                self.root,
                2,
                self.good,
                "over-cap",
                call_identity="third",
                clock=lambda: self.now,
            )
        self.assertEqual(len(self.sent), 2)

    def test_backward_clock_does_not_abandon(self) -> None:
        self.make_unknown("future", "task")
        self.now -= 1801
        with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
            self.call("early", phase="task")
        self.assertEqual(self.rows()[0]["state"], "dispatched_unknown")

    def test_classified_failures_are_failed_and_do_not_block_new_call(self) -> None:
        for index, error in enumerate(
            (OSError("offline"), ValueError("invalid"), RuntimeError("classified"))
        ):
            identity = "failure-" + str(index)

            def fail(prompt: str, error: Exception = error) -> dict[str, Any]:
                self.sent.append(prompt)
                raise error

            with self.assertRaises(type(error)):
                self.runner.budgeted_call(
                    self.root,
                    100,
                    fail,
                    identity,
                    phase="task",
                    call_identity=identity,
                )
            self.assertEqual(self.rows()[-1]["state"], "failed")
            self.assertEqual(self.rows()[-1]["error_type"], type(error).__name__)
            with self.assertRaisesRegex(RuntimeError, "SoakCallAlreadyAttempted"):
                self.call(identity, phase="task")
            self.call(identity + "-next", phase="task")

    def test_model_outcome_unknown_type_and_baseexceptions_remain_unknown(self) -> None:
        class ModelOutcomeUnknown(RuntimeError):
            pass

        class SpecificOutcomeUnknown(ModelOutcomeUnknown):
            pass

        for index, error in enumerate(
            (
                ModelOutcomeUnknown("uncertain"),
                SpecificOutcomeUnknown("uncertain subtype"),
                KeyboardInterrupt(),
                SystemExit(7),
            )
        ):

            def fail(prompt: str, error: BaseException = error) -> dict[str, Any]:
                raise error

            with self.assertRaises(type(error)):
                self.runner.budgeted_call(self.root, 100, fail, "request", call_identity=str(index))
            self.assertEqual(self.rows()[-1]["state"], "dispatched_unknown")

    def test_completed_identity_replays_persisted_response_without_budget_spend(self) -> None:
        expected = self.call("done")
        self.runner = runner()
        actual = self.runner.budgeted_call(
            self.root,
            1,
            self.good,
            "must-not-send",
            call_identity="done",
        )
        self.assertEqual(actual, expected)
        self.assertEqual(self.sent, ["request-done"])
        self.assertEqual(len(self.rows()), 1)

    def test_response_probe_reconciles_before_gate_and_does_not_resend(self) -> None:
        self.make_unknown("lost-result", "task")
        checked: list[str] = []

        def probe(identity: str) -> dict[str, Any] | None:
            checked.append(identity)
            return {"text": "persisted-response"} if identity == "lost-result" else None

        result = self.call("lost-result", phase="task", result_probe=probe)
        self.assertEqual(result, {"text": "persisted-response"})
        self.assertEqual(checked, ["lost-result"])
        self.call("next-task", phase="task")
        self.assertEqual(self.sent, ["request-lost-result", "request-next-task"])
        self.assertEqual(self.rows()[0]["state"], "completed")

    def test_probe_failure_is_not_evidence_of_completion(self) -> None:
        self.make_unknown("unresolved", "task")

        def broken(identity: str) -> dict[str, Any] | None:
            raise OSError("unreadable")

        with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
            self.call("next-task", phase="task", result_probe=broken)
        self.assertEqual(self.rows()[0]["state"], "dispatched_unknown")

    def test_persisted_launcher_result_reconciles_by_exact_attempt_identity(self) -> None:
        self.make_unknown("attempt_one", "task")
        attempts = self.root / "launcher-attempts"
        attempts.mkdir()
        (attempts / "attempt_one.json").write_text(
            json.dumps(
                {
                    "attempt_id": "attempt_one",
                    "state": "completed",
                    "result": {"ok": True, "text": "saved"},
                }
            )
        )
        result = self.call("attempt_one", phase="task")
        self.assertEqual(result["text"], "saved")
        self.assertEqual(self.sent, ["request-attempt_one"])
        self.assertEqual(self.rows()[0]["state"], "completed")

    def test_mismatched_or_degraded_persisted_result_cannot_clear_gate(self) -> None:
        self.make_unknown("attempt_one", "task")
        attempts = self.root / "launcher-attempts"
        attempts.mkdir()
        for attempt, result in (
            ("another", {"ok": True}),
            ("attempt_one", {"ok": False}),
            ("attempt_one", {"ok": True, "generation_degraded": True}),
        ):
            (attempts / "attempt_one.json").write_text(
                json.dumps(
                    {
                        "attempt_id": attempt,
                        "state": "completed",
                        "result": result,
                    }
                )
            )
            with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
                self.call("attempt_one", phase="task")
        self.assertEqual(self.rows()[0]["state"], "dispatched_unknown")

    def test_prompt_phase_round_is_stable_and_fallback_calls_are_distinct(self) -> None:
        prompt = json.dumps({"phase": "thought", "round": 42, "context": "first"})
        with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
            self.runner.budgeted_call(self.root, 100, self.unknown, prompt)
        with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
            self.runner.budgeted_call(self.root, 100, self.good, prompt)
        self.runner.budgeted_call(
            self.root, 100, self.good, json.dumps({"phase": "thought", "round": 43})
        )
        self.runner.budgeted_call(self.root, 100, self.good, "same text")
        self.runner.budgeted_call(self.root, 100, self.good, "same text")
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(len({row["call_identity"] for row in self.rows()}), 4)

    def test_prompt_daemon_identity_correlates_exact_active_budget_id_and_phase(self) -> None:
        with closing(sqlite3.connect(self.root / "daemon.db")) as db, db:
            db.execute(
                "CREATE TABLE budget("
                "id INTEGER PRIMARY KEY,phase TEXT,state TEXT,call_identity TEXT)"
            )
            db.executemany(
                "INSERT INTO budget VALUES(?,?,?,?)",
                [
                    (11, "thought", "dispatched_unknown", "older"),
                    (12, "task", "dispatched_unknown", "active"),
                    (13, "reflection", "dispatched_unknown", "newer-unrelated"),
                ],
            )
        prompt = json.dumps({"_daemon_call_identity": "active"})
        with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
            self.runner.budgeted_call(self.root, 100, self.unknown, prompt)
        row = self.rows()[0]
        self.assertEqual(
            (row["call_identity"], row["daemon_budget_id"], row["phase"]), ("active", 12, "task")
        )
        with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
            self.call("second-task", phase="task")
        self.call("new-thought")

    def test_migrates_legacy_two_column_table_without_resetting_cap(self) -> None:
        with closing(sqlite3.connect(self.root / "daemon.db")) as db, db:
            db.execute("CREATE TABLE soak_model_calls(id INTEGER PRIMARY KEY,state TEXT NOT NULL)")
            db.execute("INSERT INTO soak_model_calls VALUES(1,'dispatched_unknown')")
            db.execute("INSERT INTO soak_model_calls VALUES(2,'completed')")
        self.runner.budgeted_call(self.root, 3, self.good, "new", clock=lambda: self.now)
        rows = self.rows()
        self.assertEqual([row["id"] for row in rows], [1, 2, 3])
        self.assertEqual(rows[0]["state"], "dispatched_unknown")
        self.assertEqual(len({row["call_identity"] for row in rows}), 3)
        with self.assertRaisesRegex(RuntimeError, "SoakTotalCallLimit"):
            self.runner.budgeted_call(self.root, 3, self.good, "extra", clock=lambda: self.now)

    def test_abandoned_attempt_can_reconcile_late_response_without_resend(self) -> None:
        self.make_unknown("late-result", "task")
        self.now += 1800
        self.call("new-task", phase="task")
        self.assertEqual(self.rows()[0]["state"], "unknown_abandoned")

        def late(identity: str) -> dict[str, Any]:
            return {"text": "late"}

        actual = self.call("late-result", phase="task", result_probe=late)
        self.assertEqual(actual, {"text": "late"})
        self.assertEqual(self.sent, ["request-late-result", "request-new-task"])
        self.assertEqual(self.rows()[0]["state"], "completed")

    def test_abandoned_unknown_remains_in_observed_model_call_totals(self) -> None:
        self.make_unknown("old-task", "task")
        self.now += 1800
        self.call("new-task", phase="task")
        with closing(sqlite3.connect(self.root / "daemon.db")) as db, db:
            db.execute("CREATE TABLE inbox(id TEXT PRIMARY KEY,delivered INTEGER)")
        counters, _ = self.runner.collect_counters(self.root)
        self.assertEqual(counters["model_unknown"], 1)
        self.assertEqual(counters["model_success"], 1)

    def test_probe_degraded_or_failed_response_is_not_completed(self) -> None:
        self.make_unknown("uncertain", "task")
        results: tuple[dict[str, Any], ...] = (
            {"ok": False},
            {"ok": True, "generation_degraded": True},
        )
        for result in results:

            def probe(identity: str, result: dict[str, Any] = result) -> dict[str, Any]:
                return result

            with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
                self.call("uncertain", phase="task", result_probe=probe)
        self.assertEqual(self.rows()[0]["state"], "dispatched_unknown")

    def test_concurrent_same_identity_sends_once(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def delayed(prompt: str) -> dict[str, Any]:
            self.sent.append(prompt)
            entered.set()
            self.assertTrue(release.wait(5))
            return {"text": "single-result"}

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(
                self.runner.budgeted_call,
                self.root,
                100,
                delayed,
                "once",
                call_identity="same",
            )
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
                    self.call("same")
                self.assertEqual(len(self.rows()), 1)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5), {"text": "single-result"})
        self.assertEqual(self.sent, ["once"])

    def test_concurrent_distinct_readonly_calls_admitted_under_total_cap(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def delayed(prompt: str) -> dict[str, Any]:
            self.sent.append(prompt)
            entered.set()
            self.assertTrue(release.wait(5))
            return {"text": "first"}

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(
                self.runner.budgeted_call,
                self.root,
                2,
                delayed,
                "first",
                call_identity="first",
            )
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.call("second"), {"text": "offline-result"})
                with self.assertRaisesRegex(RuntimeError, "SoakTotalCallLimit"):
                    self.runner.budgeted_call(
                        self.root, 2, self.good, "third", call_identity="third"
                    )
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5), {"text": "first"})
        self.assertEqual(self.sent, ["first", "request-second"])

    def test_legacy_unknown_grace_persists_and_only_blocks_sideeffects(self) -> None:
        with closing(sqlite3.connect(self.root / "daemon.db")) as db, db:
            db.execute("CREATE TABLE soak_model_calls(id INTEGER PRIMARY KEY,state TEXT NOT NULL)")
            db.execute("INSERT INTO soak_model_calls VALUES(1,'dispatched_unknown')")
        with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
            self.call("task", phase="task")
        self.call("thought")
        self.assertEqual(self.rows()[0]["dispatched_at"], self.now)
        self.runner = runner()
        self.now += 1800
        self.call("task", phase="task")
        self.assertEqual(self.rows()[0]["state"], "unknown_abandoned")
        self.assertEqual(self.rows()[0]["dispatched_at"], 1000.0)

    def test_real_mode_wrapper_correlates_active_daemon_call_without_model_access(self) -> None:
        # Exercise real wrapper + StableModel, replacing only process entry/launcher.
        sys.path.insert(0, str(Path(__file__).parent))
        try:
            import daemon_wiring as wiring
            import jarvis_daemon as daemon
        finally:
            sys.path.pop(0)
        with closing(sqlite3.connect(self.root / "daemon.db")) as db, db:
            db.execute(
                "CREATE TABLE budget("
                "id INTEGER PRIMARY KEY,phase TEXT,state TEXT,call_identity TEXT)"
            )
        dispatches: list[str] = []

        def launch(*args: Any, **kwargs: Any) -> dict[str, Any]:
            prompt = str(args[2])
            dispatches.append(prompt)
            if len(dispatches) == 1:
                raise wiring.launcher.LauncherError("offline uncertain outcome")
            return {"ok": True, "text": "wrapper-offline"}

        def main() -> int:
            raw = wiring.StableModel(self.root, {}, "FAKE-OFFLINE-NONSECRET", "offline-timeline")
            for identity, phase, should_send in (
                ("task-one", "task", True),
                ("task-two", "task", False),
                ("new-thought", "thought", True),
            ):
                with closing(sqlite3.connect(self.root / "daemon.db")) as db, db:
                    db.execute(
                        "INSERT INTO budget(phase,state,call_identity) "
                        "VALUES(?,'dispatched_unknown',?)",
                        (phase, identity),
                    )
                prompt = json.dumps({"_daemon_call_identity": identity})
                if identity == "task-one" or not should_send:
                    with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
                        raw(prompt)
                else:
                    self.assertEqual(raw(prompt)["text"], "wrapper-offline")
            return 0

        with (
            patch.object(wiring.StableModel, "__call__", wiring.StableModel.__call__),
            patch.object(wiring.launcher, "launch", side_effect=launch),
            patch.object(daemon, "main", side_effect=main),
            patch.object(sys, "argv", ["run_soak.py"]),
        ):
            self.assertEqual(
                self.runner.daemon_worker(
                    {
                        "root": str(self.root),
                        "mode": "real",
                        "max_model_calls": 10,
                        "config": "/never-read-offline-config",
                        "key_file": "/never-read-offline-key",
                    }
                ),
                0,
            )
        self.assertEqual(len(dispatches), 2)
        self.assertEqual(
            [(row["call_identity"], row["daemon_budget_id"], row["phase"]) for row in self.rows()],
            [("task-one", 1, "task"), ("new-thought", 3, "thought")],
        )

    def test_malformed_probe_receipts_do_not_globally_block_readonly(self) -> None:
        self.make_unknown("pending")
        malformed: tuple[Any, ...] = (
            [],
            "not a response",
            {"text": float("nan")},
            {"text": object()},
        )
        for index, result in enumerate(malformed):

            def probe(identity: str, result: Any = result) -> Any:
                return result

            with self.subTest(result=type(result).__name__):
                self.call("safe-" + str(index), result_probe=probe)
        self.assertEqual(self.rows()[0]["state"], "dispatched_unknown")
        self.assertEqual(len(self.sent), 5)

    def test_shared_policy_is_read_at_call_time_not_copied(self) -> None:
        sys.path.insert(0, str(Path(__file__).parent))
        try:
            import daemon_wiring as wiring
        finally:
            sys.path.pop(0)
        self.make_unknown("task", "task")
        with patch.object(wiring, "UNKNOWN_ABANDON_SECONDS", 23, create=True):
            self.now += 23
            self.call("after-deadline", phase="task")
        self.make_unknown("custom-one", "custom-readonly")
        with patch.object(
            wiring, "READ_ONLY_MODEL_PHASES", frozenset({"custom-readonly"}), create=True
        ):
            self.call("custom-two", phase="custom-readonly")

    def test_policy_fallback_works_with_older_daemon_module(self) -> None:
        sys.path.insert(0, str(Path(__file__).parent))
        try:
            import daemon_wiring as wiring
        finally:
            sys.path.pop(0)
        with patch.dict(vars(wiring)):
            wiring.__dict__.pop("UNKNOWN_ABANDON_SECONDS", None)
            wiring.__dict__.pop("READ_ONLY_MODEL_PHASES", None)
            self.make_unknown("old-daemon-task", "task")
            with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
                self.call("second-task", phase="task")
            self.call("new-thought")
            self.now += 1800
            self.call("second-task", phase="task")


if __name__ == "__main__":
    unittest.main()
