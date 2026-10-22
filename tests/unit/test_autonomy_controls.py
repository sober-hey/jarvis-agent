"""Offline K3-007 behavior-type permissions; never grant execution authority."""

import sqlite3
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest

from jarvis import autonomy_controls as controls
from jarvis.approval import ApprovalGate, ApprovalStatus, ApprovalStore


class Clock:
    def __init__(self, now: int = 10_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


@pytest.mark.parametrize(
    ("action", "allowed", "approval"),
    [
        ("READ", True, False),
        ("WRITE", False, True),
        ("NOTIFY", True, False),
        ("EXTERNAL", False, True),
        ("DANGEROUS", False, False),
    ],
)
def test_default_policy(tmp_path: Path, action: str, allowed: bool, approval: bool) -> None:
    with controls.AutonomyController(tmp_path / "controls.db", draft_fixture=True) as controller:
        result = controller.check(controls.ActionClass[action])
        assert result.allowed is allowed
        assert result.requires_approval is approval
        assert result.reason == f"default_policy:{action}"
    assert controls.DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert controls.RUNTIME_ENABLED is False


def test_override_replaces_only_selected_class_and_history_is_retained(tmp_path: Path) -> None:
    clock = Clock()
    with controls.AutonomyController(
        tmp_path / "controls.db", clock=clock, draft_fixture=True
    ) as controller:
        first = controller.override(
            controls.ActionClass.WRITE, controls.PermissionMode.ALLOW, clock.now + 1000
        )
        assert controller.check(controls.ActionClass.WRITE) == controls.PermissionResult(
            True, False, "temporary_override:WRITE"
        )
        assert controller.check(controls.ActionClass.EXTERNAL).requires_approval
        assert controller.list_overrides() == [first]
        second = controller.override(
            controls.ActionClass.WRITE, controls.PermissionMode.DENY, clock.now + 100
        )
        assert not controller.check(controls.ActionClass.WRITE).allowed
        assert not controller.check(controls.ActionClass.WRITE).requires_approval
        assert controller.list_overrides() == [second]
        assert [entry.event_type for entry in controller.history()] == ["OVERRIDE", "OVERRIDE"]
        clock.now += 100
        # Superseded ALLOW does not revive when the newer DENY expires.
        assert controller.check(controls.ActionClass.WRITE).requires_approval
        assert controller.list_overrides() == []
        assert [entry.event_type for entry in controller.history()] == [
            "OVERRIDE",
            "OVERRIDE",
            "EXPIRE",
        ]


@pytest.mark.parametrize("mode", list(controls.PermissionMode))
def test_override_modes_and_exclusive_expiry_boundary(
    tmp_path: Path, mode: controls.PermissionMode
) -> None:
    clock = Clock()
    with controls.AutonomyController(
        tmp_path / "controls.db", clock=clock, draft_fixture=True
    ) as controller:
        entry = controller.override(controls.ActionClass.DANGEROUS, mode, clock.now + 100)
        assert entry.action_class is controls.ActionClass.DANGEROUS
        assert entry.mode is mode
        assert entry.created_at == clock.now
        assert entry.expires_at == clock.now + 100
        clock.now += 99
        result = controller.check(controls.ActionClass.DANGEROUS)
        assert result.allowed is (mode is controls.PermissionMode.ALLOW)
        assert result.requires_approval is (mode is controls.PermissionMode.REQUIRE_APPROVAL)
        clock.now += 1
        assert controller.list_overrides() == []
        assert controller.check(controls.ActionClass.DANGEROUS) == controls.PermissionResult(
            False, False, "default_policy:DANGEROUS"
        )
        assert [entry.event_type for entry in controller.history()] == ["OVERRIDE", "EXPIRE"]
        assert controller.history()[-1].target_id == entry.id


def test_reset_restores_defaults_without_deleting_history(tmp_path: Path) -> None:
    clock = Clock()
    with controls.AutonomyController(
        tmp_path / "controls.db", clock=clock, draft_fixture=True
    ) as controller:
        write = controller.override(
            controls.ActionClass.WRITE, controls.PermissionMode.ALLOW, clock.now + 1000
        )
        read = controller.override(
            controls.ActionClass.READ, controls.PermissionMode.DENY, clock.now + 1000
        )
        controller.reset(controls.ActionClass.WRITE)
        assert controller.check(controls.ActionClass.WRITE).requires_approval
        assert controller.list_overrides() == [read]
        assert controller.history()[-1].event_type == "RESET"
        assert controller.history()[-1].target_id == write.id
        controller.reset(controls.ActionClass.WRITE)
        assert controller.check(controls.ActionClass.WRITE).requires_approval
        assert len(controller.history()) == 4


def test_reopen_preserves_active_override_and_expired_override_is_not_reused(
    tmp_path: Path,
) -> None:
    clock = Clock()
    path = tmp_path / "controls.db"
    with controls.AutonomyController(path, clock=clock, draft_fixture=True) as controller:
        override = controller.override(
            controls.ActionClass.WRITE, controls.PermissionMode.ALLOW, clock.now + 100
        )
    with controls.AutonomyController(path, clock=clock, draft_fixture=True) as controller:
        assert controller.list_overrides() == [override]
        assert controller.check(controls.ActionClass.WRITE).allowed
    clock.now += 100
    with controls.AutonomyController(path, clock=clock, draft_fixture=True) as controller:
        assert not controller.check(controls.ActionClass.WRITE).allowed
        assert controller.check(controls.ActionClass.WRITE).requires_approval
        assert controller.list_overrides() == []
        assert len(controller.history()) == 2
    with controls.AutonomyController(path, clock=clock, draft_fixture=True) as controller:
        assert len(controller.history()) == 2


def test_custom_defaults_are_immutable_detached_and_reset_uses_them(tmp_path: Path) -> None:
    modes = {controls.ActionClass.WRITE: controls.PermissionMode.DENY}
    policy = controls.ControlPolicy(modes)
    modes[controls.ActionClass.WRITE] = controls.PermissionMode.ALLOW
    with pytest.raises(TypeError):
        cast(dict[controls.ActionClass, controls.PermissionMode], policy.modes)[
            controls.ActionClass.WRITE
        ] = controls.PermissionMode.ALLOW
    clock = Clock()
    with controls.AutonomyController(
        tmp_path / "controls.db", policy, clock=clock, draft_fixture=True
    ) as controller:
        assert not controller.check(controls.ActionClass.WRITE).requires_approval
        assert controller.check(controls.ActionClass.READ).allowed
        controller.override(
            controls.ActionClass.WRITE, controls.PermissionMode.ALLOW, clock.now + 1
        )
        controller.reset(controls.ActionClass.WRITE)
        assert controller.check(controls.ActionClass.WRITE) == controls.PermissionResult(
            False, False, "default_policy:WRITE"
        )


def test_draft_opt_in_is_required_and_does_not_create_db(tmp_path: Path) -> None:
    path = tmp_path / "controls.db"
    with pytest.raises(controls.AutonomyControlError, match="draft_fixture=True"):
        controls.AutonomyController(path)
    assert not path.exists()


@pytest.mark.parametrize("bad", ["READ", "UNKNOWN", None, True, 1])
def test_unknown_or_untyped_action_never_allows(tmp_path: Path, bad: object) -> None:
    with controls.AutonomyController(tmp_path / "controls.db", draft_fixture=True) as controller:
        action = cast(controls.ActionClass, bad)
        operations: list[Callable[[], object]] = [
            lambda: controller.check(action),
            lambda: controller.override(action, controls.PermissionMode.ALLOW, 2**62),
            lambda: controller.reset(action),
        ]
        if bad is not None:
            operations.append(lambda: controller.history(action))
        for operation in operations:
            with pytest.raises(ValueError, match="ActionClass"):
                operation()
        assert controller.list_overrides() == []
        assert controller.history() == []


@pytest.mark.parametrize("bad", ["ALLOW", "UNKNOWN", None, True, 1])
def test_untyped_mode_never_changes_policy(tmp_path: Path, bad: object) -> None:
    clock = Clock()
    with controls.AutonomyController(
        tmp_path / "controls.db", clock=clock, draft_fixture=True
    ) as controller:
        with pytest.raises(ValueError, match="PermissionMode"):
            controller.override(
                controls.ActionClass.DANGEROUS,
                cast(controls.PermissionMode, bad),
                clock.now + 1,
            )
        assert not controller.check(controls.ActionClass.DANGEROUS).allowed
        assert controller.history() == []


@pytest.mark.parametrize("bad", [-1, True, 1.5, "100", None, 2**63, 10_000_000, 9_999_999])
def test_invalid_or_elapsed_expiry_rejected_without_history(tmp_path: Path, bad: object) -> None:
    clock = Clock()
    with controls.AutonomyController(
        tmp_path / "controls.db", clock=clock, draft_fixture=True
    ) as controller:
        with pytest.raises(ValueError):
            controller.override(
                controls.ActionClass.DANGEROUS, controls.PermissionMode.ALLOW, cast(int, bad)
            )
        assert controller.history() == []
        assert controller.list_overrides() == []


@pytest.mark.parametrize("kind", ["action", "mode"])
def test_policy_rejects_untyped_configuration(kind: str) -> None:
    values = (
        {"READ": controls.PermissionMode.ALLOW}
        if kind == "action"
        else {controls.ActionClass.READ: "ALLOW"}
    )
    with pytest.raises(ValueError):
        controls.ControlPolicy(cast(dict[controls.ActionClass, controls.PermissionMode], values))


def test_expiry_and_reset_remain_removed_after_clock_rollback_and_reopen(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "controls.db"
    with controls.AutonomyController(path, clock=clock, draft_fixture=True) as controller:
        controller.override(
            controls.ActionClass.DANGEROUS, controls.PermissionMode.ALLOW, clock.now + 5
        )
        clock.now += 5
        assert not controller.check(controls.ActionClass.DANGEROUS).allowed
        clock.now -= 1
        with pytest.raises(controls.AutonomyControlError, match="backwards"):
            controller.check(controls.ActionClass.DANGEROUS)
    with pytest.raises(controls.AutonomyControlError, match="backwards"):
        controls.AutonomyController(path, clock=clock, draft_fixture=True)
    clock.now += 1
    with controls.AutonomyController(path, clock=clock, draft_fixture=True) as controller:
        assert controller.list_overrides() == []
        assert len(controller.history()) == 2


def test_current_and_history_atomic_rollback_on_current_insert_failure(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "controls.db"
    with controls.AutonomyController(path, clock=clock, draft_fixture=True) as controller:
        original = controller.override(
            controls.ActionClass.WRITE, controls.PermissionMode.ALLOW, clock.now + 1000
        )
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TRIGGER fail_current BEFORE INSERT ON draft_control_current "
                "BEGIN SELECT RAISE(ABORT,'injected failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            controller.override(
                controls.ActionClass.WRITE, controls.PermissionMode.DENY, clock.now + 1000
            )
        assert controller.list_overrides() == [original]
        assert len(controller.history()) == 1


@pytest.mark.parametrize("event", ["RESET", "EXPIRE"])
def test_reset_and_expiry_rollback_if_audit_fails(tmp_path: Path, event: str) -> None:
    clock = Clock()
    path = tmp_path / "controls.db"
    with controls.AutonomyController(path, clock=clock, draft_fixture=True) as controller:
        controller.override(controls.ActionClass.READ, controls.PermissionMode.DENY, clock.now + 5)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TRIGGER fail_history BEFORE INSERT ON draft_control_history "
                f"WHEN NEW.event_type='{event}' "
                "BEGIN SELECT RAISE(ABORT,'injected audit failure'); END"
            )
        if event == "EXPIRE":
            clock.now += 5
        with pytest.raises(sqlite3.IntegrityError, match="injected audit failure"):
            if event == "RESET":
                controller.reset(controls.ActionClass.READ)
            else:
                controller.check(controls.ActionClass.READ)
        with sqlite3.connect(path) as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM draft_control_current").fetchone()[0] == 1
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM draft_control_history").fetchone()[0] == 1
            )


def test_history_is_append_only_and_filtered(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "controls.db"
    with controls.AutonomyController(path, clock=clock, draft_fixture=True) as controller:
        controller.override(
            controls.ActionClass.READ, controls.PermissionMode.DENY, clock.now + 100
        )
        controller.reset(controls.ActionClass.WRITE)
        assert len(controller.history(controls.ActionClass.READ)) == 1
        for statement in (
            "DELETE FROM draft_control_history",
            "UPDATE draft_control_history SET mode='ALLOW' WHERE id=1",
        ):
            with (
                sqlite3.connect(path) as connection,
                pytest.raises(sqlite3.IntegrityError, match="append-only"),
            ):
                connection.execute(statement)
        detached = controller.list_overrides()
        detached.clear()
        assert len(controller.list_overrides()) == 1


def test_existing_core_or_modified_schema_rejected_without_migration(tmp_path: Path) -> None:
    path = tmp_path / "controls.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
    with pytest.raises(controls.AutonomyControlError, match="standalone"):
        controls.AutonomyController(path, draft_fixture=True)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == [
            ("tasks",),
            ("sqlite_autoindex_tasks_1",),
        ]
    candidate = tmp_path / "candidate.db"
    with controls.AutonomyController(candidate, draft_fixture=True):
        pass
    with sqlite3.connect(candidate) as connection:
        connection.execute("DROP TRIGGER draft_control_history_no_update")
    with pytest.raises(controls.AutonomyControlError, match="standalone"):
        controls.AutonomyController(candidate, draft_fixture=True)


def test_two_live_instances_observe_dynamic_changes_without_reconstruction(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "controls.db"
    with (
        controls.AutonomyController(path, clock=clock, draft_fixture=True) as owner,
        controls.AutonomyController(path, clock=clock, draft_fixture=True) as observer,
    ):
        owner.override(controls.ActionClass.READ, controls.PermissionMode.DENY, clock.now + 100)
        assert not observer.check(controls.ActionClass.READ).allowed
        observer.reset(controls.ActionClass.READ)
        assert owner.check(controls.ActionClass.READ).allowed
        assert owner.list_overrides() == []


def test_threads_serialize_history_and_current_pointer(tmp_path: Path) -> None:
    clock = Clock()
    with controls.AutonomyController(
        tmp_path / "controls.db", clock=clock, draft_fixture=True
    ) as controller:

        def update(index: int) -> controls.OverrideRecord:
            mode = controls.PermissionMode.ALLOW if index % 2 else controls.PermissionMode.DENY
            return controller.override(controls.ActionClass.READ, mode, clock.now + 1000)

        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(update, range(40)))
        latest = max(records, key=lambda item: item.id)
        assert controller.list_overrides() == [latest]
        assert len(controller.history()) == 40


def test_new_process_restores_override_history_and_reset(tmp_path: Path) -> None:
    path = tmp_path / "controls.db"
    clock = Clock()
    with controls.AutonomyController(path, clock=clock, draft_fixture=True) as controller:
        controller.override(
            controls.ActionClass.WRITE, controls.PermissionMode.ALLOW, clock.now + 100
        )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from jarvis.autonomy_controls import AutonomyController, ActionClass
with AutonomyController(sys.argv[1], clock=lambda:10_000_000, draft_fixture=True) as controller:
    assert controller.check(ActionClass.WRITE).allowed
    assert len(controller.history()) == 1
    controller.reset(ActionClass.WRITE)
    assert controller.check(ActionClass.WRITE).requires_approval
print('RESTORED_AND_RESET')
""",
            str(path),
        ],
        text=True,
        capture_output=True,
        timeout=15,
        check=True,
    )
    assert result.stdout.strip() == "RESTORED_AND_RESET"
    with controls.AutonomyController(path, clock=clock, draft_fixture=True) as controller:
        assert not controller.check(controls.ActionClass.WRITE).allowed
        assert len(controller.history()) == 2


def test_allow_is_not_a_bypass_for_approval_high_risk_floor(tmp_path: Path) -> None:
    clock = Clock()
    with (
        controls.AutonomyController(
            tmp_path / "controls.db", clock=clock, draft_fixture=True
        ) as controller,
        ApprovalStore(tmp_path / "approvals.db", draft_fixture=True) as store,
    ):
        controller.override(
            controls.ActionClass.EXTERNAL, controls.PermissionMode.ALLOW, clock.now + 100
        )
        assert controller.check(controls.ActionClass.EXTERNAL).allowed
        approval = ApprovalGate(store).evaluate(
            "operation-1", "external.call", {}, timestamp=clock.now
        )
        assert approval.status is ApprovalStatus.PENDING
        assert not approval.may_proceed
