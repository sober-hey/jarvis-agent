"""Bounded, restartable mind-entry projection; all fixtures are private."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from inner_loop import MindStore
from kg_projector import CATEGORY_MAP, SKIP_CATEGORIES, KGProjector
from knowledge_graph import KG_CATEGORIES, NODE_TYPES, KnowledgeGraph


class KGProjectorTests(unittest.TestCase):
    def setUp(self):
        base = Path.home() / ".jarvis"
        base.mkdir(mode=0o700, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="kg-projector-test-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mind = MindStore(self.root)
        self.kg = KnowledgeGraph(self.root)

    def append(self, category: str = "thought", payload: dict[str, Any] | None = None):
        return self.mind.append(
            category, {"text": "considered"} if payload is None else payload, source="test"
        )

    def projector(self, *, batch_limit: int = 200):
        return KGProjector(self.root, self.mind, self.kg, batch_limit=batch_limit)

    def nodes(self):
        return self.kg.get_all_nodes()

    def state(self):
        return json.loads((self.root / "kg_projector_state.json").read_text())

    def test_every_explicit_mapping_uses_supported_graph_schema_and_provenance(self):
        self.assertFalse(set(CATEGORY_MAP) & SKIP_CATEGORIES)
        for category, rule in CATEGORY_MAP.items():
            self.assertIn(rule.node_type, NODE_TYPES)
            self.assertIn(rule.kg_category, KG_CATEGORIES)
            self.append(category, {"text": "consideration", "status": "pending"})
        result = self.projector().tick()
        self.assertEqual(result["projected"], len(CATEGORY_MAP))
        self.assertEqual(len(self.nodes()), len(CATEGORY_MAP))
        for node in self.nodes():
            self.assertEqual(node["origin_table"], "mind.entries")
            self.assertEqual(node["evidence_ids"], [node["origin_id"]])
            self.assertLessEqual(len(node["value"]), 500)
            self.assertNotIn("payload", node["metadata"])

    def test_required_features_are_mapped_to_their_domains(self):
        expected = {
            "health_alert": ("health_event", "health"),
            "health_daily_report": ("health_event", "health"),
            "thought": ("observation", "jarvis_self"),
            "tool_call": ("observation", "tool"),
            "goal_set": ("observation", "goal"),
            "goal_progress": ("observation", "goal"),
            "goal_completed": ("observation", "goal"),
            "evolution_proposal": ("observation", "evolution"),
            "evolution_cycle": ("observation", "evolution"),
            "self_evaluation": ("observation", "evolution"),
            "vision_input": ("observation", "vision"),
            "core_mission": ("observation", "mission"),
        }
        for category in expected:
            self.append(category, {"text": "short", "goal": "objective", "key": "mission"})
        self.projector().tick()
        by_category = {node["metadata"]["mind_category"]: node for node in self.nodes()}
        for category, (node_type, domain) in expected.items():
            self.assertEqual(by_category[category]["node_type"], node_type)
            self.assertEqual(by_category[category]["category"], domain)

    def test_idempotent_replay_preserves_full_node_snapshot(self):
        self.append()
        self.append("health_alert", {"metric": "spo2", "severity": "critical"})
        p = self.projector()
        self.assertEqual(p.tick()["projected"], 2)
        original = self.nodes()
        self.assertEqual(p.tick()["processed"], 0)
        # Simulate lost cursor after committed KG writes, not a source rewrite.
        (self.root / "kg_projector_state.json").unlink()
        self.assertEqual(self.projector().tick()["projected"], 2)
        self.assertEqual(self.nodes(), original)
        self.assertEqual(self.state()["projected"], 2)

    def test_restart_resumes_and_new_append_is_projected_once(self):
        for _ in range(5):
            self.append()
        self.assertEqual(self.projector(batch_limit=2).tick()["processed"], 2)
        self.assertEqual(self.state()["cursor"], 2)
        restarted = self.projector(batch_limit=2)
        self.assertEqual(restarted.tick()["processed"], 2)
        self.assertEqual(restarted.tick()["processed"], 1)
        self.assertEqual(restarted.tick()["processed"], 0)
        self.append()
        self.assertEqual(restarted.tick()["projected"], 1)
        self.assertEqual(len(self.nodes()), 6)
        evaluation = self.projector().self_evaluate()
        self.assertEqual(evaluation["projected"], 6)
        self.assertEqual(evaluation["cursor"], 6)

    def test_batch_limit_counts_skipped_and_unknown_rows_too(self):
        for cat in ("recall", "thought", "future_category", "thought", "thought"):
            self.append(cat)
        p = self.projector(batch_limit=3)
        result = p.tick()
        self.assertEqual(result["processed"], 3)
        self.assertEqual(result["projected"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["unmapped"], 1)
        self.assertEqual(result["cursor"], 3)
        self.assertEqual(len(self.nodes()), 1)
        self.assertEqual(p.tick()["projected"], 2)

    def test_skip_categories_do_not_decode_payload_or_enter_graph(self):
        for category in sorted(SKIP_CATEGORIES):
            self.append(category, {"private": "not copied"})
        with patch("kg_projector.json.loads", side_effect=AssertionError("decoded skipped row")):
            result = self.projector().tick()
        self.assertEqual(result["skipped"], len(SKIP_CATEGORIES))
        self.assertEqual(self.nodes(), [])
        self.assertEqual(self.projector().self_evaluate()["skipped"], len(SKIP_CATEGORIES))

    def test_unknown_categories_are_durable_and_self_evaluation_demands_mapping(self):
        self.append("future_sensor")
        self.append("future_sensor")
        self.append("new_analyzer")
        self.projector().tick()
        evaluation = self.projector().self_evaluate()
        self.assertEqual(evaluation["unmapped_categories"], {"future_sensor": 2, "new_analyzer": 1})
        self.assertEqual(evaluation["unmapped"], 3)
        self.assertTrue(evaluation["mapping_required"])
        self.assertIn("future_sensor", " ".join(evaluation["known_limitations"]))
        self.assertTrue(evaluation["improvement_directions"])
        self.assertEqual(self.nodes(), [])

    def test_sense_event_kind_and_source_override_generic_tool_wrapper(self):
        for source, kind, _domain in (
            ("health", "heart_rate", "health"),
            ("location", "location", "location"),
            ("activitywatch", "app_usage", "sense"),
            ("browser", "page_visit", "sense"),
            ("speech", "speech", "sense"),
            ("camera", "camera", "vision"),
        ):
            self.append(
                "event",
                {
                    "kind": "tool_result",
                    "data": {
                        "sense_event": {
                            "source": source,
                            "kind": kind,
                            "observed_at": "2026-10-01T00:00:00+00:00",
                            "payload": {"metric": "heart_rate", "value": 70, "private": "no copy"},
                        }
                    },
                },
            )
        self.projector().tick()
        for node in self.nodes():
            self.assertEqual(node["node_type"], "observation")
            kind = node["metadata"]["sense_kind"]
            expected = {"heart_rate": "health", "location": "location", "camera": "vision"}
            self.assertEqual(node["category"], expected.get(kind, "sense"))
            self.assertEqual(node["key"], "sense:" + kind)
            self.assertEqual(node["metadata"]["event_time"], "2026-10-01T00:00:00+00:00")
            self.assertNotIn("no copy", json.dumps(node))

    def test_summaries_are_bounded_and_exclude_private_structures(self):
        secret = "NEVER-COPY-PRIVATE-RAW"
        entries = [
            ("thought", {"text": "思考" * 700, "private": secret}),
            (
                "tool_call",
                {
                    "tool_name": "search",
                    "status": "unknown",
                    "args": secret,
                    "result": {"content": secret},
                    "duration_ms": 20,
                },
            ),
            (
                "vision_input",
                {"description": "screen description", "image_path": secret, "data_url": secret},
            ),
            ("event", {"kind": "conversation", "data": {"role": "user", "text": secret}}),
            ("goal_progress", {"goal_id": "g-1", "progress": 0.5, "private": secret}),
        ]
        for category, payload in entries:
            self.append(category, payload)
        self.projector().tick()
        for node in self.nodes():
            self.assertLessEqual(len(node["value"]), 500)
            self.assertNotIn(secret, json.dumps(node))
            self.assertNotEqual(node["node_type"], "fact")
        self.assertTrue(any("progress=0.5" in n["value"] for n in self.nodes()))

    def test_projector_does_not_create_edges_or_call_unbounded_auto_link(self):
        self.append("event", {"kind": "time"})
        self.append("health_alert", {"metric": "heart_rate", "severity": "critical"})
        with patch.object(self.kg, "auto_link", side_effect=AssertionError("unbounded")):
            self.projector().tick()
        self.assertEqual(self.kg.get_all_edges(), [])

    def test_kg_write_failure_does_not_advance_cursor_or_committed_counters(self):
        self.append()
        self.projector().tick()
        prior = self.state()
        self.append()
        self.append()
        real = self.kg.add_node
        attempts = 0

        def fail_second(*args: Any, **kwargs: Any):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise sqlite3.OperationalError("injected write failure")
            return real(*args, **kwargs)

        p = self.projector()
        with patch.object(self.kg, "add_node", side_effect=fail_second):
            with self.assertRaises(sqlite3.OperationalError):
                p.tick()
        self.assertEqual(self.state(), prior)
        self.assertEqual(len(self.nodes()), 2)
        self.assertEqual(p.self_evaluate()["last_error_type"], "OperationalError")
        self.assertEqual(p.tick()["projected"], 2)
        self.assertEqual(len(self.nodes()), 3)
        self.assertEqual(self.state()["projected"], 3)

    def test_atomic_state_failure_retries_without_rewriting_committed_nodes(self):
        self.append()
        p = self.projector()
        with patch("kg_projector.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                p.tick()
        self.assertFalse((self.root / "kg_projector_state.json").exists())
        before = self.nodes()
        self.assertEqual(p.tick()["projected"], 1)
        self.assertEqual(self.nodes(), before)
        self.assertEqual(self.state()["cursor"], 1)
        self.assertEqual(list(self.root.glob(".kg-projector-*.tmp")), [])

    def test_evolution_summaries_match_actual_producer_shapes(self):
        payloads: dict[str, dict[str, Any]] = {
            "evolution_proposal": {
                "topic": "graph retrieval",
                "summary": "Use indexed lookup",
                "skill_name": "lookup-helper",
                "estimated_value": 0.8,
                "requires_human_approval": True,
            },
            "evolution_review": {
                "review": {
                    "useful": True,
                    "novel": False,
                    "rationale": "Already available",
                    "estimated_value": 0.3,
                },
                "evidence": {"topic": "graph retrieval", "raw": "DO NOT COPY"},
            },
            "evolution_research_session": {
                "topics": ["graph retrieval"],
                "domain_modules": {},
                "proposal_ids": ["p1"],
                "errors": [],
            },
            "evolution_weekly_completed": {"week": "2026-10-19", "proposal_ids": ["p1"]},
            "evolution_research_error": {"stage": "review", "error_type": "ValueError"},
        }
        for category, payload in payloads.items():
            self.append(category, payload)
        self.projector().tick()
        values = {node["metadata"]["mind_category"]: node["value"] for node in self.nodes()}
        self.assertIn("Use indexed lookup", values["evolution_proposal"])
        self.assertIn("Already available", values["evolution_review"])
        self.assertIn("proposal_ids.count=1", values["evolution_research_session"])
        self.assertIn("topics.count=1", values["evolution_research_session"])
        self.assertIn("week=2026-10-19", values["evolution_weekly_completed"])
        self.assertIn("stage=review", values["evolution_research_error"])
        self.assertNotIn("DO NOT COPY", json.dumps(self.nodes()))

    def test_post_replace_directory_fsync_failure_reports_committed_warning(self):
        self.append()
        p = self.projector()
        real_fsync = os.fsync
        calls = 0

        def fail_directory(fd: int):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("directory flush failed")
            return real_fsync(fd)

        with patch("kg_projector.os.fsync", side_effect=fail_directory):
            result = p.tick()
        self.assertEqual(result["cursor"], 1)
        self.assertEqual(result["durability_warning"], "OSError")
        self.assertEqual(self.state()["cursor"], 1)
        self.assertEqual(p.self_evaluate()["last_durability_error_type"], "OSError")
        self.assertEqual(p.tick()["processed"], 0)
        self.assertEqual(len(self.nodes()), 1)

    def test_state_is_private_and_corruption_fails_closed(self):
        self.append()
        p = self.projector()
        p.tick()
        state = self.root / "kg_projector_state.json"
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        state.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.projector().tick()
        self.assertEqual(len(self.nodes()), 1)

    def test_state_symlink_and_hardlink_rejected(self):
        target = self.root / "other.json"
        target.write_text("{}", encoding="utf-8")
        target.chmod(0o600)
        state = self.root / "kg_projector_state.json"
        state.symlink_to(target)
        with self.assertRaises(ValueError):
            self.projector().tick()
        state.unlink()
        os.link(target, state)
        with self.assertRaises(ValueError):
            self.projector().tick()

    def test_invalid_limit_root_mismatch_and_nonprivate_root_rejected(self):
        invalid: list[Any] = [0, -1, True, 1.5, "2"]
        for value in invalid:
            with self.assertRaises(ValueError):
                self.projector(batch_limit=value)
        other = self.root / "other"
        other.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            KGProjector(other, self.mind, self.kg)
        self.root.chmod(0o755)
        self.addCleanup(self.root.chmod, 0o700)
        with self.assertRaises(ValueError):
            self.projector()


if __name__ == "__main__":
    unittest.main()
