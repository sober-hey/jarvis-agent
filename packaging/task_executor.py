"""Keyword task routing and text execution through the product chat boundary.

This module does not consume inbox messages or send replies. A missing model
selection is a signal for the caller to use its default deepseek-flash route.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from model_registry import ModelRegistry

# More specific capabilities win over generic writing or speed requests.
_TASK_KEYWORDS = (
    (
        "code",
        ("代码", "编程", "函数", "脚本", "调试", "写程序"),
        r"\b(?:code|coding|programming|python|javascript|typescript|sql|debug|function|script|bug)\b",
    ),
    (
        "image",
        ("图片", "图像", "画图", "绘图", "生图", "画一", "照片", "插画", "海报"),
        r"\b(?:image|picture|draw|drawing|illustration|poster|photo)\b",
    ),
    (
        "analysis",
        ("分析", "对比", "比较", "推理", "计算", "统计", "研究", "评估"),
        r"\b(?:analysis|analy[sz]e|compare|comparison|reasoning|calculate|statistics|research|evaluate)\b",
    ),
    (
        "writing",
        ("写作", "撰写", "文章", "邮件", "文案", "作文", "写一", "润色", "翻译", "总结", "报告"),
        r"\b(?:write|writing|rewrite|essay|email|article|draft|translate|summari[sz]e|report)\b",
    ),
    (
        "fast",
        ("快速", "快点", "简短", "简答", "一句话", "马上回答"),
        r"\b(?:fast|quick|quickly|brief|briefly|short answer)\b",
    ),
)


def self_evaluate() -> dict[str, Any]:
    """Return implementation gaps without selecting a route or executing work."""
    from datetime import UTC, datetime

    return {
        "module": "task_executor",
        "domain": "keyword task classification model routing safe text execution",
        "current_version": (
            "ordered bilingual keyword rules and keyed quality ranking via chat_launcher"
        ),
        "known_limitations": [
            (
                "Classification uses fixed substring/regex rules, not semantic intent "
                "or measured accuracy."
            ),
            (
                "The image label is routable metadata; execute_task only returns "
                "text, not generated images."
            ),
            (
                "Quality/key availability is registry metadata, not a live endpoint "
                "or credential check."
            ),
            (
                "Execution does not expose token usage or retry unknown outcomes; "
                "this module consumes no inbox."
            ),
        ],
        "improvement_directions": [
            (
                "Evaluate routing on a frozen multilingual ambiguous-intent test set "
                "before changing rules."
            ),
            (
                "Expose structured completion, degradation and reconciliation "
                "metadata without leaking credentials."
            ),
            "Add explicit capability compatibility checks for non-text task labels.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


def classify_task(text: str) -> str:
    """Classify locally; specialist keywords take priority over speed hints.

    Chinese terms are substring matches; English terms use word boundaries so
    unrelated words such as ``withdraw`` do not trigger ``draw``.
    """
    normalized = text.casefold()
    for task_type, chinese, english in _TASK_KEYWORDS:
        if any(term in normalized for term in chinese) or re.search(english, normalized):
            return task_type
    return "general"


def select_model(task_type: str, registry: ModelRegistry) -> dict[str, Any] | None:
    """Return the highest-quality keyed exact match, or caller-default signal.

    Unknown quality ranks below measured quality; equal scores are ordered by
    model ID for reproducibility. Merely declaring general is not a wildcard.
    """
    candidates = [
        row
        for row in registry.list_all()
        if row.get("has_key") == 1 and task_type in row.get("task_types", [])
    ]
    candidates.sort(
        key=lambda row: (
            -(row["quality_score"] if row.get("quality_score") is not None else -1),
            row["id"],
        )
    )
    return dict(candidates[0]) if candidates else None


def execute_task(text: str, model_config: dict[str, Any], key_path: str, data_root: Path) -> str:
    """Execute text once using chat_entry via its existing trusted launcher.

    ``model_config`` is a product route, not a registry metadata row. The five
    required fields are model/provider/base_url/api_mode/context_length; omitted
    reasoning_effort means None. Provider labels must obey product_config (use
    custom for OpenAI-compatible services). Never infer endpoints or credentials.

    Each route gets a private data subdirectory because chat_entry pins its
    engine config to a data root. Task sessions are independent from dialogue
    context. Credentials use the launcher's anonymous FD transport, never argv.
    Failures return safe text; unknown outcomes are never automatically retried.
    """
    import chat_launcher as launcher
    from context_limits import CHAT_CONTEXT_BYTE_LIMIT

    if not text.strip():
        return "任务执行失败：任务内容不能为空。"
    if len(text.encode("utf-8")) > CHAT_CONTEXT_BYTE_LIMIT:
        return "任务执行失败：任务内容超过对话入口的长度限制。"
    try:
        config = dict(model_config)
        config.setdefault("reasoning_effort", None)
        config = launcher.product.validate_config(config)
    except (ValueError, TypeError):
        return "任务执行失败：模型配置无效；请提供产品支持的完整路由。"
    try:
        key_file = Path(key_path).expanduser()
        if not key_file.is_file():
            return "任务执行失败：密钥文件不存在或不是普通文件。"
        key = launcher.read_key(key_file=key_file, key_fd=None)
    except (OSError, ValueError, TypeError, launcher.LauncherError):
        return "任务执行失败：密钥文件不可读或不符合产品凭据安全要求。"

    try:
        root = (
            Path(data_root).expanduser()
            / "task-executions"
            / launcher.product.sha256_config(config)
        )
        result = launcher.launch(
            config,
            key,
            text,
            root,
            "task-" + uuid.uuid4().hex,
            source_channel="imessage",
        )
        if result.get("generation_degraded"):
            return "任务执行失败：模型生成已降级，任务未完成；请核查执行记录，勿自动重试。"
        reply = result.get("text")
        if result.get("ok") is not True or not isinstance(reply, str) or not reply.strip():
            return "任务执行失败：执行入口未返回有效文本；请核查执行记录，勿自动重试。"
        if key in reply:
            return "任务执行失败：输出包含凭据，已抑制；请核查执行记录，勿自动重试。"
        return reply
    except launcher.LauncherError as exc:
        # LauncherError messages are deliberately safe and carry reconciliation
        # IDs for unknown outcomes. Arbitrary provider errors must not escape.
        message = str(exc)
        if key not in message:
            return "任务执行失败：" + message
        return "任务执行失败：诊断信息已抑制；请核查执行记录，勿自动重试。"
    except Exception:
        return "任务执行失败：执行入口异常；结果可能未知，请核查执行记录，勿自动重试。"
