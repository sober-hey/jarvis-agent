"""Behavior tests for the bounded product mind, always using disposable data."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import capabilities
import product_memory
from inner_loop import MindStore
from inner_runtime import InnerLoop, validate
from senses.activitywatch import ActivityWatchAdapter
from senses.base import Adapter, CursorStore, SenseEvent
from senses.health import HealthAdapter
from senses.location import LocationAdapter
from senses.speech import SpeechAdapter

Record = dict[str, Any]


def answer(prompt: str, *, question: bool = False, select: bool = True) -> Record:
    context = json.loads(prompt)
    if "proposed_question" in context:
        return {"text": json.dumps({"repeats_self_report": False, "reason": "new information"})}
    if "decision" in context:
        return {"text": json.dumps({"question": "Sir，童年最喜欢的书是哪一本？"})}
    ref = context["events"][-1]["id"]
    q = None
    if question:
        q = {
            "text": "样本城市是什么？" if select else "Sir 的童年最爱哪本书？",
            "assessments": [
                {
                    "capability_id": c["id"],
                    "can_resolve": select,
                    "reason": "提供城市" if select else "仅城市记录，没有阅读经历",
                }
                for c in context["capabilities"]
            ],
            "selected_capability": context["capabilities"][0]["id"] if select else None,
            "missing_information": "" if select else "资料没有童年阅读经历",
        }
    return {
        "text": json.dumps(
            {
                "thoughts": [
                    {
                        "id": "t1",
                        "kind": "question" if question else "idle",
                        "text": "一次思考",
                        "refs": [ref],
                        "question": q,
                    }
                ],
                "resolved_questions": [],
                "relevance": [
                    {
                        "belief_id": b["belief_id"],
                        "capability_id": c["id"],
                        "score": 0.0,
                        "reason": "not useful",
                    }
                    for b in context.get("relevance_candidates", [])
                    for c in context["capabilities"]
                ],
            }
        ),
        "usage": {"total_tokens": 10},
    }


class FakeSenseAdapter(Adapter):
    def __init__(self, path: Path, source: str, kind: str, payload: Record) -> None:
        super().__init__(path)
        self.source = source
        self.kind = kind
        self.payload = payload
        self.calls = 0
        self.empty_cursors: list[bool] = []
        self.fail = False
        self.bad_schema = False

    def poll(self, store: CursorStore, since: datetime | None = None) -> list[SenseEvent]:
        self.begin()
        self.calls += 1
        self.empty_cursors.append(store.get(self.source, "test") is None)
        store.set(self.source, "test", "visited")
        if self.fail:
            raise OSError("private path and raw values must never escape")
        if self.bad_schema:
            return self.unavailable()
        return [
            self.event(
                self.kind,
                datetime(2026, 10, 16, tzinfo=UTC),
                "private raw summary",
                self.payload,
                "private-record-id",
            )
        ]


class CapabilityTests(unittest.TestCase):
    def test_real_execution_is_isolated_bounded_and_sanitized(self) -> None:
        examples: list[tuple[str, str, Record]] = [
            (
                "overland",
                "location",
                {
                    "region": "天水市",
                    "region_status": "resolved",
                    "coordinates": "RAW_SECRET",
                    "address": "RAW_SECRET",
                },
            ),
            ("health", "heart_rate", {"value": "RAW_SECRET", "metric": "RAW_SECRET"}),
            ("activitywatch", "app_usage", {"app": "https://RAW_SECRET", "title": "RAW_SECRET"}),
            ("speech", "speech", {"topic": "工作交流", "language": "zh", "text": "RAW_SECRET"}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source"
            path.touch()
            adapters = [FakeSenseAdapter(path, *example) for example in examples]
            entries = [
                entry
                for entry in capabilities.catalog(adapters=adapters)
                if entry["implementation"] == "senses_read_only"
            ]
            self.assertEqual(
                {entry["id"] for entry in entries},
                {
                    "location_track",
                    "health_record",
                    "activity_usage",
                    "speech_record",
                    "health_monitoring",
                },
            )
            by_source = {adapter.source: adapter for adapter in adapters}
            self.assertTrue(all(entry["available"] for entry in entries))
            self.assertTrue(all(adapter.calls == 0 for adapter in adapters))
            for entry in entries:
                adapter = by_source[entry["call"]["arguments"]["source"]]
                with self.subTest(capability=entry["id"], source=adapter.source):
                    adapter.empty_cursors.clear()
                    cursor_roots: list[Path] = []
                    original_poll = capabilities.poll_all

                    def observed_poll(
                        *,
                        expected_adapter: Adapter = adapter,
                        roots: list[Path] = cursor_roots,
                        real_poll: Any = original_poll,
                        **kwargs: Any,
                    ) -> list[SenseEvent]:
                        cursor_root = kwargs["data_root"]
                        self.assertIs(kwargs["dry_run"], True)
                        self.assertEqual(kwargs["adapters"], [expected_adapter])
                        self.assertFalse((cursor_root / "senses_cursor.db").exists())
                        roots.append(cursor_root)
                        return real_poll(**kwargs)

                    for _ in range(2):
                        with patch("capabilities.poll_all", side_effect=observed_poll):
                            result = capabilities.execute(entry, adapters=adapters)
                        self.assertEqual(result["source"], adapter.source)
                        self.assertEqual(result["sampled_at"], "2026-10-16T00:00:00+00:00")
                        self.assertIs(result["synthetic"], False)
                        self.assertEqual(result["status"], "available")
                        self.assertEqual(result["precision"], entry["precision"])
                        rendered = json.dumps(result)
                        for secret in ("RAW_SECRET", "private", directory, "https://"):
                            self.assertNotIn(secret, rendered)
                        self.assertLess(len(rendered), 2000)
                    self.assertEqual(adapter.empty_cursors, [True, True])
                    self.assertEqual(len(set(cursor_roots)), 2)
                    self.assertTrue(all(not root.exists() for root in cursor_roots))
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["source"])

    def test_missing_and_failed_adapters_are_unavailable(self) -> None:
        entries = capabilities.catalog(adapters=[])
        sensed = [entry for entry in entries if entry["implementation"] == "senses_read_only"]
        independent = [entry for entry in entries if entry["implementation"] != "senses_read_only"]
        self.assertEqual(
            {entry["id"] for entry in independent},
            {"device_control", "model_search", "web_search", "citation_lookup"},
        )
        self.assertTrue(all(entry["available"] for entry in independent))
        self.assertFalse(any(entry["available"] for entry in sensed))
        for entry in sensed:
            result = capabilities.execute(entry, adapters=[])
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["sampled_at"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source"
            path.touch()
            adapter = FakeSenseAdapter(path, "health", "heart_rate", {})
            entry = capabilities.catalog(adapters=[adapter])[1]
            for mode in ("fail", "bad_schema"):
                setattr(adapter, mode, True)
                result = capabilities.execute(entry, adapters=[adapter])
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn("private", json.dumps(result))
                self.assertFalse(capabilities.catalog(adapters=[adapter])[1]["available"])
                setattr(adapter, mode, False)
            missing = FakeSenseAdapter(path / "absent", "health", "heart_rate", {})
            self.assertFalse(capabilities.catalog(adapters=[missing])[1]["available"])
            with patch("capabilities.os.access", return_value=False):
                self.assertFalse(capabilities.catalog(adapters=[adapter])[1]["available"])

    def test_shared_source_preserves_selected_precision_in_all_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source"
            path.touch()
            adapter = FakeSenseAdapter(path, "health", "heart_rate", {})
            entries = [
                entry
                for entry in capabilities.catalog(adapters=[adapter])
                if entry["implementation"] == "senses_read_only"
                and entry["call"]["arguments"]["source"] == "health"
            ]
            self.assertEqual(
                {entry["id"] for entry in entries}, {"health_record", "health_monitoring"}
            )
            for entry in entries:
                for outcome in (
                    "available",
                    "empty",
                    "failed",
                    "missing",
                    "unreadable",
                    "bad_schema",
                ):
                    with self.subTest(capability=entry["id"], outcome=outcome):
                        adapter.fail = outcome == "failed"
                        adapter.bad_schema = outcome == "bad_schema"
                        if outcome == "empty":

                            def empty_poll(
                                store: CursorStore, since: datetime | None = None
                            ) -> list[SenseEvent]:
                                adapter.begin()
                                return []

                            with patch.object(adapter, "poll", side_effect=empty_poll):
                                result = capabilities.execute(entry, adapters=[adapter])
                        elif outcome == "unreadable":
                            with patch("capabilities.os.access", return_value=False):
                                result = capabilities.execute(entry, adapters=[adapter])
                        else:
                            result = capabilities.execute(
                                entry, adapters=[] if outcome == "missing" else [adapter]
                            )
                        expected_status = (
                            outcome if outcome in {"available", "empty"} else "unavailable"
                        )
                        self.assertEqual(result["status"], expected_status)
                        self.assertEqual(result["precision"], entry["precision"])
                        self.assertEqual(result["source"], "health")
                        self.assertIs(result["synthetic"], False)

    def test_independent_capabilities_execute_without_sensor_adapters(self) -> None:
        from citations import CitationStore
        from device_controller import DeviceController

        entries = {entry["id"]: entry for entry in capabilities.catalog(adapters=[])}
        rows = [
            {"title": "Offline evidence", "url": "https://example.invalid", "snippet": "fixture"}
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("search.search", return_value=rows) as search,
                patch(
                    "capabilities.default_adapters", side_effect=AssertionError("must not probe")
                ),
                patch("capabilities.poll_all", side_effect=AssertionError("must not poll")),
                patch("device_controller.get_default_controller", return_value=DeviceController()),
            ):
                for identity, arguments in (
                    ("web_search", {"query": "offline evidence", "max_results": 2}),
                    ("model_search", {"task_type": "offline evaluation"}),
                ):
                    with self.subTest(capability=identity):
                        result = capabilities.execute(
                            entries[identity],
                            adapters=[],
                            arguments=arguments,
                            data_root=root,
                            conversation_id="isolated-capability-test",
                        )
                        self.assertEqual(result["status"], "available")
                        self.assertEqual(result["source"], identity)
                        self.assertEqual(result["results"], rows)
                        self.assertEqual(result["count"], 1)
                        self.assertIs(result["synthetic"], False)
                        saved: Record = cast(Any, CitationStore(root)).get(result["citation_id"])
                        self.assertEqual(saved["results"], rows)
                        self.assertEqual(saved["conversation_id"], "isolated-capability-test")
                self.assertEqual(search.call_count, 2)
                search.assert_any_call("offline evidence", max_results=2)
                lookup = capabilities.invoke(
                    {
                        "name": "citation_lookup",
                        "arguments": {"conversation_id": "isolated-capability-test"},
                    },
                    adapters=[],
                    data_root=root,
                )
                self.assertEqual(lookup["status"], "available")
                self.assertEqual(lookup["count"], 2)
                missing = capabilities.execute(
                    entries["citation_lookup"],
                    adapters=[],
                    arguments={"query": "no-match"},
                    data_root=root,
                )
                self.assertEqual(missing["status"], "empty")
                self.assertEqual(missing["count"], 0)
                devices = capabilities.execute(entries["device_control"], adapters=[])
                self.assertEqual(devices["status"], "empty")
                self.assertEqual(devices["devices"], [])
                self.assertIs(devices["xiaomi_configured"], False)
                self.assertIs(devices["ok"], True)
                self.assertTrue(entries["device_control"]["available"])

    def test_recognized_validates_all_metadata_without_probing(self) -> None:
        entries = capabilities.catalog(adapters=[]) + capabilities.fixture_catalog(
            include_sleep=True
        )
        with patch("capabilities.default_adapters", side_effect=AssertionError("must not probe")):
            for entry in entries:
                self.assertTrue(capabilities.recognized(entry))
                availability = deepcopy(entry)
                availability["available"] = not entry["available"]
                self.assertTrue(capabilities.recognized(availability))
                if entry["implementation"] == "local_fixture":
                    self.assertTrue(capabilities.execute(availability)["synthetic"])
                for field in set(entry) - {"available"}:
                    forged = deepcopy(entry)
                    forged[field] = "forged"
                    self.assertFalse(capabilities.recognized(forged))
                    with self.assertRaises(ValueError):
                        capabilities.execute(forged, adapters=[])
                forged = deepcopy(entry)
                forged["unexpected"] = "metadata"
                self.assertFalse(capabilities.recognized(forged))
                numeric_false = deepcopy(entry)
                numeric_false["costs_money"] = 0
                self.assertFalse(capabilities.recognized(numeric_false))

    def test_fixture_catalog_remains_explicit_and_synthetic(self) -> None:
        entries = capabilities.fixture_catalog(include_sleep=True)
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertIn("precision", entry)
            self.assertIn("freshness", entry)
            self.assertTrue(capabilities.execute(entry)["synthetic"])
        entries[0]["call"]["name"] = "altered"
        self.assertEqual(capabilities.fixture_catalog()[0]["call"]["name"], "local_city_sample")

    def test_passive_sanitizer_rejects_free_form_data_and_normalizes_time(self) -> None:
        stamp = "2026-10-16T08:00:00+08:00"
        for source, kind, payload in (
            (
                "overland",
                "location",
                {"region": "天水市 https://secret", "region_status": "secret"},
            ),
            ("health", "secret", {"metric": "secret", "value": "secret"}),
            ("speech", "speech", {"topic": "secret", "language": "secret"}),
            ("secret", "secret", {"address": "secret"}),
        ):
            result = capabilities.summarize_event(source, kind, payload, stamp)
            self.assertNotIn("secret", json.dumps(result))
            self.assertEqual(result["sampled_at"], "2026-10-16T00:00:00+00:00")
            self.assertIs(result["synthetic"], False)
        invalid_time = capabilities.summarize_event("health", "sleep", {}, "secret")
        self.assertIsNone(invalid_time["sampled_at"])
        self.assertEqual(invalid_time["status"], "unavailable")

    def test_existing_adapters_read_disposable_sources_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tracks = root / "tracks"
            tracks.mkdir()
            (tracks / "day.jsonl").write_text(
                json.dumps(
                    {
                        "locations": [
                            {
                                "properties": {
                                    "timestamp": "2026-10-16T00:00:00Z",
                                    "city": "测试市",
                                    "address": "PRIVATE",
                                },
                                "geometry": {"coordinates": [10.125, 20.125]},
                            }
                        ]
                    }
                )
                + "\n"
            )
            health = root / "health.db"
            with sqlite3.connect(health) as connection:
                connection.executescript(
                    "CREATE TABLE points(metric,ts,qty,vmin,vmax);"
                    "CREATE TABLE sleep(day,sleep_start,sleep_end);"
                    "INSERT INTO points VALUES('heart_rate','2026-10-16T00:00:00Z',12345,1,2);"
                )
            activity = root / "activity.db"
            with sqlite3.connect(activity) as connection:
                connection.executescript(
                    "CREATE TABLE bucketmodel(key,id,type,client);"
                    "CREATE TABLE eventmodel(id,bucket_id,timestamp,duration,datastr);"
                    "INSERT INTO bucketmodel VALUES(1,'test','currentwindow','app-only');"
                )
                connection.execute(
                    "INSERT INTO eventmodel VALUES(1,1,?,1,?)",
                    (
                        "2026-10-16T00:00:00Z",
                        json.dumps({"app": "Editor", "url": "https://PRIVATE"}),
                    ),
                )
            speech = root / "speech"
            speech_file = speech / "2026-10-16" / "08-00-00.merged.jsonl"
            speech_file.parent.mkdir(parents=True)
            speech_file.write_text(
                json.dumps({"start": 0, "end": 1, "text": "PRIVATE 工作"}) + "\n"
            )
            adapters = [
                LocationAdapter(tracks),
                HealthAdapter(health),
                ActivityWatchAdapter(activity),
                SpeechAdapter(speech),
            ]
            before = {
                str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            entries = [
                entry
                for entry in capabilities.catalog(adapters=adapters)
                if entry["implementation"] == "senses_read_only"
            ]
            self.assertEqual(
                {entry["call"]["arguments"]["source"] for entry in entries},
                {adapter.source for adapter in adapters},
            )
            for entry in entries:
                result = capabilities.execute(entry, adapters=adapters)
                self.assertEqual(result["status"], "available")
                self.assertEqual(result["record_count"], 1)
                for private in ("PRIVATE", "12345", "10.125", "20.125", directory):
                    self.assertNotIn(private, json.dumps(result))
            after = {
                str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
            broken = root / "broken.db"
            with sqlite3.connect(broken):
                pass
            adapter = HealthAdapter(broken)
            entry = capabilities.catalog(adapters=[adapter])[1]
            self.assertEqual(
                capabilities.execute(entry, adapters=[adapter])["status"], "unavailable"
            )
            self.assertFalse(capabilities.catalog(adapters=[adapter])[1]["available"])

    def test_empty_and_long_histories_have_bounded_latest_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            adapter = LocationAdapter(path)
            entry = capabilities.catalog(adapters=[adapter])[0]
            empty = capabilities.execute(entry, adapters=[adapter])
            self.assertEqual(empty["status"], "empty")
            self.assertEqual(empty["record_count"], 0)
            self.assertIsNone(empty["sampled_at"])
            rows = [
                {
                    "locations": [
                        {
                            "properties": {
                                "timestamp": f"2026-10-16T00:{minute:02d}:00Z",
                                "city": "测试市",
                            },
                            "geometry": {"coordinates": [10, 20]},
                        }
                    ]
                }
                for minute in range(50)
            ]
            (path / "day.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            result = capabilities.execute(entry, adapters=[adapter])
            self.assertEqual(result["record_count"], 50)
            self.assertEqual(result["sampled_at"], "2026-10-16T00:49:00+00:00")
            self.assertEqual(result["region"], "测试市")
            self.assertLess(len(json.dumps(result)), 2000)

    def test_default_catalog_describes_senses_and_independent_capabilities(self) -> None:
        with (
            patch("capabilities.default_adapters", return_value=[]) as default_adapters,
            patch("capabilities.poll_all", side_effect=AssertionError("must not poll")),
            patch(
                "capabilities.DeviceCapability", side_effect=AssertionError("must not construct")
            ),
        ):
            entries = capabilities.catalog()
        default_adapters.assert_called_once_with()
        self.assertEqual(
            {entry["id"] for entry in entries},
            {
                "location_track",
                "health_record",
                "activity_usage",
                "speech_record",
                "health_monitoring",
                "device_control",
                "model_search",
                "web_search",
                "citation_lookup",
            },
        )
        self.assertEqual(len(entries), len({entry["id"] for entry in entries}))
        expected_implementations = {
            "location_track": "senses_read_only",
            "health_record": "senses_read_only",
            "activity_usage": "senses_read_only",
            "speech_record": "senses_read_only",
            "health_monitoring": "senses_read_only",
            "device_control": "device_control",
            "model_search": "model_search",
            "web_search": "web_search",
            "citation_lookup": "citation_lookup",
        }
        expected_fields = {
            "id",
            "name",
            "provides",
            "persona_description",
            "call",
            "costs_money",
            "security_sensitive",
            "precision",
            "freshness",
            "implementation",
            "available",
        }
        for entry in entries:
            self.assertEqual(set(entry), expected_fields)
            self.assertEqual(entry["implementation"], expected_implementations[entry["id"]])
            self.assertFalse(entry["costs_money"])
            self.assertIs(entry["security_sensitive"], entry["id"] == "device_control")
            self.assertIsInstance(entry["available"], bool)
            self.assertIs(entry["available"], entry["implementation"] != "senses_read_only")
            self.assertTrue(entry["persona_description"].startswith("我"))
        rendered = json.dumps(entries, ensure_ascii=False)
        for forbidden in ("住址", "定位", "核实位置", "老家", "/Volumes/", "/Users/"):
            self.assertNotIn(forbidden, rendered)


class LoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.store = MindStore(self.root)

    def test_restart_keeps_hard_call_budget(self) -> None:
        loop = InnerLoop(
            self.store, answer, catalog=capabilities.fixture_catalog(), max_rounds=2, idle_seconds=0
        )
        loop.run()
        self.assertEqual(len(self.store.entries("thought")), 2)
        InnerLoop(
            MindStore(self.root),
            answer,
            catalog=capabilities.fixture_catalog(),
            max_rounds=2,
            idle_seconds=0,
        ).run()
        self.assertEqual(len(self.store.entries("model_call")), 2)
        with self.assertRaises(ValueError):
            InnerLoop(self.store, answer, catalog=capabilities.fixture_catalog(), max_rounds=31)

    def test_invalid_json_is_recorded_without_retry(self) -> None:
        calls: list[str] = []

        def broken(prompt: str) -> Record:
            calls.append(prompt)
            return {"text": "bad-json"}

        InnerLoop(
            self.store, broken, catalog=capabilities.fixture_catalog(), max_rounds=2, idle_seconds=0
        ).run()
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.store.entries("validation_error")), 2)
        self.assertEqual(self.store.entries("thought"), [])
        self.assertEqual(len(self.store.entries("model_response")), 2)

    def test_call_budget_includes_question_generation_and_survives_restart(self) -> None:
        from datetime import UTC, datetime

        self.store.append_belief(
            belief_id=None,
            subject="person",
            attribute="fact",
            value="spoken",
            source="inferred",
            confidence=0.4,
            last_verified=datetime.now(UTC).isoformat(),
            importance=0.9,
        )
        loop = InnerLoop(
            self.store,
            answer,
            catalog=[],
            max_rounds=30,
            max_model_calls=1,
            active_seconds=0,
            idle_seconds=0,
        )
        loop.run()
        self.assertEqual(len(self.store.entries("model_call")), 1)
        self.assertEqual(len(self.store.entries("question_generation_failure")), 1)
        InnerLoop(self.store, answer, catalog=[], max_model_calls=1).run()
        self.assertEqual(len(self.store.entries("model_call")), 1)

    def test_matching_source_executes_once_and_retains_provenance(self) -> None:
        def model(prompt: str) -> Record:
            return answer(prompt, question=True)

        loop = InnerLoop(
            self.store,
            model,
            catalog=capabilities.fixture_catalog(),
            max_rounds=2,
            idle_seconds=0,
            active_seconds=0,
        )
        loop.run()
        self.assertEqual(len(self.store.entries("action")), 1)
        states = loop.questions()
        self.assertEqual(states[0]["state"], "evidence_available")
        self.assertEqual(len(states), 1)
        self.assertTrue(self.store.entries("thought")[0]["refs"])
        tool_events = [
            r for r in self.store.entries("event") if r["payload"]["kind"] == "tool_result"
        ]
        self.assertTrue(tool_events[0]["payload"]["data"]["result"]["synthetic"])

    def test_scheduler_is_only_action_arbiter(self) -> None:
        from datetime import UTC, datetime

        belief = self.store.append_belief(
            belief_id=None,
            subject="person",
            attribute="fact",
            value="spoken",
            source="inferred",
            confidence=0.4,
            last_verified=datetime.now(UTC).isoformat(),
            importance=0.9,
        )

        def model(prompt: str) -> Record:
            parsed = json.loads(answer(prompt, question=True)["text"])
            parsed["relevance"] = [
                {
                    "belief_id": belief["belief_id"],
                    "capability_id": "location_track",
                    "score": 1.0,
                    "reason": "useful sample",
                }
            ]
            return {"text": json.dumps(parsed)}

        executed: list[str] = []

        def executor(capability: Record) -> Record:
            executed.append(capability["id"])
            return capabilities.execute(capability)

        loop = InnerLoop(
            self.store,
            model,
            catalog=capabilities.fixture_catalog(),
            max_rounds=2,
            executor=executor,
            idle_seconds=0,
            active_seconds=0,
        )
        loop.run()
        self.assertEqual(executed, ["location_track"])
        self.assertEqual(len(self.store.entries("action")), 1)
        self.assertEqual(self.store.entries("action")[0]["source"], "curiosity-scheduler")

    def test_missing_information_enters_sir_question_queue(self) -> None:
        belief = self.store.append_belief(
            belief_id=None,
            subject="person",
            attribute="reading",
            value="unknown preference",
            source="inferred",
            confidence=0.4,
            last_verified=datetime.now(UTC).isoformat(),
            importance=0.9,
        )

        def model(prompt: str) -> Record:
            context = json.loads(prompt)
            if "decision" in context or "proposed_question" in context:
                return answer(prompt)
            raw = json.loads(answer(prompt, question=True, select=False)["text"])
            raw["thoughts"][0]["refs"] = [belief["belief_id"]]
            return {"text": json.dumps(raw)}

        loop = InnerLoop(self.store, model, catalog=capabilities.fixture_catalog(), max_rounds=1)
        loop.run()
        self.assertEqual(loop.questions()[0]["state"], "ask_sir")
        self.assertEqual(self.store.entries("action"), [])

    def test_algorithm_question_always_queues_or_records_failure(self) -> None:
        from datetime import UTC, datetime

        self.store.append_belief(
            belief_id=None,
            subject="person",
            attribute="fact",
            value="spoken",
            source="inferred",
            confidence=0.4,
            last_verified=datetime.now(UTC).isoformat(),
            importance=0.9,
        )
        for valid in (True, False):
            with self.subTest(valid=valid):

                def model(prompt: str, valid: bool = valid) -> Record:
                    context = json.loads(prompt)
                    if "proposed_question" in context:
                        return {"text": json.dumps({"repeats_self_report": False, "reason": "new"})}
                    if "decision" in context:
                        return {
                            "text": json.dumps(
                                {"question": "Sir，能补充这件事吗？" if valid else None}
                            )
                        }
                    return answer(prompt)

                # Each case has its own ledger; no cross-case intent suppression.
                with tempfile.TemporaryDirectory(dir=Path.home()) as folder:
                    root = Path(folder)
                    root.chmod(0o700)
                    store = MindStore(root)
                    belief = self.store.beliefs()[0]
                    store.append_belief(
                        **{
                            k: belief[k]
                            for k in (
                                "belief_id",
                                "subject",
                                "attribute",
                                "value",
                                "source",
                                "confidence",
                                "last_verified",
                                "importance",
                            )
                        }
                    )
                    loop = InnerLoop(
                        store, model, catalog=[], max_rounds=2, active_seconds=0, idle_seconds=0
                    )
                    loop.run()
                    calls = store.entries("model_call")
                    self.assertEqual(
                        [c["payload"]["phase"] for c in calls],
                        (
                            ["thought", "question", "question_review", "thought"]
                            if valid
                            else ["thought", "question", "thought"]
                        ),
                    )
                    self.assertEqual(len(loop.questions()), int(valid))
                    self.assertEqual(
                        len(store.entries("question_generation_failure")), int(not valid)
                    )
                    self.assertEqual(store.entries("action"), [])
                    InnerLoop(store, model, catalog=[], max_rounds=2).run()
                    self.assertEqual(len(store.entries("model_call")), 4 if valid else 3)

    def test_money_and_security_never_execute(self) -> None:
        for field in ("costs_money", "security_sensitive"):
            with self.subTest(field=field):
                self.store = MindStore(self.root)
                catalog = capabilities.fixture_catalog()
                catalog[0][field] = True
                loop = InnerLoop(self.store, answer, catalog=catalog)
                thought = json.loads(
                    answer(
                        json.dumps({"events": [{"id": "a"}], "capabilities": catalog}),
                        question=True,
                    )["text"]
                )["thoughts"][0]
                thought["question"]["text"] = field
                loop.route(thought, field)
                self.assertFalse(
                    any(q["question_id"] == "scheduler:" + field for q in loop.questions())
                )
                decision = loop.curiosity.decide(
                    [],
                    {
                        "relevance_candidates": [],
                        "cached_relevance": [],
                        "as_of": "2026-10-16T00:00:00+00:00",
                    },
                    catalog,
                    1,
                )
                self.assertEqual(decision["kind"], "pending_sir_permission")
                loop.generate_question(decision, 1)
                self.assertEqual(loop.questions()[-1]["state"], "pending_sir_permission")
        self.assertEqual(self.store.entries("action"), [])

    def test_memory_and_events_reach_model_and_bad_references_rejected(self) -> None:
        fact = product_memory.MemoryStore(self.root / "memory.db").add("Sir 住在天水", "a", "b")
        loop = InnerLoop(self.store, answer, catalog=capabilities.fixture_catalog(), max_rounds=1)
        loop.ingest("camera", {"description": "fixture"}, source="camera-fixture")
        context = loop.context()
        self.assertEqual(context["memories"][0]["id"], fact["id"])
        output = answer(json.dumps(context))
        with self.assertRaises(ValueError):
            validate(output["text"], set(), capabilities.fixture_catalog(), [])
        loop.run()
        self.assertEqual(len(self.store.entries("thought")), 1)

    def test_source_cannot_be_skipped_when_marked_resolvable(self) -> None:
        context = {"events": [{"id": "a"}], "capabilities": capabilities.fixture_catalog()}
        output = json.loads(answer(json.dumps(context), question=True)["text"])
        output["thoughts"][0]["question"]["selected_capability"] = None
        with self.assertRaises(ValueError):
            validate(json.dumps(output), {"a"}, capabilities.fixture_catalog(), [])

    def test_full_context_is_scanned_and_seed_injection_blocks_dispatch(self) -> None:
        product_memory.MemoryStore(self.root / "memory.db").add("住址", "seed", "seed")
        calls: list[str] = []

        def model(prompt: str) -> Record:
            calls.append(prompt)
            return answer(prompt)

        InnerLoop(
            self.store, model, catalog=capabilities.fixture_catalog(), max_rounds=1, idle_seconds=0
        ).run()
        self.assertEqual(calls, [])
        self.assertIn("住址", self.store.entries("context_scan")[0]["payload"]["full_context_hits"])
        self.assertEqual(len(self.store.entries("context_error")), 1)

    def test_redundant_generated_question_is_reviewed_and_discarded(self) -> None:
        memory = product_memory.MemoryStore(self.root / "memory.db")
        memory.add("Sir 说他偏爱纸质书", "seed", "seed")
        phases: list[str] = []

        def model(prompt: str) -> Record:
            context = json.loads(prompt)
            if "proposed_question" in context:
                phases.append("review")
                return {"text": json.dumps({"repeats_self_report": True, "reason": "已有近期原话"})}
            if "decision" in context:
                phases.append("question")
                return {"text": json.dumps({"question": "Sir，您还是偏爱纸质书吗？"})}
            return answer(prompt)

        loop = InnerLoop(self.store, model, catalog=[])
        loop.generate_question(
            {
                "kind": "ask_sir",
                "belief_id": "new-question",
                "question_id": "q1",
                "reason": "missing",
            },
            1,
        )
        self.assertEqual(phases, ["question", "review"])
        self.assertEqual(loop.questions(), [])
        rejected = self.store.entries("question_rejected")
        self.assertEqual(rejected[0]["payload"]["question_id"], "q1")
        self.assertEqual(rejected[0]["payload"]["reason"], "repeats_self_report")

    def test_handled_question_does_not_starve_following_request(self) -> None:
        def model(prompt: str) -> Record:
            context = json.loads(prompt)
            if "proposed_question" in context:
                return {
                    "text": json.dumps({"repeats_self_report": False, "reason": "new information"})
                }
            return answer(prompt)

        loop = InnerLoop(self.store, model, catalog=[])
        for identity in ("q1", "q2"):
            self.store.append_belief(
                belief_id=identity,
                subject="person",
                attribute=identity,
                value="unknown",
                source="inferred",
                confidence=0.4,
                last_verified=datetime.now(UTC).isoformat(),
                importance=0.3,
            )
            self.store.append(
                "action_request",
                {
                    "question_id": identity,
                    "belief_id": identity,
                    "question": {
                        "text": identity,
                        "selected_capability": None,
                        "missing_information": "unknown",
                    },
                },
                source="model",
            )
        context: Record = {
            "beliefs": self.store.beliefs(),
            "relevance_candidates": self.store.beliefs(),
            "cached_relevance": [],
            "as_of": datetime.now(UTC).isoformat(),
        }
        first = loop.curiosity.decide([], context, [], 1)
        loop.generate_question(first, 1)
        self.assertEqual(loop.questions()[0]["question_id"], "q1")
        second = loop.curiosity.decide([], context, [], 2)
        self.assertEqual(second["question_id"], "q2")

    def test_model_error_retains_safe_exception_type(self) -> None:
        def model(prompt: str) -> Record:
            raise TimeoutError("secret details must not be logged")

        InnerLoop(self.store, model, catalog=capabilities.fixture_catalog(), max_rounds=1).run()
        payload = self.store.entries("model_error")[0]["payload"]
        self.assertEqual(payload["error_type"], "TimeoutError")
        self.assertNotIn("secret", json.dumps(payload))

    def test_max_call_budget_allows_forty_and_rejects_forty_one(self) -> None:
        InnerLoop(self.store, answer, catalog=capabilities.fixture_catalog(), max_model_calls=40)
        with self.assertRaises(ValueError):
            InnerLoop(
                self.store, answer, catalog=capabilities.fixture_catalog(), max_model_calls=41
            )

    def test_unknown_implementation_cannot_execute(self) -> None:
        forged = capabilities.fixture_catalog()[0]
        forged["call"]["name"] = "shell"
        with self.assertRaises(ValueError):
            capabilities.execute(forged)


class InboxTests(unittest.TestCase):
    def test_events_survive_restart_and_cannot_be_rewritten(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("inner_loop"), "persistent mind inbox is not implemented"
        )
        import inner_loop

        with tempfile.TemporaryDirectory(dir=Path.home()) as folder:
            root = Path(folder)
            root.chmod(0o700)
            store = inner_loop.MindStore(root)
            event = store.ingest("conversation", {"text": "hello"}, source="chat")
            again = inner_loop.MindStore(root)
            self.assertEqual(again.entries("event")[0]["id"], event["id"])
            self.assertEqual(event["source"], "chat")
            self.assertTrue(event["created_at"])
            with again.connection() as db:
                with self.assertRaises(inner_loop.sqlite3.IntegrityError):
                    db.execute("DELETE FROM entries")
                with self.assertRaises(inner_loop.sqlite3.IntegrityError):
                    db.execute("UPDATE entries SET source='other'")


if __name__ == "__main__":
    unittest.main()
