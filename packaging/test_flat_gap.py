"""Flat gap wire compatibility; synthetic, private-free fixtures only."""

from __future__ import annotations

import copy
import json
import unittest
from typing import Any

from inner_runtime import INSTRUCTIONS, parse_response_json, validate


GAP = {
    "subject": "fixture",
    "attribute": "unknown",
    "importance": 0.8,
    "reason": "synthetic evidence needed",
}
CATALOG = [{"id": "cap-fixture"}]


def response(flat: bool = True) -> dict[str, Any]:
    q: dict[str, Any] = {
        "text": "Synthetic question",
        "assessments": [
            {"capability_id": "cap-fixture", "can_resolve": False, "reason": "unavailable"}
        ],
        "selected_capability": None,
        "missing_information": "synthetic unknown",
    }
    q.update({"gap_" + k: v for k, v in GAP.items()} if flat else {"gap": copy.deepcopy(GAP)})
    return {
        "thoughts": [
            {
                "id": "t1",
                "kind": "question",
                "text": "Synthetic thought",
                "refs": ["fixture-source"],
                "question": q,
            }
        ],
        "resolved_questions": [],
    }


def check(raw: dict[str, Any], refs: set[str] | None = None) -> dict[str, Any]:
    return validate(json.dumps(raw), {"fixture-source"} if refs is None else refs, CATALOG, [])


class FlatGapTests(unittest.TestCase):
    def test_new_and_old_have_identical_canonical_result(self):
        new = check(response())
        self.assertEqual(new, check(response(False)))
        q = new["thoughts"][0]["question"]
        self.assertEqual(q["gap"], GAP)
        self.assertFalse(any(k.startswith("gap_") for k in q))

    def test_prompt_only_advertises_flat_gap(self):
        for key in GAP:
            self.assertIn('"gap_' + key + '"', INSTRUCTIONS)
        self.assertNotIn('"gap":{', INSTRUCTIONS)

    def test_each_missing_flat_field_is_rejected(self):
        for key in GAP:
            with self.subTest(field=key):
                raw = response()
                del raw["thoughts"][0]["question"]["gap_" + key]
                with self.assertRaises(ValueError):
                    check(raw)

    def test_mixed_old_and_flat_is_rejected_even_when_identical(self):
        for flat in (True, False):
            raw = response(flat)
            q = raw["thoughts"][0]["question"]
            q.update({"gap": copy.deepcopy(GAP)} if flat else {"gap_reason": GAP["reason"]})
            with self.subTest(flat=flat), self.assertRaises(ValueError):
                check(raw)

    def test_flat_types_ranges_and_blanks_are_not_relaxed(self):
        for key, values in {
            "subject": [None, 3, [], "", " ", "x" * 1001],
            "attribute": [None, False, {}, "", " "],
            "reason": [None, 3, [], "", " "],
            "importance": [True, None, "0.8", [], -0.1, 1.1, float("nan"), float("inf")],
        }.items():
            for value in values:
                for flat in (True, False):
                    raw = response(flat)
                    q = raw["thoughts"][0]["question"]
                    if flat:
                        q["gap_" + key] = value
                    else:
                        q["gap"][key] = value
                    with (
                        self.subTest(field=key, flat=flat, type=type(value).__name__),
                        self.assertRaises(ValueError),
                    ):
                        check(raw)

    def test_legacy_optional_gap_remains_optional(self):
        raw = response(False)
        del raw["thoughts"][0]["question"]["gap"]
        self.assertNotIn("gap", check(raw)["thoughts"][0]["question"])

    def test_flat_conversion_preserves_unrelated_legacy_keys(self):
        raw = response()
        raw["thoughts"][0]["question"]["target"] = "fixture"
        q = check(raw)["thoughts"][0]["question"]
        self.assertEqual(q["target"], "fixture")
        self.assertEqual(q["gap"], GAP)
        old = response(False)
        old["thoughts"][0]["question"]["target"] = "fixture"
        self.assertEqual(check(raw), check(old))

    def test_evidence_capability_and_question_guards_still_apply(self):
        for mutation in (
            "no_refs",
            "unknown_refs",
            "missing_assessment",
            "bad_selection",
            "no_missing_info",
            "null_question",
            "not_question",
        ):
            raw = response()
            t = raw["thoughts"][0]
            q = t["question"]
            if mutation == "no_refs":
                t["refs"] = []
            if mutation == "unknown_refs":
                t["refs"] = ["not-supplied"]
            if mutation == "missing_assessment":
                q["assessments"] = []
            if mutation == "bad_selection":
                q["selected_capability"] = "cap-fixture"
            if mutation == "no_missing_info":
                q["missing_information"] = ""
            if mutation == "null_question":
                t["question"] = None
            if mutation == "not_question":
                t["kind"] = "review"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                check(raw)

    def test_parser_never_adds_missing_delimiter(self):
        text = json.dumps(response(False))
        text = text.replace('"synthetic evidence needed"}}}', '"synthetic evidence needed"}}', 1)
        with self.assertRaises(ValueError):
            parse_response_json(text)

    def test_old_missing_fields_and_wrong_types_still_rejected(self):
        for key in GAP:
            raw = response(False)
            del raw["thoughts"][0]["question"]["gap"][key]
            with self.subTest(field=key), self.assertRaises(ValueError):
                check(raw)
        for value in (None, 4, [], {}):
            raw = response(False)
            raw["thoughts"][0]["question"]["gap"] = value
            with self.subTest(type=type(value).__name__), self.assertRaises(ValueError):
                check(raw)


if __name__ == "__main__":
    unittest.main()
