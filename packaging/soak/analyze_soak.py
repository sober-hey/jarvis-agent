# pyright: strict
"""Data-only soak analysis. JSON objects are the intentionally dynamic boundary."""

from __future__ import annotations

import argparse
import json
import math
from math import isfinite
from pathlib import Path
from typing import Any, NamedTuple, cast


def _object(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if isfinite(value) and value >= 0 else None


def _status(values: list[str]) -> str:
    if "ISSUE" in values:
        return "ISSUE"
    return "OBSERVED_OK" if values and all(v == "OBSERVED_OK" for v in values) else "UNKNOWN"


def _slope(points: list[tuple[float, float]]) -> float | None:
    if len(points) < 3:
        return None
    x_mean = sum(x for x, _ in points) / len(points)
    y_mean = sum(y for _, y in points) / len(points)
    denominator = sum((x - x_mean) ** 2 for x, _ in points)
    if denominator <= 0:
        return None
    return sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator


_MEMORY_WARMUP_SECONDS = 300.0
_MEMORY_WINDOW_SECONDS = 300.0
_MEMORY_MIN_SECONDS = 1800.0
_MEMORY_MIN_WINDOW_SAMPLES = 30
_MEMORY_MAX_GAP_SECONDS = 15.0
_MEMORY_PLATEAU_SECONDS = 900.0


class _MemoryFit(NamedTuple):
    slope: float | None
    r_squared: float | None
    residual_rmse_bytes: float | None
    max_residual_bytes: float | None


def _memory_fit(points: list[tuple[float, float]]) -> _MemoryFit:
    slope = _slope(points)
    if slope is None:
        return _MemoryFit(None, None, None, None)
    x_mean = sum(t for t, _ in points) / len(points)
    y_mean = sum(rss for _, rss in points) / len(points)
    residuals = [rss - (y_mean + slope * (t - x_mean)) for t, rss in points]
    squared_error = sum(value * value for value in residuals)
    variance = sum((rss - y_mean) ** 2 for _, rss in points)
    return _MemoryFit(
        slope,
        max(0.0, 1 - squared_error / variance) if variance else 1.0,
        math.sqrt(squared_error / len(points)),
        max(abs(value) for value in residuals),
    )


def _memory_shape(points: list[tuple[float, float]]) -> dict[str, Any]:
    """Conservative shape evidence, never a causal memory-leak diagnosis.

    Windows are half-open [start, end), anchored to each observed PID segment.
    The terminal sample closes coverage and participates in OLS/tail assessment,
    but belongs to the next bin, not both bins. No interpolation or logical time.
    """
    origin, end = points[0][0], points[-1][0]
    duration = end - origin
    grouped: dict[int, list[tuple[float, float]]] = {}
    for time, rss in points:
        index = math.floor((time - origin) / _MEMORY_WINDOW_SECONDS)
        grouped.setdefault(index, []).append((time, rss))
    windows: list[dict[str, Any]] = []
    smooth_windows = True
    for index in range(math.ceil(duration / _MEMORY_WINDOW_SECONDS)):
        start = origin + index * _MEMORY_WINDOW_SECONDS
        stop = start + _MEMORY_WINDOW_SECONDS
        window = grouped.get(index, [])
        values = [rss for _, rss in window]
        # Include both unobserved edges: clustered samples are not full coverage.
        times = [start, *(t for t, _ in window), min(stop, end)]
        gap = max((b - a for a, b in zip(times, times[1:], strict=False)), default=0.0)
        fit = _memory_fit(window)
        tolerance = max(1_000_000.0, sum(values) / len(values) * 0.01) if values else 0.0
        if start >= origin + _MEMORY_WARMUP_SECONDS and (stop <= end or len(window) >= 3):
            smooth_windows = smooth_windows and (
                fit.max_residual_bytes is not None and fit.max_residual_bytes <= tolerance
            )
        windows.append(
            {
                "start_wall_seconds": start,
                "end_wall_seconds": stop,
                "observed_end_wall_seconds": min(stop, end),
                "complete": stop <= end,
                "sample_count": len(window),
                "mean_rss_bytes": sum(values) / len(values) if values else None,
                "max_rss_bytes": max(values) if values else None,
                "min_rss_bytes": min(values) if values else None,
                "max_gap_wall_seconds": gap,
                "adequate": stop <= end
                and len(window) >= _MEMORY_MIN_WINDOW_SAMPLES
                and gap <= _MEMORY_MAX_GAP_SECONDS,
            }
        )
    post = [(t, rss) for t, rss in points if t >= origin + _MEMORY_WARMUP_SECONDS]
    fit = _memory_fit(post)
    post_duration = post[-1][0] - post[0][0] if post else 0.0
    complete_windows = [w for w in windows if w["complete"]]
    post_windows = [
        w for w in complete_windows if w["start_wall_seconds"] >= origin + _MEMORY_WARMUP_SECONDS
    ]
    max_gap = max((b[0] - a[0] for a, b in zip(points, points[1:], strict=False)), default=0.0)
    reasons: list[str] = []
    if duration < _MEMORY_MIN_SECONDS:
        reasons.append("segment_shorter_than_1800_wall_seconds")
    if len(complete_windows) < 6 or len(post_windows) < 5:
        reasons.append("fewer_than_six_complete_300_second_windows")
    if any(not w["adequate"] for w in complete_windows):
        reasons.append("inadequate_samples_in_complete_window")
    if max_gap > _MEMORY_MAX_GAP_SECONDS:
        reasons.append("sample_gap_exceeds_15_wall_seconds")
    qualified = not reasons
    means = [float(w["mean_rss_bytes"]) for w in post_windows if w["mean_rss_bytes"] is not None]
    maxima = [float(w["max_rss_bytes"]) for w in post_windows if w["max_rss_bytes"] is not None]
    tail_start = max(origin + _MEMORY_WARMUP_SECONDS, end - _MEMORY_PLATEAU_SECONDS)
    tail = [(t, rss) for t, rss in post if t >= tail_start]
    tail_fit = _memory_fit(tail)
    tail_values = [rss for _, rss in tail]
    tolerance = max(1_000_000.0, sum(tail_values) / len(tail_values) * 0.01) if tail else 0.0
    tail_flat = (
        qualified
        and end - tail_start >= _MEMORY_PLATEAU_SECONDS
        and tail_fit.slope is not None
        and abs(tail_fit.slope * (end - tail_start)) <= tolerance
        and tail_fit.residual_rmse_bytes is not None
        and abs(tail_fit.slope * (end - tail_start)) <= max(1.0, 4 * tail_fit.residual_rmse_bytes)
        and max(tail_values) - min(tail_values) <= tolerance
    )
    # A declining or previously noisy trace is not evidence of a rise/plateau.
    no_decline = all(
        max(series[: index + 1]) - value <= tolerance
        for series in (means, maxima)
        for index, value in enumerate(series)
    ) and points[0][1] <= (min(tail_values) + tolerance if tail else 0.0)
    regrowth = False
    if qualified:
        for index in range(max(0, len(post_windows) - 3)):
            start = float(post_windows[index]["start_wall_seconds"])
            stop = float(post_windows[index + 2]["end_wall_seconds"])
            prior = [(t, rss) for t, rss in post if start <= t < stop]
            prior_fit = _memory_fit(prior)
            values = [rss for _, rss in prior]
            if (
                values
                and prior_fit.slope is not None
                and prior_fit.residual_rmse_bytes is not None
                and max(values) - min(values) <= tolerance
                and abs(prior_fit.slope * (stop - start))
                <= max(1.0, 4 * prior_fit.residual_rmse_bytes)
                and any(rss > max(values) + tolerance for t, rss in post if t >= stop)
            ):
                regrowth = True
                break
    plateau = tail_flat and no_decline and smooth_windows and not regrowth
    rise = fit.slope * post_duration if fit.slope is not None else None
    minimum_rise = max(2_000_000.0, post[0][1] * 0.02) if post else 2_000_000.0
    expected_step = fit.slope * _MEMORY_WINDOW_SECONDS if fit.slope is not None else 0.0
    consistent_steps = expected_step > 0 and all(
        0.5 * expected_step <= b - a <= 1.5 * expected_step
        for series in (means, maxima)
        for a, b in zip(series, series[1:], strict=False)
    )
    linear = (
        qualified
        and not regrowth
        and rise is not None
        and rise >= minimum_rise
        and fit.r_squared is not None
        and fit.r_squared >= 0.95
        and fit.residual_rmse_bytes is not None
        and fit.residual_rmse_bytes <= rise * 0.1
        and fit.max_residual_bytes is not None
        and fit.max_residual_bytes <= rise * 0.25
        and consistent_steps
        and tail_fit.slope is not None
        and fit.slope is not None
        and 0.5 * fit.slope <= tail_fit.slope <= 1.5 * fit.slope
    )
    classification = (
        "sustained_linear_growth" if linear else "rise_then_plateau" if plateau else "uncertain"
    )
    return {
        "classification": classification,
        "status": "ISSUE" if linear else "OBSERVED_OK" if plateau else "UNKNOWN",
        "warmup_wall_seconds": _MEMORY_WARMUP_SECONDS,
        "window_wall_seconds": _MEMORY_WINDOW_SECONDS,
        "post_warmup_start_wall_seconds": post[0][0] if post else None,
        "post_warmup_sample_count": len(post),
        "post_warmup_slope_mb_per_hour": fit.slope * 3600 / 1_000_000
        if fit.slope is not None
        else None,
        "post_warmup_r_squared": fit.r_squared,
        "post_warmup_residual_rmse_bytes": fit.residual_rmse_bytes,
        "post_warmup_max_residual_bytes": fit.max_residual_bytes,
        "post_warmup_fitted_growth_bytes": rise,
        "minimum_meaningful_growth_bytes": minimum_rise,
        "consistent_window_growth": consistent_steps,
        "shape_checks": {
            "post_warmup_windows_smooth": smooth_windows,
            "no_material_decline": no_decline,
            "sustained_linear_growth": linear,
            "terminal_plateau": plateau,
        },
        "windows": windows,
        "qualification": {
            "qualified": qualified,
            "reasons": reasons,
            "minimum_segment_wall_seconds": _MEMORY_MIN_SECONDS,
            "minimum_samples_per_complete_window": _MEMORY_MIN_WINDOW_SAMPLES,
            "maximum_allowed_gap_wall_seconds": _MEMORY_MAX_GAP_SECONDS,
            "max_gap_wall_seconds": max_gap,
            "complete_window_count": len(complete_windows),
            "complete_post_warmup_window_count": len(post_windows),
        },
        "plateau": {
            "observed": plateau,
            "tail_flat": tail_flat,
            "start_wall_seconds": tail_start if tail else None,
            "duration_wall_seconds": end - tail_start if tail else 0.0,
            "slope_mb_per_hour": tail_fit.slope * 3600 / 1_000_000
            if tail_fit.slope is not None
            else None,
            "tolerance_bytes": tolerance,
            "rise_observed": plateau
            and bool(tail_values)
            and sum(tail_values) / len(tail_values) - points[0][1] > tolerance,
            "growth_after_plateau_observed": regrowth if qualified else None,
            "note": "Terminal 900-wall-second assessment; stable-from-start is included "
            "with rise_observed=false. No prediction of future stability.",
        },
    }


def _memory(rows: list[tuple[float, dict[str, Any]]]) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    points: list[tuple[float, float]] = []
    identity: tuple[float | None, float | None] | None = None
    missing = False

    def finish() -> None:
        if not points:
            return
        slope = _slope(points)
        # Preserve the historical three equal-count windows as a diagnostic only.
        # It includes warmup and is not the new wall-window classification/status.
        windows = [points[i * len(points) // 3 : (i + 1) * len(points) // 3] for i in range(3)]
        means = [sum(y for _, y in window) / len(window) for window in windows if window]
        growth = (
            slope > 0 and len(means) == 3 and means[0] < means[1] < means[2]
            if slope is not None
            else None
        )
        segments.append(
            {
                "pid": identity[0] if identity else None,
                "restart_count": identity[1] if identity else None,
                "sample_count": len(points),
                "start_wall_seconds": points[0][0],
                "end_wall_seconds": points[-1][0],
                "slope_mb_per_hour": slope * 3600 / 1_000_000 if slope is not None else None,
                "continuous_growth": growth,
                **_memory_shape(points),
            }
        )
        points.clear()

    for time, component in rows:
        pid = _number(component.get("pid"))
        restart = _number(component.get("restart_count"))
        rss = _number(component.get("rss_bytes"))
        next_identity = (pid, restart)
        if next_identity != identity or pid is None or restart is None or rss is None:
            finish()
        identity = next_identity
        if pid is None or restart is None or rss is None:
            missing = True
        else:
            points.append((time, rss))
    finish()
    statuses = [str(segment["status"]) for segment in segments]
    if missing:
        statuses.append("UNKNOWN")
    return {
        "status": _status(statuses),
        "classification": "sustained_linear_growth"
        if any(s["classification"] == "sustained_linear_growth" for s in segments)
        else "rise_then_plateau"
        if _status(statuses) == "OBSERVED_OK"
        else "uncertain",
        "segments": segments,
        "method": "Contiguous PID/restart segments; fixed half-open 300-wall-second windows "
        "anchored to segment start; mean/max of observed samples, no interpolation. "
        "Full OLS retained as diagnostic; classification drops the first 300 wall seconds "
        "of each segment. Slopes in decimal MB/wall-hour; logical time ignored.",
        "growth_rule": (
            "Qualified >=1800-wall-second segment, >=6 complete windows (>=5 after warmup), "
            ">=30 samples/window, all gaps <=15s. Sustained linear growth additionally requires "
            "fitted rise >=max(2MB,2% initial post-warmup RSS), R^2>=0.95, RMSE<=10% of rise, "
            "max residual<=25% of rise; mean/max window steps and terminal slope within "
            "50-150% of overall fitted growth. Growth noise is judged relative to fitted rise; "
            "the absolute window-flatness cutoff applies only to positive plateau evidence. "
            "ISSUE means observed sustained linear growth, not proof of a leak. "
            "continuous_growth is the legacy positive full OLS plus three rising equal-count "
            "window means diagnostic only; it never determines status."
        ),
        "plateau_rule": "Qualified segment with flat terminal 900 wall seconds: raw RSS range "
        "and fitted drift <=max(1MB,1% mean RSS), fitted drift <=max(1 byte,4*residual RMSE), "
        "no material prior decline and low post-warmup within-window noise. Flat-from-start "
        "is included with rise_observed=false. Other patterns are uncertain/UNKNOWN; "
        "OBSERVED_OK does not exclude slower growth, future growth or a leak.",
    }


def _counts(samples: list[dict[str, Any]], key: str) -> list[int | None]:
    result: list[int | None] = []
    for sample in samples:
        value = _number(_object(sample.get("counters")).get(key))
        result.append(int(value) if value is not None and value.is_integer() else None)
    return result


def _complete(values: list[int | None], *, cumulative: bool = False) -> bool:
    if not values or any(v is None for v in values):
        return False
    numbers = [v for v in values if v is not None]
    return not cumulative or all(b >= a for a, b in zip(numbers, numbers[1:], strict=False))


def _restarts(rows: list[tuple[float, dict[str, Any]]]) -> dict[str, Any]:
    counts = [_number(row.get("restart_count")) for _, row in rows]
    known = [v for v in counts if v is not None]
    states = [row.get("state") for _, row in rows]
    fused = any(state in ("circuit_open", "fused", "fuse_open") for state in states)
    complete = (
        bool(rows)
        and len(known) == len(rows)
        and all(isinstance(state, str) and bool(state) for state in states)
    )
    monotonic = all(b >= a for a, b in zip(known, known[1:], strict=False))
    maximum = max(known) if known else None
    repeated = maximum >= 3 if maximum is not None else None
    return {
        "status": "ISSUE"
        if fused or repeated
        else "OBSERVED_OK"
        if complete and monotonic
        else "UNKNOWN",
        "max_restart_count": maximum,
        "restarts_during_window": known[-1] - known[0] if complete and monotonic else None,
        "repeated_threshold": 3,
        "repeated_observed": repeated,
        "fused_observed": fused if complete or fused else None,
        "threshold_reason": (
            "Three restarts distinguish repetition from the one expected injected SIGKILL; "
            "no causal attribution inferred."
        ),
    }


def _heartbeat(rows: list[tuple[float, dict[str, Any]]]) -> dict[str, Any]:
    states = [str(row.get("heartbeat", "unknown")) for _, row in rows]
    durations: dict[str, float] = {
        key: 0.0 for key in ("healthy", "degraded", "unhealthy", "unknown")
    }
    for index, ((start, _), (end, _)) in enumerate(zip(rows, rows[1:], strict=False)):
        state = states[index] if states[index] in durations else "unknown"
        durations[state] += max(0.0, end - start)
    bad = any(state in ("degraded", "unhealthy") for state in states)
    complete = len(rows) >= 2 and all(state == "healthy" for state in states)
    duration_known = len(rows) >= 2 and all(
        state in ("healthy", "degraded", "unhealthy") for state in states
    )
    return {
        "status": "ISSUE" if bad else "OBSERVED_OK" if complete else "UNKNOWN",
        "degraded_wall_seconds": durations["degraded"] if duration_known else None,
        "degraded_known_wall_seconds": durations["degraded"] if len(rows) >= 2 else None,
        "unhealthy_wall_seconds": durations["unhealthy"] if duration_known else None,
        "unknown_wall_seconds": durations["unknown"] if len(rows) >= 2 else None,
        "degraded_sample_count": states.count("degraded"),
        "method": (
            "left-sample state over observed wall intervals; no extrapolation after final sample"
        ),
    }


def _reconcile(
    samples: list[dict[str, Any]], keys: tuple[str, ...], expression: str
) -> dict[str, Any]:
    columns = [_counts(samples, key) for key in keys]
    residuals: list[int | None] = []
    for row in zip(*columns, strict=False):
        if any(value is None for value in row):
            residuals.append(None)
        else:
            numbers = [value for value in row if value is not None]
            residuals.append(numbers[0] - sum(numbers[1:]))
    mismatches = sum(v is not None and v != 0 for v in residuals)
    complete = bool(samples) and all(
        _complete(column, cumulative=index < min(2, len(columns) - 1))
        for index, column in enumerate(columns)
    )
    return {
        "status": "ISSUE" if mismatches else "OBSERVED_OK" if complete else "UNKNOWN",
        "equation": expression,
        "unreconciled": residuals[-1] if residuals else None,
        "mismatch_sample_count": mismatches,
        "possible_lost_max": max((v for v in residuals if v is not None and v > 0), default=None)
        if mismatches
        else 0
        if complete
        else None,
        "note": (
            "positive residual = unaccounted; negative = over-accounted; "
            "counts alone cannot prove loss or ID-level disjointness"
        ),
    }


def _backlog(timed: list[tuple[float, dict[str, Any]]], key: str) -> dict[str, Any]:
    values = _counts([sample for _, sample in timed], key)
    points = [
        (time, float(value))
        for (time, _), value in zip(timed, values, strict=False)
        if value is not None
    ]
    slope = _slope(points) if _complete(values) else None
    increasing = slope is not None and slope > 0 and points[-1][1] > points[0][1]
    return {
        "status": "ISSUE" if increasing else "OBSERVED_OK" if slope is not None else "UNKNOWN",
        "initial": values[0] if values else None,
        "final": values[-1] if values else None,
        "slope_items_per_hour": slope * 3600 if slope is not None else None,
        "growth_observed": increasing if slope is not None else None,
        "method": (
            "OLS >=3 complete wall-time points; positive slope and positive end-to-start "
            "change; nonzero pending alone is not growth"
        ),
    }


def _duplicates(samples: list[dict[str, Any]]) -> dict[str, Any]:
    processing = _counts(samples, "duplicate_processing")
    consumption = _counts(samples, "duplicate_consumption")
    p_max = max((v for v in processing if v is not None), default=None)
    c_max = max((v for v in consumption if v is not None), default=None)
    bad = (p_max is not None and p_max > 0) or (c_max is not None and c_max > 0)
    return {
        "status": "ISSUE"
        if bad
        else "OBSERVED_OK"
        if _complete(processing, cumulative=True) and _complete(consumption, cumulative=True)
        else "UNKNOWN",
        "processing_max": p_max,
        "consumption_max": c_max,
    }


def _models(samples: list[dict[str, Any]]) -> dict[str, Any]:
    columns = [_counts(samples, key) for key in ("model_success", "model_failed", "model_unknown")]
    complete = all(_complete(column, cumulative=index < 2) for index, column in enumerate(columns))
    success, failed, unknown = [column[-1] if column else None for column in columns]
    known = success + failed if success is not None and failed is not None else None
    rate = failed / known if complete and known and failed is not None else None
    total = known + unknown if known is not None and unknown is not None else None
    bad = any(v is not None and v > 0 for v in columns[1])
    return {
        "status": "ISSUE"
        if bad
        else "OBSERVED_OK"
        if complete and known and unknown == 0
        else "UNKNOWN",
        "success": success,
        "failed": failed,
        "unknown": unknown,
        "total": total,
        "failure_rate": rate,
        "failure_rate_denominator": (
            "success + failed; unknown excluded (not assumed failed or successful)"
        ),
        "counter_scope": (
            "cumulative snapshot totals, never summed across samples; "
            "success/failed decreases mark coverage UNKNOWN; unknown is an in-flight gauge"
        ),
    }


def _resources(rows: list[tuple[float, dict[str, Any]]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    complete = bool(rows)
    for key in ("rss_bytes", "cpu_percent", "open_files"):
        values = [_number(row.get(key)) for _, row in rows]
        known = [value for value in values if value is not None]
        result[f"{key}_last"] = values[-1] if values else None
        result[f"{key}_max_observed"] = max(known) if known else None
        complete = complete and len(known) == len(values)
    result["status"] = "OBSERVED_OK" if complete else "UNKNOWN"
    return result


def _databases(samples: list[dict[str, Any]]) -> dict[str, Any]:
    names = {str(name) for sample in samples for name in _object(sample.get("database_bytes"))}
    result: dict[str, Any] = {}
    for name in sorted(names):
        values = [_number(_object(sample.get("database_bytes")).get(name)) for sample in samples]
        known = [value for value in values if value is not None]
        first = values[0] if values else None
        last = values[-1] if values else None
        result[name] = {
            "status": "OBSERVED_OK" if len(known) == len(samples) else "UNKNOWN",
            "initial_bytes": first,
            "final_bytes": last,
            "max_bytes_observed": max(known) if known else None,
            "growth_bytes": last - first if last is not None and first is not None else None,
        }
    return result


def analyze(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Return deterministic, JSON-safe conclusions limited to supplied observations."""
    timed: list[tuple[float, dict[str, Any]]] = []
    errors: list[str] = []
    for index, sample in enumerate(samples):
        time = _number(sample.get("elapsed_seconds"))
        if time is None:
            errors.append(f"sample {index}: missing/invalid elapsed_seconds")
        elif timed and time <= timed[-1][0]:
            errors.append(f"sample {index}: wall timestamps must strictly increase")
        else:
            timed.append((time, sample))
        raw_errors: object = sample.get("observation_errors")
        if isinstance(raw_errors, list):
            errors.extend(str(error) for error in cast(list[object], raw_errors))
    names = {str(name) for sample in samples for name in _object(sample.get("components"))}
    names.update(("web", "jarvis-daemon"))
    components: dict[str, Any] = {}
    for name in sorted(names):
        rows = [
            (time, _object(_object(sample.get("components")).get(name))) for time, sample in timed
        ]
        memory = _memory(rows)
        restarts = _restarts(rows)
        heartbeat = _heartbeat(rows)
        resources = _resources(rows)
        pid_complete = bool(rows) and all(_number(row.get("pid")) is not None for _, row in rows)
        components[name] = {
            "memory": memory,
            "restarts": restarts,
            "heartbeat": heartbeat,
            "resources": resources,
            "status": _status(
                [
                    str(memory["status"]),
                    str(restarts["status"]),
                    str(heartbeat["status"]),
                    str(resources["status"]),
                    "OBSERVED_OK" if pid_complete else "UNKNOWN",
                ]
            ),
        }
    inbox = _reconcile(
        samples,
        ("enqueued", "delivered", "upstream_pending"),
        "enqueued = delivered + upstream_pending",
    )
    mind = _reconcile(
        samples,
        ("mind_enqueued", "consumed", "suspended", "pending"),
        "mind_enqueued = consumed + suspended + pending",
    )
    backlog = {"inbox": _backlog(timed, "upstream_pending"), "mind": _backlog(timed, "pending")}
    handoff_values = _counts(samples, "handoff_unaccounted")
    handoff_complete = _complete(handoff_values)
    handoff_bad = any(value is not None and value > 0 for value in handoff_values)
    handoff: dict[str, Any] = {
        "status": "ISSUE" if handoff_bad else "OBSERVED_OK" if handoff_complete else "UNKNOWN",
        "unaccounted_max": max((v for v in handoff_values if v is not None), default=None),
        "note": "Delivered inbox IDs absent from mind daemon_event_id set; IDs not exported. "
        "Independent read-only DB snapshots can transiently disagree; inspect persistence.",
    }
    queues: dict[str, Any] = {
        "inbox": inbox,
        "mind": mind,
        "handoff": handoff,
        "backlog": backlog,
        "note": (
            "Inbox delivery may aggregate/backfill many inbox IDs into one mind event; "
            "never equate enqueued with mind_enqueued."
        ),
    }
    queues["status"] = _status(
        [
            str(inbox["status"]),
            str(mind["status"]),
            str(handoff["status"]),
            str(backlog["inbox"]["status"]),
            str(backlog["mind"]["status"]),
        ]
    )
    duplicates = _duplicates(samples)
    models = _models(samples)
    statuses = [str(component["status"]) for component in components.values()] + [
        str(queues["status"]),
        str(duplicates["status"]),
        str(models["status"]),
    ]
    if errors or len(timed) < 2:
        statuses.append("UNKNOWN")
    logical = [_number(sample.get("logical_elapsed_seconds")) for sample in samples]
    logical_span = (
        logical[-1] - logical[0]
        if len(logical) >= 2 and logical[-1] is not None and logical[0] is not None
        else None
    )
    return {
        "schema_version": 1,
        "status": _status(statuses),
        "sample_count": len(samples),
        "observed_wall_seconds": timed[-1][0] - timed[0][0] if len(timed) >= 2 else None,
        "observed_logical_seconds": logical_span,
        "components": components,
        "queues": queues,
        "duplicates": duplicates,
        "model_calls": models,
        "cognition_coverage": cognition_coverage(samples),
        "database_bytes": _databases(samples),
        "counters": {
            key: (_counts(samples, key)[-1] if samples else None)
            for key in (
                "enqueued",
                "delivered",
                "mind_enqueued",
                "consumed",
                "suspended",
                "pending",
                "upstream_pending",
                "duplicate_processing",
                "duplicate_consumption",
                "thoughts",
                "scheduler_executions",
                "ask_sir_queue",
                "active_messages",
                "model_success",
                "model_failed",
                "model_unknown",
            )
        },
        "observation_errors": errors,
        "scope": (
            "Data-only observed window; OBSERVED_OK is not a duration, stability, "
            "production or behavioral acceptance claim."
        ),
    }


def cognition_coverage(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Conservative logical-hour coverage: only wholly contained deltas count.

    Do not invent timestamps for thoughts observed across an hour boundary; count
    those in neither hour. Coarse/missing/reset samples are UNKNOWN, not no-thought.
    This is scheduling simulation coverage, never 24h wall-time acceptance.
    """
    points = [
        (
            _number(row.get("logical_elapsed_seconds")),
            _number(_object(row.get("counters")).get("thoughts")),
        )
        for row in samples
    ]
    if len(points) < 2 or any(t is None or n is None for t, n in points):
        return {
            "status": "UNKNOWN",
            "complete_hours": 0,
            "hours_without_verified_thoughts": [],
            "hourly_minimum_thoughts": [],
        }
    valid = [(float(t), float(n)) for t, n in points if t is not None and n is not None]
    if any(b[0] <= a[0] or b[1] < a[1] for a, b in zip(valid, valid[1:], strict=False)):
        return {
            "status": "UNKNOWN",
            "complete_hours": 0,
            "hours_without_verified_thoughts": [],
            "hourly_minimum_thoughts": [],
        }
    origin = valid[0][0]
    valid = [(time - origin, count) for time, count in valid]
    first_hour = 0
    last_hour = math.floor(valid[-1][0] / 3600)
    hourly: list[int] = []
    for hour in range(first_hour, last_hour):
        count = sum(
            b[1] - a[1]
            for a, b in zip(valid, valid[1:], strict=False)
            if a[0] >= hour * 3600 and b[0] <= (hour + 1) * 3600
        )
        hourly.append(int(count))
    gaps = [first_hour + n for n, count in enumerate(hourly) if count == 0]
    coarse = any(b[0] - a[0] >= 3600 for a, b in zip(valid, valid[1:], strict=False))
    status = "UNKNOWN" if not hourly or coarse else "ISSUE" if gaps else "OBSERVED_OK"
    return {
        "status": status,
        "complete_hours": len(hourly),
        "hours_without_verified_thoughts": gaps,
        "hourly_minimum_thoughts": hourly,
        "method": "Only thought-count deltas wholly within a logical hour count; "
        "boundary deltas excluded. Hours anchored to first sample. "
        "Missing/reset/coarse (>=1h) samples UNKNOWN.",
    }


def _markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Soak observations",
        "",
        f"Status: {summary['status']}",
        f"Samples: {summary['sample_count']}; "
        f"observed wall seconds: {summary['observed_wall_seconds']}",
        "",
        str(summary["scope"]),
        "",
    ]
    for title, value in (
        ("Memory / Restarts / Heartbeat", summary["components"]),
        ("Queues", summary["queues"]),
        ("Duplicates", summary["duplicates"]),
        ("Model calls", summary["model_calls"]),
        ("Logical-hour cognition coverage", summary["cognition_coverage"]),
        ("Database bytes", summary["database_bytes"]),
        ("Final counters", summary["counters"]),
        ("Observation errors", summary["observation_errors"]),
    ):
        lines.extend(
            [
                f"## {title}",
                "",
                "```json",
                json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False),
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def write_summary(samples_path: Path, output_dir: Path) -> dict[str, Any]:
    """Read JSONL and create summaries in a fresh directory (never overwrite)."""
    samples: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    for line_number, line in enumerate(
        samples_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            value: object = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("expected JSON object")
            samples.append(cast(dict[str, Any], value))
        except (ValueError, TypeError) as exc:
            parse_errors.append(f"line {line_number}: {type(exc).__name__}")
    summary = analyze(samples)
    if parse_errors:
        summary["observation_errors"].extend(parse_errors)
        if summary["status"] != "ISSUE":
            summary["status"] = "UNKNOWN"
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "SUMMARY.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    (output_dir / "SUMMARY.md").write_text(_markdown(summary), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = write_summary(cast(Path, args.samples), cast(Path, args.output_dir))
    except OSError as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps({"status": result["status"], "output_dir": str(args.output_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
