"""FileOps checks confined to owned disposable files under $TMPDIR."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from file_ops import FileOps


class FileOpsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="file-ops-", dir=os.environ["TMPDIR"])
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.ops = FileOps([self.root])

    def test_utf8_write_read_edit_delete_round_trip(self):
        text = "第一行\nsecond\nthird\n"
        target = self.root / "nested" / "文本.txt"
        result = self.ops.write_file("nested/文本.txt", text)
        self.assertEqual(result["bytes"], len(text.encode("utf-8")))
        self.assertEqual(target.read_text(), text)
        self.assertEqual(
            self.ops.read_file("nested/文本.txt", 2, 2),
            {
                "path": str(target),
                "content": "second\n",
                "total_lines": 3,
                "start_line": 2,
                "end_line": 2,
            },
        )
        target.chmod(0o640)
        self.assertEqual(self.ops.edit_file(str(target), "second", "changed")["replacements"], 1)
        self.assertEqual(target.read_text(), "第一行\nchanged\nthird\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertTrue(self.ops.delete_file(str(target))["deleted"])
        self.assertFalse(target.exists())
        self.assertFalse(self.ops.delete_file(str(target))["deleted"])

    def test_ambiguous_edits_and_invalid_ranges_preserve_content(self):
        self.ops.write_file("a.txt", "same same\n")
        for old in ("same", "absent", ""):
            with self.assertRaises(ValueError):
                self.ops.edit_file("a.txt", old, "new")
            self.assertEqual((self.root / "a.txt").read_text(), "same same\n")
        self.assertEqual(self.ops.edit_file("a.txt", "same", "new", True)["replacements"], 2)
        self.assertEqual((self.root / "a.txt").read_text(), "new new\n")
        for start, end in ((0, 0), (2, 1), (True, 0)):
            with self.assertRaises(ValueError):
                self.ops.read_file("a.txt", start, end)

    def test_root_traversal_symlinks_and_empty_policy_are_rejected(self):
        self.ops.write_file("inside.txt", "original")
        for name in ("../escape.txt", str(self.root.parent / "outside.txt")):
            with self.assertRaises(PermissionError):
                self.ops.write_file(name, "bad")
        (self.root / "alias.txt").symlink_to(self.root / "inside.txt")
        with self.assertRaises((OSError, ValueError)):
            self.ops.read_file("alias.txt")
        (self.root / "folder").mkdir()
        (self.root / "aliasdir").symlink_to(self.root / "folder", target_is_directory=True)
        with self.assertRaises(OSError):
            self.ops.write_file("aliasdir/new.txt", "bad")
        with self.assertRaises(PermissionError):
            FileOps([]).read_file(str(self.root / "inside.txt"))
        self.assertEqual((self.root / "inside.txt").read_text(), "original")
        self.assertFalse((self.root / "folder/new.txt").exists())

    def test_list_dir_recursive_patterns_ignore_symlinks(self):
        for name in ("z.txt", "sub/b.py", "a.py"):
            self.ops.write_file(name, name)
        (self.root / "link.py").symlink_to(self.root / "a.py")
        self.assertEqual(
            [row["name"] for row in self.ops.list_dir(str(self.root), "**/*.py")],
            ["a.py", "sub/b.py"],
        )
        with self.assertRaises(PermissionError):
            self.ops.list_dir(str(self.root), "../*")

    def test_patch_add_update_move_and_delete(self):
        self.ops.write_file("old.txt", "one\ntwo\n")
        self.ops.write_file("delete.txt", "delete\n")
        result = self.ops.apply_patch(
            "*** Begin Patch\n*** Add File: new.txt\n+新建\n"
            "*** Update File: old.txt\n*** Move to: moved.txt\n@@\n one\n-two\n+three\n"
            "*** Delete File: delete.txt\n*** End Patch"
        )
        self.assertEqual(
            result,
            {
                "added": ["new.txt"],
                "updated": ["moved.txt"],
                "deleted": ["delete.txt"],
                "errors": [],
            },
        )
        self.assertEqual((self.root / "new.txt").read_text(), "新建\n")
        self.assertEqual((self.root / "moved.txt").read_text(), "one\nthree\n")
        self.assertFalse((self.root / "old.txt").exists())
        self.assertFalse((self.root / "delete.txt").exists())
        self.assertFalse(list(self.root.rglob(".fileops-*")))

    def test_invalid_patch_does_not_publish_earlier_addition(self):
        self.ops.write_file("a.txt", "original\n")
        result = self.ops.apply_patch(
            "*** Begin Patch\n*** Add File: new.txt\n+new\n"
            "*** Update File: a.txt\n@@\n-missing\n+bad\n*** End Patch"
        )
        self.assertTrue(result["errors"])
        self.assertEqual(result["added"], [])
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual((self.root / "a.txt").read_text(), "original\n")

    def test_patch_publish_failure_rolls_back_real_files(self):
        self.ops.write_file("a.txt", "A\n")
        self.ops.write_file("b.txt", "B\n")
        replace = os.replace
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second publish failure")
            return replace(*args, **kwargs)

        with patch("file_ops.os.replace", side_effect=fail_second):
            result = self.ops.apply_patch(
                "*** Begin Patch\n*** Update File: a.txt\n@@\n-A\n+X\n"
                "*** Update File: b.txt\n@@\n-B\n+Y\n*** End Patch"
            )
        self.assertTrue(result["errors"])
        self.assertEqual(result["updated"], [])
        self.assertEqual((self.root / "a.txt").read_text(), "A\n")
        self.assertEqual((self.root / "b.txt").read_text(), "B\n")
        self.assertFalse(list(self.root.rglob(".fileops-*")))


class SelfEvaluationTests(unittest.TestCase):
    def test_module_hook_is_offline_and_describes_real_limits(self):
        import builtins
        from contextlib import ExitStack
        from datetime import UTC, datetime, timedelta
        from unittest.mock import patch
        import file_ops as module

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
                patch.object(module, "FileOps", side_effect=AssertionError("constructor"))
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
        self.assertEqual(report["module"], "file_ops")
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
