"""Isolated real-ripgrep acceptance tests; no repository writes."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).with_name("code_search.py")


class SearchTests(unittest.TestCase):
    def test_find_files_and_files_output(self):
        spec = importlib.util.spec_from_file_location("code_search", MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as folder:
            root = Path(folder)
            (root / "sub").mkdir()
            (root / "a.py").write_text("yes\n")
            (root / "sub" / "b.py").write_text("yes\n")
            (root / "sub" / "c.txt").write_text("yes\n")
            cs = module.CodeSearch()
            self.assertTrue(hasattr(cs, "find_files"), "find_files API is missing")
            self.assertEqual(
                cs.find_files("*.py", folder), [str(root / "a.py"), str(root / "sub" / "b.py")]
            )
            self.assertEqual(cs.find_files("sub/*.py", folder), [str(root / "sub" / "b.py")])
            self.assertEqual(cs.find_files("*.missing", folder), [])
            self.assertEqual(cs.find_files("*.py", folder, 1), [str(root / "a.py")])
            self.assertEqual(
                cs.search("yes", folder, "*.py", limit=1, output_mode="files_only"),
                [{"file": str(root / "a.py"), "line": 0, "content": "", "context": []}],
            )

    def test_count_and_count_output(self):
        spec = importlib.util.spec_from_file_location("code_search", MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as folder:
            source = Path(folder) / "name:带换行\n.py"
            source.write_text("hit hit\nother\nhit\n")
            (Path(folder) / "skip.txt").write_text("hit\n")
            cs = module.CodeSearch()
            self.assertTrue(hasattr(cs, "count"), "count API is missing")
            self.assertEqual(cs.count("hit", folder, "*.py"), {str(source): 2})
            self.assertEqual(cs.count("absent", folder), {})
            self.assertEqual(
                cs.search("hit", folder, "*.py", output_mode="count"),
                [{"file": str(source), "line": 0, "content": "2", "context": [], "count": 2}],
            )

    def test_context_limits_and_errors(self):
        spec = importlib.util.spec_from_file_location("code_search", MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as folder:
            root = Path(folder)
            source = root / "上下文.py"
            source.write_text("前文\nneedle\nneedle again\n后文\n")
            cs = module.CodeSearch()
            rows = cs.search("needle", folder, "*.py", context_lines=1, limit=1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["context"], ["前文", "needle", "needle again"])
            self.assertEqual(cs.search("absent", folder), [])
            self.assertEqual(cs.search("needle", folder, limit=0), [])
            self.assertEqual(cs.find_files("*.py", folder, limit=0), [])
            for mode in ("content", "files_only", "count"):
                with self.assertRaises(RuntimeError):
                    cs.search("[", folder, output_mode=mode)
                self.assertEqual(cs.search("absent", folder, output_mode=mode), [])
            with self.assertRaises(RuntimeError):
                cs.count("[", folder)
            with self.assertRaises(RuntimeError):
                cs.search("needle", str(root / "nonexistent"))
            with self.assertRaises(ValueError):
                cs.search("needle", folder, output_mode="wrong")
            with self.assertRaises(ValueError):
                cs.search("needle", folder, context_lines=-1)
            with self.assertRaises(ValueError):
                cs.find_files("*.py", folder, limit=-1)
            with self.assertRaises(FileNotFoundError):
                module.CodeSearch("/nonexistent/rg")
            (root / "options.txt").write_text("--dash\n")
            self.assertEqual(cs.search("--dash", folder)[0]["content"], "--dash")
            explicit = module.CodeSearch(cs.rg_path)
            self.assertEqual(explicit.count("needle", str(source)), {str(source): 2})

    def test_content_search(self):
        self.assertTrue(MODULE.is_file(), "CodeSearch implementation is missing")
        spec = importlib.util.spec_from_file_location("code_search", MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as folder:
            source = Path(folder) / "sample.py"
            source.write_text("before\ndef hello():\n    pass\nafter\n")
            hits = module.CodeSearch().search(r"^def \w+", folder, "*.py")
            self.assertEqual(
                hits, [{"file": str(source), "line": 2, "content": "def hello():", "context": []}]
            )


class SelfEvaluationTests(unittest.TestCase):
    def test_module_hook_is_offline_and_describes_real_limits(self):
        import builtins
        from contextlib import ExitStack
        from datetime import UTC, datetime, timedelta
        from unittest.mock import patch
        import code_search as module

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
                patch.object(module, "CodeSearch", side_effect=AssertionError("constructor"))
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
        self.assertEqual(report["module"], "code_search")
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
