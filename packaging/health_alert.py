"""Health anomaly persistence, graph projection, and concise Sir-facing advice."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from inner_loop import MindStore

if TYPE_CHECKING:
    from knowledge_graph import KnowledgeGraph


class HealthAlertManager:
    """Project health alerts into the shared graph and append critical alerts to mind."""

    def __init__(self, data_root: Path, knowledge_graph: KnowledgeGraph) -> None:
        self.data_root = Path(data_root)
        self.knowledge_graph = knowledge_graph
        self.mind = MindStore(self.data_root)

    def self_evaluate(self) -> dict[str, Any]:
        """Describe persistence/reporting gaps without querying private stores."""
        return {
            "module": "health_alert",
            "domain": "health alert provenance persistence graph projection and reporting",
            "current_version": "health_event 图谱投影、critical 级 health_alert 入 mind.db 与有界文本报告",
            "known_limitations": [
                "仅 critical 告警进入 mind.db；近期查询只查最多 1000 条 health_alert，不能代表所有级别或完整历史",
                "KG 与 mind 写入不是跨库原子事务；本模块无独立重放去重，重复处理 critical 可能追加重复 mind 记录",
                "只生成固定模板建议，不读取健康测量、不作医学诊断，也不负责通知送达；上游排队不能算送达",
                "auto_link 是相关性而非因果推断；缺失/无效时间戳回退为处理时间，不能冒充可靠测量时间",
            ],
            "improvement_directions": [
                "增加跨库失败恢复与稳定告警 ID 去重协议，区分投影完成、排队和真实投递回执",
                "为全部告警级别建立可分页历史查询并保留原始时间戳质量标记",
                "让报告保留阈值来源、非诊断性与缺测边界，而非用通用建议替代具体风险复核",
            ],
            "last_evaluated_at": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _field(alert: Any, *names: str, default: Any = None) -> Any:
        record: Any = alert
        for name in names:
            if isinstance(record, Mapping) and name in record:
                return record[name]
            if hasattr(record, name):
                return getattr(record, name)
        return default

    @classmethod
    def _normalize(cls, alert: Any) -> dict[str, Any]:
        if not isinstance(alert, Mapping) and not hasattr(alert, "__dict__"):
            raise ValueError("Health alert must be a mapping or record object")
        raw: dict[str, Any] = dict(alert) if isinstance(alert, Mapping) else dict(vars(alert))
        observed = cls._field(alert, "observed_at", "timestamp", "created_at")
        if isinstance(observed, datetime):
            observed = observed.isoformat()
        if not isinstance(observed, str) or not observed.strip():
            observed = datetime.now(UTC).isoformat()
        try:
            parsed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
            observed = parsed.astimezone(UTC).isoformat()
        except (ValueError, OverflowError):
            observed = datetime.now(UTC).isoformat()
        severity = str(cls._field(alert, "severity", "level", default="warning")).lower()
        metric = str(cls._field(alert, "metric", "kind", "category", default="健康指标"))[:200]
        message = str(
            cls._field(alert, "message", "description", "summary", default="检测到健康指标异常")
        )[:2000]
        identity = cls._field(alert, "id", "alert_id", "event_id")
        if not isinstance(identity, str) or not identity.strip():
            identity = None
        evidence_ids: list[str] = []
        raw_evidence: Any = cls._field(alert, "evidence_ids", default=[])
        if isinstance(raw_evidence, list):
            evidence_ids = cast(list[str], [ref for ref in raw_evidence if isinstance(ref, str)])
        data = {
            "severity": severity,
            "metric": metric,
            "message": message,
            "observed_at": observed,
            "value": cls._field(alert, "value"),
            "unit": cls._field(alert, "unit"),
            "source": cls._field(alert, "source", default="health_analyzer"),
        }
        # Keep arbitrary extra attributes only when they are valid JSON data.
        for key in ("threshold", "baseline", "trend", "evidence_ids"):
            value = cls._field(alert, key)
            if value is not None:
                data[key] = value
        try:
            json.dumps(data, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            data = {
                k: v
                for k, v in data.items()
                if k in {"severity", "metric", "message", "observed_at", "source"}
            }
        data["id"] = identity
        return data

    @staticmethod
    def _advice(alert: Mapping[str, Any]) -> str:
        metric_labels = {
            "heart_rate": "心率",
            "resting_heart_rate": "静息心率",
            "heart_rate_variability": "心率变异性",
            "oxygen_saturation": "血氧",
            "steps": "步数",
            "sleep": "睡眠",
            "respiratory_rate": "呼吸频率",
        }
        metric = metric_labels.get(alert["metric"], alert["metric"])
        if alert["severity"] == "critical":
            return f"Sir，{metric}出现明显异常；先暂停剧烈活动并留意身体反应，记录复测结果。"
        if alert["severity"] in {"high", "warning", "moderate"}:
            return f"Sir，{metric}有偏离；今天留意休息、补水，并观察后续读数是否持续偏离。"
        return f"Sir，{metric}有轻微变化；保持日常节奏，留意接下来几次测量的趋势。"

    def process_alerts(self, alerts: list[Any]) -> list[str]:
        advice: list[str] = []
        for alert in alerts:
            item = self._normalize(alert)
            identity = item["id"] or f"{item['metric']}:{item['observed_at']}:{item['severity']}"
            evidence_ids = item.get("evidence_ids", [])
            if not isinstance(evidence_ids, list) or any(
                not isinstance(ref, str) for ref in evidence_ids
            ):
                evidence_ids = []
            self.knowledge_graph.add_node(
                "health_event",
                "health",
                item["metric"],
                item["message"],
                1.0 if item["severity"] == "critical" else 0.7,
                str(item["source"]),
                evidence_ids,
                origin_table="health_alerts",
                origin_id=identity,
                metadata={
                    key: value for key, value in item.items() if key not in {"id", "evidence_ids"}
                },
            )
            if item["severity"] == "critical":
                self.mind.append(
                    "health_alert",
                    {key: value for key, value in item.items() if key != "id"},
                    source="health_alert_manager",
                    refs=evidence_ids,
                )
            advice.append(self._advice(item))
        self.knowledge_graph.auto_link()
        return advice

    def get_recent_alerts(self, days: int = 7) -> list[dict[str, Any]]:
        if type(days) is not int or days < 0:
            raise ValueError("days must be a nonnegative integer")
        cutoff = datetime.now(UTC) - timedelta(days=days)
        rows = self.mind.entries(category="health_alert", limit=1000)
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
                payload = row["payload"]
                observed = datetime.fromisoformat(
                    str(payload.get("observed_at", row["created_at"])).replace("Z", "+00:00")
                )
                if (
                    created.tzinfo is None
                    or observed.tzinfo is None
                    or observed.astimezone(UTC) < cutoff
                ):
                    continue
                result.append(row)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return result

    def generate_health_report(self, analyzer_summary: dict[str, Any], alerts: list[Any]) -> str:
        status = analyzer_summary.get("summary", analyzer_summary.get("status", "暂无明显变化"))
        if not isinstance(status, str):
            status = "健康数据已更新"
        normalized = [self._normalize(alert) for alert in alerts]
        critical = [item["metric"] for item in normalized if item["severity"] == "critical"]
        notable = [
            item["metric"]
            for item in normalized
            if item["severity"] in {"high", "warning", "moderate"}
        ]
        overview = f"Sir，你当前的健康概况：{status[:70]}。"
        caution = "注意事项：" + (
            "需留意" + "、".join(dict.fromkeys(critical + notable)) + "的读数变化。"
            if critical or notable
            else "目前没有记录到明显异常。"
        )
        suggestion = "具体建议：今天保持规律休息与补水，并结合后续测量观察趋势。"
        return (overview + caution + suggestion)[:200]
