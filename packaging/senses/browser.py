"""Read Hermes browser JSONL as domain-only, high-sensitivity visit events."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from .base import Adapter, CursorStore, Payload, SenseEvent, digest, number, parse_time


def self_evaluate() -> dict[str, object]:
    """Report browser adapter loss and privacy boundaries without reading history."""
    from datetime import UTC

    return {
        "module": "senses.browser",
        "domain": "incremental browser JSONL page visits and provenance",
        "current_version": "validated HTTP(S) visit events with URL/domain and optional title/duration",
        "known_limitations": [
            "并非 domain-only：当前 payload 保留完整 URL（含路径/查询及可能的 userinfo）和标题，属于高敏数据",
            "只表示 page_visit；duration_seconds 若由源提供也不等于实际阅读/注意力，不采集焦点心跳",
            "现有下游 capabilities.summarize_event 不认识 browser_history/page_visit，清洗会变为 unknown/unavailable",
            "JSONL 已消费偏移不重读：修改旧行或在文件内原地修订标题/时长可能不触发新事件",
        ],
        "improvement_directions": [
            "明确 URL/标题最小化与有授权全文保留两种策略，禁止把当前实现误称仅域名脱敏",
            "补齐下游源/种类白名单、第一人称能力和事件来源契约后再声称常驻接线完成",
            "为显式时长来源、焦点心跳与旧行修订提供独立版本和验证边界",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


class BrowserHistoryAdapter(Adapter):
    source = "browser_history"

    def poll(self, store: CursorStore, since: datetime | None = None) -> list[SenseEvent]:
        self.begin()
        events: list[SenseEvent] = []
        try:
            if not self.path.is_dir():
                return self.unavailable()
            # Hermes writes one ISO-date-named file per day.
            for path in sorted(self.path.glob("*.jsonl")):
                for line, row in self.lines(path, store):
                    try:
                        url = row.get("url")
                        if (
                            not isinstance(url, str)
                            or "\\" in url
                            or any(character.isspace() or ord(character) < 32 for character in url)
                        ):
                            raise ValueError("invalid browser URL")
                        parsed = urlsplit(url)
                        domain = parsed.hostname
                        if parsed.scheme not in {"http", "https"} or not domain:
                            raise ValueError("invalid browser URL")
                        # Validate (but never retain) ports; urlsplit is otherwise permissive.
                        if parsed.port is not None and not 0 < parsed.port <= 65535:
                            raise ValueError("invalid browser port")
                        observed = parse_time(row.get("visited_at", row.get("timestamp")))
                        payload: Payload = {"url": url, "domain": domain}
                        raw_title = row.get("title")
                        if isinstance(raw_title, str) and raw_title.strip():
                            payload["title"] = raw_title.strip()[:500]
                        if row.get("duration_seconds") is not None:
                            duration = number(row["duration_seconds"])
                            if duration < 0:
                                raise ValueError("negative visit duration")
                            payload["duration_seconds"] = duration
                        key = digest([str(path), row.get("record_id", line)])
                        event = self.event(
                            "page_visit",
                            observed,
                            "收到一条浏览页面访问记录。",
                            payload,
                            key,
                            path=path,
                            line=line,
                            sensitivity="high",
                        )
                        signature = digest([observed.isoformat(), event.payload])
                        self.emit(events, event, store, key, signature, since)
                    except (ValueError, TypeError, OverflowError):
                        self.last_status.bad_rows += 1
        except OSError:
            return self.unavailable()
        return events
