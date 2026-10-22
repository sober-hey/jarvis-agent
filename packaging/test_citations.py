import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import os
import stat
from unittest.mock import patch
import unittest

SCRATCH = Path("/Volumes/扩展硬盘/hermes/.hermes/cache/scratch/wave2-cognition/research-tests")
SCRATCH.mkdir(parents=True, exist_ok=True)


def make_store(root):
    import citations

    return citations.CitationStore(root)


class CitationTests(unittest.TestCase):
    def test_00_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("citations"), "citations module must exist")

    def test_02_conversation_isolation(self):
        import citations

        self.assertTrue(
            hasattr(citations.CitationStore, "list_for_conversation"), "conversation query missing"
        )
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            store = make_store(Path(directory))
            ids = [
                store.record("identical", [], conversation)
                for conversation in ("A", "B", None, "A", "A' OR 1=1 --")
            ]
            self.assertEqual(
                {entry["id"] for entry in store.list_for_conversation("A")}, {ids[0], ids[3]}
            )
            self.assertEqual([entry["id"] for entry in store.list_for_conversation("B")], [ids[1]])
            self.assertEqual(store.list_for_conversation("unknown"), [])
            self.assertEqual(
                [entry["id"] for entry in store.list_for_conversation("A' OR 1=1 --")], [ids[4]]
            )

    def test_03_literal_keyword_search(self):
        import citations

        self.assertTrue(
            hasattr(citations.CitationStore, "search_citations"), "keyword query missing"
        )
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            store = make_store(Path(directory))
            ids = [
                store.record("Atomic Ledger", [], "A"),
                store.record(
                    "other",
                    [{"url": "https://docs.example/unique-host", "snippet": "中文证据摘要"}],
                    "B",
                ),
                store.record("percent 100%", [], "C"),
                store.record("literal_a", [{"summary": "summary-only-term"}], "D"),
                store.record("literala", [], "E"),
                store.record(
                    "unrelated",
                    [{"url": "https://plain.example", "description": 'back\\slash and "quoted"'}],
                    None,
                ),
            ]
            for needle, wanted in [
                ("ledger", {ids[0]}),
                ("unique-host", {ids[1]}),
                ("证据", {ids[1]}),
                ("summary-only-term", {ids[3]}),
                ("%", {ids[2]}),
                ("_", {ids[3]}),
                ("back\\slash", {ids[5]}),
                ('"quoted"', {ids[5]}),
                ("' OR 1=1 --", set()),
                ("no-match", set()),
            ]:
                with self.subTest(needle=needle):
                    self.assertEqual(
                        {entry["id"] for entry in store.search_citations(needle)}, wanted
                    )
            self.assertEqual({entry["id"] for entry in store.search_citations("")}, set(ids))

    def test_04_secure_creation_shared_directory_unchanged(self):
        import citations

        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            shared = Path(directory)
            shared.chmod(0o755)
            root = shared / "new" / "private"
            original_connect = sqlite3.connect
            observed = []

            def checked_connect(*args, **kwargs):
                db = root / "citations.db"
                self.assertTrue(db.exists(), "DB must be precreated before SQLite opens it")
                observed.append(stat.S_IMODE(db.stat().st_mode))
                self.assertEqual(observed[-1], 0o600, "DB must be private from first SQLite open")
                return original_connect(*args, **kwargs)

            with (
                patch.object(citations.sqlite3, "connect", checked_connect),
                patch.object(os, "umask", side_effect=AssertionError("global umask forbidden")),
            ):
                store = make_store(root)
                store.record("permission", [], None)
            self.assertTrue(observed)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(root.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((root / "citations.db").stat().st_mode), 0o600)
            make_store(shared).record("shared", [], None)
            self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o755)

    def test_05_reject_database_symlinks_before_touching_target(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            root = Path(directory)
            target = root / "target.db"
            with sqlite3.connect(target) as db:
                db.execute("CREATE TABLE untouched (id TEXT)")
            target.chmod(0o644)
            before = target.read_bytes()
            (root / "citations.db").symlink_to(target)
            with self.assertRaises(OSError):
                make_store(root)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
            (root / "citations.db").unlink()
            (root / "citations.db").symlink_to(root / "missing.db")
            with self.assertRaises(OSError):
                make_store(root)
            self.assertFalse((root / "missing.db").exists())
            (root / "citations.db").unlink()
            store = make_store(root)
            (root / "citations.db").unlink()
            (root / "citations.db").symlink_to(target)
            with self.assertRaises(OSError):
                store.record("must not write", [], None)
            self.assertEqual(target.read_bytes(), before)

    def test_06_failed_record_is_atomic_and_connections_close(self):
        import citations

        connections = []

        class TrackedConnection(sqlite3.Connection):
            def close(self):
                self.was_closed = True
                return super().close()

        real_connect = sqlite3.connect

        def tracked_connect(*args, **kwargs):
            connection = real_connect(*args, factory=TrackedConnection, **kwargs)
            connection.was_closed = False
            connections.append(connection)
            return connection

        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            root = Path(directory)
            with patch.object(citations.sqlite3, "connect", tracked_connect):
                store = make_store(root)
                saved = store.record("saved", [{"url": "https://safe.example"}], "A")
                with real_connect(root / "citations.db") as db:
                    db.executescript("""CREATE TABLE audit (message TEXT);
                    CREATE TRIGGER fail_record BEFORE INSERT ON citations WHEN NEW.query = 'explode'
                    BEGIN INSERT INTO audit VALUES ('partial write'); SELECT RAISE(FAIL, 'injected failure'); END;""")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "injected failure"):
                    store.record("explode", [{"snippet": "never committed"}], "A")
                with real_connect(root / "citations.db") as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM citations").fetchone()[0], 1)
                with self.assertRaises(TypeError):
                    store.record("unserializable", [{"bad": object()}], "A")
                self.assertEqual(store.get(saved)["query"], "saved")
                self.assertEqual(len(store.list_for_conversation("A")), 1)
                self.assertEqual(store.search_citations("never committed"), [])
                self.assertEqual(make_store(root).get(saved)["query"], "saved")
            self.assertTrue(connections)
            self.assertTrue(all(connection.was_closed for connection in connections))

    def test_07_reject_invalid_payloads_without_rows(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            root = Path(directory)
            store = make_store(root)
            for query, results, conversation in [
                (42, [], None),
                ("q", {}, None),
                ("q", ["not a dict"], None),
                ("q", [], 42),
            ]:
                with self.subTest(query=query, results=results, conversation=conversation):
                    with self.assertRaises(TypeError):
                        store.record(query, results, conversation)
            with sqlite3.connect(root / "citations.db") as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM citations").fetchone()[0], 0)

    def test_08_concurrent_threads_multiple_instances(self):
        from concurrent.futures import ThreadPoolExecutor

        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            root = Path(directory) / "concurrent"
            shared = make_store(root)

            def worker(number):
                store = shared if number % 2 else make_store(root)
                ids = []
                for index in range(20):
                    citation_id = store.record(
                        f"thread-{number}-{index}",
                        [{"snippet": "concurrent evidence"}],
                        str(number),
                    )
                    self.assertEqual(store.get(citation_id)["conversation_id"], str(number))
                    ids.append(citation_id)
                return ids

            with ThreadPoolExecutor(max_workers=8) as executor:
                ids = [
                    citation_id for batch in executor.map(worker, range(8)) for citation_id in batch
                ]
            self.assertEqual(len(set(ids)), 160)
            self.assertEqual(
                {entry["id"] for entry in make_store(root).search_citations("concurrent evidence")},
                set(ids),
            )
            for number in range(8):
                self.assertEqual(len(shared.list_for_conversation(str(number))), 20)
            with sqlite3.connect(root / "citations.db") as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_09_concurrent_processes_initialization(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            root = Path(directory) / "first-open-race"
            program = """import json, pathlib, sys
from citations import CitationStore
store = CitationStore(pathlib.Path(sys.argv[1]))
print(json.dumps([store.record('process-' + sys.argv[2], [{'url': 'https://process.example'}], sys.argv[2]) for _ in range(20)]))
"""
            processes = [
                subprocess.Popen(
                    [sys.executable, "-B", "-c", program, str(root), str(number)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for number in range(4)
            ]
            ids = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=45)
                self.assertEqual(process.returncode, 0, stderr)
                ids.extend(json.loads(stdout))
            self.assertEqual(len(set(ids)), 80)
            self.assertEqual(
                {entry["id"] for entry in make_store(root).search_citations("process.example")},
                set(ids),
            )
            self.assertEqual(stat.S_IMODE((root / "citations.db").stat().st_mode), 0o600)

    def test_01_record_get_reopen(self):
        import citations

        self.assertTrue(hasattr(citations, "CitationStore"), "CitationStore API missing")
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            root = Path(directory) / "data"
            store = make_store(root)
            results = [
                {
                    "url": "https://example.org/资料",
                    "title": "Source",
                    "snippet": "中文摘要",
                    "extra": {"rank": 1},
                }
            ]
            citation_id = store.record("查证", results, "conversation-A")
            self.assertIsInstance(citation_id, str)
            self.assertTrue(citation_id)
            entry = store.get(citation_id)
            self.assertEqual(
                set(entry), {"id", "query", "conversation_id", "created_at", "results"}
            )
            self.assertEqual(entry["id"], citation_id)
            self.assertEqual(entry["query"], "查证")
            self.assertEqual(entry["conversation_id"], "conversation-A")
            self.assertEqual(entry["results"], results)
            from datetime import datetime

            self.assertIsNotNone(datetime.fromisoformat(entry["created_at"]).tzinfo)
            self.assertEqual(make_store(root).get(citation_id), entry)
            self.assertEqual(store.get("missing"), {})
            second = store.record("查证", [], None)
            self.assertNotEqual(citation_id, second)
            self.assertIsNone(store.get(second)["conversation_id"])
            with sqlite3.connect(root / "citations.db") as db:
                columns = [
                    (row[1], row[2], row[5]) for row in db.execute("PRAGMA table_info(citations)")
                ]
                self.assertEqual(
                    columns,
                    [
                        ("id", "TEXT", 1),
                        ("query", "TEXT", 0),
                        ("conversation_id", "TEXT", 0),
                        ("created_at", "TEXT", 0),
                        ("results_json", "TEXT", 0),
                    ],
                )
                self.assertEqual(
                    json.loads(
                        db.execute(
                            "SELECT results_json FROM citations WHERE id=?", (citation_id,)
                        ).fetchone()[0]
                    ),
                    results,
                )


class SelfEvaluationTests(unittest.TestCase):
    def evaluate(self):
        import importlib

        module = importlib.import_module("citations")
        self.assertTrue(
            callable(getattr(module.CitationStore, "self_evaluate", None)),
            "component-owned self_evaluate hook is missing",
        )
        return module.CitationStore.__new__(module.CitationStore).self_evaluate()

    def test_metadata_schema_identity_and_utc_timestamp(self):
        from datetime import datetime, timezone

        before = datetime.now(timezone.utc)
        report = self.evaluate()
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
        self.assertEqual(report["module"], "citations")
        self.assertIn("citation", report["domain"].lower())
        for key in ("domain", "current_version"):
            self.assertIsInstance(report[key], str)
            self.assertTrue(report[key].strip())
        for key in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[key], list)
            self.assertTrue(report[key])
            self.assertTrue(all(isinstance(x, str) and x.strip() for x in report[key]))
        stamp = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.utcoffset().total_seconds(), 0)
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, datetime.now(timezone.utc))

    def test_metadata_never_reads_credentials_database_or_network(self):
        from unittest.mock import patch

        # Import first, then deny file, database and network boundaries. The
        # instance has no attributes, so even reading data_root would fail.
        import importlib

        importlib.import_module("citations")
        with (
            patch("builtins.open", side_effect=AssertionError("file read forbidden")),
            patch("pathlib.Path.open", side_effect=AssertionError("path read forbidden")),
            patch("sqlite3.connect", side_effect=AssertionError("runtime DB forbidden")),
            patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
        ):
            report = self.evaluate()
        import json

        self.assertIsInstance(json.dumps(report), str)

    def test_reports_do_not_share_mutable_metadata(self):
        first = self.evaluate()
        expected = self.evaluate()
        first["known_limitations"].clear()
        first["improvement_directions"].append("test-only mutation")
        fresh = self.evaluate()
        self.assertEqual(fresh["known_limitations"], expected["known_limitations"])
        self.assertEqual(fresh["improvement_directions"], expected["improvement_directions"])
