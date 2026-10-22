"""Non-diagnostic engineering alerts for analyzer dictionaries.

Defaults implement requested engineering thresholds, not clinical guidance.
Override JSON: {"version": 1, "rules": {"rule_id": {"threshold": number}}}.
Supported rule IDs: resting_heart_rate_high, resting_heart_rate_low,
spo2_low, daily_spo2_low, sleep_low, sleep_very_low_streak,
steps_low_streak, wrist_temperature_deviation. Only known thresholds may
be overridden; invalid files/values raise ValueError. No readings are read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
from typing import Any


@dataclass
class HealthAlert:
    severity: str
    metric: str
    message: str
    value: float
    threshold: float
    context: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


_DEFAULTS = {
    "resting_heart_rate_high": 100.0,
    "resting_heart_rate_low": 45.0,
    "spo2_low": 95.0,
    "daily_spo2_low": 97.0,
    "sleep_low": 5.0,
    "sleep_very_low_streak": 6.0,
    "steps_low_streak": 3000.0,
    "wrist_temperature_deviation": 1.5,
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


class HealthRules:
    """Apply versioned defaults and optional safe local threshold overrides."""

    DEFAULT_VERSION = 1

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.version = self.DEFAULT_VERSION
        self.thresholds = dict(_DEFAULTS)
        override = self.data_root / "health_rules.json"
        if override.exists():
            try:
                payload = json.loads(override.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid health_rules.json: {exc}") from exc
            if not isinstance(payload, dict) or payload.get("version") != self.DEFAULT_VERSION:
                raise ValueError("health_rules.json must have supported version 1")
            rules = payload.get("rules", {})
            if not isinstance(rules, dict) or set(rules) - set(_DEFAULTS):
                raise ValueError("health_rules.json has unknown or invalid rules")
            for name, config in rules.items():
                if not isinstance(config, dict) or set(config) != {"threshold"}:
                    raise ValueError(f"Invalid override for {name}")
                value = _number(config["threshold"])
                if value is None or value <= 0:
                    raise ValueError(f"Invalid threshold for {name}")
                self.thresholds[name] = value

    def self_evaluate(self) -> dict[str, Any]:
        """Describe active rule configuration without reading personal readings."""
        return {
            "module": "health_rules",
            "domain": "non-diagnostic health engineering thresholds and consecutive-day trends",
            "current_version": f"规则 v{self.version}：八项阈值规则，支持本地 threshold 覆盖与连续日期校验",
            "known_limitations": [
                "固定工程阈值不是临床诊断；不结合症状、训练背景或个体风险自动判断疾病",
                "腕温只比较相对个人基线的偏差趋势，不用绝对体温诊断发热；基线由上游提供而非本模块估算",
                "连续日规则依赖上游 daily 与 end_date，缺测中断序列；同日重复行以最后一行覆盖，不做源质量合并",
                "仅返回 HealthAlert，不读健康库、不写 mind/KG、不调度或投递通知；阈值覆盖只校验正有限数，未校验规则间一致性",
            ],
            "improvement_directions": [
                "用合成边界、缺测、重复日期和阈值覆盖用例固化非诊断性规则语义",
                "增加阈值之间的一致性校验及来源质量/基线充分性标记，避免将缺测当正常",
                "在保留工程阈值可解释性的前提下研究个体趋势，不将腕温偏差升级为诊断",
            ],
            "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _alert(
        self, severity: str, metric: str, rule: str, value: float, wording: str, **extra: Any
    ) -> HealthAlert:
        threshold = self.thresholds[rule]
        message = f"Sir，{wording}；此为非诊断性工程阈值提醒。"
        details = {"rule_id": rule, "rules_version": self.version, **extra}
        return HealthAlert(
            severity,
            metric,
            message,
            value,
            threshold,
            json.dumps(details, ensure_ascii=False, sort_keys=True),
        )

    @staticmethod
    def _daily(analysis: dict) -> list[dict]:
        daily = analysis.get("daily")
        if isinstance(daily, list):
            return [x for x in daily if isinstance(x, dict)]
        return []

    def _streak(
        self, analysis: dict, metric: str, field: str, threshold: float, days: int, predicate
    ) -> tuple[int, float | None, date | None]:
        rows = self._daily(analysis)
        anchor = _date(analysis.get("end_date") or analysis.get("date"))
        if anchor is None and rows:
            anchor = max((_date(r.get("date")) for r in rows if _date(r.get("date"))), default=None)
        if anchor is None:
            return 0, None, None
        by_date = {_date(r.get("date")): r for r in rows if _date(r.get("date")) is not None}
        values = []

        def valid(v: float | None) -> bool:
            if v is None:
                return False
            if metric == "spo2":
                return 0 < v <= 100
            if metric == "sleep":
                return 0 <= v <= 24
            return v >= 0

        for ago in range(days):
            row = by_date.get(anchor - timedelta(days=ago))
            v = _number(row.get(field)) if row else None
            if not valid(v) or not predicate(v):
                break
            values.append(v)
        return len(values), (values[-1] if values else None), anchor

    def evaluate(self, analysis: dict) -> list[HealthAlert]:
        if not isinstance(analysis, dict):
            return []
        metric = analysis.get("metric")
        summary = analysis.get("daily_summary")
        if not isinstance(summary, dict) and (
            metric == "daily_summary"
            or any(
                isinstance(analysis.get(k), dict)
                for k in ("heart_rate", "sleep", "steps", "spo2", "temperature")
            )
        ):
            summary = analysis
        if isinstance(summary, dict):
            metric = "daily_summary"
            # Subsection aggregate values represent the target day; retain only
            # its supplied daily history for consecutive-day rules.
            if metric == "daily_summary":
                result = []
                for name in ("heart_rate", "spo2", "sleep", "steps", "temperature"):
                    section = summary.get(name)
                    if isinstance(section, dict):
                        item = {
                            **section,
                            "metric": name,
                            "daily": section.get("daily", summary.get("daily", [])),
                            "date": section.get("date", summary.get("date")),
                            "end_date": section.get("end_date", summary.get("date")),
                        }
                        result.extend(self.evaluate(item))
                return result
        if metric not in {"heart_rate", "sleep", "steps", "spo2", "temperature"}:
            return []
        alerts = []
        if metric == "heart_rate":
            v = _number(analysis.get("max_resting_heart_rate"))
            if v is None:
                v = _number(analysis.get("avg_resting_heart_rate"))
            low = _number(analysis.get("min_resting_heart_rate"))
            if low is None:
                low = _number(analysis.get("avg_resting_heart_rate"))
            if v is not None and v > self.thresholds["resting_heart_rate_high"]:
                alerts.append(
                    self._alert(
                        "critical",
                        metric,
                        "resting_heart_rate_high",
                        v,
                        "静息心率高于设定阈值，请复核测量与静息状态；若持续偏高或伴胸痛、气促、晕厥，请及时就医",
                    )
                )
            if low is not None and 0 < low < self.thresholds["resting_heart_rate_low"]:
                alerts.append(
                    self._alert(
                        "warning",
                        metric,
                        "resting_heart_rate_low",
                        low,
                        "静息心率偏低，请先看个人趋势、运动训练背景并复核记录；单次低值不直接判为异常",
                    )
                )
        elif metric == "spo2":
            v = _number(analysis.get("min_spo2"))
            if v is not None and 0 < v <= 100 and v < self.thresholds["spo2_low"]:
                alerts.append(
                    self._alert(
                        "critical",
                        metric,
                        "spo2_low",
                        v,
                        "血氧读数低于设定阈值，请静止、正确佩戴后复测；若持续偏低或伴气促、胸痛、意识异常，请及时就医",
                    )
                )
            streak, val, anchor = self._streak(
                analysis,
                metric,
                "avg_spo2",
                self.thresholds["daily_spo2_low"],
                3,
                lambda x: x < self.thresholds["daily_spo2_low"],
            )
            if streak == 3:
                alerts.append(
                    self._alert(
                        "warning",
                        metric,
                        "daily_spo2_low",
                        val,
                        "连续三日平均血氧低于设定阈值，请复核趋势",
                        end_date=anchor.isoformat(),
                    )
                )
        elif metric == "sleep":
            v = _number(analysis.get("avg_sleep_hours"))
            if v is not None and 0 <= v <= 24 and v < self.thresholds["sleep_low"]:
                alerts.append(
                    self._alert("warning", metric, "sleep_low", v, "睡眠时长低于设定阈值")
                )
            streak, val, anchor = self._streak(
                analysis,
                metric,
                "sleep_hours",
                self.thresholds["sleep_very_low_streak"],
                3,
                lambda x: x < self.thresholds["sleep_very_low_streak"],
            )
            if streak == 3:
                alerts.append(
                    self._alert(
                        "critical",
                        metric,
                        "sleep_very_low_streak",
                        val,
                        "连续三日睡眠时长低于设定阈值，请复核趋势",
                        end_date=anchor.isoformat(),
                    )
                )
        elif metric == "steps":
            streak, val, anchor = self._streak(
                analysis,
                metric,
                "steps",
                self.thresholds["steps_low_streak"],
                7,
                lambda x: x < self.thresholds["steps_low_streak"],
            )
            if streak == 7:
                alerts.append(
                    self._alert(
                        "info",
                        metric,
                        "steps_low_streak",
                        val,
                        "连续七日步数低于设定阈值",
                        end_date=anchor.isoformat(),
                    )
                )
        elif metric == "temperature" and analysis.get("kind") == "wrist":
            deviation = _number(analysis.get("deviation_from_baseline"))
            if deviation is None:
                temp, baseline = (
                    _number(analysis.get("avg_temperature")),
                    _number(analysis.get("baseline_temperature")),
                )
                deviation = (
                    abs(temp - baseline) if temp is not None and baseline is not None else None
                )
            if (
                deviation is not None
                and abs(deviation) > self.thresholds["wrist_temperature_deviation"]
            ):
                alerts.append(
                    self._alert(
                        "warning",
                        metric,
                        "wrist_temperature_deviation",
                        abs(deviation),
                        "腕部温度相对个人基线偏离，请关注个人趋势；不使用绝对体温判断",
                    )
                )
        return alerts
