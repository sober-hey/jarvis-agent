"""Unified polling without writing the inner-loop database."""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from .activitywatch import ActivityWatchAdapter
from .base import Adapter, AdapterStatus, CursorStore, SenseEvent, parse_time
from .browser import BrowserHistoryAdapter
from .health import HealthAdapter
from .location import LocationAdapter
from .speech import SpeechAdapter


def self_evaluate() -> dict[str, object]:
    """Describe polling boundaries without constructing or polling real adapters."""
    from datetime import UTC

    return {
        "module": "senses.api",
        "domain": "unified incremental sensor polling and cursor transactions",
        "current_version": "five default adapters, per-adapter savepoints and ordered SenseEvent output",
        "known_limitations": [
            "O(source rows)：增量发射并非增量扫描，SQLite 仍全表比对且所有新事件在内存排序",
            "默认源路径绑定本机目录；自评不查源可用性，传入 adapters 才能显式隔离数据源",
            "仅回滚标为 unavailable 的适配器；意外异常会中断整轮并回滚游标事务",
            "返回事件后游标已提交，但调用方尚未保证写入 mind；不是端到端恰好一次投递",
            "本层不写 mind/KG；下游 capabilities 清洗未识别 browser_history/page_visit 时会变为 unknown",
        ],
        "improvement_directions": [
            "引入有界扫描/发射批次并保留迟到回填和修订事件语义",
            "将源配置和权限诊断显式化，用合成故障验证每源失败隔离",
            "设计 durable outbox/ack 后再提交游标的交付契约，另补浏览源清洗与来源投影验收",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


def default_adapters() -> list[Adapter]:
    root = Path("/Volumes/扩展硬盘/hermes")
    return [
        LocationAdapter(root / "tracks"),
        HealthAdapter(root / "health/health.db"),
        ActivityWatchAdapter(
            Path.home() / "Library/Application Support/activitywatch/aw-server/peewee-sqlite.v2.db"
        ),
        SpeechAdapter(root / "lifelog/data"),
        BrowserHistoryAdapter(root / "lifelog/browser_history/chrome"),
    ]


def default_data_root() -> Path:
    return Path(os.environ.get("JARVIS_HOME", str(Path.home() / ".jarvis")))


def poll_all(
    *,
    data_root: Path | None = None,
    adapters: Sequence[Adapter] | None = None,
    since: str | datetime | None = None,
    dry_run: bool = False,
    statuses: list[AdapterStatus] | None = None,
) -> list[SenseEvent]:
    """Return newly observed/revised events; caller owns delivery and mind storage.

    Normal polls advance cursors even for records older than ``since``. Dry runs
    read an existing cursor into memory and never create/advance persistent state.
    SQLite adapters scan row identities to detect late backfill/replacements.
    This is incremental emission; source scan cost is currently O(source rows).
    """
    cutoff = parse_time(since) if isinstance(since, str) else since
    if cutoff is not None and cutoff.tzinfo is None:
        cutoff = parse_time(cutoff.isoformat())
    selected = list(adapters) if adapters is not None else default_adapters()
    root = (data_root or default_data_root()).absolute()
    # Prevent accidentally placing cursor writes inside any read-only source.
    for adapter in selected:
        source = adapter.path.resolve()
        target = root.resolve() / "senses_cursor.db"
        if target == source or source in target.parents:
            raise ValueError("cursor root overlaps sensor source")
    events: list[SenseEvent] = []
    with CursorStore(root / "senses_cursor.db", dry_run=dry_run) as store:
        # RELEASE of a top-level SQLite SAVEPOINT commits immediately. Keep an
        # explicit outer transaction so a later unexpected failure cannot leave
        # earlier adapters consumed without delivering their returned events.
        store.connection.execute("BEGIN")
        for adapter in selected:
            store.connection.execute("SAVEPOINT adapter_poll")
            new_events = adapter.poll(store, cutoff)
            if adapter.last_status.status == "unavailable":
                store.connection.execute("ROLLBACK TO adapter_poll")
                adapter.last_status.emitted = 0
            else:
                events.extend(new_events)
            store.connection.execute("RELEASE adapter_poll")
            if statuses is not None:
                statuses.append(adapter.last_status)
    return sorted(events, key=lambda event: (event.observed_at, event.source, event.kind))


def statistics(events: Sequence[SenseEvent]) -> dict[str, object]:
    return {"count": len(events), "kinds": dict(sorted(Counter(e.kind for e in events).items()))}
