"""Immutable, content-addressed archive snapshots and provenance-aware readers."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote


def self_evaluate() -> dict[str, Any]:
    """Describe implemented behavior and gaps; never inspect runtime records."""
    return {
        "module": "absorb_formats",
        "domain": "archive snapshots structured document chunking provenance",
        "current_version": "Content-addressed read-only snapshots of SQLite and UTF-8 text formats with record provenance and 6000-character chunks",
        "known_limitations": [
            "Source bytes, records and chunks are materialized in memory; there is no streaming or file-size budget.",
            "PDF, Office, images and non-UTF-8 binary inputs have no dedicated parser; decoding failures are skipped.",
            "Each SQLite backup has a consistent transaction, but a multi-file archive is not a single atomic cross-file snapshot.",
        ],
        "improvement_directions": [
            "Explore bounded streaming readers while preserving stable record references and content hashes.",
            "Evaluate explicit format adapters and structured skip diagnostics on synthetic documents.",
            "Document and test archive-wide consistency boundaries for files changing during capture.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    snapshot_path: str
    source_ref: str
    format: str
    text: str
    source_file: str = ""


@dataclass(frozen=True)
class Snapshot:
    identity: str
    directory: Path
    files: list[dict[str, Any]]
    skipped: list[dict[str, str]]
    chunks: list[Chunk]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _private(path: Path) -> None:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Snapshot path contains symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def _pieces(text: str, maximum: int = 6000) -> list[str]:
    if len(text) <= maximum:
        return [text]
    result: list[str] = []
    current = ""
    for paragraph in re.split(r"(?<=\n)|(?<=[。！？.!?])", text):
        if len(current) + len(paragraph) > maximum and current:
            result.append(current)
            current = ""
        while len(paragraph) > maximum:
            result.append(paragraph[:maximum])
            paragraph = paragraph[maximum:]
        current += paragraph
    if current:
        result.append(current)
    return result


def _records(path: Path, fmt: str) -> list[tuple[str, str]]:
    if fmt == "sqlite":
        with sqlite3.connect("file:" + quote(str(path)) + "?mode=ro&immutable=1", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            tables = [
                str(r[0])
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND n"
                    "ame NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            result: list[tuple[str, str]] = []
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                cursor = db.execute("SELECT * FROM " + quoted)
                names = [column[0] for column in cursor.description or ()]
                for index, row in enumerate(cursor, 1):
                    values = {
                        key: {"binary_sha256": sha(value)} if isinstance(value, bytes) else value
                        for key, value in zip(names, row, strict=True)
                    }
                    result.append(
                        (
                            f"table {table} record {index}",
                            json.dumps(values, ensure_ascii=False, default=str),
                        )
                    )
            return result
    text = path.read_text(encoding="utf-8-sig")
    if fmt == "json":
        value = cast(object, json.loads(text))
        if isinstance(value, list):
            return [
                (f"record [{i}]", json.dumps(item, ensure_ascii=False))
                for i, item in enumerate(cast(list[object], value))
            ]
        if isinstance(value, dict):
            mapping = cast(dict[str, object], value)
            # A singular object is one semantic record; wrappers containing arrays
            # yield records while preserving their wrapper key in the location.
            if len(mapping) == 1 and isinstance(next(iter(mapping.values())), list):
                key, items = next(iter(mapping.items()))
                return [
                    (f"record {key}[{i}]", json.dumps(item, ensure_ascii=False))
                    for i, item in enumerate(cast(list[object], items))
                ]
        return [("record $", text)]
    if fmt == "jsonl":
        return [
            (f"line {i} record", line)
            for i, line in enumerate(text.splitlines(), 1)
            if line.strip()
        ]
    if fmt == "csv":
        reader = csv.DictReader(io.StringIO(text))
        result = []
        for row in reader:
            result.append((f"line {reader.line_num} row", json.dumps(row, ensure_ascii=False)))
        return result
    result = []
    lines = text.splitlines(keepends=True)
    start = 1
    current: list[str] = []
    heading = ""
    for number, line in enumerate(lines, 1):
        if not line.strip() or (fmt == "md" and re.match(r"^#{1,6}\s", line)):
            if current:
                result.append((f"lines {start}-{number - 1}", heading + "".join(current).strip()))
                current = []
            start = number + 1
            if fmt == "md" and re.match(r"^#{1,6}\s", line):
                heading = line.strip() + "\n"
            continue
        if not current:
            start = number
        current.append(line)
    if current:
        result.append((f"lines {start}-{len(lines)}", heading + "".join(current).strip()))
    return result


def snapshot(source: Path, data_dir: Path) -> Snapshot:
    """Read originals only to create/identify a snapshot, then parse snapshots only.

    SQLite sources are read with mode=ro and backed up through SQLite, so WAL
    records present at the snapshot transaction are included without mutating SQL.
    """
    source = source.absolute()
    if not source.exists():
        raise FileNotFoundError(source)
    if source == data_dir or source in data_dir.parents or data_dir in source.parents:
        raise ValueError("Archive and data directory must not overlap")
    stage = data_dir / "snapshots" / ("stage-" + os.urandom(8).hex())
    _private(stage)
    paths = [source] if not source.is_dir() else sorted(source.rglob("*"))
    files: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for path in paths:
        if path.is_dir():
            continue
        relative = path.name if source.is_file() else str(path.relative_to(source))
        try:
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("non-regular or symlink source")
            raw = path.read_bytes()
            suffix = path.suffix.lower()
            fmt = (
                "sqlite"
                if suffix in {".sqlite", ".sqlite3", ".db"}
                or raw.startswith(b"SQLite format 3\x00")
                else suffix.lstrip(".")
            )
            if fmt not in {"sqlite", "md", "txt", "json", "jsonl", "csv"}:
                fmt = "text"
            target = stage / relative
            _private(target.parent)
            if fmt == "sqlite":
                with (
                    sqlite3.connect("file:" + quote(str(path)) + "?mode=ro", uri=True) as origin,
                    sqlite3.connect(target) as copy,
                ):
                    origin.execute("PRAGMA query_only=ON")
                    origin.backup(copy)
            else:
                target.write_bytes(raw)
            copied = target.read_bytes()
            os.chmod(target, 0o400)
            files.append(
                {
                    "source_path": str(path),
                    "relative": relative,
                    "sha256": sha(copied),
                    "original_sha256": sha(raw),
                    "format": fmt,
                }
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            skipped.append({"source_path": str(path), "error": type(exc).__name__})
    identity = sha(
        json.dumps(
            {
                "files": [{k: f[k] for k in ("relative", "sha256", "format")} for f in files],
                "skipped": skipped,
            },
            sort_keys=True,
        ).encode()
    )
    final = stage.parent / identity
    if final.exists():
        import shutil

        shutil.rmtree(stage)
    else:
        stage.rename(final)
    chunks: list[Chunk] = []
    for metadata in files:
        path = final / metadata["relative"]
        if sha(path.read_bytes()) != metadata["sha256"]:
            raise ValueError("Snapshot digest mismatch")
        metadata["snapshot_path"] = str(path)
        try:
            records = _records(path, metadata["format"])
            for ref, record in records:
                for index, piece in enumerate(_pieces(record)):
                    location = ref + (f" segment {index + 1}" if len(record) > 6000 else "")
                    chunk_id = sha(
                        (
                            identity + "\0" + metadata["relative"] + "\0" + location + "\0" + piece
                        ).encode()
                    )
                    chunks.append(
                        Chunk(
                            chunk_id,
                            str(path),
                            location,
                            metadata["format"],
                            piece,
                            metadata["source_path"],
                        )
                    )
            metadata["status"] = "read"
        except (OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
            metadata["status"] = "skipped"
            skipped.append({"source_path": metadata["source_path"], "error": type(exc).__name__})
    return Snapshot(identity, final, files, skipped, chunks)
