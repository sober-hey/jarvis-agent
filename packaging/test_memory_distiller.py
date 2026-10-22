from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager, closing
from pathlib import Path
from unittest import mock

import context_limits
import memory_distiller
from memory_distiller import (
    STRICT_JSON_SUFFIX,
    MemoryDistiller,
    ModelOutputError,
    parse_model_json,
)


def extraction(*, key: str = "bedtime", refs: list[str] | None = None) -> str:
    return json.dumps(
        {
            "preferences": [{"key": key, "value": "23:00", "evidence_ids": refs or []}],
            "hypotheses": [],
            "patterns": [],
        }
    )


class FakeProfile:
    def __init__(self) -> None:
        self.facts: list[dict[str, object]] = []

    @contextmanager
    def connection(self, write: bool = False):
        yield None

    def upsert(self, **fields: object) -> dict[str, object]:
        self.facts.append(fields)
        return {"applied": True}

    def upsert_in_transaction(self, db: object, **fields: object) -> dict[str, object]:
        self.facts.append(fields)
        return {"applied": True}


class FakeHypotheses:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def propose(self, **fields: object) -> None:
        self.items.append(fields)


class ParseModelJsonTests(unittest.TestCase):
    def test_strict_object_and_null_are_accepted(self) -> None:
        self.assertEqual(parse_model_json('{"a": 1}'), {"a": 1})
        self.assertIsNone(parse_model_json("null"))

    def test_fence_preamble_and_trailing_prose_are_tolerated(self) -> None:
        text = 'Here is the result:\n```json\n{"preferences": [], "hypotheses": []}\n```\nDone.'
        self.assertEqual(parse_model_json(text), {"preferences": [], "hypotheses": []})
        self.assertEqual(parse_model_json('Result: {"a": 1} — hope that helps'), {"a": 1})

    def test_trailing_commas_are_repaired_without_touching_strings(self) -> None:
        text = '{"a": ",}", "b": [1, 2,],}'
        self.assertEqual(parse_model_json(text), {"a": ",}", "b": [1, 2]})

    def test_prose_and_trailing_commas_can_be_combined(self) -> None:
        text = 'Result follows: {"a": ",}", "b": [1, 2,],} End of result.'
        self.assertEqual(parse_model_json(text), {"a": ",}", "b": [1, 2]})

    def test_truncated_output_reports_a_safe_reason(self) -> None:
        for text in ('{"preferences": [', '{"preferences": [{"key": "private-data'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ModelOutputError, "truncated") as raised:
                    parse_model_json(text)
                self.assertNotIn("private-data", str(raised.exception))

    def test_control_characters_inside_strings_are_tolerated(self) -> None:
        self.assertEqual(parse_model_json('{"a": "line\nbreak"}'), {"a": "line\nbreak"})

    def test_bom_is_ignored(self) -> None:
        self.assertEqual(parse_model_json('\ufeff{"a": 1}'), {"a": 1})

    def test_unparseable_output_raises_model_output_error(self) -> None:
        for text in ('{"preferences": [', "not json at all", "", '{"a": NaN}'):
            with self.subTest(text=text):
                with self.assertRaises(ModelOutputError):
                    parse_model_json(text)

    def test_truncated_outer_document_is_never_salvaged_from_nested_object(self) -> None:
        with self.assertRaisesRegex(ModelOutputError, "truncated"):
            parse_model_json('{"preferences": [], "hypotheses": [], "partial": {"ok": true}')

    def test_escaped_quotes_and_delimiters_inside_strings_are_preserved(self) -> None:
        value = {"text": 'quote " and backslash \\ and comma ,} and bracket ]'}
        self.assertEqual(parse_model_json("Result: " + json.dumps(value)[:-1] + ",} Done."), value)

    def test_non_text_output_is_rejected(self) -> None:
        with self.assertRaises(ModelOutputError):
            parse_model_json(None)  # type: ignore[arg-type]


class MemoryDistillerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="distill-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.db = sqlite3.connect(self.root / "mind.db")
        self.db.execute(
            "CREATE TABLE entries (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "id TEXT NOT NULL UNIQUE, category TEXT NOT NULL, created_at TEXT NOT NULL, "
            "source TEXT NOT NULL, payload TEXT NOT NULL, refs TEXT NOT NULL)"
        )
        self.ids = []
        for number in range(3):
            identity = f"entry-{number}"
            self.ids.append(identity)
            self.db.execute(
                "INSERT INTO entries(id,category,created_at,source,payload,refs) "
                "VALUES(?,?,?,?,?,?)",
                (
                    identity,
                    "thought",
                    "2026-10-19T00:00:00+00:00",
                    "unit",
                    json.dumps({"text": "x" * 600}),
                    "[]",
                ),
            )
        self.db.commit()
        self.db.close()
        self.profile = FakeProfile()
        self.hypotheses = FakeHypotheses()
        self.distiller = MemoryDistiller(self.root, self.profile, self.hypotheses, max_entries=10)
        # One observation per request: each record carries 512 bytes of payload.
        self.limits = mock.patch.object(context_limits, "CHAT_CONTEXT_BYTE_LIMIT", 1800)
        self.limits.start()
        self.addCleanup(self.limits.stop)

    def state(self) -> dict[str, object]:
        path = self.root / "distillation_log.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def test_decorated_replies_succeed_without_repair_calls(self) -> None:
        for decorate in (
            lambda text: "```json\n" + text + "\n```",
            lambda text: "Explanation before.\n" + text + "\nExplanation after.",
            lambda text: "Result: " + text[:-1] + ",} Done.",
        ):
            with self.subTest(decorate=decorate):
                self.distiller.log_path.unlink(missing_ok=True)
                with mock.patch.object(
                    memory_distiller, "distillation_model", return_value=decorate(extraction())
                ) as model:
                    result = self.distiller.distill()
                self.assertEqual(model.call_count, 3)
                self.assertEqual(result.entries_processed, 3)
                self.assertIsNone(result.error)

    def test_persistence_failure_does_not_acknowledge_failed_batch(self) -> None:
        def fail(**fields: object) -> None:
            raise OSError("fixture persistence failure")

        with (
            mock.patch.object(memory_distiller, "distillation_model", return_value=extraction()),
            mock.patch.object(self.profile, "upsert", side_effect=fail),
        ):
            with self.assertRaises(OSError):
                self.distiller.distill()
        self.assertNotIn("last_entry_id", self.state())
        self.assertTrue(self.distiller.get_trigger_check())
        with mock.patch.object(memory_distiller, "distillation_model", return_value=extraction()):
            self.assertEqual(self.distiller.distill().entries_processed, 3)

    def test_real_stores_retry_after_checkpoint_failure_without_duplicate_hypothesis(self) -> None:
        import product_home
        from hypothesis_engine import HypothesisEngine
        from sir_profile import SirProfile

        # Scratch is a test-owned host fixture; leave all other root/path/mode
        # checks intact and never open the operator's product root.
        with mock.patch.object(product_home, "protected_paths", return_value=()):
            profile = SirProfile(self.root)
            hypotheses = HypothesisEngine(self.root)
        distiller = MemoryDistiller(self.root, profile, hypotheses, max_entries=3)
        self.limits.stop()  # Exercise one real cross-store batch, not three fixture-sized requests.
        output = json.loads(extraction())
        output["hypotheses"] = [
            {
                "claim": "A fixture observation may recur",
                "basis": "Fixture evidence only",
                "evidence_ids": [self.ids[0]],
            }
        ]
        with (
            mock.patch.object(
                memory_distiller, "distillation_model", return_value=json.dumps(output)
            ),
            mock.patch.object(distiller, "_checkpoint", side_effect=OSError("checkpoint fixture")),
        ):
            with self.assertRaises(OSError):
                distiller.distill()
        self.assertNotIn("last_entry_id", self.state())
        with mock.patch.object(
            memory_distiller, "distillation_model", return_value=json.dumps(output)
        ):
            resumed = distiller.distill()
        self.assertEqual(resumed.entries_processed, 3)
        with profile.connection() as db:
            fact = db.execute("SELECT source,evidence_ids FROM facts").fetchone()
            self.assertEqual(fact["source"], "inferred")
            self.assertEqual(json.loads(fact["evidence_ids"]), self.ids)
            self.assertEqual(db.execute("SELECT count(*) FROM facts").fetchone()[0], 1)
        with hypotheses.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hypotheses").fetchone()[0], 1)
        self.assertEqual(self.state()["last_entry_id"], self.ids[-1])

    def test_daemon_worker_records_reason_and_retains_retryable_entries(self) -> None:
        import threading
        from types import SimpleNamespace
        from daemon_wiring import Wiring

        logged: list[tuple[str, dict[str, object]]] = []
        worker = SimpleNamespace(
            memory_distiller=self.distiller,
            _distillation_lock=threading.Lock(),
            _distillation_running=True,
            _distillation_retry_at=0.0,
            log=lambda event, **fields: logged.append((event, fields)),
        )
        with mock.patch.object(
            memory_distiller, "distillation_model", return_value='{"preferences": ['
        ):
            Wiring._run_distillation(worker)
        self.assertFalse(worker._distillation_running)
        self.assertGreater(worker._distillation_retry_at, 0)
        self.assertEqual(logged[0][1]["error"], "ModelOutputError:truncated")
        self.assertEqual(logged[0][1]["entries"], 0)
        self.assertEqual(logged[0][1]["pending"], 3)
        self.assertTrue(self.distiller.get_trigger_check())

    def test_malformed_reply_is_repaired_by_one_bounded_retry(self) -> None:
        prompts: list[str] = []

        def model(root: Path, prompt: str) -> str:
            prompts.append(prompt)
            return '{"preferences": [' if len(prompts) == 1 else extraction()

        with mock.patch.object(memory_distiller, "distillation_model", model):
            result = self.distiller.distill()

        self.assertEqual(len(prompts), 4)
        self.assertTrue(prompts[1].endswith(STRICT_JSON_SUFFIX))
        self.assertLessEqual(len(prompts[1].encode("utf-8")), 1800)
        self.assertIsNone(result.error)
        self.assertEqual(result.entries_processed, 3)
        self.assertEqual(result.pending_entries, 0)
        self.assertEqual(result.preferences_written, 3)

    def test_persistent_garbage_stops_the_round_without_losing_entries(self) -> None:
        calls = 0

        def model(root: Path, prompt: str) -> str:
            nonlocal calls
            calls += 1
            return "I cannot comply with JSON today."

        with mock.patch.object(memory_distiller, "distillation_model", model):
            result = self.distiller.distill()

        # Only the first batch is attempted (plus its single repair), not all three.
        self.assertEqual(calls, 2)
        self.assertEqual(result.error, "ModelOutputError:invalid_json")
        self.assertEqual(result.entries_processed, 0)
        self.assertEqual(result.pending_entries, 3)
        self.assertIsNone(result.last_entry_id)
        self.assertEqual(self.profile.facts, [])
        self.assertNotIn("last_entry_id", self.state())
        self.assertEqual(self.state()["pending_entries"], 3)
        self.assertEqual(self.state()["last_error"], result.error)
        self.assertTrue(self.distiller.get_trigger_check())

    def test_truncated_batch_reports_reason_without_advancing_checkpoint(self) -> None:
        with mock.patch.object(
            memory_distiller, "distillation_model", return_value='{"preferences": ['
        ):
            result = self.distiller.distill()
        self.assertEqual(result.error, "ModelOutputError:truncated")
        self.assertEqual(result.entries_processed, 0)
        self.assertEqual(result.pending_entries, 3)
        self.assertNotIn("last_entry_id", self.state())

    def test_cold_start_failure_retries_same_entries_after_new_arrivals_and_restart(self) -> None:
        self.distiller.max_entries = 2
        with mock.patch.object(
            memory_distiller, "distillation_model", return_value='{"preferences": ['
        ):
            failed = self.distiller.distill()
        self.assertEqual(failed.entries_processed, 0)
        self.assertNotIn("last_entry_id", self.state())
        with closing(sqlite3.connect(self.root / "mind.db")) as db:
            db.execute(
                "INSERT INTO entries(id,category,created_at,source,payload,refs) VALUES(?,?,?,?,?,?)",
                ("new-arrival", "thought", "2026-10-19T00:01:00+00:00", "unit", "{}", "[]"),
            )
            db.commit()
        restarted = MemoryDistiller(self.root, self.profile, self.hypotheses, max_entries=2)
        prompts = []

        def model(root: Path, prompt: str) -> str:
            prompts.append(prompt)
            return extraction()

        with mock.patch.object(memory_distiller, "distillation_model", model):
            resumed = restarted.distill()
        observations = [
            row["id"]
            for prompt in prompts
            for row in json.loads(prompt.split("OBSERVATIONS_JSON:\n", 1)[1])
        ]
        self.assertEqual(observations, self.ids[1:])
        self.assertEqual(resumed.entries_processed, 2)
        self.assertEqual(self.state()["last_entry_id"], self.ids[2])

    def test_pending_small_tail_remains_triggerable(self) -> None:
        with mock.patch.object(
            memory_distiller, "distillation_model", side_effect=[extraction(), "{", "{"]
        ):
            self.distiller.distill()
        self.assertTrue(self.distiller.get_trigger_check())
        with mock.patch.object(memory_distiller, "distillation_model", return_value=extraction()):
            self.distiller.distill()
        self.assertFalse(self.distiller.get_trigger_check())

    def test_failed_later_batch_keeps_earlier_progress_and_pending_tail(self) -> None:
        replies = [extraction(), '{"preferences": [', '{"preferences": [']

        def model(root: Path, prompt: str) -> str:
            return replies.pop(0)

        with mock.patch.object(memory_distiller, "distillation_model", model):
            result = self.distiller.distill()

        state = self.state()
        self.assertEqual(result.error, "ModelOutputError:truncated")
        self.assertEqual(result.entries_processed, 1)
        self.assertEqual(result.pending_entries, 2)
        self.assertEqual(len(self.profile.facts), 1)
        self.assertEqual(state["last_entry_id"], self.ids[0])
        self.assertEqual(state["entries_processed"], 1)
        self.assertEqual(state["preferences_written"], 1)

        # The next round resumes at entry-1 and does not replay entry-0.
        with mock.patch.object(
            memory_distiller, "distillation_model", lambda root, prompt: extraction()
        ):
            resumed = self.distiller.distill()

        self.assertIsNone(resumed.error)
        self.assertEqual(resumed.entries_processed, 2)
        self.assertEqual(resumed.pending_entries, 0)
        self.assertEqual(len(self.profile.facts), 3)
        self.assertEqual(self.state()["last_entry_id"], self.ids[2])

    def test_provider_failure_propagates_and_keeps_finished_batches(self) -> None:
        calls = 0

        def model(root: Path, prompt: str) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return extraction()
            raise RuntimeError("DistillationModelUnavailable")

        with mock.patch.object(memory_distiller, "distillation_model", model):
            with self.assertRaises(RuntimeError):
                self.distiller.distill()

        self.assertEqual(self.state()["last_entry_id"], self.ids[0])
        self.assertEqual(len(self.profile.facts), 1)

    def test_unknown_evidence_id_triggers_repair_not_a_crash(self) -> None:
        replies = [extraction(refs=["not-an-entry"])]

        def model(root: Path, prompt: str) -> str:
            return replies.pop(0) if replies else extraction()

        with mock.patch.object(memory_distiller, "distillation_model", model):
            result = self.distiller.distill()

        self.assertIsNone(result.error)
        self.assertEqual(result.entries_processed, 3)
        self.assertEqual(self.profile.facts[0]["evidence_ids"], [self.ids[0]])


if __name__ == "__main__":
    unittest.main()
