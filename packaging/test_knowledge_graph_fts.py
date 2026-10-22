"""Disposable, private regression fixtures for the R1 indexed query path."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from knowledge_graph import KnowledgeGraph, _query_terms


class KnowledgeGraphFtsTests(unittest.TestCase):
    def setUp(self) -> None:
        # PrivateStore rejects the host scratch tree, including TMPDIR.
        self.temp = tempfile.TemporaryDirectory(prefix="kg-r1-test-", dir=Path.home() / ".jarvis")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kg = KnowledgeGraph(self.root)

    def node(self, value: str, **kwargs: Any) -> str:
        args: dict[str, Any] = dict(
            node_type="fact",
            category="event",
            key="fixture",
            value=value,
            confidence=0.8,
            source="fixture",
        )
        args.update(kwargs)
        return self.kg.add_node(**args)

    def ids(self, question: str) -> set[str]:
        return {n["id"] for n in self.kg.query(question, k=1000)}

    def test_default_limit_is_twenty_before_neighbor_expansion(self) -> None:
        for i in range(25):
            self.node(f"needle {i}")
        real = self.kg._one_hop
        with patch.object(self.kg, "_one_hop", wraps=real) as expand:
            self.assertEqual(len(self.kg.query("needle")), 20)
            self.assertEqual(expand.call_count, 20)
        self.assertEqual(len(self.kg.query("needle", k=3)), 3)
        self.assertEqual(len(self.kg.query("needle", k=50)), 25)

    def test_no_full_snapshot_or_python_bfs_on_query_path(self) -> None:
        identity = self.node("索引检索")
        with (
            patch.object(self.kg, "_snapshot", side_effect=AssertionError("snapshot")),
            patch.object(self.kg, "_related", side_effect=AssertionError("BFS")),
        ):
            self.assertEqual(self.ids("索引"), {identity})

    def test_query_uses_all_three_authoritative_fields_not_metadata(self) -> None:
        value = self.node("valueonly")
        key = self.node("none", key="keyonly")
        category = self.node("none", category="health")
        self.node("none", source="sourceonly", metadata={"hidden": "secretvalue"})
        self.assertEqual(self.ids("valueonly"), {value})
        self.assertEqual(self.ids("keyonly"), {key})
        self.assertEqual(self.ids("health"), {category})
        self.assertEqual(self.ids("secretvalue sourceonly"), set())

    def test_short_terms_long_han_bigrams_and_unigrams_use_side_index(self) -> None:
        rate = self.node("心率偏高")
        age = self.node("年龄")
        single = self.node("与")
        self.assertEqual(self.ids("心率"), {rate})
        self.assertIn(rate, self.ids("询问心率了吗"))
        self.assertEqual(self.ids("年龄"), {age})
        self.assertEqual(self.ids("与"), {single})
        self.assertEqual(self.ids("心高"), set())

    def test_nfkc_casefold_punctuation_and_each_trigram_is_quoted(self) -> None:
        identity = self.node("ＡＬＰＨＡ_ＢＥＴＡ，心-率")
        self.assertIn(identity, self.ids("alphabeta"))
        self.assertIn(identity, self.ids("心率"))
        self.assertEqual(_query_terms("ＡＬＰＨＡ")[1], '"alpha"')
        for question in ('"', "*()[]{}:^", "   ", "%", "_"):
            self.assertEqual(self.ids(question), set())
        # MATCH operators are treated as plain bound text, never executable syntax.
        self.node("literal NEAR marker")
        self.assertTrue(self.ids('NEAR(foo) OR "literal" -notpresent*'))
        self.assertIsNotNone(self.kg.get_node(identity))

    def test_fts_long_terms_preserve_literal_substring_and_avoid_nonce_false_hits(self) -> None:
        self.node("abc")
        self.node("bcd")
        literal = self.node("xxabcdyy")
        self.assertEqual(self.ids("abcd"), {literal})
        self.node("context revision constraint")
        self.assertEqual(self.ids("nonce_context_constraint_9f6d8b2e"), set())

    def test_induced_one_hop_includes_neighbor_edges_but_not_depth_two(self) -> None:
        root = self.node("uniqueroot")
        left, right, far = (self.node(v) for v in ("left", "right", "far"))
        e1 = self.kg.add_edge(root, left, "supports", 0.8)
        e2 = self.kg.add_edge(right, root, "precedes", 0.8)
        e3 = self.kg.add_edge(left, right, "supports", 0.8)
        self.kg.add_edge(right, far, "supports", 0.8)
        result = self.kg.query("uniqueroot")[0]
        self.assertEqual({n["id"] for n in result["neighbors"]}, {left, right})
        self.assertEqual({e["id"] for e in result["edges"]}, {e1, e2, e3})
        self.assertNotIn(far, {n["id"] for n in result["neighbors"]})

    def test_sql_triggers_cover_insert_update_delete_and_upsert(self) -> None:
        identity = self.node("beforexyz 心率", origin_table="fixture", origin_id="one")
        updated = self.node("afteruvw 血压", origin_table="fixture", origin_id="one")
        self.assertEqual(identity, updated)
        self.assertEqual(self.ids("beforexyz 心率"), set())
        self.assertEqual(self.ids("afteruvw 血压"), {identity})
        with self.kg.connection(write=True) as db:
            db.execute(
                "UPDATE nodes SET value='rawupdate 上海',key='changedkey',"
                "category='health' WHERE id=?",
                (identity,),
            )
        self.assertEqual(self.ids("afteruvw 血压"), set())
        self.assertEqual(self.ids("rawupdate 上海 health changedkey"), {identity})
        with self.kg.connection(write=True) as db:
            db.execute("DELETE FROM nodes WHERE id=?", (identity,))
        self.assertEqual(self.ids("rawupdate 上海"), set())
        with self.kg.connection() as db:
            for gram in ("上海", "上", "海"):
                self.assertIsNone(
                    db.execute(
                        "SELECT rowid FROM nodes_short WHERE nodes_short MATCH ?",
                        ('"' + gram + '"',),
                    ).fetchone()
                )
            db.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('integrity-check')")
            db.execute("INSERT INTO nodes_short(nodes_short) VALUES('integrity-check')")

    def test_index_updates_rollback_with_node_write_transaction(self) -> None:
        identity = self.node("originalxyz 心率")
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.kg.connection(write=True) as db:
                db.execute("UPDATE nodes SET value='aborteduvw 血压' WHERE id=?", (identity,))
                raise RuntimeError("abort")
        self.assertEqual(self.ids("originalxyz 心率"), {identity})
        self.assertEqual(self.ids("aborteduvw 血压"), set())

    def test_migration_backfills_legacy_rows_and_reopen_is_noop(self) -> None:
        identity = self.node("existingxyz 心率")
        before = self.kg.get_all_nodes()
        self.kg.rollback_search_indexes()
        with self.kg.connection(write=True) as db:
            db.execute("UPDATE nodes SET value='legacyuvw 血压' WHERE id=?", (identity,))
        graph = KnowledgeGraph(self.root)
        self.assertEqual({n["id"] for n in graph.query("legacyuvw 血压")}, {identity})
        migrated = graph.get_node(identity)
        assert migrated is not None
        self.assertEqual(migrated["created_at"], before[0]["created_at"])
        graph.migrate_search_indexes()
        with graph.connection(write=True) as db:
            changes = db.total_changes
            graph._migrate_search(db)
            self.assertEqual(db.total_changes, changes)
        self.assertEqual(KnowledgeGraph(self.root).get_all_nodes(), graph.get_all_nodes())

    def test_rebuild_repairs_indexes_without_changing_nodes_or_edges(self) -> None:
        root = self.node("repairxyz 心率")
        neighbor = self.node("neighbor")
        self.kg.add_edge(root, neighbor, "supports", 0.8)
        before = self.kg.get_all_nodes(), self.kg.get_all_edges()
        with self.kg.connection(write=True) as db:
            db.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('delete-all')")
            db.execute("INSERT INTO nodes_short(nodes_short) VALUES('delete-all')")
        self.assertEqual(self.ids("repairxyz 心率"), set())
        for _ in range(2):
            self.kg.migrate_search_indexes(rebuild=True)
            self.assertEqual(self.ids("repairxyz 心率"), {root})
            self.assertEqual((self.kg.get_all_nodes(), self.kg.get_all_edges()), before)

    def test_schema_rollback_is_idempotent_and_preserves_authoritative_tables(self) -> None:
        a, b = self.node("root"), self.node("neighbor")
        self.kg.add_edge(a, b, "supports", 0.8)
        before = self.kg.get_all_nodes(), self.kg.get_all_edges()
        self.kg.rollback_search_indexes()
        self.kg.rollback_search_indexes()
        self.assertEqual((self.kg.get_all_nodes(), self.kg.get_all_edges()), before)
        with self.kg.connection() as db:
            self.assertFalse(
                list(
                    db.execute(
                        "SELECT name FROM sqlite_master WHERE name LIKE 'nodes_fts%' "
                        "OR name LIKE 'nodes_bigram%' OR name LIKE 'nodes_short%' "
                        "OR name LIKE 'nodes_search_%'"
                    )
                )
            )
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.kg.migrate_search_indexes()
        self.assertEqual(self.ids("root"), {a})

    def test_failed_migration_rolls_back_ddl_and_backfill_atomically(self) -> None:
        identity = self.node("existingxyz")
        self.kg.rollback_search_indexes()
        with self.assertRaisesRegex(sqlite3.OperationalError, "user-defined function"):
            with self.kg.connection(write=True) as db:

                def fail(_value: str) -> str:
                    raise ValueError("backfill failed")

                db.create_function("kg_search_text", 1, fail)
                self.kg._migrate_search(db)
        with self.kg.connection() as db:
            self.assertFalse(
                list(
                    db.execute(
                        "SELECT name FROM sqlite_master WHERE name IN "
                        "('nodes_fts','nodes_short','nodes_bigram')"
                    )
                )
            )
        self.assertIsNotNone(self.kg.get_node(identity))
        self.kg.migrate_search_indexes()
        self.assertEqual(self.ids("existingxyz"), {identity})

    def test_query_plan_uses_fts_covering_gram_and_both_edge_directions(self) -> None:
        with self.kg.connection() as db:
            plans = [
                r[3]
                for r in db.execute(
                    "EXPLAIN QUERY PLAN SELECT rowid FROM nodes_short WHERE nodes_short MATCH ?",
                    ('"心率"',),
                )
            ]
            self.assertTrue(any("VIRTUAL TABLE INDEX" in p and "M" in p for p in plans), plans)
            plans = [
                r[3]
                for r in db.execute(
                    "EXPLAIN QUERY PLAN SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ?",
                    ('"abc"',),
                )
            ]
            self.assertTrue(any("VIRTUAL TABLE INDEX" in p and "M" in p for p in plans), plans)
            for sql, index in (("from_node", "sqlite_autoindex_edges"), ("to_node", "edges_to")):
                plans = [
                    r[3]
                    for r in db.execute(
                        f"EXPLAIN QUERY PLAN SELECT * FROM edges WHERE {sql}=?", ("fixture",)
                    )
                ]
                self.assertTrue(any("SEARCH" in p and index in p for p in plans), plans)

    def test_failed_rebuild_restores_previous_index_and_rows(self) -> None:
        identity = self.node("originalxyz 心率")
        with self.assertRaisesRegex(sqlite3.OperationalError, "user-defined function"):
            with self.kg.connection(write=True) as db:

                def fail(_value: str) -> str:
                    raise ValueError("backfill failed")

                db.create_function("kg_search_text", 1, fail)
                self.kg._migrate_search(db, rebuild=True)
        self.assertEqual(self.ids("originalxyz 心率"), {identity})

    def test_id_changes_remove_old_side_index_references(self) -> None:
        identity = self.node("originalxyz 心率")
        with self.kg.connection(write=True) as db:
            db.execute("UPDATE nodes SET id='changed-id' WHERE id=?", (identity,))
        self.assertEqual(self.ids("originalxyz 心率"), {"changed-id"})
        with self.kg.connection() as db:
            # nodes_short is keyed by nodes.rowid, so an id change cannot leave
            # a dangling id reference; the index must still be consistent.
            db.execute("INSERT INTO nodes_short(nodes_short) VALUES('integrity-check')")
            self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])
        self.assertEqual({n["id"] for n in self.kg.query("心率")}, {"changed-id"})

    def test_legacy_bigram_file_migrates_to_compact_short_index(self) -> None:
        ids = [self.node(v) for v in ("心率偏高 上海", "血压 北京", "与的 x")]
        expected = {q: self.ids(q) for q in ("心率", "上海", "血压", "与", "的", "x")}
        # Recreate the former R1 layout: nodes_bigram table plus its triggers.
        self.kg.rollback_search_indexes()
        with self.kg.connection(write=True) as db:
            db.execute("""CREATE TABLE nodes_bigram (gram TEXT NOT NULL,
                node_id TEXT NOT NULL REFERENCES nodes(id),
                PRIMARY KEY(gram,node_id)) WITHOUT ROWID""")
            db.execute("""INSERT INTO nodes_bigram SELECT j.value,n.id FROM nodes n,
                json_each(kg_short_grams(n.value,n.category,n.key)) j""")
            db.execute(
                "CREATE TRIGGER nodes_search_insert AFTER INSERT ON nodes BEGIN "
                "INSERT INTO nodes_bigram SELECT j.value,NEW.id FROM "
                "json_each(kg_short_grams(NEW.value,NEW.category,NEW.key)) j; END"
            )
        graph = KnowledgeGraph(self.root)
        with graph.connection() as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
            self.assertNotIn("nodes_bigram", names)
            self.assertIn("nodes_short", names)
            sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='nodes_search_insert'"
            ).fetchone()[0]
            self.assertNotIn("nodes_bigram", sql)
        self.assertEqual({q: {n["id"] for n in graph.query(q)} for q in expected}, expected)
        later = graph.add_node("fact", "health", "k", "心率稳定", 0.9, "test", [])
        self.assertIn(later, {n["id"] for n in graph.query("心率")})
        self.assertEqual(len(set(ids)), 3)

    def test_short_index_matches_exact_gram_membership(self) -> None:
        values = ["心率偏高", "上海浦东", "a1 心", "与的", "血压ABC", "Ｆｕｌｌ宽", "心心"]
        ids = {self.node(v): v for v in values}
        from knowledge_graph import _gram_set

        universe = {g for v in values for g in _gram_set(v, "event", "fixture")}
        with self.kg.connection() as db:
            for gram in universe:
                got = {
                    r[0]
                    for r in db.execute(
                        "SELECT n.id FROM nodes_short s JOIN nodes n ON n.rowid=s.rowid "
                        "WHERE nodes_short MATCH ?",
                        ('"' + gram.replace('"', '""') + '"',),
                    )
                }
                want = {i for i, v in ids.items() if gram in _gram_set(v, "event", "fixture")}
                self.assertEqual(got, want, gram)

    def test_empty_invalid_input_and_no_synthetic_cross_field_bigrams(self) -> None:
        self.node("甲", key="乙")
        self.assertEqual(self.ids("甲乙"), set())
        for question in ("", "!!!"):
            self.assertEqual(self.kg.query(question), [])
        self.assertEqual(self.kg.query("fixture", k=0), [])
        for k in (True, -1, 1.5, None):
            with self.assertRaises(ValueError):
                self.kg.query("fixture", k=k)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.kg.query(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main(verbosity=2)
