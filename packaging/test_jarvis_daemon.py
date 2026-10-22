"""Integration tests with disposable product roots and no provider access."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis_daemon import CST


def offline_daemon_bootstrap() -> None:
    """Child-only dependency injection; default product components remain real."""
    import sys
    from datetime import UTC

    import daemon_wiring
    import jarvis_daemon
    from senses.base import CursorStore, SenseEvent

    root = Path(sys.argv[sys.argv.index("--data-root") + 1])
    original_model = daemon_wiring.FixtureModel

    class Source(daemon_wiring.FixtureAdapter):
        def poll(self, store: CursorStore, since: datetime | None = None) -> list[SenseEvent]:
            old = datetime(2026, 10, 14, tzinfo=UTC)
            recent = datetime(2026, 10, 16, 15, 29, tzinfo=UTC)
            # Replay deliberately; the staged cursor and inbox dedupe stay real.
            events = [
                self.event("speech", old, "synthetic historical", {"index": n}, str(n))
                for n in range(20000)
            ]
            if (root / "release-new").exists():
                events.append(self.event("speech", recent, "synthetic new", {"new": True}, "new"))
            return events

    class Model(original_model):
        def __call__(self, prompt: str) -> dict[str, Any]:
            with sqlite3.connect(root / "daemon.db") as db:
                failures = db.execute(
                    "SELECT count(*) FROM budget WHERE state='failed'"
                ).fetchone()[0]
            if failures < 3:
                raise OSError("synthetic transient")
            if not (root / "recover").exists():
                raise OSError("synthetic awaiting recovery")
            return super().__call__(prompt)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("real model or credential access forbidden")

    daemon_wiring.FixtureAdapter = Source
    daemon_wiring.FixtureModel = Model
    daemon_wiring.StableModel = forbidden  # type: ignore[assignment]
    daemon_wiring.launcher.read_key = forbidden
    daemon_wiring.launcher.launch = forbidden
    jarvis_daemon.IDLE_SECONDS = 2
    raise SystemExit(jarvis_daemon.main())


def offline_supervisor_bootstrap() -> None:
    import sys
    from dataclasses import replace

    import daemon_wiring
    import supervisor

    original = daemon_wiring.component

    def component(root: Path, **kwargs: Any) -> Any:
        spec = original(root, fixture=True, speed=30)
        code = (
            f"import sys; sys.path.insert(0,{str(Path(__file__).parent)!r}); "
            "from test_jarvis_daemon import offline_daemon_bootstrap; offline_daemon_bootstrap()"
        )
        return replace(spec, command=(sys.executable, "-B", "-c", code, *spec.command[3:]))

    daemon_wiring.component = component
    raise SystemExit(supervisor.main())


class DaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="jarvis-acceptance-daemon-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "data"
        self.root.mkdir(mode=0o700)
        self.now = datetime(2026, 10, 16, 23, 29, tzinfo=CST).timestamp()

    def wiring(self) -> Any:
        import daemon_wiring

        self.assertTrue(hasattr(daemon_wiring, "Wiring"), "resident adapters are missing")
        return daemon_wiring.Wiring.create(
            self.root, fixture=True, acceptance=True, clock=lambda: self.now
        )

    def test_offline_default_supervisor_backfill_fail_recover_kill_restart(self) -> None:
        import os
        import signal
        import socket
        import subprocess
        import sys
        import time

        parent = self.root.parent
        config = parent / "offline-config.json"
        key = parent / "fake-key"
        key.write_text("FAKE-DAEMON-NONSECRET")
        key.chmod(0o600)
        config.write_text(
            json.dumps(
                {
                    "model": "fake-test-model",
                    "provider": "custom",
                    "base_url": "http://127.0.0.1:57431/v1",
                    "api_mode": "chat_completions",
                    "context_length": 256000,
                    "reasoning_effort": "low",
                }
            )
        )
        config.chmod(0o600)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        code = (
            f"import sys; sys.path.insert(0,{str(Path(__file__).parent)!r}); "
            "from test_jarvis_daemon import offline_supervisor_bootstrap; "
            "offline_supervisor_bootstrap()"
        )
        log = (parent / "offline-process.log").open("wb")
        self.addCleanup(log.close)
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                code,
                "run",
                "--data-root",
                str(self.root),
                "--config",
                str(config),
                "--key-file",
                str(key),
                "--port",
                str(port),
                "--test-port",
                "57431",
                "--acceptance-mode",
                "--tick",
                "0.05",
            ],
            stdout=log,
            stderr=log,
        )

        def cleanup() -> None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        self.addCleanup(cleanup)

        def read(name: str) -> Any:
            try:
                return json.loads((self.root / name).read_text())
            except (FileNotFoundError, ValueError):
                return {}

        def wait(predicate: Any, timeout: float = 25) -> None:
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if predicate():
                    return
                if process.poll() is not None:
                    self.fail((parent / "offline-process.log").read_text())
                time.sleep(0.05)
            self.fail(
                "offline lifecycle timeout: "
                + json.dumps(read("supervisor-status.json"))
                + (parent / "offline-process.log").read_text()
            )

        wait(lambda: read("daemon.heartbeat").get("status") == "degraded")
        first_health = read("daemon.heartbeat")
        self.assertGreaterEqual(first_health["cognition"]["recent_failures"], 3)
        with sqlite3.connect(self.root / "mind.db") as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM entries WHERE category='daemon_sense_consumed'"
                ).fetchone()[0],
                0,
            )
        (self.root / "recover").touch()
        wait(lambda: read("daemon.heartbeat").get("status") == "healthy")
        wait(
            lambda: all(
                c["state"] == "healthy"
                for c in read("supervisor-status.json").get("components", {}).values()
            )
        )
        state = read("supervisor-status.json")
        self.assertEqual(set(state["components"]), {"web", "jarvis-daemon"})
        first_pid = state["components"]["jarvis-daemon"]["pid"]
        timeline = read("daemon.heartbeat")["timeline_id"]
        os.kill(first_pid, signal.SIGKILL)
        wait(
            lambda: (
                read("supervisor-status.json")
                .get("components", {})
                .get("jarvis-daemon", {})
                .get("restart_count", 0)
                >= 1
            )
        )
        wait(lambda: read("daemon.heartbeat").get("pid") != first_pid)
        self.assertEqual(read("daemon.heartbeat")["timeline_id"], timeline)
        (self.root / "release-new").touch()

        def consumed_new() -> bool:
            with sqlite3.connect(self.root / "mind.db") as db:
                return (
                    db.execute(
                        "SELECT count(*) FROM entries WHERE category='daemon_sense_consumed'"
                    ).fetchone()[0]
                    == 2
                )

        wait(consumed_new)
        with sqlite3.connect(self.root / "daemon.db") as db:
            inbox = [json.loads(row[0]) for row in db.execute("SELECT payload FROM inbox")]
            self.assertEqual(len(inbox), 2)
            self.assertEqual(sum(e["payload"].get("count", 0) for e in inbox), 20000)
            self.assertEqual(db.execute("SELECT count(*) FROM backfill_seen").fetchone()[0], 20000)
        with sqlite3.connect(self.root / "mind.db") as db:
            markers = [
                json.loads(row[0])["event_id"]
                for row in db.execute(
                    "SELECT payload FROM entries WHERE category='daemon_sense_consumed'"
                )
            ]
            self.assertEqual(len(markers), len(set(markers)))
        tracked = [first_pid] + [
            c["pid"] for c in read("supervisor-status.json")["components"].values()
        ]
        process.terminate()
        self.assertEqual(process.wait(timeout=20), 0)
        self.assertEqual(read("supervisor-status.json")["state"], "stopped")
        self.assertEqual(read("daemon.heartbeat")["phase"], "stopped")
        for pid in tracked:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        with socket.socket() as sock:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", port)), 0)
        self.offline_result = {
            "backfill_history_count": 20000,
            "inbox_count": len(inbox),
            "consumption_markers": len(markers),
            "unique_consumption_markers": len(set(markers)),
            "degraded_heartbeat": first_health,
            "final_heartbeat": read("daemon.heartbeat"),
            "final_supervisor": read("supervisor-status.json"),
            "tracked_pids": tracked,
            "exit_code": process.returncode,
            "data_parent": str(parent),
            "port_closed": True,
            "all_pids_gone": True,
        }

    def test_continuous_idle_thoughts_cover_every_hour_of_a_day(self) -> None:
        from jarvis_daemon import Daemon

        self.now = datetime(2026, 10, 16, 0, 0, tzinfo=CST).timestamp()
        wiring = self.wiring()
        wiring.adapters = []
        daemon = Daemon(wiring, clock=lambda: self.now)
        counts: list[int] = []
        for _ in range(24):
            before = len(wiring.mind.store.entries("thought"))
            for _ in range(12):
                daemon.tick()
                self.now += 300
            counts.append(len(wiring.mind.store.entries("thought")) - before)
        self.assertTrue(all(count > 0 for count in counts), counts)
        self.assertGreater(sum(counts), 30)

    def test_burst_is_paced_and_restart_does_not_refill_bucket(self) -> None:
        from daemon_wiring import CALL_BUCKET_CAPACITY, CALL_REFILL_SECONDS, MODEL_CALLS_PER_DAY

        wiring = self.wiring()

        def good(prompt: str) -> dict[str, Any]:
            return {"text": "synthetic"}

        wiring.raw_model = good
        for _ in range(int(CALL_BUCKET_CAPACITY)):
            wiring.model("{}", "thought")
        with self.assertRaisesRegex(RuntimeError, "ModelBudgetDeferred"):
            wiring.model("{}", "thought")
        restarted = self.wiring()
        self.assertFalse(restarted.budget_status()["allowed"])
        self.now += CALL_REFILL_SECONDS
        self.assertTrue(restarted.budget_status()["allowed"])
        self.assertLessEqual(restarted.used(), MODEL_CALLS_PER_DAY)
        self.now -= 3600
        self.assertLessEqual(restarted.budget_status()["tokens"], 1)

    def test_near_daily_ceiling_slows_without_counting_budget_as_failure(self) -> None:
        from daemon_wiring import CALL_REFILL_SECONDS, MODEL_CALLS_PER_DAY

        self.now = datetime(2026, 10, 16, 12, 0, tzinfo=CST).timestamp()
        wiring = self.wiring()
        with wiring.connect() as db:
            db.executemany(
                "INSERT INTO budget(day,phase,state) VALUES(?,?,'completed')",
                [(wiring.day(), "thought")] * (MODEL_CALLS_PER_DAY - 25),
            )
        status = wiring.budget_status()
        self.assertGreater(status["interval_seconds"], CALL_REFILL_SECONDS)
        self.assertTrue(wiring.think())
        self.assertFalse(wiring.think())
        self.assertEqual(wiring.cognition_health()["recent_failures"], 0)
        self.now += status["interval_seconds"] + 1
        self.assertTrue(wiring.think())
        self.assertLess(wiring.used(), MODEL_CALLS_PER_DAY)
        self.assertFalse(wiring.budget_status("task")["allowed"])

    def test_backlog_summary_is_bounded_and_receipts_reconcile(self) -> None:
        from daemon_wiring import PENDING_SENSE_LIMIT

        wiring = self.wiring()
        for n in range(128):
            wiring.mind.ingest(
                "tool_result",
                {
                    "daemon_event_id": str(n),
                    "sense_event": {
                        "source": "source-" + str(n % 2),
                        "kind": "speech",
                        "observed_at": datetime.fromtimestamp(self.now, CST).isoformat(),
                        "summary": "private raw values",
                        "payload": {"private": n},
                    },
                },
                source="senses:source-" + str(n % 2),
            )
        wiring.compact_pending()
        accounting = wiring.queue_accounting()
        self.assertLessEqual(accounting["pending"], PENDING_SENSE_LIMIT)
        self.assertEqual(accounting["merged"], 128)
        self.assertEqual(accounting["residual"], 0)
        summary = wiring.mind.store.entries("event")[-1]["payload"]["data"]["sense_event"]
        self.assertEqual(summary["payload"]["count"], 128)
        self.assertEqual(len(summary["payload"]["groups"]), 2)
        self.assertNotIn("private", json.dumps(summary))
        self.assertTrue(wiring.think())
        result = wiring.queue_accounting()
        self.assertEqual(result["pending"], 0)
        self.assertEqual(
            result["enqueued"], result["consumed"] + result["merged"] + result["suspended"]
        )
        self.assertEqual(self.wiring().queue_accounting(), result)

    def test_sir_turn_and_pending_senses_wake_idle_thoughts(self) -> None:
        from unittest.mock import patch

        import daemon_wiring
        from jarvis_daemon import ACTIVE_SECONDS, IDLE_SECONDS, Daemon

        wiring = self.wiring()
        wiring.adapters = []
        daemon = Daemon(wiring, clock=lambda: self.now)
        daemon.tick()
        self.assertGreaterEqual(daemon.next_due["think"] - self.now, IDLE_SECONDS)
        self.now += 16
        with patch.object(
            daemon_wiring.launcher,
            "read_timeline",
            return_value=[{"role": "user", "content": "synthetic new speech", "id": "sir-1"}],
        ):
            daemon.tick()
        self.assertLessEqual(daemon.next_due["think"] - self.now, ACTIVE_SECONDS)

    def test_poll_burst_compacts_pending_before_returning(self) -> None:
        from datetime import UTC
        from unittest.mock import patch

        import daemon_wiring
        from daemon_wiring import PENDING_SENSE_LIMIT
        from senses.base import Provenance, SenseEvent

        wiring = self.wiring()
        at = datetime.fromtimestamp(self.now, UTC)
        events = [
            SenseEvent(
                "fixture",
                "speech",
                at,
                at,
                "synthetic",
                {"synthetic": True},
                "low",
                Provenance("fixture", str(n)),
            )
            for n in range(1000)
        ]
        original = daemon_wiring.senses.poll_all

        def source(**kwargs: Any) -> list[SenseEvent]:
            original(**kwargs)
            return events

        with patch.object(daemon_wiring.senses, "poll_all", side_effect=source):
            wiring.poll()
        self.assertLessEqual(wiring.queue_accounting()["pending"], PENDING_SENSE_LIMIT)
        with wiring.connect() as db:
            self.assertLessEqual(
                db.execute("SELECT count(*) FROM inbox WHERE delivered=0").fetchone()[0],
                PENDING_SENSE_LIMIT,
            )
        self.assertEqual(wiring.queue_accounting()["residual"], 0)

    def test_merge_crash_restarts_without_overlapping_consumption(self) -> None:
        wiring = self.wiring()
        for n in range(64):
            wiring.mind.ingest("tool_result", {"daemon_event_id": str(n)}, source="synthetic")
        original = wiring.mind.store.append
        failed = False

        def interrupted(category: str, payload: Any, **kwargs: Any) -> Any:
            nonlocal failed
            if category == "daemon_sense_merged" and not failed:
                failed = True
                raise OSError("synthetic merge receipt crash")
            return original(category, payload, **kwargs)

        wiring.mind.store.append = interrupted
        with self.assertRaises(OSError):
            wiring.compact_pending()
        restarted = self.wiring()
        self.assertEqual(restarted.queue_accounting()["merged"], 64)
        self.assertEqual(restarted.queue_accounting()["pending"], 1)
        self.assertTrue(restarted.think())
        self.assertEqual(restarted.queue_accounting()["pending"], 0)

    def test_nested_merges_preserve_every_source_and_time_window(self) -> None:
        wiring = self.wiring()
        for batch in range(2):
            for n in range(64):
                wiring.mind.ingest(
                    "tool_result",
                    {
                        "daemon_event_id": f"{batch}-{n}",
                        "sense_event": {
                            "kind": "backfill_summary",
                            "source": str(n),
                            "observed_at": datetime.fromtimestamp(
                                self.now + n * 300, CST
                            ).isoformat(),
                            "payload": {"count": 10},
                            "background_only": True,
                        },
                    },
                    source="senses:" + str(n),
                )
            wiring.compact_pending()
        with wiring.connect() as db:
            groups = json.loads(
                db.execute("SELECT groups_json FROM summary_groups ORDER BY rowid DESC").fetchone()[
                    0
                ]
            )
        self.assertEqual(sum(group["count"] for group in groups), 1280)
        self.assertEqual(len(groups), 64)
        self.assertEqual(wiring.queue_accounting()["pending"], 1)

    def test_summary_context_preserves_counts_without_receipt_member_ids(self) -> None:
        wiring = self.wiring()
        for n in range(128):
            wiring.mind.ingest("tool_result", {"daemon_event_id": str(n)}, source="synthetic")
        wiring.compact_pending()
        pending = wiring.mind.context()["daemon_pending_senses"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].get("summary", {}).get("count"), 128)
        self.assertNotIn("merged_event_ids", json.dumps(wiring.mind.context()))

    def test_full_day_continuous_burst_stays_under_single_daily_cap(self) -> None:
        from daemon_wiring import CALL_REFILL_SECONDS, MODEL_CALLS_PER_DAY

        self.now = datetime(2026, 10, 16, 0, 0, tzinfo=CST).timestamp()
        wiring = self.wiring()

        def good(prompt: str) -> dict[str, Any]:
            return {"text": "synthetic"}

        wiring.raw_model = good
        hourly: list[int] = []
        for _ in range(24):
            count = 0
            for _ in range(int(3600 / CALL_REFILL_SECONDS)):
                while wiring.budget_status()["allowed"]:
                    wiring.model("{}", "thought")
                    count += 1
                self.now += CALL_REFILL_SECONDS
            hourly.append(count)
        with wiring.connect() as db:
            total = db.execute("SELECT count(*) FROM budget").fetchone()[0]
        self.assertTrue(all(count > 0 for count in hourly), hourly)
        self.assertLessEqual(total, MODEL_CALLS_PER_DAY)
        self.assertGreater(total, 5000)

    def test_daily_hard_ceiling_cannot_be_bypassed_by_other_phases(self) -> None:
        from daemon_wiring import MODEL_CALLS_PER_DAY

        wiring = self.wiring()
        with wiring.connect() as db:
            db.executemany(
                "INSERT INTO budget(day,phase,state) VALUES(?,?,'completed')",
                [(wiring.day(), "thought")] * MODEL_CALLS_PER_DAY,
            )
        for phase in ("thought", "question", "review", "task", "digest", "reflection"):
            with self.assertRaisesRegex(RuntimeError, "ModelBudgetDeferred"):
                wiring.model("{}", phase)
        self.assertEqual(wiring.used(), MODEL_CALLS_PER_DAY)

    def test_live_recovery_reconciles_consumption_before_merging_more_inputs(self) -> None:
        wiring = self.wiring()
        wiring.poll()
        original = wiring.mind.store.append

        def interrupted(category: str, payload: Any, **kwargs: Any) -> Any:
            if category == "daemon_sense_consumed":
                raise OSError("synthetic consumed receipt crash")
            return original(category, payload, **kwargs)

        wiring.mind.store.append = interrupted
        with self.assertRaises(OSError):
            wiring.think()
        wiring.mind.store.append = original
        for n in range(64):
            wiring.mind.ingest("tool_result", {"daemon_event_id": str(n)}, source="synthetic")
        wiring.compact_pending()
        wiring.reconcile_consumption()
        self.assertEqual(wiring.queue_accounting()["consumed"], 1)
        self.assertEqual(wiring.queue_accounting()["merged"], 64)

    def test_pending_inputs_are_canonical_ref_targets_without_merge_metadata(self) -> None:
        wiring = self.wiring()
        for n in range(32):
            wiring.mind.ingest("tool_result", {"daemon_event_id": str(n)}, source="synthetic")
        for n in range(10):
            wiring.mind.ingest("time", {"n": n}, source="clock")
        context = wiring.mind.context()
        pending_ids = {row["id"] for row in context["daemon_pending_senses"]}
        canonical_ids = {row["id"] for row in context["events"]}
        self.assertTrue(pending_ids <= canonical_ids)

    def test_soak_analysis_reports_all_logical_hours_and_honest_gaps(self) -> None:
        analyze = importlib.import_module("soak.analyze_soak")
        cognition_coverage = analyze.cognition_coverage
        samples: list[dict[str, Any]] = [
            {"logical_elapsed_seconds": n * 1800, "counters": {"thoughts": n}} for n in range(49)
        ]
        result = cognition_coverage(samples)
        self.assertEqual(result["status"], "OBSERVED_OK")
        self.assertEqual(result["complete_hours"], 24)
        self.assertEqual(result["hours_without_verified_thoughts"], [])
        samples[19]["counters"]["thoughts"] = 18
        samples[20]["counters"]["thoughts"] = 18
        self.assertEqual(cognition_coverage(samples)["status"], "ISSUE")
        self.assertEqual(cognition_coverage([])["status"], "UNKNOWN")

    def test_launcher_error_remains_unknown_not_a_retryable_failed_call(self) -> None:
        from unittest.mock import patch

        import daemon_wiring
        from daemon_wiring import StableModel

        wiring = self.wiring()
        wiring.raw_model = StableModel(wiring.root, {}, "FAKE-offline", wiring.timeline_id)
        with patch.object(
            daemon_wiring.launcher,
            "launch",
            side_effect=daemon_wiring.launcher.LauncherError("synthetic unknown dispatch"),
        ):
            with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
                wiring.model("{}", "thought")
        with wiring.connect() as db:
            self.assertEqual(
                db.execute("SELECT state FROM budget").fetchone()[0], "dispatched_unknown"
            )
        with self.assertRaisesRegex(RuntimeError, "ModelBudgetDeferred"):
            wiring.model("{}", "thought")

    def test_context_trimming_keeps_candidate_origins_linked(self) -> None:
        from daemon_wiring import CONTEXT_BYTE_LIMIT, bounded_context

        context = {
            "instructions": "protocol",
            "pending_memories": [
                {"origin_id": "memory:a", "text": "a" * CONTEXT_BYTE_LIMIT},
                {"origin_id": "memory:b", "text": "b" * CONTEXT_BYTE_LIMIT},
            ],
            "beliefs": [],
            "pending_observations": [],
            "cached_relevance": [],
            "relevance_candidates": [{"belief_id": "memory:a"}, {"belief_id": "memory:b"}],
        }
        result = bounded_context(context)
        origins = {row["origin_id"] for row in result["pending_memories"]}
        self.assertTrue({row["belief_id"] for row in result["relevance_candidates"]} <= origins)

    def test_late_day_soft_threshold_still_tapers_before_hard_cap(self) -> None:
        from daemon_wiring import CALL_REFILL_SECONDS, MODEL_CALLS_PER_DAY

        self.now = datetime(2026, 10, 16, 23, 50, tzinfo=CST).timestamp()
        wiring = self.wiring()
        with wiring.connect() as db:
            db.executemany(
                "INSERT INTO budget(day,phase,state) VALUES(?,?,'completed')",
                [(wiring.day(), "thought")] * int(MODEL_CALLS_PER_DAY * 0.9),
            )
        self.assertGreater(wiring.budget_status()["interval_seconds"], CALL_REFILL_SECONDS)
        self.assertLessEqual(wiring.budget_status()["tokens"], 1)

    def test_compaction_does_not_reset_existing_failure_backoff(self) -> None:
        wiring = self.wiring()
        wiring.poll()
        identity = wiring.mind.context()["daemon_pending_senses"][0]["id"]
        with wiring.connect() as db:
            db.execute(
                "INSERT INTO sense_retry VALUES(?,4,?,'pending','OSError')",
                (identity, self.now + 300),
            )
        for n in range(64):
            wiring.mind.ingest("tool_result", {"daemon_event_id": str(n)}, source="synthetic")
        wiring.compact_pending()
        summary = wiring.mind.context()["daemon_pending_senses"][0]["id"]
        with wiring.connect() as db:
            row = db.execute(
                "SELECT failures,next_at,state FROM sense_retry WHERE event_id=?", (summary,)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 4)
        self.assertEqual(row[1], self.now + 300)
        self.assertEqual(row[2], "pending")
        self.assertFalse(wiring.think())

    def test_daemon_entrypoint_exists(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("jarvis_daemon"))

    def test_sensor_reaches_model_and_reviewed_question_reaches_outreach(self) -> None:
        wiring = self.wiring()
        self.assertEqual(wiring.poll(), 1)
        self.assertTrue(wiring.think())
        wiring.contact()
        events = [
            r
            for r in wiring.mind.store.entries("event")
            if r["payload"]["data"].get("daemon_event_id")
        ]
        self.assertEqual(len(events), 1)
        calls = wiring.mind.store.entries("model_call")
        self.assertTrue(any(e["id"] in json.dumps(c["payload"]) for e in events for c in calls))
        questions = [r for r in wiring.mind.questions() if r["state"] == "ask_sir"]
        self.assertEqual(len(questions), 1)
        with sqlite3.connect(self.root / "outreach.db") as db:
            queued = db.execute("SELECT payload FROM intents").fetchall()
        self.assertEqual(len(queued), 1)
        queued_intent = json.loads(queued[0][0])
        self.assertTrue(queued_intent["needs_response"])
        self.assertEqual(queued_intent["reviewed_question"], questions[0]["text"])
        self.assertEqual(
            [c["payload"]["phase"] for c in calls], ["thought", "question", "question_review"]
        )

    def test_failed_round_requeues_without_consumption_then_recovers(self) -> None:
        wiring = self.wiring()
        wiring.poll()
        good = wiring.raw_model

        def broken(prompt: str) -> Any:
            raise OSError("synthetic failure")

        wiring.raw_model = broken
        self.assertFalse(wiring.think())
        self.assertEqual(wiring.mind.store.entries("daemon_sense_consumed"), [])
        calls = len(wiring.mind.store.entries("model_call"))
        wiring.think()
        self.assertEqual(len(wiring.mind.store.entries("model_call")), calls)
        wiring.raw_model = good
        self.now += 3600
        self.assertTrue(wiring.think())
        self.assertEqual(len(wiring.mind.store.entries("daemon_sense_consumed")), 1)
        restarted = self.wiring()
        self.assertEqual(restarted.mind.context()["daemon_pending_senses"], [])
        restarted.think()
        self.assertEqual(len(restarted.mind.store.entries("daemon_sense_consumed")), 1)

    def test_unknown_dispatch_stays_unknown_and_new_thought_can_continue(self) -> None:
        wiring = self.wiring()
        wiring.poll()
        with wiring.connect() as db:
            db.execute(
                "INSERT INTO budget(day,phase,state) VALUES(?,?,?)",
                (wiring.day(), "thought", "dispatched_unknown"),
            )
        self.assertTrue(wiring.think())
        self.assertTrue(wiring.mind.store.entries("daemon_sense_consumed"))
        with wiring.connect() as db:
            self.assertEqual(
                db.execute("SELECT state FROM budget WHERE id=1").fetchone()[0],
                "dispatched_unknown",
            )

    def test_partial_thought_write_never_consumes_the_batch(self) -> None:
        wiring = self.wiring()
        wiring.poll()
        base = wiring.raw_model

        def two_thoughts(prompt: str) -> Any:
            result = base(prompt)
            parsed = json.loads(result["text"])
            if "thoughts" in parsed:
                import copy

                second = copy.deepcopy(parsed["thoughts"][0])
                second["id"] = "t2"
                parsed["thoughts"].append(second)
                result["text"] = json.dumps(parsed)
            return result

        original = wiring.mind.store.append
        persisted = 0

        def interrupted(category: str, payload: Any, **kwargs: Any) -> Any:
            nonlocal persisted
            if category == "thought":
                persisted += 1
                if persisted == 2:
                    raise OSError("crash in multi-thought commit")
            return original(category, payload, **kwargs)

        wiring.raw_model = two_thoughts
        wiring.mind.store.append = interrupted
        with self.assertRaises(OSError):
            wiring.think()
        restarted = self.wiring()
        self.assertFalse(restarted.mind.store.entries("daemon_sense_consumed"))
        self.assertTrue(restarted.mind.context()["daemon_pending_senses"])

    def test_persisted_thought_crash_is_reconciled_without_model_replay(self) -> None:
        wiring = self.wiring()
        wiring.poll()
        original = wiring.reconcile_consumption
        count = 0

        def interrupted() -> None:
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("crash after thought commit")
            original()

        wiring.reconcile_consumption = interrupted
        with self.assertRaises(OSError):
            wiring.think()
        self.assertFalse(wiring.mind.store.entries("daemon_sense_consumed"))
        self.assertTrue(wiring.mind.store.entries("thought"))
        restarted = self.wiring()
        self.assertEqual(len(restarted.mind.store.entries("daemon_sense_consumed")), 1)
        self.assertEqual(restarted.mind.context()["daemon_pending_senses"], [])

    def test_invalid_model_response_does_not_consume_perceptions(self) -> None:
        wiring = self.wiring()
        wiring.poll()

        def invalid(prompt: str) -> Any:
            return {"text": "invalid json"}

        wiring.raw_model = invalid
        self.assertFalse(wiring.think())
        self.assertFalse(wiring.mind.store.entries("daemon_sense_consumed"))
        self.assertTrue(wiring.mind.store.entries("validation_error"))

    def test_failures_degrade_heartbeat_and_suspend_poison_batch(self) -> None:
        from daemon_wiring import SENSE_FAILURE_LIMIT
        from jarvis_daemon import Daemon

        wiring = self.wiring()
        wiring.poll()

        def broken(prompt: str) -> Any:
            raise OSError("synthetic failure")

        wiring.raw_model = broken
        for _ in range(SENSE_FAILURE_LIMIT):
            wiring.think()
            self.now += 3600
        daemon = Daemon(wiring, clock=lambda: self.now)
        daemon.heartbeat("idle")
        health = json.loads((self.root / "daemon.heartbeat").read_text())
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["cognition"]["recent_failures"], SENSE_FAILURE_LIMIT)
        self.assertEqual(health["cognition"]["last_error_type"], "OSError")
        self.assertEqual(wiring.mind.store.entries("daemon_sense_consumed"), [])
        suspended = wiring.mind.store.entries("daemon_sense_suspended")
        self.assertEqual(len(suspended), 1)
        self.assertEqual(suspended[0]["payload"]["reason"], "failure_limit")
        self.assertEqual(wiring.mind.context()["daemon_pending_senses"], [])
        wiring.raw_model = __import__("daemon_wiring").FixtureModel()
        wiring.think()
        daemon.heartbeat("idle")
        self.assertEqual(
            json.loads((self.root / "daemon.heartbeat").read_text())["status"], "healthy"
        )

    def test_oversized_multibyte_context_is_trimmed_before_runtime_guard(self) -> None:
        from context_limits import INNER_CONTEXT_BYTE_LIMIT as CONTEXT_BYTE_LIMIT

        wiring = self.wiring()
        for number in range(20):
            wiring.mind.ingest(
                "tool_result",
                {"daemon_event_id": str(number), "value": "感知" * 10000},
                source="synthetic",
            )
        context = wiring.mind.context()
        self.assertLessEqual(
            len(json.dumps(context, ensure_ascii=False).encode()), CONTEXT_BYTE_LIMIT
        )
        self.assertGreater(
            context["daemon_context_omitted"].get("events", 0)
            + context.get("context_omitted", {}).get("events", 0),
            0,
        )
        seen: list[str] = []
        good = wiring.raw_model

        def capture(prompt: str) -> Any:
            seen.append(prompt)
            return good(prompt)

        wiring.raw_model = capture
        self.assertTrue(wiring.think())
        self.assertTrue(seen)
        self.assertTrue(all(len(p.encode()) <= CONTEXT_BYTE_LIMIT for p in seen))
        self.assertFalse(wiring.mind.store.entries("context_error"))

    def test_context_budget_aliases_share_one_source(self) -> None:
        import daemon_wiring
        import inner_model
        import jarvis_daemon
        from context_limits import INNER_CONTEXT_BYTE_LIMIT

        self.assertEqual(jarvis_daemon.CONTEXT_BYTE_LIMIT, INNER_CONTEXT_BYTE_LIMIT)
        self.assertEqual(daemon_wiring.CONTEXT_BYTE_LIMIT, INNER_CONTEXT_BYTE_LIMIT)
        self.assertEqual(inner_model.INNER_CONTEXT_BYTE_LIMIT, INNER_CONTEXT_BYTE_LIMIT)

    def test_auxiliary_prefix_and_multibyte_context_fit_shared_budget(self) -> None:
        from context_limits import INNER_CONTEXT_BYTE_LIMIT

        wiring = self.wiring()
        seen: list[str] = []

        def capture(prompt: str) -> Any:
            seen.append(prompt)
            return {"text": "synthetic"}

        wiring.raw_model = capture
        prefix = "复盘材料："
        wiring.model(prefix + json.dumps({"events": ["界" * 10000]}, ensure_ascii=False), "task")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith(prefix))
        self.assertLessEqual(len(seen[0].encode("utf-8")), INNER_CONTEXT_BYTE_LIMIT)
        self.assertIn("daemon_context_omitted", json.loads(seen[0][len(prefix) :]))

    def test_initial_backfill_summarizes_history_but_keeps_new_arrivals_raw(self) -> None:
        from datetime import UTC
        from unittest.mock import patch

        import daemon_wiring
        from senses.base import Provenance, SenseEvent

        wiring = self.wiring()
        old = datetime.fromtimestamp(self.now - 86400 * 2, UTC)
        recent = datetime.fromtimestamp(self.now - 10, UTC)

        def event(number: int, stamp: datetime) -> SenseEvent:
            return SenseEvent(
                "synthetic",
                "speech",
                stamp,
                recent,
                f"private-{number}",
                {"secret": number},
                "high",
                Provenance("synthetic", str(number)),
            )

        events = [event(n, old) for n in range(20000)] + [event(20001, recent)]
        real_poll = daemon_wiring.senses.poll_all

        def poll(**kwargs: Any) -> list[SenseEvent]:
            real_poll(**kwargs)  # Create the real staged cursor database.
            return events

        with patch.object(daemon_wiring.senses, "poll_all", side_effect=poll):
            self.assertEqual(wiring.poll(), 2)
            events = [event(20002, old)]  # Late arrival is new, even with an old observation time.
            self.assertEqual(wiring.poll(), 1)
        rows = wiring.mind.store.entries("event")
        summaries = [
            r for r in rows if r["payload"]["data"]["sense_event"]["kind"] == "backfill_summary"
        ]
        self.assertEqual(len(summaries), 1)
        summary = summaries[0]["payload"]["data"]["sense_event"]
        self.assertEqual(summary["payload"]["count"], 20000)
        self.assertNotIn("private-", json.dumps(summary))
        self.assertNotIn("secret", json.dumps(summary))
        self.assertEqual(len(rows), 3)

    def test_backfill_replay_after_cursor_crash_does_not_expand_summary(self) -> None:
        from datetime import UTC
        from unittest.mock import patch

        import daemon_wiring
        from senses.base import Provenance, SenseEvent

        wiring = self.wiring()
        stamp = datetime.fromtimestamp(self.now - 86400, UTC)
        event = SenseEvent(
            "synthetic",
            "speech",
            stamp,
            stamp,
            "private",
            {"secret": 1},
            "high",
            Provenance("synthetic", "one"),
        )
        original_poll = daemon_wiring.senses.poll_all

        def source(**kwargs: Any) -> list[SenseEvent]:
            original_poll(**kwargs)
            return [event]

        with patch.object(daemon_wiring.senses, "poll_all", side_effect=source):
            with patch.object(daemon_wiring.os, "replace", side_effect=OSError("cursor crash")):
                with self.assertRaises(OSError):
                    wiring.poll()
            restarted = self.wiring()
            self.assertEqual(restarted.poll(), 1)
            self.assertEqual(restarted.poll(), 0)
        summaries = restarted.mind.store.entries("event")
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["payload"]["data"]["sense_event"]["payload"]["count"], 1)

    def test_claimed_todo_uses_model_and_is_not_lost_after_generation_crash(self) -> None:
        wiring = self.wiring()
        with sqlite3.connect(self.root / "affect.db") as db:
            db.execute(
                "INSERT INTO todos(created_at,task,recovery) VALUES(?,?,?)",
                (self.now - 86400, "synthetic task", "synthetic"),
            )
        wiring.tasks()
        self.assertEqual(wiring.used("task"), 1)
        with sqlite3.connect(self.root / "affect.db") as db:
            self.assertEqual(db.execute("SELECT status FROM todos").fetchone()[0], "resumed")
            self.assertEqual(
                db.execute("SELECT count(*) FROM dialogue_todo_delivery").fetchone()[0], 1
            )
        wiring.tasks()
        self.assertEqual(wiring.used("task"), 1)

    def test_reflection_model_is_budgeted_and_lessons_enter_both_contexts(self) -> None:
        from datetime import UTC
        from unittest.mock import patch

        wiring = self.wiring()
        # Wiring's fixture clock does not control MindStore's wall-clock writes.
        with patch("inner_loop.datetime", wraps=datetime) as store_datetime:
            store_datetime.now.return_value = datetime.fromtimestamp(self.now, UTC)
            wiring.poll()
        wiring.reflect("2026-10-16")
        self.assertEqual(wiring.used("reflection"), 1)
        self.assertTrue(wiring.dialogue_context()["lessons"])
        self.assertTrue(wiring.mind.context()["lessons"])

    def test_restart_replay_does_not_duplicate_event_or_outreach(self) -> None:
        wiring = self.wiring()
        wiring.poll()
        wiring.think()
        wiring.contact()
        timeline = wiring.timeline_id
        restarted = self.wiring()
        self.assertEqual(restarted.timeline_id, timeline)
        self.assertEqual(restarted.poll(), 0)
        restarted.contact()
        events = [
            r
            for r in restarted.mind.store.entries("event")
            if r["payload"]["data"].get("daemon_event_id")
        ]
        self.assertEqual(len(events), 1)
        with sqlite3.connect(self.root / "outreach.db") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM intents").fetchone()[0], 1)
        self.assertEqual(restarted.used(), 3)

    def test_reflection_at_2330_once_including_restart(self) -> None:
        from jarvis_daemon import Daemon

        wiring = self.wiring()
        daemon = Daemon(wiring, clock=lambda: self.now)
        daemon.tick()
        self.assertEqual(wiring.used("reflection"), 0)
        self.now += 60
        daemon.tick()
        daemon.tick()
        Daemon(self.wiring(), clock=lambda: self.now).tick()
        self.assertEqual(wiring.used("reflection"), 1)

    def test_failure_isolated_and_heartbeat_changes(self) -> None:
        from jarvis_daemon import Daemon

        wiring = self.wiring()

        def broken() -> int:
            raise OSError("synthetic")

        wiring.poll = broken
        daemon = Daemon(wiring, clock=lambda: self.now)
        daemon.tick()
        before = json.loads((self.root / "daemon.heartbeat").read_text())
        self.now += 16
        daemon.tick()
        after = json.loads((self.root / "daemon.heartbeat").read_text())
        self.assertGreater(after["ticks"], before["ticks"])
        with wiring.connect() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM log WHERE event='stage_error'").fetchone()[0], 2
            )
        self.assertGreaterEqual(wiring.used("thought"), 1)

    def test_model_budget_persists_on_restart_and_renews_next_day(self) -> None:
        from daemon_wiring import MODEL_CALLS_PER_DAY

        wiring = self.wiring()
        with wiring.connect() as db:
            db.executemany(
                "INSERT INTO budget(day,phase,state) VALUES(?,?,?)",
                [(wiring.day(), "thought", "completed")] * MODEL_CALLS_PER_DAY,
            )
        self.assertFalse(self.wiring().think())
        self.now += 86400
        self.assertTrue(self.wiring().think())

    def test_reflection_has_reserved_daily_budget(self) -> None:
        from daemon_wiring import MODEL_CALLS_PER_DAY

        wiring = self.wiring()
        with wiring.connect() as db:
            db.executemany(
                "INSERT INTO budget(day,phase,state) VALUES(?,?,?)",
                [(wiring.day(), "thought", "completed")] * (MODEL_CALLS_PER_DAY - 1),
            )
        self.assertFalse(wiring.think())
        wiring.reflect("2026-10-16")
        self.assertEqual(wiring.used("reflection"), 1)

    def test_stop_at_phase_boundary_starts_no_more_work(self) -> None:
        from jarvis_daemon import Daemon

        wiring = self.wiring()
        daemon = Daemon(wiring, clock=lambda: self.now)
        wiring.think = lambda: daemon.stopping.set()

        def unexpected() -> None:
            self.fail("work started after stop")

        wiring.contact = unexpected
        wiring.tasks = unexpected
        wiring.dispatch = unexpected
        daemon.tick()

    def test_pending_sensor_batch_is_visible_to_mind(self) -> None:
        wiring = self.wiring()
        for number in range(12):
            wiring.mind.ingest("tool_result", {"daemon_event_id": str(number)}, source="synthetic")
        context = wiring.mind.context()
        # 上下文按字节预算裁剪，pending senses 属于可裁剪项；断言的是
        # "没有被静默丢掉"：在场的条数 + 被记入 omitted 的条数必须等于 12。
        shown = len(context["daemon_pending_senses"])
        trimmed = context.get("context_omitted", {}).get("daemon_pending_senses", 0)
        self.assertEqual(shown + trimmed, 12)

    def test_generated_todo_result_recovered_without_model_retry(self) -> None:
        wiring = self.wiring()
        with sqlite3.connect(self.root / "affect.db") as db:
            db.execute(
                "INSERT INTO todos(created_at,task,recovery) VALUES(?,?,?)",
                (self.now - 86400, "synthetic task", "synthetic"),
            )

        def interrupted(key: str, text: str, reason: str) -> str:
            raise OSError("synthetic enqueue interruption")

        wiring.enqueue_once = interrupted
        with self.assertRaises(OSError):
            wiring.tasks()
        restarted = self.wiring()
        restarted.tasks()
        self.assertEqual(restarted.used("task"), 1)
        with sqlite3.connect(self.root / "outreach.db") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_quiet_backlog_gets_model_digest_without_database_lock(self) -> None:
        from jarvis_daemon import CST

        wiring = self.wiring()
        self.now = datetime(2026, 10, 16, 3, 0, tzinfo=CST).timestamp()
        wiring.enqueue_once("synthetic-quiet", "synthetic body", "synthetic")
        wiring.dispatch()
        self.now = datetime(2026, 10, 16, 7, 31, tzinfo=CST).timestamp()
        wiring.dispatch()
        with sqlite3.connect(self.root / "outreach.db") as db:
            row = db.execute("SELECT kind,text FROM deliveries").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "quiet")
        self.assertEqual(wiring.used("digest"), 1)

    def test_no_perception_no_outreach_negative_control(self) -> None:
        wiring = self.wiring()
        wiring.think()
        wiring.contact()
        with sqlite3.connect(self.root / "outreach.db") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_real_launcher_offline_http_keeps_internal_json_off_public_timeline(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        import chat_launcher as launcher
        from daemon_wiring import StableModel

        calls: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                calls.append(self.rfile.read(int(self.headers["Content-Length"])).decode())
                response = json.dumps(
                    {
                        "id": "synthetic-http",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "fake-test-model",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps({"synthetic": True}),
                                },
                            }
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        route = {
            "model": "fake-test-model",
            "provider": "custom",
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "api_mode": "chat_completions",
            "context_length": 256000,
            "reasoning_effort": "low",
        }
        root = launcher.prepare_root(self.root)
        timeline = launcher.timeline_identity(root)
        model = StableModel(
            root, route, "FAKE-DAEMON-NONSECRET", timeline, test_port=server.server_port
        )
        from context_limits import INNER_CONTEXT_BYTE_LIMIT

        prompts = [
            json.dumps(
                {
                    "outreach_intent": {"reason": "synthetic event"},
                    "instructions": "FAKE outward system instruction",
                }
            ),
            json.dumps({"synthetic": "internal"}),
            "x" * INNER_CONTEXT_BYTE_LIMIT,
            "界" * (INNER_CONTEXT_BYTE_LIMIT // 3) + "x" * (INNER_CONTEXT_BYTE_LIMIT % 3),
            "\0" * INNER_CONTEXT_BYTE_LIMIT,
        ]
        for prompt in prompts:
            with self.subTest(prompt_bytes=len(prompt.encode("utf-8")), first=repr(prompt[:1])):
                result = model(prompt)
                self.assertEqual(result["text"], json.dumps({"synthetic": True}))
                self.assertEqual(result["session_id"], timeline)
        self.assertEqual(len(calls), len(prompts))
        self.assertEqual(
            json.loads(calls[0])["messages"][0],
            {"role": "system", "content": "FAKE outward system instruction"},
        )
        from inner_voice import SYSTEM_PROMPT

        self.assertEqual(
            json.loads(calls[1])["messages"][0], {"role": "system", "content": SYSTEM_PROMPT}
        )
        self.assertEqual(launcher.read_timeline(root), [])
        with sqlite3.connect(root / "engine-home/state.db") as db:
            sessions = db.execute("SELECT id FROM sessions").fetchall()
        self.assertEqual(sessions, [(timeline,)])

    def test_delivery_visible_once_in_public_timeline(self) -> None:
        import chat_launcher as launcher

        wiring = self.wiring()
        wiring.poll()
        wiring.think()
        wiring.contact()
        wiring.dispatch()
        wiring.dispatch()
        timeline = launcher.read_timeline(self.root)
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0]["role"], "assistant")
        self.assertTrue(timeline[0]["content"].startswith("synthetic:"))
        self.wiring().dispatch()
        self.assertEqual(len(launcher.read_timeline(self.root)), 1)

    def test_stage_deadline_bypasses_module_exception_fallback(self) -> None:
        import time

        from jarvis_daemon import Daemon

        wiring = self.wiring()
        daemon = Daemon(wiring, clock=lambda: self.now)
        fallback: list[bool] = []

        def stalled() -> None:
            try:
                time.sleep(1)
            except Exception:
                fallback.append(True)
                time.sleep(1)

        started = time.monotonic()
        daemon.stage("synthetic-stall", stalled, 0.03)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(fallback, [])

    def test_sensor_cursor_crash_recovers_without_duplicate_ingestion(self) -> None:
        from unittest.mock import patch

        import daemon_wiring

        wiring = self.wiring()
        real_replace = daemon_wiring.os.replace

        def interrupted(source: Path, target: Path) -> None:
            if target.name == "senses_cursor.db":
                raise OSError("synthetic cursor publication crash")
            real_replace(source, target)

        with patch.object(daemon_wiring.os, "replace", side_effect=interrupted):
            with self.assertRaises(OSError):
                wiring.poll()
        restarted = self.wiring()
        self.assertEqual(restarted.poll(), 1)
        self.assertEqual(restarted.poll(), 0)


if __name__ == "__main__":
    unittest.main()
