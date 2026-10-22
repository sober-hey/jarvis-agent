# pyright: strict
"""Wall-time RSS classification fixtures; no models or product processes."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from typing import Any

if __package__:
    from .test_soak import load_analyzer
else:
    from test_soak import load_analyzer


def samples_for(
    rss_mb: Callable[[float], float], *, duration: int = 2400, step: int = 5
) -> list[dict[str, Any]]:
    return [
        {
            "elapsed_seconds": time,
            "logical_elapsed_seconds": time * 10,
            "components": {
                "web": {"pid": 10, "restart_count": 0, "rss_bytes": rss_mb(time) * 1_000_000}
            },
        }
        for time in range(0, duration + 1, step)
    ]


def memory_for(samples: list[dict[str, Any]]) -> dict[str, Any]:
    return load_analyzer().analyze(samples)["components"]["web"]["memory"]


class MemoryTests(unittest.TestCase):
    def test_sustained_linear_growth_uses_wall_time_windows(self) -> None:
        samples = samples_for(lambda time: 100 + time / 60)
        memory = memory_for(samples)
        self.assertEqual(memory.get("classification"), "sustained_linear_growth")
        self.assertEqual(memory["status"], "ISSUE")
        segment = memory["segments"][0]
        self.assertEqual(segment.get("classification"), "sustained_linear_growth")
        self.assertAlmostEqual(segment["slope_mb_per_hour"], 60)
        self.assertAlmostEqual(segment["post_warmup_slope_mb_per_hour"], 60)
        self.assertEqual(segment["post_warmup_start_wall_seconds"], 300)
        self.assertTrue(segment["qualification"]["qualified"])
        windows = segment["windows"]
        self.assertEqual(len(windows), 8)
        self.assertEqual(windows[0]["start_wall_seconds"], 0)
        self.assertEqual(windows[0]["end_wall_seconds"], 300)
        self.assertAlmostEqual(windows[0]["mean_rss_bytes"], 102_458_333.33333333)
        self.assertAlmostEqual(windows[0]["max_rss_bytes"], 104_916_666.66666667)
        self.assertTrue(all(window["adequate"] for window in windows))
        self.assertFalse(segment["plateau"]["observed"])

    def test_linear_growth_with_small_relative_noise_is_not_hidden_by_absolute_cutoff(self) -> None:
        # Small jitter is 1.5MB around a 40MB trend, unlike the 20MB ambiguous
        # noise fixture. Significance is relative to fitted growth, not a fixed
        # per-window 1MB allocator cutoff used for positive plateau evidence.
        memory = memory_for(
            samples_for(
                lambda time: 100 + time / 60 + (1.5 if time % 10 else -1.5),
            )
        )
        self.assertEqual(memory["classification"], "sustained_linear_growth")
        self.assertEqual(memory["status"], "ISSUE")

    def test_warmup_rise_then_plateau_excludes_first_300_wall_seconds_per_pid(self) -> None:
        first = samples_for(lambda time: 100 + min(time, 300) / 10)
        second = samples_for(lambda time: 300 + min(time, 300) / 5)
        for sample in second:
            sample["elapsed_seconds"] += 2405
            sample["logical_elapsed_seconds"] *= 100
            sample["components"]["web"].update(pid=20, restart_count=1)
        memory = memory_for(first + second)
        self.assertEqual(memory.get("classification"), "rise_then_plateau")
        self.assertEqual(memory["status"], "OBSERVED_OK")
        self.assertEqual(len(memory["segments"]), 2)
        for segment, start in zip(memory["segments"], (300, 2705), strict=True):
            with self.subTest(pid=segment["pid"]):
                self.assertEqual(segment["classification"], "rise_then_plateau")
                self.assertEqual(segment["post_warmup_start_wall_seconds"], start)
                self.assertAlmostEqual(segment["post_warmup_slope_mb_per_hour"], 0)
                self.assertGreater(segment["slope_mb_per_hour"], 0)
                self.assertFalse(segment["continuous_growth"])
                self.assertTrue(segment["plateau"]["observed"])
                self.assertGreaterEqual(segment["plateau"]["duration_wall_seconds"], 900)
                self.assertFalse(segment["plateau"]["growth_after_plateau_observed"])

    def test_insufficient_windows_never_claim_growth_or_ok(self) -> None:
        trends: tuple[Callable[[float], float], ...] = (
            lambda time: 100 + time / 30,
            lambda time: 100.0,
        )
        for duration in (120, 240, 360, 1795):
            for trend in trends:
                with self.subTest(duration=duration, trend=trend):
                    memory = memory_for(samples_for(trend, duration=duration))
                    self.assertEqual(memory.get("classification"), "uncertain")
                    self.assertEqual(memory["status"], "UNKNOWN")
                    segment = memory["segments"][0]
                    self.assertFalse(segment["qualification"]["qualified"])
                    self.assertIn(
                        "segment_shorter_than_1800_wall_seconds",
                        segment["qualification"]["reasons"],
                    )
                    self.assertFalse(segment["plateau"]["observed"])
        # Three points in one hour retain the old diagnostic, not the old ISSUE.
        sparse = memory_for(samples_for(lambda time: 100 + time / 180, duration=3600, step=1800))
        self.assertTrue(sparse["segments"][0]["continuous_growth"])
        self.assertEqual(sparse["status"], "UNKNOWN")
        self.assertEqual(sparse["classification"], "uncertain")

    def test_sparse_gappy_noisy_declining_and_nonlinear_are_uncertain(self) -> None:
        def gappy() -> list[dict[str, Any]]:
            samples = samples_for(lambda time: 100 + time / 60)
            return [
                sample
                for sample in samples
                if sample["elapsed_seconds"] % 300 < 150 or sample["elapsed_seconds"] == 2400
            ]

        cases: dict[str, list[dict[str, Any]]] = {
            "sparse": samples_for(lambda time: 100 + time / 60, step=30),
            "gappy": gappy(),
            "noisy": samples_for(lambda time: 100 + time / 60 + (20 if time % 10 else 0)),
            "decline": samples_for(lambda time: 200 - time / 60),
            "decline_then_flat": samples_for(lambda time: 200 - min(time, 900) / 60),
            "nonlinear": samples_for(lambda time: 100 + (time / 300) ** 2),
            "late_acceleration": samples_for(lambda time: 100 + max(time - 1800, 0) / 20),
            "noisy_then_flat": samples_for(
                lambda time: 150 + (20 if time < 1200 and time % 10 else 0)
            ),
        }
        for name, samples in cases.items():
            with self.subTest(pattern=name):
                memory = memory_for(samples)
                self.assertEqual(memory.get("classification"), "uncertain")
                self.assertEqual(memory["status"], "UNKNOWN")
        for name in ("sparse", "gappy"):
            self.assertFalse(memory_for(cases[name])["segments"][0]["qualification"]["qualified"])

    def test_final_partial_bin_does_not_veto_adequate_complete_windows(self) -> None:
        trends: tuple[tuple[str, Callable[[float], float]], ...] = (
            ("linear", lambda time: 100 + time / 60),
            ("plateau", lambda time: 100 + min(time, 900) / 60),
        )
        for duration in (1800, 1805, 2405):
            for name, trend in trends:
                with self.subTest(duration=duration, pattern=name):
                    memory = memory_for(samples_for(trend, duration=duration))
                    self.assertEqual(
                        memory["classification"],
                        "sustained_linear_growth" if name == "linear" else "rise_then_plateau",
                    )
                    if duration % 300:
                        window = memory["segments"][0]["windows"][-1]
                        self.assertFalse(window["complete"])
                        self.assertFalse(window["adequate"])
                        self.assertGreater(window["sample_count"], 0)

    def test_small_coherent_drift_is_not_a_confirmed_plateau(self) -> None:
        for direction in (-1, 1):
            memory = memory_for(
                samples_for(
                    lambda time, direction=direction: 100 + direction * time / 4800,
                )
            )
            self.assertEqual(memory["classification"], "uncertain")
            self.assertEqual(memory["status"], "UNKNOWN")
            self.assertFalse(memory["segments"][0]["plateau"]["observed"])

    def test_flat_plateau_and_low_allocator_jitter_are_qualified_not_leaks(self) -> None:
        patterns: tuple[Callable[[float], float], ...] = (
            lambda time: 100.0,
            lambda time: 100 + min(time, 900) / 60 + (0.1 if time % 10 else 0),
        )
        for pattern in patterns:
            memory = memory_for(samples_for(pattern))
            self.assertEqual(memory["classification"], "rise_then_plateau")
            self.assertEqual(memory["status"], "OBSERVED_OK")
        flat = memory_for(samples_for(lambda time: 100.0))
        self.assertFalse(flat["segments"][0]["plateau"]["rise_observed"])

    def test_plateau_followed_by_regrowth_is_uncertain_with_diagnostic(self) -> None:
        samples = samples_for(lambda time: 100 + max(time - 1800, 0) / 60)
        memory = memory_for(samples)
        self.assertEqual(memory["classification"], "uncertain")
        self.assertEqual(memory["status"], "UNKNOWN")
        self.assertTrue(memory["segments"][0]["plateau"]["growth_after_plateau_observed"])

    def test_multiple_rises_separated_by_plateau_are_ambiguous(self) -> None:
        memory = memory_for(
            samples_for(
                lambda time: 100 + min(time, 300) / 30 + min(max(time - 1500, 0), 300) / 30,
                duration=3000,
            )
        )
        self.assertEqual(memory["classification"], "uncertain")
        self.assertEqual(memory["status"], "UNKNOWN")
        self.assertTrue(memory["segments"][0]["plateau"]["growth_after_plateau_observed"])
        self.assertFalse(memory["segments"][0]["plateau"]["observed"])

    def test_pid_reuse_restart_generations_cannot_pool_short_segments(self) -> None:
        samples = samples_for(lambda time: 100 + time / 60)
        for sample in samples:
            if sample["elapsed_seconds"] >= 1200:
                sample["components"]["web"]["restart_count"] = 1
        memory = memory_for(samples)
        self.assertEqual(memory["classification"], "uncertain")
        self.assertEqual(memory["status"], "UNKNOWN")
        self.assertEqual(len(memory["segments"]), 2)
        self.assertEqual(
            [segment["post_warmup_start_wall_seconds"] for segment in memory["segments"]],
            [300, 1500],
        )
        self.assertTrue(
            all(not segment["qualification"]["qualified"] for segment in memory["segments"])
        )

    def test_logical_speed_cannot_change_memory_conclusions(self) -> None:
        samples = samples_for(lambda time: 100 + time / 60)
        expected = memory_for(samples)
        for sample in samples:
            sample["logical_elapsed_seconds"] *= 3600
        self.assertEqual(memory_for(samples), expected)

    def test_missing_rss_and_irregular_gaps_remain_explicit(self) -> None:
        samples = samples_for(lambda time: 100.0)
        samples[-1]["components"]["web"]["rss_bytes"] = None
        memory = memory_for(samples)
        self.assertEqual(memory["status"], "UNKNOWN")
        self.assertEqual(memory["classification"], "uncertain")
        samples = samples_for(lambda time: 100 + time / 60)
        samples = [sample for sample in samples if sample["elapsed_seconds"] not in (610, 615, 620)]
        memory = memory_for(samples)
        self.assertEqual(memory["classification"], "uncertain")
        qualification = memory["segments"][0]["qualification"]
        self.assertIn("sample_gap_exceeds_15_wall_seconds", qualification["reasons"])
        self.assertEqual(qualification["max_gap_wall_seconds"], 20)


if __name__ == "__main__":
    unittest.main()
