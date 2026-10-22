"""Hypothesis scoring/lifecycle tests on isolated synthetic records."""

from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hypothesis_engine import HypothesisEngine


class HypothesisEngineTests(unittest.TestCase):
    def test_evidence_retries_do_not_inflate_confidence_or_execute_conditions(self):
        with tempfile.TemporaryDirectory(prefix="hypothesis-unit-") as folder:
            root = Path(folder)
            with patch("product_home.validate_root", return_value=root):
                engine = HypothesisEngine(root)
            identity = engine.propose(
                "habit",
                "fictional-walk",
                "walk after lunch",
                0.5,
                "caller checks observation",
                "caller checks counterexample",
            )
            first = engine.update_evidence(identity, supporting_ids=["e1", "e1"])
            retry = engine.update_evidence(identity, supporting_ids=["e1"])
            self.assertAlmostEqual(first["confidence"], 0.58)
            self.assertEqual(first["confidence"], retry["confidence"])
            self.assertEqual(retry["evidence_for"], ["e1"])
            self.assertEqual(retry["status"], "pending")
            with self.assertRaises(ValueError):
                engine.update_evidence(identity, opposing_ids=["e1"])

    def test_confirmation_requires_support_and_projects_inferred_fact(self):
        with tempfile.TemporaryDirectory(prefix="hypothesis-unit-") as folder:
            root = Path(folder)
            with patch("product_home.validate_root", return_value=root):
                engine = HypothesisEngine(root)
            identity = engine.propose(
                "habit",
                "fictional-tea",
                "afternoon tea",
                0.9,
                "caller checked",
                "caller counterexample",
            )
            self.assertFalse(engine.try_confirm(identity))
            updated = engine.update_evidence(identity, supporting_ids=["fixture-support"])
            self.assertEqual(updated["status"], "confirmed")
            fact = engine.profile.get("habit", "fictional-tea")[0]
            self.assertEqual(fact["source"], "inferred")
            self.assertEqual(fact["evidence_ids"], ["fixture-support"])


class MigratedLegacyTests(unittest.TestCase):
    """Migrated scratch/jarvis_profile_tests.py; synthetic data, scratch-only I/O."""

    def setUp(self):
        import tempfile
        from unittest.mock import patch

        self.temp = tempfile.TemporaryDirectory(prefix="legacy-profile-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        validation = patch("product_home.validate_root", return_value=self.root)
        validation.start()
        self.addCleanup(validation.stop)

    def test_hypothesis_lifecycle_dedup_and_atomic_profile(self):
        import importlib.util

        self.assertIsNotNone(
            importlib.util.find_spec("hypothesis_engine"), "HypothesisEngine missing"
        )
        from hypothesis_engine import HypothesisEngine
        from sir_profile import SirProfile

        he = HypothesisEngine(self.root)
        hid = he.propose("habit", "active_time", "深夜活跃", 0.6, "14 days", "direct report")
        self.assertEqual(len(he.list_pending()), 1)
        he.update_evidence(hid, supporting_ids=["a", "b", "c", "d"])
        self.assertEqual(len(he.list_confirmed()), 1)
        self.assertTrue(he.try_confirm(hid))
        fact = SirProfile(self.root).get()[0]
        self.assertGreater(fact["confidence"], 0.85)
        self.assertEqual(fact["source"], "inferred")
        self.assertEqual(fact["evidence_ids"], ["a", "b", "c", "d"])
        he.update_evidence(hid, supporting_ids=["a", "b", "c"])
        self.assertEqual(he.list_confirmed()[0]["confidence"], fact["confidence"])
        self.assertEqual(os.stat(he.path).st_mode & 0o777, 0o600)

    def test_hypothesis_conflict_and_explicit_refutation(self):
        from hypothesis_engine import HypothesisEngine
        from sir_profile import SirProfile

        sp = SirProfile(self.root)
        sp.upsert("habit", "sleep", "早睡", 1.0, "self_report")
        he = HypothesisEngine(self.root)
        hid = he.propose("habit", "sleep", "晚睡", 0.6, "observed", "self report")
        he.update_evidence(hid, supporting_ids=["a", "b", "c", "d"])
        self.assertFalse(he.try_confirm(hid))
        self.assertEqual(len(he.list_pending()), 1)
        self.assertEqual(len(sp.get_conflicts()), 1)
        self.assertFalse(he.try_refute(hid, "no counter evidence yet"))
        he.update_evidence(hid, opposing_ids=["self-report-entry"])
        self.assertTrue(he.try_refute(hid, "direct counter evidence"))
        self.assertEqual(he.get(hid)["status"], "refuted")
        self.assertEqual(sp.get()[0]["value"], "早睡")

    def test_hypothesis_negative_evidence_expiry_and_terminal(self):
        from hypothesis_engine import HypothesisEngine

        he = HypothesisEngine(self.root)
        hid = he.propose("habit", "x", "claim", 0.6, "yes", "no")
        he.update_evidence(hid, opposing_ids=["a", "b"])
        self.assertLess(he.get(hid)["confidence"], 0.6)
        self.assertEqual(he.get(hid)["status"], "refuted")
        he.update_evidence(hid, supporting_ids=["c", "d", "e", "f", "g"])
        self.assertFalse(he.try_confirm(hid))
        stale = he.propose("habit", "y", "weak", 0.4, "yes", "no")
        high = he.propose("habit", "z", "strong", 0.8, "yes", "no")
        with he.connection(write=True) as db:
            db.execute("UPDATE hypotheses SET expires_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(he.expire_stale(), 1)
        self.assertEqual(he.get(stale)["status"], "insufficient_evidence")
        self.assertEqual(he.get(high)["status"], "pending")

    def test_confirmed_claim_reopens_without_remaining_a_fact(self):
        from hypothesis_engine import HypothesisEngine
        from sir_profile import SirProfile

        he = HypothesisEngine(self.root)
        hid = he.propose("habit", "x", "claim", 0.6, "yes", "no")
        he.update_evidence(hid, supporting_ids=["a", "b", "c", "d"])
        he.update_evidence(hid, opposing_ids=["opposing-entry"])
        self.assertEqual(he.get(hid)["status"], "pending")
        self.assertEqual(SirProfile(self.root).get_high_confidence(), [])
        he.update_evidence(hid, supporting_ids=["e", "f"])
        self.assertEqual(he.get(hid)["status"], "confirmed")
        self.assertEqual(
            SirProfile(self.root).get()[0]["evidence_ids"], ["a", "b", "c", "d", "e", "f"]
        )
        he.try_refute(hid, "explicit opposite")
        self.assertEqual(SirProfile(self.root).get_high_confidence(), [])

    def test_confirmed_claim_keeps_new_evidence(self):
        from hypothesis_engine import HypothesisEngine
        from sir_profile import SirProfile

        he = HypothesisEngine(self.root)
        hid = he.propose("habit", "x", "claim", 0.6, "yes", "no")
        he.update_evidence(hid, supporting_ids=["a", "b", "c", "d"])
        he.update_evidence(hid, supporting_ids=["e"])
        self.assertEqual(SirProfile(self.root).get()[0]["evidence_ids"], ["a", "b", "c", "d", "e"])

    def test_validation_duplicates_batching_and_unknown_ids(self):
        from hypothesis_engine import HypothesisEngine
        from sir_profile import SirProfile

        he = HypothesisEngine(self.root)
        p = SirProfile(self.root)
        for score in [float("nan"), float("inf"), -1, 1.1, True]:
            with self.assertRaises(ValueError):
                p.upsert("identity", "x", "x", score, "inferred")
        with self.assertRaises(ValueError):
            p.upsert("unknown", "x", "x", 0.5, "inferred")
        with self.assertRaises(KeyError):
            he.try_confirm("missing")
        a = he.propose("habit", "a", "claim", 0.6, "yes", "no")
        b = he.propose("habit", "b", "claim", 0.6, "yes", "no")
        he.update_evidence(a, supporting_ids=["a", "b", "c"])
        for entry in ["a", "b", "c"]:
            he.update_evidence(b, supporting_ids=[entry])
        self.assertEqual(he.get(a)["confidence"], he.get(b)["confidence"])
        before = he.get(a)
        with self.assertRaises(ValueError):
            he.update_evidence(a, opposing_ids=["a"])
        self.assertEqual(he.get(a), before)
        self.assertFalse(he.try_confirm(a))
        no_evidence = he.propose("habit", "c", "claim", 0.99, "yes", "no")
        self.assertFalse(he.try_confirm(no_evidence))

    def test_multidatabase_confirmation_rolls_back_on_error(self):
        from hypothesis_engine import HypothesisEngine
        from sir_profile import SirProfile

        he = HypothesisEngine(self.root)
        hid = he.propose("habit", "x", "claim", 0.6, "yes", "no")
        with he.connection(write=True) as db:
            db.execute("""CREATE TRIGGER fail_confirm BEFORE UPDATE ON hypotheses
                WHEN NEW.status='confirmed' BEGIN SELECT RAISE(ABORT,'probe'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            he.update_evidence(hid, supporting_ids=["a", "b", "c", "d"])
        self.assertEqual(SirProfile(self.root).get(), [])
        self.assertEqual(he.get(hid)["confidence"], 0.6)
        self.assertEqual(he.get(hid)["evidence_for"], [])


class SelfEvaluationTests(unittest.TestCase):
    def hook(self):
        import importlib

        module = importlib.import_module("hypothesis_engine")
        return module, object.__new__(module.HypothesisEngine).self_evaluate

    def test_honest_schema_identity_algorithm_and_fresh_timestamp(self):
        import json
        from datetime import UTC, datetime

        _module, hook = self.hook()
        before = datetime.now(UTC)
        report = hook()
        after = datetime.now(UTC)
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
        self.assertEqual(report["module"], "hypothesis_engine")
        self.assertIn("weights", report["current_version"].lower())
        self.assertIn("caller", " ".join(report["known_limitations"]).lower())
        self.assertTrue(report["domain"].strip())
        for key in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[key], list)
            self.assertTrue(report[key])
            self.assertTrue(all(isinstance(item, str) and item.strip() for item in report[key]))
        stamp = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, after)
        json.dumps(report, allow_nan=False)

    def test_metadata_is_io_free_without_initialized_store_or_credentials(self):
        from pathlib import Path
        import sqlite3
        import socket
        import subprocess
        import os
        from unittest.mock import patch

        _module, hook = self.hook()
        with (
            patch("builtins.open", side_effect=AssertionError("file access")),
            patch.object(Path, "open", side_effect=AssertionError("path access")),
            patch.object(sqlite3, "connect", side_effect=AssertionError("database access")),
            patch.object(socket, "socket", side_effect=AssertionError("network access")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("process access")),
            patch.object(os, "open", side_effect=AssertionError("descriptor access")),
        ):
            first = hook()
            first["known_limitations"].append("mutation sentinel")
            first["improvement_directions"].clear()
            second = hook()
        self.assertNotIn("mutation sentinel", second["known_limitations"])
        self.assertTrue(second["improvement_directions"])
        self.assertEqual(second["module"], "hypothesis_engine")
