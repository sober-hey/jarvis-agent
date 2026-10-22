"""Overland JSONL batches; administrative names only leave this adapter."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

from .base import Adapter, CursorStore, SenseEvent, digest, number, object_dict, parse_time


def self_evaluate() -> dict[str, object]:
    """Evaluate location parser contracts without reading tracks or geocoding."""
    from datetime import UTC

    return {
        "module": "senses.location",
        "domain": "Overland point ingestion and optional administrative region resolution",
        "current_version": "JSONL point dedupe by timestamp/coordinates with optional injected region resolver",
        "known_limitations": [
            "当前输出精确 lon/lat、Wi-Fi、海拔及 motion；不是仅行政区脱敏，也不由 precision=meter 证明实际米级精度",
            "行政区只接受有限中文后缀；默认没有 resolver，不含 city/district 的点只能 region_status=unknown",
            "去重只绑定时间与经纬度，同点补充行政区、精度或 motion 元数据不会重新发射",
            "motion 可为数组而基础 Payload 注解仅允许标量；当前没有运行时结构校验",
            "只读文件里的既有定位，不主动采集 GPS、识别在家状态或证明人员实际到场",
        ],
        "improvement_directions": [
            "明确坐标/Wi-Fi 的最小化策略及实际精度来源，避免标签冒充设备测量保证",
            "显式注入受控区域解析器并标记来源/不确定度，不在自评时发送坐标",
            "让内容修订签名覆盖有意义元数据并统一结构化载荷校验，测试同点补全行为",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


type RegionResolver = Callable[[float, float], str | None]


def administrative_name(value: object) -> str | None:
    # Administrative names only, never free-form addresses, URLs or coordinates.
    if isinstance(value, str) and re.fullmatch(r"[\u3400-\u9fff]{1,20}[市区县州盟旗]", value):
        return value
    return None


class LocationAdapter(Adapter):
    source = "overland"

    def __init__(self, path: Path, resolver: RegionResolver | None = None) -> None:
        super().__init__(path)
        self.resolver = resolver

    def poll(self, store: CursorStore, since: datetime | None = None) -> list[SenseEvent]:
        self.begin()
        events: list[SenseEvent] = []
        try:
            if not self.path.is_dir():
                return self.unavailable()
            for path in sorted(self.path.glob("*.jsonl")):
                for line, batch in self.lines(path, store):
                    points = batch.get("locations")
                    if not isinstance(points, list):
                        self.last_status.bad_rows += 1
                        continue
                    for index, raw in enumerate(cast(list[object], points)):
                        try:
                            point = object_dict(raw)
                            props = object_dict(point.get("properties"))
                            observed = parse_time(props.get("timestamp"))
                            coords = object_dict(point.get("geometry")).get("coordinates")
                            if not isinstance(coords, list):
                                raise ValueError("missing coordinates")
                            values = cast(list[object], coords)
                            if len(values) < 2:
                                raise ValueError("missing coordinates")
                            lon, lat = number(values[0]), number(values[1])
                            if not -180 <= lon <= 180 or not -90 <= lat <= 90:
                                raise ValueError("invalid coordinates")
                            region = administrative_name(
                                props.get("district")
                            ) or administrative_name(props.get("city"))
                            if region is None and self.resolver is not None:
                                region = administrative_name(self.resolver(lat, lon))
                            motion = props.get("motion")
                            wifi_name = props.get("wifi")
                            altitude = props.get("altitude")
                            h_acc = props.get("horizontal_accuracy")
                            payload_data: dict[str, object] = {
                                "lon": round(lon, 7),
                                "lat": round(lat, 7),
                                "precision": "meter",
                                "region": region,
                                "region_status": "resolved" if region else "unknown",
                            }
                            if h_acc is not None:
                                try:
                                    payload_data["horizontal_accuracy_meters"] = number(h_acc)
                                except (ValueError, TypeError):
                                    pass
                            if isinstance(motion, list) and motion:
                                payload_data["motion"] = motion
                            if isinstance(wifi_name, str) and wifi_name.strip():
                                payload_data["wifi"] = wifi_name.strip()
                            if altitude is not None:
                                try:
                                    payload_data["altitude_meters"] = number(altitude)
                                except (ValueError, TypeError):
                                    pass
                            signature = digest([observed.isoformat(), lon, lat])
                            event = self.event(
                                "location",
                                observed,
                                "收到一条米级精度位置记录。",
                                payload_data,
                                f"{line}:{index}",
                                path=path,
                                line=line,
                            )
                            self.emit(events, event, store, signature, signature, since)
                        except (ValueError, TypeError, OverflowError):
                            self.last_status.bad_rows += 1
        except OSError:
            return self.unavailable()
        return events
