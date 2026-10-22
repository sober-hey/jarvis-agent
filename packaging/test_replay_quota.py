"""Synthetic, side-effect-free quota allocator tests."""

import importlib.util
import json
import unittest
from copy import deepcopy
from typing import Any


class QuotaReplayTests(unittest.TestCase):
    def test_module_exists_for_offline_candidate(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("replay_quota"))

    def test_bound_context_uses_eligible_quota_without_losing_memory(self) -> None:
        from inner_runtime import bound_context
        from replay_quota import size

        raw: dict[str, Any] = {
            "instructions": "s" * 4700,
            "capabilities": [{"id": "c"}],
            "recalled": [{"id": str(i), "payload": {"text": "x" * 400}} for i in range(8)],
            "events": [],
            "recent_thoughts": [],
            "beliefs": [],
            "relevance_candidates": [],
            "pending_observations": [],
            "pending_memories": [],
        }
        out = bound_context(raw)
        self.assertGreaterEqual(len(out["recalled"]), 2)
        self.assertLessEqual(size(out) + 256, 10600)
        self.assertEqual(out["capabilities"], raw["capabilities"])
        self.assertEqual(out, __import__("replay_quota").quota_context(raw, reserved_bytes=256))

    def test_baseline_allocator_still_zeroes_memory_without_the_floor(self) -> None:
        from inner_runtime import bound_context, bounded_context_baseline

        raw: dict[str, Any] = {
            "instructions": "s" * 3000,
            "capabilities": [{"id": "c", "name": "c", "call": {}, "available": True}],
            "recalled": [{"id": "only-one", "payload": {"text": "x" * 400}}],
            "events": [],
            "recent_thoughts": [],
            "beliefs": [],
            "relevance_candidates": [],
            "pending_observations": [],
            "pending_memories": [],
        }
        self.assertEqual(bound_context(raw), bounded_context_baseline(raw))

    def test_indivisible_floor_falls_back_to_baseline(self) -> None:
        from inner_runtime import bound_context, bounded_context_baseline

        raw: dict[str, Any] = {
            "instructions": "s" * 4700,
            "capabilities": [{"id": "c", "name": "c", "call": {}, "available": True}],
            "recalled": [{"id": "a", "text": "x" * 4000}, {"id": "b", "text": "x" * 4000}],
            "events": [],
            "recent_thoughts": [],
            "beliefs": [],
            "relevance_candidates": [],
            "pending_observations": [],
            "pending_memories": [],
        }
        self.assertEqual(bound_context(raw), bounded_context_baseline(raw))

    def test_memory_floor_full_rows_and_input_immutable(self) -> None:
        from replay_quota import quota_context, size

        raw: dict[str, Any] = {
            "instructions": "s" * 4700,
            "capabilities": [{"id": "c", "name": "c", "call": {}, "available": True}],
            "beliefs": [{"belief_id": "b", "value": "v"}],
            "relevance_candidates": [{"belief_id": "b", "value": "v"}],
            "recalled": [{"id": str(i), "payload": {"text": "x" * 400}} for i in range(8)],
            "events": [{"id": "e", "payload": {"text": "x" * 2000}}],
            "recent_thoughts": [{"id": "t", "payload": {"text": "x" * 2000}}],
            "pending_observations": [],
            "daemon_pending_senses": [],
        }
        before = deepcopy(raw)
        out = quota_context(raw)
        self.assertEqual(raw, before)
        self.assertEqual(out["recalled"][:2], raw["recalled"][:2])
        self.assertLessEqual(size(out), 10088)
        self.assertTrue(all(out[k] for k in ("capabilities", "beliefs", "relevance_candidates")))

    def test_oversized_recall_floor_rejected(self) -> None:
        from replay_quota import quota_context

        with self.assertRaises(ValueError):
            quota_context(
                {"recalled": [{"id": "a", "text": "x" * 4000}, {"id": "b", "text": "x" * 4000}]}
            )

    def test_source_capability_and_observation_remain_an_evidence_unit(self) -> None:
        from replay_quota import quota_context

        caps = [
            {
                "id": "cap-" + str(i),
                "name": "cap",
                "available": True,
                "call": {"name": "senses_poll", "arguments": {"source": str(i)}},
                "implementation": "senses_read_only",
                "persona_description": "x" * 600,
            }
            for i in range(6)
        ]
        event = {
            "id": "event",
            "source": "senses:5",
            "created_at": "2026-10-01T00:00:00+00:00",
            "payload": {
                "kind": "tool_result",
                "data": {
                    "belief_id": "b",
                    "evidence": {"capability_id": "cap-5"},
                    "sense_event": {
                        "source": "5",
                        "kind": "sample",
                        "observed_at": "2026-10-01T00:00:00+00:00",
                        "payload": {
                            "status": "available",
                            "sampled_at": "2026-10-01T00:00:00+00:00",
                        },
                    },
                },
            },
        }
        with self.assertRaisesRegex(ValueError, "Quota source capability omitted"):
            quota_context(
                {
                    "capabilities": caps,
                    "pending_observations": [event],
                    "relevance_candidates": [{"belief_id": "b"}],
                }
            )

    def test_candidate_and_pending_observation_are_indivisible(self) -> None:
        from replay_quota import quota_context

        pending: list[dict[str, Any]] = [
            {
                "id": "e" + str(i),
                "payload": {"data": {"belief_id": "b" + str(i)}, "text": "x" * 250},
            }
            for i in range(3)
        ]
        raw: dict[str, Any] = {
            "relevance_candidates": [
                {"belief_id": "b" + str(i), "text": "x" * 400} for i in range(3)
            ],
            "pending_observations": pending,
            "recalled": [],
            "events": [],
        }
        out = quota_context(raw)
        kept = {r["belief_id"] for r in out["relevance_candidates"]}
        self.assertEqual(
            out["pending_observations"],
            [r for r in pending if r["payload"]["data"]["belief_id"] in kept],
        )

    def test_memory_ref_metric_uses_only_actual_thought_refs(self) -> None:
        from replay_quota import evaluate

        context: dict[str, Any] = {
            k: []
            for k in (
                "events",
                "thoughts",
                "memories",
                "beliefs",
                "recent_thoughts",
                "recalled",
                "pending_observations",
                "relevance_candidates",
                "pending_memories",
            )
        }
        context["recalled"] = [{"id": "m"}]
        body: dict[str, Any] = {
            "thoughts": [
                {
                    "id": "t1",
                    "kind": "review",
                    "text": "memory text without a ref",
                    "refs": [],
                    "question": None,
                }
            ],
            "resolved_questions": [],
            "relevance": [],
            "scheduler_question": None,
            "belief_updates": [],
            "knowledge_gaps": [],
            "outreach_intents": [],
        }
        self.assertFalse(evaluate(json.dumps(body), context)["memory_used"])
        body["thoughts"][0]["refs"] = ["m"]
        self.assertTrue(evaluate(json.dumps(body), context)["memory_used"])

    def test_evaluate_reports_only_alias_repairs(self) -> None:
        from replay_quota import evaluate

        context: dict[str, Any] = {
            k: []
            for k in (
                "events",
                "thoughts",
                "memories",
                "beliefs",
                "recent_thoughts",
                "recalled",
                "pending_observations",
                "relevance_candidates",
                "pending_memories",
            )
        }
        context["recalled"] = [{"id": "m"}]
        body: dict[str, Any] = {
            "thoughts": [
                {"id": "t1", "kind": "review", "text": "x", "refs": ["m"], "question": None}
            ],
            "resolved_questions": [],
            "relevance": [],
            "scheduler_question": None,
            "belief_updates": [],
            "knowledge_gaps": [],
            "outreach_intents": [],
        }
        self.assertEqual(evaluate(json.dumps(body), context)["ref_normalizations"], 0)
        body["thoughts"][0]["refs"] = ["mind:entries:m"]
        self.assertEqual(evaluate(json.dumps(body), context)["ref_normalizations"], 1)

    def test_recall_view_never_advertises_the_namespaced_second_id(self) -> None:
        from inner_runtime import bound_context, bounded_context_baseline, recall_view

        rows = [
            {
                "id": "entry-1",
                "evidence_id": "mind:entries:entry-1",
                "recall_role": "evidence",
                "payload": {"text": "x"},
            },
            {"id": "memory:abc", "evidence_id": "memory:abc", "recall_role": "evidence"},
        ]
        raw: dict[str, Any] = {
            "instructions": "s" * 4700,
            "capabilities": [{"id": "c", "name": "c", "call": {}, "available": True}],
            "recalled": deepcopy(rows),
            "events": [],
            "recent_thoughts": [],
            "beliefs": [],
            "relevance_candidates": [],
            "pending_observations": [],
            "pending_memories": [],
        }
        for allocator in (bound_context, bounded_context_baseline):
            with self.subTest(allocator=allocator.__name__):
                out = allocator(raw)
                for row in out["recalled"]:
                    self.assertNotIn("evidence_id", row)
                    self.assertIn("id", row)
                # The caller's copy keeps the ledger provenance untouched.
                self.assertIn("evidence_id", raw["recalled"][0])
        self.assertEqual(recall_view(raw)["recalled"][0]["id"], "entry-1")

    def test_instructions_schema_example_has_no_citable_id(self) -> None:
        from inner_runtime import INSTRUCTIONS

        self.assertNotIn('"id":"t1"', INSTRUCTIONS)
        self.assertNotIn("本轮较早的t1", INSTRUCTIONS)
        self.assertIn("念头编号由你在本次输出内按顺序自定", INSTRUCTIONS)
        self.assertIn("不得写自身编号", INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
