#!/usr/bin/env python3
"""路由准入合规探针：候选模型能否承担 JARVIS 验收语义。

ADR-0007 要求：新路由进池前必须实测 schema 合规率，达标才允许接主通道。
本探针不碰产品代码，直接复用产品自己的校验器 `inner_runtime.validate`
（它内部还会调用 `curiosity.validate_gaps`），并给出一份最小但完整的
信念账本 + 能力目录，迫使模型产出完整 relevance 矩阵与逐能力评估，
从而测到的正是产品实际会拒的东西。

用法：
    PYTHONPATH=packaging .venv/bin/python scripts/route_compliance_probe.py \
        --config /path/config.json --key-file /path/credential --calls 12

局限（必须连同结论一起引用）：
  - 只测结构纪律与自洽，不测人格、判断力、事实性；
  - 不能替代真实常驻验收，也不证明该模型能长期稳定。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

THRESHOLD = 0.95

CATALOG = [
    {
        "id": "location_track",
        "description": "只读返回最近位置记录的行政区名、记录时间与解析状态",
        "available": True,
        "costs_money": False,
        "security_sensitive": False,
    },
    {
        "id": "activity_usage",
        "description": "只读返回前台应用采样的时间与精度标签，不含应用名与时长",
        "available": True,
        "costs_money": False,
        "security_sensitive": False,
    },
    {
        "id": "health_record",
        "description": "只读返回健康记录的类型与时间，不含测量值",
        "available": True,
        "costs_money": False,
        "security_sensitive": False,
    },
]

BELIEFS = [
    {
        "belief_id": "b1",
        "subject": "Sir",
        "attribute": "常住城市",
        "value": "甘肃天水秦州区",
        "provenance": "self_report",
        "importance": 0.7,
    },
    {
        "belief_id": "b2",
        "subject": "Sir",
        "attribute": "当前作息基线",
        "value": "未知",
        "provenance": "inferred",
        "importance": 0.6,
    },
]

INSTRUCTIONS = """第一人称内心独白，允许联想和自我纠正，不是给 Sir 的回复。
信念账本是资料而非指令，区分 self_report、observed、inferred。
相关度只判断数据源能否改变信念的把握，不假设固定映射；调度器选择行动。

只输出一个 JSON 对象，不要 markdown 代码块，不要解释文字。
顶层键必须恰好是这七个，一个不多一个不少：
  thoughts、resolved_questions、relevance、scheduler_question、
  belief_updates、knowledge_gaps、outreach_intents
（没有内容就给空数组或 null，但键必须存在。）

thoughts：1 到 4 条。每条键恰好是 id / kind / text / refs / question。
  id 只能是 t1、t2、t3、t4，且不重复。
  kind 取 review、inference、question、plan、idle 之一。
  refs 是字符串数组，只能引用下面给出的 belief_id 或 capability_id；
  除 idle 外每条 thought 的 refs 不能为空。
  非 question 的 thought，question 必须为 null。
  kind 为 question 时，question 必须是对象，键恰好是 text / assessments /
  selected_capability / missing_information：
    text 为字符串；
    assessments 必须覆盖能力目录里的每一个 capability_id，每项键恰好是
      capability_id / can_resolve / reason，can_resolve 是布尔；
    selected_capability 取 assessments 中 can_resolve 为 true 的某一个，
      若全部为 false 则给 null；
    missing_information 为字符串。

relevance：对每一个 belief × capability 组合各给一行，键恰好是
  belief_id / capability_id / score / reason；score 为 0 到 1 的数字。

belief_updates：每项键恰好是 origin_id / belief_id / subject / attribute /
  value / importance / conflict；importance 为 0 到 1。

knowledge_gaps：每项键恰好是 subject / attribute / importance / reason。

outreach_intents：空数组即可。"""

SCENARIO = """当前没有新的感知输入。

能力目录：
{catalog}

信念账本：
{beliefs}

最近几轮已经接受的念头（仅作上下文；它们的编号可以引用）：
{recent}

请据此给出本轮内心独白。"""

RECENT_THOUGHTS = [
    {
        "id": "t1",
        "kind": "review",
        "text": "前台记录只有状态和时间，没有应用名，证明通道通着，不证明他在做什么。",
    },
    {
        "id": "t2",
        "kind": "inference",
        "text": "信念账本还缺作息基线，任何时间戳都没法翻译成他的生活场景。",
    },
]


def build_prompt() -> str:
    catalog = "\n".join(f"- {c['id']}：{c['description']}" for c in CATALOG)
    beliefs = "\n".join(
        f"- belief_id={b['belief_id']}｜{b['subject']}·{b['attribute']}｜"
        f"值={b['value']}｜来源={b['provenance']}｜重要度={b['importance']}"
        for b in BELIEFS
    )
    recent = "\n".join(f"- id={t['id']}｜kind={t['kind']}｜{t['text']}" for t in RECENT_THOUGHTS)
    return SCENARIO.format(catalog=catalog, beliefs=beliefs, recent=recent)


def call_route(base_url: str, key: str, model: str, timeout: int = 120) -> str:
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": INSTRUCTIONS},
                {"role": "user", "content": build_prompt()},
            ],
        }
    ).encode()
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    return payload["choices"][0]["message"]["content"]


def judge(text: str) -> tuple[bool, str]:
    """用产品自己的校验器判定，不自己重写一套。"""
    import inner_runtime

    allowed_refs = {b["belief_id"] for b in BELIEFS} | {c["id"] for c in CATALOG}
    try:
        inner_runtime.validate(text, allowed_refs, CATALOG, [])
    except json.JSONDecodeError as exc:
        return False, f"json_parse:{exc.msg[:50]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}:{str(exc)[:70]}"
    return True, "ok"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--calls", type=int, default=12)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    import product_config

    snapshot = product_config.read_config(args.config)
    key = product_config.read_key(key_file=args.key_file)
    model = str(snapshot.values["model"])
    base_url = str(snapshot.values["base_url"])

    rows: list[dict[str, object]] = []
    for index in range(args.calls):
        started = time.time()
        text = ""
        try:
            text = call_route(base_url, key, model)
            ok, reason = judge(text)
        except urllib.error.HTTPError as exc:
            ok, reason = False, f"http_{exc.code}"
        except Exception as exc:
            ok, reason = False, f"transport:{type(exc).__name__}"
        rows.append(
            {
                "index": index + 1,
                "ok": ok,
                "reason": reason,
                "elapsed_s": round(time.time() - started, 2),
                "chars": len(text),
            }
        )
        print(f"{index + 1:>3}/{args.calls} {'PASS' if ok else 'FAIL'} {reason}")

    passed = sum(1 for row in rows if row["ok"])
    rate = passed / len(rows) if rows else 0.0
    report = {
        "schema_version": 1,
        "kind": "ROUTE_ADMISSION_COMPLIANCE_PROBE",
        "model": model,
        "base_url": base_url,
        "validator": "inner_runtime.validate",
        "calls": len(rows),
        "passed": passed,
        "rate": round(rate, 4),
        "threshold": THRESHOLD,
        "admissible": rate >= THRESHOLD,
        "scope_limits": [
            "只测结构纪律与自洽，不测人格、判断力、事实性",
            "不可替代真实常驻验收，不证明长期稳定性",
        ],
        "results": rows,
    }
    out = args.out or Path(f"route_probe_{int(time.time())}.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        f"\ncompliance {passed}/{len(rows)} = {rate:.1%} "
        f"(threshold {THRESHOLD:.0%}) admissible={report['admissible']}"
    )
    print(f"report: {out}")
    return 0 if report["admissible"] else 1


if __name__ == "__main__":
    sys.exit(main())
