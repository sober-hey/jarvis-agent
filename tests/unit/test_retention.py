"""K2-008 detached retention draft: no product store or runtime wiring."""

import math
from dataclasses import replace
from typing import Any

import pytest

from jarvis.memory_schema import MemoryEntry, MemoryType

DAY_MS = 86_400_000
NOW = 200 * DAY_MS


def _entry(identifier: str = "memory", **changes: Any) -> MemoryEntry:
    values: dict[str, Any] = {
        "id": identifier,
        "type": MemoryType.EPISODIC,
        "content": "offline fixture",
        "source": "event:fixture",
        "created_at": NOW,
        "last_accessed_at": NOW,
        "importance": 4,
    }
    values.update(changes)
    return MemoryEntry(**values)


def test_recent_entry_is_kept_without_mutating_schema_importance() -> None:
    from jarvis.retention import (
        RUNTIME_ENABLED,
        RetentionDecision,
        RetentionManager,
        RetentionPolicy,
    )

    entry = _entry()
    manager = RetentionManager(RetentionPolicy(), clock=lambda: NOW)
    assert manager.evaluate(entry) is RetentionDecision.KEEP
    assert entry.importance == 4
    assert RUNTIME_ENABLED is False


@pytest.mark.parametrize("days", [0, 15, 30, 60, 10_000])
def test_forgetting_curve_uses_requested_exponential_without_schema_floor(days: int) -> None:
    from jarvis.retention import RetentionManager, RetentionPolicy

    entry = _entry(created_at=0, last_accessed_at=0)
    manager = RetentionManager(RetentionPolicy(half_life_days=30), clock=lambda: days * DAY_MS)
    assert manager.decayed_importance(entry) == pytest.approx(4 * math.exp(-days / 30))
    assert entry.importance == 4


def test_curve_uses_last_access_and_clamps_backward_clock() -> None:
    from jarvis.retention import RetentionManager, RetentionPolicy

    entry = _entry(created_at=0)
    manager = RetentionManager(RetentionPolicy(), clock=lambda: NOW - 1)
    assert manager.decayed_importance(entry) == 4
    assert manager.decayed_importance(replace(entry, last_accessed_at=NOW - DAY_MS)) < 4


@pytest.mark.parametrize("auto_delete", [False, True])
def test_low_importance_decays_unless_automatic_deletion_is_enabled(auto_delete: bool) -> None:
    from jarvis.retention import RetentionDecision, RetentionManager, RetentionPolicy

    entry = _entry(created_at=0, last_accessed_at=0)
    policy = RetentionPolicy(auto_delete_below_threshold=auto_delete)
    manager = RetentionManager(policy, clock=lambda: NOW)
    expected = RetentionDecision.DELETE if auto_delete else RetentionDecision.DECAY
    assert manager.evaluate(entry) is expected


def test_partially_decayed_but_usable_entry_is_decay_and_threshold_is_strict() -> None:
    from jarvis.retention import RetentionDecision, RetentionManager, RetentionPolicy

    entry = _entry(created_at=NOW - DAY_MS, last_accessed_at=NOW - DAY_MS)
    manager = RetentionManager(RetentionPolicy(auto_delete_below_threshold=True), clock=lambda: NOW)
    assert manager.evaluate(entry) is RetentionDecision.DECAY
    boundary = RetentionManager(
        RetentionPolicy(min_importance_threshold=4, auto_delete_below_threshold=True),
        clock=lambda: NOW,
    )
    assert boundary.evaluate(_entry()) is RetentionDecision.KEEP


@pytest.mark.parametrize("memory_type", list(MemoryType))
def test_age_limits_are_by_type_and_expire_at_boundary(memory_type: MemoryType) -> None:
    from jarvis.retention import RetentionDecision, RetentionManager, RetentionPolicy

    policy = RetentionPolicy(max_age_days={memory_type: 10})
    manager = RetentionManager(policy, clock=lambda: NOW)
    old = _entry(type=memory_type, created_at=NOW - 10 * DAY_MS)
    assert manager.evaluate(old) is RetentionDecision.DELETE
    assert manager.evaluate(replace(old, created_at=old.created_at + 1)) is RetentionDecision.KEEP
    unlimited = RetentionManager(RetentionPolicy(), clock=lambda: NOW)
    assert unlimited.evaluate(old) is RetentionDecision.KEEP


def test_policy_copies_mutable_inputs_and_has_no_cross_instance_default_state() -> None:
    from jarvis.retention import RetentionPolicy

    limits = {MemoryType.WORKING: 1.0}
    kinds = {MemoryType.WORKING}
    policy = RetentionPolicy(max_age_days=limits, user_deletable_types=kinds)
    limits[MemoryType.WORKING] = 0
    kinds.clear()
    assert policy.max_age_days[MemoryType.WORKING] == 1
    assert policy.user_deletable_types == {MemoryType.WORKING}
    assert RetentionPolicy().max_age_days == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"max_age_days": {MemoryType.WORKING: -1}},
        {"max_age_days": {"WORKING": 1}},
        {"max_age_days": []},
        {"min_importance_threshold": -1},
        {"min_importance_threshold": 6},
        {"min_importance_threshold": math.nan},
        {"min_importance_threshold": True},
        {"auto_delete_below_threshold": 1},
        {"half_life_days": 0},
        {"half_life_days": math.inf},
        {"grace_period_days": -1},
        {"grace_period_days": math.nan},
        {"user_deletable_types": [MemoryType.WORKING]},
        {"user_deletable_types": {"WORKING"}},
    ],
)
def test_policy_rejects_unsafe_configuration(changes: dict[str, Any]) -> None:
    from jarvis.retention import RetentionPolicy

    with pytest.raises((TypeError, ValueError)):
        RetentionPolicy(**changes)


def test_soft_delete_preserves_content_hides_it_and_can_be_restored() -> None:
    from jarvis.retention import RetentionDecision, RetentionManager, RetentionPolicy

    entry = _entry()
    manager = RetentionManager(RetentionPolicy(), entries=[entry], clock=lambda: NOW)
    assert manager.mark_deleted(entry.id) is True
    assert manager.deleted_at[entry.id] == NOW
    assert manager.get(entry.id) is None
    assert manager.get(entry.id, include_deleted=True) == entry
    assert manager.evaluate(entry) is RetentionDecision.DELETE
    assert manager.restore(entry.id) is True
    assert entry.id not in manager.deleted_at
    assert manager.get(entry.id) == entry
    assert manager.restore(entry.id) is False


def test_repeated_deletion_does_not_extend_grace_or_mutate_readonly_timestamps() -> None:
    from jarvis.retention import RetentionManager, RetentionPolicy

    now = [NOW]
    manager = RetentionManager(RetentionPolicy(), entries=[_entry()], clock=lambda: now[0])
    manager.mark_deleted("memory")
    now[0] += DAY_MS
    assert manager.mark_deleted("memory") is False
    assert manager.deleted_at["memory"] == NOW
    with pytest.raises(TypeError):
        manager.deleted_at["memory"] = now[0]  # type: ignore[index]


def test_user_deletable_types_is_enforced_for_explicit_requests_only() -> None:
    from jarvis.retention import RetentionManager, RetentionPolicy

    manager = RetentionManager(
        RetentionPolicy(user_deletable_types={MemoryType.WORKING}),
        entries=[_entry()],
        clock=lambda: NOW,
    )
    with pytest.raises(PermissionError):
        manager.mark_deleted("memory")
    assert manager.get("memory") == _entry()
    assert manager.deleted_at == {}
    for action in (manager.mark_deleted, manager.restore):
        with pytest.raises(KeyError):
            action("missing")


def test_enforce_soft_deletes_only_delete_candidates_and_does_not_compound_decay() -> None:
    from jarvis.retention import RetentionDecision, RetentionManager, RetentionPolicy

    recent = _entry("keep")
    decaying = _entry("decay", created_at=NOW - DAY_MS, last_accessed_at=NOW - DAY_MS)
    expired = _entry("expire", type=MemoryType.WORKING, created_at=0)
    low = _entry("low", created_at=0, last_accessed_at=0)
    entries = [recent, decaying, expired, low]
    manager = RetentionManager(
        RetentionPolicy(max_age_days={MemoryType.WORKING: 1}, auto_delete_below_threshold=True),
        clock=lambda: NOW,
    )
    assert manager.enforce(entries) == []  # No hard-delete authorization before grace.
    assert manager.deleted_at == {"expire": NOW, "low": NOW}
    assert manager.get("keep") == recent
    assert manager.get("decay") == decaying
    assert manager.evaluate(decaying) is RetentionDecision.DECAY
    assert manager.enforce(entries) == []
    assert manager.get("decay") == decaying
    assert entries == [recent, decaying, expired, low]  # No caller-owned list mutation.


def test_restore_at_grace_boundary_and_hard_delete_after_grace() -> None:
    from jarvis.retention import RetentionManager, RetentionPolicy

    now = [NOW]
    first, second = _entry("first"), _entry("second")
    manager = RetentionManager(
        RetentionPolicy(grace_period_days=2), entries=[first, second], clock=lambda: now[0]
    )
    manager.mark_deleted("first")
    manager.mark_deleted("second")
    now[0] += 2 * DAY_MS
    assert manager.enforce([]) == []
    assert manager.restore("first") is True
    now[0] += 1
    with pytest.raises(ValueError, match="grace"):
        manager.restore("second")
    assert manager.enforce([]) == [second]
    assert manager.get("second", include_deleted=True) is None
    assert manager.get("first") == first
    assert "second" not in manager.deleted_at
    with pytest.raises(KeyError):
        manager.restore("second")


def test_hard_delete_list_has_only_expired_tombstones_once_and_cannot_resurrect() -> None:
    from jarvis.retention import RetentionDecision, RetentionManager, RetentionPolicy

    now = [NOW]
    first, second = _entry("first"), _entry("second")
    manager = RetentionManager(RetentionPolicy(grace_period_days=1), clock=lambda: now[0])
    manager.enforce([first, second])
    manager.mark_deleted("first")
    now[0] += DAY_MS
    manager.mark_deleted("second")
    now[0] += 1
    assert manager.enforce([first, second]) == [first]
    assert manager.enforce([first, second]) == []
    assert manager.get("first", include_deleted=True) is None
    assert manager.evaluate(first) is RetentionDecision.DELETE
    with pytest.raises(ValueError, match="purged"):
        manager.register(first)
    assert manager.deleted_at == {"second": NOW + DAY_MS}


def test_policy_automatic_expiry_still_honors_grace_for_non_user_deletable_type() -> None:
    from jarvis.retention import RetentionManager, RetentionPolicy

    now = [NOW]
    entry = _entry(created_at=0)
    manager = RetentionManager(
        RetentionPolicy(
            max_age_days={MemoryType.EPISODIC: 1}, user_deletable_types=set(), grace_period_days=0
        ),
        clock=lambda: now[0],
    )
    assert manager.enforce([entry]) == []
    now[0] += 1
    assert manager.enforce([entry]) == [entry]


def test_unusable_decay_is_hidden_but_preserved_without_automatic_deletion() -> None:
    from jarvis.retention import RetentionDecision, RetentionManager, RetentionPolicy

    entry = _entry(created_at=0, last_accessed_at=0)
    manager = RetentionManager(RetentionPolicy(), entries=[entry], clock=lambda: NOW)
    assert manager.evaluate(entry) is RetentionDecision.DECAY
    assert manager.get(entry.id) is None
    assert manager.get(entry.id, include_deleted=True) == entry
    assert manager.enforce([]) == []
    assert manager.deleted_at == {}


@pytest.mark.parametrize("operation", ["evaluate", "decayed_importance", "register", "enforce"])
def test_tampered_entry_is_revalidated_before_use(operation: str) -> None:
    from jarvis.retention import RetentionManager, RetentionPolicy

    entry = _entry()
    object.__setattr__(entry, "importance", 100)
    manager = RetentionManager(RetentionPolicy(), clock=lambda: NOW)
    with pytest.raises(ValueError):
        getattr(manager, operation)([entry] if operation == "enforce" else entry)
    assert manager.get(entry.id, include_deleted=True) is None
    assert manager.deleted_at == {}


@pytest.mark.parametrize("kind", ["duplicate", "conflict", "invalid", "generator_failure"])
def test_batch_failure_is_atomic_and_preserves_existing_soft_delete(kind: str) -> None:
    from jarvis.retention import RetentionManager, RetentionPolicy

    now = [NOW]
    old, new = _entry("old"), _entry("new", type=MemoryType.WORKING, created_at=0)
    manager = RetentionManager(
        RetentionPolicy(max_age_days={MemoryType.WORKING: 1}, grace_period_days=1),
        entries=[old],
        clock=lambda: now[0],
    )
    manager.mark_deleted("old")
    now[0] += 2 * DAY_MS
    if kind == "duplicate":
        batch: Any = [new, new]
    elif kind == "conflict":
        batch = [new, replace(old, content="silent overwrite")]
    elif kind == "invalid":
        batch = [new, object()]
    else:

        def broken() -> Any:
            yield new
            raise RuntimeError("fixture generator failure")

        batch = broken()
    with pytest.raises((TypeError, ValueError, RuntimeError)):
        manager.enforce(batch)
    assert manager.get("new", include_deleted=True) is None
    assert manager.get("old", include_deleted=True) == old
    assert manager.deleted_at == {"old": NOW}


@pytest.mark.parametrize("invalid_clock", [-1, True, 1.1, math.nan, 2**63])
def test_invalid_clock_prevents_enforcement_without_importing_batch(invalid_clock: Any) -> None:
    from jarvis.retention import RetentionManager, RetentionPolicy

    manager = RetentionManager(RetentionPolicy(), clock=lambda: invalid_clock)
    with pytest.raises(ValueError):
        manager.enforce([_entry()])
    assert manager.deleted_at == {}
    assert manager.get("memory", include_deleted=True) is None


def test_backward_clock_does_not_purge_or_extend_deleted_at() -> None:
    from jarvis.retention import RetentionManager, RetentionPolicy

    now = [NOW]
    manager = RetentionManager(
        RetentionPolicy(grace_period_days=0), entries=[_entry()], clock=lambda: now[0]
    )
    manager.mark_deleted("memory")
    now[0] -= DAY_MS
    assert manager.enforce([]) == []
    assert manager.deleted_at == {"memory": NOW}
    assert manager.restore("memory") is True


def test_get_returns_an_isolated_snapshot_and_register_rejects_conflict() -> None:
    from jarvis.retention import RetentionManager, RetentionPolicy

    entry = _entry()
    manager = RetentionManager(RetentionPolicy(), entries=[entry], clock=lambda: NOW)
    manager.register(entry)
    with pytest.raises(ValueError, match="conflicting"):
        manager.register(replace(entry, content="replacement"))
    snapshot = manager.get(entry.id)
    assert snapshot is not None
    object.__setattr__(snapshot, "content", "caller tampering")
    object.__setattr__(entry, "content", "input tampering")
    assert manager.get("memory") == _entry()


def test_enforce_reads_clock_once_per_batch_and_does_not_register_evaluation() -> None:
    from jarvis.retention import RetentionManager, RetentionPolicy

    calls: list[int] = []

    def clock() -> int:
        calls.append(1)
        return NOW

    manager = RetentionManager(RetentionPolicy(), clock=clock)
    manager.enforce([_entry("first"), _entry("second")])
    assert len(calls) == 1
    manager.evaluate(_entry("unknown"))
    with pytest.raises(KeyError):
        manager.mark_deleted("unknown")
