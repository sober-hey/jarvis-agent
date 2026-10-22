"""K1-010 prompt-injection heuristic DRAFT; NOT_APPROVED / NOT_FROZEN.

Offline, text-only evaluation helper; no runtime wiring or execution authority.
Regex matches are signals, NOT proof that unmatched or sanitized text is safe.
Only scan untrusted source text, never use this to rewrite trusted system policy.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import cast

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False
HIGH_RISK_PLACEHOLDER = "[UNTRUSTED_HIGH_RISK_CONTENT_WITHHELD]"
REDACTED_PLACEHOLDER = "[UNTRUSTED_CONTENT_REDACTED]"
_FLAGS = re.IGNORECASE | re.MULTILINE


class RiskLevel(IntEnum):
    """Relative heuristic severity, not a probability or execution permission."""

    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3


def _text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{name} must be text")
    return value


def _risk(value: object) -> RiskLevel:
    if not isinstance(value, RiskLevel):
        raise ValueError("risk_level must be a RiskLevel")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a sequence")
    return cast(Sequence[object], value)


@dataclass(frozen=True)
class InjectionPattern:
    """Trusted scanner configuration; never accept regexes from model/source text.

    Caller-supplied regexes have Python re's performance semantics (no timeout).
    Built-ins avoid unbounded wildcard gaps; configuration still needs review.
    """

    pattern_id: str
    description: str
    regex_pattern: str
    risk_level: RiskLevel

    def __post_init__(self) -> None:
        _text(self.pattern_id, "pattern_id")
        _text(self.description, "description")
        _text(self.regex_pattern, "regex_pattern")
        if _risk(self.risk_level) is RiskLevel.NONE:
            raise ValueError("pattern risk_level must be LOW, MEDIUM or HIGH")
        try:
            compiled = re.compile(self.regex_pattern, _FLAGS)
        except re.error as error:
            raise ValueError("regex_pattern must be a valid regular expression") from error
        if compiled.search("") is not None:
            raise ValueError("regex_pattern must not match empty text")


@dataclass(frozen=True)
class InjectionHit:
    """A source span [start, end) in Python Unicode code-point offsets.

    matched_text may contain private source material: callers must not blindly
    log/forward hits. Descriptions and IDs come only from trusted configuration.
    """

    pattern_id: str
    description: str
    risk_level: RiskLevel
    start: int
    end: int
    matched_text: str


DEFAULT_PATTERNS: tuple[InjectionPattern, ...] = (
    InjectionPattern(
        "ignore_prior",
        "Instruction to discard higher-priority policy",
        r"\b(?:ignore|forget|disregard)\s+(?:(?:all|the|your|any)\s+){0,3}"
        r"(?:previous|prior|earlier|above)\s+(?:system\s+)?(?:instructions?|rules?|prompts?)\b",
        RiskLevel.HIGH,
    ),
    InjectionPattern(
        "role_override",
        "Assignment of an unrestricted replacement role",
        r"\byou\s+are\s+now\s+(?:an?\s+)?(?:unrestricted|uncensored|unfiltered|"
        r"unlimited|jailbroken|evil)\s+(?:assistant|ai|agent|model)\b",
        RiskLevel.HIGH,
    ),
    InjectionPattern(
        "unsafe_roleplay",
        "Roleplay requesting an unrestricted actor",
        r"\b(?:act\s+as|pretend\s+to\s+be|role[ -]?play\s+as)\s+(?:an?\s+)?"
        r"(?:unrestricted|uncensored|unfiltered|evil|jailbroken)\s+(?:assistant|ai|agent|model)\b",
        RiskLevel.MEDIUM,
    ),
    InjectionPattern(
        "system_delimiter",
        "Source text impersonating a privileged message delimiter",
        r"<\s*/?\s*(?:system|developer)\s*>|\[\s*/?\s*(?:system|inst)\s*\]|"
        r"<\|(?:im_start|start_header_id)\|>\s*(?:system|developer)\b|"
        r"^[ \t]*(?:system|developer)[ \t]*:",
        RiskLevel.HIGH,
    ),
    InjectionPattern(
        "system_replacement",
        "Source text declaring replacement system/developer instructions",
        r"\b(?:new|updated|replacement|override)\s+(?:system|developer)\s+"
        r"(?:prompt|instructions?|message)\s*:",
        RiskLevel.HIGH,
    ),
    InjectionPattern(
        "policy_bypass",
        "Instruction to bypass safety, permission or approval controls",
        r"(?:^[ \t]*|(?<=[.!?])[ \t]*|\bplease\s+|\byou\s+must\s+)"
        r"(?:bypass|disable|override|ignore)\s+(?:(?:all|the|your|any)\s+){0,3}"
        r"(?:safety|security|policy|policies|permission|permissions|approval|guardrails?)\b",
        RiskLevel.HIGH,
    ),
    InjectionPattern(
        "privileged_mode",
        "Claim that a privileged unrestricted mode is enabled",
        r"\b(?:developer|admin|root|god)\s+mode\s+(?:(?:is|has\s+been)\s+)?"
        r"(?:now\s+)?(?:enabled|activated|on)\b",
        RiskLevel.MEDIUM,
    ),
    InjectionPattern(
        "unapproved_execution",
        "Tool execution requested without approval",
        r"\b(?:execute|run|invoke|call)\s+[^\r\n.!?]{0,100}\b"
        r"(?:without|bypassing|skipping)\s+(?:(?:user|human|owner)\s+)?"
        r"(?:approval|permission|confirmation)\b",
        RiskLevel.HIGH,
    ),
    InjectionPattern(
        "secret_disclosure",
        "Request to disclose protected prompts or secrets",
        r"\b(?:reveal|print|show|dump|expose|output|disclose)\s+"
        r"(?:(?:your|the|all|my|entire|full|hidden|original)\s+){0,3}"
        r"(?:system\s+(?:prompt|instructions?)|developer\s+(?:prompt|instructions?)|"
        r"api[ _-]?keys?|passwords?|secrets?|credentials?|access\s+tokens?)\b",
        RiskLevel.HIGH,
    ),
    InjectionPattern(
        "data_exfiltration",
        "Request to send sensitive data to another destination",
        r"\b(?:send|upload|transmit|exfiltrate|email|post)\s+"
        r"[^\r\n.!?]{0,80}\b(?:secrets?|credentials?|api[ _-]?keys?|passwords?|"
        r"private\s+data|system\s+prompt)\b[^\r\n.!?]{0,80}\bto\b",
        RiskLevel.HIGH,
    ),
    InjectionPattern(
        "jailbreak_marker",
        "Low-confidence jailbreak terminology (also occurs in benign discussion)",
        r"\bjailbreak\b|\bDAN\s+mode\b",
        RiskLevel.LOW,
    ),
    InjectionPattern(
        "zh_ignore_prior",
        "Chinese instruction to discard prior policy",
        r"(?:忽略|忘记|无视)[^\r\n。！？]{0,12}(?:之前|先前|前面|以上|上述)"
        r"[^\r\n。！？]{0,12}(?:指令|规则|提示词)",
        RiskLevel.HIGH,
    ),
    InjectionPattern(
        "zh_role_override",
        "Chinese assignment of an unrestricted replacement role",
        r"(?:你现在是|从现在起你是)[^\r\n。！？]{0,20}(?:无限制|没有任何限制|不受限制)"
        r"[^\r\n。！？]{0,10}(?:助手|模型|智能体)",
        RiskLevel.HIGH,
    ),
    InjectionPattern(
        "zh_policy_bypass",
        "Chinese request to bypass permission or safety controls",
        r"(?:绕过|禁用|关闭|跳过)[^\r\n。！？]{0,8}(?:安全策略|权限检查|审批|安全限制)",
        RiskLevel.HIGH,
    ),
    InjectionPattern(
        "zh_secret_disclosure",
        "Chinese request to disclose prompts or secrets",
        r"(?:泄露|显示|输出|打印|透露)[^\r\n。！？]{0,10}(?:系统提示词|密钥|密码|凭据)",
        RiskLevel.HIGH,
    ),
    InjectionPattern(
        "zh_data_exfiltration",
        "Chinese request to transmit sensitive data",
        r"(?:把|将)[^\r\n。！？]{0,10}(?:密钥|密码|凭据|私密数据|系统提示词)"
        r"[^\r\n。！？]{0,10}(?:发送|上传|传输)(?:到|至|给)",
        RiskLevel.HIGH,
    ),
)


class PromptGuard:
    """Heuristic scanner for untrusted text; not a Policy Engine or OS sandbox.

    Normal sanitization replaces whole logical lines intersecting any hit, to
    avoid leaving the attack payload after removing just its trigger phrase.
    Adjacent/overlapping redactions merge. Unaffected text/endings stay unchanged.
    With sandbox_mode=True, a HIGH hit replaces the ENTIRE source with a fixed
    placeholder instead; this returns text, never a content-blocking exception.
    Other severities still use line redaction. No hits means unchanged, NOT safe.
    """

    def __init__(
        self,
        *,
        patterns: Sequence[InjectionPattern] = DEFAULT_PATTERNS,
        sandbox_mode: bool = False,
    ) -> None:
        if type(sandbox_mode) is not bool:
            raise ValueError("sandbox_mode must be a boolean")
        compiled: list[tuple[InjectionPattern, re.Pattern[str]]] = []
        seen: set[str] = set()
        for pattern in _sequence(patterns, "patterns"):
            if not isinstance(pattern, InjectionPattern):
                raise ValueError("patterns must contain InjectionPattern values")
            if pattern.pattern_id in seen:
                raise ValueError("duplicate pattern_id")
            seen.add(pattern.pattern_id)
            compiled.append((pattern, re.compile(pattern.regex_pattern, _FLAGS)))
        self._compiled = tuple(compiled)
        self._sandbox_mode = sandbox_mode

    def scan(self, text: str) -> list[InjectionHit]:
        """Return all nonempty matches, deterministically sorted by source span."""
        _text(text, "text", allow_empty=True)
        hits = [
            InjectionHit(
                pattern.pattern_id,
                pattern.description,
                pattern.risk_level,
                match.start(),
                match.end(),
                match.group(),
            )
            for pattern, compiled in self._compiled
            for match in compiled.finditer(text)
            if match.start() < match.end()
        ]
        return sorted(hits, key=lambda hit: (hit.start, hit.end, hit.pattern_id))

    def classify_risk(self, hits: Sequence[InjectionHit]) -> RiskLevel:
        """Highest configured severity, or NONE; repeated hits do not accumulate."""
        risks: list[RiskLevel] = []
        for hit in _sequence(hits, "hits"):
            if not isinstance(hit, InjectionHit):
                raise ValueError("hits must contain InjectionHit with RiskLevel")
            risks.append(_risk(hit.risk_level))
        return max(risks, default=RiskLevel.NONE)

    def sanitize(self, text: str) -> str:
        """Redact candidate model input; original evidence remains caller-owned."""
        hits = self.scan(text)
        if not hits:
            return text
        if self._sandbox_mode and self.classify_risk(hits) >= RiskLevel.HIGH:
            return HIGH_RISK_PLACEHOLDER
        # Include all Python logical-line boundaries (CRLF treated as one).
        boundaries = list(re.finditer(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]", text))
        starts = [0, *(match.end() for match in boundaries)]
        ends = [*(match.start() for match in boundaries), len(text)]
        spans: list[tuple[int, int]] = []
        for hit in hits:
            start = starts[bisect_right(starts, hit.start) - 1]
            end = ends[bisect_right(starts, hit.end - 1) - 1]
            # A custom match may end inside a line break; remove all matched text.
            end = max(end, hit.end)
            if spans and start <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], end))
            else:
                spans.append((start, end))
        parts: list[str] = []
        cursor = 0
        for start, end in spans:
            parts.extend((text[cursor:start], REDACTED_PLACEHOLDER))
            cursor = end
        parts.append(text[cursor:])
        return "".join(parts)
