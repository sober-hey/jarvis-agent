"""Local read-only health analytics, not medical diagnosis or emergency monitoring.

Supports raw HealthKit ``points(type,startDate,endDate,value,unit,...)`` and
hub ``points(metric,ts,day,qty,vmin,vmax,source)`` plus its ``sleep`` table.
The latter contains aggregates, not raw beats: p95 is a percentile of exported
averages and is explicitly labelled. Missing days are None, never zero. Windows
include completed local calendar days ending yesterday; sleep belongs to its
wake-up date. Public summaries require stdlib only; _read_metric needs pandas.

Step/sleep sources are not summed across devices. For each day choose the source
with the largest coverage, preferring Watch on a tie. This is an engineering
fallback, not HealthKit's proprietary source-priority deduplication.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date as Date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import tempfile
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

_ALIASES = {
    "heart_rate": ("HKQuantityTypeIdentifierHeartRate", "heart_rate"),
    "resting_heart_rate": ("HKQuantityTypeIdentifierRestingHeartRate", "resting_heart_rate"),
    "spo2": (
        "HKQuantityTypeIdentifierOxygenSaturation",
        "blood_oxygen_saturation",
        "oxygen_saturation",
        "spo2",
    ),
    "steps": ("HKQuantityTypeIdentifierStepCount", "step_count", "steps"),
    "sleep": ("HKCategoryTypeIdentifierSleepAnalysis", "sleep_analysis", "sleep"),
    "body_temperature": ("HKQuantityTypeIdentifierBodyTemperature", "body_temperature"),
    "wrist_temperature": (
        "HKQuantityTypeIdentifierAppleSleepingWristTemperature",
        "apple_sleeping_wrist_temperature",
        "wrist_temperature",
    ),
}
_UNITS = {
    "heart_rate": "bpm",
    "resting_heart_rate": "bpm",
    "spo2": "%",
    "steps": "count",
    "body_temperature": "°C",
    "wrist_temperature": "°C",
    "sleep": "category",
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _mean(values) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.mean(vals) if vals else None


def _percentile(values, p: float) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    pos = (len(vals) - 1) * p
    lo, hi = math.floor(pos), math.ceil(pos)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S.%f %z"):
            try:
                dt = datetime.strptime(value.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt.astimezone() if dt.tzinfo else dt.astimezone()


def _union(intervals) -> tuple[float, datetime | None, datetime | None]:
    merged = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(end, merged[-1][1])
    return (
        sum((b - a).total_seconds() for a, b in merged) / 3600,
        merged[0][0] if merged else None,
        merged[-1][1] if merged else None,
    )


def _clock(values) -> str | None:
    times = [dt.hour * 60 + dt.minute for v in values if (dt := _datetime(v))]
    if not times:
        return None
    # Circular mean prevents midnight from averaging to noon.
    angle = math.atan2(
        sum(math.sin(t * math.tau / 1440) for t in times),
        sum(math.cos(t * math.tau / 1440) for t in times),
    )
    minute = round((angle % math.tau) * 1440 / math.tau) % 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


class HealthAnalyzer:
    def __init__(self, health_db_path: Path, data_root: Path):
        self.health_db_path = Path(health_db_path).expanduser().resolve()
        self.data_root = Path(data_root).expanduser()
        self._quality: dict[str, Any] = {}

    def self_evaluate(self) -> dict[str, Any]:
        """Describe analytical gaps without reading health data or diagnosing."""
        from datetime import UTC

        return {
            "module": "health_analyzer",
            "domain": "HRV analysis apple watch health anomaly detection",
            "current_version": "read-only HealthKit/hub summaries with source-aware aggregation",
            "known_limitations": [
                "NN 间期数据稀疏，无法计算标准 RMSSD",
                "腿温只有趋势偏差，不是绝对温度",
            ],
            "improvement_directions": [
                "neurokit2 即时 HRV 分析",
                "Z-score 异常检测渐进替换阈值规则",
            ],
            "last_evaluated_at": datetime.now(UTC).isoformat(),
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.health_db_path.as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    @staticmethod
    def _window(days: int, end: Date | None = None) -> tuple[Date, Date]:
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            raise ValueError("days must be a positive integer")
        end = end or (datetime.now().date() - timedelta(days=1))
        return end - timedelta(days=days - 1), end

    @staticmethod
    def _key(metric_type: str) -> str:
        for key, aliases in _ALIASES.items():
            if metric_type == key or metric_type in aliases:
                return key
        raise ValueError(f"Unsupported health metric: {metric_type}")

    @staticmethod
    def _normal_value(metric: str, value: Any, unit: str) -> float | None:
        v = _number(value)
        if v is None:
            return None
        u = unit.strip().lower()
        if metric == "spo2":
            if u in ("fraction", "1"):
                if not 0 < v <= 1:
                    return None
                v *= 100
            elif u == "" and 0 < v <= 1:
                v *= 100
            elif u not in ("", "%", "percent", "percentage"):
                return None
            if not 0 < v <= 100:
                return None
        elif metric in ("heart_rate", "resting_heart_rate"):
            if u in ("count/s", "hz"):
                v *= 60
            elif u not in ("", "bpm", "count/min", "counts/min", "count/minute"):
                return None
            if v <= 0:
                return None
        elif metric in ("body_temperature", "wrist_temperature"):
            if u in ("degf", "°f", "f", "fahrenheit"):
                v = (v - 32) * 5 / 9
            elif u not in ("", "degc", "°c", "c", "celsius"):
                return None
        elif metric == "steps" and (v < 0 or u not in ("", "count", "counts", "steps", "步")):
            return None
        return v

    def _records(self, metric: str, start: Date, end: Date) -> list[dict[str, Any]]:
        aliases = _ALIASES[metric]
        with closing(self._connect()) as conn:
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(points)")}
            marks = ",".join("?" for _ in aliases)
            if {"type", "startDate", "endDate", "value", "unit"} <= columns:
                schema = "healthkit"
                source = "sourceName" if "sourceName" in columns else "''"
                device = "device" if "device" in columns else "''"
                # Widen SQL UTC/local bounds by a day, then filter parsed local dates.
                low = (start - timedelta(days=2)).isoformat()
                high = (end + timedelta(days=2)).isoformat()
                sql = f"SELECT type,startDate,endDate,value,unit,{source} AS source,{device} AS device FROM points WHERE type IN ({marks}) AND startDate>=? AND startDate<?"
                raw = conn.execute(sql, (*aliases, low, high)).fetchall()
            elif {"metric", "ts", "day", "qty"} <= columns:
                schema = "hub"
                fields = ",".join(
                    n if n in columns else f"NULL AS {n}" for n in ("vmin", "vmax", "source")
                )
                sql = f"SELECT metric,ts,day,qty,{fields} FROM points WHERE metric IN ({marks}) AND day>=? AND day<=?"
                raw = conn.execute(sql, (*aliases, start.isoformat(), end.isoformat())).fetchall()
            else:
                raise ValueError(
                    "Unsupported points schema; expected HealthKit or hub metric/ts/qty"
                )
        self._quality[metric] = {"schema": schema, "rejected_records": 0}
        out, seen = [], set()
        for row in raw:
            r = dict(row)
            dt = _datetime(r.get("startDate") if schema == "healthkit" else r.get("ts"))
            end_dt = _datetime(r.get("endDate")) or dt
            if dt is None or end_dt is None:
                self._quality[metric]["rejected_records"] += 1
                continue
            day = end_dt.date() if metric == "sleep" else dt.date()
            if schema == "hub":
                try:
                    day = Date.fromisoformat(str(r["day"]))
                except (TypeError, ValueError):
                    self._quality[metric]["rejected_records"] += 1
                    continue
            if not start <= day <= end:
                continue
            unit = str(r.get("unit") or "") if schema == "healthkit" else _UNITS[metric]
            value = r.get("value") if schema == "healthkit" else r.get("qty")
            if metric != "sleep":
                value = self._normal_value(metric, value, unit)
                if value is None:
                    self._quality[metric]["rejected_records"] += 1
                    continue
            source = str(r.get("source") or "")
            signature = (dt.isoformat(), end_dt.isoformat(), str(value), unit, source)
            if signature in seen:
                continue
            seen.add(signature)
            out.append(
                {
                    "timestamp": dt,
                    "endDate": end_dt,
                    "date": day.isoformat(),
                    "value": value,
                    "unit": _UNITS[metric],
                    "sourceName": source,
                    "device": r.get("device") or "",
                    "type": r.get("type", r.get("metric")),
                    "min": self._normal_value(metric, r.get("vmin"), unit),
                    "max": self._normal_value(metric, r.get("vmax"), unit),
                    "aggregated": schema == "hub",
                }
            )
        return sorted(out, key=lambda r: r["timestamp"])

    def _read_metric(self, metric_type: str, days: int = 30) -> pd.DataFrame:
        """Normalized, timezone-aware timestamp index; pandas is an optional extra."""
        import pandas as pd

        start, end = self._window(days)
        rows = self._records(self._key(metric_type), start, end)
        names = (
            "endDate",
            "date",
            "value",
            "unit",
            "sourceName",
            "device",
            "type",
            "min",
            "max",
            "aggregated",
        )
        frame = pd.DataFrame(rows) if rows else pd.DataFrame(columns=("timestamp", *names))
        frame.index = pd.DatetimeIndex(frame.pop("timestamp"), name="timestamp")
        if frame.empty:
            frame.index = pd.DatetimeIndex(
                [], tz=datetime.now().astimezone().tzinfo, name="timestamp"
            )
        return frame.sort_index()

    @staticmethod
    def _days(start: Date, end: Date):
        for i in range((end - start).days + 1):
            yield (start + timedelta(days=i)).isoformat()

    @staticmethod
    def _selected(rows):
        """Single-source fallback avoids double counting Watch + phone exports."""
        groups: dict[str, list] = {}
        for row in rows:
            groups.setdefault(row["sourceName"], []).append(row)
        if not groups:
            return []
        return max(
            groups.values(),
            key=lambda rs: (len(rs), "watch" in rs[0]["sourceName"].lower(), rs[0]["sourceName"]),
        )

    @staticmethod
    def _meta(metric: str, start: Date, end: Date, daily: list) -> dict[str, Any]:
        return {
            "metric": metric,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "days": (end - start).days + 1,
            "daily": daily,
            "missing_data_policy": "missing_is_none_not_zero",
        }

    @staticmethod
    def _trend(current, previous) -> dict[str, Any]:
        cur, prev = _mean(current), _mean(previous)
        delta = cur - prev if cur is not None and prev is not None else None
        return {
            "current_mean": cur,
            "previous_mean": prev,
            "difference": delta,
            "percent_change": delta / prev * 100 if delta is not None and prev else None,
            "direction": None
            if delta is None
            else ("up" if delta > 0 else "down" if delta < 0 else "stable"),
        }

    def _heart_rate(self, days: int, end: Date) -> dict[str, Any]:
        start, end = self._window(days, end)
        comparison_start = min(start, end - timedelta(days=13))
        hr = self._records("heart_rate", comparison_start, end)
        resting = self._records("resting_heart_rate", comparison_start, end)
        by_day = {}
        for d in self._days(comparison_start, end):
            h = [r for r in hr if r["date"] == d]
            rr = [r["value"] for r in resting if r["date"] == d]
            by_day[d] = {
                "date": d,
                "avg_resting_heart_rate": _mean(rr),
                "min_resting_heart_rate": min(rr) if rr else None,
                "max_resting_heart_rate": max(rr) if rr else None,
                "avg_heart_rate": _mean(r["value"] for r in h),
                "sample_count": len(h),
                "resting_sample_count": len(rr),
            }
        daily = [by_day[d] for d in self._days(start, end)]
        selected = [r for r in hr if start.isoformat() <= r["date"] <= end.isoformat()]
        mins = [r["min"] if r["min"] is not None else r["value"] for r in selected]
        maxs = [r["max"] if r["max"] is not None else r["value"] for r in selected]
        current = [
            by_day[d]["avg_resting_heart_rate"] for d in self._days(end - timedelta(days=6), end)
        ]
        previous = [
            by_day[d]["avg_resting_heart_rate"]
            for d in self._days(end - timedelta(days=13), end - timedelta(days=7))
        ]
        return {
            **self._meta("heart_rate", start, end, daily),
            "avg_resting_heart_rate": _mean(r["avg_resting_heart_rate"] for r in daily),
            "min_resting_heart_rate": min(
                (
                    r["min_resting_heart_rate"]
                    for r in daily
                    if r["min_resting_heart_rate"] is not None
                ),
                default=None,
            ),
            "max_resting_heart_rate": max(
                (
                    r["max_resting_heart_rate"]
                    for r in daily
                    if r["max_resting_heart_rate"] is not None
                ),
                default=None,
            ),
            "avg_heart_rate": _mean(r["value"] for r in selected),
            "max_heart_rate": max(maxs) if maxs else None,
            "min_heart_rate": min(mins) if mins else None,
            "p95": _percentile([r["value"] for r in selected], 0.95),
            "p95_basis": "exported_averages"
            if any(r["aggregated"] for r in selected)
            else "samples",
            "trend": {
                **self._trend(current, previous),
                "window_days": 7,
                "metric": "resting_heart_rate",
            },
            "data_quality": {
                k: dict(self._quality[k]) for k in ("heart_rate", "resting_heart_rate")
            },
        }

    def analyze_heart_rate(self, days: int = 7) -> dict[str, Any]:
        return self._heart_rate(days, self._window(days)[1])

    def _steps(self, days: int, end: Date) -> dict[str, Any]:
        start, end = self._window(days, end)
        prev_start = start - timedelta(days=days)
        rows = self._records("steps", prev_start, end)
        daily = []
        for d in self._days(prev_start, end):
            chosen = self._selected([r for r in rows if r["date"] == d])
            daily.append(
                {
                    "date": d,
                    "steps": sum(r["value"] for r in chosen) if chosen else None,
                    "sample_count": len(chosen),
                    "source": chosen[0]["sourceName"] if chosen else None,
                }
            )
        previous, current = daily[:days], daily[days:]
        observed = [r for r in current if r["steps"] is not None]
        return {
            **self._meta("steps", start, end, current),
            "avg_daily_steps": _mean(r["steps"] for r in current),
            "highest_day": max(observed, key=lambda r: r["steps"]) if observed else None,
            "observed_days": len(observed),
            "source_policy": "single_source_per_day_max_sample_coverage",
            "trend": self._trend([r["steps"] for r in current], [r["steps"] for r in previous]),
            "data_quality": dict(self._quality["steps"]),
        }

    def analyze_steps(self, days: int = 7) -> dict[str, Any]:
        return self._steps(days, self._window(days)[1])

    def _spo2(self, days: int, end: Date) -> dict[str, Any]:
        start, end = self._window(days, end)
        rows = self._records("spo2", start, end)
        daily = []
        for d in self._days(start, end):
            records = [r for r in rows if r["date"] == d]
            vals = [r["value"] for r in records]
            mins = [r["min"] if r["min"] is not None else r["value"] for r in records]
            daily.append(
                {
                    "date": d,
                    "avg_spo2": _mean(vals),
                    "min_spo2": min(mins) if mins else None,
                    "abnormal_count": sum(v < 95 for v in mins),
                    "sample_count": len(vals),
                }
            )
        vals = [r["value"] for r in rows]
        mins = [r["min"] if r["min"] is not None else r["value"] for r in rows]
        return {
            **self._meta("spo2", start, end, daily),
            "avg_spo2": _mean(vals),
            "min_spo2": min(mins) if mins else None,
            "abnormal_count": sum(v < 95 for v in mins),
            "abnormal_count_basis": "exported_intervals"
            if any(r["aggregated"] for r in rows)
            else "samples",
            "sample_count": len(vals),
            "abnormal_threshold": 95.0,
            "unit": "%",
            "data_quality": dict(self._quality["spo2"]),
        }

    def analyze_spo2(self, days: int = 7) -> dict[str, Any]:
        return self._spo2(days, self._window(days)[1])

    @staticmethod
    def _stage(value: Any) -> str | None:
        names = {
            "0": "in_bed",
            "1": "asleep",
            "2": "awake",
            "3": "core",
            "4": "deep",
            "5": "rem",
            "inbed": "in_bed",
            "asleep": "asleep",
            "asleepunspecified": "asleep",
            "asleepcore": "core",
            "asleepdeep": "deep",
            "asleeprem": "rem",
            "core": "core",
            "deep": "deep",
            "rem": "rem",
            "awake": "awake",
        }
        text = (
            str(value)
            .replace("HKCategoryValueSleepAnalysis", "")
            .replace("_", "")
            .replace(" ", "")
            .lower()
        )
        if text.endswith(".0"):
            text = text[:-2]
        return names.get(text)

    def _sleep(self, days: int, end: Date) -> dict[str, Any]:
        start, end = self._window(days, end)
        with closing(self._connect()) as conn:
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(sleep)")}
            hub = {"day", "sleep_start", "sleep_end", "total", "deep"} <= columns
            raw = (
                conn.execute(
                    "SELECT day,sleep_start,sleep_end,total,deep FROM sleep WHERE day>=? AND day<=?",
                    (start.isoformat(), end.isoformat()),
                ).fetchall()
                if hub
                else []
            )
        daily = []
        if hub:
            by_day = {r["day"]: dict(r) for r in raw}
            for d in self._days(start, end):
                r = by_day.get(d, {})
                total, deep = _number(r.get("total")), _number(r.get("deep"))
                total = total if total is not None and 0 <= total <= 24 else None
                deep = (
                    deep if deep is not None and total is not None and 0 <= deep <= total else None
                )
                daily.append(
                    {
                        "date": d,
                        "sleep_hours": total,
                        "deep_sleep_hours": deep,
                        "deep_sleep_ratio": deep / total if deep is not None and total else None,
                        "sleep_onset": r.get("sleep_start"),
                        "wake_time": r.get("sleep_end"),
                    }
                )
            quality = {"schema": "hub_sleep", "duration_unit": "hours"}
        else:
            rows = self._records("sleep", start - timedelta(days=1), end)
            # Stage intervals before midnight belong to the same sleep session
            # as following stages. Three-hour separation is an engineering
            # sessionization heuristic, not a clinical sleep rule.
            by_source: dict[str, list] = {}
            for r in rows:
                if (
                    self._stage(r["value"]) in ("asleep", "core", "deep", "rem")
                    and r["endDate"] > r["timestamp"]
                ):
                    by_source.setdefault(r["sourceName"], []).append(r)
            sessions = []
            for group in by_source.values():
                session = []
                stop = None
                for r in sorted(group, key=lambda r: r["timestamp"]):
                    if stop is not None and r["timestamp"] > stop + timedelta(hours=3):
                        sessions.append(session)
                        session = []
                        stop = None
                    session.append(r)
                    stop = max(stop, r["endDate"]) if stop else r["endDate"]
                if session:
                    sessions.append(session)
            rows = []
            for session in sessions:
                wake_date = max(r["endDate"] for r in session).date().isoformat()
                rows.extend({**r, "date": wake_date} for r in session)
            for d in self._days(start, end):
                chosen = self._selected(
                    [
                        r
                        for r in rows
                        if r["date"] == d
                        and self._stage(r["value"]) in ("asleep", "core", "deep", "rem")
                    ]
                )
                intervals = [
                    (r["timestamp"], r["endDate"]) for r in chosen if r["endDate"] > r["timestamp"]
                ]
                deep_intervals = [
                    (r["timestamp"], r["endDate"])
                    for r in chosen
                    if self._stage(r["value"]) == "deep" and r["endDate"] > r["timestamp"]
                ]
                total, onset, wake = _union(intervals)
                deep, _, _ = _union(deep_intervals)
                has_deep = any(self._stage(r["value"]) in ("deep", "core", "rem") for r in chosen)
                # No stages != measured zero deep sleep.
                measured = bool(intervals) and total <= 24
                daily.append(
                    {
                        "date": d,
                        "sleep_hours": total if measured else None,
                        "deep_sleep_hours": deep if measured and has_deep else None,
                        "deep_sleep_ratio": deep / total
                        if measured and has_deep and total
                        else None,
                        "sleep_onset": onset.isoformat() if measured else None,
                        "wake_time": wake.isoformat() if measured else None,
                    }
                )
            quality = dict(self._quality["sleep"])
        total = sum(
            r["sleep_hours"]
            for r in daily
            if r["sleep_hours"] is not None and r["deep_sleep_hours"] is not None
        )
        deep = sum(r["deep_sleep_hours"] for r in daily if r["deep_sleep_hours"] is not None)
        return {
            **self._meta("sleep", start, end, daily),
            "avg_sleep_hours": _mean(r["sleep_hours"] for r in daily),
            "deep_sleep_ratio": deep / total if total else None,
            "sleep_onset": _clock([r["sleep_onset"] for r in daily]),
            "wake_time": _clock([r["wake_time"] for r in daily]),
            "data_quality": quality,
        }

    def analyze_sleep(self, days: int = 7) -> dict[str, Any]:
        return self._sleep(days, self._window(days)[1])

    def _temperature(self, days: int, end: Date) -> dict[str, Any]:
        start, end = self._window(days, end)
        rows = self._records("wrist_temperature", start, end)
        kind = "wrist"
        if not rows:
            rows = self._records("body_temperature", start, end)
            kind = "body"
        baseline = None
        path = self.data_root / "health_baseline.json"
        baseline_status = "not_calibrated"
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                temp = saved.get("temperature", {})
                if saved.get("version") == 1 and temp.get("kind") == kind:
                    baseline = _number(temp.get("median"))
                    baseline_status = "available" if baseline is not None else "no_valid_samples"
                else:
                    baseline_status = "incompatible_baseline"
            except (ValueError, OSError, TypeError, AttributeError):
                baseline_status = "invalid_baseline"
        daily = []
        for d in self._days(start, end):
            vals = [r["value"] for r in rows if r["date"] == d]
            avg = _mean(vals)
            daily.append(
                {
                    "date": d,
                    "avg_temperature": avg,
                    "baseline_temperature": baseline,
                    "deviation_from_baseline": avg - baseline
                    if avg is not None and baseline is not None
                    else None,
                    "kind": kind,
                    "sample_count": len(vals),
                }
            )
        avg = _mean(r["avg_temperature"] for r in daily)
        return {
            **self._meta("temperature", start, end, daily),
            "kind": kind,
            "unit": "°C",
            "avg_temperature": avg,
            "baseline_temperature": baseline,
            "deviation_from_baseline": avg - baseline
            if avg is not None and baseline is not None
            else None,
            "baseline_status": baseline_status,
            "data_quality": dict(self._quality[kind + "_temperature"]),
        }

    def analyze_temperature(self, days: int = 7) -> dict[str, Any]:
        """Wrist trends preferred. Body temperature is never relabelled wrist."""
        return self._temperature(days, self._window(days)[1])

    def daily_summary(self, date: str | None = None) -> dict[str, Any]:
        """Target-day aggregates plus seven-day daily history for streak rules."""
        if date is not None and (not isinstance(date, str) or len(date) != 10):
            raise ValueError("date must be YYYY-MM-DD")
        target = Date.fromisoformat(date) if date is not None else self._window(1)[1]
        result: dict[str, Any] = {
            "metric": "daily_summary",
            "date": target.isoformat(),
            "created_at": datetime.now().astimezone().isoformat(),
        }
        for name, fn in (
            ("heart_rate", self._heart_rate),
            ("sleep", self._sleep),
            ("steps", self._steps),
            ("spo2", self._spo2),
            ("temperature", self._temperature),
        ):
            item = fn(1, target)
            item["daily"] = fn(7, target)["daily"]
            item["history_days"] = 7
            result[name] = item
        return result

    def baseline_calibration(self, days: int = 30) -> dict[str, Any]:
        """Observed daily median/MAD; no public reference values or normality claim.

        Insufficient days are saved with calibrated=False, never invented. At
        least seven observed days is an engineering quality gate, not a clinical
        validation. Caller explicitly controls when a new baseline is accepted.
        """
        start, end = self._window(days)
        hr = self._heart_rate(days, end)
        sl = self._sleep(days, end)
        st = self._steps(days, end)
        ox = self._spo2(days, end)
        temp = self._temperature(days, end)
        out: dict[str, Any] = {
            "version": 1,
            "created_at": datetime.now().astimezone().isoformat(),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "days": days,
            "method": "daily_median_and_unscaled_MAD",
            "minimum_observed_days": 7,
        }
        for name, result, field in (
            ("heart_rate", hr, "avg_resting_heart_rate"),
            ("sleep", sl, "sleep_hours"),
            ("steps", st, "steps"),
            ("spo2", ox, "avg_spo2"),
            ("temperature", temp, "avg_temperature"),
        ):
            vals = [r[field] for r in result["daily"] if r[field] is not None]
            median = statistics.median(vals) if vals else None
            out[name] = {
                "median": median,
                "mad": statistics.median([abs(v - median) for v in vals]) if vals else None,
                "mean": _mean(vals),
                "p05": _percentile(vals, 0.05),
                "p95": _percentile(vals, 0.95),
                "observed_days": len(vals),
                "coverage": len(vals) / days,
                "calibrated": len(vals) >= 7,
            }
        out["temperature"]["kind"] = temp["kind"]
        self.data_root.mkdir(parents=True, exist_ok=True)
        target = self.data_root / "health_baseline.json"
        # Same-directory replace is atomic; health.db is never written.
        fd, tmp = tempfile.mkstemp(prefix=".health-baseline-", suffix=".json", dir=self.data_root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(out, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return out
