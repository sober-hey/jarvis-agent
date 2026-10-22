"""K0-009 DRAFT path-policy checks on private filesystem fixtures only."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest


def test_allowed_root_and_child_pass(tmp_path: Path) -> None:
    from jarvis.path_control import DRAFT_STATUS, RUNTIME_ENABLED, PathGuard

    root = tmp_path / "allowed"
    root.mkdir()
    child = root / "资料.txt"
    child.write_text("fixture", encoding="utf-8")
    guard = PathGuard([root])
    assert guard.resolve_and_check(root) == root
    assert guard.resolve_and_check(child) == child
    assert DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert RUNTIME_ENABLED is False


@pytest.fixture
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "allowed"
    directory.mkdir()
    return directory


def test_multiple_roots(root: Path, tmp_path: Path) -> None:
    from jarvis.path_control import PathGuard

    other = tmp_path / "other"
    other.mkdir()
    guard = PathGuard([root, other])
    assert guard.resolve_and_check(other) == other


@pytest.mark.parametrize("name", ["outside", "allowed-sibling"])
def test_outside_and_string_prefix_sibling_denied(root: Path, tmp_path: Path, name: str) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    outside = tmp_path / name
    outside.mkdir()
    with pytest.raises(PathControlError):
        PathGuard([root]).resolve_and_check(outside)


@pytest.mark.parametrize("suffix", ["../outside", "child/../safe", "child/..", ".."])
def test_raw_parent_components_always_denied(root: Path, suffix: str) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    with pytest.raises(PathControlError):
        PathGuard([root]).resolve_and_check(f"{root}/{suffix}")


@pytest.mark.parametrize("suffix", ["", "child//leaf", "./leaf", "leaf/", "\x00", "\udcff"])
def test_empty_components_and_invalid_encoding_denied(root: Path, suffix: str) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    with pytest.raises(PathControlError):
        PathGuard([root]).resolve_and_check(f"{root}/{suffix}")


@pytest.mark.parametrize("target", ["", "relative.txt", ".", "../outside"])
def test_relative_or_empty_path_denied(root: Path, target: str) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    with pytest.raises(PathControlError):
        PathGuard([root]).resolve_and_check(target)


@pytest.mark.parametrize("kind", ["outside", "inside", "dangling", "loop", "ancestor"])
def test_all_symlinks_denied(root: Path, tmp_path: Path, kind: str) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    target = root / "link"
    outside = tmp_path / "outside"
    outside.mkdir()
    inside = root / "inside"
    inside.write_text("fixture")
    if kind == "outside":
        target.symlink_to(outside, target_is_directory=True)
    elif kind == "inside":
        target.symlink_to(inside)
    elif kind == "dangling":
        target.symlink_to(outside / "missing")
    elif kind == "loop":
        target.symlink_to(target)
    else:
        target.symlink_to(root, target_is_directory=True)
        target = target / "inside"
    with pytest.raises(PathControlError):
        PathGuard([root]).resolve_and_check(target)


@pytest.mark.parametrize("symlink_ancestor", [False, True])
def test_symlink_in_configured_root_denied(
    root: Path, tmp_path: Path, symlink_ancestor: bool
) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    child = root / "child"
    child.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(PathControlError):
        PathGuard([alias / "child" if symlink_ancestor else alias])


@pytest.mark.parametrize("operation", ["write", "execute"])
def test_roots_default_read_only(root: Path, operation: str) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    with pytest.raises(PathControlError):
        PathGuard([root]).check_permission(root, operation)


@pytest.mark.parametrize("operation", ["read", "write", "execute"])
def test_explicit_permissions(root: Path, operation: str) -> None:
    from jarvis.path_control import PathGuard

    guard = PathGuard([root], permissions={root: {"read", "write", "execute"}})
    assert guard.check_permission(root, operation) == root


def test_longest_component_prefix_overrides_broader_grants(root: Path) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    private = root / "read-only"
    private.mkdir()
    guard = PathGuard([root], permissions={root: {"read", "write"}, private: {"read"}})
    assert guard.check_permission(private, "read") == private
    with pytest.raises(PathControlError):
        guard.check_permission(private, "write")
    sibling = root / "read-only-sibling"
    sibling.mkdir()
    assert guard.check_permission(sibling, "write") == sibling


def test_explicit_rules_default_deny_unmatched_and_empty_grant(root: Path) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    private = root / "private"
    private.mkdir()
    for permissions in ({}, {private: set[str]()}):
        guard = PathGuard([root], permissions=permissions)
        with pytest.raises(PathControlError):
            guard.check_permission(private, "read")


def test_write_is_not_read_or_execute_grant(root: Path) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    guard = PathGuard([root], permissions={root: {"write"}})
    assert guard.check_permission(root, "write") == root
    for operation in ("read", "execute"):
        with pytest.raises(PathControlError):
            guard.check_permission(root, operation)


def test_permission_map_cannot_expand_allowed_roots(root: Path, tmp_path: Path) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    with pytest.raises(PathControlError):
        PathGuard([root], permissions={tmp_path: {"read"}})


@pytest.mark.parametrize("operation", ["READ", "delete", "", True, None])
def test_unknown_or_mistyped_permission_denied(root: Path, operation: object) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    with pytest.raises(PathControlError):
        PathGuard([root]).check_permission(root, cast("str", operation))


@pytest.mark.parametrize("grants", [{"delete"}, "read", {True}])
def test_bad_permission_configuration_denied(root: Path, grants: object) -> None:
    from collections.abc import Iterable

    from jarvis.path_control import PathControlError, PathGuard

    with pytest.raises(PathControlError):
        PathGuard([root], permissions={root: cast("Iterable[str]", grants)})


def test_policy_is_copied_not_live_mutable(root: Path) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    grants = {"read"}
    permissions = {root: grants}
    roots = [root]
    guard = PathGuard(roots, permissions=permissions)
    grants.add("write")
    permissions.clear()
    roots.clear()
    assert guard.check_permission(root, "read") == root
    with pytest.raises(PathControlError):
        guard.check_permission(root, "write")


def test_missing_leaf_only_allowed_for_authorized_write(root: Path) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    leaf = root / "new.txt"
    guard = PathGuard([root], permissions={root: {"read", "write", "execute"}})
    assert guard.check_permission(leaf, "write") == leaf
    assert not leaf.exists()  # Checking policy never creates the target.
    for operation in ("read", "execute"):
        with pytest.raises(PathControlError):
            guard.check_permission(leaf, operation)
    with pytest.raises(PathControlError):
        guard.resolve_and_check(leaf)
    with pytest.raises(PathControlError):
        guard.check_permission(root / "missing-parent" / "leaf", "write")


def test_missing_or_regular_file_roots_denied(root: Path) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    file = root / "file"
    file.touch()
    for invalid in (root / "missing", file):
        with pytest.raises(PathControlError):
            PathGuard([invalid])
    with pytest.raises(PathControlError):
        PathGuard([])


def test_regular_file_cannot_be_ancestor(root: Path) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    leaf = root / "file"
    leaf.touch()
    with pytest.raises(PathControlError):
        PathGuard([root]).resolve_and_check(leaf / "child")


def test_special_file_denied_without_blocking(root: Path) -> None:
    import os

    from jarvis.path_control import PathControlError, PathGuard

    fifo = root / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(PathControlError):
        PathGuard([root]).resolve_and_check(fifo)


def test_replaced_root_is_not_authorized(root: Path) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    guard = PathGuard([root])
    root.rename(root.with_name("original"))
    root.mkdir()
    with pytest.raises(PathControlError):
        guard.resolve_and_check(root)


def test_symlink_inserted_after_configuration_denied(root: Path, tmp_path: Path) -> None:
    from jarvis.path_control import PathControlError, PathGuard

    guard = PathGuard([root])
    root.rename(tmp_path / "old")
    root.symlink_to(tmp_path / "old", target_is_directory=True)
    with pytest.raises(PathControlError):
        guard.resolve_and_check(root)


def test_missing_platform_primitive_denies(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from jarvis.path_control import PathGuard, PlatformCapabilityError

    guard = PathGuard([root])
    monkeypatch.delattr(os, "O_NOFOLLOW")
    with pytest.raises(PlatformCapabilityError):
        guard.resolve_and_check(root)
    with pytest.raises(PlatformCapabilityError):
        PathGuard([root])


@pytest.mark.parametrize("primitive", ["dir_fd", "follow_symlinks"])
def test_missing_platform_support_set_denies(
    root: Path, monkeypatch: pytest.MonkeyPatch, primitive: str
) -> None:
    import os

    from jarvis.path_control import PathGuard, PlatformCapabilityError

    name = "supports_dir_fd" if primitive == "dir_fd" else "supports_follow_symlinks"
    monkeypatch.setattr(os, name, set[object]())
    with pytest.raises(PlatformCapabilityError):
        PathGuard([root])


def test_replacement_during_validation_denied(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from jarvis.path_control import PathControlError, PathGuard

    child = root / "child"
    child.mkdir()
    guard = PathGuard([root])
    realpath = os.path.realpath

    def replacing_realpath(path: object, *, strict: bool = False) -> str:
        child.rename(root / "original-child")
        child.mkdir()
        return realpath(cast("str", path), strict=strict)

    monkeypatch.setattr(os.path, "realpath", replacing_realpath)
    with pytest.raises(PathControlError, match="binding changed"):
        guard.resolve_and_check(child)


@pytest.mark.parametrize("reject", [False, True])
def test_traversal_closes_all_directory_fds(
    root: Path, monkeypatch: pytest.MonkeyPatch, reject: bool
) -> None:
    import os

    from jarvis.path_control import PathControlError, PathGuard

    child = root / "child"
    child.mkdir()
    guard = PathGuard([root])
    real_open = os.open
    real_close = os.close
    opened: set[int] = set()

    def tracked_open(
        path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = real_open(cast("str", path), flags, mode, dir_fd=dir_fd)
        opened.add(fd)
        return fd

    def tracked_close(fd: int) -> None:
        opened.remove(fd)
        real_close(fd)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {tracked_open})
    if reject:
        # Deny after some ancestor fds have been opened.
        target = child / "link"
        target.symlink_to(root)
        with pytest.raises(PathControlError):
            guard.resolve_and_check(target)
    else:
        assert guard.resolve_and_check(child) == child
    assert not opened


def test_missing_write_leaf_appearing_during_validation_denied(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from jarvis.path_control import PathControlError, PathGuard

    guard = PathGuard([root], permissions={root: {"write"}})
    target = root / "new"
    realpath = os.path.realpath

    def inserting_realpath(path: object, *, strict: bool = False) -> str:
        # realpath(strict=False) alone accepts a self-referential loop spelling.
        target.symlink_to(target)
        return realpath(cast("str", path), strict=strict)

    monkeypatch.setattr(os.path, "realpath", inserting_realpath)
    with pytest.raises(PathControlError):
        guard.check_permission(target, "write")


def test_os_denial_is_not_silently_accepted(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from jarvis.path_control import PathControlError, PathGuard

    guard = PathGuard([root])

    def denied_realpath(path: object, *, strict: bool = False) -> str:
        raise PermissionError("injected OS refusal")

    monkeypatch.setattr(os.path, "realpath", denied_realpath)
    with pytest.raises(PathControlError, match="safely inspected"):
        guard.resolve_and_check(root)
