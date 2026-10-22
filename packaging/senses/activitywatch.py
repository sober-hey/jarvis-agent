"""ActivityWatch app-only events; no browser bucket, titles or page identity."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .base import (
    Adapter,
    CursorStore,
    SenseEvent,
    digest,
    json_object,
    number,
    parse_time,
    source_database,
)


def self_evaluate() -> dict[str, object]:
    """Describe foreground-event semantics without opening the ActivityWatch DB."""
    from datetime import UTC

    return {
        "module": "senses.activitywatch",
        "domain": "read-only ActivityWatch foreground application events",
        "current_version": "SQLite snapshot with per-row fingerprints and revised heartbeat emission",
        "known_limitations": [
            "foreground_not_attention：前台窗口时长不是注意力、阅读或有效工作时长；不结合 AFK 状态",
            "只选 currentwindow/app-only 桶；不输出页面 URL 或标题，不能推断浏览内容",
            "每次仍扫描候选行；同 ID duration 延长会重发修订事件，消费者不能把修订全量相加",
            "依赖当前 bucketmodel/eventmodel schema，快照竞态或权限失败统一标为 unavailable",
        ],
        "improvement_directions": [
            "显式关联 AFK 与窗口区间，保留前台时长和注意力推断的证据区别",
            "给修订事件增加可合并 identity/version 契约，避免时长重复累计",
            "在保持回填和原地更新检测的前提下减少全表扫描并细分源失败原因",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


class ActivityWatchAdapter(Adapter):
    source = "activitywatch"

    def poll(self, store: CursorStore, since: datetime | None = None) -> list[SenseEvent]:
        self.begin()
        events: list[SenseEvent] = []
        try:
            with source_database(self.path) as connection:
                query = (
                    "SELECT e.id,e.timestamp,e.duration,e.datastr FROM eventmodel e "
                    "JOIN bucketmodel b ON e.bucket_id=b.key "
                    "WHERE b.type='currentwindow' OR b.client LIKE '%app-only%'"
                )
                for row in connection.execute(query):
                    self.last_status.scanned += 1
                    key = "event:" + str(row["id"])
                    signature = digest(tuple(row))
                    if not store.changed(self.source, key, signature):
                        continue
                    try:
                        observed = parse_time(row["timestamp"])
                        duration = number(row["duration"])
                        data = json_object(str(row["datastr"]))
                        app = data.get("app")
                        if (
                            duration < 0
                            or not isinstance(app, str)
                            or not app.strip()
                            or len(app) > 160
                            or any(c in app for c in ("/", "\\", "\n"))
                        ):
                            raise ValueError("invalid app event")
                        event = self.event(
                            "app_usage",
                            observed,
                            "收到一段前台应用使用记录。",
                            {
                                "app": app,
                                "duration_seconds": duration,
                                "end": (observed + timedelta(seconds=duration)).isoformat(),
                                "duration_semantics": "foreground_not_attention",
                            },
                            key,
                            sensitivity="medium",
                        )
                        # Re-read same IDs because AW heartbeat extends duration in place.
                        self.emit(events, event, store, key, signature, since)
                    except (ValueError, TypeError, OverflowError):
                        self.last_status.bad_rows += 1
                        store.mark(self.source, key, signature)
        except (OSError, sqlite3.Error):
            return self.unavailable()
        return events
