"""Real-file checks for the bounded local document chain (no model calls)."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))


class FileChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="jarvis-file-chain-", dir="/Users/sober")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "notes.txt"
        self.source.write_text("项目会议\n\n先做文件读写。\n完成后验证恢复。\n", encoding="utf-8")
        self.source.chmod(0o600)

    def module(self) -> Any:
        self.assertIsNotNone(
            importlib.util.find_spec("file_task_chain"), "file chain not implemented"
        )
        return importlib.import_module("file_task_chain")

    def test_real_file_to_markdown_with_persistent_progress(self) -> None:
        chain = self.module()
        record = chain.create(self.root, self.source, "整理资料", "first")
        self.assertEqual(record["status"], "snapshotted")
        self.assertFalse((self.root / "published").exists())
        record = chain.resume(self.root, record["task_id"])
        self.assertEqual(record["status"], "awaiting_approval")
        record = chain.resume(self.root, record["task_id"], publish=True)
        self.assertEqual(record["status"], "completed")
        output = self.root / record["published_relpath"]
        self.assertIn("先做文件读写。", output.read_text())
        self.assertEqual(len(record["steps"]), 3)
        self.assertTrue(all(step["receipt"] for step in record["steps"]))
        self.assertEqual(chain.query(self.root, record["task_id"]), record)
        self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)

    def snapshot(self) -> dict[str, tuple[bytes | None, int, int]]:
        return {
            str(path.relative_to(self.root)): (
                path.read_bytes() if path.is_file() else None,
                path.stat().st_ino,
                path.stat().st_mtime_ns,
            )
            for path in [self.root, *self.root.rglob("*")]
        }

    def test_completed_replay_query_and_create_are_zero_write(self) -> None:
        chain = self.module()
        from unittest.mock import patch

        record = chain.create(self.root, self.source, "整理", "replay")
        chain.resume(self.root, record["task_id"], publish=True)
        before = self.snapshot()
        with patch.object(chain.files, "atomic", wraps=chain.files.atomic) as writer:
            chain.query(self.root, record["task_id"])
            chain.resume(self.root, record["task_id"], publish=True)
            chain.create(self.root, self.source, "整理", "replay")
        self.assertEqual(writer.call_count, 0)
        self.assertEqual(self.snapshot(), before)

    def test_changed_source_after_snapshot_uses_original(self) -> None:
        chain = self.module()
        record = chain.create(self.root, self.source, "整理", "original")
        self.source.write_text("CHANGED")
        result = chain.resume(self.root, record["task_id"], publish=True)
        output = (self.root / result["published_relpath"]).read_text()
        self.assertNotIn("CHANGED", output)
        self.assertIn("先做文件读写。", output)

    def test_conflicting_request_key_rejected(self) -> None:
        chain = self.module()
        chain.create(self.root, self.source, "整理", "conflict")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            chain.create(self.root, self.source, "不同指令", "conflict")
        self.assertEqual(self.snapshot(), before)

    def test_source_symlink_hardlink_and_outside_rejected(self) -> None:
        chain = self.module()
        symlink = self.root / "link.txt"
        symlink.symlink_to(self.source)
        with self.assertRaises((ValueError, OSError)):
            chain.create(self.root, symlink, "整理", "symlink")
        symlink.unlink()
        hardlink = self.root / "hard.txt"
        os.link(self.source, hardlink)
        with self.assertRaises((ValueError, OSError)):
            chain.create(self.root, hardlink, "整理", "hardlink")
        with self.assertRaises((ValueError, OSError)):
            chain.create(self.root, self.root.parent / "outside.txt", "整理", "outside")

    def test_pdf_is_explicitly_unsupported(self) -> None:
        chain = self.module()
        with self.assertRaisesRegex(ValueError, "PDF/OCR not implemented"):
            chain.create(self.root, self.root / "file.pdf", "整理", "pdf")

    def test_final_target_conflict_never_overwritten(self) -> None:
        chain = self.module()
        record = chain.create(self.root, self.source, "整理", "target")
        published = self.root / "published"
        published.mkdir(mode=0o700)
        target = published / (record["task_id"] + ".md")
        target.write_text("USER FILE")
        target.chmod(0o600)
        result = chain.resume(self.root, record["task_id"], publish=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(target.read_text(), "USER FILE")

    def test_started_effect_missing_blocks_without_retry(self) -> None:
        chain = self.module()
        record = chain.create(self.root, self.source, "整理", "unknown")
        ledger = self.root / "file-chains" / record["task_id"] / "record.json"
        import json

        record["steps"][0]["receipt"] = None
        ledger.write_text(json.dumps(record))
        (ledger.parent / "input.snapshot").unlink()
        result = chain.resume(self.root, record["task_id"], publish=True)
        self.assertEqual(result["status"], "unknown")
        before = self.snapshot()
        chain.resume(self.root, record["task_id"], publish=True)
        self.assertEqual(self.snapshot(), before)

    def test_forged_completed_ledger_never_creates_publication(self) -> None:
        chain = self.module()
        import json

        record = chain.create(self.root, self.source, "整理", "forged")
        record = chain.resume(self.root, record["task_id"])
        ledger = self.root / "file-chains" / record["task_id"] / "record.json"
        record["status"] = "completed"
        ledger.write_text(json.dumps(record))
        (self.root / "published").mkdir(mode=0o700)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            chain.resume(self.root, record["task_id"])
        self.assertEqual(self.snapshot(), before)

    def test_modified_operation_identity_is_rejected(self) -> None:
        chain = self.module()
        import json

        record = chain.create(self.root, self.source, "整理", "identity")
        ledger = self.root / "file-chains" / record["task_id"] / "record.json"
        record["steps"][0]["operation_key"] = "forged"
        ledger.write_text(json.dumps(record))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            chain.resume(self.root, record["task_id"])
        self.assertEqual(self.snapshot(), before)

    def test_completed_artifact_identity_includes_receipt_binding(self) -> None:
        chain = self.module()
        import json

        record = chain.create(self.root, self.source, "整理", "receipt")
        record = chain.resume(self.root, record["task_id"], publish=True)
        ledger = self.root / "file-chains" / record["task_id"] / "record.json"
        record["steps"][2]["receipt"]["sha256"] = "0" * 64
        ledger.write_text(json.dumps(record))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            chain.resume(self.root, record["task_id"], publish=True)
        self.assertEqual(self.snapshot(), before)

    def test_large_and_nonprivate_inputs_are_rejected(self) -> None:
        chain = self.module()
        self.source.write_bytes(b"a" * (chain.files.MAX_INPUT + 1))
        with self.assertRaises(ValueError):
            chain.create(self.root, self.source, "整理", "large")
        self.source.write_text("资料")
        self.source.chmod(0o644)
        with self.assertRaises(ValueError):
            chain.create(self.root, self.source, "整理", "permissions")

    def test_completed_tamper_rejected_without_repair(self) -> None:
        chain = self.module()
        record = chain.create(self.root, self.source, "整理", "tamper")
        record = chain.resume(self.root, record["task_id"], publish=True)
        target = self.root / record["published_relpath"]
        target.write_text("USER MODIFIED")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            chain.resume(self.root, record["task_id"], publish=True)
        self.assertEqual(self.snapshot(), before)


class SelfEvaluationTests(unittest.TestCase):
    def test_offline_module_evaluation_describes_real_scope(self):
        import importlib
        import json
        from datetime import datetime
        from unittest.mock import patch

        module = importlib.import_module("file_task_chain")
        hook = getattr(module, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-level self_evaluate is required")
        with (
            patch("pathlib.Path.open", side_effect=AssertionError("unexpected file IO")),
            patch("pathlib.Path.mkdir", side_effect=AssertionError("unexpected mkdir")),
            patch("subprocess.Popen", side_effect=AssertionError("unexpected process")),
        ):
            report = hook()
        self.assertEqual(report["module"], "file_task_chain")
        self.assertTrue(report["domain"].strip())
        self.assertTrue(report["current_version"].strip())
        for key in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[key], list)
            self.assertTrue(report[key])
            self.assertTrue(all(isinstance(value, str) and value.strip() for value in report[key]))
        self.assertIn("ocr", " ".join(report["known_limitations"]).lower())
        self.assertIsNotNone(datetime.fromisoformat(report["last_evaluated_at"]).utcoffset())
        report["known_limitations"].clear()
        self.assertTrue(hook()["known_limitations"])
        json.dumps(report)


if __name__ == "__main__":
    unittest.main()
