"""Offline CLI regression tests; fixtures never touch a resident data root."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("export_mind_stats.py")


class ReplayExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(
            prefix="mind-replay-", dir=os.environ["TMPDIR"]
        )
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.launch = self.root / "LAUNCH.json"
        self.launch.write_text(json.dumps({"runtime_data_root": str(self.root)}))
        with sqlite3.connect(self.root / "mind.db") as connection:
            connection.execute(
                "CREATE TABLE entries "
                "(seq INTEGER PRIMARY KEY, id TEXT, category TEXT, payload TEXT)"
            )

    def append(self, identity: str, category: str, payload: dict) -> None:
        with sqlite3.connect(self.root / "mind.db") as connection:
            connection.execute(
                "INSERT INTO entries(id, category, payload) VALUES (?, ?, ?)",
                (identity, category, json.dumps(payload)),
            )

    def export(self, *, replay: bool = True) -> dict:
        output = self.root / "report.json"
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT),
                "--launch",
                str(self.launch),
                "--out",
                str(output),
                *(["--replay"] if replay else []),
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(output.read_text())

    def test_replay_captures_real_validator_exception(self) -> None:
        self.append(
            "call-1",
            "model_call",
            {"context": {"allowed_refs": [], "catalog": [], "unresolved_questions": []}},
        )
        self.append("response-1", "model_response", {"call_id": "call-1", "text": "{}"})
        self.append("error-1", "validation_error", {"call_id": "call-1"})

        sample = self.export()["rejected_samples"][0]

        self.assertEqual(sample["replay"]["status"], "rejected")
        self.assertEqual(sample["replay"]["exception_type"], "ValueError")
        self.assertEqual(sample["replay"]["exception_message"], "Envelope fields")

    @staticmethod
    def native_context() -> dict:
        return {
            "events": [{"id": "event-1"}],
            "thoughts": [],
            "memories": [{"id": "memory-1"}],
            "beliefs": [],
            "recent_thoughts": [],
            "recalled": [],
            "pending_observations": [],
            "relevance_candidates": [],
            "pending_memories": [],
            "capabilities": [],
            "unresolved_questions": [{"question_id": "question-1"}],
        }

    def test_replay_uses_native_context_and_exact_call_id(self) -> None:
        context = self.native_context()
        # Same round, different call: pairing by round or reading payload['call_id']
        # would select the wrong context or miss the match.
        self.append("wrong-call", "model_call", {"round": 1, "context": {}})
        self.append("right-call", "model_call", {"round": 1, "context": context})
        text = json.dumps(
            {
                "thoughts": [
                    {
                        "id": "t1",
                        "kind": "inference",
                        "text": "A thought citing persisted inputs.",
                        "refs": ["event-1", "memory:memory-1"],
                        "question": None,
                    }
                ],
                "resolved_questions": ["question-1"],
            }
        )
        self.append("response", "model_response", {"call_id": "right-call", "text": text})
        self.append("error", "validation_error", {"call_id": "right-call"})
        self.assertEqual(self.export()["rejected_samples"][0]["replay"], {"status": "passed"})

    def test_incomplete_or_malformed_context_is_unavailable(self) -> None:
        for index, context in enumerate(
            (
                {},
                {"allowed_refs": [], "catalog": []},
                {"allowed_refs": "not-a-list", "catalog": [], "unresolved_questions": []},
                {**self.native_context(), "memories": None},
            )
        ):
            call_id = f"call-{index}"
            self.append(call_id, "model_call", {"context": context})
            self.append(f"response-{index}", "model_response", {"call_id": call_id, "text": "{}"})
            self.append(f"error-{index}", "validation_error", {"call_id": call_id})
        samples = self.export()["rejected_samples"]
        self.assertEqual(len(samples), 4)
        for sample in samples:
            self.assertEqual(sample["replay"]["status"], "context_unavailable")
            self.assertTrue(sample["replay"]["reason"])
            self.assertNotIn("exception_type", sample["replay"])

    def test_missing_call_is_unavailable(self) -> None:
        self.append("response", "model_response", {"call_id": "missing", "text": "{}"})
        self.append("error", "validation_error", {"call_id": "missing"})
        self.assertEqual(
            self.export()["rejected_samples"][0]["replay"],
            {"status": "context_unavailable", "reason": "matching model_call not found"},
        )

    def test_replay_retains_every_rejected_response_including_invalid_json(self) -> None:
        texts = ("{", "[]", "null", "{}", '{"thoughts": 1}', '{"thoughts": [{"refs": [{}]}]}')
        for index, text in enumerate(texts):
            call_id = f"call-{index}"
            self.append(
                call_id,
                "model_call",
                {"context": {"allowed_refs": [], "catalog": [], "unresolved_questions": []}},
            )
            self.append(f"response-{index}", "model_response", {"call_id": call_id, "text": text})
            self.append(f"error-{index}", "validation_error", {"call_id": call_id})
        samples = self.export()["rejected_samples"]
        self.assertEqual(len(samples), len(texts))
        self.assertEqual({sample["call_id"] for sample in samples}, {f"call-{i}" for i in range(6)})
        self.assertEqual(samples[0]["replay"]["exception_type"], "JSONDecodeError")
        for sample in samples:
            self.assertEqual(sample["replay"]["status"], "rejected")
            self.assertTrue(sample["replay"]["exception_message"])

    def test_non_replay_report_preserves_legacy_fields_and_limit(self) -> None:
        for index in range(6):
            call_id = f"call-{index}-long-identifier"
            self.append(f"response-{index}", "model_response", {"call_id": call_id, "text": "{}"})
            self.append(f"error-{index}", "validation_error", {"call_id": call_id})
        report = self.export(replay=False)
        self.assertEqual(len(report["rejected_samples"]), 5)
        self.assertEqual(
            report["rejected_samples"][0],
            {
                "call_id": "call-0-long-identifier"[:12],
                "chars": 2,
                "local_refs": [],
                "thought_kinds": [],
            },
        )
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["local_ref_breakdown"]["rejected_without"], 6)
        self.assertEqual(report["reject_rate"], 1.0)
        self.assertNotIn("replay_scope", report)


if __name__ == "__main__":
    unittest.main()
