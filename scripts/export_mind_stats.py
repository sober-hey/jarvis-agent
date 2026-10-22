#!/usr/bin/env python3
"""常驻运行停机前导出 mind.db 统计。

为什么必须在停机前跑：`packaging/soak/run_soak.py` 停止时会删掉整个临时
数据根，`mind.db` / `daemon.db` 一并消失，事后无法回查"哪些响应被拒、
被拒的响应长什么样"。这条数据丢失过一次，导致一个缺陷只能降级成假设。

用法（停机前）：
    .venv/bin/python scripts/export_mind_stats.py \
        --launch .runs_01/<run>/LAUNCH.json \
        --out .runs_01/<run>/mind-stats.json

加 --replay：离线调用当前 packaging/inner_runtime.validate，全部拒绝样本（含非 JSON）
写入 rejected_samples[].replay，保存 status、exception_type、exception_message。
缺失历史上下文时 status=context_unavailable，不用空上下文猜测。
注意：只回放纯 validate；真实 validation_error 也可能来自其后的矩阵/信念等阶段，
所以 passed 只表示本次 validate 未复现，不能判定原始运行接受或根因已解决。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

LOCAL_THOUGHT_IDS = {"t1", "t2", "t3", "t4"}


def payloads(connection: sqlite3.Connection, category: str) -> list[dict]:
    rows = connection.execute(
        "SELECT payload FROM entries WHERE category=? ORDER BY seq", (category,)
    ).fetchall()
    out: list[dict] = []
    for (raw,) in rows:
        try:
            value = json.loads(raw)
        except Exception:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def replay_response(connection: sqlite3.Connection, response: dict) -> dict:
    """Replay only the pure envelope validator, using this call's persisted inputs."""
    row = connection.execute(
        "SELECT payload FROM entries WHERE category='model_call' AND id=?",
        (response.get("call_id"),),
    ).fetchone()
    if row is None:
        return {"status": "context_unavailable", "reason": "matching model_call not found"}
    try:
        call = json.loads(row[0])
        if not isinstance(call, dict) or not isinstance(call.get("context"), dict):
            raise ValueError("model_call.context must be an object")
        context = call["context"]
        # The live validator's fourth argument is unresolved questions, NOT memories.
        # Memories contribute memory:<id> provenance refs through outreach_refs instead.
        catalog = context["catalog"] if "catalog" in context else context["capabilities"]
        questions = context["unresolved_questions"]
        if not isinstance(catalog, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in catalog
        ):
            raise ValueError("catalog/capabilities must be a list of objects with string ids")
        if not isinstance(questions, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("question_id"), str)
            for item in questions
        ):
            raise ValueError("unresolved_questions must be a list of objects with question_id")
        if "allowed_refs" in context:
            refs = context["allowed_refs"]
            if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                raise ValueError("allowed_refs must be a list of strings")
            allowed = set(refs)
        else:
            # Reconstruct exactly the same input as InnerLoop.step, never substituting
            # empty lists for missing historical context or consulting current state.
            for key, identity in (
                ("events", "id"),
                ("thoughts", "id"),
                ("memories", "id"),
                ("beliefs", "belief_id"),
                ("recent_thoughts", "id"),
                ("recalled", "id"),
                ("pending_observations", "id"),
                ("relevance_candidates", "belief_id"),
                ("pending_memories", "origin_id"),
            ):
                items = context[key]
                if not isinstance(items, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get(identity), str)
                    for item in items
                ):
                    raise ValueError(f"{key} must be a list of objects with string {identity}")
            allowed = None
    except (ValueError, KeyError, TypeError) as exc:
        return {"status": "context_unavailable", "reason": str(exc)}

    # Equivalent to PYTHONPATH=packaging, independent of the caller's working directory.
    # Disable bytecode writes: packaging is frozen during a resident run.
    sys.dont_write_bytecode = True
    packaging = str(Path(__file__).resolve().parents[1] / "packaging")
    if packaging not in sys.path:
        sys.path.insert(0, packaging)
    import inner_runtime

    if allowed is None:
        allowed = inner_runtime.InnerLoop.outreach_refs(context)
    try:
        inner_runtime.validate(response.get("text"), allowed, catalog, questions)
    except Exception as exc:
        return {
            "status": "rejected",
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
        }
    return {"status": "passed"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--replay", action="store_true", help="Replay rejected responses offline")
    args = parser.parse_args()

    launch = json.loads(args.launch.read_text())
    root = Path(str(launch["runtime_data_root"]))
    mind = root / "mind.db"
    if not mind.exists():
        print(f"mind.db not found at {mind} — run already cleaned up")
        return 1

    connection = sqlite3.connect(f"file:{mind}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    if args.replay:
        connection.execute("BEGIN")  # One consistent read snapshot while the daemon runs.

    responses = payloads(connection, "model_response")
    rejected_ids = {str(p.get("call_id")) for p in payloads(connection, "validation_error")}

    rejected_local = rejected_plain = accepted_local = accepted_plain = 0
    samples: list[dict] = []
    for response in responses:
        call_id = str(response.get("call_id"))
        text = response.get("text") or ""
        try:
            parsed = json.loads(text)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        if args.replay:
            # Legacy statistics skipped unparseable JSON; replay must also retain
            # malformed envelopes without the summary walk crashing first.
            parsed = dict(parsed)
            thoughts = parsed.get("thoughts")
            parsed["thoughts"] = thoughts if isinstance(thoughts, list) else []
        local_refs: list[str] = []
        for thought in parsed.get("thoughts") or []:
            if not isinstance(thought, dict):
                continue
            refs = thought.get("refs") or []
            if args.replay and not isinstance(refs, list):
                refs = []
            for ref in refs:
                if (not args.replay or isinstance(ref, str)) and ref in LOCAL_THOUGHT_IDS:
                    local_refs.append(f"{thought.get('id')}->{ref}")
        has_local = bool(local_refs)
        rejected = call_id in rejected_ids
        if rejected and has_local:
            rejected_local += 1
        elif rejected:
            rejected_plain += 1
        elif has_local:
            accepted_local += 1
        else:
            accepted_plain += 1
        if rejected and len(samples) < 5:
            samples.append(
                {
                    "call_id": call_id[:12],
                    "chars": len(text),
                    "local_refs": local_refs[:6],
                    "thought_kinds": [
                        t.get("kind") for t in (parsed.get("thoughts") or []) if isinstance(t, dict)
                    ],
                }
            )

    counts = {
        category: connection.execute(
            "SELECT COUNT(*) FROM entries WHERE category=?", (category,)
        ).fetchone()[0]
        for category in (
            "model_call",
            "model_response",
            "validation_error",
            "thought",
            "action",
            "knowledge_gap",
        )
    }
    if args.replay:
        samples = []
        for response in responses:
            if str(response.get("call_id")) not in rejected_ids:
                continue
            text = response.get("text") or ""
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                parsed = None
            thoughts = parsed.get("thoughts") if isinstance(parsed, dict) else None
            thoughts = thoughts if isinstance(thoughts, list) else []
            samples.append(
                {
                    "call_id": str(response.get("call_id")),
                    "chars": len(text),
                    "local_refs": [
                        f"{thought.get('id')}->{ref}"
                        for thought in thoughts
                        if isinstance(thought, dict) and isinstance(thought.get("refs"), list)
                        for ref in thought["refs"]
                        if isinstance(ref, str) and ref in LOCAL_THOUGHT_IDS
                    ][:6],
                    "thought_kinds": [t.get("kind") for t in thoughts if isinstance(t, dict)],
                    "replay": replay_response(connection, response),
                }
            )
    connection.close()

    report = {
        "schema_version": 1,
        "run": str(args.launch.parent),
        "data_root": str(root),
        "counts": counts,
        "reject_rate": (round(len(rejected_ids) / len(responses), 4) if responses else None),
        "local_ref_breakdown": {
            "rejected_with_local_refs": rejected_local,
            "rejected_without": rejected_plain,
            "accepted_with_local_refs": accepted_local,
            "accepted_without": accepted_plain,
        },
        "rejected_samples": samples,
        "note": "必须在 run_soak 停机前导出；停机后临时数据根被删除。",
    }
    if args.replay:
        report["replay_scope"] = (
            "Current packaging/inner_runtime.validate only; passed means rejection was not "
            "reproduced here, not that the original response passed later validation stages."
        )
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["counts"], ensure_ascii=False))
    print(f"reject_rate={report['reject_rate']} local_refs={report['local_ref_breakdown']}")
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
