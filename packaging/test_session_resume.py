"""Migrated offline regression; fresh scratch fixtures, no model or live DB.
Constructor/path admission is not covered by the injected bootstrap.
"""

import importlib.util
import json
import os
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import session_resume as protocol


def _scratch():
    root = Path.home() / ".jarvis"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o777 != 0o700:
        raise AssertionError(f"test data root is not 0700: {root}")
    return root


def make_session(root, mind):
    from session_resume import SessionResume

    # Constructor is deliberately NOT under test: it rejects host scratch roots.
    session = SessionResume.__new__(SessionResume)
    session.data_root = root
    session.mind_store = mind
    session.sessions_dir = root / "sessions"
    session.sessions_dir.mkdir(mode=0o700, exist_ok=True)
    session.current_session_id = None
    session.current_context = None
    session._lock = threading.RLock()
    return session


class SessionTests(unittest.TestCase):
    def test_save_resume_roundtrip(self):
        self.assertIsNotNone(
            importlib.util.find_spec("session_resume"), "session_resume module missing"
        )
        with tempfile.TemporaryDirectory(prefix="session-goal-", dir=_scratch()) as folder:
            root = Path(folder)
            ms = SimpleNamespace(path=root / "mind.db")
            sr = make_session(root, ms)
            context = {
                "current_task": "testing",
                "progress": 0.5,
                "pending_items": ["a", "b"],
                "think_history": [{"text": "保留原文"}],
            }
            self.assertEqual(sr.save_state("test-1", context), "test-1")
            context["pending_items"].append("external mutation")
            result = make_session(root, ms).resume("test-1")
            self.assertTrue(result["resumed"])
            self.assertEqual(result["context"]["pending_items"], ["a", "b"])
            self.assertEqual(result["context"]["think_history"], [{"text": "保留原文"}])

    def test_list_checkpoint_and_invalid_ids(self):
        from session_resume import SessionResume

        self.assertTrue(hasattr(SessionResume, "list_sessions"), "session listing missing")
        self.assertTrue(hasattr(SessionResume, "auto_save_checkpoint"), "checkpoint hook missing")
        with tempfile.TemporaryDirectory(prefix="session-goal-", dir=_scratch()) as folder:
            root = Path(folder)
            sr = make_session(root, SimpleNamespace(path=root / "mind.db"))
            self.assertIsNone(sr.auto_save_checkpoint())
            context = {
                "current_task": "one",
                "progress": 0.1,
                "pending_items": [],
                "think_history": [],
            }
            sr.save_state("one", context)
            sr.save_state("two", {**context, "current_task": "two"})
            self.assertEqual([x["session_id"] for x in sr.list_sessions(1)], ["two"])
            self.assertEqual(sr.list_sessions(0), [])
            sr.current_context["progress"] = 0.8
            self.assertEqual(sr.auto_save_checkpoint(), "two")
            self.assertEqual(sr.resume("two")["context"]["progress"], 0.8)
            self.assertFalse(sr.resume("missing")["resumed"])
            for session_id in ["../outside", "/tmp/no", "", "a/b"]:
                with self.assertRaises(ValueError):
                    sr.save_state(session_id, context)
            for progress in [float("nan"), float("inf"), -0.1, 1.1, True]:
                with self.assertRaises(ValueError):
                    sr.save_state("bad", {**context, "progress": progress})
            for limit in [-1, True, 1.1]:
                with self.assertRaises(ValueError):
                    sr.list_sessions(limit)
            corrupt = sr.sessions_dir / "corrupt.json"
            corrupt.write_text("{")
            corrupt.chmod(0o600)
            self.assertEqual(len(sr.list_sessions()), 2)
            with self.assertRaises(ValueError):
                sr.resume("corrupt")

    def test_checkpoint_write_failure_preserves_previous(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory(prefix="session-goal-", dir=_scratch()) as folder:
            root = Path(folder)
            sr = make_session(root, SimpleNamespace(path=root / "mind.db"))
            context = {
                "current_task": "safe",
                "progress": 0.2,
                "pending_items": [],
                "think_history": [],
            }
            sr.save_state("safe", context)
            before = (sr.sessions_dir / "safe.json").read_bytes()
            with patch(
                "session_resume.os.replace", side_effect=OSError("injected replace failure")
            ):
                with self.assertRaises(OSError):
                    sr.save_state("safe", {**context, "progress": 0.8})
            self.assertEqual((sr.sessions_dir / "safe.json").read_bytes(), before)
            self.assertEqual(list(sr.sessions_dir.glob(".checkpoint-*")), [])
            self.assertEqual(sr.current_context["progress"], 0.2)

    def test_checkpoint_private_paths(self):
        import stat

        with tempfile.TemporaryDirectory(prefix="session-goal-", dir=_scratch()) as folder:
            root = Path(folder)
            sr = make_session(root, SimpleNamespace(path=root / "mind.db"))
            context = {
                "current_task": "safe",
                "progress": 0.2,
                "pending_items": [],
                "think_history": [],
            }
            sr.save_state("safe", context)
            path = sr.sessions_dir / "safe.json"
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(sr.sessions_dir.stat().st_mode), 0o700)
            (sr.sessions_dir / "link.json").symlink_to(path)
            for action in [lambda: sr.resume("link"), lambda: sr.save_state("link", context)]:
                with self.assertRaises(ValueError):
                    action()
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                sr.resume("safe")


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
        self.assertEqual(report["module"], "session_resume")
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
                protocol.SessionResume,
                "save_state",
                side_effect=AssertionError("unexpected protocol use"),
            ),
        ):
            first = hook()
            first["known_limitations"].append("caller-only")
            first["improvement_directions"].clear()
            second = hook()
        self.assertNotIn("caller-only", second["known_limitations"])
        self.assertTrue(second["improvement_directions"])


class WALSessionTests(unittest.TestCase):
    def test_real_wal_mind_allows_save_resume_and_checkpoint(self):
        from inner_loop import MindStore

        with tempfile.TemporaryDirectory(prefix="session-wal-", dir=_scratch()) as folder:
            root = Path(folder)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            mind = MindStore(root)
            session = protocol.SessionResume(root, mind)
            context = {
                "current_task": "WAL",
                "progress": 0.5,
                "pending_items": [],
                "think_history": [],
            }
            self.assertEqual(session.save_state("wal", context), "wal")
            reopened = protocol.SessionResume(root, mind)
            self.assertEqual(reopened.resume("wal")["context"], context)
            self.assertEqual(reopened.auto_save_checkpoint(), "wal")
            with mind.connection() as db:
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")


class AdmissionTests(unittest.TestCase):
    def test_production_constructor_rejects_host_scratch_without_writes(self):
        from session_resume import SessionResume

        # The product rejects data roots under the host Hermes tree. Pin that
        # boundary explicitly (HERMES_HOME) instead of assuming the ambient TMPDIR
        # already lives inside it: an isolated TMPDIR such as ~/.tmp-pytest does
        # not, and the assertion would silently stop testing anything.
        with tempfile.TemporaryDirectory(
            prefix="host-hermes-", dir=Path(os.environ["TMPDIR"]).resolve()
        ) as host:
            host_root = Path(host).resolve()
            with tempfile.TemporaryDirectory(prefix="session-admission-", dir=host_root) as folder:
                root = Path(folder).resolve()
                with patch.dict(os.environ, {"HERMES_HOME": str(host_root)}):
                    with self.assertRaisesRegex(ValueError, "host Hermes"):
                        SessionResume(root, SimpleNamespace(path=root / "mind.db"))
                self.assertEqual(list(root.iterdir()), [])

    def test_registry_detects_real_module_hook(self):
        from integration_registry import REGISTRY

        self.assertTrue(REGISTRY._has_self_evaluate(REGISTRY.get("session_resume")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
