"""Independent red-team review using the product's existing model transport.

``review`` evaluates one submitted revision and returns actionable feedback.
The caller owns revision and resubmission; reviewing the same unchanged result
repeatedly would not constitute a repair loop. ``max_iterations`` is the caller's
positive loop budget, while ``CriticReport.iterations`` counts reviews performed
by this call (one). A failed report never becomes a pass when a budget expires.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from inner_model import ProductModel


@dataclass(frozen=True)
class CriticReport:
    """Model judgement of a submitted result, not an execution/test receipt."""

    passed: bool
    reason: str
    suggestions: list[str]
    iterations: int


class CriticAgent:
    """Create a separate model call without executor history or dialogue persona."""

    def self_evaluate(self) -> dict[str, Any]:
        """Describe implemented behavior and gaps without reading private state."""
        return {
            "module": "critic_agent",
            "domain": "independent model red-team review structured judgement validation",
            "current_version": "One isolated ProductModel review per submitted revision with strict duplicate-aware JSON judgement validation",
            "known_limitations": [
                "Each review performs one model judgement; max_iterations is a caller budget and no automatic revision loop is implemented.",
                "The critic uses the supplied product route; a separate call does not guarantee independent model-family judgement.",
                "Feedback evaluates submitted text, not execution receipts or independently run tests.",
            ],
            "improvement_directions": [
                "Let callers resubmit actual revised artifacts and attach revision-specific verification evidence.",
                "Evaluate reviewer/executor error correlation before selecting an independent reviewer route.",
                "Keep model judgements distinct from executable acceptance checks and test receipts.",
            ],
            "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        }

    def review(
        self,
        task_description: str,
        result: str,
        *,
        config: dict[str, Any],
        key: str,
        data_root: Path,
        max_iterations: int = 3,
    ) -> CriticReport:
        """Review once; return failures for caller-controlled revision.

        Use the supplied route unchanged (including a configured DeepSeek Flash
        route). Transport errors propagate without retrying unknown requests;
        malformed judgements raise ValueError, never implicit approval.
        """
        if type(max_iterations) is not int or max_iterations < 1:
            raise ValueError("max_iterations must be a positive integer")
        task_value = cast(object, task_description)
        result_value = cast(object, result)
        if not isinstance(task_value, str) or not task_value.strip():
            raise ValueError("task_description must be a non-empty string")
        if not isinstance(result_value, str):
            raise ValueError("result must be a string")

        prompt = (
            f"任务要求：{task_description}\n"
            f"交付结果：{result}\n"
            "请以红队审查员身份评审以上结果。给出：passed（通过）或 failed（不通过），"
            "以及具体理由和修改建议。由 JSON 返回："
            '{"passed": true/false, "reason": "...", "suggestions": [...]}'
        )
        model = ProductModel(dict(config), key, data_root)
        response = model(prompt)
        text = response.get("text")
        if not isinstance(text, str):
            raise ValueError("Critic response must contain JSON text")
        text = text.strip()
        lines = text.splitlines()
        if (
            len(lines) >= 3
            and lines[0].strip().lower() in {"```", "```json"}
            and lines[-1].strip() == "```"
        ):
            text = "\n".join(lines[1:-1])

        def unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
            fields: dict[str, object] = {}
            for name, value in pairs:
                if name in fields:
                    raise ValueError("Duplicate critic judgement field")
                fields[name] = value
            return fields

        try:
            raw: object = json.loads(text, object_pairs_hook=unique_fields)
        except ValueError:
            raise ValueError("Critic response must be valid JSON") from None
        if not isinstance(raw, dict):
            raise ValueError("Invalid critic judgement schema")
        judgement = cast(dict[str, object], raw)
        passed = judgement.get("passed")
        reason = judgement.get("reason")
        suggestions = judgement.get("suggestions")
        if (
            not isinstance(passed, bool)
            or not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(suggestions, list)
        ):
            raise ValueError("Invalid critic judgement schema")
        items = cast(list[object], suggestions)
        if any(not isinstance(item, str) for item in items):
            raise ValueError("Invalid critic judgement schema")
        return CriticReport(
            passed=passed,
            reason=reason,
            suggestions=cast(list[str], list(items)),
            iterations=1,
        )
