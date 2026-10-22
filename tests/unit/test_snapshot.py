"""K0-010 snapshot DRAFT: private fixtures, no executor or product writes."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import jsonschema
import pytest

from jarvis.path_control import PathControlError, PathGuard


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "input"
    root.mkdir()
    return root


def test_same_input_has_stable_id_and_canonical_manifest(root: Path) -> None:
    from jarvis.snapshot import Snapshot

    a, b = root / "a.txt", root / "中文.txt"
    a.write_bytes(b"alpha")
    b.write_bytes(b"beta")
    builder = Snapshot(PathGuard([root]), root=root)
    first = builder.create([b, a], plan_id="plan-1")
    second = builder.create([a, b], plan_id="plan-1")
    assert first == second
    assert first.snapshot_id == second.snapshot_id
    assert first.manifest_bytes == second.manifest_bytes
    assert [entry.path for entry in first.files] == ["a.txt", "中文.txt"]
    manifest = json.loads(first.manifest_bytes)
    schema = Path(__file__).parents[2] / "contracts/snapshot_manifest.schema.json"
    jsonschema.validate(manifest, json.loads(schema.read_text()))
    assert manifest["file_count"] == 2
    assert manifest["total_bytes"] == 9
    assert first.files[0].sha256 == hashlib.sha256(b"alpha").hexdigest()
    assert first.files[0].metadata.size_bytes == 5
    assert builder.verify(first, plan_id="plan-1") is True


def test_frozen_bytes_survive_source_change_but_verification_fails(root: Path) -> None:
    from jarvis.snapshot import Snapshot, SnapshotVerificationError

    source = root / "a.txt"
    source.write_bytes(b"before")
    builder = Snapshot(PathGuard([root]), root=root)
    frozen = builder.create([source], plan_id="plan-1")
    source.write_bytes(b"after!")  # Same size: size checks alone cannot catch this.
    assert frozen.files[0].content == b"before"
    with pytest.raises(SnapshotVerificationError):
        builder.verify(frozen, plan_id="plan-1")
    assert builder.create([source], plan_id="plan-1").snapshot_id != frozen.snapshot_id


def test_plan_binding_does_not_change_content_address(root: Path) -> None:
    from jarvis.snapshot import Snapshot, SnapshotVerificationError

    source = root / "a.txt"
    source.write_bytes(b"fixture")
    builder = Snapshot(PathGuard([root]), root=root)
    first = builder.create([source], plan_id="plan-1")
    second = builder.create([source], plan_id="plan-2")
    assert first.plan_id == "plan-1"
    assert second.plan_id == "plan-2"
    assert first.snapshot_id == second.snapshot_id
    assert first.binding == ("plan-1", first.snapshot_id)
    with pytest.raises(SnapshotVerificationError):
        builder.verify(first, plan_id="plan-2")
    with pytest.raises(FrozenInstanceError):
        first.plan_id = "plan-2"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        first.files[0].content = b"changed"  # type: ignore[misc]


@pytest.mark.parametrize("spelling", ["outside", "parent", "sibling", "relative"])
def test_path_escape_is_rejected(root: Path, tmp_path: Path, spelling: str) -> None:
    from jarvis.snapshot import Snapshot

    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"private")
    sibling = tmp_path / "input-sibling"
    sibling.mkdir()
    sibling_file = sibling / "file.txt"
    sibling_file.write_bytes(b"private")
    paths = {
        "outside": str(outside),
        "parent": f"{root}/../outside.txt",
        "sibling": str(sibling_file),
        "relative": "outside.txt",
    }
    with pytest.raises(PathControlError):
        Snapshot(PathGuard([root]), root=root).create([paths[spelling]], plan_id="plan-1")


def test_policy_is_checked_for_each_child_not_just_directory(root: Path) -> None:
    from jarvis.snapshot import Snapshot

    blocked = root / "blocked.txt"
    blocked.write_bytes(b"private")
    guard = PathGuard([root], permissions={root: {"read"}, blocked: set[str]()})
    with pytest.raises(PathControlError):
        Snapshot(guard, root=root).create(directories=[root], plan_id="plan-1")


@pytest.mark.parametrize("kind", ["leaf", "ancestor", "fifo"])
def test_symlinks_and_special_files_are_rejected(root: Path, tmp_path: Path, kind: str) -> None:
    from jarvis.snapshot import Snapshot

    source = root / "file.txt"
    if kind == "fifo":
        os.mkfifo(source)
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"secret")
        if kind == "leaf":
            source.symlink_to(outside / "secret.txt")
        else:
            alias = root / "alias"
            alias.symlink_to(outside, target_is_directory=True)
            source = alias / "secret.txt"
    with pytest.raises(PathControlError):
        Snapshot(PathGuard([root]), root=root).create([source], plan_id="plan-1")


def test_flat_directory_captures_hidden_files_and_empty_directory(root: Path) -> None:
    from jarvis.snapshot import Snapshot

    builder = Snapshot(PathGuard([root]), root=root)
    empty = builder.create(directories=[root], plan_id="plan-1")
    assert empty.files == ()
    assert empty.source_directories == (str(root),)
    assert builder.verify(empty, plan_id="plan-1")
    (root / ".hidden").write_bytes(b"hidden")
    (root / "empty").write_bytes(b"")
    captured = builder.create(directories=[root], plan_id="plan-1")
    assert [entry.path for entry in captured.files] == [".hidden", "empty"]
    assert captured.files[1].size_bytes == 0


def test_nested_directory_is_rejected_not_silently_skipped(root: Path) -> None:
    from jarvis.snapshot import Snapshot, SnapshotError

    (root / "nested").mkdir()
    with pytest.raises(SnapshotError):
        Snapshot(PathGuard([root]), root=root).create(directories=[root], plan_id="plan-1")


@pytest.mark.parametrize("change", ["add", "remove", "rename"])
def test_flat_directory_membership_changes_fail_verification(root: Path, change: str) -> None:
    from jarvis.snapshot import Snapshot, SnapshotVerificationError

    source = root / "a.txt"
    source.write_bytes(b"fixture")
    builder = Snapshot(PathGuard([root]), root=root)
    frozen = builder.create(directories=[root], plan_id="plan-1")
    if change == "add":
        (root / "added.txt").write_bytes(b"new")
    elif change == "remove":
        source.unlink()
    else:
        source.rename(root / "renamed.txt")
    with pytest.raises(SnapshotVerificationError):
        builder.verify(frozen, plan_id="plan-1")


def test_missing_source_and_replaced_inode_fail_verification(root: Path) -> None:
    from jarvis.snapshot import Snapshot, SnapshotVerificationError

    source = root / "a.txt"
    source.write_bytes(b"same")
    builder = Snapshot(PathGuard([root]), root=root)
    frozen = builder.create([source], plan_id="plan-1")
    replacement = root / "replacement"
    replacement.write_bytes(b"same")
    replacement.replace(source)
    with pytest.raises(SnapshotVerificationError):
        builder.verify(frozen, plan_id="plan-1")
    source.unlink()
    with pytest.raises(SnapshotVerificationError):
        builder.verify(frozen, plan_id="plan-1")


@pytest.mark.parametrize("files,directories", [([], []), ("file", []), ([], "directory")])
def test_invalid_or_missing_source_collections_fail(
    root: Path, files: object, directories: object
) -> None:
    from jarvis.snapshot import Snapshot, SnapshotError

    with pytest.raises((SnapshotError, PathControlError)):
        Snapshot(PathGuard([root]), root=root).create(
            files,  # type: ignore[arg-type]
            directories=directories,  # type: ignore[arg-type]
            plan_id="plan-1",
        )


@pytest.mark.parametrize("plan_id", ["", "\x00", "\udcff", "   "])
def test_invalid_plan_ids_fail(root: Path, plan_id: str) -> None:
    from jarvis.snapshot import Snapshot, SnapshotError

    with pytest.raises(SnapshotError):
        Snapshot(PathGuard([root]), root=root).create(directories=[root], plan_id=plan_id)


def test_duplicate_sources_are_rejected(root: Path) -> None:
    from jarvis.snapshot import Snapshot, SnapshotError

    source = root / "a.txt"
    source.write_bytes(b"fixture")
    builder = Snapshot(PathGuard([root]), root=root)
    with pytest.raises(SnapshotError):
        builder.create([source, source], plan_id="plan-1")
    with pytest.raises(SnapshotError):
        builder.create([source], directories=[root], plan_id="plan-1")


def test_file_count_and_byte_limits_fail_closed(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jarvis.snapshot as module

    source = root / "a.txt"
    source.write_bytes(b"12345")
    builder = module.Snapshot(PathGuard([root]), root=root)
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 4)
    with pytest.raises(module.SnapshotError):
        builder.create([source], plan_id="plan-1")
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 20)
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 4)
    with pytest.raises(module.SnapshotError):
        builder.create([source], plan_id="plan-1")
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 20)
    monkeypatch.setattr(module, "MAX_FILES", 1)
    (root / "b.txt").write_bytes(b"x")
    with pytest.raises(module.SnapshotError):
        builder.create(directories=[root], plan_id="plan-1")


def test_tampered_frozen_content_or_manifest_is_rejected(root: Path) -> None:
    from jarvis.snapshot import Snapshot, SnapshotVerificationError

    source = root / "a.txt"
    source.write_bytes(b"fixture")
    builder = Snapshot(PathGuard([root]), root=root)
    frozen = builder.create([source], plan_id="plan-1")
    bad_file = replace(frozen.files[0], content=b"tampered")
    source_identities = (
        replace(frozen, files=(bad_file,)),
        replace(frozen, snapshot_id="forged"),
        replace(frozen, manifest_bytes=b"{}"),
        replace(frozen, source_root=str(root.parent)),
    )
    for bad in source_identities:
        with pytest.raises(SnapshotVerificationError):
            builder.verify(bad, plan_id="plan-1")


def test_identity_ignores_source_metadata_and_absolute_root(root: Path, tmp_path: Path) -> None:
    from jarvis.snapshot import Snapshot

    source = root / "a.txt"
    source.write_bytes(b"fixture")
    builder = Snapshot(PathGuard([root]), root=root)
    first = builder.create([source], plan_id="plan-1")
    os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 1_000_000))
    second = builder.create([source], plan_id="plan-1")
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.txt").write_bytes(b"fixture")
    third = Snapshot(PathGuard([other]), root=other).create(directories=[other], plan_id="plan-3")
    assert first.snapshot_id == second.snapshot_id == third.snapshot_id
    assert first.files[0].metadata != second.files[0].metadata


def test_content_change_while_reading_is_rejected(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jarvis.snapshot as module

    source = root / "a.txt"
    source.write_bytes(b"before")
    original_read = os.read
    changed = False

    def concurrent_read(fd: int, length: int) -> bytes:
        nonlocal changed
        chunk = original_read(fd, length)
        if chunk and not changed:
            changed = True
            source.write_bytes(b"after!")
        return chunk

    monkeypatch.setattr(os, "read", concurrent_read)
    with pytest.raises((module.SnapshotError, PathControlError)):
        module.Snapshot(PathGuard([root]), root=root).create([source], plan_id="plan-1")
    assert changed


def test_second_pass_rejects_earlier_file_changing_during_later_read(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jarvis.snapshot as module

    first, second = root / "a.txt", root / "b.txt"
    first.write_bytes(b"before")
    second.write_bytes(b"second")
    second_inode = second.stat().st_ino
    original_read = os.read
    changed = False

    def concurrent_read(fd: int, length: int) -> bytes:
        nonlocal changed
        chunk = original_read(fd, length)
        if chunk and not changed and os.fstat(fd).st_ino == second_inode:
            changed = True
            first.write_bytes(b"after!")
        return chunk

    monkeypatch.setattr(os, "read", concurrent_read)
    with pytest.raises(module.SnapshotVerificationError):
        module.Snapshot(PathGuard([root]), root=root).create([first, second], plan_id="plan-1")
    assert changed


def test_replaced_snapshot_root_rejected_even_under_broader_guard(root: Path) -> None:
    from jarvis.snapshot import Snapshot, SnapshotVerificationError

    guard = PathGuard([root.parent])
    builder = Snapshot(guard, root=root)
    frozen = builder.create(directories=[root], plan_id="plan-1")
    root.rename(root.with_name("old-root"))
    root.mkdir()
    with pytest.raises(SnapshotVerificationError):
        builder.verify(frozen, plan_id="plan-1")


def test_draft_does_not_enable_runtime(root: Path) -> None:
    from jarvis.snapshot import DRAFT_STATUS, RUNTIME_ENABLED, Snapshot

    assert DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert RUNTIME_ENABLED is False
    builder = Snapshot(PathGuard([root]), root=root)
    record = builder.create(directories=[root], plan_id="plan-1")
    assert record.status == "FROZEN_IN_MEMORY_NOT_COMMITTED"
