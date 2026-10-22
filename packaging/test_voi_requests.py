"""Generic question-to-gap scheduling tests; no real models or private data."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import capabilities
from inner_loop import MindStore, Record
from inner_runtime import InnerLoop
from test_inner_loop import answer


class QuestionVOITests(unittest.TestCase):
    def setUp(self) -> None:
        # Nest the store under an owned private parent, not shared /private/tmp.
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis-fix-tests-voi-", dir="/private/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "data"
        self.root.mkdir(mode=0o700)
        self.store = MindStore(self.root)
        self.catalog = capabilities.catalog(adapters=[])
        for capability in self.catalog:
            capability["available"] = True
        self.executed: list[str] = []
        self.contexts: list[Record] = []

    def model(self, prompt: str) -> Record:
        context = json.loads(prompt)
        output = json.loads(answer(prompt)["text"])
        if "events" not in context:
            return {"text": json.dumps(output)}
        self.contexts.append(context)
        # The byte budget can advertise only part of the catalog, and the model
        # can only speak about the capabilities it was shown: an assessment or a
        # score for a hidden id is rejected as unknown. Pick the source the
        # fixture wants when it is visible, otherwise the first visible one.
        visible = context["capabilities"]
        selected = next(
            (c["id"] for c in visible if c["id"] == self.catalog[0]["id"]), visible[0]["id"]
        )
        for row in output["relevance"]:
            row["score"] = 0.9 if row["capability_id"] == selected else 0.0
        thought = output["thoughts"][0]
        thought["kind"] = "question"
        thought["question"] = {
            "text": "Which state does the latest record support?",
            "assessments": [
                {
                    "capability_id": capability["id"],
                    "can_resolve": capability["id"] == selected,
                    "reason": "the source can answer the unknown",
                }
                for capability in visible
            ],
            "selected_capability": selected,
            "missing_information": "",
            "gap": {
                "subject": "record",
                "attribute": "latest state",
                "importance": 0.8,
                "reason": "the state is not established",
            },
        }
        return {"text": json.dumps(output)}

    def execute(self, capability: Record) -> Record:
        self.executed.append(capability["id"])
        return {"synthetic": True, "status": "available"}

    def loop(self, model: object = None) -> InnerLoop:
        return InnerLoop(
            self.store,
            self.model if model is None else model,  # type: ignore[arg-type]
            catalog=self.catalog,
            executor=self.execute,
            max_rounds=4,
            idle_seconds=0,
            active_seconds=0,
        )

    def test_self_generated_question_enters_scored_voi_then_executes_once(self) -> None:
        loop = self.loop()
        self.assertTrue(loop.step())
        self.assertEqual(self.executed, [])
        self.assertEqual(self.store.count("validation_error"), 0)
        gaps = self.store.beliefs()
        self.assertEqual(len(gaps), 1)
        self.assertIsNone(gaps[0]["value"])
        self.assertEqual(gaps[0]["source"], "gap")
        self.assertEqual(
            self.store.recent("action_request", 1)[0]["payload"]["belief_id"], gaps[0]["belief_id"]
        )
        self.assertTrue(loop.step())
        self.assertEqual(self.executed, [self.catalog[0]["id"]])
        score = self.store.recent("belief_score", 1)[0]["payload"]["scores"][0]
        self.assertAlmostEqual(score["u"], 1.0)
        self.assertAlmostEqual(score["capabilities"][0]["voi"], 0.72)
        self.assertEqual(self.store.recent("action", 1)[0]["source"], "curiosity-scheduler")
        # Restart, same question and capability: no second execution.
        self.assertTrue(self.loop().step())
        self.assertEqual(self.executed, [self.catalog[0]["id"]])
        self.assertEqual(self.store.count("knowledge_gap"), 1)

    def test_low_voi_does_not_execute_or_fall_back_to_legacy(self) -> None:
        def low_model(prompt: str) -> Record:
            output = json.loads(self.model(prompt)["text"])
            for row in output.get("relevance", []):
                row["score"] = 0.4
            for thought in output.get("thoughts", []):
                if thought["question"]:
                    thought["question"]["gap"]["importance"] = 0.3
            return {"text": json.dumps(output)}

        loop = self.loop(low_model)
        loop.run()
        self.assertEqual(self.executed, [])
        scores = self.store.entries("belief_score")[1:]
        self.assertEqual(len(scores), 3)
        self.assertTrue(all(s["payload"]["scores"] for s in scores))
        self.assertTrue(
            all(
                c["voi"] <= 0.15
                for s in scores
                for b in s["payload"]["scores"]
                for c in b["capabilities"]
            )
        )
        self.assertEqual(loop.questions(), [])

    def test_question_metadata_reuses_existing_semantic_belief_identity(self) -> None:
        from datetime import UTC, datetime

        self.store.append_belief(
            belief_id="existing-record",
            subject="record",
            attribute="latest state",
            value="prior state",
            source="inferred",
            confidence=0.1,
            last_verified=datetime.now(UTC).isoformat(),
            importance=0.8,
        )
        loop = self.loop()
        self.assertTrue(loop.step())
        request = self.store.recent("action_request", 1)[0]["payload"]
        self.assertEqual(request["belief_id"], "existing-record")
        self.assertEqual(self.store.count("knowledge_gap"), 0)

    def test_multiple_question_gaps_are_admitted_without_orphaning_later_ones(self) -> None:
        def model(prompt: str) -> Record:
            output = json.loads(self.model(prompt)["text"])
            context = json.loads(prompt)
            if "events" in context:
                original = output["thoughts"][0]
                output["thoughts"] = []
                for number in range(4):
                    thought = json.loads(json.dumps(original))
                    thought["id"] = f"t{number + 1}"
                    thought["question"]["text"] += str(number)
                    thought["question"]["gap"]["attribute"] += str(number)
                    output["thoughts"].append(thought)
            return {"text": json.dumps(output)}

        loop = self.loop(model)
        self.assertTrue(loop.step())
        self.assertEqual(self.store.count("knowledge_gap"), 2)
        self.assertTrue(loop.step())
        self.assertEqual(self.store.count("knowledge_gap"), 4)

    def test_resolvable_question_is_not_delegated_to_owner_when_voi_is_low(self) -> None:
        def low_model(prompt: str) -> Record:
            output = json.loads(self.model(prompt)["text"])
            for row in output.get("relevance", []):
                row["score"] = 0.1
            return {"text": json.dumps(output)}

        self.loop(low_model).run()
        self.assertEqual(self.executed, [])
        self.assertEqual(self.store.count("question_generation_intent"), 0)
        self.assertEqual(self.loop().questions(), [])

    def test_fixture_catalog_cannot_bypass_low_voi(self) -> None:
        self.catalog = capabilities.fixture_catalog()
        self.test_low_voi_does_not_execute_or_fall_back_to_legacy()

    def test_waiting_thought_cannot_spawn_renamed_gap(self) -> None:
        # Establish an owner-only unknown and waiting state, then try a synonym.
        from curiosity import Curiosity

        curiosity = Curiosity(self.store)
        curiosity.discover(
            [{"subject": "p", "attribute": "preference", "importance": 0.8, "reason": "missing"}], 1
        )
        identity = self.store.beliefs()[0]["belief_id"]
        loop = self.loop()
        loop.state("scheduler:" + identity, "generated by model", "ask_sir", refs=[])

        def model(prompt: str) -> Record:
            output = json.loads(self.model(prompt)["text"])
            output["thoughts"][0]["refs"] = [identity]
            return {"text": json.dumps(output)}

        self.assertTrue(self.loop(model).step())
        self.assertEqual(self.store.count("thought_suppressed"), 1)
        self.assertEqual(self.store.count("action_request"), 0)
        self.assertEqual(self.store.count("knowledge_gap"), 1)

    def test_oversized_orphan_metadata_is_budgeted(self) -> None:
        loop = self.loop()
        for number in range(4):
            self.store.append(
                "action_request",
                {
                    "question_id": str(number),
                    "belief_id": None,
                    "question": {
                        "text": "unknown " + str(number),
                        "assessments": [{"reason": "x" * 10000}],
                    },
                },
                source="model",
            )
        context = loop.context()
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False).encode()), 10000)
        self.assertTrue(context["unbound_questions"])

    def test_waiting_attention_ignores_non_evidence_and_consumes_new_evidence(self) -> None:
        from curiosity import Curiosity

        curiosity = Curiosity(self.store)
        curiosity.discover(
            [{"subject": "p", "attribute": "unknown", "importance": 0.8, "reason": "missing"}], 1
        )
        belief = self.store.beliefs()[0]
        identity = belief["belief_id"]
        self.loop().state("scheduler:" + identity, "model output", "ask_sir", refs=[])
        for data in ({"role": "assistant"}, {"synthetic": True}, {"backfill": True}):
            self.store.ingest("conversation", data, source="chat")
            self.assertFalse(curiosity.attention(belief)["new_evidence"])
        self.store.ingest("conversation", {"role": "user"}, source="chat")
        self.assertTrue(curiosity.attention(belief)["new_evidence"])
        self.store.append("thought", {"text": "considered"}, source="model", refs=[identity])
        self.assertFalse(curiosity.attention(belief)["new_evidence"])

    def test_unbound_legacy_request_remains_visible_for_model_metadata(self) -> None:
        loop = self.loop()
        loop.ingest("time", {}, source="clock")
        legacy = json.loads(self.model(json.dumps(loop.context()))["text"])["thoughts"][0]
        del legacy["question"]["gap"]
        loop.route(legacy, "legacy-orphan")
        context = loop.context()
        self.assertIn("unbound_questions", context)
        pending = context["unbound_questions"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["question_id"], "legacy-orphan")
        self.assertTrue(loop.step())
        self.assertTrue(loop.step())
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(loop.context()["unbound_questions"], [])

    def test_waiting_gap_is_visible_but_not_repeated_focus_without_new_evidence(self) -> None:
        from curiosity import gap_identity

        gap: Record = {
            "subject": "person",
            "attribute": "unprovided preference",
            "importance": 0.8,
            "reason": "only the person can establish it",
        }
        identity = gap_identity(gap["subject"], gap["attribute"])

        def model(prompt: str) -> Record:
            context = json.loads(prompt)
            if "events" not in context:
                return answer(prompt)
            self.contexts.append(context)
            output = json.loads(answer(prompt)["text"])
            output["knowledge_gaps"] = [gap]
            if any(b["belief_id"] == identity for b in context["beliefs"]):
                thought = output["thoughts"][0]
                thought["kind"] = "inference"
                thought["refs"] = [identity]
            return {"text": json.dumps(output)}

        loop = self.loop(model)
        self.assertTrue(loop.step())
        self.assertTrue(loop.step())
        self.assertEqual(loop.questions()[0]["state"], "ask_sir")
        self.assertTrue(loop.step())
        waiting = next(b for b in self.contexts[-1]["beliefs"] if b["belief_id"] == identity)
        self.assertIn("attention", waiting)
        self.assertEqual(waiting["attention"]["status"], "awaiting_sir")
        self.assertFalse(waiting["attention"]["new_evidence"])
        self.assertIsNotNone(waiting["attention"]["last_thought_at"])
        self.assertNotIn(
            identity, [b["belief_id"] for b in self.contexts[-1]["relevance_candidates"]]
        )
        focused = [t for t in self.store.entries("thought") if identity in t["refs"]]
        self.assertEqual(len(focused), 1)
        self.assertEqual(self.store.count("thought_suppressed"), 1)
        # New owner evidence reopens attention, without a time-only retry.
        loop.ingest("conversation", {"text": "new evidence from the owner"}, source="chat")
        self.assertTrue(loop.step())
        refreshed = next(b for b in self.contexts[-1]["beliefs"] if b["belief_id"] == identity)
        self.assertTrue(refreshed["attention"]["new_evidence"])
        self.assertIn(identity, [b["belief_id"] for b in self.contexts[-1]["relevance_candidates"]])


if __name__ == "__main__":
    unittest.main()
