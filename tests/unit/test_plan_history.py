"""K1-006 offline plan lineage DRAFT, not runtime or durable evidence."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from jarvis.intent import OperationIntent
from jarvis.planner import Plan, Planner, TaskInput


def _plan(root: str = "/fixture/input", request_key: str = "history-task") -> Plan:
    return Planner().plan(
        {
            "schema_version": 1,
            "request_key": request_key,
            "task_kind": "inventory.generate",
            "input": {"root": root},
            "output": {"name": "inventory.json"},
        }
    )


def test_version_chain_preserves_changed_candidates_without_changing_planner_ids() -> None:
    from jarvis.plan_history import DRAFT_STATUS, RUNTIME_ENABLED, PlanHistory, PlanVersion

    history = PlanHistory()
    original = _plan()
    changed = _plan("/fixture/changed")
    first = history.record_version(original)
    second = history.record_version(changed, parent_version=first)
    third = history.record_version(_plan("/fixture/third"), parent_version=second.version_id)

    assert isinstance(first, PlanVersion)
    assert first.parent_version_id is None
    assert second.parent_version_id == first.version_id
    assert third.parent_version_id == second.version_id
    assert len({first.version_id, second.version_id, third.version_id}) == 3
    assert first.plan_id == second.plan_id == third.plan_id == original.id
    assert changed.id != original.id
    assert second.steps_snapshot == changed.steps
    assert second.steps_snapshot[0].plan_id == changed.id
    assert DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert RUNTIME_ENABLED is False


def test_side_effect_records_use_original_intent_identity_and_version_scope() -> None:
    from jarvis.plan_history import PlanHistory, SideEffectRecord

    history = PlanHistory()
    plan = _plan()
    first = history.record_version(plan)
    second = history.record_version(_plan("/fixture/changed"), first)
    step = plan.steps[0]
    intent = OperationIntent(
        task_id=plan.task_id,
        logical_effect_id=step.logical_effect_id,
        step_id=step.id,
        target_path="out/inventory.json",
        prepared_path="prepared/inventory.json",
        expected_sha256="a" * 64,
        expected_size=42,
        input_snapshot_sha256="b" * 64,
    )
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    effect = SideEffectRecord(
        effect_id="observed-publish-1",
        plan_version_id=first.version_id,
        operation_id=intent.operation_id,
        effect_type=intent.intended_action,
        target=intent.target_path,
        timestamp=timestamp,
    )
    assert history.query_effects(first.version_id) == []
    assert history.record_side_effect(effect) == effect
    assert history.record_side_effect(effect) == effect  # Idempotent observation replay.
    assert history.query_effects(first.version_id) == [effect]
    assert history.query_effects(second.version_id) == []
    api_effect = SideEffectRecord(
        "observed-api-1",
        second.version_id,
        "fixture-operation-api",
        "api.call",
        "https://example.invalid/v1/resource",
        timestamp,
    )
    history.record_side_effect(api_effect)
    assert history.query_effects(second.version_id) == [api_effect]
    assert history.query_effects(first.version_id) == [effect]


def test_parent_links_trace_current_to_root_with_branches_isolated() -> None:
    from jarvis.plan_history import PlanHistory

    history = PlanHistory()
    root = history.record_version(_plan())
    left = history.record_version(_plan("/fixture/left"), root)
    right = history.record_version(_plan("/fixture/right"), root.version_id)
    current = history.record_version(_plan("/fixture/current"), left)
    versions = {v.version_id: v for v in history.query_history(root.plan_id)}
    trace: list[str] = []
    cursor = current
    while True:
        trace.append(cursor.version_id)
        if cursor.parent_version_id is None:
            break
        cursor = versions[cursor.parent_version_id]
    assert trace == [current.version_id, left.version_id, root.version_id]
    assert right.parent_version_id == root.version_id
    assert right.version_id not in trace


def test_versions_sort_by_utc_time_even_if_wall_clock_moves_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarvis.plan_history as module

    base = datetime(2026, 1, 1, tzinfo=UTC)
    times = iter([base + timedelta(seconds=2), base, base + timedelta(seconds=1), base])

    class Clock:
        @staticmethod
        def now(_tz: object) -> datetime:
            return next(times)

    monkeypatch.setattr(module, "datetime", Clock)
    history = module.PlanHistory()
    root = history.record_version(_plan())
    child = history.record_version(_plan("/fixture/child"), root)
    grandchild = history.record_version(_plan("/fixture/grandchild"), child)
    other = history.record_version(_plan(request_key="other-task"))
    assert history.query_history(root.plan_id) == [child, grandchild, root]
    assert history.query_history(other.plan_id) == [other]
    assert history.query_history("missing-plan") == []
    # Chain order is causal; timestamps are not an execution-order authority.
    assert child.parent_version_id == root.version_id


def test_effects_sort_by_timestamp_and_ties_preserve_insertion_order() -> None:
    from jarvis.plan_history import PlanHistory, SideEffectRecord

    history = PlanHistory()
    version = history.record_version(_plan())
    base = datetime(2026, 1, 1, tzinfo=UTC)
    later = SideEffectRecord("later", version.version_id, "op", "api.call", "target", base)
    early = replace(later, effect_id="early", timestamp=base - timedelta(seconds=1))
    tied = replace(later, effect_id="tied")
    for effect in (later, early, tied):
        history.record_side_effect(effect)
    assert history.query_effects(version.version_id) == [early, later, tied]


def test_snapshot_and_returned_records_cannot_rewrite_history() -> None:
    from jarvis.plan_history import PlanHistory, SideEffectRecord

    history = PlanHistory()
    plan = _plan()
    first = history.record_version(plan)
    second = history.record_version(_plan("/fixture/second"), first)
    assert first.steps_snapshot is not plan.steps
    assert first.steps_snapshot[0] is not plan.steps[0]
    assert first.steps_snapshot[0].input is not plan.steps[0].input
    with pytest.raises(FrozenInstanceError):
        first.parent_version_id = second.version_id  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValidationError):
        first.steps_snapshot[0].input.root = "/rewrite"  # pyright: ignore[reportAttributeAccessIssue]
    effect = SideEffectRecord(
        "fx",
        first.version_id,
        "op",
        "file.publish.no_clobber",
        "out/file",
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    history.record_side_effect(effect)
    with pytest.raises(FrozenInstanceError):
        effect.target = "rewrite"  # pyright: ignore[reportAttributeAccessIssue]
    history.query_history(first.plan_id).clear()
    history.query_effects(first.version_id).clear()
    assert history.query_history(first.plan_id) == [first, second]
    assert history.query_effects(first.version_id) == [effect]


def test_unknown_parent_or_cross_task_parent_cannot_create_orphans() -> None:
    from jarvis.plan_history import PlanHistory

    history = PlanHistory()
    plan = _plan()
    first = history.record_version(plan)
    with pytest.raises(ValueError, match="parent version"):
        history.record_version(plan, "missing-version")
    with pytest.raises(ValueError, match="different task"):
        history.record_version(_plan(request_key="other-task"), first)
    with pytest.raises(ValueError, match="recorded snapshot"):
        history.record_version(plan, replace(first, plan_id="forged-plan"))
    with pytest.raises(ValueError, match="parent version"):
        PlanHistory().record_version(plan, first)
    assert history.query_history(first.plan_id) == [first]


def test_missing_or_conflicting_effect_is_rejected_without_losing_original() -> None:
    from jarvis.plan_history import PlanHistory, SideEffectRecord

    history = PlanHistory()
    first = history.record_version(_plan())
    second = history.record_version(_plan("/fixture/second"), first)
    effect = SideEffectRecord(
        "fx", first.version_id, "op", "api.call", "target", datetime(2026, 1, 1, tzinfo=UTC)
    )
    with pytest.raises(ValueError, match="version"):
        history.record_side_effect(replace(effect, plan_version_id="missing-version"))
    history.record_side_effect(effect)
    for conflict in (
        replace(effect, target="other"),
        replace(effect, plan_version_id=second.version_id),
    ):
        with pytest.raises(ValueError, match="effect_id"):
            history.record_side_effect(conflict)
    assert history.query_effects(first.version_id) == [effect]
    assert history.query_effects("missing-version") == []


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "forged-plan"},
        {"steps": ()},
        {
            "steps": (
                _plan().steps[0].model_copy(update={"input": TaskInput(root="/different-input")}),
            )
        },
    ],
)
def test_rejects_bypassed_plan_models_before_appending(changes: dict[str, Any]) -> None:
    from jarvis.plan_history import PlanHistory

    history = PlanHistory()
    plan = _plan()
    with pytest.raises(ValueError):
        history.record_version(plan.model_copy(update=changes))
    assert history.query_history(plan.id) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (name, value)
        for name in ("effect_id", "plan_version_id", "operation_id", "effect_type", "target")
        for value in ("", "  ", "bad\x00text", "bad\ud800text", 1)
    ],
)
def test_effect_metadata_rejects_invalid_text(field: str, value: object) -> None:
    from jarvis.plan_history import SideEffectRecord

    values: dict[str, Any] = dict(
        effect_id="fx",
        plan_version_id="version",
        operation_id="op",
        effect_type="api.call",
        target="target",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    values[field] = value
    with pytest.raises(ValueError):
        SideEffectRecord(**values)


@pytest.mark.parametrize("timestamp", [datetime(2026, 1, 1), 0, "2026-01-01"])
def test_effect_timestamp_requires_aware_datetime(timestamp: Any) -> None:
    from jarvis.plan_history import SideEffectRecord

    with pytest.raises(ValueError, match="timestamp"):
        SideEffectRecord("fx", "version", "op", "api.call", "target", timestamp)


def test_unicode_and_opaque_targets_preserved_without_normalization() -> None:
    from jarvis.plan_history import SideEffectRecord

    target = "https://example.invalid/资源?q=a%2Fb"
    effect = SideEffectRecord(
        "观测-1",
        "版本-1",
        "原操作-1",
        "api.call",
        target,
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert effect.target == target
    assert effect.operation_id == "原操作-1"


def test_effects_sort_by_absolute_time_across_dst_fold() -> None:
    from jarvis.plan_history import PlanHistory, SideEffectRecord

    history = PlanHistory()
    version = history.record_version(_plan())
    zone = ZoneInfo("America/New_York")
    first = SideEffectRecord(
        "first",
        version.version_id,
        "op",
        "api.call",
        "target",
        datetime(2026, 11, 1, 1, 50, tzinfo=zone, fold=0),
    )
    second = replace(
        first, effect_id="second", timestamp=datetime(2026, 11, 1, 1, 10, tzinfo=zone, fold=1)
    )
    history.record_side_effect(first)
    history.record_side_effect(second)
    assert history.query_effects(version.version_id) == [first, second]
    assert first.timestamp.tzinfo is zone  # Do not normalize the original observation.


def test_same_effect_id_different_dst_instants_conflict() -> None:
    from jarvis.plan_history import PlanHistory, SideEffectRecord

    history = PlanHistory()
    version = history.record_version(_plan())
    zone = ZoneInfo("America/New_York")
    first = SideEffectRecord(
        "fx",
        version.version_id,
        "op",
        "api.call",
        "target",
        datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0),
    )
    history.record_side_effect(first)
    with pytest.raises(ValueError, match="effect_id"):
        history.record_side_effect(replace(first, timestamp=first.timestamp.replace(fold=1)))
    # Equivalent absolute instants in another timezone are an idempotent replay.
    assert (
        history.record_side_effect(replace(first, timestamp=first.timestamp.astimezone(UTC)))
        == first
    )
