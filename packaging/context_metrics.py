"""Offline, content-free context measurements; never called by the daemon.

The serializer matches inner_runtime.call_model (ensure_ascii=False, default
JSON separators, insertion order). A fixed-prefix hash fingerprints the actual
system/instructions/advertised-capability *projection*, not a provider cache hit
or a contiguous prefix: today's runtime puts dynamic context_round first.
Only caller-supplied objects are observed; no data roots or credentials are read.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _render(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def render_fixed_prefix(context: dict[str, Any], *, system_prompt: str | None = None) -> str:
    """Render the static projection without sorting or compacting advertised rows.

    Defaults to the SYSTEM_PROMPT actually used by chat_entry's inner path, not
    inner_voice.get_system_prompt(), which that path does not call. Missing fields
    remain missing. Pass the historical system explicitly when replaying history.
    """
    if system_prompt is None:
        from inner_voice import SYSTEM_PROMPT

        system_prompt = SYSTEM_PROMPT
    if not isinstance(system_prompt, str):
        raise TypeError("system_prompt must be text")
    return _render(
        {
            "system": system_prompt,
            "context": {k: context[k] for k in ("instructions", "capabilities") if k in context},
        }
    )


def fixed_prefix_hash(context: dict[str, Any], *, system_prompt: str | None = None) -> str:
    """SHA-256 of render_fixed_prefix's UTF-8 bytes; not measured cache usage."""
    return hashlib.sha256(
        render_fixed_prefix(context, system_prompt=system_prompt).encode("utf-8")
    ).hexdigest()


def measure_context(context: dict[str, Any], *, system_prompt: str | None = None) -> dict[str, Any]:
    """Return byte/count metadata, never section bodies or evidence identifiers.

    utf8_bytes measures a JSON value including its quotes/brackets. A member's
    bytes include the serialized key and ': ', but not outer braces or ', '.
    Their sum plus json_framing_utf8_bytes equals the exact context render size.
    Containers count direct children; non-null scalars count one; null/absent zero.
    Omitted-only sections cost zero bytes and remain visible with item_count=0.
    The omission total sums counters, not unique origins (dependencies overlap).
    """
    if not isinstance(context, dict) or any(not isinstance(k, str) for k in context):
        raise TypeError("context must have string field names")
    rendered = _render(context).encode("utf-8")
    omitted = context.get("context_omitted", {})
    if not isinstance(omitted, dict) or any(
        not isinstance(k, str) or type(v) is not int or v < 0 for k, v in omitted.items()
    ):
        raise ValueError("context_omitted must contain nonnegative integer counters")
    sections: dict[str, Any] = {}
    for key in dict.fromkeys([*context, *omitted]):
        present = key in context
        value = context.get(key)
        value_bytes = len(_render(value).encode("utf-8")) if present else 0
        sections[key] = {
            "present": present,
            "utf8_bytes": value_bytes,
            "member_utf8_bytes": len(_render(key).encode("utf-8")) + 2 + value_bytes
            if present
            else 0,
            "item_count": len(value) if isinstance(value, (list, dict)) else int(value is not None),
            "omitted_count": omitted.get(key, 0),
        }
    prefix = render_fixed_prefix(context, system_prompt=system_prompt).encode("utf-8")
    return {
        "context_utf8_bytes": len(rendered),
        "sections": sections,
        "json_framing_utf8_bytes": 2 + max(0, len(context) - 1) * 2,
        "context_omitted_count": sum(omitted.values()),
        "fixed_prefix_sha256": hashlib.sha256(prefix).hexdigest(),
        "fixed_prefix_utf8_bytes": len(prefix),
        "fixed_prefix_scope": "system_instructions_capabilities_projection_not_cache_receipt",
    }


OUTCOME_CATEGORIES = (
    "transport_error",
    "empty",
    "malformed_json",
    "truncated_json",
    "schema_violation",
    "cross_reference_violation",
    "unsupported_evidence",
    "single_item_dropped",
    "full_round_accepted",
)
_MAX_RESPONSE_CHARS = 2 * 1024 * 1024


def _strict_json(text: str) -> Any:
    from inner_runtime import unique

    def finite(token: str) -> float:
        import math

        value = float(token)
        if not math.isfinite(value):
            raise ValueError("nonfinite")
        return value

    def reject_constant(token: str) -> None:
        raise ValueError("nonfinite")

    return json.loads(
        text,
        object_pairs_hook=unique,
        parse_float=finite,
        parse_constant=reject_constant,
        strict=True,
    )


def _fields(row: Any, required: set[str], optional: set[str] | None = None) -> bool:
    return isinstance(row, dict) and required <= set(row) <= required | (optional or set())


def _text(value: Any, limit: int | None = None) -> bool:
    return isinstance(value, str) and bool(value.strip()) and (limit is None or len(value) <= limit)


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _strict_schema(raw: Any) -> bool:
    """Documented wire shape, before runtime defaults/coercion/key removal.

    This is shape compliance only: membership of references/IDs and evidence
    support are separate. Optional extension arrays may be absent, but required
    relevance/scheduler and every thought's explicit question field may not.
    Shared scalar/bounds validators are reused without persisting anything.
    """
    from curiosity import validate_gaps, validate_updates
    from inner_runtime import KINDS

    required = {
        "thoughts",
        "resolved_questions",
        "relevance",
        "scheduler_question",
        "belief_updates",
    }
    optional = {"knowledge_gaps", "outreach_intents"}
    if not _fields(raw, required, optional):
        return False
    if not _strings(raw["resolved_questions"]):
        return False
    if raw["scheduler_question"] is not None and not isinstance(raw["scheduler_question"], str):
        return False
    thoughts = raw["thoughts"]
    if not isinstance(thoughts, list) or not 1 <= len(thoughts) <= 4:
        return False
    identities: set[str] = set()
    for thought in thoughts:
        if not _fields(thought, {"id", "kind", "text", "refs", "question"}):
            return False
        identity, kind = thought["id"], thought["kind"]
        if (
            not isinstance(identity, str)
            or identity not in {"t1", "t2", "t3", "t4"}
            or identity in identities
            or not isinstance(kind, str)
            or kind not in KINDS
            or not _text(thought["text"], 3000)
            or not _strings(thought["refs"])
        ):
            return False
        identities.add(identity)
        question = thought["question"]
        if kind != "question":
            if question is not None:
                return False
            continue
        if not _fields(
            question, {"text", "assessments", "selected_capability", "missing_information"}, {"gap"}
        ):
            return False
        if not _text(question["text"], 1000) or not isinstance(
            question["missing_information"], str
        ):
            return False
        if question["selected_capability"] is not None and not isinstance(
            question["selected_capability"], str
        ):
            return False
        assessments = question["assessments"]
        if not isinstance(assessments, list):
            return False
        for assessment in assessments:
            if (
                not _fields(assessment, {"capability_id", "can_resolve", "reason"})
                or not isinstance(assessment["capability_id"], str)
                or type(assessment["can_resolve"]) is not bool
                or not _text(assessment["reason"])
            ):
                return False
        if "gap" in question:
            if not isinstance(question["gap"], dict):
                return False
            validate_gaps([question["gap"]])
    relevance = raw["relevance"]
    if not isinstance(relevance, list):
        return False
    for row in relevance:
        if (
            not _fields(row, {"belief_id", "capability_id", "score", "reason"})
            or not isinstance(row["belief_id"], str)
            or not isinstance(row["capability_id"], str)
            or type(row["score"]) not in {int, float}
            or not 0 <= row["score"] <= 1
            or not _text(row["reason"])
        ):
            return False
    gaps = raw.get("knowledge_gaps", [])
    if not isinstance(gaps, list) or any(not isinstance(g, dict) for g in gaps):
        return False
    validate_gaps(gaps)
    validate_updates(raw.get("belief_updates", []))
    intents = raw.get("outreach_intents", [])
    if not isinstance(intents, list) or len(intents) > 2:
        return False
    for intent in intents:
        if (
            not _fields(intent, {"reason", "evidence", "urgency", "needs_response"})
            or not _text(intent["reason"], 500)
            or any(c in intent["reason"] for c in "\r\n")
            or not isinstance(intent["urgency"], str)
            or intent["urgency"] not in {"low", "normal", "high", "emergency"}
            or intent["needs_response"] is not False
            or not _strings(intent["evidence"])
            or not 1 <= len(intent["evidence"]) <= 8
        ):
            return False
    return True


def _schema_compliant(raw: Any) -> bool:
    try:
        return _strict_schema(raw)
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
        return False


def _allowed_refs(context: dict[str, Any]) -> set[str]:
    """Same prompt-visible sources as InnerLoop.outreach_refs, allowing sparse fixtures."""
    allowed = {
        r["id"]
        for key in ("events", "thoughts", "recent_thoughts", "recalled", "pending_observations")
        for r in context.get(key, [])
    }
    allowed.update("memory:" + r["id"] for r in context.get("memories", []))
    allowed.update(
        b["belief_id"] for key in ("beliefs", "relevance_candidates") for b in context.get(key, [])
    )
    allowed.update(m["origin_id"] for m in context.get("pending_memories", []))
    return allowed


def classify_outcome(
    text: str | None,
    *,
    context: dict[str, Any] | None = None,
    allowed_refs: set[str] | None = None,
    catalog: list[dict[str, Any]] | None = None,
    questions: list[dict[str, Any]] | None = None,
    transport_error: object = None,
    evidence_supported: bool | None = None,
) -> dict[str, Any]:
    """Classify a supplied main-thought response, without models, stores or writes.

    Precedence: transport, empty, lexical parse, runtime envelope/refs, matrix,
    explicit unsupported evidence, thought isolation, accepted. A repaired valid
    response can be accepted while BOTH original-compliance flags remain false.
    original_schema_compliant is shape only, independent of provenance/support.
    tolerant_usable means at least one thought survived the response validators;
    an incomplete matrix may preserve thoughts but is NOT full-round acceptance.
    'single_item_dropped' includes multiple/cascading drops; dropped_item_count is
    exact. All thoughts dropped is unsupported_evidence, never usable/accepted.

    full_round_accepted means the observed validation boundary only, not committed
    runtime state or semantic truth. Curiosity.update/decide/discover are NOT run.
    evidence_supported may carry an independent offline evidence adjudication;
    None means semantic support is unmeasured, not a positive judgment.
    Invalid caller context raises rather than being counted as model failure.
    """
    if evidence_supported is not None and type(evidence_supported) is not bool:
        raise TypeError("evidence_supported must be bool or None")
    context = {} if context is None else context
    catalog = context.get("capabilities", []) if catalog is None else catalog
    questions = context.get("unresolved_questions", []) if questions is None else questions
    allowed = _allowed_refs(context) if allowed_refs is None else set(allowed_refs)
    report: dict[str, Any] = {
        "category": "malformed_json",
        "reason": "invalid_json",
        "original_json_compliant": False,
        "original_schema_compliant": False,
        "tolerant_json_parseable": False,
        "tolerant_schema_usable": False,
        "tolerant_usable": False,
        "full_round_accepted": False,
        "repair_applied": False,
        "retained_item_count": 0,
        "dropped_item_count": 0,
        "missing_relevance_count": 0,
        "semantic_evidence_supported": evidence_supported,
        "belief_update_evidence_checked": False,
        "validation_scope": "response_validation_and_relevance_matrix_not_runtime_commit",
    }
    if transport_error is not None:
        report.update(category="transport_error", reason="transport_failure")
        return report
    if text is None or isinstance(text, str) and not text.lstrip("\ufeff").strip():
        report.update(category="empty", reason="empty_text")
        return report
    if not isinstance(text, str):
        report.update(category="schema_violation", reason="non_text_response")
        return report
    if len(text) > _MAX_RESPONSE_CHARS:
        report.update(reason="response_size_limit")
        return report
    try:
        original = _strict_json(text)
        report["original_json_compliant"] = True
        report["original_schema_compliant"] = _schema_compliant(original)
    except (ValueError, TypeError, OverflowError, RecursionError):
        pass
    from curiosity import validate_matrix, validate_updates
    from inner_runtime import parse_response_json, validate

    try:
        raw = parse_response_json(text)
        report["tolerant_json_parseable"] = True
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        if (
            isinstance(exc, ValueError)
            and str(exc) == "Model output truncated or has unclosed JSON"
        ):
            report.update(category="truncated_json", reason="unclosed_json")
        return report
    try:
        parsed = validate(text, allowed, catalog, questions)
        validate_updates(parsed["belief_updates"])
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else ""
        cross_errors = {
            "Invalid thought provenance",
            "Invalid resolution reference",
            "Every capability must be assessed; matching source must be selected",
        }
        ambiguous_reference_errors = {
            "Invalid capability assessment",
            "Invalid outreach intent or provenance; questions require review",
        }
        if (reason in cross_errors or reason in ambiguous_reference_errors) and _schema_compliant(
            raw
        ):
            report.update(category="cross_reference_violation", reason="invalid_reference")
        elif reason == "Evidence required":
            report.update(category="unsupported_evidence", reason="missing_evidence")
        else:
            report.update(category="schema_violation", reason="invalid_response_shape")
        return report
    report["tolerant_schema_usable"] = True
    report["repair_applied"] = (
        not report["original_json_compliant"] or not report["original_schema_compliant"]
    )
    report["retained_item_count"] = len(parsed["thoughts"])
    report["dropped_item_count"] = sum(
        d.get("action") == "dropped" for d in parsed.get("validation_degradations", [])
    )
    report["tolerant_usable"] = bool(parsed["thoughts"])
    try:
        missing = validate_matrix(parsed["relevance"], context, catalog, require_complete=False)
    except (ValueError, TypeError, KeyError):
        report.update(
            category="cross_reference_violation",
            reason="invalid_relevance_reference",
            tolerant_usable=False,
        )
        return report
    report["missing_relevance_count"] = len(missing)
    # Only prompt-visible identity checks; updating evidence would require state
    # and clock-sensitive provenance logic. Never instantiate Curiosity/MindStore.
    memory_origins = {m["origin_id"] for m in context.get("pending_memories", [])}
    origins = {origin: origin for origin in memory_origins}
    origins.update(
        {
            e["id"]: e["payload"]["data"].get("belief_id")
            for e in context.get("pending_observations", [])
        }
    )
    for update in parsed["belief_updates"]:
        if update["origin_id"] not in origins:
            report.update(
                category="cross_reference_violation",
                reason="unknown_belief_origin",
                tolerant_usable=False,
            )
            return report
        target = origins[update["origin_id"]]
        permitted = {target} if update["origin_id"] in memory_origins else {None, target}
        if target is not None and update["belief_id"] not in permitted:
            report.update(
                category="cross_reference_violation",
                reason="belief_target_mismatch",
                tolerant_usable=False,
            )
            return report
    if evidence_supported is False or not parsed["thoughts"]:
        report.update(
            category="unsupported_evidence", reason="unsupported_evidence", tolerant_usable=False
        )
    elif missing:
        report.update(category="schema_violation", reason="incomplete_relevance_matrix")
    elif report["dropped_item_count"]:
        report.update(category="single_item_dropped", reason="thought_evidence_isolation")
    else:
        report.update(
            category="full_round_accepted",
            reason="validation_boundary_accepted",
            full_round_accepted=True,
        )
    return report


def self_evaluate() -> dict[str, Any]:
    """Static description only; no private state, clocks, files or network access."""
    return {
        "module": "context_metrics",
        "domain": "offline context byte accounting and thought-response validation observation",
        "current_version": "v1 standalone observer; no daemon integration",
        "known_limitations": [
            "Bytes are context JSON, not HTTP envelope bytes, tokens, latency, "
            "or provider cache-hit receipts.",
            "Fixed prefix is an ordered system/instructions/capabilities projection, "
            "not today's contiguous wire prefix; dynamic context_round precedes instructions.",
            "Current SYSTEM_PROMPT is the default; historical replay must explicitly "
            "supply its historical system text.",
            "Schema checks mirror the documented thought wire shape and can drift "
            "if runtime instructions or validators change.",
            "Optional extension arrays may be absent, but thought/relevance/scheduler/"
            "belief_updates fields must be explicit for original-schema compliance.",
            "full_round_accepted covers response validation and the relevance matrix, "
            "not Curiosity.update/decide/discover, attention suppression, persistence "
            "or daemon consumption.",
            "Semantic evidence truth and belief-update evidence/time/identity provenance "
            "are not fully checked; evidence_supported is optional caller adjudication, "
            "not inferred support.",
            "Incomplete-matrix thoughts can be tolerant_usable while full_round_accepted "
            "is false; single_item_dropped can count multiple dependent drops.",
            "Truncation is identified by the existing parser's unclosed-document diagnostic, "
            "not a provider finish_reason or proof of transport loss.",
            "Omission totals sum counters including dependent rows and capability-detail "
            "compaction, not unique lost facts; arbitrary caller field names are not anonymized.",
        ],
        "improvement_directions": [
            "Freeze historical system and context renders alongside offline replay fixtures.",
            "Add typed diagnostics at product validators before replacing conservative "
            "error-message classification.",
            "Measure actual transport/cache receipts and evidence adjudication separately "
            "before claiming end-to-end reliability.",
        ],
    }
