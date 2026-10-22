"""Isolated SQLite concurrency/category regressions for KnowledgeGraph."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

from knowledge_graph import KnowledgeGraph
from sir_profile import SirProfile


class KnowledgeGraphWalTests(unittest.TestCase):
    def setUp(self) -> None:
        # Product roots reject host scratch; all fixtures are disposable and private.
        parent = Path.home() / ".jarvis"
        parent.mkdir(mode=0o700, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="kg-wal-test-", dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.graph = KnowledgeGraph(self.root)

    def node(self, key: str = "fixture", **kwargs: Any) -> str:
        values: dict[str, Any] = dict(
            node_type="fact", category="event", key=key, value=key, confidence=0.8, source="fixture"
        )
        values.update(kwargs)
        return self.graph.add_node(**values)

    def test_connections_use_wal_and_five_second_busy_timeout(self) -> None:
        for write in (False, True):
            with self.graph.connection(write=write) as db:
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
                self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_kg_has_its_own_controlled_category_allowlist(self) -> None:
        categories = {
            "event",
            "family",
            "habit",
            "health",
            "identity",
            "location",
            "preference",
            "work",
            "jarvis_self",
            "mission",
            "tool",
            "skill",
            "evolution",
            "goal",
            "task",
            "device",
            "vision",
            "sense",
            "model",
            "external",
            "persona",
            "affect",
            "observability",
        }
        for category in sorted(categories):
            with self.subTest(category=category):
                identity = self.node(category=category)
                node = self.graph.get_node(identity)
                assert node is not None
                self.assertEqual(node["category"], category)
        from knowledge_graph import KG_CATEGORIES

        self.assertEqual(KG_CATEGORIES, categories)

    def test_unknown_and_nontext_categories_and_blank_keys_still_rejected(self) -> None:
        for category in ("unknown", "Mission", "mission ", "", None, [], 3):
            with self.subTest(category=category), self.assertRaises(ValueError):
                self.node(category=category)
        for key in ("", " ", None):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.node(key=key, category="mission")
        self.assertEqual(self.graph.get_all_nodes(), [])

    def test_sir_profile_write_through_retains_original_validation(self) -> None:
        profile = SirProfile(self.root, knowledge_graph=self.graph)
        for category in ("mission", "jarvis_self", "device"):
            with self.assertRaises(ValueError):
                profile.upsert(category, "fixture", "fixture", 0.9, "self_report", ["fixture-e"])
        fact = profile.upsert("identity", "fixture", "fixture", 0.9, "self_report", ["fixture-e"])
        nodes = self.graph.get_all_nodes()
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["origin_id"], fact["id"])
        self.assertEqual(nodes[0]["evidence_ids"], ["fixture-e"])
        # A claimed Sir ledger origin must not bypass Sir's category boundary.
        with self.assertRaises(ValueError):
            self.node(category="mission", origin_table="sir_profile", origin_id="bad")

    def test_mission_migration_is_targeted_idempotent_and_preserves_every_other_field(self) -> None:
        identity = self.node(
            "jarvis_self_dev_goal",
            category="identity",
            evidence_ids=["fixture-e"],
            metadata={"fixture": True},
        )
        other_key = self.node("other", category="identity")
        other_category = self.node("jarvis_self_dev_goal", category="work")
        already_mission = self.node("jarvis_self_dev_goal", category="mission")
        self.graph.add_edge(identity, other_key, "supports", 0.7, ["fixture-e"])
        before = {n["id"]: n for n in self.graph.get_all_nodes()}
        before_edges = self.graph.get_all_edges()
        self.assertEqual(self.graph.migrate_jarvis_self_dev_goal(), 1)
        self.assertEqual(self.graph.migrate_jarvis_self_dev_goal(), 0)
        expected = {**before[identity], "category": "mission"}
        self.assertEqual(self.graph.get_node(identity), expected)
        for unchanged in (other_key, other_category, already_mission):
            self.assertEqual(self.graph.get_node(unchanged), before[unchanged])
        self.assertEqual(len(self.graph.get_all_nodes()), len(before))
        self.assertEqual(self.graph.get_all_edges(), before_edges)

    def test_eight_threads_write_two_hundred_nodes_without_errors(self) -> None:
        barrier = threading.Barrier(8)

        def write_batch(worker: int) -> list[str]:
            barrier.wait(timeout=10)
            return [self.node(f"worker-{worker}-{item}") for item in range(25)]

        with ThreadPoolExecutor(max_workers=8) as executor:
            identities = [node for batch in executor.map(write_batch, range(8)) for node in batch]
        self.assertEqual(len(set(identities)), 200)
        self.assertEqual(len(self.graph.get_all_nodes()), 200)

    def test_write_lock_is_reentrant_and_covers_commit_and_exception_release(self) -> None:
        lock = self.graph._write_lock
        self.assertTrue(lock.acquire(timeout=1))
        try:
            self.node("reentrant")
        finally:
            lock.release()
        entered = threading.Event()
        attempt = threading.Event()

        def contender() -> None:
            attempt.set()
            with self.graph.connection(write=True):
                entered.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.graph.connection(write=True):
                future = executor.submit(contender)
                self.assertTrue(attempt.wait(5))
                self.assertFalse(entered.wait(0.05))
            future.result(timeout=5)
        self.assertTrue(entered.is_set())
        with self.assertRaisesRegex(RuntimeError, "rollback fixture"):
            with self.graph.connection(write=True) as db:
                db.execute("DELETE FROM nodes")
                raise RuntimeError("rollback fixture")
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(self.node, "after rollback").result(timeout=5)
        self.assertEqual(len(self.graph.get_all_nodes()), 2)

    def test_separate_instances_use_sqlite_lock_and_wal_readers_do_not_block(self) -> None:
        other = KnowledgeGraph(self.root)
        entered = threading.Event()
        attempt = threading.Event()

        def writer() -> str:
            attempt.set()
            result = other.add_node("fact", "event", "second", "second", 0.8, "fixture")
            entered.set()
            return result

        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.graph.connection(write=True):
                future = executor.submit(writer)
                self.assertTrue(attempt.wait(5))
                self.assertFalse(entered.wait(0.05))
                self.assertEqual(other.get_all_nodes(), [])
            identity = future.result(timeout=5)
        self.assertIsNotNone(self.graph.get_node(identity))

    def test_four_processes_share_the_graph_without_python_global_lock(self) -> None:
        script = """
import sys
from pathlib import Path
from knowledge_graph import KnowledgeGraph
kg = KnowledgeGraph(Path(sys.argv[1]))
for item in range(50):
    kg.add_node('fact', 'mission', f'{sys.argv[2]}-{item}', 'fixture', .8, 'fixture')
"""
        processes: list[subprocess.Popen[str]] = []
        try:
            for worker in range(4):
                processes.append(
                    subprocess.Popen(
                        [sys.executable, "-c", script, str(self.root), str(worker)],
                        cwd=Path(__file__).parent,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            for process in processes:
                out, err = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, out + err)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
        self.assertEqual(len(self.graph.get_all_nodes()), 200)

    def test_shared_connection_policy_wraps_every_public_write_path(self) -> None:
        from hypothesis_engine import HypothesisEngine

        profile = SirProfile(self.root / "sources")
        profile.upsert("identity", "fixture", "fixture", 0.9, "self_report", ["fixture-e"])
        engine = HypothesisEngine(self.root / "sources")
        engine.propose("habit", "fixture", "fixture", 0.4, "support", "refute", ["fixture-e"])
        calls: list[int] = []
        original = self.graph._put_node

        def assert_owned(*args: Any, **kwargs: Any) -> str:
            self.assertTrue(self.graph._write_lock._is_owned())
            calls.append(1)
            return original(*args, **kwargs)

        with patch.object(self.graph, "_put_node", side_effect=assert_owned):
            self.node("manual")
            self.graph.sync_from_sir_profile(profile)
            self.graph.sync_from_hypotheses(engine)
        self.assertEqual(len(calls), 3)
        with patch.object(self.graph, "_put_edge", wraps=self.graph._put_edge) as edge:
            nodes = self.graph.get_all_nodes()
            self.graph.add_edge(nodes[0]["id"], nodes[1]["id"], "supports", 0.5)
            self.assertEqual(edge.call_count, 1)
        self.graph.auto_link()
        self.graph.migrate_jarvis_self_dev_goal()

    def test_auto_link_temporal_rule_remains_precedes_not_causality(self) -> None:
        self.node(
            "behavior",
            node_type="behavior_pattern",
            category="habit",
            metadata={"event_time": "2026-10-19T01:00:00+08:00"},
        )
        self.node(
            "health",
            node_type="health_event",
            category="health",
            metadata={"event_time": "2026-10-19T02:00:00+08:00"},
        )
        self.graph.auto_link()
        edges = self.graph.get_all_edges()
        self.assertEqual([edge["edge_type"] for edge in edges], ["precedes"])
        self.assertEqual(self.graph.find_causal_chain("habit", "health"), [])
        self.assertEqual(self.graph.self_evaluate()["module"], "knowledge_graph")


if __name__ == "__main__":
    unittest.main(verbosity=2)
