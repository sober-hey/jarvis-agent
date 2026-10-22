"""Single-user product data directory validation, not an OS sandbox.

Never chmod an existing directory or migrate/delete existing user data.
The legacy run subtree remains available for development compatibility.
"""

from __future__ import annotations

import os
import pwd
import stat
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
LEGACY = PROJECT / ".runs_01/b01-chat-20261014"
HOST_ROOT = Path("/Volumes/扩展硬盘/hermes")
NAMES = ("home", "engine-home", "runtime", "tmp", "cache", "config")


def safe_absolute(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Data directory requires an absolute path without traversal")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Data path contains a symbolic link")
    return path


def overlap(path: Path, other: Path) -> bool:
    return path == other or path in other.parents or other in path.parents


def protected_paths(root: Path, extra: tuple[Path, ...] = ()) -> tuple[Path, ...]:
    user_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    paths = [HOST_ROOT, user_home / ".hermes", *extra]
    for name in ("HERMES_HOME", "HERMES_ROOT"):
        value = os.environ.get(name)
        # The child's own isolated engine home is not the host profile.
        if value and Path(value) != root / "engine-home":
            paths.append(Path(value).absolute().resolve())
    return tuple(paths)


def private_directory(path: Path) -> None:
    info = path.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("Data directories must be owned by current UID with mode 0700")


def validate_root(
    root: Path,
    *,
    legacy: Path = LEGACY,
    extra: tuple[Path, ...] = (),
    validation_mode: str = "product",
) -> Path:
    if validation_mode not in {"product", "acceptance"}:
        raise ValueError("Unknown data root validation mode")
    acceptance = validation_mode == "acceptance"
    if acceptance:
        # Darwin's fixed /tmp alias is the only symlink exception. All remaining
        # components still pass safe_absolute; the caller cannot choose a prefix.
        if root.parts[:2] == ("/", "tmp"):
            root = Path("/tmp").resolve().joinpath(*root.parts[2:])
        temporary = Path("/tmp").resolve()
        if temporary not in root.parents:
            raise ValueError("Acceptance data must live in the system temporary directory")
        name = root.relative_to(temporary).parts[0]
        if not name.startswith("jarvis-acceptance-") or name == "jarvis-acceptance-":
            raise ValueError("Acceptance data requires the jarvis-acceptance- prefix")
    root = safe_absolute(root)
    legacy_ok = legacy in root.parents and legacy == LEGACY
    if overlap(root, PROJECT) and not legacy_ok:
        raise ValueError("Data directory overlaps product source tree")
    if any(overlap(root, other) for other in protected_paths(root, extra)):
        raise ValueError("Data directory overlaps host Hermes")
    nearest = next((part for part in root.parents if part.exists()), None)
    if nearest is None:
        raise ValueError("Data directory has no existing parent")
    parent = nearest.stat()
    # Only an already-created, owned 0700 acceptance root may be directly
    # beneath sticky shared /tmp. Never relax parent rules for new roots.
    temporary_parent = acceptance and root.exists() and nearest == Path("/tmp").resolve()
    if not temporary_parent and (
        parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise ValueError("Data directory requires a user-owned non-shared parent")
    if root.exists():
        private_directory(root)
        for name in NAMES:
            target = safe_absolute(root / name)
            if target.exists():
                private_directory(target)
        # Only inspect known product files; unrelated user files remain untouched.
        for name in (
            "engine-home/config.yaml",
            "engine-home/state.db",
            "engine-home/state.db-wal",
            "engine-home/state.db-shm",
            "launcher-session.lock",
            "launcher-session.json",
        ):
            target = safe_absolute(root / name)
            if target.exists():
                info = target.stat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                ):
                    raise ValueError("Product files must be owned regular files without hard links")
        attempts = safe_absolute(root / "launcher-attempts")
        if attempts.exists():
            private_directory(attempts)
    return root


def self_evaluate() -> dict[str, object]:
    """Inspect public path policy only; never stat/create a user's data root."""
    from datetime import datetime, timezone

    names = list(NAMES)
    checks = {
        "safe_directory_names": bool(names)
        and len(set(names)) == len(names)
        and all(
            isinstance(n, str)
            and n not in {"", ".", ".."}
            and Path(n).name == n
            and not Path(n).is_absolute()
            for n in names
        ),
        "absolute_boundaries": all(p.is_absolute() for p in (PROJECT, HOST_ROOT, LEGACY)),
        "legacy_is_product_subtree": PROJECT in LEGACY.parents,
        "overlap_guard": overlap(PROJECT, PROJECT / "candidate")
        and not overlap(PROJECT, HOST_ROOT),
    }
    return {
        "module": "product_home",
        "domain": "POSIX private data directories path traversal ownership permissions",
        "current_version": "1.0.0",
        "known_limitations": [
            "Directory validation is not an OS sandbox or race-free descriptor traversal.",
            "This evaluation inspects policy only, not production directories, ownership or permissions.",
        ],
        "improvement_directions": [
            "Evaluate descriptor-based traversal against symlink replacement races."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "directory_names": names,
        "checks": checks,
        "ok": all(checks.values()),
    }


def make_private(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for target in reversed(missing):
        target.mkdir(mode=0o700, exist_ok=True)
        private_directory(target)
    private_directory(path)


def prepare_root(path: Path | None, *, legacy: Path = LEGACY) -> Path:
    if path is None:
        value = os.environ.get("JARVIS_HOME")
        home = Path(value) if value else Path(pwd.getpwuid(os.getuid()).pw_dir) / ".jarvis"
        home = validate_root(home, legacy=legacy)
        make_private(home)
        path = home / "sessions"
    root = validate_root(path, legacy=legacy)
    make_private(root)
    for name in NAMES:
        make_private(root / name)
    return validate_root(root, legacy=legacy)
