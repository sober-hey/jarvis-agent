"""Legacy KG acceptance, updated for the already-established temporal rule.

Derived from scratch/kg-development/test_knowledge_graph.py; its archive is
unchanged. Only three obsolete temporal assertions were adjusted: automatic
caused_by -> precedes, temporal description, and matching manual takeover type.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path


class KnowledgeGraphTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec("knowledge_graph"), "knowledge_graph module missing"
        )
        from knowledge_graph import KnowledgeGraph

        base = Path("/Users/sober/.jarvis")
        base.mkdir(mode=0o700, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="kg-tests-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kg = KnowledgeGraph(self.root)

    def node(self, key, **kwargs):
        values = dict(
            node_type="fact", category="event", key=key, value=key, confidence=0.8, source="manual"
        )
        values.update(kwargs)
        return self.kg.add_node(**values)

    def test_persistent_nodes_have_json_and_private_database(self):
        from knowledge_graph import KnowledgeGraph

        a = self.node(
            "name",
            category="identity",
            value="董鑂",
            evidence_ids=["e1", "e1"],
            metadata={"nested": {"flag": True}},
        )
        row = KnowledgeGraph(self.root).get_node(a)
        self.assertEqual(row["value"], "董鑂")
        self.assertEqual(row["evidence_ids"], ["e1"])
        self.assertEqual(row["metadata"], {"nested": {"flag": True}})
        self.assertEqual((self.root / "knowledge_graph.db").stat().st_mode & 0o777, 0o600)
        self.assertIsNone(self.kg.get_node("missing"))
        self.assertEqual(len(self.kg.get_all_nodes()), 1)
        self.assertEqual(self.kg.get_all_edges(), [])

    def test_node_origin_is_idempotent_and_updates(self):
        a = self.node("name", origin_table="manual", origin_id="origin1", value="before")
        b = self.node("name", origin_table="manual", origin_id="origin1", value="after")
        self.assertEqual(a, b)
        self.assertEqual(len(self.kg.get_all_nodes()), 1)
        self.assertEqual(self.kg.get_node(a)["value"], "after")

    def test_edges_validate_types_confidence_endpoints_and_deduplicate(self):
        a, b = self.node("a"), self.node("b")
        x = self.kg.add_edge(a, b, "caused_by", 0.6, ["e1"], "candidate")
        self.assertEqual(x, self.kg.add_edge(a, b, "caused_by", 0.6, ["e1"], "candidate"))
        self.assertEqual(len(self.kg.get_all_edges()), 1)
        self.assertEqual(self.kg.get_all_edges()[0]["evidence_ids"], ["e1"])
        for bad in (float("nan"), float("inf"), -0.1, 1.1, True):
            with self.assertRaises(ValueError):
                self.node("bad", confidence=bad)
        with self.assertRaises(ValueError):
            self.node("bad", node_type="person")
        with self.assertRaises(ValueError):
            self.node("bad", category="unknown")
        with self.assertRaises(ValueError):
            self.kg.add_edge(a, b, "guesses", 0.5)
        with self.assertRaises(KeyError):
            self.kg.add_edge(a, "missing", "supports", 0.5)
        with self.assertRaises(ValueError):
            self.kg.add_edge(a, a, "supports", 0.5)
        with self.assertRaises(ValueError):
            self.node("bad", evidence_ids="e1")
        with self.assertRaises(ValueError):
            self.node("bad", metadata={"x": float("nan")})

    def test_keyword_query_and_bfs_respect_depth_and_cycles(self):
        a = self.node("late_night", category="habit", value="习惯凌晨3点后使用电脑")
        b, c, d = self.node("心率偏高"), self.node("third"), self.node("fourth")
        self.kg.add_edge(a, b, "caused_by", 0.6)
        self.kg.add_edge(c, b, "supports", 0.8)
        self.kg.add_edge(c, a, "correlates_with", 0.4)
        self.kg.add_edge(c, d, "precedes", 0.5)
        self.assertEqual(self.kg.query_related(a, 0)["neighbors"], [])
        self.assertEqual({n["id"] for n in self.kg.query_related(a, 1)["neighbors"]}, {b, c})
        self.assertEqual({n["id"] for n in self.kg.query_related(a, 2)["neighbors"]}, {b, c, d})
        self.assertEqual(len(self.kg.query_related(a, 2)["edges"]), 4)
        matches = self.kg.query("凌晨")
        self.assertEqual(matches[0]["id"], a)
        self.assertEqual({n["id"] for n in matches[0]["neighbors"]}, {b, c})
        self.assertEqual(self.kg.query("late_night")[0]["id"], a)
        self.assertTrue(self.kg.query("我凌晨使用电脑吗？"))
        self.assertEqual(self.kg.query(""), [])
        self.assertEqual(self.kg.query("%"), [])
        self.assertEqual(self.kg.query_related("absent")["nodes"], [])
        with self.assertRaises(ValueError):
            self.kg.query_related(a, -1)

    def test_causal_chain_is_directed_cycle_safe_and_not_correlation(self):
        a = self.node("a", node_type="behavior_pattern", category="habit")
        b = self.node("b", node_type="health_event", category="health")
        c = self.node("c", category="work")
        self.kg.add_edge(a, b, "caused_by", 0.6)
        self.kg.add_edge(b, c, "evolves_to", 0.8)
        self.kg.add_edge(c, a, "correlates_with", 0.9)
        paths = self.kg.find_causal_chain("habit", "work")
        self.assertEqual([n["id"] for n in paths[0]["nodes"]], [a, b, c])
        self.assertEqual(self.kg.find_causal_chain("work", "habit"), [])
        self.assertTrue(self.kg.find_causal_chain("behavior_pattern", "health_event"))

    def test_export_dot_escapes_labels_and_includes_edges(self):
        a = self.node("a", value='quote " newline\n反斜杠\\')
        b = self.node("b")
        self.kg.add_edge(a, b, "supports", 0.7, description='a " b')
        dot = self.kg.export_dot()
        self.assertTrue(dot.startswith("digraph"))
        self.assertIn("supports", dot)
        self.assertIn('\\"', dot)
        self.assertIn("\\n", dot)
        self.assertIn("->", dot)

    def test_sync_real_profile_and_hypothesis_lifecycle_without_source_writes(self):
        import hashlib

        from hypothesis_engine import HypothesisEngine
        from sir_profile import SirProfile

        sp = SirProfile(self.root / "sources")
        sp.upsert("identity", "name", "董鑂", 1, "self_report", ["e1"])
        he = HypothesisEngine(self.root / "sources")
        pending = he.propose("habit", "late_night", "经常熬夜", 0.7, "three entries", "opposed")
        confirmed = he.propose("preference", "tea", "喜欢茶", 0.9, "support", "opposed", ["e2"])
        self.assertTrue(he.try_confirm(confirmed))
        source_paths = [sp.path, he.path]
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
        self.kg.sync_from_sir_profile(sp)
        self.kg.sync_from_hypotheses(he)
        self.kg.sync_from_sir_profile(sp)
        self.kg.sync_from_hypotheses(he)
        nodes = self.kg.get_all_nodes()
        self.assertEqual(len(nodes), 4)
        h = next(n for n in nodes if n["origin_id"] == confirmed)
        self.assertEqual(h["metadata"]["status"], "confirmed")
        self.kg.auto_link()
        edges = self.kg.get_all_edges()
        evolves = [e for e in edges if e["edge_type"] == "evolves_to"]
        self.assertEqual(len(evolves), 1)
        self.assertEqual(evolves[0]["from_node"], h["id"])
        self.assertEqual(
            before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
        )
        original_ids = {n["origin_id"]: n["id"] for n in nodes}
        sp.upsert("identity", "name", "董鑂", 1, "self_report", ["e3"])
        he.update_evidence(pending, supporting_ids=["e4"])
        self.kg.sync_from_sir_profile(sp)
        self.kg.sync_from_hypotheses(he)
        self.assertEqual(original_ids, {n["origin_id"]: n["id"] for n in self.kg.get_all_nodes()})
        self.assertEqual(
            next(n for n in self.kg.get_all_nodes() if n["key"] == "name")["evidence_ids"],
            ["e1", "e3"],
        )
        he.update_evidence(confirmed, opposing_ids=["oppose"])
        self.kg.sync_from_hypotheses(he)
        self.kg.auto_link()
        self.assertFalse([e for e in self.kg.get_all_edges() if e["edge_type"] == "evolves_to"])

    def test_auto_link_overlap_timing_idempotence_and_manual_preservation(self):
        a = self.node(
            "a", category="habit", evidence_ids=["e1", "e2"], origin_table="manual", origin_id="a"
        )
        b = self.node("b", category="habit", evidence_ids=["e2", "e3"])
        c = self.node("a", category="habit", evidence_ids=["e1"])
        behavior = self.node(
            "late_night",
            node_type="behavior_pattern",
            category="habit",
            metadata={"event_time": "2026-10-19T01:00:00+08:00"},
            evidence_ids=["eb"],
        )
        health = self.node(
            "high_hr",
            node_type="health_event",
            category="health",
            metadata={"event_time": "2026-10-19T02:00:00+08:00"},
            evidence_ids=["eh"],
        )
        distant = self.node(
            "far",
            node_type="health_event",
            category="health",
            metadata={"event_time": "2026-10-20T02:00:00+08:00"},
        )
        self.kg.auto_link()
        edges = self.kg.get_all_edges()
        correlated = [e for e in edges if e["edge_type"] == "correlates_with"]
        self.assertTrue(
            any(
                set((e["from_node"], e["to_node"])) == {a, b} and e["evidence_ids"] == ["e2"]
                for e in correlated
            )
        )
        self.assertFalse(any(set((e["from_node"], e["to_node"])) == {a, c} for e in correlated))
        causal = [e for e in edges if e["edge_type"] == "precedes"]
        self.assertEqual(len(causal), 1)
        self.assertEqual((causal[0]["from_node"], causal[0]["to_node"]), (behavior, health))
        self.assertLessEqual(causal[0]["confidence"], 0.3)
        self.assertIn("非因果", causal[0]["description"])
        self.assertNotIn(distant, [e["to_node"] for e in causal])
        self.kg.auto_link()
        self.assertEqual(edges, self.kg.get_all_edges())
        manual = self.kg.add_edge(behavior, health, "precedes", 0.75, ["confirmed"], "manual")
        self.node("a", category="habit", evidence_ids=[], origin_table="manual", origin_id="a")
        self.kg.auto_link()
        self.assertTrue(
            any(e["id"] == manual and e["confidence"] == 0.75 for e in self.kg.get_all_edges())
        )
        self.assertFalse(
            any(set((e["from_node"], e["to_node"])) == {a, b} for e in self.kg.get_all_edges())
        )

    def test_manual_hypothesis_with_unspecified_status_can_correlate(self):
        a = self.node("hyp", node_type="hypothesis", category="habit", evidence_ids=["e1"])
        b = self.node(
            "pattern", node_type="behavior_pattern", category="habit", evidence_ids=["e1"]
        )
        self.kg.auto_link()
        self.assertEqual(len(self.kg.get_all_edges()), 1)
        self.assertEqual(
            set((self.kg.get_all_edges()[0]["from_node"], self.kg.get_all_edges()[0]["to_node"])),
            {a, b},
        )

    def test_opposing_evidence_never_becomes_shared_support(self):
        from hypothesis_engine import HypothesisEngine

        he = HypothesisEngine(self.root / "sources")
        pending = he.propose("habit", "h", "claim", 0.5, "support", "opposed", ["yes"])
        he.update_evidence(pending, opposing_ids=["no"])
        self.kg.sync_from_hypotheses(he)
        self.node("f", category="habit", evidence_ids=["no"])
        self.kg.auto_link()
        self.assertEqual(self.kg.get_all_edges(), [])

    def test_all_types_and_unsafe_database_paths(self):
        from knowledge_graph import EDGE_TYPES, NODE_TYPES, KnowledgeGraph

        for kind in NODE_TYPES:
            self.node(kind, node_type=kind)
        a, b = self.node("a"), self.node("b")
        for kind in EDGE_TYPES:
            self.kg.add_edge(a, b, kind, 0.5)
        self.assertEqual(len(self.kg.get_all_edges()), 6)
        path = self.root / "knowledge_graph.db"
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            KnowledgeGraph(self.root)
        path.chmod(0o600)
        linkroot = self.root / "unsafe"
        linkroot.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            KnowledgeGraph(linkroot)

    def test_timing_fallback_invalid_and_future_behavior(self):
        behavior = self.node("late", node_type="behavior_pattern", category="habit")
        event = self.node("heart", node_type="health_event", category="health")
        self.kg.auto_link()
        edge = self.kg.get_all_edges()[0]
        self.assertEqual((edge["from_node"], edge["to_node"]), (behavior, event))
        self.assertLessEqual(edge["confidence"], 0.1)
        self.assertIn("创建时间", edge["description"])
        self.node(
            "future",
            node_type="behavior_pattern",
            category="habit",
            metadata={"event_time": "2099-01-01T01:00:00+00:00"},
        )
        self.node(
            "invalid", node_type="health_event", category="health", metadata={"event_time": "bad"}
        )
        self.node(
            "naive",
            node_type="health_event",
            category="health",
            metadata={"event_time": "2026-10-19T01:00:00"},
        )
        self.kg.auto_link()
        self.assertEqual(self.kg.get_all_edges(), [edge])


if __name__ == "__main__":
    unittest.main(verbosity=2)
