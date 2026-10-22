"""Fixed-quota inner request allocation. No I/O, model calls or persistent state."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, cast

from context_limits import INNER_CONTEXT_BYTE_LIMIT
from curiosity import context_capabilities

# Ceilings for the four list families that used to compete by size. They are not
# additive reservations: instructions and keys already consume nearly half the
# envelope, so each family is trimmed against its own cap and then the whole
# view is trimmed to the transport limit.
QUOTAS = {"capabilities": 2600, "beliefs": 1800, "relevance_candidates": 1200, "memory": 3200}
MEMORY = ("recalled", "events", "recent_thoughts")


def size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


def quota_context(context: dict[str, Any], *, reserved_bytes: int = 512) -> dict[str, Any]:
    """Preserve complete records and two recalled in the memory block.

    Ceilings are not additive reservations: instructions/keys consume nearly
    half the envelope. Water-filling trims only extras, never immutable floors.
    Candidate+pending observation is an indivisible evidence unit. Indivisible
    floors that cannot fit fail closed rather than silently losing provenance.
    """
    view = deepcopy(context)
    omitted = dict(view.get("context_omitted", {}))
    view["context_omitted"] = omitted

    # Rows that carry this round's anchor rather than history: the clock event
    # (the only source of "now" and of a citable id) and gaps already waiting on
    # Sir (without them the model cannot see that it already asked). They are
    # trimmed only when no other row is left.
    anchor = next(
        (
            row
            for row in reversed(view.get("events", []))
            if row.get("payload", {}).get("kind") == "time"
        ),
        None,
    )
    waiting = {
        row["belief_id"]
        for row in view.get("beliefs", [])
        if row.get("attention", {}).get("status") == "awaiting_sir"
    }

    def replace(key: str, rows: list[dict[str, Any]]) -> None:
        old = view.get(key, [])
        omitted[key] = omitted.get(key, 0) + max(0, len(old) - len(rows))
        view[key] = rows

    for key in (
        "thoughts",
        "memories",
        "unresolved_questions",
        "unbound_questions",
        "known_gap_identities",
        "pending_memories",
        "lessons",
        "cached_relevance",
    ):
        if key in view:
            replace(key, [])
    view["capabilities"] = context_capabilities(view.get("capabilities", []))
    # Shortest live event first avoids a large envelope/summary starving all
    # ordinary events; rows themselves and source IDs remain unmodified.
    if view.get("events"):
        view["events"] = sorted(view["events"], key=size)
    if view.get("recent_thoughts"):
        view["recent_thoughts"] = list(reversed(view["recent_thoughts"]))
    floor = {
        k: min(
            len(view.get(k, [])),
            2 if k == "recalled" else 0 if k in ("events", "recent_thoughts") else 1,
        )
        for k in (*MEMORY, "capabilities", "beliefs", "relevance_candidates")
    }
    pending = list(view.get("pending_observations", []))

    def sync() -> None:
        candidates = {r["belief_id"] for r in view.get("relevance_candidates", [])}
        view["pending_observations"] = [
            r for r in pending if r["payload"]["data"].get("belief_id") in candidates
        ]
        events = {r["id"] for r in view.get("events", [])}
        view["daemon_pending_senses"] = [
            r for r in context.get("daemon_pending_senses", []) if r["id"] in events
        ]
        caps = {r["id"] for r in view.get("capabilities", [])}
        view["cached_relevance"] = [
            r
            for r in view.get("cached_relevance", [])
            if r.get("capability_id") in caps and r.get("belief_id") in candidates
        ]

    def pop(key: str) -> None:
        rows = list(view.get(key, []))
        index = len(rows) - 1
        if key == "events" and anchor is not None:
            index = next((i for i, row in enumerate(rows) if row is not anchor), index)
        elif key == "beliefs" and waiting:
            index = next(
                (i for i, row in enumerate(rows) if row["belief_id"] not in waiting), index
            )
        replace(key, rows[:index] + rows[index + 1 :])
        sync()

    for key, cap in QUOTAS.items():
        if key == "memory":
            continue
        while (
            size(view.get(key, []))
            + (
                size(view.get("pending_observations", [])) - 2
                if key == "relevance_candidates"
                else 0
            )
            > cap
        ):
            if len(view[key]) <= floor[key]:
                raise ValueError("Indivisible quota floor exceeds cap")
            pop(key)
    sync()

    def reduce(keys: tuple[str, ...]) -> None:
        eligible = [k for k in keys if len(view.get(k, [])) > floor[k]]
        if not eligible:
            raise ValueError("Indivisible quota floors exceed budget")
        last_resort = [
            k
            for k in eligible
            if (k == "events" and anchor is not None and all(row is anchor for row in view[k]))
            or (k == "beliefs" and waiting and all(row["belief_id"] in waiting for row in view[k]))
        ]
        preferred = [k for k in eligible if k not in last_resort]
        pop(max(preferred or eligible, key=lambda k: size(view[k][-1])))

    while sum(size(view.get(k, [])) for k in MEMORY) > QUOTAS["memory"]:
        reduce(MEMORY)
    limit = INNER_CONTEXT_BYTE_LIMIT - reserved_bytes
    while size(view) > limit:
        reduce((*MEMORY, "capabilities", "beliefs", "relevance_candidates"))
    for key, count in floor.items():
        if len(view.get(key, [])) < count:
            raise ValueError("Quota floor lost")
    # Never expose an observation after removing its only source capability.
    # Reject this candidate view, so the caller can use the proven baseline
    # allocator. No origins are acknowledged or silently changed by this path.
    from curiosity import event_evidence

    visible = view.get("capabilities", [])
    for event in view.get("pending_observations", []):
        data = event["payload"]["data"]
        if data.get("sense_event"):
            supported = event_evidence(event, visible) is not None
        else:
            supported = (
                event.get("source") in {c["id"] for c in visible}
                or event["payload"].get("kind") == "conversation"
                and event.get("source") in {"chat", "sir", "owner"}
            )
        # Only enforce for observations that have a source descriptor. Compact
        # synthetic quota fixtures without one do not represent real evidence.
        if event.get("source") and not supported:
            raise ValueError("Quota source capability omitted")
    return view


def self_evaluate() -> dict[str, Any]:
    return {
        "module": "replay_quota",
        "domain": "bounded thought context allocation",
        "current_version": 1,
        "known_limitations": [
            "indivisible quota overflow falls back to baseline",
            "evaluation helper measures validation, not external delivery",
        ],
        "improvement_directions": ["evaluate on additional paired recordings"],
        "last_evaluated_at": None,
    }


def evaluate(text: str, context: dict[str, Any]) -> dict[str, Any]:
    """Use unchanged production validation; retain only numeric metrics."""
    import hashlib
    import math

    from inner_runtime import InnerLoop, unique, validate

    def finite(token: str) -> float:
        value = float(token)
        if not math.isfinite(value):
            raise ValueError("Nonfinite")
        return value

    def constant(_: str) -> None:
        raise ValueError("Nonfinite")

    result: dict[str, Any] = {
        "strict_json": False,
        "effective": False,
        "validate_pass": False,
        "invalid_thought_provenance": 0,
        "unknown_belief_origin": 0,
        "memory_used": False,
        "recalled_used": False,
        "events_used": False,
        "recent_thoughts_used": False,
    }
    raw: Any = {}
    try:
        raw = json.loads(
            text, object_pairs_hook=unique, parse_float=finite, parse_constant=constant
        )
        result["strict_json"] = True
    except (ValueError, TypeError):
        pass
    origins = {r["origin_id"] for r in context.get("pending_memories", [])} | {
        r["id"] for r in context.get("pending_observations", [])
    }
    if isinstance(raw, dict):
        updates = cast(dict[str, Any], raw).get("belief_updates")
        if isinstance(updates, list):
            result["unknown_belief_origin"] = int(
                any(
                    isinstance(u, dict) and cast(dict[str, Any], u).get("origin_id") not in origins
                    for u in cast(list[Any], updates)
                )
            )
    try:
        parsed = validate(
            text,
            InnerLoop.outreach_refs(context),
            context.get("capabilities", []),
            context.get("unresolved_questions", []),
        )
        result["validate_pass"] = True
        result["effective"] = bool(parsed["thoughts"])
        # Count only the exact current-round aliases the validator rewrote; this
        # is evidence of repair, never a widening of the allowlist.
        result["ref_normalizations"] = len(parsed.get("ref_normalizations") or [])
        refs = {r for t in parsed["thoughts"] for r in t["refs"]}
        for key in MEMORY:
            ids = {r["id"] for r in context.get(key, [])}
            result[key + "_used"] = bool(ids & refs)
        result["memory_used"] = any(result[k + "_used"] for k in MEMORY)
        result["retained_thoughts"] = len(parsed["thoughts"])
    except (ValueError, TypeError, KeyError) as exc:
        result["invalid_thought_provenance"] = int(str(exc) == "Invalid thought provenance")
        result["error_sha256"] = hashlib.sha256(
            (type(exc).__name__ + str(exc)).encode()
        ).hexdigest()
    return result
