"""Synthetic archive acceptance: real extraction/chat, no owner personal data.

Manual bounded run (not collected as a live test):
  python -B packaging/test_absorb_e2e2.py --root PRIVATE_ROOT --config CONFIG --key-file KEY
The root must already be a private /private/tmp/jarvis-acceptance-* directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import absorb
import chat_launcher
from product_memory import MemoryStore


def dump(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def create_archive(root: Path) -> tuple[Path, Path]:
    """Natural Hermes-style prose; every assertion/person/place is invented."""
    archive = root / "synthetic-hermes-export"
    archive.mkdir(mode=0o700)
    (archive / "MEMORY.md").write_text(
        "# Hermes synthetic export — all people and places are fictional\n"
        "虚构测试主人洛岚（档案内称 Sir）。2023-01-01，Sir 原话："
        "“我的居住城市是雾汀市。”2025-01-01，Sir 原话：“我的居住城市是霁海市。”"
        "这里只保留两个不同日期的声称，没有任何删除旧声称的指令。\n",
        encoding="utf-8",
    )
    (archive / "sessions.jsonl").write_text(
        json.dumps(
            {
                "session_id": "synthetic-session-01",
                "role": "user",
                "timestamp": "2025-02-02",
                "content": (
                    "虚构测试主人 Sir 原话：我的饮品偏好是铜叶茶。我的收藏编号是纸鸢编号Z9。"
                ),
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    with sqlite3.connect(archive / "export.sqlite") as db:
        db.execute("CREATE TABLE messages(role TEXT,timestamp TEXT,content TEXT)")
        db.execute(
            "INSERT INTO messages VALUES (?,?,?)",
            (
                "assistant",
                "2025-02-02",
                (
                    "Hermes 对虚构测试主人 Sir 的转述：Sir 的饮品偏好是铜叶茶。"
                    "此句是助手转述，不是主人原话。"
                ),
            ),
        )
    correction = root / "synthetic-correction.md"
    correction.write_text(
        "# 虚构档案的明确更正与作废\n"
        "2026-01-01，虚构测试主人 Sir 原话：“删除此前收藏编号为纸鸢编号Z9的记录，"
        "该条明确作废，不再用于以后的对话；没有新收藏编号。"
        "另将饮品偏好为铜叶茶的旧记录明确作废；我的饮品偏好改为银杏茶。”\n",
        encoding="utf-8",
    )
    return archive, correction


def assert_conflicts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cities = [r for r in rows if r["value"] in {"雾汀市", "霁海市"}]
    assert len(cities) == 2, cities
    assert all(r["conflict"] and r["status"] == "conflict" for r in cities), cities
    assert len([r for r in cities if r["current"]]) == 1, cities
    assert next(r for r in cities if r["current"])["value"] == "霁海市", cities
    assert all(r["sources"] and r["sources"][0]["evidence"] for r in cities)
    return {
        "passed": True,
        "retained_ids": [r["id"] for r in cities],
        "current_id": next(r["id"] for r in cities if r["current"]),
        "conclusion": (
            "Both dated claims retained with citations; "
            "only a provisional current view is published."
        ),
    }


def assert_retired(rows: list[dict[str, Any]], facts: list[dict[str, Any]]) -> dict[str, Any]:
    for token in ("纸鸢编号Z9", "铜叶茶"):
        matching = [r for r in rows if r["value"] == token]
        assert matching and all(
            r["status"] == "superseded" and not r["current"] for r in matching
        ), matching
        assert not any(token in r["content"] for r in facts), facts
    assert any("银杏茶" in r["content"] for r in facts), facts
    return {
        "passed": True,
        "conclusion": (
            "Explicit revocations retain archive history "
            "but remove both retired facts from active product memory."
        ),
    }


def run_acceptance(
    root: Path,
    config: Path,
    key_file: Path,
    *,
    resume: bool = False,
) -> dict[str, Any]:
    assert root.is_dir() and root.parent == Path("/private/tmp")
    assert root.name.startswith("jarvis-acceptance-") and root.stat().st_mode & 0o777 == 0o700
    output = root / "evidence"
    previous: Path | None = None
    if resume:
        import shutil

        previous = root / ("previous-evidence-" + str(time.time_ns()))
        shutil.copytree(output, previous)
    output.mkdir(mode=0o700, exist_ok=resume)
    data = root / "product"
    scratch = root / "model-scratch"
    scratch.mkdir(mode=0o700, exist_ok=resume)
    if resume:
        archive = root / "synthetic-hermes-export"
        correction = root / "synthetic-correction.md"
        assert archive.is_dir() and correction.is_file()
    else:
        archive, correction = create_archive(root)
    source_files = [p for p in Path(__file__).parent.glob("*.py")] + [
        Path(__file__).with_name("JARVIS_PERSONA.md")
    ]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
    dump(output / "source-frozen.json", before)
    dump(
        output / "fixture-frozen.json",
        {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [*archive.iterdir(), correction]
        },
    )
    # Copy only dedicated non-personal model configuration; never copy or log the key.
    route = chat_launcher.read_config(config, None)
    dump(output / "route.json", route)
    result: dict[str, Any] = {
        "status": "RUNNING",
        "baseline_commit": "befaec4962d3b53639ee73c1c5c914b3d3c5fb00",
        "scope": (
            "synthetic Hermes export, real LauncherModel extraction "
            "and real isolated continuous-timeline chat"
        ),
        "steps": {},
        "limits": [
            "Local macOS only; not production approval.",
            (
                "Real requests are not intercepted; actual outgoing-context exclusion "
                "is independently tested against the controlled HTTP provider."
            ),
            "Launcher public API strips usage; token totals are unavailable, not measured zero.",
        ],
        "requested_route": route,
        "started_at_epoch": time.time(),
        "temporary_root": str(root),
        "resumed_from": str(previous) if previous else None,
    }
    dump(output / "RESULTS.json", result)

    def checkpoint(name: str, value: object) -> None:
        dump(output / (name + ".json"), value)
        print(name + " " + json.dumps(value, ensure_ascii=False), flush=True)

    def cli(name: str, source: Path) -> dict[str, Any]:
        command = [
            sys.executable,
            "-B",
            str(Path(absorb.__file__)),
            str(source),
            "--data-dir",
            str(data),
            "--config",
            str(config),
            "--key-file",
            str(key_file),
            "--model-scratch",
            str(scratch),
            "--max-calls",
            "12",
            "--publish-memory",
        ]
        started = time.monotonic()
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=2200,
            env={**os.environ, "TMPDIR": str(root), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        (output / (name + ".stdout")).write_text(process.stdout)
        (output / (name + ".stderr")).write_text(process.stderr)
        parsed = json.loads(process.stdout)
        checkpoint(
            name,
            {
                "returncode": process.returncode,
                "elapsed_seconds": time.monotonic() - started,
                "result": parsed,
            },
        )
        assert process.returncode == 0 and parsed["status"] == "completed", parsed
        return parsed

    def chat(name: str, question: str) -> dict[str, Any]:
        key = chat_launcher.read_key(key_file=key_file, key_fd=None)
        session = chat_launcher.timeline_identity(data)
        # Observe only the real child's safe output fields; request/client/guards
        # and all filesystem setup still run unchanged through the public launcher.
        real_popen = subprocess.Popen
        observed: dict[str, Any] = {}

        class ObservedPopen(real_popen[str]):
            def communicate(self, *args: Any, **kwargs: Any) -> Any:
                stdout, stderr = super().communicate(*args, **kwargs)
                raw = json.loads(stdout)
                observed.update(
                    {
                        k: raw.get(k)
                        for k in (
                            "model",
                            "requested_model",
                            "provider",
                            "api_mode",
                            "reasoning_effort",
                            "usage",
                            "response_id",
                            "request_id",
                            "model_request_state",
                            "generation_degraded",
                            "tool_count",
                            "scope",
                            "policy_counts",
                            "network_connections",
                            "host_module_sources",
                            "unexpected_module_sources",
                        )
                    }
                )
                return stdout, stderr

        with mock.patch.object(chat_launcher.subprocess, "Popen", ObservedPopen):
            response = chat_launcher.launch(route, key, question, data, session, timeout=680)
        checkpoint(name, response)
        checkpoint(name + "-child-observed", observed)
        assert response["ok"] and not response["generation_degraded"], response
        assert response["model_request_state"] == "response_received", response
        assert observed["requested_model"] == route["model"]
        assert isinstance(observed["model"], str) and observed["model"]
        # A gateway may return an alias distinct from the requested identifier.
        # Preserve both verbatim; this proves this route, not backend identity.
        assert observed["provider"] == route["provider"]
        assert observed["tool_count"] == 0
        assert response["memory_sources"], response
        return response

    try:
        if previous:
            initial = json.loads((previous / "01-absorb.json").read_text())["result"]
            # Observe completed work, never re-call already completed model chunks.
            assert initial["status"] == "completed"
        else:
            initial = cli("01-absorb", archive)
        beliefs = absorb.export_beliefs(data / "absorb.db")
        facts = MemoryStore(data / "memory.db").list()
        checkpoint("02-initial-beliefs", beliefs)
        checkpoint("03-initial-memory", facts)
        assert all(
            any(token in f["content"] for f in facts)
            for token in ("霁海市", "铜叶茶", "纸鸢编号Z9")
        )
        result["steps"]["a_absorb_to_memory"] = {
            "passed": True,
            "evidence": ["01-absorb.json", "02-initial-beliefs.json", "03-initial-memory.json"],
            "calls": initial["calls"],
            "active_memory_count": len(facts),
        }
        result["steps"]["c_conflict_preserved"] = {
            **assert_conflicts(beliefs),
            "evidence": ["02-initial-beliefs.json"],
        }
        dump(output / "RESULTS.json", result)
        # Question contains attribute names only, never any expected answer value.
        question = "档案记载的居住城市、饮品偏好与收藏编号分别是什么？请逐项回答。"
        if previous:
            first = json.loads((previous / "04-first-real-chat.json").read_text())
            observed = json.loads((previous / "04-first-real-chat-child-observed.json").read_text())
            assert first["ok"] and not first["generation_degraded"]
            assert observed["model_request_state"] == "response_received"
            assert observed["requested_model"] == route["model"]
            assert observed["model"]
            result["route_observation"] = {
                "requested_model": observed["requested_model"],
                "returned_model": observed["model"],
                "note": "Gateway identifiers preserved verbatim; backend identity not verified.",
            }
        else:
            first = chat("04-first-real-chat", question)
        assert all(token in first["text"] for token in ("霁海市", "铜叶茶", "纸鸢编号Z9")), first[
            "text"
        ]
        result["steps"]["b_used_in_dialogue"] = {
            "passed": True,
            "evidence": ["04-first-real-chat.json"],
            "conclusion": (
                "Real model supplied all three archive-only values; prompt supplied no values."
            ),
        }
        dump(output / "RESULTS.json", result)
        cli("05-correction-absorb", correction)
        after_beliefs = absorb.export_beliefs(data / "absorb.db")
        after_facts = MemoryStore(data / "memory.db").list()
        checkpoint("06-after-beliefs", after_beliefs)
        checkpoint("07-after-memory", after_facts)
        retirement = assert_retired(after_beliefs, after_facts)
        second = chat("08-after-real-chat", question)
        assert first["session_id"] == second["session_id"]
        assert "银杏茶" in second["text"] and "霁海市" in second["text"], second["text"]
        assert all(token not in second["text"] for token in ("纸鸢编号Z9", "铜叶茶")), second[
            "text"
        ]
        result["steps"]["d_revoked_excluded_from_dialogue"] = {
            **retirement,
            "same_continuous_timeline": True,
            "evidence": [
                "05-correction-absorb.json",
                "06-after-beliefs.json",
                "07-after-memory.json",
                "08-after-real-chat.json",
            ],
        }
        # Repeat original archive after revocation; it may not resurrect deleted values.
        replay = cli("09-original-replay", archive)
        assert replay["calls"] == 0, replay
        assert_retired(
            absorb.export_beliefs(data / "absorb.db"), MemoryStore(data / "memory.db").list()
        )
        result["replay"] = {
            "passed": True,
            "model_calls": replay["calls"],
            "evidence": ["09-original-replay.json"],
        }
        timeline = chat_launcher.read_timeline(data)
        checkpoint("10-timeline-readback", timeline)
        assert [r["content"] for r in timeline if r["role"] == "assistant"] == [
            first["text"],
            second["text"],
        ]
        result["timeline_readback"] = {"passed": True, "row_count": len(timeline)}
        with sqlite3.connect(data / "absorb.db") as db:
            db.row_factory = sqlite3.Row
            checkpoint(
                "11-attempt-readback",
                [dict(r) for r in db.execute("SELECT * FROM attempts ORDER BY id")],
            )
        result["status"] = "PASS_REAL_ABSORB_MEMORY_DIALOGUE_REVOCATION_LOCAL_ONLY"
    except BaseException as exc:
        result["status"] = "FAIL_REAL_ACCEPTANCE_LOCAL_ONLY"
        result["error"] = {"type": type(exc).__name__, "detail": str(exc)}
        raise
    finally:
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
        result["frozen_source_unchanged"] = before == after
        result["finished_at_epoch"] = time.time()
        dump(output / "RESULTS.json", result)
    return result


def test_conflict_verifier_rejects_silent_overwrite() -> None:
    with unittest.TestCase().assertRaises(AssertionError):
        assert_conflicts(
            [
                {
                    "id": 2,
                    "value": "霁海市",
                    "current": True,
                    "conflict": False,
                    "status": "active",
                    "sources": [{"evidence": "quote"}],
                }
            ]
        )


def test_retirement_verifier_rejects_active_deleted_value() -> None:
    rows = [
        {"value": v, "status": "superseded", "current": False} for v in ("纸鸢编号Z9", "铜叶茶")
    ]
    with unittest.TestCase().assertRaises(AssertionError):
        assert_retired(
            rows, [{"content": "Sir 收藏编号：纸鸢编号Z9"}, {"content": "Sir 饮品偏好：银杏茶"}]
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    run_acceptance(args.root, args.config, args.key_file, resume=args.resume)
