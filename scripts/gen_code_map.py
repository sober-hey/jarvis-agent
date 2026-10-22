#!/usr/bin/env python3
"""从源码生成模块地图（docs/CODE_MAP.md）。

用途：接手或加新功能前，先看这张表知道"哪个文件负责什么"，不用逐个打开。
只读源码，不导入、不执行；docstring 取第一行，缺失时按文件名推断。
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "CODE_MAP.md"


def _prefixes(*names: str) -> Callable[[Path], bool]:
    """按文件名前缀归类，比一长串 lambda 好读也好改。"""
    return lambda p: p.name.startswith(names)


SECTIONS: list[tuple[str, str, Callable[[Path], bool]]] = [
    # 语音子系统已隔离到仓库根 voice/（见 voice/README.md）。
    ("语音子系统（voice/，已隔离）", "voice", lambda p: True),
    ("语音相关设备接入（仍在 packaging/）", "packaging", _prefixes("device_", "avatar_")),
    (
        "守护进程与认知循环",
        "packaging",
        _prefixes("inner_", "daemon_", "mind", "curiosity", "reflect"),
    ),
    (
        "模型调用与路由",
        "packaging",
        _prefixes("chat_", "config_", "engine_", "model_", "route", "provider"),
    ),
    (
        "记忆与知识",
        "packaging",
        _prefixes("knowledge", "memory", "recall", "kg_", "citation", "sir_"),
    ),
    (
        "健康、生活与外部数据",
        "packaging",
        _prefixes("health_", "lifelog", "senses", "activity", "browser_", "device_"),
    ),
    (
        "运维、安全与工具",
        "packaging",
        _prefixes(
            "supervisor",
            "product_home",
            "approval",
            "budget",
            "backup",
            "evidence",
            "soak",
            "web_server",
            "tool_",
            "search",
            "deploy",
        ),
    ),
]


def docline(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text("utf-8"))
    except (OSError, SyntaxError):
        return "（无法解析）"
    doc = ast.get_docstring(tree) or ""
    first = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return first or "（无模块说明）"


def loc(path: Path) -> int:
    try:
        return sum(1 for _ in path.open("rb"))
    except OSError:
        return 0


def collect(folder: str, predicate) -> list[tuple[str, str, int]]:
    rows = []
    for path in sorted((ROOT / folder).glob("*.py")):
        if path.name.startswith("test_") or not predicate(path):
            continue
        rows.append((path.name, docline(path), loc(path)))
    return rows


def render() -> str:
    lines = [
        "# 代码地图",
        "",
        "由 `scripts/gen_code_map.py` 从源码自动生成，不要手改。",
        "每一行是「文件 / 职责（模块 docstring 首行）/ 行数」。",
        "",
    ]
    used: set[str] = set()
    for title, folder, predicate in SECTIONS:
        rows = collect(folder, predicate)
        if not rows:
            continue
        used.update(r[0] for r in rows)
        lines += [f"## {title}", "", "| 文件 | 职责 | 行数 |", "|---|---|---|"]
        for name, doc, count in rows:
            lines.append(f"| `{folder}/{name}` | {doc} | {count} |")
        lines.append("")

    rest = [
        (p.name, docline(p), loc(p))
        for p in sorted((ROOT / "packaging").glob("*.py"))
        if not p.name.startswith("test_") and p.name not in used
    ]
    if rest:
        lines += ["## 其它运行时模块", "", "| 文件 | 职责 | 行数 |", "|---|---|---|"]
        for name, doc, count in rest:
            lines.append(f"| `packaging/{name}` | {doc} | {count} |")
        lines.append("")

    src = ROOT / "src" / "jarvis"
    if src.is_dir():
        lines += [
            "## 内核层（K0–K4 草案，未全部接线）",
            "",
            "| 文件 | 职责 | 行数 |",
            "|---|---|---|",
        ]
        for path in sorted(src.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(ROOT)
            lines.append(f"| `{rel}` | {docline(path)} | {loc(path)} |")
        lines.append("")

    apps = ROOT / "apps"
    if apps.is_dir():
        lines += ["## 客户端", "", "| 位置 | 说明 |", "|---|---|"]
        for name, note in (
            ("mac", "macOS 菜单栏 App（SwiftUI + AVAudioEngine；UtteranceGate 负责一句话切分）"),
            ("ios", "iPhone App（球体界面；SpeechGate + 引擎内播放）"),
            ("watchos", "Apple Watch App（球体、令牌导入）"),
            ("wakeword", "离线唤醒词模型训练与推理"),
            ("web", "本机 Web 面板（仅 loopback）"),
        ):
            if (apps / name).is_dir():
                lines.append(f"| `apps/{name}/` | {note} |")
        lines.append("")

    voice_dir = ROOT / "voice"
    if voice_dir.is_dir():
        voice_mods = [p for p in voice_dir.glob("*.py") if not p.name.startswith("test_")]
        voice_tests = list((voice_dir / "tests").glob("test_*.py"))
        lines.append(
            f"语音子系统：`voice/` 下 {len(voice_mods)} 个模块、{len(voice_tests)} 个测试文件"
            "（独立运行：`PYTHONPATH=voice:packaging pytest voice/tests -q`）。"
        )
    total = sum(1 for _ in (ROOT / "packaging").glob("*.py"))
    lines.append(f"运行时模块合计：`packaging/` 下 {total} 个 .py（含测试）。")
    return "\n".join(lines) + "\n"


def main() -> int:
    OUT.write_text(render(), encoding="utf-8")
    print(f"written {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
