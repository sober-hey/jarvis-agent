"""Synthetic snapshot regressions; no archive or model access."""

from pathlib import Path
import tempfile
import unittest

import absorb_formats


class AbsorbFormatsTests(unittest.TestCase):
    def test_snapshot_records_provenance_and_keeps_source_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="format-unit-") as folder:
            root = Path(folder)
            source = root / "fiction.json"
            source.write_text('[{"label":"synthetic-a"},{"label":"synthetic-b"}]')
            before = source.read_bytes()
            snap = absorb_formats.snapshot(source, root / "snapshot-data")
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(len(snap.chunks), 2)
            self.assertEqual(snap.chunks[0].source_ref, "record [0]")
            self.assertEqual(snap.chunks[0].source_file, str(source))
            self.assertEqual(
                snap.identity, absorb_formats.snapshot(source, root / "snapshot-data").identity
            )

    def test_chunk_size_is_bounded_without_text_loss(self):
        text = "synthetic " * 1500
        with tempfile.TemporaryDirectory(prefix="format-chunks-") as folder:
            root = Path(folder)
            source = root / "fiction.txt"
            source.write_text(text)
            snap = absorb_formats.snapshot(source, root / "snapshot-data")
            pieces = [chunk.text for chunk in snap.chunks]
            self.assertEqual("".join(pieces), text.strip())
            self.assertTrue(all(len(piece) <= 6000 for piece in pieces))


class SelfEvaluationTests(unittest.TestCase):
    def hook(self):
        import importlib

        module = importlib.import_module("absorb_formats")
        return module, module.self_evaluate

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
        self.assertEqual(report["module"], "absorb_formats")
        self.assertIn("snapshot", report["current_version"].lower())
        self.assertIn("memory", " ".join(report["known_limitations"]).lower())
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
        self.assertEqual(second["module"], "absorb_formats")
