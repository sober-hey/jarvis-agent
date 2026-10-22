"""Offline scheduler tests: no external message, search or model calls."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

SCRATCH = Path("/Users/sober/.hermes/cache/scratch/evolution-scheduler").resolve()


class FakeClock:
    def __init__(self, at: datetime) -> None:
        self.value = at.timestamp()

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeMind:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.rows: list[dict[str, Any]] = []

    def append(
        self, category: str, payload: dict[str, Any], *, source: str, refs: list[str] | None = None
    ) -> dict[str, Any]:
        row = dict(
            id=str(len(self.rows) + 1),
            category=category,
            payload=payload,
            source=source,
            refs=refs or [],
        )
        self.rows.append(row)
        return row

    def recent(
        self, category: str, limit: int = 1, *, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in reversed(self.rows)
            if row["category"] == category
            and all(row["payload"].get(k) == v for k, v in (where or {}).items())
        ][:limit]

    def count(self, category: str) -> int:
        return sum(row["category"] == category for row in self.rows)


class FakeOutreach:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def enqueue(self, text: str, **kwargs: Any) -> str:
        self.messages.append(dict(text=text, **kwargs))
        return str(len(self.messages))


class FakeEvolution:
    def __init__(self, root: Path) -> None:
        self.mind_store = FakeMind(root)
        self.calls: list[datetime | None] = []
        self._modules: dict[str, Any] = {}
        self.outreach = None
        self.last_session_errors: list[dict[str, Any]] = []

    def get_pending_proposals(self) -> list[Any]:
        return []

    def run_weekly_if_due(self, now: datetime | None = None) -> list[Any]:
        self.calls.append(now)
        self.mind_store.append(
            "evolution_research_session",
            {
                "topics": [],
                "proposal_ids": [],
                "errors": [],
            },
            source="self_evolution",
        )
        return []


class EvolutionSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        SCRATCH.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.temp = tempfile.TemporaryDirectory(prefix="root-", dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = FakeClock(datetime(2026, 10, 18, 15, 59, 59, tzinfo=UTC))
        self.evolution: Any = FakeEvolution(self.root)
        self.outreach: Any = FakeOutreach()

    def make_scheduler(self, **kwargs: Any) -> Any:
        self.assertIsNotNone(
            importlib.util.find_spec("evolution_scheduler"),
            "EvolutionScheduler has not been implemented",
        )
        cls = importlib.import_module("evolution_scheduler").EvolutionScheduler
        scheduler = cls(self.root, self.evolution, self.outreach, self.clock, **kwargs)
        self.addCleanup(lambda: scheduler.wait(3))
        return scheduler

    def test_restart_preserves_due_date(self):
        self.clock.advance(1)
        scheduler = self.make_scheduler()
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        state = self.root / "evolution_schedule.json"
        self.assertTrue(state.is_file(), "successful runs must persist the next due instant")
        restarted = self.make_scheduler()
        self.assertFalse(restarted.tick())
        self.assertEqual(len(self.evolution.calls), 1)
        self.clock.advance(timedelta(days=7).total_seconds())
        self.assertTrue(restarted.tick())
        self.assertTrue(restarted.wait(3))
        self.assertEqual(len(self.evolution.calls), 2)

    def test_failure_retries_after_six_hours_across_restart(self):
        self.clock.advance(1)
        scheduler = self.make_scheduler()
        original = self.evolution.run_weekly_if_due
        attempts: list[Any] = []

        def flaky(now: datetime | None = None) -> list[Any]:
            attempts.append(now)
            if len(attempts) == 1:
                raise RuntimeError("private-token-must-not-be-persisted")
            return original(now=now)

        self.evolution.run_weekly_if_due = flaky
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        self.assertEqual(len(attempts), 1)
        restarted = self.make_scheduler()
        self.clock.advance(timedelta(hours=6).total_seconds() - 1)
        self.assertFalse(restarted.tick())
        self.clock.advance(1)
        self.assertTrue(restarted.tick())
        self.assertTrue(restarted.wait(3))
        self.assertEqual(len(attempts), 2)
        self.assertNotIn("private-token", (self.root / "evolution_schedule.json").read_text())
        self.assertEqual(attempts[0].weekday(), 0)

    def test_concurrent_ticks_never_reenter_or_block_on_research(self):
        self.clock.advance(1)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        scheduler = self.make_scheduler()
        original = self.evolution.run_weekly_if_due

        def blocked(now: datetime | None = None) -> list[Any]:
            entered.set()
            release.wait(3)
            return original(now=now)

        self.evolution.run_weekly_if_due = blocked
        self.assertTrue(scheduler.tick())
        self.assertTrue(entered.wait(1))

        def tick_one(_index: int) -> bool:
            return scheduler.tick()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(tick_one, range(100)))
        self.assertFalse(any(results))
        self.assertEqual(self.evolution.calls, [])
        release.set()
        self.assertTrue(scheduler.wait(3))
        self.assertEqual(len(self.evolution.calls), 1)

    def test_tick_before_due_does_no_filesystem_or_database_work(self):
        scheduler = self.make_scheduler()
        with (
            patch("pathlib.Path.open", side_effect=AssertionError("unexpected IO")),
            patch("os.open", side_effect=AssertionError("unexpected IO")),
            patch.object(self.evolution.mind_store, "recent", side_effect=AssertionError("SQL")),
        ):
            for _ in range(100):
                self.assertFalse(scheduler.tick())

    def test_configurable_interval_is_persisted(self):
        self.clock.advance(1)
        scheduler = self.make_scheduler(interval_weeks=2)
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        restarted = self.make_scheduler()
        self.clock.advance(timedelta(days=7).total_seconds())
        self.assertFalse(restarted.tick())
        self.clock.advance(timedelta(days=7).total_seconds())
        self.assertTrue(restarted.tick())
        self.assertTrue(restarted.wait(3))
        self.assertEqual(len(self.evolution.calls), 2)

    def test_no_proposals_means_no_notification(self):
        self.clock.advance(1)
        scheduler = self.make_scheduler()
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        self.assertEqual(self.outreach.messages, [])
        cycles = self.evolution.mind_store.recent("evolution_cycle", 1)
        self.assertEqual(cycles[0]["payload"]["proposal_count"], 0)

    def test_single_chinese_message_and_evaluation_cycle_audit(self):
        self.clock.advance(1)
        module = SimpleNamespace(
            self_evaluate=lambda: {
                "module": "health",
                "domain": "sleep health",
                "current_version": "1",
                "known_limitations": ["不足"],
                "improvement_directions": ["改进"],
                "last_evaluated_at": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
            }
        )
        self.evolution._modules = {"health": module}
        proposals = [
            SimpleNamespace(
                proposal_id=str(i),
                topic="sleep health",
                summary="候选能力说明",
                skill_name="sleep_" + str(i),
                requires_human_approval=True,
            )
            for i in range(2)
        ]

        def research(now: datetime | None = None) -> list[Any]:
            self.evolution.calls.append(now)
            self.evolution.mind_store.append(
                "evolution_research_session",
                {
                    "topics": ["sleep health"],
                    "candidate_count": 3,
                    "proposal_ids": [p.proposal_id for p in proposals],
                    "errors": [],
                },
                source="self_evolution",
            )
            return proposals

        self.evolution.run_weekly_if_due = research
        scheduler = self.make_scheduler()
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        self.assertEqual(len(self.outreach.messages), 1)
        message = self.outreach.messages[0]
        self.assertTrue(message["text"].startswith("Sir：\n\n"))
        self.assertTrue(message["text"].endswith("\n\nJARVIS"))
        self.assertIn("Hermes", message["text"])
        self.assertIn("授权", message["text"])
        self.assertEqual(message["urgency"], "normal")
        self.assertEqual(message["channel"], "imessage")
        self.assertNotIn("recipient", message)
        evaluation = self.evolution.mind_store.recent("self_evaluation", 2)
        self.assertTrue(any(r["payload"].get("module") == "health" for r in evaluation))
        cycle = self.evolution.mind_store.recent("evolution_cycle", 1)[0]["payload"]
        self.assertEqual(cycle["domains"], ["sleep health"])
        self.assertEqual(cycle["candidate_count"], 3)
        self.assertEqual(cycle["proposal_count"], 2)

    def test_scheduler_self_evaluate_is_public_metadata_only(self):
        scheduler = self.make_scheduler()
        fields = scheduler.self_evaluate()
        self.assertEqual(
            set(fields),
            {
                "module",
                "domain",
                "current_version",
                "known_limitations",
                "improvement_directions",
                "last_evaluated_at",
            },
        )
        self.assertEqual(fields["module"], "evolution_scheduler")

    def test_separate_scheduler_instances_share_one_claim(self):
        self.clock.advance(1)
        first, second = self.make_scheduler(), self.make_scheduler()
        entered, release = threading.Event(), threading.Event()
        original = self.evolution.run_weekly_if_due

        def blocked(now: datetime | None = None) -> list[Any]:
            entered.set()
            release.wait(3)
            return original(now=now)

        self.evolution.run_weekly_if_due = blocked
        self.assertTrue(first.tick())
        self.assertTrue(entered.wait(1))
        self.assertTrue(second.tick())
        self.assertTrue(second.wait(1))
        release.set()
        self.assertTrue(first.wait(3))
        self.assertEqual(len(self.evolution.calls), 1)
        self.clock.advance(60)
        self.assertTrue(second.tick())
        self.assertTrue(second.wait(3))
        self.assertEqual(len(self.evolution.calls), 1)

    def test_retry_after_monday_uses_original_logical_monday(self):
        self.clock.value = datetime(2026, 10, 19, 15, 59, tzinfo=UTC).timestamp()
        scheduler = self.make_scheduler()
        original = self.evolution.run_weekly_if_due
        attempts: list[Any] = []

        def flaky(now: datetime | None = None) -> list[Any]:
            attempts.append(now)
            if len(attempts) == 1:
                raise RuntimeError("offline")
            return original(now=now)

        self.evolution.run_weekly_if_due = flaky
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        self.clock.advance(timedelta(hours=6).total_seconds())
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(attempts[1].weekday(), 0)

    def test_notification_failure_retries_persisted_digest_without_research(self):
        self.clock.advance(1)
        original = self.evolution.run_weekly_if_due

        def research(now: datetime | None = None) -> list[Any]:
            original(now=now)
            return [SimpleNamespace(proposal_id="p1", topic="health", summary="研究建议")]

        self.evolution.run_weekly_if_due = research
        scheduler = self.make_scheduler()
        with patch.object(self.outreach, "enqueue", side_effect=RuntimeError("unavailable")):
            self.assertTrue(scheduler.tick())
            self.assertTrue(scheduler.wait(3))
        self.assertEqual(scheduler.last_error_type, "RuntimeError")
        restarted = self.make_scheduler()
        self.clock.advance(timedelta(hours=6).total_seconds())
        self.assertTrue(restarted.tick())
        self.assertTrue(restarted.wait(3))
        self.assertEqual(len(self.evolution.calls), 1)
        self.assertEqual(len(self.outreach.messages), 1)
        self.assertEqual(restarted.last_error_type, None)

    def test_evaluation_failure_is_isolated(self):
        self.clock.advance(1)

        def broken() -> None:
            raise ValueError("private-exception")

        self.evolution._modules["broken"] = SimpleNamespace(self_evaluate=broken)
        scheduler = self.make_scheduler()
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        rows = self.evolution.mind_store.recent("self_evaluation", 10)
        self.assertTrue(
            any(
                r["payload"].get("module") == "broken"
                and r["payload"].get("error_type") == "ValueError"
                for r in rows
            )
        )
        self.assertNotIn("private-exception", json.dumps(rows))
        self.assertEqual(len(self.evolution.calls), 1)

    def test_state_is_private_atomic_and_corrupt_state_fails_closed(self):
        self.clock.advance(1)
        scheduler = self.make_scheduler()
        seen: list[tuple[Path, Path]] = []
        replace = os.replace

        def replacing(source: str, target: str) -> None:
            seen.append((Path(source).parent, Path(target)))
            return replace(source, target)

        with patch("evolution_scheduler.os.replace", side_effect=replacing):
            self.assertTrue(scheduler.tick())
            self.assertTrue(scheduler.wait(3))
        path = self.root / "evolution_schedule.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(seen)
        self.assertTrue(all(p == self.root and t == path for p, t in seen))
        self.assertEqual(list(self.root.glob(".evolution-schedule-*")), [])
        path.write_text("{invalid", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.make_scheduler()
        self.assertEqual(path.read_text(), "{invalid")

    def test_real_mind_store_self_evolution_and_outreach_integration(self):
        # Only bypass the production host-directory exclusion for this owned
        # scratch fixture; retain real root/file mode and symlink validation.
        import product_home
        from inner_loop import MindStore
        from outreach import Outreach
        from self_evolution import SelfEvolution
        from skill_registry import SkillRegistry

        self.clock.advance(1 + timedelta(hours=9).total_seconds())
        with patch.object(product_home, "protected_paths", return_value=()):
            mind = MindStore(self.root)
        queue = Outreach(self.root)

        # Construct on the daemon/main thread, exactly like production wiring.
        # A fake list_installed() hides cross-thread SQLite ProgrammingError.
        registry = SkillRegistry(self.root / "skills" / "registry.db")
        self.addCleanup(registry.close)

        def no_preload(_evolution: Any) -> None:
            pass

        loader = SimpleNamespace(
            search_fn=None,
            context=SimpleNamespace(outreach=queue),
            registry=registry,
        )
        with patch.object(SelfEvolution, "_preload_modules", no_preload):
            evolution = SelfEvolution(self.root, mind, cast(Any, loader))
        module = SimpleNamespace(
            self_evaluate=lambda: {
                "module": "health",
                "domain": "sleep health",
                "current_version": "1",
                "known_limitations": ["gap"],
                "improvement_directions": ["research"],
                "last_evaluated_at": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
            }
        )
        evolution.register_module(module)
        reviews: list[str] = []

        def reviewer(_prompt: str) -> dict[str, str]:
            reviews.append(_prompt)
            return {
                "text": json.dumps(
                    {
                        "estimated_value": 0.9,
                        "summary": "只读睡眠数据核验能力",
                        "skill_name": "sleep_check_" + str(len(reviews)),
                        "useful": True,
                        "novel": True,
                        "evidence_sufficient": True,
                        "requires_core_change": False,
                        "rationale": "matching fixture evidence",
                    }
                )
            }

        evolution.reviewer = reviewer
        candidates = [
            {
                "url": "https://github.com/example/research" + str(i),
                "updated": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
                "title": "fixture",
                "content": "evidence",
            }
            for i in range(3)
        ]
        self.evolution, self.outreach = evolution, queue
        scheduler = self.make_scheduler()

        def candidate_fixture(_self: Any, _topic: str) -> list[dict[str, str]]:
            return candidates

        with patch.object(SelfEvolution, "_candidates", candidate_fixture):
            self.assertTrue(scheduler.tick())
            self.assertTrue(scheduler.wait(3))
        self.assertIsNone(scheduler.last_error_type)
        self.assertEqual(mind.count("evolution_research_error"), 0)
        self.assertEqual(mind.count("evolution_review"), 3)
        self.assertEqual(registry.list_installed(), [])
        self.assertEqual(mind.count("evolution_proposal"), 3)
        self.assertTrue(all(p.requires_human_approval for p in evolution.get_pending_proposals()))
        cycle = mind.recent("evolution_cycle", 1)[0]["payload"]
        self.assertEqual(cycle["candidate_count"], 3)
        self.assertEqual(cycle["proposal_count"], 3)
        self.assertEqual(cycle["domains"], ["sleep health"])
        self.assertEqual(mind.count("self_evaluation"), 2)
        self.assertEqual(mind.count("evolution_notification_queued"), 0)
        with closing(sqlite3.connect(queue.db_path)) as db:
            messages = db.execute("SELECT text,urgency,channel FROM messages").fetchall()
            self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0][0].startswith("Sir：\n\n"))
        self.assertTrue(messages[0][0].endswith("\n\nJARVIS"))
        self.assertEqual(messages[0][1:], ("normal", "imessage"))
        self.assertEqual(mind.count("evolution_proposal_approval"), 0)
        self.assertEqual(mind.count("evolution_proposal_processed"), 0)
        deliveries = queue.dispatch_due(
            datetime.fromtimestamp(self.clock(), UTC), imessage_available=True
        )
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].channel, "imessage")
        # Real queue and its offline channel stub; no personal-message side effect.
        stub_paths = list((self.root / "channel-stubs" / "imessage").glob("*.json"))
        self.assertEqual(len(stub_paths), 1)
        stub = json.loads(stub_paths[0].read_text())
        self.assertTrue(stub["fake"])
        self.assertTrue(stub["delivery"]["text"].startswith("Sir：\n\n"))

    def test_started_sunday_but_missed_monday_catches_up_on_tuesday(self):
        scheduler = self.make_scheduler()
        self.clock.advance(timedelta(days=2).total_seconds())
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        self.assertEqual(len(self.evolution.calls), 1)
        self.assertEqual(self.evolution.calls[0].weekday(), 0)

    def test_in_progress_claim_survives_a_process_restart(self):
        self.clock.advance(1)
        now = self.clock()
        path = self.root / "evolution_schedule.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "interval_weeks": 1,
                    "phase": "research",
                    "cycle_id": "crashed",
                    "scheduled_for": now,
                    "next_due": now + timedelta(hours=6).total_seconds(),
                    "baseline_proposal_ids": [],
                }
            )
        )
        path.chmod(0o600)
        scheduler = self.make_scheduler()
        self.assertFalse(scheduler.tick())
        self.clock.advance(timedelta(hours=6).total_seconds())
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        self.assertEqual(len(self.evolution.calls), 1)

    def test_old_proposals_are_not_renotified(self):
        self.clock.advance(1)
        old = SimpleNamespace(proposal_id="old", topic="old", summary="older proposal")
        self.evolution.get_pending_proposals = lambda: [old]

        def research(now: datetime | None = None) -> list[Any]:
            self.evolution.calls.append(now)
            return [old]

        self.evolution.run_weekly_if_due = research
        scheduler = self.make_scheduler()
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        self.assertEqual(self.outreach.messages, [])

    def test_large_research_result_uses_one_bounded_summary(self):
        self.clock.advance(1)
        proposals = [
            SimpleNamespace(proposal_id=str(i), topic="领域" * 100, summary="摘要" * 1000)
            for i in range(100)
        ]

        def research(now: datetime | None = None) -> list[Any]:
            self.evolution.calls.append(now)
            return proposals

        self.evolution.run_weekly_if_due = research
        scheduler = self.make_scheduler()
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        self.assertEqual(len(self.outreach.messages), 1)
        message = self.outreach.messages[0]["text"]
        self.assertIn("100 项新提案", message)
        self.assertIn("其余 92 项", message)
        self.assertLess(len(message.encode("utf-8")), 16384)

    def test_worker_start_failure_backs_off(self):
        self.clock.advance(1)
        scheduler = self.make_scheduler()
        with patch("evolution_scheduler.threading.Thread.start", side_effect=RuntimeError()):
            self.assertFalse(scheduler.tick())
        self.assertEqual(scheduler.last_error_type, "RuntimeError")
        self.assertFalse(scheduler.tick())
        self.clock.advance(timedelta(hours=6).total_seconds())
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        self.assertEqual(len(self.evolution.calls), 1)

    def test_partial_proposals_survive_research_failure_then_retry(self):
        self.clock.advance(1)
        pending: list[Any] = []
        self.evolution.get_pending_proposals = lambda: list(pending)
        attempts: list[Any] = []

        def research(now: datetime | None = None) -> list[Any]:
            attempts.append(now)
            if len(attempts) == 1:
                pending.append(
                    SimpleNamespace(
                        proposal_id="partial", topic="sleep", summary="already persisted"
                    )
                )
                raise RuntimeError("failed after a proposal was committed")
            return []

        self.evolution.run_weekly_if_due = research
        scheduler = self.make_scheduler()
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        restarted = self.make_scheduler()
        self.clock.advance(timedelta(hours=6).total_seconds())
        self.assertTrue(restarted.tick())
        self.assertTrue(restarted.wait(3))
        self.assertEqual(len(self.outreach.messages), 1)
        self.assertIn("partial", self.outreach.messages[0]["text"])

    def test_initial_schedule_survives_restart_before_first_run(self):
        scheduler = self.make_scheduler()
        self.assertFalse(scheduler.tick())
        self.clock.advance(timedelta(days=2).total_seconds())
        restarted = self.make_scheduler()
        self.assertTrue(restarted.tick())
        self.assertTrue(restarted.wait(3))
        self.assertEqual(len(self.evolution.calls), 1)
        self.assertEqual(self.evolution.calls[0].weekday(), 0)

    def test_waits_until_monday_then_runs(self):
        scheduler = self.make_scheduler()
        self.assertFalse(scheduler.tick())
        self.assertEqual(self.evolution.calls, [])
        self.clock.advance(1)
        self.assertTrue(scheduler.tick())
        self.assertTrue(scheduler.wait(3))
        self.assertEqual(len(self.evolution.calls), 1)
        self.assertFalse(scheduler.tick())


if __name__ == "__main__":
    unittest.main()
