"""Trusted senses, web search, citation ledger and synthetic fixture capabilities."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import amap
from device_controller import DeviceCapability
from senses.api import default_adapters, default_data_root, poll_all
from senses.base import Adapter, AdapterStatus, parse_time
from senses.location import administrative_name

# Trusted metadata is private; returned dictionaries may be mutated by callers.
_REAL: list[dict[str, Any]] = [
    {
        "id": identity,
        "name": name,
        "provides": provides,
        "persona_description": persona_description,
        "call": {"name": "senses_poll", "arguments": {"source": source}},
        "costs_money": False,
        "security_sensitive": False,
        "precision": precision,
        "freshness": "latest_record_timestamp; historical records may be stale",
        "implementation": "senses_read_only",
        "available": True,
    }
    for identity, source, name, provides, precision, persona_description in (
        (
            "location_track",
            "overland",
            "行政区时序记录",
            "行政区名称、记录时间与解析状态；不含坐标。",
            "administrative",
            "我能查看你的行政区位置记录与采集时间，判断记录是否仍然新鲜",
        ),
        (
            "health_record",
            "health",
            "健康记录概况",
            "健康及睡眠记录的类型与时间；不含测量值。",
            "record_metadata",
            "我能查看心率、睡眠、步数等健康记录的类型与时间，但这条感知不提供测量值",
        ),
        (
            "activity_usage",
            "activitywatch",
            "应用使用概况",
            "前台应用记录的时间与数量；不含页面、窗口标题或链接。",
            "foreground_metadata",
            "我能查看前台应用活动的记录时间与数量，但这条感知不提供应用名称和使用时长",
        ),
        (
            "speech_record",
            "speech",
            "语音记录概况",
            "语音记录的时间、宽泛主题与语言；不含原文。",
            "broad_category",
            "我能查看语音记录的时间、宽泛主题和语言，但这条感知不提供原话",
        ),
    )
]

_REAL.append(
    {
        "id": "health_monitoring",
        "name": "健康监测与分析",
        "provides": "分析心率、睡眠、血氧、步数等 Apple Watch 健康数据并识别异常。",
        "persona_description": "我能看到你的心率、睡眠、血氧、步数等 Apple Watch 健康数据，发现异常会主动告诉你",
        "call": {"name": "senses_poll", "arguments": {"source": "health"}},
        "costs_money": False,
        "security_sensitive": False,
        "precision": "health_metrics",
        "freshness": "latest_record_timestamp; historical records may be stale",
        "implementation": "senses_read_only",
        "available": True,
    }
)
_REAL.append(deepcopy(DeviceCapability.descriptor))

_WEB: list[dict[str, Any]] = [
    {
        "id": "model_search",
        "persona_description": "我能上网了解当前模型的能力与对比，更新自己的模型档案",
        "name": "模型能力搜索",
        "provides": "搜索当前最佳 AI 模型对比，更新模型档案库",
        "call": {"name": "model_search", "arguments": {"task_type": ""}},
        "costs_money": False,
        "security_sensitive": False,
        "precision": "snippet",
        "freshness": "on_demand_search_snippet; verify source publication dates",
        "implementation": "model_search",
        "available": True,
    },
    {
        "id": "web_search",
        "persona_description": "我能随时上网搜索，想查什么直接查，并保留找到的资料来源",
        "name": "联网搜索",
        "provides": "用 DuckDuckGo 搜索互联网，返回标题、URL、摘要",
        "call": {"name": "web_search", "arguments": {"query": "", "max_results": 5}},
        "costs_money": False,
        "security_sensitive": False,
        "precision": "snippet",
        "freshness": "on_demand_search_snippet; verify source publication dates",
        "implementation": "web_search",
        "available": True,
    },
    {
        "id": "citation_lookup",
        "persona_description": "我能查回自己记录过的搜索引用，找到当时依据的资料来源",
        "name": "引用查询",
        "provides": "按对话 ID 或关键词查历史搜索引用",
        "call": {"name": "citation_lookup", "arguments": {}},
        "costs_money": False,
        "security_sensitive": False,
        "precision": "exact",
        "freshness": "historical_search_snapshot",
        "implementation": "citation_lookup",
        "available": True,
    },
]

CATALOG: list[dict[str, Any]] = [
    {
        "id": "location_track",
        "name": "城市级时序样本",
        "provides": "带采集时间的城市级数据样本；固定样本，不连接设备。",
        "persona_description": "我能读取一个固定的城市样本，但它不是你的真实位置",
        "call": {"name": "local_city_sample", "arguments": {}},
        "costs_money": False,
        "security_sensitive": False,
        "precision": "city",
        "freshness": "fixed_synthetic_sample",
        "implementation": "local_fixture",
        "available": True,
    }
]

# Live Web services are separate from read-only local senses. Coordinates and
# keywords leave the machine, so these descriptors are always permission-gated.
_AMAP: list[dict[str, Any]] = [
    {
        "id": "amap_reverse_geocode",
        "persona_description": "我能在获准后查清位置坐标对应的街道与地址，查询会把坐标发给地图服务",
        "name": "高德逆地理编码",
        "provides": "把最新坐标解析为省市区、乡镇和格式化地址；坐标会发送到高德。",
        "call": {
            "name": "amap_reverse_geocode",
            "arguments": {"coord_type": "wgs84", "radius": 1000},
        },
        "costs_money": True,
        "security_sensitive": True,
        "precision": "street_address",
        "freshness": "on_demand_live",
        "implementation": "amap_http",
        "available": False,
    },
    {
        "id": "amap_nearby_poi",
        "persona_description": "我能在获准后查找你附近的地点，查询会把坐标与查找内容发给地图服务",
        "name": "高德周边地点搜索",
        "provides": "以最新坐标为中心按关键词检索周边地点；坐标和关键词会发送到高德。",
        "call": {
            "name": "amap_around_search",
            "arguments": {
                "coord_type": "wgs84",
                "keywords": "餐饮",
                "radius": 1000,
                "limit": 10,
            },
        },
        "costs_money": True,
        "security_sensitive": True,
        "precision": "poi",
        "freshness": "on_demand_live",
        "implementation": "amap_http",
        "available": False,
    },
]

SLEEP_CATALOG_ENTRY: dict[str, Any] = {
    "id": "sleep_record",
    "name": "睡眠记录",
    "provides": "固定的入睡时间样本；不连接设备。",
    "persona_description": "我能读取一个固定的睡眠样本，但它不是你的真实睡眠记录",
    "call": {"name": "local_sleep_sample", "arguments": {}},
    "costs_money": False,
    "security_sensitive": False,
    "precision": "synthetic_sleep_time",
    "freshness": "fixed_synthetic_sample",
    "implementation": "local_fixture",
    "available": True,
}


def self_evaluate() -> dict[str, Any]:
    """Audit the static descriptor lists without probing any source.

    No adapter is constructed, no permission is checked, no network or database
    call is made. Duplicates or non-first-person descriptions are reported as
    facts about the declarations, not treated as delivery evidence.
    """
    groups = {"real": _REAL, "web": _WEB, "amap": _AMAP, "fixture": CATALOG}
    entries: list[dict[str, Any]] = []
    for group in groups.values():
        entries.extend(group)
    descriptions = [
        entry.get("persona_description")
        for entry in entries
        if isinstance(entry.get("persona_description"), str)
    ]
    duplicates = {}
    for name, group in groups.items():
        identifiers = [entry.get("id") for entry in group]
        repeated = sorted(
            {
                str(value)
                for value in identifiers
                if value is not None and identifiers.count(value) > 1
            }
        )
        if repeated:
            duplicates[name] = repeated
    return {
        "module": "capabilities",
        "domain": "trusted capability descriptors, dispatch and synthetic fixtures",
        "entry_counts": {name: len(group) for name, group in groups.items()},
        "duplicate_ids": duplicates,
        "first_person_descriptions": sum(1 for text in descriptions if text.startswith("我")),
        "known_limitations": [
            "Descriptor counts are static; availability flags here are defaults, not live probes.",
            "Execution paths require adapters, credentials and permissions that this hook does not exercise.",
            "Synthetic fixture entries are intentionally not real device or web evidence.",
            "Fixture ids may repeat real ids (for example location_track); the groups are separate namespaces.",
        ],
        "improvement_directions": [
            "Add a catalog-wide duplicate/first-person assertion to the product test suite.",
            "Report per-source evidence timestamps without contacting the source.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


def catalog(*, adapters: Sequence[Adapter] | None = None) -> list[dict[str, Any]]:
    """Describe real sources plus search/ledger without network or database I/O."""
    selected = list(default_adapters() if adapters is None else adapters)
    by_source = {adapter.source: adapter for adapter in selected}
    entries = deepcopy(_REAL)
    for entry in entries:
        if entry["implementation"] == "device_control":
            continue  # Listing devices is available even without a Xiaomi account.
        adapter = by_source.get(entry["call"]["arguments"]["source"])
        try:
            entry["available"] = bool(
                adapter is not None
                and adapter.path.exists()
                and os.access(adapter.path, os.R_OK)
                and adapter.last_status.status != "unavailable"
            )
        except OSError:
            entry["available"] = False
    return entries + deepcopy(_WEB)


def fixture_catalog(*, include_sleep: bool = False) -> list[dict[str, Any]]:
    entries = deepcopy(CATALOG)
    if include_sleep:
        entries.append(deepcopy(SLEEP_CATALOG_ENTRY))
    return entries


def amap_catalog() -> list[dict[str, Any]]:
    """Live map descriptors; availability only reflects whether a key is set."""
    entries = deepcopy(_AMAP)
    available = amap.configured()
    for entry in entries:
        entry["available"] = available
    return entries


# Never validate against the mutable public fixture compatibility constants.
_FIXTURES = deepcopy([*CATALOG, SLEEP_CATALOG_ENTRY])


def recognized(capability: Mapping[str, Any]) -> bool:
    """Match exact trusted metadata without performing I/O; availability is advisory."""
    if not isinstance(capability.get("available"), bool):
        return False
    candidate = {key: value for key, value in capability.items() if key != "available"}
    for trusted in [*_REAL, *_FIXTURES, *_AMAP, *_WEB]:
        expected = {key: value for key, value in trusted.items() if key != "available"}
        # JSON preserves boolean-vs-number distinctions that Python equality ignores.
        try:
            if json.dumps(candidate, sort_keys=True, allow_nan=False) == json.dumps(
                expected, sort_keys=True, allow_nan=False
            ):
                return True
        except (TypeError, ValueError):
            return False
    return False


def _base_summary(source: str, status: str) -> dict[str, Any]:
    entry = next(
        (entry for entry in _REAL if entry["call"]["arguments"].get("source") == source), None
    )
    return {
        "sampled_at": None,
        "synthetic": False,
        "source": source if entry else "unknown",
        "precision": entry["precision"] if entry else "unknown",
        "status": status,
    }


def summarize_event(
    source: str, kind: str, payload: Mapping[str, Any], observed_at: str | datetime
) -> dict[str, Any]:
    """Allowlist metadata only, including for passive runtime sensor events."""
    summary = _base_summary(source, "available")
    if payload.get("synthetic") or payload.get("backfill") or payload.get("background_only"):
        summary["synthetic"] = bool(payload.get("synthetic"))
        summary["status"] = "unavailable"
        return summary
    try:
        stamp = observed_at.isoformat() if isinstance(observed_at, datetime) else observed_at
        summary["sampled_at"] = parse_time(stamp).isoformat()
    except (ValueError, TypeError, OverflowError):
        summary["status"] = "unavailable"
        return summary
    allowed_kinds = {
        "overland": {"location"},
        "health": {
            "heart_rate",
            "resting_heart_rate",
            "heart_rate_variability",
            "steps",
            "respiratory_rate",
            "oxygen_saturation",
            "health_metric",
            "sleep",
        },
        "activitywatch": {"app_usage"},
        "speech": {"speech"},
    }
    summary["kind"] = kind if kind in allowed_kinds.get(source, set()) else "unknown"
    if summary["kind"] == "unknown":
        summary["status"] = "unavailable"
        return summary
    if source == "overland":
        region = administrative_name(payload.get("region"))
        summary["region"] = region
        summary["region_status"] = "resolved" if region else "unknown"
    elif source == "speech":
        topic = payload.get("topic")
        language = payload.get("language")
        summary["topic"] = (
            topic if topic in ("工作交流", "音乐相关", "运动相关", "日常语音") else "unknown"
        )
        summary["language"] = (
            language if language in ("zh", "en", "ja", "ko", "yue", "und") else "und"
        )
    return summary


def _execute_real(capability: dict[str, Any], adapters: Sequence[Adapter] | None) -> dict[str, Any]:
    source = capability["call"]["arguments"]["source"]
    result = _base_summary(source, "unavailable")
    # Multiple capabilities can share a source; source-only summaries use its
    # canonical descriptor, while explicit execution retains the selected one.
    result["precision"] = capability["precision"]
    selected = list(default_adapters() if adapters is None else adapters)
    adapter = next((adapter for adapter in selected if adapter.source == source), None)
    if adapter is None:
        return result
    statuses: list[AdapterStatus] = []
    try:
        if not adapter.path.exists() or not os.access(adapter.path, os.R_OK):
            adapter.unavailable()
            return result
        # A fresh empty root prevents dry_run from importing the daemon's live cursor.
        with tempfile.TemporaryDirectory(prefix="jarvis-capability-") as directory:
            events = poll_all(
                data_root=Path(directory), adapters=[adapter], dry_run=True, statuses=statuses
            )
        if not statuses or statuses[0].status == "unavailable":
            return result
        if statuses[0].bad_rows and not events:
            adapter.unavailable()
            return result
        result["status"] = "available" if events else "empty"
        result["record_count"] = len(events)
        # One latest record provides a bounded response regardless of source history.
        if events:
            event = events[-1]
            result.update(summarize_event(source, event.kind, event.payload, event.observed_at))
            result["precision"] = capability["precision"]
        return result
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
        adapter.unavailable()
        return {**_base_summary(source, "unavailable"), "precision": capability["precision"]}


def _latest_location(
    adapters: Sequence[Adapter] | None,
) -> tuple[float, float, str] | None:
    """Read the newest local coordinate for an explicit map lookup, never for context."""
    selected = list(default_adapters() if adapters is None else adapters)
    adapter = next((adapter for adapter in selected if adapter.source == "overland"), None)
    if adapter is None:
        return None
    try:
        if not adapter.path.exists() or not os.access(adapter.path, os.R_OK):
            adapter.unavailable()
            return None
        statuses: list[AdapterStatus] = []
        with tempfile.TemporaryDirectory(prefix="jarvis-amap-") as directory:
            events = poll_all(
                data_root=Path(directory), adapters=[adapter], dry_run=True, statuses=statuses
            )
        if not events or not statuses or statuses[0].status == "unavailable":
            return None
        payload = events[-1].payload
        raw_latitude = payload["lat"]
        raw_longitude = payload["lon"]
        if (
            isinstance(raw_latitude, bool)
            or isinstance(raw_longitude, bool)
            or not isinstance(raw_latitude, (int, float))
            or not isinstance(raw_longitude, (int, float))
        ):
            return None
        latitude = float(raw_latitude)
        longitude = float(raw_longitude)
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            return None
        return latitude, longitude, events[-1].observed_at.isoformat()
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
        adapter.unavailable()
        return None


def _execute_amap(
    capability: dict[str, Any],
    adapters: Sequence[Adapter] | None,
    client: amap.AmapClient | None,
    overrides: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "sampled_at": None,
        "synthetic": False,
        "source": "amap",
        "precision": capability["precision"],
        "status": "unavailable",
        "provider": "amap",
        "coordinate_system": "gcj02",
    }
    selected = client or amap.AmapClient.from_environment()
    if selected is None:
        result["error"] = "not_configured"
        return result
    arguments = dict(capability["call"]["arguments"])
    if overrides:
        if set(overrides) - set(arguments):
            raise ValueError("Unknown Amap argument")
        arguments.update(overrides)
    point = _latest_location(adapters)
    if point is None:
        result["error"] = "location_unavailable"
        return result
    latitude, longitude, sampled_at = point
    result["sampled_at"] = sampled_at
    try:
        if capability["call"]["name"] == "amap_reverse_geocode":
            data = selected.reverse_geocode(
                latitude,
                longitude,
                coord_type=str(arguments.get("coord_type", "wgs84")),
                radius=cast(int, arguments.get("radius", 1000)),
            )
        elif capability["call"]["name"] == "amap_around_search":
            data = selected.search_poi(
                latitude,
                longitude,
                arguments.get("keywords", "餐饮"),
                coord_type=str(arguments.get("coord_type", "wgs84")),
                radius=cast(int, arguments.get("radius", 1000)),
                limit=cast(int, arguments.get("limit", 10)),
            )
        else:
            result["error"] = "unknown_action"
            return result
    except amap.AmapError as error:
        result["error"] = error.code
        return result
    except (OSError, ValueError, TypeError):
        result["error"] = "invalid_request"
        return result
    result.update(data)
    if data.get("kind") == "poi_search":
        result["status"] = "available" if data.get("count") else "empty"
    else:
        result["status"] = "available"
    return result


def _execute_web(
    capability: dict[str, Any],
    overrides: Mapping[str, Any] | None,
    data_root: Path | None,
    conversation_id: str | None,
) -> dict[str, Any]:
    from citations import CitationStore

    name = capability["call"]["name"]
    arguments = dict(capability["call"]["arguments"])
    if name == "model_search":
        allowed = {"task_type"}
    else:
        allowed = (
            {"query", "max_results", "conversation_id"}
            if name == "web_search"
            else {"citation_id", "query", "conversation_id"}
        )
    if overrides is not None:
        if set(overrides) - allowed:
            raise ValueError("Unknown search/citation argument")
        arguments.update(overrides)
    result: dict[str, Any] = {
        "sampled_at": datetime.now().astimezone().isoformat(),
        "synthetic": False,
        "source": name,
        "precision": capability["precision"],
        "status": "unavailable",
        "results": [],
        "count": 0,
    }
    selected_conversation = arguments.get("conversation_id", conversation_id)
    if selected_conversation is not None and not isinstance(selected_conversation, str):
        raise ValueError("conversation_id must be a string")
    if name in {"web_search", "model_search"}:
        import search

        if name == "model_search":
            task_type = arguments.get("task_type", "")
            if not isinstance(task_type, str):
                raise ValueError("task_type must be a string")
            query = f"best AI model for {task_type} 2026 comparison benchmark"
            rows = search.search(query)
            result["task_type"] = task_type
        else:
            query = arguments.get("query", "")
            limit = arguments.get("max_results", 5)
            if not isinstance(query, str) or not query.strip():
                result["error"] = "query_required"
                return result
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError("max_results must be a positive integer")
            rows = search.search(query, max_results=limit)
        # Search snippets are evidence, not an automatic model quality rating.
        store = CitationStore(data_root if data_root is not None else default_data_root())
        result["citation_id"] = store.record(query, rows, selected_conversation)
        result["query"] = query
    else:
        selectors = [key for key in ("citation_id", "conversation_id", "query") if key in arguments]
        if not selectors and selected_conversation is not None:
            selectors = ["conversation_id"]
            arguments["conversation_id"] = selected_conversation
        if len(selectors) != 1 or not isinstance(arguments[selectors[0]], str):
            result["error"] = "provide_one_of_citation_id_conversation_id_query"
            return result
        store = CitationStore(data_root if data_root is not None else default_data_root())
        selector = selectors[0]
        if selector == "citation_id":
            entry = store.get(arguments[selector])
            rows = [entry] if entry else []
        elif selector == "conversation_id":
            rows = store.list_for_conversation(arguments[selector])
        else:
            rows = store.search_citations(arguments[selector])
    result.update(results=rows, count=len(rows), status="available" if rows else "empty")
    return result


def invoke(
    call: Mapping[str, Any],
    *,
    data_root: Path | None = None,
    conversation_id: str | None = None,
    adapters: Sequence[Adapter] | None = None,
    amap_client: amap.AmapClient | None = None,
) -> dict[str, Any]:
    """Dispatch a named call without letting dynamic arguments alter trusted metadata."""
    name = call.get("name")
    arguments = call.get("arguments", {})
    if not isinstance(arguments, Mapping):
        raise ValueError("call.arguments must be a mapping")
    if name == "senses_poll":
        entry = next((e for e in _REAL if e["call"]["arguments"] == arguments), None)
    else:
        entry = next(
            (e for e in [*_REAL, *_WEB, *_AMAP, *_FIXTURES] if e["call"]["name"] == name), None
        )
    if entry is None:
        raise ValueError("Unknown capability call")
    if entry["implementation"] in {
        "web_search",
        "citation_lookup",
        "model_search",
        "device_control",
    }:
        return execute(
            deepcopy(entry),
            arguments=arguments,
            data_root=data_root,
            conversation_id=conversation_id,
        )
    if entry["implementation"] == "amap_http":
        return execute(
            deepcopy(entry), adapters=adapters, amap_client=amap_client, amap_arguments=arguments
        )
    if arguments != entry["call"]["arguments"]:
        raise ValueError("Unexpected capability arguments")
    return execute(deepcopy(entry), adapters=adapters)


def execute(
    capability: dict[str, Any],
    *,
    adapters: Sequence[Adapter] | None = None,
    amap_client: amap.AmapClient | None = None,
    amap_arguments: Mapping[str, Any] | None = None,
    arguments: Mapping[str, Any] | None = None,
    data_root: Path | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Execute trusted metadata; supply dynamic search/lookup arguments separately."""
    if not recognized(capability):
        raise ValueError("Unknown or altered capability metadata")
    if capability["implementation"] in {"web_search", "citation_lookup", "model_search"}:
        return _execute_web(capability, arguments, data_root, conversation_id)
    if capability["implementation"] == "device_control":
        result = DeviceCapability().execute(arguments or capability["call"]["arguments"])
        return {
            "sampled_at": datetime.now().astimezone().isoformat(),
            "synthetic": False,
            "source": "device_control",
            "precision": capability["precision"],
            **result,
        }
    if capability["implementation"] == "senses_read_only":
        return _execute_real(capability, adapters)
    if capability["implementation"] == "amap_http":
        return _execute_amap(capability, adapters, amap_client, amap_arguments)
    name = capability["call"]["name"]
    if name == "local_city_sample":
        return {
            "city": "天水",
            "sampled_at": "2026-10-15T00:00:00+00:00",
            "synthetic": True,
            "source": "local_city_sample",
            "precision": "city",
        }
    if name == "local_sleep_sample":
        return {
            "sleep_time": "02:00",
            "sampled_at": "2026-10-15T00:00:00+00:00",
            "synthetic": True,
            "source": "local_sleep_sample",
        }
    raise ValueError("Unknown local fixture")
