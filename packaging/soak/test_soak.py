# pyright: strict
"""Offline analyzer fixtures; no product services or models are imported."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Protocol, cast
from unittest import mock


class Analyzer(Protocol):
    def analyze(self, samples: list[dict[str, Any]]) -> dict[str, Any]: ...

    def write_summary(self, samples_path: Path, output_dir: Path) -> dict[str, Any]: ...


def load_analyzer() -> Analyzer:
    path = Path(__file__).with_name("analyze_soak.py")
    if not path.exists():
        raise AssertionError("analyze_soak.py has not been implemented")
    spec = importlib.util.spec_from_file_location("local_soak_analyzer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(Analyzer, module)


class AnalyzerTests(unittest.TestCase):
    def test_missing_data_is_unknown_not_zero_or_pass(self) -> None:
        result = load_analyzer().analyze([])
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["observed_wall_seconds"])
        self.assertEqual(result["queues"]["status"], "UNKNOWN")
        self.assertEqual(result["model_calls"]["status"], "UNKNOWN")
        self.assertIsNone(result["model_calls"]["failure_rate"])
        self.assertEqual(result["sample_count"], 0)

    def test_memory_ols_is_per_contiguous_pid_segment_in_wall_time(self) -> None:
        samples: list[dict[str, Any]] = [
            {
                "elapsed_seconds": t,
                "logical_elapsed_seconds": t * 10,
                "components": {
                    "web": {"pid": pid, "restart_count": restart, "rss_bytes": mb * 1_000_000}
                },
            }
            for t, pid, restart, mb in [
                (0, 10, 0, 100),
                (1800, 10, 0, 110),
                (3600, 10, 0, 120),
                (3605, 20, 1, 500),
                (5405, 20, 1, 500),
                (7205, 20, 1, 500),
            ]
        ]
        result = load_analyzer().analyze(samples)
        self.assertEqual(result["observed_wall_seconds"], 7205)
        segments = result["components"]["web"]["memory"]["segments"]
        self.assertEqual(len(segments), 2)
        self.assertAlmostEqual(segments[0]["slope_mb_per_hour"], 20.0)
        self.assertTrue(segments[0]["continuous_growth"])
        self.assertAlmostEqual(segments[1]["slope_mb_per_hour"], 0.0)
        self.assertFalse(segments[1]["continuous_growth"])
        # Legacy slopes remain diagnostic; three samples/hour cannot qualify
        # five-minute RSS windows or distinguish a plateau from sustained growth.
        self.assertEqual(result["components"]["web"]["memory"]["status"], "UNKNOWN")

    @staticmethod
    def fixture(times: tuple[int, ...] = (0, 5, 10)) -> list[dict[str, Any]]:
        counters = {
            "enqueued": 10,
            "delivered": 8,
            "mind_enqueued": 5,
            "consumed": 2,
            "suspended": 1,
            "pending": 2,
            "upstream_pending": 2,
            "duplicate_processing": 0,
            "duplicate_consumption": 0,
            "thoughts": 2,
            "scheduler_executions": 0,
            "ask_sir_queue": 0,
            "active_messages": 0,
            "model_success": 2,
            "model_failed": 0,
            "model_unknown": 0,
            "handoff_unaccounted": 0,
        }
        return [
            {
                "elapsed_seconds": t,
                "logical_elapsed_seconds": t * 10,
                "components": {
                    name: {
                        "pid": pid,
                        "restart_count": 0,
                        "state": "running",
                        "heartbeat": "healthy",
                        "rss_bytes": 100_000_000,
                        "cpu_percent": 1.0,
                        "open_files": 5,
                    }
                    for name, pid in (("web", 11), ("jarvis-daemon", 12))
                },
                "counters": dict(counters),
                "database_bytes": {"daemon.db": 100},
                "observation_errors": [],
            }
            for t in times
        ]

    def test_two_stage_reconciliation_does_not_compare_inbox_to_mind(self) -> None:
        samples = self.fixture()
        result = load_analyzer().analyze(samples)
        queues = result["queues"]
        self.assertEqual(queues["inbox"]["status"], "OBSERVED_OK")
        self.assertEqual(queues["inbox"]["unreconciled"], 0)
        self.assertEqual(queues["mind"]["status"], "OBSERVED_OK")
        self.assertEqual(queues["mind"]["unreconciled"], 0)
        self.assertEqual(queues["backlog"]["mind"]["status"], "OBSERVED_OK")
        self.assertEqual(queues["status"], "OBSERVED_OK")
        # Queue observations are complete, but ten seconds cannot qualify RSS.
        self.assertEqual(result["status"], "UNKNOWN")
        before = json.dumps(samples, sort_keys=True)
        load_analyzer().analyze(samples)
        self.assertEqual(before, json.dumps(samples, sort_keys=True))

    def test_lost_overaccounted_and_duplicate_counts_are_issues(self) -> None:
        for key, value, stage, expected in (
            ("mind_enqueued", 6, "mind", 1),
            ("consumed", 3, "mind", -1),
            ("delivered", 7, "inbox", 1),
        ):
            with self.subTest(key=key):
                samples = self.fixture()
                samples[-1]["counters"][key] = value
                queues = load_analyzer().analyze(samples)["queues"]
                self.assertEqual(queues[stage]["status"], "ISSUE")
                self.assertEqual(queues[stage]["unreconciled"], expected)
        samples = self.fixture()
        samples[1]["counters"]["duplicate_processing"] = 1
        samples[1]["counters"]["duplicate_consumption"] = 2
        duplicates = load_analyzer().analyze(samples)["duplicates"]
        self.assertEqual(duplicates["status"], "ISSUE")
        self.assertEqual(duplicates["processing_max"], 1)
        self.assertEqual(duplicates["consumption_max"], 2)

    def test_missing_queue_counts_are_unknown_even_if_last_is_complete(self) -> None:
        samples = self.fixture()
        samples[1]["counters"]["mind_enqueued"] = None
        queues = load_analyzer().analyze(samples)["queues"]
        self.assertEqual(queues["mind"]["status"], "UNKNOWN")
        samples = self.fixture()
        samples[-1]["counters"].pop("upstream_pending")
        self.assertEqual(load_analyzer().analyze(samples)["queues"]["inbox"]["status"], "UNKNOWN")

    def test_backlog_growth_is_observed_not_assumed_from_nonempty_end(self) -> None:
        samples = self.fixture()
        for index, sample in enumerate(samples):
            sample["counters"]["pending"] = 2 + index
            sample["counters"]["mind_enqueued"] = 5 + index
            sample["counters"]["upstream_pending"] = 2 + index
            sample["counters"]["enqueued"] = 10 + index
        backlog = load_analyzer().analyze(samples)["queues"]["backlog"]
        for stage in ("inbox", "mind"):
            self.assertEqual(backlog[stage]["status"], "ISSUE")
            self.assertAlmostEqual(backlog[stage]["slope_items_per_hour"], 720.0)
        self.assertEqual(
            load_analyzer().analyze(self.fixture((0,)))["queues"]["backlog"]["mind"]["status"],
            "UNKNOWN",
        )

    def test_one_restart_is_not_repeated_but_three_and_fuse_are_issues(self) -> None:
        for count, expected in ((1, "OBSERVED_OK"), (3, "ISSUE")):
            samples = self.fixture()
            samples[-1]["components"]["web"]["restart_count"] = count
            restarts = load_analyzer().analyze(samples)["components"]["web"]["restarts"]
            self.assertEqual(restarts["status"], expected)
            self.assertEqual(restarts["repeated_threshold"], 3)
        samples = self.fixture()
        samples[1]["components"]["web"]["state"] = "circuit_open"
        restarts = load_analyzer().analyze(samples)["components"]["web"]["restarts"]
        self.assertTrue(restarts["fused_observed"])
        self.assertEqual(restarts["status"], "ISSUE")
        samples = self.fixture()
        samples[1]["components"]["web"].pop("restart_count")
        self.assertEqual(
            load_analyzer().analyze(samples)["components"]["web"]["restarts"]["status"], "UNKNOWN"
        )

    def test_degraded_heartbeat_uses_wall_intervals_not_logical_or_tail(self) -> None:
        samples = self.fixture((0, 5, 15, 20))
        for sample in samples[:2]:
            sample["components"]["jarvis-daemon"]["heartbeat"] = "degraded"
        samples[-1]["components"]["jarvis-daemon"]["heartbeat"] = "degraded"
        heartbeat = load_analyzer().analyze(samples)["components"]["jarvis-daemon"]["heartbeat"]
        self.assertEqual(heartbeat["degraded_wall_seconds"], 15)
        self.assertEqual(heartbeat["degraded_sample_count"], 3)
        self.assertEqual(heartbeat["status"], "ISSUE")
        samples[1]["components"]["jarvis-daemon"]["heartbeat"] = "unknown"
        heartbeat = load_analyzer().analyze(samples)["components"]["jarvis-daemon"]["heartbeat"]
        self.assertEqual(heartbeat["unknown_wall_seconds"], 10)

    def test_model_totals_failure_rate_and_unknown_are_separate(self) -> None:
        samples = self.fixture()
        for sample in samples:
            sample["counters"].update(model_success=6, model_failed=2, model_unknown=1)
        model = load_analyzer().analyze(samples)["model_calls"]
        self.assertEqual((model["success"], model["failed"], model["unknown"]), (6, 2, 1))
        self.assertEqual(model["total"], 9)
        self.assertAlmostEqual(model["failure_rate"], 0.25)
        self.assertEqual(model["status"], "ISSUE")
        for sample in samples:
            sample["counters"].update(model_failed=0, model_unknown=1)
        self.assertEqual(load_analyzer().analyze(samples)["model_calls"]["status"], "UNKNOWN")
        samples[-1]["counters"]["model_success"] = None
        model = load_analyzer().analyze(samples)["model_calls"]
        self.assertIsNone(model["failure_rate"])
        samples = self.fixture()
        for sample in samples:
            sample["counters"].update(model_success=0, model_failed=0)
        self.assertEqual(load_analyzer().analyze(samples)["model_calls"]["status"], "UNKNOWN")
        self.assertIsNone(load_analyzer().analyze(samples)["model_calls"]["failure_rate"])

    def test_missing_metrics_pid_reuse_counter_reset_and_bad_time_are_unknown(self) -> None:
        for key in ("rss_bytes", "heartbeat", "state", "pid"):
            with self.subTest(key=key):
                samples = self.fixture()
                samples[1]["components"]["web"].pop(key)
                self.assertEqual(
                    load_analyzer().analyze(samples)["components"]["web"]["status"], "UNKNOWN"
                )
        samples = self.fixture((0, 5, 10, 15, 20, 25))
        for sample in samples[2:4]:
            sample["components"]["web"]["pid"] = 22
        segments = load_analyzer().analyze(samples)["components"]["web"]["memory"]["segments"]
        self.assertEqual(len(segments), 3)
        self.assertTrue(all(segment["slope_mb_per_hour"] is None for segment in segments))
        samples = self.fixture()
        samples[-1]["counters"]["model_success"] = 1
        self.assertEqual(load_analyzer().analyze(samples)["model_calls"]["status"], "UNKNOWN")
        for times in ((0, 0, 5), (0, 10, 5)):
            result = load_analyzer().analyze(self.fixture(times))
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertTrue(result["observation_errors"])
        samples = self.fixture()
        samples[1]["elapsed_seconds"] = float("nan")
        self.assertEqual(load_analyzer().analyze(samples)["status"], "UNKNOWN")
        samples = self.fixture()
        samples[1]["observation_errors"] = ["db unavailable"]
        self.assertEqual(load_analyzer().analyze(samples)["status"], "UNKNOWN")

    def test_missing_heartbeat_duration_and_restart_memory_are_unknown(self) -> None:
        samples = self.fixture()
        for sample in samples:
            sample["components"]["web"]["heartbeat"] = None
        heartbeat = load_analyzer().analyze(samples)["components"]["web"]["heartbeat"]
        self.assertIsNone(heartbeat["degraded_wall_seconds"])
        self.assertEqual(heartbeat["unknown_wall_seconds"], 10.0)
        samples = self.fixture()
        for sample in samples:
            sample["components"]["web"].pop("restart_count")
        memory = load_analyzer().analyze(samples)["components"]["web"]["memory"]
        self.assertEqual(memory["status"], "UNKNOWN")
        samples = self.fixture()
        samples[-1]["counters"].update(
            enqueued=8,
            delivered=6,
            upstream_pending=2,
            mind_enqueued=4,
            consumed=1,
            suspended=1,
            pending=2,
        )
        self.assertEqual(load_analyzer().analyze(samples)["queues"]["inbox"]["status"], "UNKNOWN")
        self.assertEqual(load_analyzer().analyze(samples)["queues"]["mind"]["status"], "UNKNOWN")

    def test_resources_databases_and_final_counters_retain_missing_as_null(self) -> None:
        samples = self.fixture()
        samples[-1]["components"]["web"]["open_files"] = None
        samples[-1]["database_bytes"]["daemon.db"] = 140
        result = load_analyzer().analyze(samples)
        self.assertIsNone(result["components"]["web"]["resources"]["open_files_last"])
        self.assertEqual(result["database_bytes"]["daemon.db"]["growth_bytes"], 40)
        self.assertEqual(result["counters"]["thoughts"], 2)
        samples[-1]["database_bytes"]["daemon.db"] = None
        self.assertIsNone(
            load_analyzer().analyze(samples)["database_bytes"]["daemon.db"]["growth_bytes"]
        )

    def test_cross_stage_missing_delivery_is_not_silently_accepted(self) -> None:
        samples = self.fixture()
        for sample in samples:
            sample["counters"]["handoff_unaccounted"] = 1
        self.assertEqual(load_analyzer().analyze(samples)["queues"]["handoff"]["status"], "ISSUE")

    def test_slow_memory_growth_with_allocator_plateaus_is_detected(self) -> None:
        samples = self.fixture((0, 3600, 7200, 10800, 14400, 18000, 21600))
        for sample, megabytes in zip(samples, (100, 110, 110, 108, 120, 130, 130), strict=True):
            sample["components"]["web"]["rss_bytes"] = megabytes * 1_000_000
        memory = load_analyzer().analyze(samples)["components"]["web"]["memory"]
        self.assertTrue(memory["segments"][0]["continuous_growth"])
        self.assertGreater(memory["segments"][0]["slope_mb_per_hour"], 0)

    def test_suspended_is_gauge_and_unknown_can_resolve(self) -> None:
        samples = self.fixture()
        for sample, suspended in zip(samples, (1, 0, 0), strict=True):
            sample["counters"].update(consumed=3 - suspended, suspended=suspended)
        samples[0]["counters"].update(model_unknown=1, model_success=1)
        self.assertEqual(load_analyzer().analyze(samples)["model_calls"]["status"], "OBSERVED_OK")

    def test_writer_and_cli_are_data_only_and_do_not_overwrite(self) -> None:
        scratch = Path(os.environ.get("TMPDIR", "/Volumes/扩展硬盘/hermes/.hermes/cache/scratch"))
        with tempfile.TemporaryDirectory(prefix="soak-fixture-", dir=scratch) as tmp:
            root = Path(tmp)
            source = root / "samples.jsonl"
            source.write_text(
                "\n".join(json.dumps(row) for row in self.fixture()) + "\n", encoding="utf-8"
            )
            output = root / "summary"
            expected = load_analyzer().write_summary(source, output)
            actual = cast(
                dict[str, Any], json.loads((output / "SUMMARY.json").read_text(encoding="utf-8"))
            )
            self.assertEqual(actual, expected)
            markdown = (output / "SUMMARY.md").read_text(encoding="utf-8")
            for heading in ("Memory", "Heartbeat", "Queues", "Model calls", "Restarts"):
                self.assertIn(heading, markdown)
            self.assertNotIn("24h PASS", markdown)
            self.assertIn("observed", markdown.lower())
            with self.assertRaises(FileExistsError):
                load_analyzer().write_summary(source, output)
            cli_output = root / "cli"
            process = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(Path(__file__).with_name("analyze_soak.py")),
                    str(source),
                    "--output-dir",
                    str(cli_output),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertTrue((cli_output / "SUMMARY.md").is_file())
            source.write_text('{"elapsed_seconds":0}\nnot json\n[]\n', encoding="utf-8")
            malformed = load_analyzer().write_summary(source, root / "malformed")
            self.assertEqual(malformed["status"], "UNKNOWN")
            self.assertEqual(len(malformed["observation_errors"]), 2)


class RunnerTests(unittest.TestCase):
    @staticmethod
    def runner() -> Any:
        spec = importlib.util.spec_from_file_location(
            "local_soak_runner", Path(__file__).with_name("run_soak.py")
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_default_real_cap_reads_product_daily_budget_at_parse_time(self) -> None:
        # main() must read the importable product constant, not copy its value.
        # Patch launch to keep this a parser-only check: no config/key is opened.
        with mock.patch.dict(sys.modules):
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            try:
                import daemon_wiring

                for budget in (daemon_wiring.MODEL_CALLS_PER_DAY, 7319):
                    runner = self.runner()
                    with (
                        mock.patch.object(daemon_wiring, "MODEL_CALLS_PER_DAY", budget),
                        mock.patch.object(runner, "freeze_and_launch", return_value=0) as launch,
                        mock.patch.object(
                            sys,
                            "argv",
                            [
                                "run_soak.py",
                                "--mode",
                                "real",
                                "--config",
                                "/nonexistent/never-open-config.json",
                                "--key-file",
                                "/nonexistent/never-open-key",
                                "--evidence-dir",
                                "/nonexistent/never-create-evidence",
                            ],
                        ),
                    ):
                        self.assertEqual(runner.main(), 0)
                        self.assertEqual(launch.call_args.args[0].max_model_calls, budget)
            finally:
                sys.path.pop(0)

    def test_explicit_real_cap_overrides_daily_default(self) -> None:
        runner = self.runner()
        with (
            mock.patch.object(runner, "freeze_and_launch", return_value=0) as launch,
            mock.patch.object(
                sys,
                "argv",
                [
                    "run_soak.py",
                    "--mode",
                    "real",
                    "--config",
                    "/nonexistent/never-open-config.json",
                    "--key-file",
                    "/nonexistent/never-open-key",
                    "--evidence-dir",
                    "/nonexistent/never-create-evidence",
                    "--max-model-calls",
                    "7",
                ],
            ),
        ):
            self.assertEqual(runner.main(), 0)
            self.assertEqual(launch.call_args.args[0].max_model_calls, 7)

    def test_persistent_total_cap_and_same_unknown_identity_never_redispatched(self) -> None:
        scratch = Path(os.environ["TMPDIR"])
        with tempfile.TemporaryDirectory(prefix="soak-cap-", dir=scratch) as name:
            root = Path(name)
            calls: list[str] = []

            def good(prompt: str) -> dict[str, Any]:
                calls.append(prompt)
                return {"text": "synthetic"}

            self.runner().budgeted_call(root, 1, good, "one")
            with self.assertRaisesRegex(RuntimeError, "SoakTotalCallLimit"):
                self.runner().budgeted_call(root, 1, good, "two")
            self.assertEqual(calls, ["one"])
            second = root / "unknown"
            second.mkdir()

            def ambiguous(prompt: str) -> dict[str, Any]:
                raise RuntimeError("ModelOutcomeUnknown")

            with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
                self.runner().budgeted_call(
                    second, 10, ambiguous, "one", call_identity="unknown-one"
                )
            with self.assertRaisesRegex(RuntimeError, "ModelOutcomeUnknown"):
                self.runner().budgeted_call(second, 10, good, "one", call_identity="unknown-one")
            self.assertEqual(calls, ["one"])
            self.runner().budgeted_call(second, 10, good, "fresh", call_identity="fresh-thought")
            self.assertEqual(calls, ["one", "fresh"])

    def test_fake_integration_snapshot_recovery_and_cleanup(self) -> None:
        if os.environ.get("JARVIS_SOAK_INTEGRATION") != "1":
            self.skipTest("set JARVIS_SOAK_INTEGRATION=1 for the 180s/10x process check")
        runner = Path(__file__).with_name("run_soak.py")
        self.assertTrue(runner.is_file(), "run_soak.py has not been implemented")
        scratch = Path(os.environ["TMPDIR"])
        with tempfile.TemporaryDirectory(prefix="soak-integration-", dir=scratch) as temporary:
            output = Path(temporary) / "evidence"
            process = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(runner),
                    "--mode",
                    "fake",
                    "--duration-seconds",
                    "180",
                    "--speed",
                    "10",
                    "--inject-faults",
                    "--evidence-dir",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            result = cast(dict[str, Any], json.loads((output / "RESULTS.json").read_text()))
            self.assertTrue(result["ok"], result)
            self.assertTrue(all(result["checks"].values()), result["checks"])
            self.assertGreaterEqual(result["sample_count"], 30)
            self.assertGreaterEqual(result["observed_wall_seconds"], 180)
            self.assertEqual(result["speed"], 10)
            self.assertFalse(Path(result["snapshot_path"]).exists())
            self.assertFalse(Path(result["data_parent"]).exists())
            self.assertTrue((output / "analysis" / "SUMMARY.json").is_file())
            self.assertTrue((output / "snapshot-sha256.json").is_file())
            self.assertEqual(result["real_model_calls"], 0)
            summary = cast(
                dict[str, Any], json.loads((output / "analysis" / "SUMMARY.json").read_text())
            )
            self.assertGreater(summary["counters"]["enqueued"], 50)
            self.assertEqual(summary["duplicates"]["processing_max"], 0)
            self.assertEqual(summary["duplicates"]["consumption_max"], 0)
            self.assertEqual(summary["queues"]["inbox"]["unreconciled"], 0)
            self.assertEqual(summary["queues"]["mind"]["unreconciled"], 0)
            self.assertEqual(summary["queues"]["handoff"]["unaccounted_max"], 0)


if __name__ == "__main__":
    unittest.main()
