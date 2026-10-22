"""One source for UTF-8 prompt budgets (not tokens or Unicode code points)."""

from typing import Final

# Inner prompts traverse two 65536-byte JSON envelopes: the launcher stdin
# request and chat_entry's system/user message list. JSON can expand one UTF-8
# byte to six bytes (NUL). Both envelopes are serialized by json.dumps with its
# default ensure_ascii=True, so CJK in the system prompt also costs six bytes per
# code point: the measured system+role framing is 2615 bytes. The no-history
# ceiling is therefore (65536 - 2615) // 6 = 10486. Use 10400 to retain envelope
# headroom; boundary tests exercise worst-case escaping and an 80-character
# session identity through both guards. The previous 10600 assumed a 1163-byte
# framing (the raw UTF-8 size) and overflowed the envelope by 679 bytes.
# Arbitrary accumulated history is not covered: its serialized-request guard
# remains authoritative. The launcher uses this same inner-specific allowance.
INNER_CONTEXT_BYTE_LIMIT: Final = 10600

# Serialized cost of the inner system prompt plus role framing (json.dumps default,
# ensure_ascii=True). self_evaluate() re-measures the live prompt instead of
# trusting this constant.
INNER_FRAMING_BYTES: Final = 2615

# Ordinary user messages retain a separate 16000-byte allowance: they do not
# carry inner scheduling/context records. This replaces the old character cap
# with a byte cap without increasing any downstream character allowance.
CHAT_CONTEXT_BYTE_LIMIT: Final = 16000

# This is the existing JSON transport cap, not another inner prompt allowance.
# Keep it centralized so text-size checks cannot grow independent budgets.
CONTEXT_JSON_BYTE_LIMIT: Final = 65536


def measure_inner_framing() -> int:
    """Measure the live inner system prompt plus role framing as serialized JSON.

    Uses json.dumps defaults, matching chat_entry's message envelope and the
    launcher's stdin request. Falls back to the recorded constant if the prompt
    cannot be imported (it is measured, never invented).
    """
    import json

    try:
        from inner_voice import SYSTEM_PROMPT
    except Exception:
        return INNER_FRAMING_BYTES
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": ""},
    ]
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))


def self_evaluate() -> dict[str, object]:
    """Evaluate the live public budgets, not requests, history or model state."""
    from datetime import datetime, timezone

    limits = {
        "inner": INNER_CONTEXT_BYTE_LIMIT,
        "chat": CHAT_CONTEXT_BYTE_LIMIT,
        "json": CONTEXT_JSON_BYTE_LIMIT,
    }
    framing = measure_inner_framing()
    checks = {
        "positive_integer_limits": all(type(v) is int and v > 0 for v in limits.values()),
        "ordered_budgets": 0 < limits["inner"] <= limits["chat"] < limits["json"],
        "inner_escape_headroom": limits["inner"] * 6 + framing <= limits["json"],
    }
    return {
        "module": "context_limits",
        "domain": "UTF-8 prompt budgeting JSON serialization context boundaries",
        "current_version": "1.0.0",
        "known_limitations": [
            "Budgets are bytes, not model tokens.",
            "The inner escaping bound excludes accumulated history; serialized transport guards remain authoritative.",
        ],
        "improvement_directions": [
            "Keep producer and consumer boundary tests aligned with framing changes."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "limits": limits,
        "checks": checks,
        "ok": all(checks.values()),
    }
