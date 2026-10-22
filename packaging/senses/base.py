"""Event contract, private cursor state, and read-only source helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Generator, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from types import TracebackType
from typing import Literal, cast
from zoneinfo import ZoneInfo

type Scalar = str | int | float | bool | None
type Payload = dict[str, Scalar]
type JSONObject = dict[str, object]
LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


def self_evaluate() -> dict[str, object]:
    """Evaluate source/cursor primitives only; never inspect a live source."""
    return {
        "module": "senses.base",
        "domain": "sensor event provenance, source snapshots and cursor persistence",
        "current_version": "UTC events, SQLite cursor fingerprints and complete-line JSONL resumption",
        "known_limitations": [
            "JSONL 只校验 inode、长度与前 4096 字节 anchor；同 inode/同长度/前缀不变的后段原地改写可能漏检",
            "DB/WAL 通过文件复制和 stat 比对建快照，不是写者协作事务；竞态会报 unavailable",
            "Payload 标量类型是类型注解而非运行时验证；location 的 motion 数组及精确坐标并不被这里删去",
            "朴素时间固定按 Asia/Shanghai 解释；游标和 seen 指纹未设置保留期或清理策略",
            "SenseEvent repr 隐藏载荷不是加密/脱敏保证；provenance 仍保留来源路径",
        ],
        "improvement_directions": [
            "为文件改写建立可验证版本或分块校验，覆盖前缀外同长替换场景",
            "定义可容纳结构化载荷的运行时校验和敏感字段策略，不将类型声明当安全边界",
            "补充并发快照及显式时区测试，设计有回放边界的游标指纹清理",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


def parse_time(value: object, default_zone: tzinfo = LOCAL_ZONE) -> datetime:
    """Normalize sensor timestamps to UTC; naive local sources use an explicit zone."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing event timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        result = datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")
    if result.tzinfo is None:
        result = result.replace(tzinfo=default_zone)
    return result.astimezone(UTC)


def number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("invalid numeric field")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite numeric field")
    return result


def object_dict(value: object) -> JSONObject:
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return cast(JSONObject, value)


def json_object(text: str) -> JSONObject:
    return object_dict(cast(object, json.loads(text)))


@dataclass(frozen=True)
class Provenance:
    path: str
    record_id: str
    line: int | None = None


@dataclass(frozen=True, repr=False)
class SenseEvent:
    source: str
    kind: str
    observed_at: datetime
    ingested_at: datetime
    summary: str
    payload: Payload = field(repr=False)
    sensitivity: Literal["low", "medium", "high"]
    provenance: Provenance = field(repr=False)

    def __repr__(self) -> str:
        return f"SenseEvent(source={self.source!r}, kind={self.kind!r})"


@dataclass
class AdapterStatus:
    source: str
    status: Literal["available", "unavailable"] = "available"
    scanned: int = 0
    bad_rows: int = 0
    emitted: int = 0
    reason: str | None = None


class CursorStore:
    """Only opaque hashes, offsets, and source keys persist; never sensor payloads."""

    def __init__(self, path: Path, *, dry_run: bool = False) -> None:
        self.path = path
        self.dry_run = dry_run
        if dry_run:
            self.connection = sqlite3.connect(":memory:")
            if path.is_file():
                with closing(
                    sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                ) as src:
                    src.backup(self.connection)
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink():
                raise ValueError("cursor cannot be a symlink")
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            self.connection = sqlite3.connect(path)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS cursor (source TEXT, key TEXT, value TEXT, "
            "PRIMARY KEY(source,key))"
        )
        self.connection.commit()

    def get(self, source: str, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM cursor WHERE source=? AND key=?", (source, key)
        ).fetchone()
        return str(row[0]) if row else None

    def set(self, source: str, key: str, value: str) -> None:
        self.connection.execute("INSERT OR REPLACE INTO cursor VALUES(?,?,?)", (source, key, value))

    def changed(self, source: str, key: str, signature: str) -> bool:
        return self.get(source, "seen:" + key) != signature

    def mark(self, source: str, key: str, signature: str) -> None:
        self.set(source, "seen:" + key, signature)

    def __enter__(self) -> CursorStore:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if kind is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@contextmanager
def source_database(path: Path) -> Generator[sqlite3.Connection]:
    """Copy committed DB + WAL; SQLite can only create/write sidecars in scratch.

    A live writer can race file copies. SQLite validation failures are surfaced as
    unavailable by adapters, never retried through a writable source connection.
    """
    if not path.is_file():
        raise FileNotFoundError("source missing")
    with tempfile.TemporaryDirectory(prefix="jarvis-senses-sqlite-") as directory:
        target = Path(directory) / "source.db"
        wal = Path(str(path) + "-wal")

        def stamp(file: Path) -> tuple[int, int, int] | None:
            try:
                info = file.stat()
            except FileNotFoundError:
                return None
            return info.st_ino, info.st_size, info.st_mtime_ns

        before = (stamp(path), stamp(wal))
        shutil.copyfile(path, target)
        if before[1] is not None:
            shutil.copyfile(wal, Path(str(target) + "-wal"))
        if before != (stamp(path), stamp(wal)):
            raise OSError("source changed during snapshot")
        with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.row_factory = sqlite3.Row
            yield connection


class Adapter:
    source: str = "base"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_status = AdapterStatus(self.source)

    def poll(self, store: CursorStore, since: datetime | None = None) -> list[SenseEvent]:
        raise NotImplementedError

    def begin(self) -> None:
        self.last_status = AdapterStatus(self.source)

    def unavailable(self) -> list[SenseEvent]:
        self.last_status.status = "unavailable"
        self.last_status.reason = "source_missing_unreadable_or_schema_unavailable"
        return []

    def event(
        self,
        kind: str,
        observed_at: datetime,
        summary: str,
        payload: Payload,
        record_id: str,
        *,
        path: Path | None = None,
        line: int | None = None,
        sensitivity: Literal["low", "medium", "high"] = "high",
    ) -> SenseEvent:
        return SenseEvent(
            self.source,
            kind,
            observed_at,
            datetime.now(UTC),
            summary,
            payload,
            sensitivity,
            Provenance(str(path or self.path), record_id, line),
        )

    def emit(
        self,
        events: list[SenseEvent],
        event: SenseEvent,
        store: CursorStore,
        key: str,
        signature: str,
        since: datetime | None,
    ) -> None:
        if store.changed(self.source, key, signature):
            if since is None or event.observed_at >= since:
                events.append(event)
                self.last_status.emitted += 1
            store.mark(self.source, key, signature)

    def lines(self, path: Path, store: CursorStore) -> Iterator[tuple[int, JSONObject]]:
        """Resume complete JSONL lines; incomplete trailing writes remain pending.

        Anchor the consumed prefix and identity so truncation/replacement resets
        offsets. Per-event fingerprints suppress replay after a reset.
        """
        key = "file:" + digest(str(path.resolve()))
        state = json_object(store.get(self.source, key) or "{}")
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            offset = int(number(state.get("offset", 0)))
            line_no = int(number(state.get("line", 0)))
            anchor_size = min(offset, 4096)
            anchor = hashlib.sha256(stream.read(anchor_size)).hexdigest()
            if (
                state.get("inode") != info.st_ino
                or info.st_size < offset
                or state.get("anchor") != anchor
            ):
                offset, line_no = 0, 0
            stream.seek(offset)
            while raw := stream.readline():
                if not raw.endswith(b"\n"):
                    break
                line_no += 1
                offset = stream.tell()
                self.last_status.scanned += 1
                try:
                    row = json_object(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    self.last_status.bad_rows += 1
                    continue
                yield line_no, row
            stream.seek(0)
            anchor = hashlib.sha256(stream.read(min(offset, 4096))).hexdigest()
            store.set(
                self.source,
                key,
                json.dumps(
                    {"inode": info.st_ino, "offset": offset, "line": line_no, "anchor": anchor}
                ),
            )
