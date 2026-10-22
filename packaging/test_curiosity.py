from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import capabilities
from curiosity import Curiosity, confidence
from inner_loop import MindStore, Record


class CuriosityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.store = MindStore(self.root)

    def belief(self, *, value: str = "spoken", importance: float = 0.8) -> Record:
        return self.store.append_belief(
            belief_id=None,
            subject="person",
            attribute="fact",
            value=value,
            source="self_report",
            confidence=0.6,
            last_verified=datetime.now(UTC).isoformat(),
            importance=importance,
        )

    def test_task_lexical_recall_finds_old_event_and_one_exploration(self) -> None:
        old = self.store.ingest(
            "conversation", {"text": "月球实验的紫晶传感器校准失败"}, source="sir"
        )
        for number in range(20):
            self.store.append("thought", {"text": f"无关天气 {number}"}, source="test")
        self.store.ingest("conversation", {"text": "紫晶传感器校准还没解决"}, source="sir")
        context = Curiosity(self.store).context([], [], 1)
        recalled = context["recalled"]
        self.assertIn(old["id"], [row["id"] for row in recalled])
        self.assertLessEqual(len(recalled), 8)
        self.assertEqual(sum(row.get("recall_role") == "exploration" for row in recalled), 1)
        self.assertEqual(context["recall_strategy"], "lexical-rrf-v1")

    def test_lexical_bigram_updates_deletions_and_unknown_identifiers(self) -> None:
        curator = Curiosity(self.store)
        memory = {
            "id": "m1",
            "content": "紫晶",
            "version": 1,
            "created_at": "2025-01-01T00:00:00+00:00",
        }
        as_of = "2026-01-01T00:00:00+00:00"
        rows, info = curator.recall_index.search("紫晶", [memory], as_of=as_of)
        self.assertEqual([r["id"] for r in rows], ["m1"])
        self.assertEqual(info["rrf_k"], 60)
        rows, _ = curator.recall_index.search("紫晶", [], as_of=as_of)
        self.assertEqual(rows, [])  # stale FTS row is not a live memory
        memory["content"] = "琥珀"
        curator.recall_index.search("琥珀", [memory], as_of=as_of)
        rows, _ = curator.recall_index.search("紫晶", [memory], as_of=as_of)
        self.assertEqual(rows, [])
        self.store.append("thought", {"text": "历史念头的无关材料"}, source="test")
        curator.recall_index.sync()
        rows, _ = curator.recall_index.search(
            "历史念头 " + "a" * 32 + " 记录了什么？", [], as_of="2030-01-01T00:00:00+00:00"
        )
        self.assertEqual(rows, [])

    def test_lexical_asof_correction_background_and_future(self) -> None:
        curator = Curiosity(self.store)
        old = self.belief(value="蓝晶")
        corrected = self.store.append_belief(
            belief_id=old["belief_id"],
            subject="person",
            attribute="fact",
            value="紫晶",
            source="self_report",
            confidence=0.6,
            last_verified="2030-01-01T00:00:00+00:00",
            importance=0.8,
        )
        self.store.ingest("conversation", {"text": "合成紫晶", "synthetic": True}, source="sir")
        curator.recall_index.sync()
        before = "2029-01-01T00:00:00+00:00"
        rows, _ = curator.recall_index.search("蓝晶", [], as_of=before)
        self.assertIn(old["belief_id"], [r["id"] for r in rows])
        rows, _ = curator.recall_index.search("紫晶", [], as_of=before)
        self.assertEqual(rows, [])
        rows, _ = curator.recall_index.search("蓝晶", [], as_of="2031-01-01T00:00:00+00:00")
        self.assertEqual(rows, [])
        rows, _ = curator.recall_index.search("紫晶", [], as_of="2031-01-01T00:00:00+00:00")
        self.assertEqual([r["id"] for r in rows], [corrected["belief_id"]])
        self.assertEqual(rows[0]["value"], "紫晶")

    def test_lexical_profile_topic_and_active_goal_query(self) -> None:
        from goal_tracker import GoalTracker
        from inner_loop_fts import task_query
        from sir_profile import SirProfile

        curator = Curiosity(self.store)
        profile = SirProfile(self.root)
        profile.upsert("identity", "system_user", "harbor", 0.9, "self_report")
        goals = GoalTracker(self.root, self.store)
        goals.set_goal("紫晶传感器校准", ["finish calibration"])
        as_of = "2030-01-01T00:00:00+00:00"
        query = task_query(self.store, as_of)
        self.assertIn("紫晶传感器校准", query)
        self.assertNotIn("goal_id", query)
        rows, _ = curator.recall_index.search("system_user", [], as_of=as_of)
        self.assertEqual(rows[0]["value"], "harbor")
        self.assertTrue(rows[0]["evidence_id"].startswith("profile:facts:"))
        self.assertEqual(rows[0]["recall_role"], "evidence")

    def test_lexical_timeout_cancels_query_and_connection_is_reusable(self) -> None:
        curator = Curiosity(self.store)
        for number in range(100):
            self.store.append("thought", {"text": f"紫晶传感器校准 {number}"}, source="test")
        curator.recall_index.sync(full=True)
        rows, info = curator.recall_index.search(
            "紫晶传感器校准", [], as_of="2030-01-01T00:00:00+00:00", deadline_ms=0
        )
        self.assertEqual(rows, [])
        self.assertEqual(info["status"], "timeout")
        rows, info = curator.recall_index.search(
            "紫晶传感器校准", [], as_of="2030-01-01T00:00:00+00:00"
        )
        self.assertTrue(rows)
        self.assertEqual(info["status"], "ok")

    def test_lexical_deleted_belief_and_stale_memory_never_starve_live_hit(self) -> None:
        as_of = "2030-01-01T00:00:00+00:00"
        curator = Curiosity(self.store)
        memories: list[Record] = [
            {
                "id": f"m{n}",
                "content": "紫晶",
                "version": 1,
                "created_at": "2025-01-01T00:00:00+00:00",
            }
            for n in range(90)
        ]
        curator.recall_index.search("紫晶", memories, as_of=as_of)
        self.store.append_belief(
            belief_id="memory:m89",
            subject="Sir",
            attribute="topic",
            value="紫晶",
            source="self_report",
            confidence=0.6,
            last_verified=datetime.now(UTC).isoformat(),
            memory_id="m89",
            importance=0.8,
        )
        curator.recall_index.sync()
        rows, _ = curator.recall_index.search("紫晶", memories[:1], as_of=as_of)
        self.assertEqual([r["id"] for r in rows], ["m0"])

    def test_lexical_entity_reference_and_date_channel(self) -> None:
        as_of = "2030-01-01T00:00:00+00:00"
        linked = "a" * 32
        thought = self.store.append("thought", {"text": "紫晶实验"}, source="test", refs=[linked])
        curator = Curiosity(self.store)
        rows, info = curator.recall_index.search(linked, [], as_of=as_of)
        self.assertEqual([r["id"] for r in rows], [thought["id"]])
        self.assertEqual(info["channels"]["entity_time"], 1)
        rows, info = curator.recall_index.search(thought["created_at"][:10], [], as_of=as_of)
        self.assertEqual([r["id"] for r in rows], [thought["id"]])
        self.assertEqual(info["channels"]["entity_time"], 1)

    def test_lexical_nested_provenance_and_future_observation_are_not_evidence(self) -> None:
        from inner_loop_fts import task_query

        curator = Curiosity(self.store)
        for flag in ("backfill", "background_only", "synthetic"):
            self.store.ingest(
                "tool_result", {"result": {"text": "紫晶", flag: True}}, source="tool"
            )
        self.store.ingest(
            "tool_result",
            {"result": {"text": "紫晶", "sampled_at": "2035-01-01T00:00:00+00:00"}},
            source="tool",
        )
        curator.recall_index.sync()
        rows, _ = curator.recall_index.search("紫晶", [], as_of="2030-01-01T00:00:00+00:00")
        self.assertEqual(rows, [])
        self.assertNotIn("紫晶", task_query(self.store, "2030-01-01T00:00:00+00:00"))

    def test_lexical_profile_ascii_key_has_word_boundaries_and_query_uses_pending(self) -> None:
        from inner_loop_fts import task_query
        from sir_profile import SirProfile

        curator = Curiosity(self.store)
        SirProfile(self.root).upsert("identity", "age", "37", 0.9, "self_report")
        rows, _ = curator.recall_index.search("storage", [], as_of="2030-01-01T00:00:00+00:00")
        self.assertEqual(rows, [])
        self.store.append(
            "question_state",
            {"question_id": "q1", "text": "紫晶实验进展", "state": "unresolved"},
            source="test",
        )
        curator.context([], [], 1)
        self.assertIn("紫晶实验进展", task_query(self.store, "2030-01-01T00:00:00+00:00"))
        self.store.append(
            "question_state",
            {"question_id": "q1", "text": "紫晶实验进展", "state": "resolved"},
            source="test",
        )
        curator.context([], [], 1)
        self.assertNotIn("紫晶实验进展", task_query(self.store, "2030-01-01T00:00:00+00:00"))

    def test_lexical_asof_comparison_uses_instants_not_iso_lexicography(self) -> None:
        curator = Curiosity(self.store)
        memory = {
            "id": "m1",
            "content": "紫晶",
            "version": 1,
            "created_at": "2030-01-01T01:00:00+08:00",
        }
        rows, _ = curator.recall_index.search("紫晶", [memory], as_of="2029-12-31T18:00:00+00:00")
        self.assertEqual([r["id"] for r in rows], ["m1"])

    def test_lexical_task_sources_share_query_term_budget_and_compact_views(self) -> None:
        from goal_tracker import GoalTracker

        curator = Curiosity(self.store)
        target = self.store.append("thought", {"text": "紫晶校准" * 1000}, source="test")
        self.store.ingest("conversation", {"text": "latest sensor data " * 200}, source="sir")
        GoalTracker(self.root, self.store).set_goal("紫晶校准", ["finish"])
        context = curator.context([], [], 1)
        rows = [r for r in context["recalled"] if r["id"] == target["id"]]
        self.assertEqual(len(rows), 1)
        self.assertLessEqual(len(rows[0]["payload"]["text"]), 768)
        self.assertEqual(self.store.recent("thought", 1)[0]["payload"]["text"], "紫晶校准" * 1000)

    def test_lexical_projection_rollback_rebuild_preserves_source_evidence(self) -> None:
        target = self.store.append("thought", {"text": "紫晶实验"}, source="test")
        curator = Curiosity(self.store)
        curator.recall_index.rollback()
        curator.recall_index.rollback()
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM entries").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT name FROM sqlite_master WHERE name='recall_docs_v1'").fetchall(),
                [],
            )
        curator = Curiosity(self.store)
        rows, _ = curator.recall_index.search("紫晶实验", [], as_of="2030-01-01T00:00:00+00:00")
        self.assertEqual([r["id"] for r in rows], [target["id"]])

    def test_lexical_index_restart_is_idempotent_and_source_append_only(self) -> None:
        self.store.append("thought", {"text": "紫晶传感器"}, source="test")
        Curiosity(self.store)
        with self.store.connection() as db:
            before = [tuple(r) for r in db.execute("SELECT * FROM entries")]
            count = db.execute("SELECT count(*) FROM recall_docs_v1").fetchone()[0]
        Curiosity(self.store)
        with self.store.connection() as db:
            self.assertEqual(count, db.execute("SELECT count(*) FROM recall_docs_v1").fetchone()[0])
            self.assertEqual(before, [tuple(r) for r in db.execute("SELECT * FROM entries")])
        self.assertFalse((self.root / "sir_profile.db").exists())
        self.assertFalse((self.root / "goals.db").exists())

    def test_fresh_self_report_does_not_request_reconfirmation(self) -> None:
        row = self.belief(importance=1.0)
        context = {
            "beliefs": [row],
            "relevance_candidates": [row],
            "cached_relevance": [],
            "as_of": row["last_verified"],
        }
        decision = Curiosity(self.store).decide([], context, [], 1)
        self.assertEqual(decision["kind"], "associate")

    def test_decide_ignores_beliefs_outside_current_candidates(self) -> None:
        self.belief(importance=1.0)
        context: Record = {
            "relevance_candidates": [],
            "cached_relevance": [],
            "as_of": datetime.now(UTC).isoformat(),
        }
        decision = Curiosity(self.store).decide([], context, [], 1)
        self.assertEqual(decision["kind"], "associate")

    def test_source_confidence_and_decay_are_deterministic(self) -> None:
        row = self.belief()
        now = datetime.fromisoformat(row["last_verified"])
        self.assertAlmostEqual(confidence(row, now), 0.6)
        later = now.replace(day=now.day + 1) if now.day < 28 else now
        self.assertLessEqual(confidence(row, later), 0.6)

    def test_voi_selects_highest_capability_and_records_score(self) -> None:
        row = self.belief(importance=0.9)
        curiosity = Curiosity(self.store)
        context = {
            "beliefs": [row],
            "relevance_candidates": [row],
            "cached_relevance": [],
            "as_of": datetime.now(UTC).isoformat(),
        }
        relevance = [
            {
                "belief_id": row["belief_id"],
                "capability_id": "location_track",
                "score": 0.9,
                "reason": "sample can change confidence",
            }
        ]
        decision = curiosity.decide(relevance, context, capabilities.fixture_catalog(), 1)
        self.assertEqual(decision["kind"], "execute")
        self.assertEqual(decision["capability_id"], "location_track")
        self.assertEqual(len(self.store.entries("belief_score")), 1)
        self.assertGreater(self.store.entries("belief_score")[0]["payload"]["scores"][0]["u"], 0)

    def test_two_beliefs_attempted_by_value_in_separate_rounds(self) -> None:
        first = self.belief(importance=1.0)
        second = self.belief(importance=0.8)
        curiosity = Curiosity(self.store)
        context = {
            "relevance_candidates": [first, second],
            "cached_relevance": [],
            "as_of": datetime.now(UTC).isoformat(),
        }
        catalog = capabilities.fixture_catalog(include_sleep=True)
        rows = [
            {
                "belief_id": b["belief_id"],
                "capability_id": c["id"],
                "score": 1.0 if i == j else 0.0,
                "reason": "independent source",
            }
            for i, b in enumerate((first, second))
            for j, c in enumerate(catalog)
        ]
        decision = curiosity.decide(rows, context, catalog, 1)
        self.assertEqual(decision["belief_id"], first["belief_id"])
        self.store.append("action", {**decision, "state": "intent"}, source="curiosity-scheduler")
        decision = curiosity.decide(rows, context, catalog, 2)
        self.assertEqual(decision["belief_id"], second["belief_id"])
        self.assertEqual(decision["capability_id"], catalog[1]["id"])

    def test_paid_source_is_zero_voi_and_waits(self) -> None:
        row = self.belief(importance=1.0)
        catalog = capabilities.fixture_catalog()
        catalog[0]["costs_money"] = True
        context = {
            "beliefs": [row],
            "relevance_candidates": [row],
            "cached_relevance": [],
            "as_of": datetime.now(UTC).isoformat(),
        }
        decision = Curiosity(self.store).decide(
            [
                {
                    "belief_id": row["belief_id"],
                    "capability_id": catalog[0]["id"],
                    "score": 1.0,
                    "reason": "strong",
                }
            ],
            context,
            catalog,
            1,
        )
        self.assertEqual(decision["kind"], "pending_sir_permission")
        self.assertEqual(self.store.entries("action"), [])

    def test_synthetic_tool_result_records_attempt_without_observed_upgrade(self) -> None:
        row = self.belief(value="spoken")
        event = self.store.ingest(
            "tool_result",
            {"belief_id": row["belief_id"], "result": {"value": "observed", "synthetic": True}},
            source="location_track",
        )
        context = {
            "pending_memories": [],
            "pending_observations": [event],
            "relevance_candidates": [row],
            "capabilities": capabilities.fixture_catalog(),
        }
        Curiosity(self.store).update(
            [
                {
                    "origin_id": event["id"],
                    "belief_id": row["belief_id"],
                    "subject": "person",
                    "attribute": "renamed by model",
                    "value": "observed",
                    "importance": 0.8,
                    "conflict": False,
                }
            ],
            context,
            relevance=[
                {
                    "belief_id": row["belief_id"],
                    "capability_id": "location_track",
                    "score": 0.9,
                    "reason": "fixture",
                }
            ],
        )
        self.assertEqual(self.store.beliefs()[0]["source"], "self_report")
        self.assertEqual(self.store.beliefs()[0]["attribute"], "fact")
        self.assertEqual(
            self.store.entries("belief_origin")[0]["payload"]["status"], "attempted_no_evidence"
        )

    def test_zero_relevance_observation_does_not_upgrade_belief(self) -> None:
        row = self.belief(value="spoken")
        event = self.store.ingest(
            "tool_result",
            {"belief_id": row["belief_id"], "result": {"value": "observed", "synthetic": False}},
            source="location_track",
        )
        context = {
            "pending_memories": [],
            "pending_observations": [event],
            "relevance_candidates": [row],
            "capabilities": capabilities.fixture_catalog(),
        }
        Curiosity(self.store).update(
            [
                {
                    "origin_id": event["id"],
                    "belief_id": row["belief_id"],
                    "subject": "person",
                    "attribute": "renamed by model",
                    "value": "observed",
                    "importance": 0.8,
                    "conflict": False,
                }
            ],
            context,
            relevance=[
                {
                    "belief_id": row["belief_id"],
                    "capability_id": "location_track",
                    "score": 0.0,
                    "reason": "unrelated",
                }
            ],
        )
        self.assertEqual(self.store.beliefs()[0]["source"], "self_report")
        self.assertEqual(len(self.store.entries("belief_versions")), 0)

    def test_attribute_rewrite_keeps_stable_belief_identity(self) -> None:
        row = self.belief(value="spoken")
        event = self.store.ingest(
            "tool_result",
            {"belief_id": row["belief_id"], "result": {"value": "observed", "synthetic": False}},
            source="location_track",
        )
        context = {
            "pending_memories": [],
            "pending_observations": [event],
            "relevance_candidates": [row],
            "capabilities": capabilities.fixture_catalog(),
        }
        Curiosity(self.store).update(
            [
                {
                    "origin_id": event["id"],
                    "belief_id": row["belief_id"],
                    "subject": "person",
                    "attribute": "model renamed this",
                    "value": "observed",
                    "importance": 0.8,
                    "conflict": True,
                }
            ],
            context,
            relevance=[
                {
                    "belief_id": row["belief_id"],
                    "capability_id": "location_track",
                    "score": 0.9,
                    "reason": "fixture",
                }
            ],
        )
        belief = self.store.beliefs()[0]
        self.assertEqual(belief["belief_id"], row["belief_id"])
        self.assertEqual(belief["attribute"], "model renamed this")
        self.assertEqual(belief["source"], "observed")

    def test_append_only_conflict_keeps_both_versions(self) -> None:
        row = self.belief(value="spoken")
        event = self.store.ingest(
            "tool_result",
            {"belief_id": row["belief_id"], "result": {"value": "observed"}},
            source="location_track",
        )
        context = {
            "pending_memories": [],
            "pending_observations": [event],
            "relevance_candidates": [row],
            "capabilities": capabilities.fixture_catalog(),
        }
        Curiosity(self.store).update(
            [
                {
                    "origin_id": event["id"],
                    "belief_id": row["belief_id"],
                    "subject": "person",
                    "attribute": "fact",
                    "value": "observed",
                    "importance": 0.8,
                    "conflict": True,
                }
            ],
            context,
            relevance=[
                {
                    "belief_id": row["belief_id"],
                    "capability_id": "location_track",
                    "score": 1.0,
                    "reason": "direct evidence",
                }
            ],
        )
        self.assertEqual([b["value"] for b in self.store.beliefs()], ["observed"])
        self.assertEqual(len(self.store.entries("conflict")), 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM belief_versions").fetchone()[0], 2)

    def test_reconfirmation_window_and_conflict_after_verification(self) -> None:
        from datetime import timedelta

        from curiosity import SELF_REPORT_RECONFIRM_SECONDS

        row = self.belief()
        now = datetime.fromisoformat(row["last_verified"])
        curiosity = Curiosity(self.store)
        self.assertGreaterEqual(SELF_REPORT_RECONFIRM_SECONDS, 30 * 86400)
        self.assertFalse(curiosity.question_eligible(row, now + timedelta(days=29)))
        self.assertTrue(curiosity.question_eligible(row, now + timedelta(days=30)))
        conflict = self.store.append("conflict", {"belief_id": row["belief_id"]}, source="test")
        after_conflict = datetime.fromisoformat(conflict["created_at"])
        self.assertTrue(curiosity.question_eligible(row, after_conflict))
        row["last_verified"] = (after_conflict + timedelta(seconds=1)).isoformat()
        self.assertFalse(curiosity.question_eligible(row, after_conflict + timedelta(seconds=2)))

    def test_update_matrix_failure_does_not_import_first_memory(self) -> None:
        row = self.belief()
        context: Record = {
            "pending_memories": [{"origin_id": "memory:new", "created_at": row["last_verified"]}],
            "pending_observations": [],
            "relevance_candidates": [row],
            "cached_relevance": [],
            "capabilities": capabilities.fixture_catalog(),
        }
        update: Record = {
            "origin_id": "memory:new",
            "belief_id": "memory:new",
            "subject": "person",
            "attribute": "fact",
            "value": "new",
            "importance": 0.8,
            "conflict": False,
        }
        before = self.store.beliefs()
        with self.assertRaisesRegex(ValueError, "Incomplete relevance matrix"):
            Curiosity(self.store).update([update], context, relevance=[])
        self.assertEqual(self.store.beliefs(), before)
        self.assertEqual(self.store.entries(), [])

    def test_missing_target_source_relevance_never_upgrades(self) -> None:
        row = self.belief()
        event = self.store.ingest(
            "tool_result", {"belief_id": row["belief_id"], "result": {}}, source="location_track"
        )
        context: Record = {
            "pending_memories": [],
            "pending_observations": [event],
            "relevance_candidates": [row],
            "capabilities": [],
        }
        update: Record = {
            "origin_id": event["id"],
            "belief_id": row["belief_id"],
            "subject": "person",
            "attribute": "fact",
            "value": "replacement",
            "importance": 0.8,
            "conflict": True,
        }
        Curiosity(self.store).update([update], context, relevance=[])
        self.assertEqual(self.store.beliefs()[0]["source"], "self_report")
        self.assertEqual(self.store.entries("conflict"), [])
        self.assertEqual(
            self.store.entries("belief_origin")[0]["payload"]["status"], "attempted_no_evidence"
        )

    def test_legacy_generated_identity_blocks_ranked_question(self) -> None:
        row = self.belief()
        row["source"] = "inferred"
        self.store.append(
            "action_request",
            {"belief_id": row["belief_id"], "question_id": "legacy"},
            source="test",
        )
        self.store.append("question_generation_intent", {"question_id": "legacy"}, source="test")
        context: Record = {
            "beliefs": [row],
            "relevance_candidates": [row],
            "cached_relevance": [],
            "as_of": row["last_verified"],
        }
        self.assertEqual(Curiosity(self.store).decide([], context, [], 1)["kind"], "associate")

    def test_cached_rank_can_be_replaced_in_next_round(self) -> None:
        row = self.belief()
        cached = {
            "belief_id": row["belief_id"],
            "capability_id": "location_track",
            "score": 0.0,
            "reason": "old score",
        }
        context: Record = {
            "beliefs": [row],
            "relevance_candidates": [row],
            "cached_relevance": [cached],
            "as_of": row["last_verified"],
        }
        decision = Curiosity(self.store).decide(
            [{**cached, "score": 1.0}], context, capabilities.fixture_catalog(), 1
        )
        self.assertEqual(decision["kind"], "execute")

    def test_unknown_observation_source_never_upgrades(self) -> None:
        row = self.belief()
        event = self.store.ingest(
            "tool_result",
            {
                "belief_id": row["belief_id"],
                "result": {"synthetic": False},
            },
            source="unknown",
        )
        context: Record = {
            "pending_memories": [],
            "pending_observations": [event],
            "relevance_candidates": [row],
            "capabilities": [],
        }
        update = {
            "origin_id": event["id"],
            "belief_id": row["belief_id"],
            "subject": "person",
            "attribute": "fact",
            "value": "replacement",
            "importance": 0.8,
            "conflict": True,
        }
        relevance = [
            {
                "belief_id": row["belief_id"],
                "capability_id": "unknown",
                "score": 1.0,
                "reason": "untrusted",
            }
        ]
        Curiosity(self.store).update([update], context, relevance=relevance)
        self.assertEqual(self.store.beliefs()[0]["source"], "self_report")

    def test_ranked_generated_question_yields_to_next_belief(self) -> None:
        first, second = self.belief(), self.belief()
        first["source"] = second["source"] = "inferred"
        self.store.append(
            "question_generation_intent",
            {"question_id": "scheduler:" + first["belief_id"]},
            source="test",
        )
        context: Record = {
            "beliefs": [first, second],
            "relevance_candidates": [first, second],
            "cached_relevance": [],
            "as_of": first["last_verified"],
        }
        decision = Curiosity(self.store).decide([], context, [], 1)
        self.assertEqual(decision["belief_id"], second["belief_id"])

    def test_fresh_bound_legacy_question_is_not_asked(self) -> None:
        row = self.belief()
        self.store.append(
            "action_request",
            {
                "belief_id": row["belief_id"],
                "question_id": "legacy",
                "question": {
                    "text": "repeat",
                    "selected_capability": None,
                    "missing_information": "repeat",
                },
            },
            source="model",
        )
        context: Record = {
            "beliefs": [row],
            "relevance_candidates": [row],
            "cached_relevance": [],
            "as_of": row["last_verified"],
        }
        self.assertEqual(Curiosity(self.store).decide([], context, [], 1)["kind"], "associate")

    def test_duplicate_legacy_belief_is_not_asked_twice(self) -> None:
        belief = self.belief(importance=0.3)
        self.store.append_belief(
            **{
                k: belief[k]
                for k in (
                    "belief_id",
                    "subject",
                    "attribute",
                    "value",
                    "confidence",
                    "last_verified",
                    "importance",
                )
            },
            source="inferred",
        )
        for identity in ("first", "second"):
            self.store.append(
                "action_request",
                {
                    "question_id": identity,
                    "belief_id": belief["belief_id"],
                    "question": {
                        "text": identity,
                        "selected_capability": None,
                        "missing_information": "unknown",
                    },
                },
                source="model",
            )
        self.store.append("question_generation_intent", {"question_id": "first"}, source="test")
        context: Record = {
            "beliefs": self.store.beliefs(),
            "relevance_candidates": self.store.beliefs(),
            "cached_relevance": [],
            "as_of": datetime.now(UTC).isoformat(),
        }
        self.assertEqual(Curiosity(self.store).decide([], context, [], 2)["kind"], "associate")

    def test_generated_legacy_request_cannot_block_next(self) -> None:
        for identity in ("first", "second"):
            self.store.append_belief(
                belief_id=identity,
                subject="person",
                attribute=identity,
                value="unknown",
                source="inferred",
                confidence=0.4,
                last_verified=datetime.now(UTC).isoformat(),
                importance=0.3,
            )
            self.store.append(
                "action_request",
                {
                    "question_id": identity,
                    "belief_id": identity,
                    "question": {
                        "text": identity,
                        "selected_capability": None,
                        "missing_information": "unknown",
                    },
                },
                source="model",
            )
        self.store.append("question_generation_intent", {"question_id": "first"}, source="test")
        context: Record = {
            "beliefs": self.store.beliefs(),
            "relevance_candidates": self.store.beliefs(),
            "cached_relevance": [],
            "as_of": datetime.now(UTC).isoformat(),
        }
        decision = Curiosity(self.store).decide([], context, [], 1)
        self.assertEqual(decision["question_id"], "second")

    def test_fixed_sample_cannot_repeat_after_legacy_action(self) -> None:
        row = self.belief(importance=1.0)
        self.store.append(
            "action",
            {"belief_id": "legacy-question", "capability_id": "location_track"},
            source="test",
        )
        context: Record = {
            "beliefs": [row],
            "relevance_candidates": [row],
            "cached_relevance": [],
            "as_of": row["last_verified"],
        }
        relevance = [
            {
                "belief_id": row["belief_id"],
                "capability_id": "location_track",
                "score": 1.0,
                "reason": "rank raised",
            }
        ]
        self.assertNotEqual(
            Curiosity(self.store).decide(relevance, context, capabilities.fixture_catalog(), 1)[
                "kind"
            ],
            "execute",
        )

    def test_legacy_identity_uses_current_belief(self) -> None:
        row = self.belief()
        self.store.append(
            "action_request",
            {
                "belief_id": row["belief_id"],
                "question_id": "legacy",
                "question": {
                    "text": "read",
                    "selected_capability": "location_track",
                    "missing_information": "unknown",
                },
            },
            source="model",
        )
        context: Record = {
            "beliefs": [row],
            "relevance_candidates": [row],
            "cached_relevance": [],
            "as_of": row["last_verified"],
        }
        relevance = [
            {
                "belief_id": row["belief_id"],
                "capability_id": "location_track",
                "score": 0.0,
                "reason": "low ranking",
            }
        ]
        decision = Curiosity(self.store).decide(
            relevance, context, capabilities.fixture_catalog(), 1
        )
        self.assertEqual(decision["belief_id"], row["belief_id"])
        self.assertEqual(decision["question_id"], "legacy")

    def test_matrix_failure_precedes_any_cache_write(self) -> None:
        first, second = self.belief(), self.belief()
        context: Record = {
            "beliefs": [first, second],
            "relevance_candidates": [first, second],
            "cached_relevance": [],
            "as_of": first["last_verified"],
        }
        rows = [
            {
                "belief_id": first["belief_id"],
                "capability_id": "location_track",
                "score": 0.9,
                "reason": "first only",
            }
        ]
        before = self.store.entries()
        with self.assertRaisesRegex(ValueError, "Incomplete relevance matrix"):
            Curiosity(self.store).decide(rows, context, capabilities.fixture_catalog(), 1)
        self.assertEqual(self.store.entries(), before)


class PerceptionBeliefTests(unittest.TestCase):
    store: MindStore
    setUp = CuriosityTests.setUp
    belief = CuriosityTests.belief

    def test_self_report_event_forms_self_report_without_hidden_call(self) -> None:
        from inner_runtime import InnerLoop

        loop = InnerLoop(self.store, lambda _: {}, catalog=[])
        event = loop.ingest("conversation", {"text": "I prefer paper books"}, source="chat")
        context = loop.context()
        pending = context["pending_observations"][0]
        identity = pending["payload"]["data"]["belief_id"]
        loop.curiosity.update(
            [
                {
                    "origin_id": event["id"],
                    "belief_id": identity,
                    "subject": "Sir",
                    "attribute": "reading preference",
                    "value": "paper books",
                    "importance": 0.8,
                    "conflict": False,
                }
            ],
            context,
            relevance=[],
        )
        self.assertEqual(self.store.beliefs()[0]["source"], "self_report")
        self.assertEqual(self.store.entries("model_call"), [])

    def test_fake_model_scores_real_catalog_and_executes_single_source(self) -> None:
        from inner_runtime import InnerLoop
        from test_inner_loop import answer

        entries = capabilities.catalog(adapters=[])
        for entry in entries:
            entry["available"] = True
        belief = self.belief(importance=1.0)
        executed: list[str] = []

        def model(prompt: str) -> Record:
            result = answer(prompt)
            output = json.loads(result["text"])
            for row in output["relevance"]:
                row["score"] = 1.0 if row["capability_id"] == "activity_usage" else 0.0
            return {"text": json.dumps(output)}

        def executor(capability: Record) -> Record:
            executed.append(capability["id"])
            return {
                "synthetic": False,
                "source": "activitywatch",
                "status": "available",
                "sampled_at": belief["last_verified"],
                "kind": "app_usage",
            }

        loop = InnerLoop(
            self.store,
            model,
            catalog=entries,
            executor=executor,
            max_rounds=2,
            active_seconds=0,
            idle_seconds=0,
        )
        loop.run()
        self.assertEqual(executed, ["activity_usage"])
        self.assertEqual(len(self.store.entries("action")), 1)
        self.assertEqual(len(self.store.entries("belief_score")), 2)

    def test_new_perception_invalidates_cached_relevance_and_latest_uses_event_time(self) -> None:
        from inner_runtime import InnerLoop

        entries = capabilities.catalog(adapters=[])
        loop = InnerLoop(self.store, lambda _: {}, catalog=entries)
        observed: list[Record] = []
        for day in (15, 14):
            observed.append(
                loop.ingest(
                    "tool_result",
                    {
                        "sense_event": {
                            "source": "activitywatch",
                            "kind": "app_usage",
                            "payload": {},
                            "observed_at": f"2026-10-{day}T01:00:00+00:00",
                        }
                    },
                    source="senses:activitywatch",
                )
            )
        context = loop.context()
        pending = context["pending_observations"]
        self.assertEqual([e["id"] for e in pending], [observed[0]["id"]])
        identity = pending[0]["payload"]["data"]["belief_id"]

        def matrix(view: Record) -> list[Record]:
            """Score exactly the capabilities that prompt advertised.

            The byte budget can hide catalog rows, and the matrix validator
            rejects both a score for a hidden capability and a missing pair for
            an advertised one, so each prompt needs its own complete matrix.
            """
            return [
                {
                    "belief_id": b["belief_id"],
                    "capability_id": c["id"],
                    "score": 0.5,
                    "reason": "metadata relevance",
                }
                for b in view["relevance_candidates"]
                for c in view["capabilities"]
            ]

        loop.curiosity.update(
            [
                {
                    "origin_id": pending[0]["id"],
                    "belief_id": identity,
                    "subject": "Sir",
                    "attribute": "record",
                    "value": "exists",
                    "importance": 0.8,
                    "conflict": False,
                }
            ],
            context,
            relevance=matrix(context),
        )
        current = loop.context()
        self.assertEqual(current["pending_observations"], [])
        # Scheduling validates against the catalog this prompt advertised, exactly
        # as step() does; the full catalog would demand rows for hidden ids.
        loop.curiosity.decide(matrix(current), current, loop.context_catalog(current), 1)
        self.assertTrue(loop.context()["cached_relevance"])
        loop.ingest(
            "tool_result",
            {
                "sense_event": {
                    "source": "activitywatch",
                    "kind": "app_usage",
                    "payload": {},
                    "observed_at": "2026-10-15T02:00:00+00:00",
                }
            },
            source="senses:activitywatch",
        )
        self.assertEqual(loop.context()["cached_relevance"], [])

    def test_provenance_flags_and_unknown_kind_remain_background(self) -> None:
        from inner_runtime import InnerLoop

        loop = InnerLoop(self.store, lambda _: {}, catalog=capabilities.catalog(adapters=[]))
        for source, extra, kind in (
            ("untrusted", {}, "app_usage"),
            ("senses:activitywatch", {"background_only": True}, "app_usage"),
            ("senses:activitywatch", {}, "PRIVATE_KIND_SENTINEL"),
        ):
            loop.ingest(
                "tool_result",
                {
                    "sense_event": {
                        "source": "activitywatch",
                        "kind": kind,
                        "payload": {},
                        "observed_at": "2026-10-15T01:00:00+00:00",
                        **extra,
                    }
                },
                source=source,
            )
        context = loop.context()
        self.assertEqual(context["pending_observations"], [])
        self.assertNotIn("PRIVATE_KIND_SENTINEL", json.dumps(context))

    def test_failed_or_backfilled_real_query_does_not_upgrade(self) -> None:
        for result in (
            {"status": "unavailable"},
            {"status": "empty"},
            {"status": "available", "sampled_at": "2000-01-01T00:00:00+00:00"},
            {"status": "available", "backfill": True},
        ):
            belief = self.belief(importance=1.0)
            catalog = capabilities.catalog(adapters=[])
            evidence = {
                "synthetic": False,
                "source": "health",
                "sampled_at": datetime.now(UTC).isoformat(),
                **result,
            }
            event = self.store.ingest(
                "tool_result",
                {
                    "belief_id": belief["belief_id"],
                    "result": evidence,
                },
                source="health_record",
            )
            context = {
                "pending_memories": [],
                "pending_observations": [event],
                "relevance_candidates": [belief],
                "cached_relevance": [],
                "capabilities": catalog,
            }
            rows = [
                {
                    "belief_id": belief["belief_id"],
                    "capability_id": c["id"],
                    "score": 1.0,
                    "reason": "may inform",
                }
                for c in catalog
            ]
            Curiosity(self.store).update(
                [
                    {
                        "origin_id": event["id"],
                        "belief_id": belief["belief_id"],
                        "subject": "Sir",
                        "attribute": "record",
                        "value": "exists",
                        "importance": 1.0,
                        "conflict": False,
                    }
                ],
                context,
                relevance=rows,
            )
            latest = next(b for b in self.store.beliefs() if b["belief_id"] == belief["belief_id"])
            self.assertEqual(latest["revision"], 1)

    def test_live_source_can_serve_different_belief_after_prior_attempt(self) -> None:
        belief = self.belief(importance=1.0)
        entries = capabilities.catalog(adapters=[])
        for entry in entries:
            entry["available"] = True
        self.store.append(
            "action",
            {"belief_id": "different", "belief_revision": 1, "capability_id": "health_record"},
            source="test",
        )
        context = {
            "beliefs": [belief],
            "relevance_candidates": [belief],
            "cached_relevance": [],
            "as_of": belief["last_verified"],
        }
        rows = [
            {
                "belief_id": belief["belief_id"],
                "capability_id": c["id"],
                "score": float(c["id"] == "health_record"),
                "reason": "evidence",
            }
            for c in entries
        ]
        decision = Curiosity(self.store).decide(rows, context, entries, 1)
        self.assertEqual(decision["kind"], "execute")

    def test_real_event_creates_and_refreshes_stable_observed_belief(self) -> None:
        from inner_runtime import InnerLoop

        loop = InnerLoop(self.store, lambda _: {}, catalog=capabilities.catalog(adapters=[]))
        identities: list[str] = []
        for day in (14, 15):
            event = loop.ingest(
                "tool_result",
                {
                    "sense_event": {
                        "source": "activitywatch",
                        "kind": "app_usage",
                        "observed_at": f"2026-10-{day}T01:00:00+00:00",
                        "payload": {"app": "PRIVATE", "title": "PRIVATE"},
                    }
                },
                source="senses:activitywatch",
            )
            context = loop.context()
            self.assertNotIn("PRIVATE", json.dumps(context))
            pending = next(e for e in context["pending_observations"] if e["id"] == event["id"])
            identity = pending["payload"]["data"]["belief_id"]
            identities.append(identity)
            # Only the advertised capabilities are scoreable: hidden rows cannot
            # be judged by a model that never saw them.
            relevance = [
                {
                    "belief_id": b["belief_id"],
                    "capability_id": c["id"],
                    "score": 1.0,
                    "reason": "metadata evidence",
                }
                for b in context["relevance_candidates"]
                for c in context["capabilities"]
            ]
            loop.curiosity.update(
                [
                    {
                        "origin_id": event["id"],
                        "belief_id": identity,
                        "subject": "Sir",
                        "attribute": "active application record",
                        "value": "A foreground record exists",
                        "importance": 0.8,
                        "conflict": False,
                    }
                ],
                context,
                relevance=relevance,
            )
        self.assertEqual(identities[0], identities[1])
        self.assertEqual(len(self.store.beliefs()), 1)
        belief = self.store.beliefs()[0]
        self.assertEqual(belief["source"], "observed")
        self.assertEqual(belief["revision"], 2)
        self.assertEqual(belief["last_verified"], "2026-10-15T01:00:00+00:00")
        self.assertIn(event["id"], belief["refs"])

    def test_synthetic_and_backfill_events_cannot_create_observed(self) -> None:
        from inner_runtime import InnerLoop

        loop = InnerLoop(self.store, lambda _: {}, catalog=capabilities.catalog(adapters=[]))
        for kind, payload in (("speech", {"synthetic": True}), ("backfill_summary", {})):
            event = loop.ingest(
                "tool_result",
                {
                    "sense_event": {
                        "source": "speech",
                        "kind": kind,
                        "observed_at": "2026-10-15T01:00:00+00:00",
                        "payload": payload,
                    }
                },
                source="senses:speech",
            )
            context = loop.context()
            self.assertNotIn(event["id"], [e["id"] for e in context["pending_observations"]])
        self.assertEqual(self.store.beliefs(), [])

    def test_unavailable_real_sources_have_zero_voi_and_are_skipped(self) -> None:
        belief = self.belief(importance=1.0)
        # Only the live sense sources are under test. The web capabilities
        # (search, citation lookup) are always available by design, so scoring
        # them would legitimately produce an executable action and hide the rule
        # that an unavailable source can never earn VOI.
        catalog = [
            c
            for c in capabilities.catalog(adapters=[])
            if c["implementation"] == "senses_read_only"
        ]
        self.assertTrue(catalog)
        self.assertFalse(any(c["available"] for c in catalog))
        rows = [
            {
                "belief_id": belief["belief_id"],
                "capability_id": c["id"],
                "score": 1.0,
                "reason": "potential evidence",
            }
            for c in catalog
        ]
        context = {
            "beliefs": [belief],
            "relevance_candidates": [belief],
            "cached_relevance": [],
            "as_of": belief["last_verified"],
        }
        decision = Curiosity(self.store).decide(rows, context, catalog, 1)
        self.assertEqual(decision["kind"], "associate")
        scores = self.store.entries("belief_score")[-1]["payload"]["scores"][0]
        self.assertTrue(all(c["voi"] == 0 for c in scores["capabilities"]))


class KnowledgeGapTests(unittest.TestCase):
    store: MindStore
    root: Path
    setUp = CuriosityTests.setUp
    belief = CuriosityTests.belief

    def test_gap_limits_normalization_and_low_importance_pruning(self) -> None:
        from curiosity import MAX_ACTIVE_GAPS, MAX_NEW_GAPS, gap_identity

        curiosity = Curiosity(self.store)
        for number in range(12):
            curiosity.discover(
                [
                    {
                        "subject": "Sir",
                        "attribute": f"aspect {number}-{j}",
                        "importance": 0.8,
                        "reason": "useful missing fact",
                    }
                    for j in range(4)
                ],
                number,
            )
        active = [b for b in self.store.beliefs() if b["source"] == "gap"]
        self.assertEqual(len(active), MAX_ACTIVE_GAPS)
        for number in range(12):
            self.assertLessEqual(
                sum(r["payload"]["round"] == number for r in self.store.entries("knowledge_gap")),
                MAX_NEW_GAPS,
            )
        before = len(self.store.beliefs())
        curiosity.discover(
            [{"subject": "Sir", "attribute": "low", "importance": 0.1, "reason": "minor"}], 13
        )
        self.assertEqual(len(self.store.beliefs()), before)
        self.assertEqual(gap_identity(" Sir ", " ASPECT "), gap_identity("sir", "aspect"))
        self.assertTrue(
            any(
                r["payload"]["reason"] == "low_importance"
                for r in self.store.entries("gap_retired")
            )
        )

    def test_gap_without_source_is_generated_and_semantically_reviewed(self) -> None:
        from inner_runtime import InnerLoop
        from test_inner_loop import answer

        for repeated in (False, True):
            with self.subTest(repeated=repeated):
                root = self.root / str(repeated)
                root.mkdir(mode=0o700)
                store = MindStore(root)
                phases: list[str] = []

                def model(
                    prompt: str, *, phases: list[str] = phases, repeated: bool = repeated
                ) -> Record:
                    context = json.loads(prompt)
                    output = answer(prompt)
                    if "proposed_question" in context:
                        phases.append("review")
                        return {
                            "text": json.dumps(
                                {"repeats_self_report": repeated, "reason": "semantic judgment"}
                            )
                        }
                    if "decision" in context:
                        phases.append("question")
                    else:
                        result = json.loads(output["text"])
                        result["knowledge_gaps"] = [
                            {
                                "subject": "Sir",
                                "attribute": "unknown",
                                "importance": 0.8,
                                "reason": "important missing fact",
                            }
                        ]
                        output["text"] = json.dumps(result)
                    return output

                loop = InnerLoop(
                    store,
                    model,
                    catalog=[],
                    max_rounds=2,
                    max_model_calls=4,
                    active_seconds=0,
                    idle_seconds=0,
                )
                loop.run()
                self.assertEqual(phases, ["question", "review"])
                self.assertEqual(len(store.entries("question_state")), 0 if repeated else 1)
                self.assertEqual(len(store.entries("question_rejected")), int(repeated))

    def test_generated_gap_question_is_not_asked_again_while_awaiting_sir(self) -> None:
        from inner_runtime import InnerLoop
        from test_inner_loop import answer

        curiosity = Curiosity(self.store)
        curiosity.discover(
            [{"subject": "Sir", "attribute": "unknown", "importance": 0.8, "reason": "missing"}],
            1,
        )
        context = curiosity.context([], [], 2)
        belief_id = context["relevance_candidates"][0]["belief_id"]
        decision = curiosity.decide([], context, [], 2)
        self.assertEqual(decision["kind"], "ask_sir")
        self.assertEqual(decision["belief_id"], belief_id)
        self.assertEqual(self.store.entries("question_state"), [])

        loop = InnerLoop(
            self.store,
            answer,
            catalog=[],
            max_rounds=2,
            max_model_calls=2,
            active_seconds=0,
            idle_seconds=0,
        )
        loop.generate_question(decision, 2)
        states = self.store.entries("question_state")
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["payload"]["question_id"], "scheduler:" + belief_id)
        self.assertEqual(states[0]["payload"]["state"], "ask_sir")

        # Reconstruct the scheduler: waiting must come from persisted production state.
        curiosity = Curiosity(self.store)
        context = curiosity.context([], [], 3)
        belief = next(b for b in context["beliefs"] if b["belief_id"] == belief_id)
        self.assertEqual(belief["attention"]["status"], "awaiting_sir")
        self.assertFalse(belief["attention"]["new_evidence"])
        self.assertNotIn(belief_id, [b["belief_id"] for b in context["relevance_candidates"]])
        decision = curiosity.decide([], context, [], 3)
        self.assertEqual(decision["kind"], "associate")
        self.assertEqual(len(self.store.entries("question_state")), 1)

    def test_unqueryable_gap_is_not_starved_by_more_important_queryable_gap(self) -> None:
        from curiosity import gap_identity

        curiosity = Curiosity(self.store)
        curiosity.discover(
            [
                {"subject": "Sir", "attribute": a, "importance": i, "reason": "missing"}
                for a, i in (("queryable", 0.9), ("unqueryable", 0.8))
            ],
            1,
        )
        catalog = capabilities.catalog(adapters=[])
        context = curiosity.context([], catalog, 2)
        rows = [
            {
                "belief_id": b["belief_id"],
                "capability_id": c["id"],
                "score": float(b["attribute"] == "queryable"),
                "reason": "semantic",
            }
            for b in context["relevance_candidates"]
            for c in catalog
        ]
        # No live source can answer either gap in this round. The more important
        # gap must not absorb the ask slot: the unqueryable one (no source judged
        # able to resolve it) still has to reach Sir.
        offline = [{**c, "available": False} for c in catalog]
        decision = curiosity.decide(rows, context, offline, 2)
        self.assertEqual(decision["kind"], "ask_sir")
        self.assertEqual(decision["belief_id"], gap_identity("Sir", "unqueryable"))
        # With a live source the queryable gap is executed first. That defers the
        # unqueryable gap by a round (the product gathers silent evidence before
        # interrupting Sir); it does not starve it, because the attempt is spent
        # and the ask becomes the next decision.
        live = curiosity.decide(rows, context, catalog, 2)
        self.assertEqual(live["kind"], "execute")
        self.assertEqual(live["belief_id"], gap_identity("Sir", "queryable"))

    def test_successfully_queried_belief_can_be_rechecked_after_decay(self) -> None:
        from datetime import timedelta

        belief = self.belief(importance=1.0)
        catalog = capabilities.catalog(adapters=[])
        catalog[0]["available"] = True
        action = self.store.append(
            "action",
            {
                "belief_id": belief["belief_id"],
                "belief_revision": belief["revision"],
                "capability_id": catalog[0]["id"],
            },
            source="test",
        )
        self.store.ingest(
            "tool_result",
            {
                "action_id": action["id"],
                "belief_id": belief["belief_id"],
                "result": {"status": "available"},
            },
            source=catalog[0]["id"],
        )
        rows = [
            {
                "belief_id": belief["belief_id"],
                "capability_id": c["id"],
                "score": float(c["id"] == catalog[0]["id"]),
                "reason": "semantic",
            }
            for c in catalog
        ]
        context = {
            "beliefs": [belief],
            "relevance_candidates": [belief],
            "cached_relevance": [],
            "as_of": (
                datetime.fromisoformat(belief["last_verified"]) + timedelta(days=31)
            ).isoformat(),
        }
        self.assertEqual(Curiosity(self.store).decide(rows, context, catalog, 2)["kind"], "execute")

    def test_multibyte_context_is_bounded_before_dispatch_with_complete_matrix(self) -> None:
        from context_limits import INNER_CONTEXT_BYTE_LIMIT
        from inner_runtime import InnerLoop
        from test_inner_loop import answer

        curiosity = Curiosity(self.store)
        for number in range(10):
            curiosity.discover(
                [
                    {
                        "subject": "Sir",
                        "attribute": f"aspect-{number}-{j}",
                        "importance": 0.8,
                        "reason": "未知" * 200,
                    }
                    for j in range(2)
                ],
                number,
            )
        received: list[int] = []

        def model(prompt: str) -> Record:
            context = json.loads(prompt)
            if "events" in context:
                received.append(len(prompt.encode("utf-8")))
            return answer(prompt)

        loop = InnerLoop(self.store, model, catalog=capabilities.catalog(adapters=[]))
        self.assertTrue(loop.step())
        self.assertEqual(len(received), 1)
        self.assertLessEqual(received[0], INNER_CONTEXT_BYTE_LIMIT)
        self.assertEqual(self.store.entries("validation_error"), [])

    def test_model_gap_is_deduplicated_queried_and_becomes_observed(self) -> None:
        from inner_runtime import InnerLoop
        from test_inner_loop import answer

        executed: list[str] = []

        def model(prompt: str) -> Record:
            context = json.loads(prompt)
            output = json.loads(answer(prompt)["text"])
            output["knowledge_gaps"] = [
                {
                    "subject": "Sir",
                    "attribute": "unfamiliar aspect",
                    "importance": 0.8,
                    "reason": "missing useful evidence",
                }
            ]
            for row in output["relevance"]:
                row["score"] = 0.9
            output["belief_updates"] = [
                {
                    "origin_id": e["id"],
                    "belief_id": e["payload"]["data"]["belief_id"],
                    "subject": "Sir",
                    "attribute": "freely renamed aspect",
                    "value": "supported fact",
                    "importance": 0.8,
                    "conflict": False,
                }
                for e in context["pending_observations"]
            ]
            return {"text": json.dumps(output)}

        def execute(capability: Record) -> Record:
            executed.append(capability["id"])
            return {"value": "supported fact", "synthetic": False}

        loop = InnerLoop(
            self.store,
            model,
            catalog=capabilities.fixture_catalog(),
            executor=execute,
            max_rounds=3,
            active_seconds=0,
            idle_seconds=0,
        )
        self.assertTrue(loop.step())
        gaps = self.store.beliefs()
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["source"], "gap")
        self.assertIsNone(gaps[0]["value"])
        self.assertEqual(confidence(gaps[0], datetime.now(UTC)), 0)
        self.assertTrue(loop.step())
        self.assertEqual(executed, ["location_track"])
        score = self.store.entries("belief_score")[-1]["payload"]["scores"][0]
        self.assertAlmostEqual(score["capabilities"][0]["voi"], 0.72)
        self.assertTrue(loop.step())
        self.assertEqual(len(self.store.beliefs()), 1)
        self.assertEqual(self.store.beliefs()[0]["source"], "observed")
        self.assertEqual(self.store.beliefs()[0]["belief_id"], gaps[0]["belief_id"])
        self.assertEqual(len(self.store.entries("knowledge_gap")), 1)


class ForbiddenTermsTests(unittest.TestCase):
    def test_inner_source_text_has_no_acceptance_answer_terms(self) -> None:
        import capabilities as cap
        import inner_runtime
        import inner_voice

        text = "\n".join(
            [
                inner_runtime.INSTRUCTIONS,
                inner_voice.SYSTEM_PROMPT,
                inner_runtime.QUESTION_INSTRUCTIONS,
                __import__("curiosity").INSTRUCTIONS,
                json.dumps(cap.fixture_catalog(include_sleep=True), ensure_ascii=False),
                json.dumps(cap._REAL, ensure_ascii=False),
                json.dumps(["Sir 说他住在天水", "Sir 说他最近每天睡得很早"], ensure_ascii=False),
            ]
        )
        for term in ("住址", "定位", "核实位置", "老家"):
            self.assertNotIn(term, text)


if __name__ == "__main__":
    unittest.main()
