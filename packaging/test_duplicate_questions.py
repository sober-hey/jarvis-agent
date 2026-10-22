"""Negative controls for repeated questions; real disposable stores, fake model only."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chat_launcher import prepare_root
from daemon_wiring import CONTEXT_BYTE_LIMIT, ContextMind, FixtureModel, Wiring, bounded_context
from inner_loop import MindStore, Record
from inner_runtime import InnerLoop
from outreach import Outreach
from sir_profile import SirProfile


class DuplicateQuestionTests(unittest.TestCase):
    def setUp(self) -> None:
        parent = Path.home() / ".jarvis" / "tmp-test"
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = MindStore(self.root)

    def belief(
        self, identity: str, *, source: str = "self_report", value: str = "fixture"
    ) -> Record:
        return self.store.append_belief(
            belief_id=identity,
            subject="fixture-person",
            attribute=identity,
            value=value,
            source=source,
            confidence=0.9,
            last_verified="2026-10-20T00:00:00+00:00",
            importance=0.8,
            refs=[],
        )

    def test_review_reads_complete_self_reports_from_canonical_databases(self) -> None:
        profile = SirProfile(self.root)
        facts = [
            profile.upsert("identity", f"fact-{i}", "v" * 170, 0.9, "self_report")
            for i in range(18)
        ]
        profile.upsert("identity", "excluded", "not self report", 0.7, "inferred")
        versions = [self.belief("same", value="first"), self.belief("same", value="second")]
        self.belief("inferred-only", source="inferred")
        wire: list[Record] = []

        def model(prompt: str) -> Record:
            context: Record = json.loads(prompt)
            if "proposed_question" not in context:
                return {"text": json.dumps({"question": "Synthetic candidate?"})}
            wire.append(context)
            return {
                "text": json.dumps(
                    {
                        "repeats_self_report": bool(context.get("self_reports")),
                        "reason": "fixture evidence",
                    }
                )
            }

        loop = ContextMind(self.store, model, catalog=[])
        self.assertEqual(loop.memories.list(), [])
        loop.generate_question({"belief_id": "same", "kind": "ask_sir", "reason": "fixture"}, 1)
        self.assertEqual(len(wire), 1)
        reports = wire[0].get("self_reports", [])
        self.assertTrue({r["id"] for r in facts} <= {r.get("id") for r in reports})
        self.assertTrue({r["seq"] for r in versions} <= {r.get("seq") for r in reports})
        self.assertTrue(all(r["source"] == "self_report" for r in reports))
        self.assertEqual(self.store.count("question_rejected"), 1)
        self.assertEqual(loop.questions(), [])
        # The complete block is immutable under bounding, even if optional beliefs grow.
        context = {
            **wire[0],
            "beliefs": [{"belief_id": str(i), "padding": "x" * 2000} for i in range(20)],
        }
        self.assertEqual(bounded_context(context)["self_reports"], reports)
        # If even the required evidence cannot fit, never send a partial review.
        with self.assertRaises(ValueError):
            bounded_context(
                {
                    "instructions": "review",
                    "self_reports": [{"value": "x" * (CONTEXT_BYTE_LIMIT + 1)}],
                }
            )
        # Exercise the real generation path too: overflow must record a failed
        # review and leave no question available for outreach.
        profile.upsert("identity", "oversized", "x" * (CONTEXT_BYTE_LIMIT + 1), 0.9, "self_report")
        loop.generate_question(
            {
                "belief_id": "same",
                "question_id": "oversized",
                "kind": "ask_sir",
                "reason": "fixture",
            },
            2,
        )
        self.assertEqual(len(wire), 1)
        self.assertEqual(self.store.count("question_generation_failure"), 1)
        self.assertEqual(loop.questions(), [])

    def test_bounding_keeps_same_subject_beliefs_before_unrelated_priority(self) -> None:
        target = {
            "belief_id": "target",
            "subject": "fixture-person",
            "source": "self_report",
            "importance": 0.1,
            "padding": "x" * 1500,
        }
        unrelated = [
            {
                "belief_id": f"other-{i}",
                "subject": "unrelated",
                "source": "self_report",
                "importance": 1.0,
                "padding": "x" * 1500,
            }
            for i in range(10)
        ]
        context: Record = {
            "instructions": "review",
            "gap_subject": "fixture-person",
            "beliefs": [target, *unrelated],
        }
        snapshot = deepcopy(context)
        bounded = bounded_context(context)
        self.assertIn(target, bounded["beliefs"])
        self.assertLess(len(bounded["beliefs"]), len(context["beliefs"]))
        self.assertLessEqual(
            len(json.dumps(bounded, ensure_ascii=False).encode()), CONTEXT_BYTE_LIMIT
        )
        self.assertEqual(snapshot, context)
        # Flat/nested candidate metadata and a null non-question are accepted.
        for metadata in (
            {"question": {"gap_subject": "fixture-person"}},
            {"question": {"gap": {"subject": "fixture-person"}}},
            {"question": None, "belief": {"subject": "fixture-person"}},
        ):
            alternate = {**context, **metadata}
            alternate.pop("gap_subject")
            self.assertIn(target, bounded_context(alternate)["beliefs"])
        # Pruning a linked candidate must not indirectly delete the protected
        # belief either; the original paired-row invariant still holds.
        linked = {
            **context,
            "relevance_candidates": [
                {"belief_id": b["belief_id"], "padding": "x" * 2500} for b in context["beliefs"]
            ],
        }
        self.assertIn(target, bounded_context(linked)["beliefs"])

    def test_outreach_dedupes_question_identity_not_reason_and_expires(self) -> None:
        queue = Outreach(self.root)
        now = datetime(2026, 10, 20, 10, tzinfo=UTC)

        def enqueue(
            key: str,
            *,
            belief: str = "b",
            subject: str = "s",
            attribute: str = "a",
            reason: str = "same reason",
            hours: float = 0,
        ) -> str:
            queue.enqueue_intent(
                {
                    "id": key,
                    "reason": reason,
                    "evidence": ["fixture-evidence"],
                    "urgency": "normal",
                    "needs_response": True,
                    "belief_id": belief,
                    "gap_subject": subject,
                    "gap_attribute": attribute,
                },
                now=now + timedelta(hours=hours),
            )
            return queue.intent_history(key)[-1]["status"]

        self.assertEqual(enqueue("first"), "queued")
        self.assertEqual(enqueue("other-attribute", attribute="b"), "queued")
        self.assertEqual(enqueue("other-belief", belief="c"), "queued")
        self.assertEqual(enqueue("other-subject", subject="t"), "queued")
        self.assertEqual(
            enqueue("same-identity", reason="reworded reason", hours=5.99), "suppressed"
        )
        self.assertEqual(enqueue("boundary", reason="another reason", hours=6), "queued")
        # Real daemon callers do not supply identity fields: resolve scheduler
        # provenance instead of collapsing every reviewed question to empty keys.
        self.belief("career", source="inferred")
        self.belief("reading", source="inferred")
        mind = InnerLoop(self.store, lambda _: {}, catalog=[])
        for identity in ("career", "reading"):
            mind.state(
                "scheduler:" + identity,
                "Synthetic?",
                "ask_sir",
                refs=[],
                detail="same scheduler reason",
            )
        wiring = Wiring(
            prepare_root(self.root), FixtureModel(), fixture=True, clock=lambda: now.timestamp()
        )
        wiring.contact()
        for identity in ("career", "reading"):
            self.assertEqual(
                queue.intent_history("mind:scheduler:" + identity)[-1]["status"], "queued"
            )
        for index, attribute in enumerate(("role", "employer", "role")):
            request = self.store.append(
                "action_request",
                {
                    "belief_id": "career",
                    "question": {
                        "text": "Synthetic?",
                        "selected_capability": None,
                        "gap": {"subject": "fixture-person", "attribute": attribute},
                    },
                },
                source="test",
                refs=[],
            )
            mind.state(
                "direction-" + str(index),
                "Synthetic?",
                "ask_sir",
                refs=[request["id"]],
                detail="same scheduler reason",
            )
        wiring.contact()
        self.assertEqual(queue.intent_history("mind:direction-0")[-1]["status"], "queued")
        self.assertEqual(queue.intent_history("mind:direction-1")[-1]["status"], "queued")
        self.assertEqual(queue.intent_history("mind:direction-2")[-1]["status"], "suppressed")
        self.assertEqual(
            queue.intent_history("mind:direction-2")[-1]["policy"], "question_identity_dedupe"
        )

    def test_route_dedupes_paraphrases_but_keeps_different_query_directions(self) -> None:
        self.belief("career", source="inferred")
        loop = InnerLoop(self.store, lambda _: {}, catalog=[])
        for i in range(110):
            loop.route(
                {"question": {"text": f"Synthetic paraphrase {i}?"}, "refs": ["career"]}, str(i)
            )
        self.assertEqual(self.store.count("action_request"), 1)
        # Restart must preserve the identity dedupe, not only a process-local set.
        loop = InnerLoop(self.store, lambda _: {}, catalog=[])
        loop.route({"question": {"text": "A new paraphrase?"}, "refs": ["career"]}, "restart")
        self.assertEqual(self.store.count("action_request"), 1)
        for attribute in ("employer", "role"):
            for i in range(2):
                loop.route(
                    {
                        "question": {
                            "text": "Same wording?",
                            "gap": {"subject": "fixture-person", "attribute": attribute},
                        },
                        "refs": ["career"],
                    },
                    attribute + str(i),
                )
        self.assertEqual(self.store.count("action_request"), 3)
        self.belief("reading", source="inferred")
        loop.route({"question": {"text": "Same wording?"}, "refs": ["reading"]}, "other-belief")
        self.assertEqual(self.store.count("action_request"), 4)


if __name__ == "__main__":
    unittest.main()
