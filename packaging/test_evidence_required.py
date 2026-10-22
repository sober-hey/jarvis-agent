"""Missing evidence is isolated without laundering dependent thought chains."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from inner_loop import MindStore, Record
from inner_runtime import InnerLoop, validate


def thought(identity: str, kind: str, refs: list[str]) -> Record:
    return {
        "id": identity,
        "kind": kind,
        "text": "Synthetic private-free thought",
        "refs": refs,
        "question": None,
    }


def chain() -> Record:
    return {
        "thoughts": [
            thought("t1", "inference", []),
            thought("t2", "review", ["t1"]),
            thought("t3", "plan", ["t2"]),
        ],
        "resolved_questions": [],
    }


class MemoryPrefixAliasTests(unittest.TestCase):
    def test_prefixed_raw_ledger_id_is_normalized_to_supplied_id(self):
        raw = {"thoughts": [thought("t1", "inference", ["memory:abc"])], "resolved_questions": []}
        parsed = validate(json.dumps(raw), {"abc"}, [], [])
        self.assertEqual(parsed["thoughts"][0]["refs"], ["abc"])

    def test_supplied_prefixed_id_is_kept_verbatim(self):
        raw = {"thoughts": [thought("t1", "inference", ["memory:abc"])], "resolved_questions": []}
        parsed = validate(json.dumps(raw), {"memory:abc", "abc"}, [], [])
        self.assertEqual(parsed["thoughts"][0]["refs"], ["memory:abc"])

    def test_prefix_alias_never_admits_unsupplied_id(self):
        raw = {"thoughts": [thought("t1", "inference", ["memory:zzz"])], "resolved_questions": []}
        with self.assertRaises(ValueError):
            validate(json.dumps(raw), {"abc"}, [], [])


class CapabilityRefTests(unittest.TestCase):
    CATALOG = [
        {
            "id": "cap-a",
            "name": "a",
            "persona_description": "我能读一个来源",
            "implementation": "senses_read_only",
            "available": True,
        }
    ]

    def run_validate(self, refs, allowed):
        raw = {"thoughts": [thought("t1", "inference", refs)], "resolved_questions": []}
        return validate(json.dumps(raw), allowed, self.CATALOG, [])

    def test_capability_ref_is_removed_and_real_evidence_kept(self):
        parsed = self.run_validate(["cap-a", "src"], {"src"})
        self.assertEqual(parsed["thoughts"][0]["refs"], ["src"])
        self.assertEqual(parsed["ref_normalizations"][0]["reason"], "capability_id_not_evidence")
        self.assertNotIn("validation_degradations", parsed)

    def test_capability_only_thought_is_isolated_as_missing_evidence(self):
        parsed = self.run_validate(["cap-a"], set())
        self.assertEqual(parsed["thoughts"], [])
        self.assertEqual(parsed["validation_degradations"][0]["reason"], "missing_evidence")

    def test_open_question_ref_is_removed_and_real_evidence_kept(self):
        raw = {
            "thoughts": [thought("t1", "review", ["scheduler:gap:abc", "src"])],
            "resolved_questions": [],
        }
        questions = [{"question_id": "scheduler:gap:abc", "text": "synthetic"}]
        parsed = validate(json.dumps(raw), {"src"}, self.CATALOG, questions)
        self.assertEqual(parsed["thoughts"][0]["refs"], ["src"])
        self.assertEqual(parsed["ref_normalizations"][0]["reason"], "question_id_not_evidence")

    def test_unknown_non_catalog_ref_still_fails_closed(self):
        with self.assertRaises(ValueError):
            self.run_validate(["scheduler:gap:abc"], set())
        with self.assertRaises(ValueError):
            self.run_validate(["cap-b"], set())


class MissingEvidenceTests(unittest.TestCase):
    def test_missing_evidence_chain_is_dropped_with_audit_and_independent_work_survives(self):
        raw = chain()
        raw["thoughts"].append(thought("t4", "inference", ["source"]))
        try:
            parsed = validate(json.dumps(raw), {"source"}, [], [])
        except ValueError as exc:
            self.fail(f"Single unsupported thought aborted independent work: {type(exc).__name__}")
        self.assertEqual(parsed["thoughts"], [raw["thoughts"][-1]])
        audits = parsed["validation_degradations"]
        self.assertEqual([a["thought_id"] for a in audits], ["t1", "t2", "t3"])
        self.assertEqual(
            [a["reason"] for a in audits],
            ["missing_evidence", "unsupported_dependency", "unsupported_dependency"],
        )
        self.assertTrue(all(a["action"] == "dropped" for a in audits))

    def test_step_persists_chain_drop_audit_without_grounded_thoughts(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-evidence-", dir=Path.home()) as folder:
            store = MindStore(Path(folder))
            loop = InnerLoop(
                store, lambda _: {"text": json.dumps(chain())}, catalog=[], max_rounds=1
            )
            self.assertTrue(loop.step())
            self.assertEqual(store.count("thought"), 0)
            self.assertEqual(store.count("validation_error"), 0)
            audit = store.recent("validation_degraded", 1)[0]["payload"]
            self.assertEqual(audit["field"], "thoughts")
            self.assertEqual(audit["dropped_count"], 3)
            self.assertEqual(len(audit["details"]), 3)
            self.assertNotIn("text", json.dumps(audit))
            for category in ("action", "action_request", "question_state"):
                self.assertEqual(store.count(category), 0)

    def test_step_keeps_independent_grounded_thought_with_original_source_refs(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-evidence-", dir=Path.home()) as folder:
            store = MindStore(Path(folder))
            raw = chain()
            loop = InnerLoop(store, lambda _: {"text": json.dumps(raw)}, catalog=[], max_rounds=1)
            event = loop.ingest("conversation", {"text": "Synthetic source"}, source="sir")
            raw["thoughts"].append(thought("t4", "inference", [event["id"]]))
            self.assertTrue(loop.step())
            saved = store.entries("thought")
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["payload"]["kind"], "inference")
            self.assertEqual(saved[0]["refs"], [event["id"]])
            self.assertEqual(store.count("validation_error"), 0)
            self.assertEqual(
                store.recent("validation_degraded", 1)[0]["payload"]["dropped_count"], 3
            )

    def test_step_drops_only_belief_update_with_inadmissible_origin(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-evidence-", dir=Path.home()) as folder:
            store = MindStore(Path(folder))
            raw: Record = {
                "thoughts": [],
                "resolved_questions": [],
                "belief_updates": [
                    {
                        "origin_id": "not-a-pending-origin",
                        "belief_id": None,
                        "subject": "Sir",
                        "attribute": "synthetic",
                        "value": "synthetic",
                        "importance": 0.5,
                        "conflict": False,
                    }
                ],
            }
            loop = InnerLoop(store, lambda _: {"text": json.dumps(raw)}, catalog=[], max_rounds=1)
            event = loop.ingest("conversation", {"text": "Synthetic source"}, source="sir")
            raw["thoughts"].append(thought("t1", "inference", [event["id"]]))
            self.assertTrue(loop.step())
            self.assertEqual(store.count("validation_error"), 0)
            self.assertEqual(store.count("thought"), 1)
            audits = [r["payload"] for r in store.entries("validation_degraded")]
            belief = [a for a in audits if a.get("field") == "belief_updates"]
            self.assertEqual(len(belief), 1)
            self.assertEqual(belief[0]["reason"], "unknown_belief_origin")
            self.assertEqual(belief[0]["dropped_updates"], 1)
            self.assertNotIn("dropped_count", belief[0])
            self.assertFalse(any(b["belief_id"] == "not-a-pending-origin" for b in store.beliefs()))

    def test_dependency_through_idle_is_not_laundered(self):
        raw = chain()
        raw["thoughts"] = [thought("t1", "idle", []), thought("t2", "review", ["t1"])]
        parsed = validate(json.dumps(raw), set(), [], [])
        self.assertEqual(parsed["thoughts"], [raw["thoughts"][0]])
        self.assertEqual(parsed["validation_degradations"][0]["reason"], "unsupported_dependency")

    def test_mixed_dependency_is_conservatively_dropped_without_rewriting_refs(self):
        raw = chain()
        raw["thoughts"][1]["refs"].append("source")
        self.assertEqual(validate(json.dumps(raw), {"source"}, [], [])["thoughts"], [])

    def test_valid_local_chain_and_idle_are_unchanged(self):
        raw = chain()
        raw["thoughts"][0]["refs"] = ["source"]
        self.assertEqual(validate(json.dumps(raw), {"source"}, [], [])["thoughts"], raw["thoughts"])
        raw["thoughts"] = [thought("t1", "idle", [])]
        self.assertEqual(validate(json.dumps(raw), set(), [], [])["thoughts"], raw["thoughts"])

    def test_unknown_forward_duplicate_refs_and_invalid_shape_still_fail_closed(self):
        for thoughts in (
            [thought("t1", "review", ["t2"])],
            [thought("t1", "inference", ["fabricated"])],
            [thought("t1", "idle", []), thought("t1", "idle", [])],
            [thought("t1", "reflection", [])],
            [{**thought("t1", "inference", []), "question": {}}],
        ):
            with self.subTest(thoughts=thoughts), self.assertRaises(ValueError):
                validate(
                    json.dumps({"thoughts": thoughts, "resolved_questions": []}), set(), [], []
                )

    def test_question_rules_remain_strict_but_valid_dependent_question_is_dropped(self):
        q = {
            "text": "Synthetic unknown",
            "assessments": [],
            "selected_capability": None,
            "missing_information": "Synthetic missing information",
        }
        raw = chain()
        raw["thoughts"] = [raw["thoughts"][0], {**thought("t2", "question", ["t1"]), "question": q}]
        self.assertEqual(validate(json.dumps(raw), set(), [], [])["thoughts"], [])
        raw["thoughts"][1]["refs"] = []
        with self.assertRaisesRegex(ValueError, "Evidence required"):
            validate(json.dumps(raw), set(), [], [])
        raw["thoughts"][1]["refs"] = ["t1"]
        raw["thoughts"][1]["question"] = None
        with self.assertRaisesRegex(ValueError, "Question object"):
            validate(json.dumps(raw), set(), [], [])

    def test_resolution_is_deferred_when_thought_support_incomplete(self):
        raw = chain()
        raw["resolved_questions"] = ["q1"]
        parsed = validate(json.dumps(raw), set(), [], [{"question_id": "q1"}])
        self.assertEqual(parsed["resolved_questions"], [])
        self.assertEqual(parsed["validation_degradations"][-1]["action"], "deferred")

    def test_prompt_stays_answer_free_and_context_budgeted(self):
        from curiosity import INSTRUCTIONS as CURIOSITY_INSTRUCTIONS
        from inner_runtime import INSTRUCTIONS, answer_terms, bound_context
        from test_context_budget import stalled_context, byte_size
        from context_limits import INNER_CONTEXT_BYTE_LIMIT

        self.assertEqual(answer_terms(INSTRUCTIONS), [])
        self.assertIn("非 idle 念头必须列真实 refs", INSTRUCTIONS)
        context = stalled_context()
        context["instructions"] = INSTRUCTIONS + "\n" + CURIOSITY_INSTRUCTIONS
        self.assertLessEqual(byte_size(bound_context(context)) + 256, INNER_CONTEXT_BYTE_LIMIT)


class TolerantResponseJsonTests(unittest.TestCase):
    def test_repairs_fences_prose_trailing_commas_without_changing_string_text(self):
        raw = {"thoughts": [thought("t1", "idle", [])], "resolved_questions": []}
        raw["thoughts"][0]["text"] = 'Literal ,} and ,] and escaped " content'
        strict = json.dumps(raw)
        for text in (
            "```json\n" + strict + "\n```",
            "preface\n" + strict + "\nend",
            strict[:-1] + ",}",
            strict.replace('"question": null}', '"question": null,}').replace(
                '}], "resolved_questions"', '},], "resolved_questions"'
            ),
        ):
            with self.subTest(text=text):
                self.assertEqual(validate(text, set(), [], [])["thoughts"], raw["thoughts"])

    def test_realistic_raw_newline_in_string_is_accepted(self):
        raw = {"thoughts": [thought("t1", "idle", [])], "resolved_questions": []}
        raw["thoughts"][0]["text"] = "synthetic\nthought"
        parsed = validate(json.dumps(raw).replace("\\n", "\n"), set(), [], [])
        self.assertEqual(parsed["thoughts"], raw["thoughts"])

    def test_duplicate_keys_nonfinite_and_float_overflow_still_rejected(self):
        from inner_runtime import parse_response_json

        for value in ('{"a":1,"a":2}', 'preface {"a":1,"a":2,}', '{"a":NaN}', '{"a":1e400}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_response_json(value)

    def test_truncated_outer_object_is_never_salvaged_as_nested_document(self):
        from inner_runtime import parse_response_json

        for value in (
            '{"thoughts":[{"id":"t1"}',
            '```json\n{"thoughts":[]',
            '{"outer":"```json\n{}\n```',
            '{} {"truncated":',
            '{}\n```json\n{"truncated":',
            '{"open": "unclosed',
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_response_json(value)

    def test_malformed_json_is_not_guessed_or_retried(self):
        from inner_runtime import parse_response_json

        for value in ('{"a": unquoted}', '{"a": 1 "b": 2}', '{"a":]}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_response_json(value)


if __name__ == "__main__":
    unittest.main()
