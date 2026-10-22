from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import memory_distiller
from memory_distiller import ModelOutputError
from skill_distiller import SkillDistiller
from skill_manifest import SkillManifest
from skill_registry import SkillRegistry


def proposal() -> str:
    return json.dumps(
        {
            "name": "validate-files",
            "description": "Validate supplied files.",
            "steps": ["Check the prerequisites.", "Validate the result, preserving evidence."],
            "trigger_patterns": ["validate *"],
        }
    )


class SkillDistillerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="skill-distill-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        self.registry = SkillRegistry(self.root / "registry.db")
        self.addCleanup(self.registry.close)
        self.loader = SimpleNamespace(root=self.root / "skills", registry=self.registry)
        self.distiller = SkillDistiller(self.root, cast(Any, self.loader))

    def distill(self, response: object) -> str | None:
        with mock.patch.object(memory_distiller, "distillation_model", return_value=response):
            return self.distiller.distill_from_task(
                "Validate files", "Verified file checks", "files"
            )

    def assert_pending(self, name: str | None) -> None:
        self.assertIsNotNone(name)
        directory = self.loader.root / cast(str, name)
        manifest = SkillManifest.from_dict(
            json.loads((directory / "manifest.json").read_text()), require_checksum=True
        )
        self.assertEqual(manifest.name, name)
        self.assertIn("PENDING", manifest.tags)
        pending = json.loads((directory / "pending.json").read_text())
        self.assertEqual(pending["status"], "pending")
        self.assertTrue(pending["requires_review"])
        with closing(sqlite3.connect(self.registry.path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT active,deleted FROM skill_versions WHERE name=?", (name,)
                ).fetchone(),
                (0, 0),
            )

    def test_fenced_json_publishes_only_pending_proposal(self) -> None:
        self.assert_pending(self.distill("```json\n" + proposal() + "\n```"))

    def test_surrounding_prose_is_tolerated(self) -> None:
        self.assert_pending(self.distill("Result follows:\n" + proposal() + "\nEnd."))

    def test_prose_with_trailing_comma_is_tolerated(self) -> None:
        self.assert_pending(self.distill("Result: " + proposal()[:-1] + ",} Done."))

    def test_truncated_reply_leaves_no_proposal_and_same_task_can_retry(self) -> None:
        with self.assertRaisesRegex(ModelOutputError, "truncated"):
            self.distill(proposal()[:-1])
        with closing(sqlite3.connect(self.registry.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM skill_versions").fetchone()[0], 0)
        self.assertFalse(any(self.loader.root.glob("*/manifest.json")))
        self.assert_pending(self.distill(proposal()))

    def test_empty_response_is_not_a_valid_no_proposal(self) -> None:
        with self.assertRaisesRegex(ModelOutputError, "empty"):
            self.distill("")

    def test_non_text_reply_is_rejected_without_a_proposal(self) -> None:
        with self.assertRaises(ModelOutputError):
            self.distill(None)
        self.assertFalse(any(self.loader.root.glob("*/manifest.json")))

    def test_explicit_null_means_no_proposal(self) -> None:
        self.assertIsNone(self.distill("null"))
        self.assertFalse(any(self.loader.root.glob("*/manifest.json")))

    def test_duplicate_task_is_not_regenerated(self) -> None:
        self.assert_pending(self.distill(proposal()))
        with mock.patch.object(memory_distiller, "distillation_model") as model:
            self.assertIsNone(
                self.distiller.distill_from_task("Validate files", "Verified file checks", "files")
            )
        model.assert_not_called()

    def test_invalid_schema_does_not_publish(self) -> None:
        with self.assertRaises(ValueError):
            self.distill('{"name":"validate-files","description":"checks","steps":[]}')
        self.assertFalse(any(self.loader.root.glob("*/manifest.json")))


if __name__ == "__main__":
    unittest.main()
