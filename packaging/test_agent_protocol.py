"""Pure JSON protocol regression tests; no endpoints or model calls."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID

import agent_protocol as protocol


class SelfEvaluationTests(unittest.TestCase):
    def test_self_evaluate_contract(self):
        hook = getattr(protocol, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-level self_evaluate is required")
        before = datetime.now(UTC)
        report = hook()
        self.assertEqual(
            set(report),
            {
                "module",
                "domain",
                "current_version",
                "known_limitations",
                "improvement_directions",
                "last_evaluated_at",
            },
        )
        self.assertEqual(report["module"], "agent_protocol")
        for field in ("domain", "current_version"):
            self.assertIsInstance(report[field], str)
            self.assertTrue(report[field].strip())
        for field in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[field], list)
            self.assertTrue(report[field])
            self.assertTrue(all(isinstance(item, str) and item.strip() for item in report[field]))
        stamp = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.utcoffset(), UTC.utcoffset(stamp))
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, datetime.now(UTC))
        json.dumps(report, allow_nan=False)

    def test_self_evaluate_is_fresh_and_has_no_execution_or_file_effects(self):
        hook = getattr(protocol, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-level self_evaluate is required")
        with (
            patch("builtins.open", side_effect=AssertionError("unexpected file access")),
            patch.object(
                protocol, "serialize", side_effect=AssertionError("unexpected protocol use")
            ),
        ):
            first = hook()
            first["known_limitations"].append("caller-only")
            first["improvement_directions"].clear()
            second = hook()
        self.assertNotIn("caller-only", second["known_limitations"])
        self.assertTrue(second["improvement_directions"])


class ProtocolTests(unittest.TestCase):
    def test_task_roundtrip_preserves_unicode_and_explicit_identity(self):
        message = protocol.TaskMessage(
            task_id="legacy: 001",
            task_type="research",
            description="研究 α",
            priority=3,
            effort="ultra",
            dependencies=["dep"],
            metadata={"nested": [True, None, 1.25, {"原文": "保留"}]},
        )
        wire = protocol.serialize(message)
        restored = protocol.deserialize(wire)
        self.assertEqual(restored, message)
        self.assertIn("研究", wire)
        restored.metadata["nested"].append("mutated")
        self.assertNotEqual(restored.metadata, message.metadata)
        self.assertEqual(UUID(protocol.TaskMessage().task_id).version, 4)

    def test_result_roundtrip_and_terminal_statuses(self):
        for status in sorted(protocol.RESULT_STATUSES):
            with self.subTest(status=status):
                result = protocol.TaskResult(
                    "id",
                    status,
                    output="结果",
                    error="diagnostic",
                    duration_seconds=0.25,
                    model_used="fake",
                    token_usage={"in": 1},
                    critic_review={"passed": False},
                )
                self.assertEqual(
                    protocol.deserialize_result(protocol.serialize_result(result)), result
                )

    def test_duplicate_unknown_and_nonobject_envelopes_are_rejected(self):
        for wire in (
            "[]",
            "null",
            '{"priority":1,"priority":2}',
            '{"metadata":{"x":1,"x":2}}',
            '{"invented":1}',
            '{"metadata":{"x":NaN}}',
        ):
            with self.subTest(wire=wire), self.assertRaises(ValueError):
                protocol.deserialize(wire)
        for wire in (
            "{}",
            '{"task_id":"id","status":"running"}',
            '{"task_id":"id","status":"completed","extra":1}',
        ):
            with self.subTest(wire=wire), self.assertRaises(ValueError):
                protocol.deserialize_result(wire)

    def test_invalid_task_fields_fail_without_lossy_coercion(self):
        for values in (
            {"task_id": ""},
            {"task_type": "image"},
            {"priority": True},
            {"priority": 4},
            {"effort": "unknown"},
            {"timeout_seconds": 0},
            {"timeout_seconds": True},
            {"model_id": ""},
            {"dependencies": ("id",)},
            {"task_id": "id", "dependencies": ["id"]},
            {"dependencies": ["a", "a"]},
            {"metadata": {1: "bad"}},
            {"metadata": {"tuple": (1, 2)}},
            {"metadata": {"x": float("inf")}},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                protocol.TaskMessage(**values)
        cycle = []
        cycle.append(cycle)
        with self.assertRaises(ValueError):
            protocol.TaskMessage(metadata={"cycle": cycle})

    def test_invalid_results_and_mutated_messages_revalidated(self):
        for values in (
            {"duration_seconds": -1},
            {"duration_seconds": True},
            {"duration_seconds": float("nan")},
            {"token_usage": []},
            {"critic_review": {"x": float("inf")}},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                protocol.TaskResult("id", "completed", **values)
        message = protocol.TaskMessage()
        message.priority = True
        with self.assertRaises(ValueError):
            protocol.serialize(message)
        result = protocol.TaskResult("id", "completed")
        result.status = "running"
        with self.assertRaises(ValueError):
            protocol.serialize_result(result)


class AdmissionTests(unittest.TestCase):
    def test_registry_detects_real_module_hook(self):
        from integration_registry import REGISTRY

        self.assertTrue(REGISTRY._has_self_evaluate(REGISTRY.get("agent_protocol")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
