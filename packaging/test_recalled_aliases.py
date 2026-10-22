"""Current-round mind-entry aliases never expand the provenance allowlist."""

import json
import unittest

from inner_runtime import validate


def body(ref: str) -> str:
    return json.dumps(
        {
            "thoughts": [
                {"id": "t1", "kind": "review", "text": "synthetic", "refs": [ref], "question": None}
            ],
            "resolved_questions": [],
            "relevance": [],
            "scheduler_question": None,
            "belief_updates": [],
            "knowledge_gaps": [],
            "outreach_intents": [],
        }
    )


class RecalledAliasTests(unittest.TestCase):
    def test_supplied_entry_alias_maps_only_to_supplied_id(self) -> None:
        try:
            result = validate(body("mind:entries:visible"), {"visible"}, [], [])
        except ValueError:
            self.fail("current-round entry alias rejected")
        self.assertEqual(result["thoughts"][0]["refs"], ["visible"])
        self.assertEqual(result["ref_normalizations"][0]["reason"], "recalled_evidence_alias")

    def test_supplied_combined_alias_maps_only_to_supplied_id(self) -> None:
        try:
            result = validate(body("memory:mind:entries:visible"), {"visible"}, [], [])
        except ValueError:
            self.fail("current-round combined alias rejected")
        self.assertEqual(result["thoughts"][0]["refs"], ["visible"])
        self.assertEqual(result["ref_normalizations"][0]["reason"], "recalled_evidence_alias")

    def test_unseen_combined_alias_and_chained_forms_still_fail(self) -> None:
        for ref in (
            "memory:mind:entries:elsewhere",
            "memory:mind:entries:",
            "memory:memory:mind:entries:visible",
            "memory:mind:entries:memory:visible",
            "memory:mind:entries:t1",
        ):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(ValueError, "Invalid thought provenance"):
                    validate(body(ref), {"visible"}, [], [])

    def test_gap_and_self_reference_are_never_accepted(self) -> None:
        # A gap identity is a topic, not evidence, even when its raw id is supplied;
        # t1 is this thought's own id and cannot ground itself.
        for ref in ("gap:visible", "gap:recalled", "t1"):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(ValueError, "Invalid thought provenance"):
                    validate(body(ref), {"visible", "recalled"}, [], [])

    def test_unseen_entry_and_nested_aliases_still_fail(self) -> None:
        for ref in (
            "mind:entries:elsewhere",
            "mind:entries:",
            "mind:entries:memory:visible",
            "mind:entries:t1",
            "thought:elsewhere",
            "event:elsewhere",
            "nested-id",
        ):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(ValueError, "Invalid thought provenance"):
                    validate(body(ref), {"visible"}, [], [])

    def test_canonical_prefixed_refs_are_not_rewritten(self) -> None:
        ref = "mind:entries:visible"
        self.assertEqual(
            validate(body(ref), {ref, "visible"}, [], [])["thoughts"][0]["refs"], [ref]
        )

    def test_alias_does_not_admit_tool_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid thought provenance"):
            validate(body("mind:entries:cap"), {"visible"}, [{"id": "cap"}], [])

    def test_belief_version_alias_is_not_stripped_to_a_foreign_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid thought provenance"):
            validate(body("mind:belief_versions:7"), {"7"}, [], [])


if __name__ == "__main__":
    unittest.main()
