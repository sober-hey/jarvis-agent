"""HAE structured points and completed sleep intervals, read-only snapshots."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .base import (
    Adapter,
    CursorStore,
    Payload,
    SenseEvent,
    digest,
    number,
    parse_time,
    source_database,
)


def self_evaluate() -> dict[str, object]:
    """Describe health ingestion, not a health-data or diagnostic evaluation."""
    from datetime import UTC

    return {
        "module": "senses.health",
        "domain": "read-only health points and completed sleep interval ingestion",
        "current_version": "HAE points/sleep SQLite snapshots with changed-row fingerprints",
        "known_limitations": [
            "只传递聚合数值和起止区间，不分析疾病；未保留单位/设备来源，也不按 metric 校验生理范围",
            "没有 NN 间期处理，不能从当前 HRV 聚合值计算标准 RMSSD；SDNN 与 RMSSD 不能互换",
            "腕温若作为未知 metric 传入仅得到 health_metric；只能按来源作相对趋势，不能当核心体温或发热诊断",
            "睡眠只发 start/end/duration，未保留睡眠阶段；同日记录按 day 作为键，依赖源表唯一语义",
            "每轮扫描 points/sleep；任一表缺失会令整个适配器 unavailable，不是局部表降级",
        ],
        "improvement_directions": [
            "为测量单位、来源设备、质量与腕温基线定义明确数据契约，再做非诊断趋势分析",
            "只有在 NN 间期充足且质量可验证时才引入 RMSSD 分析，不从汇总值伪造",
            "补充睡眠阶段/多来源区间聚合与缺表降级测试，保持只读和迟到修订检测",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


KINDS = {
    "heart_rate": "heart_rate",
    "resting_heart_rate": "resting_heart_rate",
    "heart_rate_variability": "heart_rate_variability",
    "heart_rate_variability_sdnn": "heart_rate_variability",
    "step_count": "steps",
    "respiratory_rate": "respiratory_rate",
    "blood_oxygen_saturation": "oxygen_saturation",
}


class HealthAdapter(Adapter):
    source = "health"

    def poll(self, store: CursorStore, since: datetime | None = None) -> list[SenseEvent]:
        self.begin()
        events: list[SenseEvent] = []
        try:
            with source_database(self.path) as connection:
                # Row hashes catch INSERT OR REPLACE and backfilled samples whose
                # timestamps/rowids precede the last poll. Only changed records emit.
                for row in connection.execute("SELECT rowid,metric,ts,qty,vmin,vmax FROM points"):
                    self.last_status.scanned += 1
                    key = digest([row["metric"], row["ts"]])
                    signature = digest(tuple(row)[1:])
                    if not store.changed(self.source, key, signature):
                        continue
                    try:
                        observed = parse_time(row["ts"])
                        metric = str(row["metric"])
                        payload: Payload = {"metric": metric, "value": number(row["qty"])}
                        for column in ("vmin", "vmax"):
                            if row[column] is not None:
                                payload[column] = number(row[column])
                        event = self.event(
                            KINDS.get(metric, "health_metric"),
                            observed,
                            "收到一条健康测量记录。",
                            payload,
                            "points:rowid=" + str(row["rowid"]),
                        )
                        self.emit(events, event, store, key, signature, since)
                    except (ValueError, TypeError, OverflowError):
                        self.last_status.bad_rows += 1
                        store.mark(self.source, key, signature)
                for row in connection.execute("SELECT rowid,* FROM sleep"):
                    self.last_status.scanned += 1
                    key = "sleep:" + digest(row["day"])
                    signature = digest(tuple(row)[1:])
                    if not store.changed(self.source, key, signature):
                        continue
                    try:
                        start, end = parse_time(row["sleep_start"]), parse_time(row["sleep_end"])
                        if end < start:
                            raise ValueError("negative sleep interval")
                        payload = {
                            "start": start.isoformat(),
                            "end": end.isoformat(),
                            "duration_seconds": (end - start).total_seconds(),
                        }
                        event = self.event(
                            "sleep",
                            end,
                            "收到一段已结束的睡眠记录。",
                            payload,
                            "sleep:rowid=" + str(row["rowid"]),
                        )
                        self.emit(events, event, store, key, signature, since)
                    except (ValueError, TypeError, OverflowError):
                        self.last_status.bad_rows += 1
                        store.mark(self.source, key, signature)
        except (OSError, sqlite3.Error):
            return self.unavailable()
        return events
