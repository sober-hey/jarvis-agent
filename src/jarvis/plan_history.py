"""K1-006 plan history DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False.

Detached, in-memory audit candidates only; no execution, durable storage, schema
migration or packaging/ wiring. A lineage's plan_id is its first candidate's ID;
subsequent candidate IDs remain unchanged inside immutable Step snapshots.
History version IDs never replace Plan/Step/OperationIntent execution identities.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from jarvis.planner import Plan, Planner, Step

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


@dataclass(frozen=True)
class PlanVersion:
    """One full Step snapshot in a lineage; timestamps are UTC observations."""

    version_id: str
    plan_id: str
    created_at: datetime
    steps_snapshot: tuple[Step, ...]
    parent_version_id: str | None


@dataclass(frozen=True)
class SideEffectRecord:
    """Caller-supplied observation, not proof that an Intent was executed.

    Preserve the original Intent's operation_id, intended_action and target when
    available. No new operation key is minted here. Multiple observations may
    refer to one operation; effect_id identifies an observation, not a retry.
    API labels are opaque fixture metadata, not a newly supported executor.
    """

    effect_id: str
    plan_version_id: str
    operation_id: str
    effect_type: str
    target: str
    timestamp: datetime

    def __post_init__(self) -> None:
        for name in ("effect_id", "plan_version_id", "operation_id", "effect_type", "target"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip() or "\x00" in value:
                raise ValueError(f"{name} must be nonblank text without NUL")
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as error:
                raise ValueError(f"{name} must be safely UTF-8 encodable") from error
        is_aware = (
            isinstance(cast(object, self.timestamp), datetime)
            and self.timestamp.utcoffset() is not None
        )
        if not is_aware:
            raise ValueError("timestamp must be a timezone-aware datetime")


class PlanHistory:
    """Single-process offline audit store, not a repository or execution grant.

    All calls must be serialized by the fixture owner. Records vanish when the
    instance/process ends; this store is NOT the Intent-before-effect journal,
    reconciliation, independent verification or authorization infrastructure.
    """

    def __init__(self) -> None:
        self._versions: dict[str, PlanVersion] = {}
        self._tasks: dict[str, str] = {}
        self._effects: dict[str, SideEffectRecord] = {}

    def record_version(
        self, plan: Plan, parent_version: PlanVersion | str | None = None
    ) -> PlanVersion:
        """Append a snapshot; parent must already be present in this store.

        Existing K0 Plan IDs are content-derived, so changed candidates have new
        IDs. Keep the root candidate's ID as the history lineage key, without
        mutating candidates or pretending to activate a new execution graph.
        """
        checked = Plan.model_validate(plan.model_dump(mode="python"))
        if checked != Planner().plan(checked.task_spec):
            raise ValueError("plan must match the existing K0 Planner derivation")
        parent_id = (
            parent_version.version_id if isinstance(parent_version, PlanVersion) else parent_version
        )
        parent = None
        if parent_id is not None:
            parent = self._versions.get(parent_id)
            if parent is None:
                raise ValueError("parent version is not recorded in this history")
            if isinstance(parent_version, PlanVersion) and parent_version != parent:
                raise ValueError("parent version does not match the recorded snapshot")
            if self._tasks[parent_id] != checked.task_id:
                raise ValueError("parent version belongs to a different task")
        version = PlanVersion(
            version_id="plan-version-" + uuid4().hex,
            plan_id=checked.id if parent is None else parent.plan_id,
            created_at=datetime.now(UTC),
            steps_snapshot=checked.steps,
            parent_version_id=parent_id,
        )
        self._versions[version.version_id] = version
        self._tasks[version.version_id] = checked.task_id
        return version

    def record_side_effect(self, effect: SideEffectRecord) -> SideEffectRecord:
        """Append an observation or replay the identical effect_id idempotently.

        Binding and truth of the caller's observation are NOT independently
        verified here. An unknown result must still go through reconciliation;
        absence/presence in this draft is not permission to execute or retry.
        """
        checked = replace(effect)  # Revalidate even a tampered frozen dataclass.
        if checked.plan_version_id not in self._versions:
            raise ValueError("side effect version is not recorded in this history")
        prior = self._effects.get(checked.effect_id)
        if prior is not None:
            # datetime compares equal local wall times across a DST fold when
            # tzinfo is shared. Compare UTC instants without rewriting originals.
            if replace(prior, timestamp=prior.timestamp.astimezone(UTC)) != replace(
                checked, timestamp=checked.timestamp.astimezone(UTC)
            ):
                raise ValueError("effect_id already has a different observation")
            return prior
        self._effects[checked.effect_id] = checked
        return checked

    def query_history(self, plan_id: str) -> list[PlanVersion]:
        """All lineage versions, oldest observed time first; equal times are stable.

        Wall-clock order can differ from causal order after a clock adjustment.
        Follow parent_version_id, not timestamps, for ancestry or branch traversal.
        Unknown lineage IDs return an empty detached result list.
        """
        return sorted(
            (version for version in self._versions.values() if version.plan_id == plan_id),
            key=lambda version: version.created_at,
        )

    def query_effects(self, version_id: str) -> list[SideEffectRecord]:
        """Only this version's observations; no inheritance or remapping on replan."""
        return sorted(
            (effect for effect in self._effects.values() if effect.plan_version_id == version_id),
            key=lambda effect: effect.timestamp.astimezone(UTC),
        )
