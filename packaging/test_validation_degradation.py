"""Strict evidence with local scheduling degradation for incomplete matrices."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from curiosity import validate_matrix
from inner_loop import MindStore, Record
from inner_runtime import InnerLoop, validate

FIXTURE = json.loads(Path(__file__).with_name("test_validation_response_fixture.json").read_text())


class MatrixDegradationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jarvis-matrix-validation-", dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.store = MindStore(Path(self.temp.name))
        self.catalog: list[Record] = [
            {
                "id": f"capability-{n}",
                "name": f"Synthetic source {n}",
                "persona_description": "Synthetic read-only source",
                "call": {},
                "implementation": "senses_read_only",
                "available": True,
                "costs_money": False,
                "security_sensitive": False,
            }
            for n in range(1, 12)
        ]
        self.store.append_belief(
            belief_id="belief-1",
            subject="Synthetic subject",
            attribute="record",
            value="unknown",
            source="inferred",
            confidence=0.1,
            last_verified="2000-01-01T00:00:00+00:00",
            importance=1.0,
        )
        self.raw = deepcopy(FIXTURE["response"])
        self.raw["relevance"] = self.raw["relevance"][:1]
        self.loop = InnerLoop(
            self.store,
            lambda _: {"text": json.dumps(self.raw)},
            catalog=self.catalog,
            max_rounds=1,
            executor=lambda _: self.fail("Incomplete matrix must not execute"),
        )
        self.context = self.loop.context(bounded=False)
        self.context["events"] = [
            {"id": ref, "payload": {"kind": "conversation", "data": {}}, "source": "sir"}
            for ref in FIXTURE["allowed_refs"]
        ]
        self.context["pending_observations"] = []
        self.context["pending_memories"] = []
        self.context["relevance_candidates"] = self.store.beliefs()
        self.context["cached_relevance"] = []
        self.context["capabilities"] = self.catalog

    def step(self):
        with patch.object(self.loop, "context", return_value=deepcopy(self.context)):
            self.assertTrue(self.loop.step())

    def test_one_of_eleven_scores_preserves_grounded_thoughts_without_fabricating_scores(self):
        validate(json.dumps(self.raw), self.loop.outreach_refs(self.context), self.catalog, [])
        with self.assertRaisesRegex(ValueError, "Incomplete relevance matrix"):
            validate_matrix(self.raw["relevance"], self.context, self.catalog)
        self.step()
        self.assertEqual(self.store.count("thought"), len(self.raw["thoughts"]))
        self.assertEqual(self.store.count("validation_error"), 0)
        for category in (
            "belief_score",
            "relevance_cache",
            "action",
            "action_request",
            "belief_origin",
            "question_generation_intent",
        ):
            self.assertEqual(self.store.count(category), 0, category)
        warning = self.store.recent("validation_degraded", 1)[0]["payload"]
        self.assertEqual(warning["field"], "relevance")
        self.assertEqual(warning["missing_count"], 10)
        self.assertEqual(warning["reason"], "incomplete_matrix")
        self.assertEqual(
            [r["payload"]["text"] for r in self.store.entries("thought")],
            [r["text"] for r in self.raw["thoughts"]],
        )

    def test_nonquestion_null_omission_from_actual_rejected_response_retains_thoughts(self):
        fixture = json.loads(
            Path(__file__).with_name("test_validation_optional_question_fixture.json").read_text()
        )
        self.raw = deepcopy(fixture["response"])
        self.catalog = self.catalog[:6]
        self.loop.catalog = self.catalog
        self.context["capabilities"] = self.catalog
        self.context["events"] = [
            {"id": ref, "payload": {"kind": "conversation", "data": {}}, "source": "sir"}
            for ref in fixture["allowed_refs"]
        ]
        self.step()
        self.assertEqual(self.store.count("thought"), 4)
        self.assertEqual(self.store.count("validation_error"), 0)
        for entry in self.store.entries("thought"):
            if entry["payload"]["kind"] != "question":
                self.assertIn("question", entry["payload"])
                self.assertIsNone(entry["payload"]["question"])

    def test_partial_mode_rejects_malformed_or_fabricated_rows(self):
        for mutation in (
            {"score": True},
            {"score": 1.1},
            {"score": float("nan")},
            {"capability_id": "not-advertised"},
            {"belief_id": "not-a-candidate"},
            {"reason": ""},
        ):
            with self.subTest(mutation=mutation):
                row = {**self.raw["relevance"][0], **mutation}
                with self.assertRaises(ValueError):
                    validate_matrix([row], self.context, self.catalog, require_complete=False)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate_matrix(
                self.raw["relevance"] * 2, self.context, self.catalog, require_complete=False
            )

    def test_partial_matrix_cannot_route_even_a_fully_assessed_question(self):
        fixture = json.loads(
            Path(__file__).with_name("test_validation_optional_question_fixture.json").read_text()
        )
        self.raw = deepcopy(fixture["response"])
        self.catalog = self.catalog[:6]
        self.loop.catalog = self.catalog
        self.context["capabilities"] = self.catalog
        self.context["events"] = [
            {"id": ref, "payload": {"kind": "conversation", "data": {}}, "source": "sir"}
            for ref in fixture["allowed_refs"]
        ]
        self.raw["relevance"] = self.raw["relevance"][:1]
        self.step()
        self.assertEqual(self.store.count("thought"), 4)
        self.assertEqual(self.store.count("validation_error"), 0)
        self.assertEqual(self.store.count("action_request"), 0)
        self.assertEqual(self.store.count("question_generation_intent"), 0)
        self.assertEqual(self.store.count("relevance_cache"), 0)

    def test_complete_cached_matrix_does_not_degrade_or_replace_missing_with_zero(self):
        self.raw["relevance"][0]["score"] = 0.1
        self.context["cached_relevance"] = [
            {**self.raw["relevance"][0], "capability_id": c["id"], "score": 0.1}
            for c in self.catalog
            if c["id"] != self.raw["relevance"][0]["capability_id"]
        ]
        self.raw["belief_updates"] = []
        self.raw["knowledge_gaps"] = []
        self.step()
        self.assertEqual(self.store.count("validation_degraded"), 0)
        self.assertEqual(self.store.count("thought"), 4)
        scores = self.store.recent("belief_score", 1)[0]["payload"]["scores"][0]["capabilities"]
        self.assertEqual(len(scores), 11)
        self.assertTrue(all(r["r"] > 0 for r in scores))

    def test_question_kind_missing_object_still_rejected(self):
        self.raw["thoughts"][0]["kind"] = "question"
        del self.raw["thoughts"][0]["question"]
        self.step()
        self.assertEqual(self.store.count("thought"), 0)
        self.assertEqual(self.store.count("validation_error"), 1)

    def test_unknown_ref_mixed_with_valid_evidence_still_rejected(self):
        self.raw["thoughts"][0]["refs"].append("fabricated-evidence")
        self.step()
        self.assertEqual(self.store.count("thought"), 0)
        self.assertEqual(self.store.count("validation_error"), 1)

    def test_rejection_records_safe_stage_and_reason_without_raw_response(self):
        # Evidence isolation (2026-10-19): an ungrounded thought is dropped with an
        # audit record instead of discarding the whole round; no raw text is stored.
        self.raw["thoughts"][0]["refs"] = []
        self.step()
        # The incomplete relevance matrix writes its own degradation record too;
        # select the thought-isolation record explicitly.
        records = [e["payload"] for e in self.store.recent("validation_degraded", 10)]
        payload = next(p for p in records if p["field"] == "thoughts")
        self.assertEqual(payload["reason"], "evidence_isolation")
        self.assertEqual(payload["dropped_count"], 1)
        self.assertNotIn("text", payload)
        self.assertNotIn(
            "text", json.dumps(payload["details"], ensure_ascii=False).replace('"text_', "")
        )

    def test_no_refs_cannot_be_salvaged_as_grounded_text(self):
        # The ungrounded thought must not be stored; independent grounded ones stay.
        self.raw["thoughts"][0]["refs"] = []
        dropped_text = self.raw["thoughts"][0]["text"]
        self.step()
        self.assertEqual(self.store.count("thought"), len(self.raw["thoughts"]) - 1)
        kept = [e["payload"]["text"] for e in self.store.recent("thought", 10)]
        self.assertNotIn(dropped_text, kept)
        self.assertTrue(all(e["payload"]["refs"] for e in self.store.recent("thought", 10)))
        self.assertEqual(self.store.count("validation_error"), 0)


if __name__ == "__main__":
    unittest.main()
