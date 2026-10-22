"""Synthetic-only context observation tests; no stores, routes or live data roots."""

from __future__ import annotations

import importlib.util
import json
import unittest
from copy import deepcopy
from typing import Any
from unittest.mock import patch


class ContextMetricsTests(unittest.TestCase):
    def test_utf8_sections_account_for_the_actual_runtime_render(self):
        spec = importlib.util.find_spec("context_metrics")
        self.assertIsNotNone(spec, "context observation module must exist")
        from context_metrics import measure_context

        context = {
            "instructions": "synthetic 中文",
            "capabilities": [],
            "events": [{"id": "fake-event", "text": "合成"}],
            "context_omitted": {"events": 3, "memories": 2},
        }
        report = measure_context(context, system_prompt="synthetic system")
        render = json.dumps(context, ensure_ascii=False).encode("utf-8")
        self.assertEqual(report["context_utf8_bytes"], len(render))
        self.assertEqual(report["sections"]["events"]["item_count"], 1)
        self.assertEqual(report["sections"]["events"]["omitted_count"], 3)
        self.assertEqual(report["sections"]["memories"]["omitted_count"], 2)
        self.assertEqual(report["sections"]["memories"]["item_count"], 0)
        self.assertEqual(
            report["sections"]["instructions"]["utf8_bytes"],
            len(json.dumps(context["instructions"], ensure_ascii=False).encode()),
        )
        self.assertEqual(
            report["context_utf8_bytes"],
            report["json_framing_utf8_bytes"]
            + sum(s["member_utf8_bytes"] for s in report["sections"].values()),
        )

    def test_nine_categories_and_strict_repaired_separation(self):
        from context_metrics import classify_outcome

        valid = fake_response()
        missing_evidence = fake_response()
        missing_evidence["thoughts"][0]["refs"] = []
        partial = fake_response()
        partial["thoughts"].append(
            {"id": "t2", "kind": "inference", "text": "synthetic", "refs": [], "question": None}
        )
        cross = fake_response()
        cross["thoughts"][0]["refs"] = ["invented-fake-ref"]
        schema = fake_response()
        schema["thoughts"][0]["text"] = 12
        cases = [
            (
                "transport_error",
                None,
                {"transport_error": RuntimeError("fake sensitive diagnostic")},
            ),
            ("empty", " \n\t", {}),
            ("malformed_json", "{bad}", {}),
            ("truncated_json", '{"thoughts":[', {}),
            ("schema_violation", json.dumps(schema), {}),
            ("cross_reference_violation", json.dumps(cross), {}),
            ("unsupported_evidence", json.dumps(missing_evidence), {}),
            ("single_item_dropped", json.dumps(partial), {}),
            ("full_round_accepted", json.dumps(valid), {}),
        ]
        for expected, text, kwargs in cases:
            with self.subTest(category=expected):
                report = classify_outcome(text, context=fake_context(), **kwargs)
                self.assertEqual(report["category"], expected)
                self.assertNotIn("synthetic grounded body", json.dumps(report))
                self.assertNotIn("fake sensitive diagnostic", json.dumps(report))
        repaired = classify_outcome(
            "```json\n" + json.dumps(valid) + "\n```", context=fake_context()
        )
        self.assertFalse(repaired["original_json_compliant"])
        self.assertFalse(repaired["original_schema_compliant"])
        self.assertTrue(repaired["tolerant_usable"])
        self.assertTrue(repaired["full_round_accepted"])

    def test_hash_uses_actual_system_instructions_and_advertised_catalog(self):
        import hashlib

        from context_metrics import fixed_prefix_hash, render_fixed_prefix
        from curiosity import INSTRUCTIONS as CURIOSITY_INSTRUCTIONS
        from inner_runtime import INSTRUCTIONS
        from inner_voice import SYSTEM_PROMPT

        context = fake_context()
        context["instructions"] = INSTRUCTIONS + "\n" + CURIOSITY_INSTRUCTIONS
        context["capabilities"] = [{"id": "fake-cap", "available": True}]
        rendered = json.dumps(
            {
                "system": SYSTEM_PROMPT,
                "context": {
                    "instructions": context["instructions"],
                    "capabilities": context["capabilities"],
                },
            },
            ensure_ascii=False,
        )
        self.assertEqual(render_fixed_prefix(context), rendered)
        self.assertEqual(fixed_prefix_hash(context), hashlib.sha256(rendered.encode()).hexdigest())
        dynamic = deepcopy(context)
        dynamic["events"].append({"id": "fake-second", "text": "synthetic variation"})
        dynamic["context_round"] = 100
        self.assertEqual(fixed_prefix_hash(context), fixed_prefix_hash(dynamic))
        for field in ("instructions", "capabilities"):
            changed = deepcopy(context)
            changed[field] = "synthetic changed" if field == "instructions" else []
            self.assertNotEqual(fixed_prefix_hash(context), fixed_prefix_hash(changed))
        self.assertNotEqual(
            fixed_prefix_hash(context), fixed_prefix_hash(context, system_prompt="different")
        )

    def test_hash_preserves_capability_render_order_and_field_presence(self):
        from context_metrics import fixed_prefix_hash

        first = {"instructions": "fake", "capabilities": [{"id": "fake", "available": True}]}
        second = {"capabilities": [{"available": True, "id": "fake"}], "instructions": "fake"}
        self.assertNotEqual(fixed_prefix_hash(first), fixed_prefix_hash(second))
        self.assertEqual(
            fixed_prefix_hash(first), fixed_prefix_hash(dict(reversed(list(first.items()))))
        )
        self.assertNotEqual(fixed_prefix_hash({}), fixed_prefix_hash({"instructions": None}))

    def test_counts_empty_null_scalars_and_omitted_only_sections(self):
        from context_metrics import measure_context

        context = {"a": [], "b": {}, "c": None, "d": "", "e": 0, "context_omitted": {"absent": 2}}
        report = measure_context(context)
        self.assertEqual([report["sections"][k]["item_count"] for k in "abcde"], [0, 0, 0, 1, 1])
        self.assertEqual(report["context_omitted_count"], 2)
        self.assertFalse(report["sections"]["absent"]["present"])
        self.assertEqual(report["sections"]["absent"]["utf8_bytes"], 0)
        empty = measure_context({})
        self.assertEqual(empty["context_utf8_bytes"], 2)
        self.assertEqual(empty["json_framing_utf8_bytes"], 2)

    def test_invalid_omission_counters_are_not_silently_coerced(self):
        from context_metrics import measure_context

        for omitted in ([], {"events": True}, {"events": -1}, {"events": "2"}, {1: 2}):
            with self.subTest(omitted=omitted), self.assertRaises(ValueError):
                measure_context({"context_omitted": omitted})

    def test_context_measurement_is_content_free_and_nonmutating(self):
        from context_metrics import measure_context

        context = fake_context()
        context["events"][0]["content"] = "synthetic secret marker never emitted"
        snapshot = deepcopy(context)
        report = measure_context(context)
        self.assertEqual(context, snapshot)
        self.assertNotIn("synthetic secret", json.dumps(report))
        self.assertNotIn("fake-event", json.dumps(report))

    def test_strict_flags_for_runtime_defaults_and_key_removal(self):
        from context_metrics import classify_outcome

        for mutation in (
            "missing_question",
            "extra_thought_key",
            "missing_relevance",
            "string_gap",
        ):
            raw = fake_response()
            if mutation == "missing_question":
                del raw["thoughts"][0]["question"]
            elif mutation == "extra_thought_key":
                raw["thoughts"][0]["unexpected"] = "fake"
            elif mutation == "missing_relevance":
                del raw["relevance"]
            else:
                raw["knowledge_gaps"] = ["synthetic gap"]
            with self.subTest(mutation=mutation):
                report = classify_outcome(json.dumps(raw), context=fake_context())
                self.assertTrue(report["original_json_compliant"])
                self.assertFalse(report["original_schema_compliant"])
                self.assertTrue(report["tolerant_usable"])
                self.assertTrue(report["repair_applied"])

    def test_lexical_repairs_are_not_counted_as_original_compliance(self):
        from context_metrics import classify_outcome

        valid = json.dumps(fake_response())
        candidates = [
            "\ufeff" + valid,
            "synthetic prose " + valid,
            valid[:-1] + ",}",
            valid.replace("synthetic grounded body", "synthetic\nbody"),
        ]
        for text in candidates:
            with self.subTest(text=text[:15]):
                report = classify_outcome(text, context=fake_context())
                self.assertEqual(report["category"], "full_round_accepted")
                self.assertFalse(report["original_json_compliant"])
                self.assertFalse(report["original_schema_compliant"])
                self.assertTrue(report["tolerant_json_parseable"])

    def test_duplicates_nonfinite_second_documents_and_bad_nesting_fail(self):
        from context_metrics import classify_outcome

        valid = json.dumps(fake_response())
        for text in (
            '{"a":1,"a":2}',
            '{"a":NaN}',
            '{"a":1e9999}',
            valid + " {}",
            valid + " {",
            "{]",
            "not json",
        ):
            with self.subTest(text=text[-20:]):
                report = classify_outcome(text, context=fake_context())
                self.assertEqual(report["category"], "malformed_json")
                self.assertFalse(report["tolerant_usable"])
                self.assertFalse(report["original_json_compliant"])

    def test_truncation_never_salvages_complete_nested_thought(self):
        from context_metrics import classify_outcome

        valid = json.dumps(fake_response())
        for text in (valid[:-1], '{"x":"unterminated', "```json\n" + valid[:-2]):
            with self.subTest(text=text[-20:]):
                report = classify_outcome(text, context=fake_context())
                self.assertEqual(report["category"], "truncated_json")
                self.assertFalse(report["tolerant_json_parseable"])

    def test_nonobject_nontext_size_limit_and_parser_recursion_are_safe(self):
        from context_metrics import classify_outcome

        for text in ("[]", "null", "true", 42):
            with self.subTest(text=text):
                self.assertEqual(classify_outcome(text)["category"], "schema_violation")
        self.assertEqual(
            classify_outcome("x" * (2 * 1024 * 1024 + 1))["reason"], "response_size_limit"
        )
        report = classify_outcome("[" * 2000 + "]" * 2000)
        self.assertFalse(report["tolerant_usable"])

    def test_unknown_and_forward_refs_are_cross_reference_not_shape_failures(self):
        from context_metrics import classify_outcome

        for ref in ("unknown-fake-ref", "t2"):
            raw = fake_response()
            raw["thoughts"][0]["refs"] = [ref]
            raw["thoughts"].append(
                {"id": "t2", "kind": "idle", "text": "fake", "refs": [], "question": None}
            )
            report = classify_outcome(json.dumps(raw), context=fake_context())
            self.assertEqual(report["category"], "cross_reference_violation")
            self.assertTrue(report["original_schema_compliant"])
        raw = fake_response()
        raw["resolved_questions"] = ["fake-missing-question"]
        self.assertEqual(
            classify_outcome(json.dumps(raw), context=fake_context())["category"],
            "cross_reference_violation",
        )

    def test_question_assessments_cross_references_and_shape(self):
        from context_metrics import classify_outcome

        context = fake_context()
        context["capabilities"] = [{"id": "fake-cap"}]
        raw = fake_response()
        raw["thoughts"][0].update(
            kind="question",
            question={
                "text": "synthetic question",
                "assessments": [
                    {"capability_id": "fake-cap", "can_resolve": True, "reason": "synthetic reason"}
                ],
                "selected_capability": "fake-cap",
                "missing_information": "",
            },
        )
        self.assertEqual(
            classify_outcome(json.dumps(raw), context=context)["category"], "full_round_accepted"
        )
        raw["thoughts"][0]["question"]["assessments"][0]["can_resolve"] = "true"
        self.assertEqual(
            classify_outcome(json.dumps(raw), context=context)["category"], "schema_violation"
        )
        raw["thoughts"][0]["question"]["assessments"][0]["can_resolve"] = True
        raw["thoughts"][0]["question"]["selected_capability"] = "unknown-fake-cap"
        self.assertEqual(
            classify_outcome(json.dumps(raw), context=context)["category"],
            "cross_reference_violation",
        )

    def test_missing_matrix_preserves_thoughts_but_never_claims_full_round(self):
        from context_metrics import classify_outcome

        context = fake_context()
        context["capabilities"] = [{"id": "fake-cap"}]
        context["relevance_candidates"] = [{"belief_id": "fake-belief"}]
        report = classify_outcome(json.dumps(fake_response()), context=context)
        self.assertEqual(report["category"], "schema_violation")
        self.assertEqual(report["reason"], "incomplete_relevance_matrix")
        self.assertEqual(report["missing_relevance_count"], 1)
        self.assertTrue(report["tolerant_usable"])
        self.assertFalse(report["full_round_accepted"])
        context["cached_relevance"] = [
            {
                "belief_id": "fake-belief",
                "capability_id": "fake-cap",
                "score": 0.2,
                "reason": "synthetic",
            }
        ]
        self.assertTrue(
            classify_outcome(json.dumps(fake_response()), context=context)["full_round_accepted"]
        )

    def test_invalid_relevance_and_update_shapes_or_references(self):
        from context_metrics import classify_outcome

        context = fake_context()
        context["capabilities"] = [{"id": "fake-cap"}]
        context["relevance_candidates"] = [{"belief_id": "fake-belief"}]
        valid_row = {
            "belief_id": "fake-belief",
            "capability_id": "fake-cap",
            "score": 0.2,
            "reason": "synthetic",
        }
        for mutation, expected in (
            ({"score": True}, "schema_violation"),
            ({"score": 1.2}, "schema_violation"),
            ({"belief_id": "unknown-fake-belief"}, "cross_reference_violation"),
            ({"capability_id": "unknown-fake-cap"}, "cross_reference_violation"),
        ):
            raw = fake_response()
            raw["relevance"] = [{**valid_row, **mutation}]
            with self.subTest(mutation=mutation):
                self.assertEqual(
                    classify_outcome(json.dumps(raw), context=context)["category"], expected
                )
        raw = fake_response()
        raw["belief_updates"] = ["not an object"]
        self.assertEqual(
            classify_outcome(json.dumps(raw), context=context)["category"], "schema_violation"
        )

    def test_drops_and_cascading_dependencies_match_actual_validator(self):
        from context_metrics import classify_outcome
        from inner_runtime import validate

        raw = fake_response()
        raw["thoughts"].extend(
            [
                {
                    "id": "t2",
                    "kind": "inference",
                    "text": "fake unsupported",
                    "refs": [],
                    "question": None,
                },
                {
                    "id": "t3",
                    "kind": "inference",
                    "text": "fake cascade",
                    "refs": ["t2"],
                    "question": None,
                },
            ]
        )
        context = fake_context()
        context["unresolved_questions"] = [{"question_id": "fake-question"}]
        raw["resolved_questions"] = ["fake-question"]
        result = validate(json.dumps(raw), {"fake-event"}, [], context["unresolved_questions"])
        report = classify_outcome(json.dumps(raw), context=context)
        self.assertEqual(len(result["thoughts"]), report["retained_item_count"])
        self.assertEqual(report["dropped_item_count"], 2)
        self.assertEqual(report["category"], "single_item_dropped")
        self.assertEqual(result["resolved_questions"], [])
        self.assertFalse(report["full_round_accepted"])

    def test_idle_does_not_become_evidence_and_empty_drop_is_unusable(self):
        from context_metrics import classify_outcome

        raw = fake_response()
        raw["thoughts"][0].update(kind="idle", refs=[])
        raw["thoughts"].append(
            {"id": "t2", "kind": "inference", "text": "fake", "refs": ["t1"], "question": None}
        )
        report = classify_outcome(json.dumps(raw), context=fake_context())
        self.assertEqual(report["category"], "single_item_dropped")
        self.assertEqual(report["dropped_item_count"], 1)
        raw = fake_response()
        raw["thoughts"][0]["refs"] = []
        report = classify_outcome(json.dumps(raw), context=fake_context())
        self.assertEqual(report["category"], "unsupported_evidence")
        self.assertFalse(report["tolerant_usable"])
        self.assertTrue(report["original_schema_compliant"])

    def test_explicit_evidence_verdict_is_separate_from_json_schema(self):
        from context_metrics import classify_outcome

        report = classify_outcome(
            json.dumps(fake_response()), context=fake_context(), evidence_supported=False
        )
        self.assertEqual(report["category"], "unsupported_evidence")
        self.assertTrue(report["original_json_compliant"])
        self.assertTrue(report["original_schema_compliant"])
        self.assertFalse(report["tolerant_usable"])
        self.assertFalse(report["full_round_accepted"])
        self.assertIsNone(
            classify_outcome(json.dumps(fake_response()), context=fake_context())[
                "semantic_evidence_supported"
            ]
        )

    def test_actual_runtime_parser_and_validators_are_exercised(self):
        import curiosity
        import inner_runtime
        from context_metrics import classify_outcome

        with (
            patch.object(
                inner_runtime, "parse_response_json", wraps=inner_runtime.parse_response_json
            ) as parse,
            patch.object(inner_runtime, "validate", wraps=inner_runtime.validate) as validate,
            patch.object(
                curiosity, "validate_updates", wraps=curiosity.validate_updates
            ) as updates,
            patch.object(curiosity, "validate_matrix", wraps=curiosity.validate_matrix) as matrix,
        ):
            report = classify_outcome(json.dumps(fake_response()), context=fake_context())
        self.assertTrue(report["full_round_accepted"])
        self.assertGreaterEqual(parse.call_count, 2)
        validate.assert_called_once()
        self.assertGreaterEqual(updates.call_count, 1)
        matrix.assert_called_once()

    def test_prompt_visible_reference_set_matches_runtime(self):
        from context_metrics import _allowed_refs, classify_outcome
        from inner_runtime import InnerLoop

        context = fake_context()
        context["memories"] = [{"id": "fake-memory"}]
        context["beliefs"] = [{"belief_id": "fake-belief"}]
        context["recalled"] = [{"id": "fake-recall"}]
        context["recent_thoughts"] = [{"id": "fake-recent"}]
        context["pending_observations"] = [{"id": "fake-observation", "payload": {"data": {}}}]
        context["pending_memories"] = [{"origin_id": "memory:fake-pending"}]
        self.assertEqual(_allowed_refs(context), InnerLoop.outreach_refs(context))
        for ref in _allowed_refs(context):
            raw = fake_response()
            raw["thoughts"][0]["refs"] = [ref]
            self.assertTrue(classify_outcome(json.dumps(raw), context=context)["tolerant_usable"])

    def test_no_store_model_network_or_data_root_access(self):
        import inner_runtime
        from context_metrics import classify_outcome, measure_context, self_evaluate

        # Actual imports are permitted; executing any private-state path is not.
        context = fake_context()
        with (
            patch("builtins.open", side_effect=AssertionError("no file access")),
            patch("pathlib.Path.open", side_effect=AssertionError("no path access")),
            patch("sqlite3.connect", side_effect=AssertionError("no database access")),
            patch("socket.socket", side_effect=AssertionError("no network")),
            patch.object(inner_runtime, "MindStore", side_effect=AssertionError("no store")),
            patch.object(inner_runtime, "ProductModel", side_effect=AssertionError("no model")),
        ):
            self.assertTrue(
                classify_outcome(json.dumps(fake_response()), context=context)[
                    "full_round_accepted"
                ]
            )
            measure_context(context)
            result = self_evaluate()
            self.assertTrue(result["known_limitations"])
            self.assertTrue(any("not" in s for s in result["known_limitations"]))

    def test_response_context_and_error_text_are_never_returned_or_mutated(self):
        from context_metrics import classify_outcome

        context = fake_context()
        snapshot = deepcopy(context)
        raw = fake_response()
        text = json.dumps(raw)
        report = classify_outcome(text, context=context)
        self.assertEqual(context, snapshot)
        self.assertEqual(text, json.dumps(raw))
        self.assertNotIn("synthetic grounded body", json.dumps(report))
        self.assertNotIn("fake-event", json.dumps(report))
        error_report = classify_outcome(
            text, context=context, transport_error=RuntimeError("private-marker")
        )
        self.assertEqual(error_report["category"], "transport_error")
        self.assertNotIn("private-marker", json.dumps(error_report))

    def test_actual_bounding_omission_counters_and_utf8_bytes(self):
        from context_metrics import measure_context
        from inner_runtime import bound_context

        context = fake_context()
        context["events"] = [{"id": f"fake-{i}", "text": "合成" * 200} for i in range(20)]
        original = deepcopy(context)
        with patch("inner_runtime.INNER_CONTEXT_BYTE_LIMIT", 1800):
            bounded = bound_context(context)
        self.assertEqual(context, original)
        report = measure_context(bounded)
        self.assertLessEqual(report["context_utf8_bytes"] + 256, 1800)
        self.assertEqual(
            report["sections"]["events"]["item_count"]
            + report["sections"]["events"]["omitted_count"],
            20,
        )
        self.assertGreater(report["sections"]["events"]["omitted_count"], 0)

    def test_outreach_bad_shape_and_unknown_evidence_are_separated(self):
        from context_metrics import classify_outcome

        raw = fake_response()
        raw["outreach_intents"] = [
            {
                "reason": "synthetic",
                "evidence": ["fake-event"],
                "urgency": "normal",
                "needs_response": False,
            }
        ]
        self.assertTrue(
            classify_outcome(json.dumps(raw), context=fake_context())["full_round_accepted"]
        )
        raw["outreach_intents"][0]["evidence"] = ["fake-missing"]
        self.assertEqual(
            classify_outcome(json.dumps(raw), context=fake_context())["category"],
            "cross_reference_violation",
        )
        raw["outreach_intents"][0]["needs_response"] = True
        self.assertEqual(
            classify_outcome(json.dumps(raw), context=fake_context())["category"],
            "schema_violation",
        )

    def test_belief_origin_and_memory_identity_checks_do_not_write(self):
        from context_metrics import classify_outcome

        context = fake_context()
        context["pending_memories"] = [{"origin_id": "memory:fake"}]
        raw = fake_response()
        raw["belief_updates"] = [
            {
                "origin_id": "memory:fake",
                "belief_id": "memory:fake",
                "subject": "synthetic",
                "attribute": "synthetic",
                "value": "synthetic",
                "importance": 0.5,
                "conflict": False,
            }
        ]
        self.assertTrue(classify_outcome(json.dumps(raw), context=context)["full_round_accepted"])
        for target in (None, "memory:wrong-fake"):
            raw["belief_updates"][0]["belief_id"] = target
            report = classify_outcome(json.dumps(raw), context=context)
            self.assertEqual(report["category"], "cross_reference_violation")
            self.assertEqual(report["reason"], "belief_target_mismatch")
        raw["belief_updates"][0]["origin_id"] = "missing-fake"
        self.assertEqual(
            classify_outcome(json.dumps(raw), context=context)["reason"], "unknown_belief_origin"
        )

    def test_explicit_references_catalog_and_questions_override_context(self):
        from context_metrics import classify_outcome

        raw = fake_response()
        raw["resolved_questions"] = ["fake-question"]
        report = classify_outcome(
            json.dumps(raw),
            context={},
            allowed_refs={"fake-event"},
            catalog=[],
            questions=[{"question_id": "fake-question"}],
        )
        self.assertTrue(report["full_round_accepted"])
        self.assertFalse(
            classify_outcome(
                json.dumps(raw), context={}, allowed_refs=set(), catalog=[], questions=[]
            )["full_round_accepted"]
        )

    def test_malformed_thoughts_are_not_accidentally_accepted(self):
        from context_metrics import classify_outcome

        for mutation in (
            {"kind": []},
            {"id": []},
            {"refs": "fake-event"},
            {"text": ""},
            {"question": "not-an-object"},
            {"kind": "unknown"},
            {"text": "x" * 3001},
        ):
            raw = fake_response()
            raw["thoughts"][0].update(mutation)
            with self.subTest(mutation=list(mutation)):
                report = classify_outcome(json.dumps(raw), context=fake_context())
                self.assertEqual(report["category"], "schema_violation")
                self.assertFalse(report["original_schema_compliant"])
                self.assertFalse(report["tolerant_usable"])

    def test_transport_precedence_and_schema_originals_are_not_overclaimed(self):
        from context_metrics import classify_outcome

        for text in (None, "", '{"thoughts":[', json.dumps(fake_response())):
            report = classify_outcome(text, transport_error=True)
            self.assertEqual(report["category"], "transport_error")
            self.assertFalse(report["original_json_compliant"])
        raw = fake_response()
        del raw["belief_updates"]
        report = classify_outcome(json.dumps(raw), context=fake_context())
        self.assertTrue(report["original_json_compliant"])
        self.assertFalse(report["original_schema_compliant"])
        self.assertTrue(report["tolerant_usable"])

    def test_category_contract_and_self_evaluation_are_explicit(self):
        from context_metrics import OUTCOME_CATEGORIES, self_evaluate

        self.assertEqual(len(OUTCOME_CATEGORIES), 9)
        self.assertEqual(len(set(OUTCOME_CATEGORIES)), 9)
        description = json.dumps(self_evaluate())
        for limitation in ("HTTP", "cache", "Curiosity", "Semantic", "daemon", "Truncation"):
            self.assertIn(limitation, description)


def fake_context() -> dict[str, Any]:
    return {
        "instructions": "synthetic",
        "capabilities": [],
        "events": [{"id": "fake-event"}],
        "thoughts": [],
        "memories": [],
        "beliefs": [],
        "recent_thoughts": [],
        "recalled": [],
        "pending_observations": [],
        "relevance_candidates": [],
        "pending_memories": [],
        "unresolved_questions": [],
        "cached_relevance": [],
    }


def fake_response() -> dict[str, Any]:
    return {
        "thoughts": [
            {
                "id": "t1",
                "kind": "inference",
                "text": "synthetic grounded body",
                "refs": ["fake-event"],
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


if __name__ == "__main__":
    unittest.main()
