"""Migrated offline regression; fresh scratch fixtures, no model or live DB.
Constructor/path admission is not covered by the injected bootstrap.
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import goal_tracker as protocol


def _scratch():
    root = Path.home() / ".jarvis"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o777 != 0o700:
        raise AssertionError(f"test data root is not 0700: {root}")
    return root


class FixtureMindStore:
    """Fresh DELETE-journal entries schema, never a live MindStore."""

    def __init__(self, root):
        self.path = root / "mind.db"
        self.path.touch(mode=0o600)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE entries (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL, created_at TEXT NOT NULL,
                    source TEXT NOT NULL, payload TEXT NOT NULL, refs TEXT NOT NULL);
                CREATE TRIGGER no_update BEFORE UPDATE ON entries
                    BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER no_delete BEFORE DELETE ON entries
                    BEGIN SELECT RAISE(ABORT,'append only'); END;
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def count(self, category):
        with self.connection() as db:
            return db.execute(
                "SELECT count(*) FROM entries WHERE category=?", (category,)
            ).fetchone()[0]

    def entries(self, category):
        with self.connection() as db:
            return [
                {"payload": json.loads(row["payload"])}
                for row in db.execute(
                    "SELECT payload FROM entries WHERE category=? ORDER BY seq", (category,)
                )
            ]


def _private_fixture_init(store, root, filename):
    # Narrow test bootstrap; real PrivateStore.connection handles all operations.
    store.root = root
    store.path = root / filename
    store.db_path = store.path
    store.path.touch(mode=0o600)


def make_goals(root, mind):
    from goal_tracker import GoalTracker
    from sir_profile import PrivateStore

    # Bootstrap the real schema once; no global patch races between threads.
    with patch.object(PrivateStore, "__init__", _private_fixture_init):
        return GoalTracker(root, mind)


def reopen_goals(root, mind):
    from goal_tracker import GoalTracker

    tracker = GoalTracker.__new__(GoalTracker)
    tracker.root = tracker.data_root = root
    tracker.path = tracker.db_path = root / "goals.db"
    tracker.mind_store = mind
    return tracker


class GoalTests(unittest.TestCase):
    def test_set_goal_roundtrip(self):
        self.assertIsNotNone(
            importlib.util.find_spec("goal_tracker"), "goal_tracker module missing"
        )
        with tempfile.TemporaryDirectory(prefix="session-goal-", dir=_scratch()) as folder:
            root = Path(folder)
            ms = FixtureMindStore(root)
            gt = make_goals(root, ms)
            criteria = ["测试通过", "格式支持"]
            gid = gt.set_goal("完成目标", criteria)
            criteria.append("external mutation")
            status = make_goals(root, ms).get_status(gid)
            self.assertEqual(status["goal_id"], gid)
            self.assertEqual(status["goal"], "完成目标")
            self.assertEqual(status["progress"], 0.0)
            self.assertFalse(status["completed"])
            self.assertEqual(status["success_criteria"], ["测试通过", "格式支持"])
            self.assertEqual(status["notes"], [])
            self.assertEqual(ms.entries("goal_set")[-1]["payload"]["goal_id"], gid)

    def test_progress_updates_and_completion(self):
        from goal_tracker import GoalTracker

        for method in ["update_progress", "list_active_goals", "complete"]:
            self.assertTrue(hasattr(GoalTracker, method), f"{method} missing")
        with tempfile.TemporaryDirectory(prefix="session-goal-", dir=_scratch()) as folder:
            root = Path(folder)
            ms = FixtureMindStore(root)
            gt = make_goals(root, ms)
            gid = gt.set_goal("任务")
            self.assertEqual(
                gt.update_progress(gid, 0.5, "完成一半"),
                {"goal_id": gid, "progress": 0.5, "completed": False},
            )
            self.assertEqual(gt.get_status(gid)["notes"], ["完成一半"])
            self.assertEqual(len(gt.list_active_goals()), 1)
            self.assertTrue(gt.update_progress(gid, 1.2, "完成")["completed"])
            self.assertEqual(gt.get_status(gid)["progress"], 1.0)
            self.assertEqual(gt.list_active_goals(), [])
            with self.assertRaises(ValueError):
                gt.update_progress(gid, 0.4)
            second = gt.set_goal("任务二")
            self.assertEqual(
                gt.complete(second, "已验证"),
                {"goal_id": second, "completed": True, "summary": "已验证"},
            )
            self.assertEqual(gt.get_status(second)["notes"], ["已验证"])
            before = ms.count("goal_completed")
            self.assertEqual(gt.complete(second)["summary"], "已验证")
            self.assertEqual(ms.count("goal_completed"), before)
            self.assertEqual(ms.count("goal_progress"), 2)
            for progress in [-1, True, float("nan"), float("inf"), "0.5"]:
                with self.assertRaises(ValueError):
                    gt.update_progress(second, progress)
            for method in [
                lambda: gt.get_status("missing"),
                lambda: gt.update_progress("missing", 0.5),
                lambda: gt.complete("missing"),
            ]:
                with self.assertRaises(KeyError):
                    method()

    def test_goal_outbox_failure_rolls_back(self):
        # Mutation and durable intent remain atomic in goals.db. Mind delivery
        # failures are separately covered by WALTests and must retain intent.
        with tempfile.TemporaryDirectory(prefix="session-goal-", dir=_scratch()) as folder:
            root = Path(folder)
            ms = FixtureMindStore(root)
            gt = make_goals(root, ms)
            gid = gt.set_goal("任务")
            with gt.connection(write=True) as db:
                db.execute(
                    "CREATE TRIGGER reject_goals BEFORE INSERT ON goal_audit_outbox "
                    "BEGIN SELECT RAISE(ABORT,'test failure'); END"
                )
            import sqlite3

            for action in [
                lambda: gt.set_goal("失败"),
                lambda: gt.update_progress(gid, 0.8),
                lambda: gt.complete(gid),
            ]:
                with self.assertRaises(sqlite3.IntegrityError):
                    action()
            self.assertEqual(len(gt.list_active_goals()), 1)
            self.assertEqual(gt.get_status(gid)["progress"], 0)
            self.assertEqual(ms.count("goal_set"), 1)

    def test_concurrent_goal_notes_preserved(self):
        from concurrent.futures import ThreadPoolExecutor

        with tempfile.TemporaryDirectory(prefix="session-goal-", dir=_scratch()) as folder:
            root = Path(folder)
            ms = FixtureMindStore(root)
            gt = make_goals(root, ms)
            gid = gt.set_goal("并发任务")
            notes = [f"note-{i}" for i in range(12)]

            def update(note):
                return reopen_goals(root, ms).update_progress(gid, 0.5, note)

            with ThreadPoolExecutor(max_workers=4) as executor:
                rows = list(executor.map(update, notes))
            self.assertEqual(len(rows), 12)
            self.assertEqual(set(gt.get_status(gid)["notes"]), set(notes))
            self.assertEqual(len(gt.get_status(gid)["notes"]), 12)
            self.assertEqual(ms.count("goal_progress"), 12)

    def test_goal_rejects_invalid_input_and_accepts_wal(self):
        with tempfile.TemporaryDirectory(prefix="session-goal-", dir=_scratch()) as folder:
            root = Path(folder)
            ms = FixtureMindStore(root)
            gt = make_goals(root, ms)
            for args in [("", []), ("  ", []), ("goal", "wrong"), ("goal", [1]), ("goal", [""])]:
                with self.assertRaises(ValueError):
                    gt.set_goal(*args)
            self.assertEqual(ms.count("goal_set"), 0)
            import sqlite3
            from contextlib import closing

            with closing(sqlite3.connect(ms.path)) as db:
                db.execute("PRAGMA journal_mode=WAL")
            # New implementation supports WAL via outbox protocol; set_goal must
            # succeed and produce exactly one audit entry.
            identity = gt.set_goal("must succeed in WAL")
            self.assertIsNotNone(identity)
            self.assertEqual(len(gt.list_active_goals()), 1)
            self.assertEqual(ms.count("goal_set"), 1)


class WALTests(unittest.TestCase):
    def setUp(self):
        from inner_loop import MindStore

        self.folder = tempfile.TemporaryDirectory(prefix="goal-wal-", dir=_scratch())
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.mind = MindStore(self.root)
        self.goals = protocol.GoalTracker(self.root, self.mind)

    def _seed_legacy_goal(self):
        # Seed existing goals without depending on the broken audit write path.
        now = datetime.now(UTC).isoformat()
        with self.goals.connection(write=True) as db:
            db.execute(
                "INSERT INTO goals VALUES(?,?,?,?,?,?,?,?,?)",
                ("legacy", "existing", 0, 0, "[]", "[]", "", now, now),
            )
        return "legacy"

    def test_reconcile_uses_full_sync_only_inside_mind_transaction(self):
        # reconcile_audits opens its own mind connection; the trace must be
        # on that connection, not an unrelated one opened afterwards. Wrap
        # MindStore.connection to attach a trace callback to every connection
        # it yields, then trigger a set_goal which calls _try_reconcile.
        observed: list[str] = []
        original_connection = self.mind.connection

        @contextmanager
        def traced_connection():
            with original_connection() as db:
                db.set_trace_callback(observed.append)
                yield db

        self.mind.connection = traced_connection

        self.goals.set_goal("durable sync")
        statements = [statement.strip().upper() for statement in observed]
        sync = [s for s in statements if s.startswith("PRAGMA SYNCHRONOUS")]
        self.assertIn("PRAGMA SYNCHRONOUS=FULL", sync)

    def test_wal_set_goal(self):
        try:
            identity = self.goals.set_goal("WAL creation")
        except ValueError as exc:
            self.fail(f"WAL goal creation rejected: {exc}")
        self.assertEqual(self.goals.get_status(identity)["progress"], 0)
        self.assertEqual(self.mind.count("goal_set"), 1)
        with self.mind.connection() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_wal_update_progress(self):
        identity = self._seed_legacy_goal()
        try:
            result = self.goals.update_progress(identity, 0.5, "half")
        except ValueError as exc:
            self.fail(f"WAL progress rejected: {exc}")
        self.assertEqual(result["progress"], 0.5)
        self.assertEqual(self.mind.count("goal_progress"), 1)

    def test_wal_complete(self):
        identity = self._seed_legacy_goal()
        try:
            self.goals.complete(identity, "done")
        except ValueError as exc:
            self.fail(f"WAL completion rejected: {exc}")
        self.assertTrue(self.goals.get_status(identity)["completed"])
        self.goals.complete(identity, "done")
        self.assertEqual(self.mind.count("goal_completed"), 1)

    def test_audit_outage_persists_all_mutations_and_reconciles_on_reopen(self):
        with self.mind.connection() as db:
            db.execute(
                "CREATE TRIGGER reject_goals BEFORE INSERT ON entries "
                "WHEN NEW.category LIKE 'goal_%' "
                "BEGIN SELECT RAISE(ABORT,'offline'); END"
            )
        identity = self.goals.set_goal("durable")
        self.goals.update_progress(identity, 0.5, "half")
        self.goals.complete(identity, "done")
        self.assertTrue(self.goals.get_status(identity)["completed"])
        with self.goals.connection() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM goal_audit_outbox WHERE delivered=0").fetchone()[
                    0
                ],
                3,
            )
        self.assertEqual(self.mind.count("goal_completed"), 0)
        with self.mind.connection() as db:
            db.execute("DROP TRIGGER reject_goals")
        reopened = protocol.GoalTracker(self.root, self.mind)
        self.assertEqual(reopened.reconcile_audits(), 0)
        for category in ("goal_set", "goal_progress", "goal_completed"):
            self.assertEqual(self.mind.count(category), 1)
        with reopened.connection() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM goal_audit_outbox WHERE delivered=0").fetchone()[
                    0
                ],
                0,
            )

    def test_mind_commit_before_ack_replays_without_duplicate(self):
        identity = self.goals.set_goal("once")
        # Simulate lost acknowledgement after a durable mind commit.
        with self.goals.connection(write=True) as db:
            db.execute("UPDATE goal_audit_outbox SET delivered=0")
        protocol.GoalTracker(self.root, self.mind).reconcile_audits()
        self.assertEqual(self.mind.count("goal_set"), 1)
        self.assertEqual(self.goals.get_status(identity)["goal"], "once")

    def test_wal_concurrent_updates_keep_notes_and_one_audit_per_mutation(self):
        from concurrent.futures import ThreadPoolExecutor

        identity = self.goals.set_goal("concurrent WAL")
        notes = [f"wal-note-{i}" for i in range(16)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(lambda note: self.goals.update_progress(identity, 0.5, note), notes)
            )
        self.assertEqual(len(results), 16)
        self.assertEqual(set(self.goals.get_status(identity)["notes"]), set(notes))
        self.assertEqual(self.mind.count("goal_progress"), 16)
        self.assertEqual(self.goals.reconcile_audits(), 0)

    def test_legacy_completion_does_not_duplicate(self):
        identity = self._seed_legacy_goal()
        with self.goals.connection(write=True) as db:
            db.execute(
                "UPDATE goals SET completed=1,progress=1,summary='legacy done' WHERE goal_id=?",
                (identity,),
            )
        self.mind.append(
            "goal_completed", {"goal_id": identity, "summary": "legacy done"}, source="goal-tracker"
        )
        self.assertEqual(self.goals.complete(identity)["summary"], "legacy done")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.goals.complete(identity, "conflict")
        self.assertEqual(self.mind.count("goal_completed"), 1)

    def test_process_crash_boundaries_recover_once(self):
        # Real process death, not exception rollback: first after local commit,
        # then after mind COMMIT but before the local delivery acknowledgement.
        child = """
import os, sys
from pathlib import Path
from contextlib import contextmanager
from inner_loop import MindStore
from goal_tracker import GoalTracker
root, stage = Path(sys.argv[1]), sys.argv[2]
mind = MindStore(root)
goals = GoalTracker(root, mind)
if stage == "local":
    goals._try_reconcile = lambda: os._exit(73)
else:
    original = mind.connection
    @contextmanager
    def crash_after_commit():
        with original() as db:
            yield db
        os._exit(74)
    mind.connection = crash_after_commit
goals.set_goal("hard-crash")
"""
        from inner_loop import MindStore

        for stage, exit_code in (("local", 73), ("mind", 74)):
            with (
                self.subTest(stage=stage),
                tempfile.TemporaryDirectory(prefix="goal-crash-", dir=_scratch()) as folder,
            ):
                root = Path(folder)
                result = subprocess.run(
                    [sys.executable, "-c", child, str(root), stage],
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
                self.assertEqual(result.returncode, exit_code, result.stderr)
                mind = MindStore(root)
                with sqlite3.connect(root / "goals.db") as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM goals").fetchone()[0], 1)
                    self.assertEqual(
                        db.execute("SELECT delivered FROM goal_audit_outbox").fetchone()[0], 0
                    )
                self.assertEqual(mind.count("goal_set"), int(stage == "mind"))
                recovered = protocol.GoalTracker(root, mind)
                self.assertEqual(mind.count("goal_set"), 1)
                self.assertEqual(recovered.reconcile_audits(), 0)
                with recovered.connection() as db:
                    self.assertEqual(
                        db.execute("SELECT delivered FROM goal_audit_outbox").fetchone()[0], 1
                    )

    def test_envelope_conflict_is_not_acknowledged(self):
        self.goals.set_goal("immutable")
        with self.goals.connection(write=True) as db:
            db.execute("UPDATE goal_audit_outbox SET delivered=0, payload='{}'")
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.goals.reconcile_audits()
        with self.goals.connection() as db:
            self.assertEqual(db.execute("SELECT delivered FROM goal_audit_outbox").fetchone()[0], 0)
        self.assertEqual(self.mind.count("goal_set"), 1)


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
        self.assertEqual(report["module"], "goal_tracker")
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
                protocol.sqlite3, "connect", side_effect=AssertionError("unexpected protocol use")
            ),
        ):
            first = hook()
            first["known_limitations"].append("caller-only")
            first["improvement_directions"].clear()
            second = hook()
        self.assertNotIn("caller-only", second["known_limitations"])
        self.assertTrue(second["improvement_directions"])


class AdmissionTests(unittest.TestCase):
    def test_production_constructor_rejects_host_scratch_without_writes(self):
        from goal_tracker import GoalTracker

        # The product rejects data roots under the host Hermes tree. Pin that
        # boundary explicitly (HERMES_HOME) instead of assuming the ambient TMPDIR
        # already lives inside it: an isolated TMPDIR such as ~/.tmp-pytest does
        # not, and the assertion would silently stop testing anything.
        with tempfile.TemporaryDirectory(
            prefix="host-hermes-", dir=Path(os.environ["TMPDIR"]).resolve()
        ) as host:
            host_root = Path(host).resolve()
            with tempfile.TemporaryDirectory(prefix="goal-admission-", dir=host_root) as folder:
                root = Path(folder).resolve()
                from types import SimpleNamespace

                with patch.dict(os.environ, {"HERMES_HOME": str(host_root)}):
                    with self.assertRaisesRegex(ValueError, "host Hermes"):
                        GoalTracker(root, SimpleNamespace(path=root / "mind.db"))
                self.assertEqual(list(root.iterdir()), [])

    def test_registry_detects_real_module_hook(self):
        from integration_registry import REGISTRY

        self.assertTrue(REGISTRY._has_self_evaluate(REGISTRY.get("goal_tracker")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
