"""Reject historical evidence destinations before any regression can overwrite them."""

from __future__ import annotations

import ast
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evidence_support import evidence_directory

ROOT = Path(__file__).resolve().parents[1]


def historical_path_literals(source: str) -> list[int]:
    """Conservatively reject history path literals anywhere in test code."""
    history_component = ".runs" + "_01"
    return sorted(
        {
            node.lineno
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and history_component in node.value.replace("\\", "/").split("/")
        }
    )


class EvidenceIsolationTests(unittest.TestCase):
    def test_default_output_is_unique_and_disposable(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            first = evidence_directory("guard")
            second = evidence_directory("guard")
        self.addCleanup(shutil.rmtree, first, ignore_errors=True)
        self.addCleanup(shutil.rmtree, second, ignore_errors=True)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir())
        self.assertNotIn(".runs" + "_01", first.parts)

    def test_explicit_output_uses_fresh_children(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as folder:
            with patch.dict(os.environ, {"JARVIS_TEST_EVIDENCE_DIR": folder}):
                first = evidence_directory("guard")
                (first / "sentinel").write_text("preserve")
                second = evidence_directory("guard")
            self.assertEqual(first.parent, Path(folder).resolve())
            self.assertNotEqual(first, second)
            self.assertEqual((first / "sentinel").read_text(), "preserve")

    def test_historical_overrides_and_symlinks_rejected_without_writes(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as folder:
            base = Path(folder)
            history = base / (".runs" + "_01") / "old"
            history.mkdir(parents=True)
            alias = base / "alias"
            alias.symlink_to(history, target_is_directory=True)
            before = sorted(base.rglob("*"))
            for destination in (history, history / "new", alias, alias / "new"):
                with self.subTest(destination=destination):
                    with patch.dict(os.environ, {"JARVIS_TEST_EVIDENCE_DIR": str(destination)}):
                        with self.assertRaises(ValueError):
                            evidence_directory("guard")
            self.assertEqual(before, sorted(base.rglob("*")))
        with patch.dict(os.environ, {"JARVIS_TEST_EVIDENCE_DIR": ""}):
            with self.assertRaises(ValueError):
                evidence_directory("guard")

    def test_no_hardcoded_historical_test_paths(self) -> None:
        failures: list[str] = []
        for folder in (ROOT / "packaging", ROOT / "tests"):
            for path in sorted(folder.rglob("test*.py")):
                lines = historical_path_literals(path.read_text())
                if lines:
                    failures.append(f"{path.relative_to(ROOT)}:{lines}")
        self.assertEqual(failures, [], "Hardcoded historical evidence paths: " + repr(failures))

    def test_static_guard_detects_split_path_and_combined_path(self) -> None:
        history = ".runs" + "_01"
        for source in (
            f"RUNS = ROOT / {history!r} / 'old'",
            f"EVIDENCE = ROOT / '{history}/old'",
            f"(ROOT / '{history}/old/file.json').write_text('bad')",
        ):
            with self.subTest(source=source):
                self.assertEqual(historical_path_literals(source), [1])


if __name__ == "__main__":
    unittest.main()
