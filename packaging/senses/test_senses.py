"""Synthetic-only sensor contract tests; all state stays in temporary roots."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from packaging.senses.base import Adapter, CursorStore, SenseEvent


class BaseTests(unittest.TestCase):
    def test_event_time_and_cursor_survive_restart(self) -> None:
        try:
            spec = importlib.util.find_spec("packaging.senses.base")
        except ModuleNotFoundError:
            spec = None
        self.assertIsNotNone(spec, "Unified event/cursor implementation is missing")
        base = importlib.import_module("packaging.senses.base")
        self.assertEqual(
            base.parse_time("2026-10-15T08:00:00+08:00"), datetime(2026, 10, 15, tzinfo=UTC)
        )
        self.assertEqual(
            base.parse_time("2026-10-15 08:00:00 +0800"), datetime(2026, 10, 15, tzinfo=UTC)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "senses_cursor.db"
            with base.CursorStore(path) as store:
                store.set("test", "offset", "7")
            with base.CursorStore(path) as store:
                self.assertEqual(store.get("test", "offset"), "7")


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="jarvis-senses-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cursor = self.root / "state" / "senses_cursor.db"

    def adapter(self, module: str, name: str, path: Path) -> Adapter:
        try:
            cls = getattr(importlib.import_module("packaging.senses." + module), name)
        except (ModuleNotFoundError, AttributeError):
            self.fail("Required sensor adapter is missing: " + module)
        return cast(Adapter, cls(path))

    def write_lines(self, path: Path, rows: list[object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows)
        )

    def check(self, adapter: Adapter, expected: int) -> None:
        with CursorStore(self.cursor) as store:
            events = adapter.poll(store)
            self.assertEqual(len(events), expected)
            self.assertGreaterEqual(adapter.last_status.bad_rows, 1)
            for event in events:
                self.assertEqual(event.observed_at.tzinfo, UTC)
                self.assertNotIn("PRIVATE", event.summary)
                self.assertNotIn("999", event.summary)
                self.assertNotIn("PRIVATE", repr(event))
                self.assertTrue(event.provenance.record_id)
                if event.kind in {"location", "heart_rate", "app_usage", "speech"}:
                    self.assertEqual(event.observed_at, datetime(2026, 10, 15, tzinfo=UTC))
                if event.kind == "sleep":
                    self.assertEqual(event.payload["start"], "2026-10-14T15:00:00+00:00")
                    self.assertEqual(event.payload["end"], "2026-10-14T23:00:00+00:00")
                if event.kind == "location":
                    self.assertEqual(event.payload["region"], "测试市")
                    self.assertNotIn("coordinates", event.payload)
                if event.kind == "speech":
                    self.assertEqual(event.payload["topic"], "工作交流")
                    self.assertEqual(event.payload["language"], "zh")
                    self.assertNotIn("text", event.payload)
        with CursorStore(self.cursor) as store:
            self.assertEqual(adapter.poll(store), [])

    def test_location_incremental_bad_point_and_no_exact_coordinates(self) -> None:
        path = self.root / "tracks"
        point = {
            "properties": {
                "timestamp": "2026-10-15T08:00:00+08:00",
                "city": "测试市",
                "address": "PRIVATE",
            },
            "geometry": {"coordinates": [10.999, 20.999]},
        }
        self.write_lines(path / "day.jsonl", ["broken", {"locations": [None, point]}])
        adapter = self.adapter("location", "LocationAdapter", path)
        self.check(adapter, 1)
        with (path / "day.jsonl").open("a") as stream:
            stream.write(json.dumps({"locations": [point]}) + "\n")
        with CursorStore(self.cursor) as store:
            self.assertEqual(adapter.poll(store), [])

    def test_health_incremental_bad_row_and_sleep_time(self) -> None:
        path = self.root / "health.db"
        with sqlite3.connect(path) as connection:
            connection.executescript(
                "CREATE TABLE points(metric,ts,qty,vmin,vmax);"
                "CREATE TABLE sleep(day,sleep_start,sleep_end,total,deep,core,rem,awake,in_bed);"
                "INSERT INTO points VALUES('heart_rate','2026-10-15 08:00:00 +0800',999,1,2);"
                "INSERT INTO points VALUES('heart_rate','invalid',999,1,2);"
                "INSERT INTO sleep VALUES('d','2026-10-14T23:00:00+08:00',"
                "'2026-10-15T07:00:00+08:00',8,1,1,1,1,1);"
            )
        adapter = self.adapter("health", "HealthAdapter", path)
        self.check(adapter, 2)
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE points SET qty=998 WHERE ts LIKE '2026%'")
        with CursorStore(self.cursor) as store:
            self.assertEqual(len(adapter.poll(store)), 1)

    def test_aw_incremental_bad_row_and_heartbeat_update(self) -> None:
        path = self.root / "aw.db"
        with sqlite3.connect(path) as connection:
            connection.executescript(
                "CREATE TABLE bucketmodel(key INTEGER,id,type,client);"
                "CREATE TABLE eventmodel(id INTEGER,bucket_id,timestamp,duration,datastr);"
                "INSERT INTO bucketmodel VALUES(1,'app-only','currentwindow','app-only');"
            )
            connection.executemany(
                "INSERT INTO eventmodel VALUES(?,1,?,?,?)",
                [
                    (
                        1,
                        "2026-10-15T08:00:00+08:00",
                        10,
                        json.dumps({"app": "Editor", "title": "PRIVATE", "url": "PRIVATE"}),
                    ),
                    (2, "invalid", 1, "broken"),
                ],
            )
        adapter = self.adapter("activitywatch", "ActivityWatchAdapter", path)
        self.check(adapter, 1)
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE eventmodel SET duration=20 WHERE id=1")
        with CursorStore(self.cursor) as store:
            events = adapter.poll(store)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].payload["duration_seconds"], 20)
            self.assertNotIn("PRIVATE", json.dumps(events[0].payload))

    def test_speech_incremental_bad_row_filename_time_and_privacy(self) -> None:
        path = self.root / "recordings"
        self.write_lines(
            path / "2026-10-15" / "08-00-00.merged.jsonl",
            [
                "broken",
                {"start": 0, "end": 12, "text": "PRIVATE 999 开会讨论工作", "who": "PRIVATE"},
            ],
        )
        adapter = self.adapter("speech", "SpeechAdapter", path)
        self.check(adapter, 1)
        with CursorStore(self.cursor, dry_run=True) as store:
            self.assertEqual(adapter.poll(store), [])

    def test_poll_all_dry_run_and_since_do_not_persist(self) -> None:
        try:
            api = importlib.import_module("packaging.senses.api")
        except ModuleNotFoundError:
            self.fail("Unified poll_all entry point is missing")
        path = self.root / "recordings"
        self.write_lines(
            path / "2026-10-15" / "08-00-00.merged.jsonl",
            [
                {"start": 0, "end": 1, "text": "PRIVATE"},
                {"start": 60, "end": 61, "text": "PRIVATE"},
            ],
        )
        adapter = self.adapter("speech", "SpeechAdapter", path)
        data_root = self.root / "state"
        events = api.poll_all(
            data_root=data_root, adapters=[adapter], dry_run=True, since="2026-10-15T00:00:30Z"
        )
        self.assertEqual(len(events), 1)
        self.assertFalse(self.cursor.exists())
        self.assertEqual(len(api.poll_all(data_root=data_root, adapters=[adapter])), 2)
        before = self.cursor.read_bytes()
        self.assertEqual(api.poll_all(data_root=data_root, adapters=[adapter], dry_run=True), [])
        self.assertEqual(self.cursor.read_bytes(), before)

    def test_partial_jsonl_and_rotation_preserve_incremental_delivery(self) -> None:
        path = self.root / "recordings" / "2026-10-15" / "08-00-00.merged.jsonl"
        first = {"start": 0, "end": 1, "text": "PRIVATE"}
        second = {"start": 2, "end": 3, "text": "PRIVATE"}
        self.write_lines(path, [first])
        with path.open("a") as stream:
            stream.write(json.dumps(second))
        adapter = self.adapter("speech", "SpeechAdapter", path.parents[1])
        with CursorStore(self.cursor) as store:
            events = adapter.poll(store)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].observed_at, datetime(2026, 10, 15, tzinfo=UTC))
        with path.open("a") as stream:
            stream.write("\n")
        with CursorStore(self.cursor) as store:
            self.assertEqual(len(adapter.poll(store)), 1)
        self.write_lines(path, [first, second, {"start": 4, "end": 5, "text": "PRIVATE"}])
        with CursorStore(self.cursor) as store:
            self.assertEqual(len(adapter.poll(store)), 1)

    def test_wal_snapshot_source_unchanged(self) -> None:
        from packaging.senses.base import source_database

        path = self.root / "wal.db"
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE sample(value)")
            connection.execute("INSERT INTO sample VALUES(1)")
            connection.commit()
            before = {p.name: p.read_bytes() for p in self.root.glob("wal.db*")}
            with source_database(path) as snapshot:
                self.assertEqual(snapshot.execute("SELECT count(*) FROM sample").fetchone()[0], 1)
                with self.assertRaises(sqlite3.OperationalError):
                    snapshot.execute("INSERT INTO sample VALUES(2)")
            after = {p.name: p.read_bytes() for p in self.root.glob("wal.db*")}
            self.assertEqual(before, after)

    def test_poll_failure_rolls_back_cursor_for_retry(self) -> None:
        api = importlib.import_module("packaging.senses.api")

        class FailingAdapter(Adapter):
            source = "failing"

            def poll(self, store: CursorStore, since: datetime | None = None) -> list[SenseEvent]:
                store.set(self.source, "offset", "consumed")
                return self.unavailable()

        adapter = FailingAdapter(self.root / "source")
        api.poll_all(data_root=self.root / "state", adapters=[adapter])
        with CursorStore(self.cursor) as store:
            self.assertIsNone(store.get("failing", "offset"))

    def test_cli_prints_statistics_only_and_packaging_remains_compatible(self) -> None:
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch

        from packaging.requirements import Requirement
        from packaging.senses.__main__ import main
        from packaging.version import Version

        self.assertEqual(Version("1.2"), Version("1.2.0"))
        self.assertEqual(Requirement("demo>=1").name, "demo")
        adapter = self.adapter("speech", "SpeechAdapter", self.root)
        event = adapter.event(
            "speech", datetime(2026, 10, 15, tzinfo=UTC), "PRIVATE", {"topic": "PRIVATE"}, "PRIVATE"
        )
        stream = io.StringIO()
        with (
            patch("sys.argv", ["senses", "--dry-run"]),
            patch("packaging.senses.__main__.poll_all", return_value=[event]),
            redirect_stdout(stream),
        ):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(stream.getvalue()), {"count": 1, "kinds": {"speech": 1}})
        self.assertNotIn("PRIVATE", stream.getvalue())

    def test_all_missing_and_permission_errors_are_unavailable(self) -> None:
        from unittest.mock import patch

        for module, name in [
            ("location", "LocationAdapter"),
            ("health", "HealthAdapter"),
            ("activitywatch", "ActivityWatchAdapter"),
            ("speech", "SpeechAdapter"),
        ]:
            adapter = self.adapter(module, name, self.root / "missing")
            with CursorStore(self.cursor) as store:
                self.assertEqual(adapter.poll(store), [])
                self.assertEqual(adapter.last_status.status, "unavailable")
                with (
                    patch.object(Path, "is_file", side_effect=PermissionError),
                    patch.object(Path, "is_dir", side_effect=PermissionError),
                ):
                    self.assertEqual(adapter.poll(store), [])
                    self.assertEqual(adapter.last_status.status, "unavailable")

    def test_browser_visit_payload_provenance_incremental_and_dry_run(self) -> None:
        adapter = self.adapter("browser", "BrowserHistoryAdapter", self.root / "browser")
        path = adapter.path / "2026-10-15.jsonl"
        self.write_lines(
            path,
            [
                {
                    "url": "https://example.test/private?token=fixture",
                    "title": "PRIVATE title",
                    "visited_at": "2026-10-15T00:00:00Z",
                    "duration_seconds": 12,
                    "record_id": "fixture-visit",
                }
            ],
        )
        api = importlib.import_module("packaging.senses.api")
        data_root = self.root / "state"
        events = api.poll_all(data_root=data_root, adapters=[adapter], dry_run=True)
        self.assertEqual(len(events), 1)
        self.assertFalse(self.cursor.exists())
        event = events[0]
        self.assertEqual(event.kind, "page_visit")
        self.assertEqual(event.sensitivity, "high")
        self.assertEqual(event.payload["domain"], "example.test")
        # Current implementation retains URL and title; do not claim domain-only.
        self.assertEqual(event.payload["url"], "https://example.test/private?token=fixture")
        self.assertEqual(event.payload["title"], "PRIVATE title")
        self.assertEqual(event.payload["duration_seconds"], 12)
        self.assertEqual(event.provenance.line, 1)
        self.assertEqual(event.provenance.path, str(path))
        self.assertNotIn("PRIVATE", repr(event))
        self.assertEqual(len(api.poll_all(data_root=data_root, adapters=[adapter])), 1)
        self.assertEqual(api.poll_all(data_root=data_root, adapters=[adapter]), [])

    def test_browser_bad_urls_dates_and_durations_are_rejected(self) -> None:
        adapter = self.adapter("browser", "BrowserHistoryAdapter", self.root / "browser")
        base = {"url": "https://example.test", "timestamp": "2026-10-15T00:00:00Z"}
        invalid = [
            {"url": "file:///private/fixture"},
            {"url": "javascript:alert(1)"},
            {"url": "https://exa mple.test"},
            {"url": "https://example.test:70000"},
            {"url": "https://example.test:abc"},
            {"url": None},
            {"timestamp": "not-a-date"},
            {"duration_seconds": -1},
            {"duration_seconds": float("nan")},
            {"duration_seconds": True},
        ]
        self.write_lines(adapter.path / "2026-10-15.jsonl", [{**base, **v} for v in invalid])
        with CursorStore(self.cursor) as store:
            self.assertEqual(adapter.poll(store), [])
            self.assertEqual(adapter.last_status.bad_rows, len(invalid))
            self.assertEqual(adapter.last_status.status, "available")
            self.assertEqual(adapter.poll(store), [])
            self.assertEqual(adapter.last_status.scanned, 0)

    def test_browser_missing_and_permission_error_are_unavailable(self) -> None:
        from unittest.mock import patch

        adapter = self.adapter("browser", "BrowserHistoryAdapter", self.root / "missing")
        with CursorStore(self.cursor) as store:
            self.assertEqual(adapter.poll(store), [])
            self.assertEqual(adapter.last_status.status, "unavailable")
            with patch.object(Path, "is_dir", side_effect=PermissionError):
                self.assertEqual(adapter.poll(store), [])
                self.assertEqual(adapter.last_status.status, "unavailable")

    def test_default_polling_includes_browser_without_reading_sources(self) -> None:
        api = importlib.import_module("packaging.senses.api")
        selected = api.default_adapters()
        self.assertEqual(
            [a.source for a in selected],
            ["overland", "health", "activitywatch", "speech", "browser_history"],
        )

    def test_api_rejects_cursor_overlap_before_source_io(self) -> None:
        api = importlib.import_module("packaging.senses.api")
        adapter = self.adapter("browser", "BrowserHistoryAdapter", self.root / "source")
        with self.assertRaisesRegex(ValueError, "overlaps sensor source"):
            api.poll_all(data_root=adapter.path / "state", adapters=[adapter])
        self.assertFalse(adapter.path.exists())

    def test_api_unexpected_exception_rolls_back_whole_round(self) -> None:
        api = importlib.import_module("packaging.senses.api")

        class Good(Adapter):
            source = "good"

            def poll(self, store: CursorStore, since: datetime | None = None) -> list[SenseEvent]:
                store.set(self.source, "offset", "advanced")
                return []

        class Broken(Adapter):
            source = "broken"

            def poll(self, store: CursorStore, since: datetime | None = None) -> list[SenseEvent]:
                raise RuntimeError("synthetic programming error")

        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            api.poll_all(
                data_root=self.root / "state",
                adapters=[Good(self.root / "good"), Broken(self.root / "broken")],
            )
        with CursorStore(self.cursor) as store:
            self.assertIsNone(store.get("good", "offset"))

    def test_location_retains_sensitive_values_and_same_point_metadata_is_not_revised(self) -> None:
        adapter = self.adapter("location", "LocationAdapter", self.root / "tracks")
        point: dict[str, object] = {
            "properties": {
                "timestamp": "2026-10-15T00:00:00Z",
                "motion": ["walking"],
                "wifi": "fixture-wifi",
                "horizontal_accuracy": 25,
            },
            "geometry": {"coordinates": [10.1234567, 20.1234567]},
        }
        path = adapter.path / "day.jsonl"
        self.write_lines(path, [{"locations": [point]}])
        with CursorStore(self.cursor) as store:
            (event,) = adapter.poll(store)
        self.assertEqual(event.payload["lon"], 10.1234567)
        self.assertEqual(event.payload["motion"], ["walking"])
        self.assertEqual(event.payload["wifi"], "fixture-wifi")
        self.assertEqual(event.payload["region_status"], "unknown")
        cast(dict[str, object], point["properties"])["city"] = "测试市"
        with path.open("a") as stream:
            stream.write(json.dumps({"locations": [point]}) + "\n")
        with CursorStore(self.cursor) as store:
            self.assertEqual(adapter.poll(store), [])

    def test_health_generic_wrist_temperature_is_not_body_temperature(self) -> None:
        path = self.root / "health.db"
        with sqlite3.connect(path) as db:
            db.executescript(
                "CREATE TABLE points(metric,ts,qty,vmin,vmax);"
                "CREATE TABLE sleep(day,sleep_start,sleep_end);"
            )
            db.execute(
                "INSERT INTO points VALUES(?,?,?,?,?)",
                ("apple_sleeping_wrist_temperature", "2026-10-15T00:00:00Z", 0.4, None, None),
            )
        adapter = self.adapter("health", "HealthAdapter", path)
        with CursorStore(self.cursor) as store:
            (event,) = adapter.poll(store)
        self.assertEqual(event.kind, "health_metric")
        self.assertEqual(event.payload["value"], 0.4)
        self.assertNotIn("diagnosis", event.payload)
        self.assertNotIn("body_temperature", event.payload)


class SelfEvaluationTests(unittest.TestCase):
    """Offline contracts describe code, never sample the user's sensor sources."""

    def check_hook(self, name: str, limitation: str) -> None:
        from unittest.mock import patch

        module = importlib.import_module("packaging.senses." + name)
        self.assertTrue("self_evaluate" in vars(module), "each module must own its hook")
        hook = module.self_evaluate
        self.assertEqual(hook.__module__, module.__name__)
        before = datetime.now(UTC)
        with (
            patch("sqlite3.connect", side_effect=AssertionError("DB read forbidden")),
            patch.object(Path, "open", side_effect=AssertionError("source read forbidden")),
            patch.object(Path, "glob", side_effect=AssertionError("source scan forbidden")),
            patch.object(Path, "rglob", side_effect=AssertionError("source scan forbidden")),
            patch("subprocess.run", side_effect=AssertionError("external action forbidden")),
        ):
            report = hook()
            fresh = hook()
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
        self.assertEqual(report["module"], "senses." + name)
        for field in ("domain", "current_version"):
            self.assertIsInstance(report[field], str)
            self.assertTrue(report[field].strip())
        for field in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[field], list)
            self.assertTrue(report[field])
            self.assertTrue(all(isinstance(v, str) and v.strip() for v in report[field]))
        self.assertIn(limitation, " ".join(report["known_limitations"]))
        stamp = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertEqual(stamp.tzinfo, UTC)
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, datetime.now(UTC))
        self.assertNotIn("PRIVATE", json.dumps(report))
        report["known_limitations"].append("mutated")
        self.assertNotIn("mutated", fresh["known_limitations"])

    def test_activitywatch_self_evaluate_is_owned_and_offline(self) -> None:
        self.check_hook("activitywatch", "foreground_not_attention")

    def test_api_self_evaluate_is_owned_and_offline(self) -> None:
        self.check_hook("api", "O(source rows)")

    def test_base_self_evaluate_is_owned_and_offline(self) -> None:
        self.check_hook("base", "4096")

    def test_browser_self_evaluate_is_owned_and_offline(self) -> None:
        self.check_hook("browser", "unknown")

    def test_health_self_evaluate_is_owned_and_offline(self) -> None:
        self.check_hook("health", "RMSSD")

    def test_location_self_evaluate_is_owned_and_offline(self) -> None:
        self.check_hook("location", "motion")

    def test_speech_self_evaluate_is_owned_and_offline(self) -> None:
        self.check_hook("speech", "关键词")


if __name__ == "__main__":
    unittest.main()
