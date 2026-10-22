"""Transcription segments reduced locally to broad themes, never verbatim text."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .base import Adapter, CursorStore, SenseEvent, digest, number, parse_time


def self_evaluate() -> dict[str, object]:
    """Describe transcript reduction without loading recordings or ASR models."""
    from datetime import UTC

    return {
        "module": "senses.speech",
        "domain": "offline merged-transcript metadata and broad theme reduction",
        "current_version": "complete JSONL segment polling with filename capture time and broad topic labels",
        "known_limitations": [
            "只读取既有 *.merged.jsonl，不录音、不运行 sherpa-onnx 或其他 ASR，不提供实时听写",
            "主题由固定关键词首命中分类，无语义理解、说话人分离或转写准确率评估",
            "语言仅取源标签或按文字脚本猜测，中文字符不能证明普通话/粤语/方言识别正确",
            "观察时间来自目录/文件名加 start，朴素时间按 Asia/Shanghai；不保留原文，无法从事件重建具体对话",
            "签名依赖文件路径与全文，复制到另一文件会被视作新事件；不检测已消费旧行的后段原地修订",
        ],
        "improvement_directions": [
            "保留主题/语言的 heuristic 证据级别，以合成多语言和重叠主题样本验证误分类",
            "定义跨文件稳定片段 identity 与显式时区/采集时间来源，避免复制重放",
            "实时语音应接独立 voice/voice_live 入口并另验延迟和取消，不升级离线转写元数据的能力声明",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


def theme(text: str) -> str:
    """Fixed broad labels prevent names, addresses and quoted speech escaping."""
    for words, label in [
        (("会议", "开会", "工作", "项目"), "工作交流"),
        (("音乐", "唱歌"), "音乐相关"),
        (("运动", "跑步", "健身"), "运动相关"),
    ]:
        if any(word in text for word in words):
            return label
    return "日常语音"


def language(text: str, supplied: object) -> str:
    if isinstance(supplied, str) and supplied.lower() in {"zh", "en", "ja", "ko", "yue"}:
        return supplied.lower()
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    if re.search(r"[A-Za-z]", text):
        return "en"
    return "und"


class SpeechAdapter(Adapter):
    source = "speech"

    def poll(self, store: CursorStore, since: datetime | None = None) -> list[SenseEvent]:
        self.begin()
        events: list[SenseEvent] = []
        try:
            if not self.path.is_dir():
                return self.unavailable()
            for path in sorted(self.path.rglob("*.merged.jsonl")):
                try:
                    # JPR recorder date + filename is capture time, not processing mtime.
                    stem = path.name.removesuffix(".merged.jsonl").replace("-", ":")
                    base = parse_time(path.parent.name + "T" + stem)
                except ValueError:
                    self.last_status.bad_rows += 1
                    continue
                for line, row in self.lines(path, store):
                    try:
                        start, end = number(row.get("start")), number(row.get("end"))
                        text = row.get("text")
                        if start < 0 or end < start or not isinstance(text, str):
                            raise ValueError("invalid speech segment")
                        observed = base + timedelta(seconds=start)
                        label = theme(text)
                        event = self.event(
                            "speech",
                            observed,
                            f"录音主题：{label}。",
                            {
                                "duration_seconds": end - start,
                                "language": language(text, row.get("language")),
                                "language_basis": "source_or_script_heuristic",
                                "topic": label,
                            },
                            str(line),
                            path=path,
                            line=line,
                        )
                        signature = digest([str(path), start, end, text])
                        self.emit(events, event, store, signature, signature, since)
                    except (ValueError, TypeError, OverflowError):
                        self.last_status.bad_rows += 1
        except OSError:
            return self.unavailable()
        return events
