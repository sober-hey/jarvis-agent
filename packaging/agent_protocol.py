"""JSON task/result envelopes shared by independent agent execution units.

Generated task IDs are UUIDs. Explicit nonempty IDs are retained verbatim for
caller correlation (including legacy IDs). This is a wire protocol, not an
execution authorization, durable queue, sandbox, or remote HTTP implementation.
"""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, cast
from uuid import uuid4

TASK_TYPES = frozenset({"code", "writing", "analysis", "fast", "general", "research"})
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
RESULT_STATUSES = frozenset({"completed", "failed", "timeout", "cancelled"})


def self_evaluate() -> dict[str, Any]:
    """Describe wire validation only; do not construct tasks or contact endpoints."""
    from datetime import UTC, datetime

    return {
        "module": "agent_protocol",
        "domain": "agent task result JSON wire protocol validation schema evolution",
        "current_version": (
            "validated dataclass JSON envelopes with duplicate-field and finite-value checks"
        ),
        "known_limitations": [
            "Envelopes have no explicit protocol version or schema negotiation.",
            (
                "Dataclass containers remain mutable; validation is repeated on "
                "serialization, not every mutation."
            ),
            "JSON size and nesting depth are not explicitly bounded by this module.",
            (
                "Wire validity is not execution authorization, durable delivery, or a "
                "remote HTTP implementation."
            ),
            "Image classification labels from task_executor are not part of TASK_TYPES.",
        ],
        "improvement_directions": [
            (
                "Introduce explicit backward-compatible schema versioning and "
                "negotiated endpoint capabilities."
            ),
            "Add bounded payload/depth validation and adversarial serialization tests.",
            (
                "Align task labels across classifier and protocol while preserving "
                "explicit authorization boundaries."
            ),
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


def _text(value: object, name: str, *, empty: bool = False) -> None:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{name} must be a string" + ("" if empty else " and nonempty"))


def _json_value(value: object, stack: set[int] | None = None) -> None:
    """Reject lossy JSON conversions, cycles, NaN and Infinity."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) not in (dict, list):
        raise ValueError("Metadata must contain only JSON values")
    stack = set() if stack is None else stack
    identity = id(value)
    if identity in stack:
        raise ValueError("Cyclic JSON value")
    stack.add(identity)
    try:
        values: Iterable[object]
        if isinstance(value, dict):
            mapping = cast(dict[object, object], value)
            if any(not isinstance(key, str) for key in mapping):
                raise ValueError("JSON object keys must be strings")
            values = mapping.values()
        else:
            values = cast(list[object], value)
        for item in values:
            _json_value(item, stack)
    finally:
        stack.remove(identity)


@dataclass
class TaskMessage:
    task_id: str = field(default_factory=lambda: str(uuid4()))
    task_type: str = "general"
    description: str = ""
    priority: int = 1
    effort: str = "medium"
    model_id: str | None = None
    dependencies: list[str] = field(default_factory=lambda: list[str]())
    timeout_seconds: int = 300
    metadata: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())

    def __post_init__(self) -> None:
        _text(self.task_id, "task_id")
        if type(self.task_type) is not str or self.task_type not in TASK_TYPES:
            raise ValueError("Unknown task_type")
        _text(self.description, "description", empty=True)
        if type(self.priority) is not int or not 0 <= self.priority <= 3:
            raise ValueError("priority must be an integer from 0 to 3")
        if type(self.effort) is not str or self.effort not in EFFORTS:
            raise ValueError("Unknown effort")
        if self.model_id is not None:
            _text(self.model_id, "model_id")
        if type(self.dependencies) is not list:
            raise ValueError("dependencies must be a list of task IDs")
        for dependency in self.dependencies:
            _text(dependency, "dependency")
        if self.task_id in self.dependencies or len(set(self.dependencies)) != len(
            self.dependencies
        ):
            raise ValueError("Self-dependencies and duplicate dependencies are invalid")
        if type(self.timeout_seconds) is not int or self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be a positive integer")
        if type(self.metadata) is not dict:
            raise ValueError("metadata must be a JSON object")
        _json_value(self.metadata)


@dataclass
class TaskResult:
    task_id: str
    status: str
    output: str = ""
    error: str | None = None
    duration_seconds: float = 0.0
    model_used: str = ""
    token_usage: dict[str, Any] | None = None
    critic_review: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _text(self.task_id, "task_id")
        if type(self.status) is not str or self.status not in RESULT_STATUSES:
            raise ValueError("Unknown result status")
        _text(self.output, "output", empty=True)
        _text(self.model_used, "model_used", empty=True)
        if self.error is not None:
            _text(self.error, "error", empty=True)
        if (
            type(self.duration_seconds) not in (int, float)
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds < 0
        ):
            raise ValueError("duration_seconds must be finite and nonnegative")
        for value in (self.token_usage, self.critic_review):
            if value is not None:
                if type(value) is not dict:
                    raise ValueError("usage and review must be JSON objects or None")
                _json_value(value)


class ExecutorType(StrEnum):
    CODEX_CLI = "codex_cli"
    MODEL_API = "model_api"
    LOCAL_SCRIPT = "local_script"
    REMOTE_HTTP = "remote_http"


class AgentEndpoint(ABC):
    """Implementations return one terminal result; no retry is implied."""

    executor_type: ExecutorType

    @abstractmethod
    def execute(self, message: TaskMessage) -> TaskResult:
        """Execute one task using this endpoint."""

    @abstractmethod
    def health_check(self) -> bool:
        """Report local availability without performing a model request."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _decode(data: str) -> dict[str, Any]:
    if type(data) is not str:
        raise ValueError("Protocol input must be a JSON string")
    payload = json.loads(data, object_pairs_hook=_unique_object)
    if not isinstance(payload, dict):
        raise ValueError("Protocol envelope must be a JSON object")
    _json_value(cast(dict[str, Any], payload))
    return cast(dict[str, Any], payload)


def serialize(message: TaskMessage) -> str:
    message.__post_init__()
    return json.dumps(asdict(message), ensure_ascii=False, allow_nan=False)


def deserialize(data: str) -> TaskMessage:
    try:
        return TaskMessage(**_decode(data))
    except TypeError as exc:
        raise ValueError("Invalid task envelope fields") from exc


def serialize_result(result: TaskResult) -> str:
    result.__post_init__()
    return json.dumps(asdict(result), ensure_ascii=False, allow_nan=False)


def deserialize_result(data: str) -> TaskResult:
    try:
        return TaskResult(**_decode(data))
    except TypeError as exc:
        raise ValueError("Invalid result envelope fields") from exc
