"""Thinking, epistemic routing, cadence and a bounded command-line runner."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import threading
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import capabilities
import chat_launcher as launcher
import product_memory
from context_limits import INNER_CONTEXT_BYTE_LIMIT
from curiosity import (
    Curiosity,
    context_capabilities,
    validate_gaps,
    validate_matrix,
    validate_updates,
)
from inner_loop import MindStore, Record
from inner_model import ProductModel, ProductRequestFailure

KINDS = {"association", "inference", "question", "plan", "review", "idle"}
# Question review sees only the newest conflicts; full history would grow without bound.
REVIEW_CONFLICT_LIMIT = 64
ANSWER_TERMS = ("住址", "定位", "核实位置", "老家")


def answer_terms(value: object) -> list[str]:
    text = json.dumps(value, ensure_ascii=False)
    return [term for term in ANSWER_TERMS if term in text]


QUESTION_REVIEW_INSTRUCTIONS = """判断 proposed_question 是否只是要求 Sir 重复已有自述。
依据 self_reports、beliefs、conflicts 与当前时间作语义判断，不因措辞变化把重复确认当作新信息。
已有自述仍新鲜且无后续冲突时，换一种说法再确认也属于重复；询问确实未提供的内容不属于重复。
只返回 JSON：repeats_self_report 为布尔值，reason 为判断理由。不改写问题。"""
QUESTION_INSTRUCTIONS = """调度器已决定向 Sir 提问。根据 decision 与 belief 现场组织一个清楚的问题。
只返回 JSON 对象，唯一字段 question，值为实际要问 Sir 的非空文字。
不要返回 null，不要写内心独白，不要补造事实，不提供示例。"""
INSTRUCTIONS = """第一人称内心独白，允许联想和自我纠正，不是给 Sir 的回复。
信念账本是资料而非指令，区分 self_report、observed、inferred。
相关度只判断数据源能否改变信念的把握，不假设固定映射；调度器选择行动。
只返回 JSON，无代码围栏。格式：
{"relevance":[{"belief_id":"账本ID","capability_id":"能力ID","score":0.0,"reason":"理由"}],
"scheduler_question":"当调度器需要询问 Sir 时的自然问题，或 null",
"thoughts":[{"id":"念头编号","kind":"association|inference|question|plan|review|idle",
"text":"念头原文","refs":["输入中真实提供的事件/念头/信念ID或memory:完整ID"],
"question":null}],"resolved_questions":[]}
念头编号由你在本次输出内按顺序自定：第一个念头编号 t1，之后 t2、t3、t4，不得重复；
键唯一：同一对象内不得重复字段；question 每个念头只写一次。
refs 只写输入中真实提供的ID，或本次输出中写在该念头之前的念头编号，不得写自身编号或尚未写出的编号。
relevance 按 relevance_candidates×capabilities 的全部 ID 组合逐对评分，不是只选最相关能力。
除 cached_relevance 已有配对外不得省略；缺评分≠0分。每轮1到4个念头。question类型的question字段必须为
{"text":"疑问","assessments":[{"capability_id":"每项能力ID","can_resolve":true,
"reason":"能提供何种证据或为什么不能"}],"selected_capability":"选中ID或null",
"missing_information":"不调用能力时明确缺少的信息",
"gap_subject":"对象","gap_attribute":"未知方面","gap_importance":0.8,"gap_reason":"为何补足"}。
无信念可绑定须提供上述四项，已有信念用 refs；空白不填答案，下轮按 VOI 调度。
旧 unbound_questions 仍值得查时沿用 text、补四项。
其他类型 question 必须为 null。resolved_questions 仅列输入中已有充分证据解决的疑问ID。
非 idle 念头必须列真实 refs；无依据用 idle（空 refs），不得借无依据念头充证据。
可另返回 outreach_intents 数组（最多2条），用于确实值得主动告知 Sir 的事实或关怀，
不是每轮必须开口。每条仅含 reason（事实原因而非台词）、evidence（输入真实来源ID数组）、
urgency（low/normal/high/emergency，emergency仅身体安危或必须立即办理的急事）、
needs_response（此出口必须false）。需要 Sir 回答的未知信息必须走 question 与调度器复核，
数据源能查到的不得转问 Sir；不以“是否需要我做”请示日常管家事务。话由另一模型阶段现场组织。"""


def recall_view(context: Record) -> Record:
    """Expose one citable id per recall row in the request view.

    Recall rows carry both the ledger ``id`` and a namespaced ``evidence_id``
    (``mind:entries:<id>`` / ``memory:<id>``). Refs accept only the ledger form,
    so advertising the second one invited citations the validator must reject.
    The ledger row keeps every field; only the copy sent to the model changes.
    """
    rows = context.get("recalled")
    if not rows or not any("evidence_id" in row for row in rows):
        return context
    view = deepcopy(context)
    view["recalled"] = [
        {key: value for key, value in row.items() if key != "evidence_id"} for row in rows
    ]
    return view


def bound_context(context: Record, *, reserved_bytes: int = 256) -> Record:
    """Allocate the inner request view, preserving a recall floor when possible.

    The fixed-quota allocator applies only when two recalled evidence rows are
    available to protect. An indivisible or oversized floor raises ValueError,
    and the caller then gets the unchanged bounded allocator instead of a view
    that silently lost provenance.
    """
    context = recall_view(context)
    recalled_rows = context.get("recalled", [])
    if len(recalled_rows) >= 2:
        from replay_quota import quota_context

        try:
            return quota_context(context, reserved_bytes=reserved_bytes)
        except ValueError:
            pass
    return bounded_context_baseline(context, reserved_bytes=reserved_bytes)


def bounded_context_baseline(context: Record, *, reserved_bytes: int = 256) -> Record:
    """Bound a request view, never the ledger or the executable capability catalog.

    Omitted origins are not acknowledged: Curiosity only writes belief_origin for
    actual updates, and the daemon must exclude omitted observations from *all*
    delivery/receipt views. Counts describe every removed dependent row as well.
    """
    context = deepcopy(recall_view(context))
    omitted: dict[str, int] = dict(context.get("context_omitted", {}))
    context["context_omitted"] = omitted

    def size(value: Any = None) -> int:
        return len(
            json.dumps(context if value is None else value, ensure_ascii=False).encode("utf-8")
        )

    def remove(key: str, predicate: Callable[[Record], bool]) -> None:
        before = context.get(key, [])
        after = [row for row in before if not predicate(row)]
        if len(before) != len(after):
            omitted[key] = omitted.get(key, 0) + len(before) - len(after)
            context[key] = after

    def drop_identity(identity: str) -> None:
        for key in ("beliefs", "relevance_candidates", "cached_relevance"):
            remove(key, lambda row: row.get("belief_id") == identity)
        remove("pending_memories", lambda row: row.get("origin_id") == identity)
        remove(
            "pending_observations", lambda row: row["payload"]["data"].get("belief_id") == identity
        )

    original_catalog = context.get("capabilities", [])
    catalog = context_capabilities(original_catalog)
    compacted = sum(a != b for a, b in zip(original_catalog, catalog))
    if compacted:
        omitted["capability_details"] = omitted.get("capability_details", 0) + compacted
    context["capabilities"] = catalog
    # Catalog growth must not consume the entire prompt. Leave a working share
    # for observations; validation/scheduling use exactly this advertised subset.
    # Minimum useful evidence reserve; this is a partition of the shared limit,
    # not another transport limit. Count omitted catalog rows, do not inflate it.
    catalog_budget = max(
        0, INNER_CONTEXT_BYTE_LIMIT - size(context.get("instructions", "")) - reserved_bytes - 1600
    )
    if catalog and size(catalog) > catalog_budget:
        offset = (int(context.get("context_round", 1)) - 1) % len(catalog)
        catalog[:] = catalog[offset:] + catalog[:offset]
    while catalog and size(catalog) > catalog_budget:
        removed = catalog.pop()
        omitted["capabilities"] = omitted.get("capabilities", 0) + 1
        remove("cached_relevance", lambda row: row.get("capability_id") == removed["id"])

    # Provenance checks must still find the source of every exposed observation.
    from curiosity import event_evidence

    for event in list(context.get("pending_observations", [])):
        data = event["payload"]["data"]
        if data.get("sense_event"):
            supported = event_evidence(event, catalog) is not None
        else:
            supported = event["source"] in {c["id"] for c in catalog} or (
                event["payload"]["kind"] == "conversation"
                and event["source"] in {"chat", "sir", "owner"}
            )
        if not supported:
            remove("pending_observations", lambda row: row["id"] == event["id"])
            if data.get("belief_id"):
                drop_identity(data["belief_id"])

    optional = (
        "thoughts",
        "recent_thoughts",
        "recalled",
        "known_gap_identities",
        "memories",
        "unresolved_questions",
        "unbound_questions",
        "events",
        "lessons",
        "daemon_pending_senses",
    )

    # 2026-10-19: a protected recall floor and post-trim refill were tried and
    # reverted after live thought success fell (~75% -> 50%). Trimming order is
    # the pre-R2 one; recall is optional like other history lists. Any future
    # allocation change must budget system+history+skills+user together.
    #
    # Two rows are not history and are trimmed only after everything else is
    # gone: this round's clock event and beliefs already waiting on Sir.
    # Without the clock the model has no "now" and no citable event, so every
    # thought must be an ungrounded idle; without a waiting gap in view it
    # cannot see that Sir was already asked and re-asks the same question.
    anchor = next(
        (
            row
            for row in reversed(context.get("events", []))
            if row.get("payload", {}).get("kind") == "time"
        ),
        None,
    )
    waiting = {
        b["belief_id"]
        for b in context.get("beliefs", [])
        if b.get("attention", {}).get("status") == "awaiting_sir"
    }

    def clock_only(key: str) -> bool:
        rows = context.get(key, [])
        return (
            key == "events"
            and anchor is not None
            and bool(rows)
            and all(row is anchor for row in rows)
        )

    while size() + reserved_bytes > INNER_CONTEXT_BYTE_LIMIT:
        keys = [k for k in optional if context.get(k)]
        trimmable = [k for k in keys if not clock_only(k)]
        if trimmable:
            key = max(trimmable, key=lambda k: size(context[k]))
            if key == "events":
                index = next(i for i, row in enumerate(context["events"]) if row is not anchor)
                context["events"].pop(index)
            else:
                context[key].pop(0 if key not in {"memories", "lessons"} else -1)
            omitted[key] = omitted.get(key, 0) + 1
            continue
        candidates = context.get("relevance_candidates", [])
        candidate_ids = {b["belief_id"] for b in candidates}
        extra = [b for b in context.get("beliefs", []) if b["belief_id"] not in candidate_ids]
        # A gap already waiting on Sir is not history: dropping it hides the
        # pending question and the model re-asks. Ordinary extras go first, and
        # the stale relevance cache is cheaper to lose than the waiting state.
        ordinary = [b for b in extra if b["belief_id"] not in waiting]
        if ordinary:
            target = ordinary[-1]["belief_id"]
            remove("beliefs", lambda row, target=target: row["belief_id"] == target)
        elif candidates:
            observations = context.get("pending_observations", [])
            observation_ids = {e["payload"]["data"].get("belief_id") for e in observations}
            unrelated = [b for b in candidates if b["belief_id"] not in observation_ids]
            # A waiting gap that just regained evidence is a candidate again, and
            # dropping it would hide the pending question. Ordinary unrelated
            # candidates go first; the waiting state goes only after the stale
            # relevance cache and unused capability metadata are spent.
            droppable = [b for b in unrelated if b["belief_id"] not in waiting]

            def shrink_catalog(observations: list[Record]) -> bool:
                """Drop one catalog row no exposed observation needs."""
                if len(catalog) <= 1:
                    return False
                sources = {e["source"] for e in observations}
                sources.update(
                    e["payload"]["data"].get("evidence", {}).get("capability_id")
                    for e in observations
                )
                expendable = [
                    c
                    for c in catalog
                    if c["id"] not in sources
                    and all(
                        event_evidence(e, [row for row in catalog if row["id"] != c["id"]])
                        is not None
                        for e in observations
                        if e["payload"]["data"].get("sense_event")
                    )
                ]
                if not expendable:
                    return False
                removed = expendable[-1]
                catalog.remove(removed)
                omitted["capabilities"] = omitted.get("capabilities", 0) + 1
                remove(
                    "cached_relevance",
                    lambda row, removed=removed: row.get("capability_id") == removed["id"],
                )
                return True

            if droppable and (len(candidates) > 1 or len(catalog) <= 1):
                drop_identity(droppable[-1]["belief_id"])
            # Keep one complete evidence unit usable instead of spending the
            # entire budget on capability metadata and producing empty thoughts.
            elif len(candidates) == 1 and len(catalog) > 1 and shrink_catalog(observations):
                pass
            elif unrelated and context.get("cached_relevance"):
                context["cached_relevance"].pop()
                omitted["cached_relevance"] = omitted.get("cached_relevance", 0) + 1
            elif unrelated and shrink_catalog(observations):
                pass
            elif unrelated and (len(candidates) > 1 or len(catalog) <= 1):
                drop_identity(unrelated[-1]["belief_id"])
            else:
                candidate = max(
                    candidates,
                    key=lambda b: (
                        size(b)
                        + sum(
                            size(e)
                            for e in observations
                            if e["payload"]["data"].get("belief_id") == b["belief_id"]
                        )
                    ),
                )
                drop_identity(candidate["belief_id"])
        elif context.get("pending_observations"):
            event = context["pending_observations"][-1]
            remove("pending_observations", lambda row: row["id"] == event["id"])
            identity = event["payload"]["data"].get("belief_id")
            if identity:
                drop_identity(identity)
        elif context.get("pending_memories"):
            remove("pending_memories", lambda row: row is context["pending_memories"][-1])
        elif context.get("cached_relevance"):
            context["cached_relevance"].pop()
            omitted["cached_relevance"] = omitted.get("cached_relevance", 0) + 1
        elif extra:
            # Only waiting gaps are left; the state row goes before the catalog.
            target = extra[-1]["belief_id"]
            remove("beliefs", lambda row, target=target: row["belief_id"] == target)
        elif catalog:
            removed = catalog.pop()
            omitted["capabilities"] = omitted.get("capabilities", 0) + 1
        elif keys:
            # Last resort: this round's clock row goes rather than an unbounded
            # prompt, but only after every other row is already gone.
            context[keys[0]].pop()
            omitted[keys[0]] = omitted.get(keys[0], 0) + 1
        else:
            raise ValueError("Required context exceeds shared byte budget")
    return context


def unique(pairs: list[tuple[str, Any]]) -> Record:
    result: Record = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def parse_response_json(text: str) -> object:
    """Reuse distiller's lexical repairs, retaining strict duplicate/number guards.

    Do not use parse_model_json directly: it silently overwrites duplicate keys.
    Scan the outer document before any repair; never salvage nested fragments of
    a truncated object or add missing JSON delimiters/values.
    """
    from memory_distiller import _json_document, _without_trailing_commas

    if not isinstance(text, str):
        raise ValueError("Model output must be text")
    value = text.lstrip("\ufeff").strip()
    if not value or len(value) > 2 * 1024 * 1024:
        raise ValueError("Model output empty or exceeds bounded parse size")

    def finite_float(token: str) -> float:
        number = float(token)
        if not math.isfinite(number):
            raise ValueError("nonfinite")
        return number

    def decode(body: str) -> object:
        return json.loads(
            body,
            object_pairs_hook=unique,
            parse_float=finite_float,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")),
            strict=False,
        )

    try:
        return decode(value)
    except json.JSONDecodeError:
        pass
    document, incomplete = _json_document(value)
    if incomplete:
        raise ValueError("Model output truncated or has unclosed JSON")
    # Surrounding prose/fences are harmless, but a second JSON document (even
    # incomplete) is not commentary. Never accept a complete prefix of one.
    start = value.find(document)
    suffix = value[start + len(document) :].strip() if start >= 0 else ""
    if any(char in suffix for char in "{["):
        raise ValueError("Multiple model JSON documents")
    return decode(_without_trailing_commas(document))


def validate(
    text: str, allowed_refs: set[str], catalog: list[Record], questions: list[Record]
) -> Record:
    raw = parse_response_json(text)
    if not isinstance(raw, dict):
        raise ValueError("Object required")
    result = cast(Record, raw)
    # v1 fixture models may omit scheduler fields; preserve their test contract.
    result.setdefault("relevance", [])
    result.setdefault("scheduler_question", None)
    result.setdefault("belief_updates", [])
    result.setdefault("knowledge_gaps", [])
    result.setdefault("outreach_intents", [])
    intents = result["outreach_intents"]
    if not isinstance(intents, list) or len(cast(list[object], intents)) > 2:
        raise ValueError("Bounded outreach intents required")
    for raw_intent in cast(list[object], intents):
        if not isinstance(raw_intent, dict):
            raise ValueError("Outreach intent object")
        intent = cast(Record, raw_intent)
        if (
            set(intent) != {"reason", "evidence", "urgency", "needs_response"}
            or not isinstance(intent["reason"], str)
            or not 1 <= len(intent["reason"].strip()) <= 500
            or any(c in intent["reason"] for c in "\r\n")
            or intent["urgency"] not in {"low", "normal", "high", "emergency"}
            or intent["needs_response"] is not False
            or not isinstance(intent["evidence"], list)
            or not 1 <= len(cast(list[object], intent["evidence"])) <= 8
            or any(
                not isinstance(r, str) or r not in allowed_refs
                for r in cast(list[object], intent["evidence"])
            )
        ):
            raise ValueError("Invalid outreach intent or provenance; questions require review")
    validate_gaps(result["knowledge_gaps"])
    if set(result) != {
        "thoughts",
        "resolved_questions",
        "relevance",
        "scheduler_question",
        "belief_updates",
        "knowledge_gaps",
        "outreach_intents",
    }:
        raise ValueError("Envelope fields")
    relevance = result["relevance"]
    if not isinstance(relevance, list):
        raise ValueError("Relevance list")
    for item in cast(list[object], relevance):
        if not isinstance(item, dict):
            raise ValueError("Relevance object")
        row = cast(Record, item)
        score = row["score"]
        if (
            set(row) != {"belief_id", "capability_id", "score", "reason"}
            or not isinstance(row["belief_id"], str)
            or not isinstance(row["capability_id"], str)
            or type(score) not in {int, float}
            or not math.isfinite(float(score))
            or not 0 <= float(score) <= 1
            or not isinstance(row["reason"], str)
            or not row["reason"].strip()
        ):
            raise ValueError("Invalid relevance")
    if result["scheduler_question"] is not None and not isinstance(
        result["scheduler_question"], str
    ):
        raise ValueError("Invalid scheduler question")
    thoughts = result["thoughts"]
    if not isinstance(thoughts, list) or not 1 <= len(cast(list[object], thoughts)) <= 4:
        raise ValueError("Bounded thoughts required")
    local_ids: set[str] = set()
    dropped_ids: set[str] = set()
    retained: list[Record] = []
    degradations: list[Record] = []
    # Ref cleanups that drop no thought; kept apart so they do not defer
    # resolutions or count as dropped thoughts in round accounting.
    ref_normalizations: list[Record] = []
    expected = {c["id"] for c in catalog}
    open_question_ids = {
        q["question_id"]
        for q in questions
        if isinstance(q, dict) and isinstance(q.get("question_id"), str)
    }
    for raw_thought in cast(list[object], thoughts):
        # Coerce bare strings to minimal thought objects.
        # Lean models sometimes emit a string instead of the documented object.
        if isinstance(raw_thought, str):
            raw_thought = cast(
                Record,
                {
                    "id": "t1",
                    "kind": "reflection",
                    "text": raw_thought,
                    "refs": [],
                    "question": None,
                },
            )
        if not isinstance(raw_thought, dict):
            raise ValueError("Thought object")
        thought = cast(Record, raw_thought)
        # Drop unexpected keys instead of rejecting the whole round.
        allowed_thought_keys = {"id", "kind", "text", "refs", "question"}
        thought = {k: v for k, v in thought.items() if k in allowed_thought_keys}
        # A non-question has no question payload. Normalizing its omitted null
        # changes no evidence; question-kind thoughts still require the object.
        if thought.get("kind") in KINDS - {"question"}:
            thought.setdefault("question", None)
        if set(thought) != allowed_thought_keys:
            raise ValueError("Thought fields")
        identity, kind, body, refs = (thought[k] for k in ("id", "kind", "text", "refs"))
        if (
            not isinstance(identity, str)
            or identity not in {"t1", "t2", "t3", "t4"}
            or identity in local_ids
            or kind not in KINDS
            or not isinstance(body, str)
            or not body.strip()
            or len(body) > 3000
            or not isinstance(refs, list)
        ):
            raise ValueError("Invalid thought or provenance")
        # Recalled rows include raw ledger thoughts/events, but the protocol also
        # documents a memory: prefix. Models sometimes prefix a raw ledger ID.
        # Rewrite only when the prefixed form is NOT a supplied ID and the raw ID
        # IS one: an unambiguous alias, never a new or widened reference.
        refs = [
            r[len("memory:") :]
            if isinstance(r, str)
            and r.startswith("memory:")
            and r not in allowed_refs
            and r[len("memory:") :] in allowed_refs
            else r
            for r in cast(list[object], refs)
        ]
        # Recall exposes each ledger row twice: the raw `id` and the namespaced
        # `evidence_id`. Models cite the visible namespace form, so strip that
        # exact alias only when the target ID was already supplied this round.
        # Never chain aliases, consult the ledger, or widen allowed_refs.
        entry_prefix = "mind:entries:"
        entry_aliases = [
            r
            for r in refs
            if isinstance(r, str)
            and r.startswith(entry_prefix)
            and r not in allowed_refs
            and r[len(entry_prefix) :] in allowed_refs
        ]
        if entry_aliases:
            refs = [
                r[len(entry_prefix) :] if isinstance(r, str) and r in entry_aliases else r
                for r in refs
            ]
            ref_normalizations.append(
                {
                    "thought_id": identity,
                    "action": "refs_normalized",
                    "reason": "recalled_evidence_alias",
                    "count": len(entry_aliases),
                }
            )
        # Models also compose both namespaces (`memory:mind:entries:<id>`). Accept
        # exactly that one composition, and only when the bare id was supplied
        # this round. One step, no chaining: the result is never re-normalized.
        combo_prefix = "memory:" + entry_prefix
        combo_aliases = [
            r
            for r in refs
            if isinstance(r, str)
            and r.startswith(combo_prefix)
            and r not in allowed_refs
            and r[len(combo_prefix) :] in allowed_refs
        ]
        if combo_aliases:
            refs = [
                r[len(combo_prefix) :] if isinstance(r, str) and r in combo_aliases else r
                for r in refs
            ]
            ref_normalizations.append(
                {
                    "thought_id": identity,
                    "action": "refs_normalized",
                    "reason": "recalled_evidence_alias",
                    "count": len(combo_aliases),
                }
            )
        # Capability IDs name a tool in the catalog, not evidence. Models cite
        # them as refs; remove only such catalog IDs (never add or rewrite a
        # ref). A thought left without refs then follows the normal
        # missing-evidence isolation below; other unknown refs still fail closed.
        capability_refs = [
            r
            for r in refs
            if isinstance(r, str) and r in expected and r not in allowed_refs and r not in local_ids
        ]
        if capability_refs:
            refs = [r for r in refs if r not in capability_refs]
            ref_normalizations.append(
                {
                    "thought_id": identity,
                    "action": "refs_removed",
                    "reason": "capability_id_not_evidence",
                    "count": len(capability_refs),
                }
            )
        # Open question IDs (e.g. scheduler:<gap>) identify what is still
        # unknown; they are a topic, not evidence. Same rule as capability IDs.
        question_refs = [
            r
            for r in refs
            if isinstance(r, str)
            and r in open_question_ids
            and r not in allowed_refs
            and r not in local_ids
        ]
        if question_refs:
            refs = [r for r in refs if r not in question_refs]
            ref_normalizations.append(
                {
                    "thought_id": identity,
                    "action": "refs_removed",
                    "reason": "question_id_not_evidence",
                    "count": len(question_refs),
                }
            )
        thought["refs"] = refs
        if any(not isinstance(r, str) or r not in allowed_refs | local_ids for r in refs):
            raise ValueError("Invalid thought provenance")
        if not isinstance(thought["question"], (dict, type(None))):
            raise ValueError("Invalid question field")
        if kind == "question" and not refs:
            raise ValueError("Evidence required")
        q = cast(Record | None, thought["question"])
        if kind == "question":
            if not isinstance(q, dict):
                raise ValueError("Question object")
            question = q
            fields = {"text", "assessments", "selected_capability", "missing_information"}
            gap_fields = {"gap_subject", "gap_attribute", "gap_importance", "gap_reason"}
            # Drop unrelated keys as before, but never discard partial/mixed gap formats.
            question = {k: v for k, v in q.items() if k in fields | {"gap"} | gap_fields}
            flat_fields = set(question) & gap_fields
            if flat_fields:
                if flat_fields != gap_fields or "gap" in question:
                    raise ValueError("Question gap fields")
                question["gap"] = {
                    key: question.pop("gap_" + key)
                    for key in ("subject", "attribute", "importance", "reason")
                }
            if set(question) not in (fields, fields | {"gap"}):
                raise ValueError("Question fields")
            if "gap" in question:
                validate_gaps([question["gap"]])
            if flat_fields:
                # Preserve unrelated legacy keys and canonical downstream gap shape.
                thought["question"] = {k: v for k, v in q.items() if k not in gap_fields}
                thought["question"]["gap"] = question["gap"]
            if not isinstance(question["text"], str) or not 1 <= len(question["text"]) <= 1000:
                raise ValueError("Question text")
            assessments = question["assessments"]
            if not isinstance(assessments, list):
                raise ValueError("Assessments required")
            seen: set[str] = set()
            matches: set[str] = set()
            for item in cast(list[object], assessments):
                if not isinstance(item, dict):
                    raise ValueError("Assessment object")
                assessment = cast(Record, item)
                if (
                    set(assessment) != {"capability_id", "can_resolve", "reason"}
                    or assessment["capability_id"] not in expected
                    or assessment["capability_id"] in seen
                    or type(assessment["can_resolve"]) is not bool
                    or not isinstance(assessment["reason"], str)
                    or not assessment["reason"].strip()
                ):
                    raise ValueError("Invalid capability assessment")
                seen.add(assessment["capability_id"])
                if assessment["can_resolve"]:
                    matches.add(assessment["capability_id"])
            selected = question["selected_capability"]
            if (
                seen != expected
                or (matches and selected not in matches)
                or (not matches and selected is not None)
            ):
                raise ValueError(
                    "Every capability must be assessed; matching source must be selected"
                )
            if not isinstance(question["missing_information"], str) or (
                selected is None and not question["missing_information"].strip()
            ):
                raise ValueError("Missing information explanation")
        elif q is not None:
            raise ValueError("Unexpected question")
        local_ids.add(identity)
        # Isolate evidence failures only after all shape/question checks. Never
        # invent refs or retain a grounded kind whose local support was dropped.
        # Conservatively drop mixed dependencies too, rather than rewriting refs.
        reason = (
            "missing_evidence"
            if kind not in {"idle", "question"} and not refs
            else "unsupported_dependency"
            if any(r in dropped_ids for r in refs)
            else None
        )
        if reason:
            dropped_ids.add(identity)
            degradations.append(
                {
                    "thought_id": identity,
                    "kind": kind,
                    "action": "dropped",
                    "reason": reason,
                }
            )
        else:
            retained.append(thought)
            if kind == "idle":
                # Idle may be retained as a thought, but is never grounding for
                # a later claim: it represents no evidenced proposition.
                dropped_ids.add(identity)
    result["thoughts"] = retained
    if ref_normalizations:
        result["ref_normalizations"] = ref_normalizations
    resolved = result["resolved_questions"]
    unresolved_ids = {q["question_id"] for q in questions}
    if not isinstance(resolved, list) or any(
        not isinstance(q, str) or q not in unresolved_ids for q in cast(list[object], resolved)
    ):
        raise ValueError("Invalid resolution reference")
    if degradations:
        # Resolutions have no per-item provenance in the wire format. Defer
        # them rather than attributing a dropped chain to independent thoughts.
        if resolved:
            degradations.append(
                {
                    "field": "resolved_questions",
                    "action": "deferred",
                    "reason": "thought_evidence_incomplete",
                    "count": len(resolved),
                }
            )
            result["resolved_questions"] = []
        result["validation_degradations"] = degradations
    return result


class InnerLoop:
    def __init__(
        self,
        store: MindStore,
        model: Callable[[str], Record],
        *,
        max_rounds: int = 30,
        max_model_calls: int = 35,
        active_seconds: float = 1,
        idle_seconds: float = 15,
        catalog: list[Record] | None = None,
        executor: Callable[[Record], Record] = capabilities.execute,
    ) -> None:
        if type(max_rounds) is not int or not 1 <= max_rounds <= 30:
            raise ValueError("Round budget must be 1..30")
        if any(not math.isfinite(v) or v < 0 for v in (active_seconds, idle_seconds)):
            raise ValueError("Invalid cadence")
        self.store, self.model = store, model
        self._prepare_runtime()
        self.max_rounds = max_rounds
        if type(max_model_calls) is not int or not 1 <= max_model_calls <= 40:
            raise ValueError("Model call budget must be 1..40")
        self.max_model_calls = max_model_calls
        self.active_seconds, self.idle_seconds = active_seconds, idle_seconds
        self.catalog = (
            capabilities.catalog() + capabilities.amap_catalog() if catalog is None else catalog
        )
        self.executor = executor
        self.memories = product_memory.MemoryStore(store.root / "memory.db")
        self.curiosity = Curiosity(store)
        self.wakeup = threading.Event()
        self.stopping = threading.Event()

    def ingest(self, kind: str, payload: Record, *, source: str) -> Record:
        # Strip sensor payloads before the durable ledger or model context sees them.
        if isinstance(payload.get("sense_event"), dict):
            raw = cast(Record, payload["sense_event"])
            data = cast(Record, raw.get("payload", {}))
            summary = capabilities.summarize_event(
                str(raw.get("source", "")),
                str(raw.get("kind", "")),
                data,
                str(raw.get("observed_at", "")),
            )
            background = (
                data.get("synthetic") is True
                or raw.get("synthetic") is True
                or raw.get("kind") == "backfill_summary"
                or data.get("backfill") is True
                or raw.get("backfill") is True
                or raw.get("background_only") is True
                or payload.get("synthetic") is True
                or payload.get("backfill") is True
                or payload.get("background_only") is True
            )
            payload = {
                **{k: payload[k] for k in ("daemon_event_id", "timeline_id") if k in payload},
                "sense_event": {
                    "source": summary["source"],
                    "kind": summary.get("kind", "unknown"),
                    "observed_at": summary["sampled_at"],
                    "payload": summary,
                    "background_only": background,
                },
            }
        event = self.store.ingest(kind, payload, source=source)
        self.wakeup.set()
        return event

    def _prepare_runtime(self) -> None:
        with self.store.connection() as db:
            db.create_function(
                "runtime_normalized", 1, product_memory.normalized, deterministic=True
            )
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runtime_question_text_v1 (
                    text TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS runtime_question_unbound_v1 (
                    text TEXT PRIMARY KEY, seq INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_projection_cursor_v1 (
                    name TEXT PRIMARY KEY, seq INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_question_latest_v1 (
                    question_id TEXT PRIMARY KEY, first_seq INTEGER NOT NULL,
                    seq INTEGER NOT NULL, state TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runtime_question_latest_order_v1
                    ON runtime_question_latest_v1(first_seq);
            """)
        self._sync_runtime()

    def _sync_runtime(self) -> None:
        # Incrementally materialize exact normalized text and latest state. No
        # Python UDF in a persistent trigger: unrelated SQLite writers stay safe.
        with self.store.connection() as db:
            row = db.execute(
                "SELECT seq FROM runtime_projection_cursor_v1 WHERE name='questions'"
            ).fetchone()
            cutoff = int(row[0]) if row else 0
            cursor = db.execute(
                "SELECT seq,category,payload FROM entries WHERE seq>? "
                "AND category IN ('action_request','question_state') ORDER BY seq",
                (cutoff,),
            )
            while batch := cursor.fetchmany(256):
                for row in batch:
                    payload = json.loads(row["payload"])
                    if row["category"] == "action_request":
                        question = payload.get("question")
                        if question:
                            db.execute(
                                "INSERT OR IGNORE INTO runtime_question_text_v1 VALUES(?)",
                                (product_memory.normalized(question["text"]),),
                            )
                    else:
                        db.execute(
                            "INSERT INTO runtime_question_latest_v1 VALUES(?,?,?,?) "
                            "ON CONFLICT(question_id) DO UPDATE "
                            "SET seq=excluded.seq,state=excluded.state",
                            (payload["question_id"], row["seq"], row["seq"], payload["state"]),
                        )
            seq = int(db.execute("SELECT COALESCE(MAX(seq),0) FROM entries").fetchone()[0])
            db.execute(
                "INSERT INTO runtime_projection_cursor_v1 VALUES('questions',?) "
                "ON CONFLICT(name) DO UPDATE SET seq=excluded.seq",
                (seq,),
            )

        # A separate cursor backfills stores whose question index predates this
        # recovery projection, then only visits newly appended requests.
        with self.store.connection() as db:
            cursor = db.execute(
                "SELECT seq FROM runtime_projection_cursor_v1 WHERE name='unbound_questions'"
            ).fetchone()
        for row in self.store.iter_entries(
            "action_request", after_seq=int(cursor[0]) if cursor else 0
        ):
            payload = row["payload"]
            text = product_memory.normalized(payload["question"]["text"])
            with self.store.connection() as db:
                if payload.get("belief_id") is None:
                    db.execute(
                        "INSERT OR IGNORE INTO runtime_question_unbound_v1 VALUES (?,?)",
                        (text, row["seq"]),
                    )
                else:
                    db.execute("DELETE FROM runtime_question_unbound_v1 WHERE text=?", (text,))
                db.execute(
                    "INSERT OR REPLACE INTO runtime_projection_cursor_v1 VALUES (?,?)",
                    ("unbound_questions", row["seq"]),
                )

    def questions(self, limit: int | None = None) -> list[Record]:
        self._sync_runtime()
        with self.store.connection() as db:
            sql = (
                "SELECT e.payload FROM runtime_question_latest_v1 q JOIN entries e ON e.seq=q.seq "
                "WHERE q.state!='resolved' ORDER BY q.first_seq DESC"
            )
            args: tuple[int, ...] = () if limit is None else (limit,)
            if limit is not None:
                sql += " LIMIT ?"
            return [json.loads(row[0]) for row in reversed(db.execute(sql, args).fetchall())]

    def context(self, *, bounded: bool = True) -> Record:
        events = self.store.entries("event", 8)
        thoughts = self.store.entries("thought", 6)
        queries = [r["payload"].get("text", "") for r in thoughts[-3:]]
        queries.extend(json.dumps(r["payload"], ensure_ascii=False) for r in events[-3:])
        facts: dict[str, Record] = {}
        for query in queries:
            for fact in self.memories.search(query, limit=6):
                facts[fact["id"]] = {k: fact[k] for k in ("id", "content", "created_at", "version")}
        # Recent active facts seed idle thought when the inbox only has a clock.
        for fact in self.memories.list()[:4]:
            facts.setdefault(
                fact["id"], {k: fact[k] for k in ("id", "content", "created_at", "version")}
            )
        from curiosity import INSTRUCTIONS as CURIOSITY_INSTRUCTIONS

        stream = self.curiosity.context(
            [
                {k: f[k] for k in ("id", "content", "created_at", "version")}
                for f in self.memories.list()
            ],
            self.catalog,
            self.thought_count() + 1,
        )
        self._sync_runtime()
        with self.store.connection() as db:
            unbound = [
                {"question_id": p["question_id"], "text": p["question"]["text"][:1000]}
                for row in db.execute(
                    "SELECT e.payload FROM runtime_question_unbound_v1 u "
                    "JOIN entries e ON e.seq=u.seq ORDER BY u.seq DESC LIMIT 4"
                )
                for p in [json.loads(row[0])]
            ]
        raw_context: Record = {
            "context_round": self.thought_count() + 1,
            "unbound_questions": unbound,
            "instructions": INSTRUCTIONS + "\n" + CURIOSITY_INSTRUCTIONS,
            "events": events,
            "thoughts": thoughts,
            "memories": list(facts.values())[:8],
            "capabilities": self.catalog,
            "unresolved_questions": self.questions()[-10:],
            **stream,
        }
        return self.context_relevance_cache(bound_context(raw_context)) if bounded else raw_context

    def context_relevance_cache(self, context: Record, *, reserved_bytes: int = 256) -> Record:
        """Read cache with the final advertised subset, not the pre-budget catalog.

        Adding a cached row must not trigger another schema-changing trim pass.
        Omitted cache is only an optimization: the model scores its missing rows.
        """
        from curiosity import fingerprint

        catalog = self.context_catalog(context)
        rows = context.setdefault("cached_relevance", [])
        known = {(r["belief_id"], r["capability_id"]) for r in rows}
        pending_ids = {
            e["payload"]["data"].get("belief_id") for e in context.get("pending_observations", [])
        }
        beliefs = {b["belief_id"]: b for b in self.store.beliefs()}
        for candidate in context.get("relevance_candidates", []):
            belief = beliefs.get(candidate["belief_id"])
            if belief is None or candidate["belief_id"] in pending_ids:
                continue
            cached = self.store.recent(
                "relevance_cache", 1, where={"fingerprint": fingerprint(belief, catalog)}
            )
            cached_rows: list[Record] = cached[0]["payload"]["rows"] if cached else []
            for row in cached_rows:
                key = (row["belief_id"], row["capability_id"])
                if key in known:
                    continue
                rows.append(row)
                if (
                    len(json.dumps(context, ensure_ascii=False).encode("utf-8")) + reserved_bytes
                    > INNER_CONTEXT_BYTE_LIMIT
                ):
                    rows.pop()
                    omitted = context["context_omitted"]
                    omitted["cached_relevance"] = omitted.get("cached_relevance", 0) + 1
                else:
                    known.add(key)
        while (
            rows
            and len(json.dumps(context, ensure_ascii=False).encode("utf-8")) + reserved_bytes
            > INNER_CONTEXT_BYTE_LIMIT
        ):
            rows.pop()
            omitted = context["context_omitted"]
            omitted["cached_relevance"] = omitted.get("cached_relevance", 0) + 1
        return context

    def state(
        self, question_id: str, text: str, state: str, *, refs: list[str], detail: str = ""
    ) -> None:
        self.store.append(
            "question_state",
            {"question_id": question_id, "text": text, "state": state, "detail": detail},
            source="mind-router",
            refs=refs,
        )

    def route(self, thought: Record, thought_id: str) -> None:
        """Bind proposals to scored work; never execute or contact Sir here."""
        from curiosity import gap_identity

        question = thought["question"]
        gap = question.get("gap")
        current_beliefs = self.store.beliefs()
        beliefs = {b["belief_id"] for b in current_beliefs}
        targets = [r for r in thought["refs"] if r in beliefs]
        # Explicit labels name the question, not its cited evidence. Reuse an
        # existing semantic label pair rather than creating a dangling gap ID.
        matching = [
            b["belief_id"]
            for b in current_beliefs
            if gap
            and gap_identity(b["subject"], b["attribute"])
            == gap_identity(gap["subject"], gap["attribute"])
        ]
        identity = (
            matching[0]
            if len(matching) == 1
            else gap_identity(gap["subject"], gap["attribute"])
            if gap
            else targets[0]
            if len(targets) == 1
            else None
        )
        self._sync_runtime()
        text = product_memory.normalized(question["text"])
        with self.store.connection() as db:
            if identity is not None:
                db.create_function("runtime_normalized", 1, product_memory.normalized)
                direction = product_memory.normalized(
                    gap["attribute"] if gap else question.get("gap_attribute", "")
                )
                seen = db.execute(
                    "SELECT 1 FROM entries WHERE category='action_request' "
                    "AND json_extract(payload,'$.belief_id')=? "
                    "AND runtime_normalized(COALESCE("
                    "json_extract(payload,'$.question.gap.attribute'),"
                    "json_extract(payload,'$.question.gap_attribute'),''))=? LIMIT 1",
                    (identity, direction),
                ).fetchone()
            else:
                # Legacy unbound questions have no semantic identity yet.
                seen = db.execute(
                    "SELECT 1 FROM runtime_question_text_v1 WHERE text=?", (text,)
                ).fetchone()
        if seen:
            return
        self.store.append(
            "action_request",
            {
                "question_id": thought_id,
                "belief_id": identity,
                "question": question,
                "state": "awaiting_voi" if identity else "awaiting_gap_metadata",
            },
            source="model",
            refs=[thought_id],
        )

    def call_model(self, context: Record, round_number: int, phase: str) -> Record | None:
        if self.store.count("model_call") >= self.max_model_calls:
            return None
        from inner_voice import SYSTEM_PROMPT

        # Audit the entire wire context, not just the main instructions. Model
        # generated terms are retained as evidence, never silently scrubbed.
        hits = answer_terms({"system": SYSTEM_PROMPT, "context": context})
        static = {
            "system": SYSTEM_PROMPT,
            "instructions": context.get("instructions"),
            "capabilities": self.catalog,
            "seed_memories": [m["content"] for m in self.memories.list()],
        }
        violations = answer_terms(static)
        self.store.append(
            "context_scan",
            {
                "round": round_number,
                "phase": phase,
                "full_context_hits": hits,
                "static_violations": violations,
            },
            source="mind-guard",
        )
        if violations:
            self.store.append("context_error", {"reason": "answer_term_in_source"}, source="mind")
            return None
        prompt = json.dumps(context, ensure_ascii=False)
        if len(prompt.encode("utf-8")) > INNER_CONTEXT_BYTE_LIMIT:
            self.store.append("context_error", {"reason": "request_exceeds_limit"}, source="mind")
            return None
        call = self.store.append(
            "model_call",
            {
                "round": round_number,
                "phase": phase,
                "state": "dispatched_unknown",
                "retry_allowed": False,
                "context": context,
            },
            source="mind",
        )
        try:
            response = self.model(prompt)
        except Exception as exc:
            self.store.append(
                "model_error",
                {
                    "call_id": call["id"],
                    "retry_allowed": False,
                    "error_type": exc.error_type
                    if isinstance(exc, ProductRequestFailure)
                    else type(exc).__name__,
                    "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
                },
                source="mind",
                refs=[call["id"]],
            )
            return None
        self.store.append(
            "model_response", {"call_id": call["id"], **response}, source="model", refs=[call["id"]]
        )
        return {"call_id": call["id"], **response}

    def generate_question(self, decision: Record, round_number: int) -> None:
        identity = decision.get("question_id") or "scheduler:" + decision["belief_id"]
        if self.store.exists("question_generation_intent", where={"question_id": identity}):
            return
        request = {
            "instructions": QUESTION_INSTRUCTIONS,
            "decision": decision,
            "belief": next(
                (b for b in self.store.beliefs() if b["belief_id"] == decision["belief_id"]), None
            ),
            "capabilities": self.catalog,
        }
        intent = self.store.append(
            "question_generation_intent",
            {"question_id": identity, "round": round_number, "decision": decision},
            source="curiosity-scheduler",
        )
        response = self.call_model(request, round_number, "question")
        try:
            if response is None:
                raise ValueError("unavailable")
            raw: object = json.loads(response["text"], object_pairs_hook=unique)
            if not isinstance(raw, dict):
                raise ValueError("question object")
            parsed = cast(Record, raw)
            if (
                set(parsed) != {"question"}
                or not isinstance(parsed["question"], str)
                or not 1 <= len(parsed["question"].strip()) <= 1000
            ):
                raise ValueError("question language")
        except (ValueError, TypeError, KeyError):
            self.store.append(
                "question_generation_failure",
                {
                    "question_id": identity,
                    "round": round_number,
                    "reason": "model_or_validation_failure",
                    "retry_allowed": False,
                },
                source="curiosity-scheduler",
                refs=[intent["id"]],
            )
            return
        # Canonical self-reports are not memory.db facts or a budgeted prompt
        # view. Keep every version, including superseded assertions for review.
        import sqlite3
        from contextlib import closing

        with self.store.connection() as db:
            self_reports = [
                {**dict(row), "refs": json.loads(row["refs"])}
                for row in db.execute(
                    "SELECT * FROM belief_versions WHERE source='self_report' ORDER BY seq"
                )
            ]
        profile_path = self.store.root / "sir_profile.db"
        if profile_path.exists():
            with closing(sqlite3.connect(profile_path.as_uri() + "?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                self_reports.extend(
                    {**dict(row), "evidence_ids": json.loads(row["evidence_ids"])}
                    for row in db.execute(
                        "SELECT * FROM facts WHERE source='self_report' ORDER BY created_at,id"
                    )
                )
        belief = cast(Record, request["belief"] or {})
        try:
            review = self.call_model(
                {
                    "instructions": QUESTION_REVIEW_INSTRUCTIONS,
                    "proposed_question": parsed["question"],
                    "gap_subject": belief.get("subject"),
                    "self_reports": self_reports,
                    "beliefs": self.store.beliefs(),
                    "conflicts": self.store.recent("conflict", REVIEW_CONFLICT_LIMIT),
                    "as_of": intent["created_at"],
                },
                round_number,
                "question_review",
            )
        except ValueError:
            # A full evidence block that cannot fit must fail closed, not allow
            # a question after a silently abridged review.
            review = None
        try:
            if review is None:
                raise ValueError("review unavailable")
            judged: object = json.loads(review["text"], object_pairs_hook=unique)
            if not isinstance(judged, dict):
                raise ValueError("review object")
            verdict = cast(Record, judged)
            if (
                set(verdict) != {"repeats_self_report", "reason"}
                or type(verdict["repeats_self_report"]) is not bool
                or not isinstance(verdict["reason"], str)
                or not verdict["reason"].strip()
            ):
                raise ValueError("review fields")
        except (ValueError, TypeError, KeyError):
            self.store.append(
                "question_generation_failure",
                {
                    "question_id": identity,
                    "round": round_number,
                    "reason": "review_unavailable_or_invalid",
                    "retry_allowed": False,
                },
                source="curiosity-scheduler",
                refs=[intent["id"]],
            )
            return
        if verdict["repeats_self_report"]:
            self.store.append(
                "question_rejected",
                {
                    "question_id": identity,
                    "round": round_number,
                    "reason": "repeats_self_report",
                    "judgment": verdict["reason"],
                    "question": parsed["question"],
                },
                source="curiosity-scheduler",
                refs=[intent["id"], review["call_id"]],
            )
            return
        self.state(
            identity,
            parsed["question"],
            decision["kind"],
            refs=[intent["id"], response["call_id"]],
            detail=decision["reason"],
        )

    def execute_scheduled(self, decision: Record) -> None:
        round_number = decision["round"]
        if self.store.exists("action", where={"round": round_number}):
            raise ValueError("Only one action per round")
        capability = next(c for c in self.catalog if c["id"] == decision["capability_id"])
        if not capabilities.recognized(capability) or not capability.get("available", True):
            self.store.append(
                "action_blocked", {"decision": decision}, source="curiosity-scheduler"
            )
            return
        belief = next(
            (b for b in self.store.beliefs() if b["belief_id"] == decision["belief_id"]), None
        )
        intent = self.store.append(
            "action",
            {
                "belief_id": decision["belief_id"],
                "belief_revision": belief["revision"] if belief else None,
                "question_id": decision.get("question_id"),
                "capability_id": capability["id"],
                "round": round_number,
                "state": "intent",
            },
            source="curiosity-scheduler",
            refs=[decision["belief_id"]],
        )
        try:
            result = self.executor(capability)
        except Exception:
            self.store.append(
                "action_error",
                {"action_id": intent["id"], "retry_allowed": False},
                source="curiosity-scheduler",
                refs=[intent["id"]],
            )
            return
        if result.get("status") == "unavailable":
            capability["available"] = False
        event = self.ingest(
            "tool_result",
            {
                "action_id": intent["id"],
                "belief_id": decision["belief_id"] if belief else None,
                "question_id": decision.get("question_id"),
                "result": result,
            },
            source=capability["id"],
        )
        if decision.get("question_id"):
            self.state(
                decision["question_id"],
                decision["text"],
                "evidence_available",
                refs=[intent["id"], event["id"]],
            )

    def thought_count(self) -> int:
        # Missing phase means legacy thought; explicit JSON null is not missing.
        with self.store.connection() as db:
            return int(
                db.execute(
                    "SELECT COUNT(*) FROM entries WHERE category='model_call' AND "
                    "COALESCE(json_extract(payload,'$.phase'), "
                    "CASE WHEN json_type(payload,'$.phase') IS NULL THEN 'thought' END)='thought'"
                ).fetchone()[0]
            )

    def context_catalog(self, context: Record) -> list[Record]:
        """Full trusted descriptors for only the IDs advertised in this prompt."""
        if "capabilities" not in context:
            return self.catalog
        ids = {row["id"] for row in context["capabilities"]}
        return [row for row in self.catalog if row["id"] in ids]

    @staticmethod
    def outreach_refs(context: Record) -> set[str]:
        allowed = {r["id"] for key in ("events", "thoughts") for r in context[key]}
        allowed.update("memory:" + r["id"] for r in context["memories"])
        allowed.update(b["belief_id"] for b in context["beliefs"])
        allowed.update(r["id"] for r in context["recent_thoughts"])
        allowed.update(r["id"] for r in context["recalled"])
        allowed.update(e["id"] for e in context["pending_observations"])
        allowed.update(b["belief_id"] for b in context["relevance_candidates"])
        allowed.update(m["origin_id"] for m in context["pending_memories"])
        return allowed

    def reconcile_outreach(self) -> None:
        """Replay durable thought responses; no model/action is re-executed."""
        cursor = self.store.recent("outreach_reconciled", 1)
        after = int(cursor[0]["payload"]["response_seq"]) if cursor else 0
        for response in self.store.iter_entries("model_response", after_seq=after):
            call_id = response["payload"]["call_id"]
            call = self.store.get(call_id)
            if call is not None and call["payload"].get("phase", "thought") == "thought":
                context = call["payload"]["context"]
                parsed: Record
                try:
                    parsed = validate(
                        response["payload"]["text"],
                        self.outreach_refs(context),
                        self.context_catalog(context),
                        context["unresolved_questions"],
                    )
                    validate_updates(parsed["belief_updates"])
                    validate_matrix(parsed["relevance"], context, self.context_catalog(context))
                except (ValueError, TypeError, KeyError):
                    parsed = {"outreach_intents": []}
                for index, intent in enumerate(parsed["outreach_intents"]):
                    identity = call_id + ":outreach:" + str(index)
                    if not self.store.exists("outreach_intent", where={"id": identity}):
                        self.store.append(
                            "outreach_intent",
                            {"id": identity, **intent},
                            source="model",
                            refs=[call_id, *intent["evidence"]],
                        )
            self.store.append(
                "outreach_reconciled",
                {"response_seq": response["seq"]},
                source="mind",
                refs=[response["id"]],
            )

    def step(self) -> bool:
        calls = self.store.count("model_call")
        rounds = self.thought_count()
        if rounds >= self.max_rounds or calls >= self.max_model_calls:
            return False
        round_number = rounds + 1
        self.ingest("time", {"round": round_number}, source="clock")
        context = self.context()
        response = self.call_model(context, round_number, "thought")
        if response is None:
            return self.store.count("model_call") > calls
        call = {"id": response["call_id"]}
        allowed = self.outreach_refs(context)
        validation_field = "response"
        try:
            parsed = validate(
                response["text"],
                allowed,
                self.context_catalog(context),
                context["unresolved_questions"],
            )
            if details := parsed.get("validation_degradations"):
                self.store.append(
                    "validation_degraded",
                    {
                        "call_id": call["id"],
                        "field": "thoughts",
                        "reason": "evidence_isolation",
                        "dropped_count": sum(d["action"] == "dropped" for d in details),
                        "details": details,
                    },
                    source="mind",
                    refs=[call["id"]],
                )
            if cleanups := parsed.get("ref_normalizations"):
                # Separate category from validation_degraded so that its
                # dropped_count accounting for round completion is untouched.
                self.store.append(
                    "ref_normalized",
                    {"call_id": call["id"], "field": "thoughts", "details": cleanups},
                    source="mind",
                    refs=[call["id"]],
                )
            validation_field = "belief_updates"
            updates = validate_updates(parsed.get("belief_updates", []))
            # A belief update may only originate from a pending memory or a
            # pending observation of THIS round. Models sometimes cite an
            # ordinary event ID instead; drop just those updates (never widen
            # admissible origins) rather than discarding the whole round.
            admissible = {m["origin_id"] for m in context.get("pending_memories", [])}
            admissible.update(e["id"] for e in context.get("pending_observations", []))
            rejected = [u for u in updates if u["origin_id"] not in admissible]
            if rejected:
                updates = [u for u in updates if u["origin_id"] in admissible]
                # Key deliberately differs from dropped_count: that field counts
                # dropped THOUGHTS and feeds round-completion accounting.
                self.store.append(
                    "validation_degraded",
                    {
                        "call_id": call["id"],
                        "field": "belief_updates",
                        "reason": "unknown_belief_origin",
                        "dropped_updates": len(rejected),
                    },
                    source="mind",
                    refs=[call["id"]],
                )
            validation_field = "relevance"
            missing_relevance = validate_matrix(
                parsed["relevance"], context, self.context_catalog(context), require_complete=False
            )
            blocked_focus = {
                b["belief_id"]
                for b in context["beliefs"]
                if not b.get("attention", {}).get("eligible_focus", True)
            }
            suppressed_thoughts: set[str] = set()
            clock_refs = {
                row["id"] for row in context["events"] if row["payload"]["kind"] == "time"
            }
            for thought in parsed["thoughts"]:
                refs = set(thought["refs"]) - clock_refs
                if refs and refs.issubset(blocked_focus | suppressed_thoughts):
                    suppressed_thoughts.add(thought["id"])
            accepted_thoughts = [
                t for t in parsed["thoughts"] if t["id"] not in suppressed_thoughts
            ]
            decision: Record = {
                "kind": "associate",
                "reason": "incomplete relevance matrix; deferred",
            }
            if missing_relevance:
                # Missing is unknown, never zero: do not update evidence, cache
                # partial scores, execute, or route an unscored question to Sir.
                self.store.append(
                    "validation_degraded",
                    {
                        "call_id": call["id"],
                        "field": "relevance",
                        "reason": "incomplete_matrix",
                        "missing_count": len(missing_relevance),
                        "scheduling_deferred": True,
                    },
                    source="mind",
                    refs=[call["id"]],
                )
            else:
                validation_field = "belief_update_evidence"
                self.curiosity.update(updates, context, relevance=parsed["relevance"])
                # Newly imported candidates keep their stable identity in this round.
                current = {b["belief_id"]: b for b in self.store.beliefs()}
                active_ids = {b["belief_id"] for b in context["beliefs"]}
                active_ids.update(b["belief_id"] for b in context["relevance_candidates"])
                active_ids.update(m["origin_id"] for m in context["pending_memories"])
                prior_attention = {
                    b["belief_id"]: b.get("attention", {}) for b in context["beliefs"]
                }
                context["beliefs"] = [
                    {**b, "attention": prior_attention.get(identity, {})}
                    for identity, b in current.items()
                    if identity in active_ids
                ]
                for thought in accepted_thoughts:
                    if thought["kind"] == "question":
                        self.route(thought, call["id"] + ":" + thought["id"])
                validation_field = "scheduler"
                decision = self.curiosity.decide(
                    parsed["relevance"], context, self.context_catalog(context), round_number
                )
            # Discovery is retained now and assessed in the next thought, keeping
            # new semantic labels outside the already-validated relevance matrix.
            # The correlated NOT EXISTS re-parsed JSON for every candidate row:
            # at 231k entries it burned minutes of CPU inside SQLite and held the
            # launcher lock, which is why voice turns failed with "数据根被占用".
            # Read the retired set once, then filter in Python.
            with self.store.connection() as db:
                settled = {
                    str(row[0])
                    for row in db.execute(
                        "SELECT json_extract(payload,'$.belief_id') FROM entries "
                        "WHERE category IN ('knowledge_gap','gap_retired')"
                    )
                    if row[0] is not None
                }
                candidates = db.execute(
                    "SELECT payload FROM entries WHERE category='action_request' "
                    "AND json_type(payload,'$.question.gap')='object' "
                    "ORDER BY seq LIMIT 2000"
                ).fetchall()
            deferred = []
            for row in candidates:
                try:
                    body = json.loads(row[0])
                    if str(body.get("belief_id")) in settled:
                        continue
                    deferred.append(body["question"]["gap"])
                except (ValueError, KeyError, TypeError):
                    continue
                if len(deferred) >= 16:
                    break
            validation_field = "knowledge_gaps"
            self.curiosity.discover(
                deferred + parsed["knowledge_gaps"],
                round_number,
                defer_excess=True,
            )
        except (ValueError, KeyError, TypeError) as exc:
            self.store.append(
                "validation_error",
                {
                    "call_id": call["id"],
                    "retry_allowed": False,
                    "field": validation_field,
                    "error_type": type(exc).__name__,
                    "reason": (
                        str(exc)[:200] if isinstance(exc, ValueError) else "invalid_field_or_type"
                    ),
                },
                source="mind",
                refs=[call["id"]],
            )
            return True
        self.reconcile_outreach()
        if decision["kind"] == "execute":
            self.execute_scheduled(decision)
        elif decision["kind"] in {"ask_sir", "pending_sir_permission"}:
            self.generate_question(decision, round_number)
        mappings: dict[str, str] = {}
        blocked_focus = {
            b["belief_id"]
            for b in context["beliefs"]
            if not b.get("attention", {}).get("eligible_focus", True)
        }
        for thought in parsed["thoughts"]:
            refs = [mappings[r] if r in mappings else r for r in cast(list[str], thought["refs"])]
            if thought["id"] in suppressed_thoughts:
                suppressed = self.store.append(
                    "thought_suppressed",
                    {
                        "round": round_number,
                        "call_id": call["id"],
                        "reason": "awaiting_sir_without_new_evidence",
                        "thought": thought,
                    },
                    source="mind-attention",
                    refs=refs,
                )
                mappings[thought["id"]] = suppressed["id"]
                blocked_focus.add(suppressed["id"])
                continue
            row = self.store.append(
                "thought",
                {**thought, "round": round_number, "call_id": call["id"]},
                source="model",
                refs=refs,
            )
            mappings[thought["id"]] = row["id"]
        for identity in parsed["resolved_questions"]:
            question = next(
                q for q in context["unresolved_questions"] if q["question_id"] == identity
            )
            self.state(identity, question["text"], "resolved", refs=list(mappings.values()))
        return True

    def stop(self) -> None:
        self.stopping.set()
        self.wakeup.set()

    def run(self) -> None:
        fd = os.open(
            self.store.root / "mind-loop.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while not self.stopping.is_set() and self.step():
                if self.store.count("model_call") >= self.max_model_calls:
                    break
                active = self.wakeup.is_set()
                self.wakeup.clear()
                self.wakeup.wait(self.active_seconds if active else self.idle_seconds)
        finally:
            os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--key-file", type=Path)
    source.add_argument("--key-fd", type=int)
    parser.add_argument("--acceptance-mode", action="store_true")
    parser.add_argument("--max-rounds", type=int, default=30)
    parser.add_argument("--max-model-calls", type=int, default=35)
    parser.add_argument("--active-seconds", type=float, default=1)
    parser.add_argument("--idle-seconds", type=float, default=15)
    parser.add_argument("--include-sleep", action="store_true")
    parser.add_argument("--no-city-source", action="store_true")
    args = parser.parse_args()
    try:
        root = launcher.chat.home.validate_root(
            args.data_dir, validation_mode="acceptance" if args.acceptance_mode else "product"
        )
        root = launcher.prepare_root(root)
        config = launcher.read_config(args.config, None)
        key = launcher.read_key(key_file=args.key_file, key_fd=args.key_fd)
        loop = InnerLoop(
            MindStore(root),
            ProductModel(config, key, root),
            max_rounds=args.max_rounds,
            max_model_calls=args.max_model_calls,
            active_seconds=args.active_seconds,
            idle_seconds=args.idle_seconds,
            catalog=(
                []
                if args.no_city_source
                else (
                    capabilities.fixture_catalog(include_sleep=True)
                    if args.include_sleep
                    else capabilities.catalog() + capabilities.amap_catalog()
                )
            ),
        )
        loop.run()
        print(
            json.dumps(
                {
                    "ok": True,
                    "model_calls": loop.store.count("model_call"),
                    "thoughts": loop.store.count("thought"),
                }
            )
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        print('{"ok":false,"error":"inner_loop_failed; diagnostics suppressed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
