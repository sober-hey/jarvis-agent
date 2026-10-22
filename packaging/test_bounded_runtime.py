"""Regression tests for history-independent runtime reads (no live models)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import capabilities
from curiosity import Curiosity
from inner_loop import MindStore, Record
from inner_runtime import InnerLoop
from test_inner_loop import answer


class BoundedRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        # Force isolated TMPDIR: avoid overlap with host Hermes home
        isolated_tmp = Path.home() / ".jarvis" / "tmp-test"
        isolated_tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=str(isolated_tmp))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        self.store = MindStore(self.root)

    def forbid_unbounded(self) -> object:
        original = self.store.entries

        def bounded(category: str | None = None, limit: int | None = None) -> list[Record]:
            if limit is None:
                raise AssertionError(f"unbounded entries: {category}")
            return original(category, limit)

        return patch.object(self.store, "entries", side_effect=bounded)

    def test_step_preserves_round_budget_without_unbounded_history(self) -> None:
        # Missing phase is a legacy thought; explicit null is not a thought.
        self.store.append("model_call", {"state": "dispatched_unknown"}, source="test")
        self.store.append("model_call", {"phase": None}, source="test")
        loop = InnerLoop(self.store, answer, catalog=[], max_rounds=1)
        with self.forbid_unbounded():  # type: ignore[attr-defined]
            self.assertFalse(loop.step())
        self.assertEqual(len(self.store.entries("model_call")), 2)

    def test_task_directed_recall_is_bounded_audited_and_reproducible(self) -> None:
        # R2 replaced legacy 2-row random recall with lexical RRF recall plus at
        # most one labeled exploration row. Keep the bounded-read guarantee and
        # an auditable, seed-reproducible exploration choice.
        thoughts = [
            self.store.append("thought", {"text": str(i)}, source="test") for i in range(90)
        ]
        memories = [
            {"id": "m1", "content": "one", "created_at": "2000-01-01T00:00:00+00:00", "version": 1}
        ]
        seed = 415

        def run() -> Record:
            with patch("curiosity.random.SystemRandom") as system:
                system.return_value.getrandbits.return_value = seed
                with self.forbid_unbounded():  # type: ignore[attr-defined]
                    return Curiosity(self.store).context(memories, [], 1)

        context = run()
        self.assertEqual(
            [r["id"] for r in context["recent_thoughts"]], [r["id"] for r in thoughts[-8:]]
        )
        self.assertEqual(context["recall_strategy"], "lexical-rrf-v1")
        explorations = [r for r in context["recalled"] if r.get("recall_role") == "exploration"]
        self.assertLessEqual(len(explorations), 1)
        recent_ids = {r["id"] for r in thoughts[-8:]}
        self.assertFalse(recent_ids & {r["id"] for r in explorations})
        audit = self.store.entries("recall", 1)[-1]["payload"]
        self.assertEqual(audit["strategy"], "lexical-rrf-v1")
        self.assertEqual(audit["seed"], seed)
        self.assertEqual(audit["recalled_ids"], [r["id"] for r in context["recalled"]])
        self.assertEqual(audit["exploration_ids"], [r["id"] for r in explorations])
        # Same seed and unchanged ledger reproduce the same exploration choice.
        again = run()
        self.assertEqual(
            [r["id"] for r in again["recalled"] if r.get("recall_role") == "exploration"],
            audit["exploration_ids"],
        )

    def test_full_runtime_and_question_dedup_never_read_unbounded_entries(self) -> None:
        loop = InnerLoop(self.store, answer, catalog=[], max_rounds=1)
        question = {
            "text": "  ＡＢＣ, Book? ",
            "selected_capability": None,
            "missing_information": "unknown",
        }
        loop.route({"question": question, "refs": []}, "first")
        loop.state("old", "old", "ask_sir", refs=[])
        loop.state("old", "old", "resolved", refs=[])
        loop.state("new", "new", "ask_sir", refs=[])
        with self.forbid_unbounded():  # type: ignore[attr-defined]
            loop.route({"question": {**question, "text": "abc book"}, "refs": []}, "duplicate")
            self.assertEqual([r["question_id"] for r in loop.questions()], ["new"])
            self.assertTrue(loop.step())
            loop.generate_question(
                {"kind": "ask_sir", "belief_id": "unknown", "reason": "missing"}, 2
            )
        self.assertEqual(len(self.store.entries("action_request")), 1)

    def test_incremental_events_preserve_newest_evidence_and_restart(self) -> None:
        catalog = capabilities.catalog(adapters=[])
        loop = InnerLoop(self.store, answer, catalog=catalog)
        events: list[Record] = []
        for day in (15, 14):
            events.append(
                loop.ingest(
                    "tool_result",
                    {
                        "sense_event": {
                            "source": "activitywatch",
                            "kind": "app_usage",
                            "payload": {},
                            "observed_at": f"2026-10-{day}T01:00:00+00:00",
                        }
                    },
                    source="senses:activitywatch",
                )
            )
        with self.forbid_unbounded():  # type: ignore[attr-defined]
            context = loop.context()
            self.assertEqual([r["id"] for r in context["pending_observations"]], [events[0]["id"]])
            loop = InnerLoop(self.store, answer, catalog=catalog)
            self.assertEqual(
                [r["id"] for r in loop.context()["pending_observations"]], [events[0]["id"]]
            )
        self.assertNotIn("PRIVATE", json.dumps(context))


if __name__ == "__main__":
    unittest.main()
