"""Threaded scheduling against injected endpoints only; no CLI/model calls."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import task_scheduler as protocol
from agent_protocol import ExecutorType, TaskMessage, TaskResult


class FakeEndpoint:
    def __init__(self, function=None):
        self.calls = []
        self.function = function
        self.lock = threading.Lock()

    def execute(self, message):
        with self.lock:
            self.calls.append(message.task_id)
        if self.function:
            return self.function(message)
        return TaskResult(message.task_id, "completed", output="offline:" + message.description)

    def health_check(self):
        return True


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(
            dir=Path(os.environ["TMPDIR"]).resolve(), prefix="scheduler-offline-"
        )
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.scheduler = protocol.TaskScheduler(self.root, SimpleNamespace(), max_concurrent=1)
        self.addCleanup(self.scheduler.shutdown)
        self.endpoint = FakeEndpoint()
        self.scheduler.executors = {kind: self.endpoint for kind in ExecutorType}
        # Leaf sentinels fail before real credentials, subprocesses or models.
        for target in (
            "task_scheduler.execute_task",
            "task_scheduler.subprocess.run",
            "product_config.read_config",
            "product_config.read_key",
        ):
            guard = patch(target, side_effect=AssertionError("offline test crossed leaf boundary"))
            guard.start()
            self.addCleanup(guard.stop)

    def test_priority_dependency_order_and_results_drained_once(self):
        low = TaskMessage(task_id="low", description="low", priority=0)
        high = TaskMessage(task_id="high", description="high", priority=3)
        child = TaskMessage(task_id="child", description="child", priority=3, dependencies=["low"])
        for message in (low, child, high):
            self.scheduler.submit(message)
        self.scheduler.schedule()
        self.assertEqual(self.endpoint.calls, ["high", "low", "child"])
        results = self.scheduler.collect_results()
        self.assertEqual([r.task_id for r in results], self.endpoint.calls)
        self.assertTrue(all(r.status == "completed" for r in results))
        self.assertEqual(self.scheduler.collect_results(), [])
        self.assertEqual(self.scheduler.status()["completed"], 3)

    def test_unknown_dependency_blocks_until_submitted(self):
        self.scheduler.submit(
            TaskMessage(task_id="child", description="child", dependencies=["parent"])
        )
        self.scheduler.schedule()
        self.assertEqual(self.endpoint.calls, [])
        self.assertEqual(self.scheduler.status()["blocked"], 1)
        self.scheduler.submit(TaskMessage(task_id="parent", description="parent"))
        self.scheduler.schedule()
        self.assertEqual(self.endpoint.calls, ["parent", "child"])

    def test_failure_propagates_without_executing_descendants(self):
        self.endpoint.function = lambda m: TaskResult(m.task_id, "failed", error="injected failure")
        for task_id, dependencies in [
            ("grandchild", ["child"]),
            ("child", ["parent"]),
            ("parent", []),
        ]:
            self.scheduler.submit(
                TaskMessage(task_id=task_id, description=task_id, dependencies=dependencies)
            )
        self.scheduler.schedule()
        self.assertEqual(self.endpoint.calls, ["parent"])
        self.assertEqual(
            {r.task_id: r.status for r in self.scheduler.collect_results()},
            dict.fromkeys(["parent", "child", "grandchild"], "failed"),
        )

    def test_submission_snapshots_caller_input_and_rejects_cycles_and_duplicates(self):
        message = TaskMessage(task_id="a", description="original", metadata={"nested": [1]})
        self.scheduler.submit(message)
        message.description = "changed"
        message.metadata["nested"].append(2)
        queued = self.scheduler._messages["a"]
        self.assertEqual(queued.description, "original")
        self.assertEqual(queued.metadata, {"nested": [1]})
        with self.assertRaises(ValueError):
            self.scheduler.submit(TaskMessage(task_id="a", description="duplicate"))
        self.scheduler.submit(TaskMessage(task_id="b", description="b", dependencies=["c"]))
        with self.assertRaises(ValueError):
            self.scheduler.submit(TaskMessage(task_id="c", description="c", dependencies=["b"]))
        self.assertNotIn("c", self.scheduler._messages)

    def test_cancel_pending_fails_dependents_without_execution(self):
        self.scheduler.submit(TaskMessage(task_id="parent", description="parent"))
        self.scheduler.submit(
            TaskMessage(task_id="child", description="child", dependencies=["parent"])
        )
        self.scheduler.cancel("parent")
        self.scheduler.schedule()
        self.assertEqual(self.endpoint.calls, [])
        self.assertEqual(
            {r.task_id: r.status for r in self.scheduler.collect_results()},
            {"parent": "cancelled", "child": "failed"},
        )
        with self.assertRaises(KeyError):
            self.scheduler.cancel("missing")

    def test_deadline_keeps_slot_and_discards_late_result(self):
        entered = threading.Event()
        release = threading.Event()
        stopped = threading.Event()

        def execute(message):
            if message.task_id == "slow":
                entered.set()
                try:
                    if not release.wait(5):
                        raise RuntimeError("test release missing")
                finally:
                    stopped.set()
            return TaskResult(message.task_id, "completed", output="late")

        self.endpoint.function = execute
        # Release must precede shutdown even if an assertion fails.
        self.addCleanup(release.set)
        self.scheduler.submit(TaskMessage(task_id="slow", description="slow", timeout_seconds=1))
        self.scheduler.submit(TaskMessage(task_id="next", description="next"))
        self.scheduler.schedule()
        self.assertTrue(entered.is_set())
        self.assertEqual(self.scheduler.status()["running"], 1)
        self.assertEqual(self.scheduler.status()["pending"], 1)
        self.assertEqual(self.endpoint.calls, ["slow"])
        self.assertEqual(
            [(r.task_id, r.status) for r in self.scheduler.collect_results()], [("slow", "timeout")]
        )
        release.set()
        self.assertTrue(stopped.wait(2))
        # Join the actual worker, then dispatch the next task.
        for future in list(self.scheduler._active.values()):
            future.result(timeout=2)
        self.scheduler.schedule()
        self.assertEqual(
            [(r.task_id, r.status) for r in self.scheduler.collect_results()],
            [("next", "completed")],
        )
        self.assertEqual(self.scheduler.status()["timeout"], 1)

    def test_running_cancellation_is_cooperative(self):
        entered, release = threading.Event(), threading.Event()

        def execute(message):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release missing")
            return TaskResult(message.task_id, "completed", output="discard")

        self.endpoint.function = execute
        self.addCleanup(release.set)
        self.scheduler.submit(TaskMessage(task_id="active", description="active"))
        self.scheduler.schedule(block=False)
        self.assertTrue(entered.wait(2))
        self.scheduler.cancel("active")
        self.assertEqual(self.scheduler.status()["cancellation_requested"], 1)
        self.assertEqual(self.scheduler.collect_results(), [])
        release.set()
        self.scheduler.schedule()
        self.assertEqual(
            [(r.task_id, r.status) for r in self.scheduler.collect_results()],
            [("active", "cancelled")],
        )

    def test_bad_executor_and_exception_are_terminal_failures(self):
        for function in (
            lambda m: TaskResult("different", "completed"),
            lambda m: (_ for _ in ()).throw(RuntimeError("injected")),
        ):
            self.endpoint.function = function
            message = TaskMessage(description="invalid executor")
            self.scheduler.submit(message)
            self.scheduler.schedule()
            results = self.scheduler.collect_results()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].status, "failed")
            self.assertEqual(results[0].task_id, message.task_id)

    def test_decompose_resolves_aliases_and_ignores_executor_overrides(self):
        payload = [
            {"id": "one", "description": "first", "metadata": {"executor_type": "local_script"}},
            {"id": "two", "type": "code", "description": "second", "dependencies": ["one"]},
        ]
        with patch.object(
            self.scheduler.model_executor,
            "execute",
            return_value=TaskResult(
                "decompose", "completed", output="```json\n" + json.dumps(payload) + "\n```"
            ),
        ) as execute:
            messages = self.scheduler.decompose("offline complex task")
        execute.assert_called_once()
        self.assertEqual(messages[1].dependencies, [messages[0].task_id])
        self.assertEqual(messages[0].metadata, {})
        self.assertEqual(self.scheduler.status()["total"], 0)

    def test_decompose_rejects_bad_graph_atomically(self):
        for payload in (
            [],
            {},
            [{"description": "a", "dependencies": ["missing"]}],
            [
                {"id": "a", "description": "a", "dependencies": ["b"]},
                {"id": "b", "description": "b", "dependencies": ["a"]},
            ],
            [{"description": ""}],
        ):
            with (
                self.subTest(payload=payload),
                patch.object(
                    self.scheduler.model_executor,
                    "execute",
                    return_value=TaskResult("decompose", "completed", output=json.dumps(payload)),
                ),
            ):
                with self.assertRaises(ValueError):
                    self.scheduler.decompose("complex task")
                self.assertEqual(self.scheduler.status()["total"], 0)

    def test_concurrency_limit_rejects_invalid_values_and_closed_submission(self):
        for value in (0, True, 1.5):
            with self.assertRaises(ValueError):
                self.scheduler.max_concurrent = value
        self.scheduler.shutdown()
        with self.assertRaises(RuntimeError):
            self.scheduler.submit(TaskMessage(description="closed"))


class SelfEvaluationTests(unittest.TestCase):
    def test_self_evaluate_contract(self):
        hook = getattr(protocol, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-level self_evaluate is required")
        before = datetime.now(UTC)
        report = hook()
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
        self.assertEqual(report["module"], "task_scheduler")
        for field in ("domain", "current_version"):
            self.assertIsInstance(report[field], str)
            self.assertTrue(report[field].strip())
        for field in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[field], list)
            self.assertTrue(report[field])
            self.assertTrue(all(isinstance(item, str) and item.strip() for item in report[field]))
        stamp = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.utcoffset(), UTC.utcoffset(stamp))
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, datetime.now(UTC))
        json.dumps(report, allow_nan=False)

    def test_self_evaluate_is_fresh_and_has_no_execution_or_file_effects(self):
        hook = getattr(protocol, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-level self_evaluate is required")
        with (
            patch("builtins.open", side_effect=AssertionError("unexpected file access")),
            patch.object(
                protocol.TaskScheduler,
                "schedule",
                side_effect=AssertionError("unexpected protocol use"),
            ),
        ):
            first = hook()
            first["known_limitations"].append("caller-only")
            first["improvement_directions"].clear()
            second = hook()
        self.assertNotIn("caller-only", second["known_limitations"])
        self.assertTrue(second["improvement_directions"])


class AdmissionTests(unittest.TestCase):
    def test_registry_detects_real_module_hook(self):
        from integration_registry import REGISTRY

        self.assertTrue(REGISTRY._has_self_evaluate(REGISTRY.get("task_scheduler")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
