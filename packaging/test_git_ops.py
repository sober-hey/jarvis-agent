"""Isolated GitOps integration checks; never mutate the product repository."""

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).with_name("git_ops.py")
SCRATCH = Path(os.environ["TMPDIR"]).resolve()


class GitOpsChecks(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_file(), "GitOps module has not been implemented")
        spec = importlib.util.spec_from_file_location("git_ops", MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.directory = tempfile.TemporaryDirectory(prefix="git-ops-", dir=SCRATCH)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "GitOps Test")
        self.git("config", "user.email", "git-ops@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.go = module.GitOps(str(self.root))

    def git(self, *args):
        result = subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        )
        return result.stdout

    def write(self, name, text="one\n"):
        (self.root / name).write_text(text)

    def seed(self):
        self.write("tracked.txt")
        self.git("add", "--", "tracked.txt")
        self.git("commit", "-m", "initial")

    def test_diff(self):
        self.assertTrue(callable(getattr(self.go, "diff", None)), "diff missing")
        self.seed()
        self.write("tracked.txt", "two\n")
        self.assertEqual(self.go.diff(), "")
        self.assertIn("+two", self.go.diff(staged=False))
        self.git("add", "tracked.txt")
        self.assertIn("+two", self.go.diff(path="tracked.txt"))
        self.assertEqual(self.go.diff(path="missing.txt"), "")
        self.write("-option.txt")
        self.git("add", "--", "-option.txt")
        self.assertIn("option.txt", self.go.diff(path="-option.txt"))

    def test_log(self):
        self.assertTrue(callable(getattr(self.go, "log", None)), "log missing")
        self.assertEqual(self.go.log(), [])
        self.seed()
        self.assertEqual(self.go.log(count=0), [])
        row = self.go.log(count=1)[0]
        self.assertEqual(set(row), {"hash", "message", "author", "date"})
        self.assertEqual(row["hash"], self.git("rev-parse", "HEAD").strip())
        self.assertEqual(row["message"], "initial")
        self.assertEqual(row["author"], "GitOps Test")
        self.assertTrue(row["date"])
        self.assertEqual(self.go.log(format="%s (%an)")[0]["message"], "initial (GitOps Test)")
        with self.assertRaises(ValueError):
            self.go.log(count=-1)

    def test_add(self):
        self.assertTrue(callable(getattr(self.go, "add", None)), "add missing")
        self.write("*.txt")
        self.write("other.txt")
        self.write("-flag.txt")
        result = self.go.add(["*.txt", "-flag.txt", "absent.txt"])
        self.assertEqual(result["added"], ["*.txt", "-flag.txt"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["path"], "absent.txt")
        self.assertEqual(
            {item["path"] for item in self.go.status()["staged"]}, {"*.txt", "-flag.txt"}
        )
        self.assertEqual(self.go.add([]), {"added": [], "errors": []})

    def test_commit(self):
        self.assertTrue(callable(getattr(self.go, "commit", None)), "commit missing")
        self.seed()
        self.write("tracked.txt", "two\n")
        self.write("unrelated.txt")
        self.git("add", "unrelated.txt")
        result = self.go.commit("selected\n\nbody", files=["tracked.txt"])
        self.assertEqual(result["hash"], self.git("rev-parse", "HEAD").strip())
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["message"], "selected\n\nbody")
        self.assertEqual(self.go.status()["staged"], [{"path": "unrelated.txt", "status": "A"}])
        self.assertEqual(self.go.commit("remaining")["files"], 1)
        with self.assertRaises(subprocess.CalledProcessError):
            self.go.commit("nothing to commit")
        with self.assertRaises(ValueError):
            self.go.commit("   ")
        with self.assertRaises(ValueError):
            self.go.commit("empty selection", files=[])

    def test_branch_checkout(self):
        self.assertTrue(callable(getattr(self.go, "branch", None)), "branch missing")
        self.assertTrue(callable(getattr(self.go, "checkout", None)), "checkout missing")
        self.seed()
        self.assertEqual(self.go.branch()["branches"], ["main"])
        self.assertTrue(self.go.branch("topic", create=True)["created"])
        self.assertEqual(self.go.current_branch(), "main")
        self.assertEqual(self.go.checkout("topic"), {"ref": "topic", "success": True})
        self.assertEqual(self.go.current_branch(), "topic")
        self.assertEqual(self.go.branch()["current"], "topic")
        self.assertFalse(self.go.checkout("missing-ref")["success"])
        self.assertFalse(self.go.checkout("--force")["success"])
        self.assertEqual(self.go.current_branch(), "topic")
        self.assertTrue(self.go.checkout("main")["success"])
        self.assertTrue(self.go.branch("topic", delete=True)["deleted"])
        self.assertEqual(self.go.branch()["branches"], ["main"])
        with self.assertRaises(ValueError):
            self.go.branch("x", create=True, delete=True)

    def test_merge_commit_file_count(self):
        self.seed()
        self.git("branch", "topic")
        self.git("checkout", "topic")
        self.write("topic.txt")
        self.git("add", "topic.txt")
        self.git("commit", "-m", "topic change")
        self.git("checkout", "main")
        self.write("main.txt")
        self.git("add", "main.txt")
        self.git("commit", "-m", "main change")
        self.git("merge", "--no-ff", "--no-commit", "topic")
        result = self.go.commit("merge topic")
        self.assertEqual(result["files"], 1)

    def test_rename_deletion_and_unmerged_safety(self):
        self.seed()
        self.git("mv", "tracked.txt", "renamed.txt")
        self.assertEqual(self.go.commit("rename")["files"], 1)
        (self.root / "renamed.txt").unlink()
        self.assertEqual(self.go.commit("delete", ["renamed.txt"])["files"], 1)
        self.go.branch("diverged", create=True)
        self.go.checkout("diverged")
        self.write("unique.txt")
        self.go.commit("unique", ["unique.txt"])
        self.go.checkout("main")
        with self.assertRaises(subprocess.CalledProcessError):
            self.go.branch("diverged", delete=True)
        self.assertIn("diverged", self.go.branch()["branches"])
        self.go.checkout("diverged")
        self.write("unique.txt", "local work\n")
        self.assertFalse(self.go.checkout("main")["success"])
        self.assertEqual((self.root / "unique.txt").read_text(), "local work\n")

    def test_carriage_return_filenames(self):
        odd_name = "carriage\rreturn.txt"
        self.write(odd_name)
        self.assertEqual(self.go.status()["untracked"], [{"path": odd_name, "status": "??"}])
        self.assertEqual(self.go.add([odd_name])["errors"], [])
        self.assertEqual(self.go.commit("odd name")["files"], 1)
        self.assertEqual(self.go.status()["untracked"], [])

    def test_status(self):
        self.assertEqual(self.go.current_branch(), "main")
        self.assertEqual(
            self.go.status(), {"staged": [], "unstaged": [], "untracked": [], "branch": "main"}
        )
        self.seed()
        self.write("tracked.txt", "two\n")
        self.git("add", "tracked.txt")
        self.write("tracked.txt", "three\n")
        odd_name = '空 格\tline\n".txt'
        self.write(odd_name)
        self.write("renamed source.txt")
        self.git("add", "renamed source.txt")
        self.git("commit", "-m", "source")
        self.git("mv", "renamed source.txt", "renamed target.txt")
        state = self.go.status()
        self.assertIn({"path": "tracked.txt", "status": "M"}, state["unstaged"])
        self.assertIn({"path": "renamed target.txt", "status": "R"}, state["staged"])
        self.assertIn({"path": odd_name, "status": "??"}, state["untracked"])
        self.assertEqual(state, self.go.status(porcelain=False))
        self.git("checkout", "--detach")
        self.assertEqual(self.go.current_branch(), "HEAD")


class SelfEvaluationTests(unittest.TestCase):
    def test_module_hook_is_offline_and_describes_real_limits(self):
        import builtins
        from contextlib import ExitStack
        from datetime import UTC, datetime, timedelta
        from unittest.mock import patch
        import git_ops as module

        hook = getattr(module, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-level self_evaluate is required")
        before = datetime.now(UTC)
        # No construction, data/config inspection, process spawn or directory probe.
        with ExitStack() as stack:
            for target in (
                "builtins.open",
                "pathlib.Path.open",
                "pathlib.Path.read_text",
                "pathlib.Path.read_bytes",
                "os.open",
                "os.stat",
                "os.listdir",
                "os.scandir",
                "subprocess.Popen",
                "subprocess.run",
            ):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            stack.enter_context(
                patch.object(module, "GitOps", side_effect=AssertionError("constructor"))
            )
            report = hook()
            another = hook()
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
        self.assertEqual(report["module"], "git_ops")
        for name in ("domain", "current_version"):
            self.assertIsInstance(report[name], str)
            self.assertTrue(report[name].strip())
        for name in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[name], list)
            self.assertTrue(report[name])
            self.assertTrue(all(isinstance(s, str) and s.strip() for s in report[name]))
        stamp = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.utcoffset(), timedelta(0))
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, datetime.now(UTC))
        report["known_limitations"].append("caller mutation")
        self.assertNotIn("caller mutation", another["known_limitations"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
