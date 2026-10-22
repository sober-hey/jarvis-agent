#!/usr/bin/env python3
"""Fail-closed, offline-safe model-route migration for JARVIS data roots.

The command is dry-run by default.  ``--apply`` requires explicit operator
consent and explicit data roots; production application is intentionally not
performed by this task.  Use it only after an independent maintenance window.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Generator, Mapping
from pathlib import Path
from typing import Any, cast

import product_config

VERSION = 1
ENGINE_RELATIVE = Path("engine-home/config.yaml")
_ALLOWED_LEDGER_STATES = {"started", "completed", "unknown"}
Report = dict[str, Any]


class MigrationError(RuntimeError):
    """A migration precondition or safe-write check failed."""


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _private(path: Path, *, directory: bool) -> None:
    try:
        info = path.stat()
    except OSError as exc:
        raise MigrationError("migration path is not readable") from exc
    wanted = stat.S_ISDIR if directory else stat.S_ISREG
    if not wanted(info.st_mode) or info.st_uid != os.getuid():
        raise MigrationError("migration paths must be owned by the current user")
    if directory:
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise MigrationError("migration directories must be mode 0700")
    elif stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise MigrationError("migration files must be regular 0600 files")


def _safe_existing(path: Path, *, directory: bool = False) -> Path:
    path = path.absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise MigrationError("symlink paths are rejected")
    if not path.exists():
        raise MigrationError("migration path is missing")
    _private(path, directory=directory)
    return path


def _safe_child(path: Path, *, directory: bool = False) -> Path:
    path = path.absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise MigrationError("symlink paths are rejected")
    if path.exists():
        _private(path, directory=directory)
    return path


def _canonical_config(values: Mapping[str, object]) -> bytes:
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def expected_engine(values: Mapping[str, object]) -> bytes:
    """Return precisely the generated engine JSON used by chat_launcher."""
    return json.dumps(
        {
            "model": {
                "default": values["model"],
                "provider": values["provider"],
                "base_url": values["base_url"],
                "context_length": values["context_length"],
            },
            "compression": {"enabled": False},
        }
    ).encode()


def _validate_apply_scope(config_path: Path, candidate_path: Path, roots: list[Path]) -> None:
    """Reject symlinked paths; explicit --apply is the production consent gate."""
    paths = [config_path, candidate_path, *roots]
    for path in paths:
        absolute = path.absolute()
        if any(part.is_symlink() for part in (absolute, *absolute.parents)):
            raise MigrationError("--apply refuses symlink paths")


def discover_runtime_roots(
    config_path: Path, explicit_roots: list[Path] | None = None
) -> list[Path]:
    """Discover source-configured roots without creating or changing anything."""
    if explicit_roots:
        roots = [Path(value).absolute() for value in explicit_roots]
    else:
        home = config_path.absolute().parent
        candidates = [
            home,
            home / "loop-data",
            home / "sessions",
            home / "tasks",
        ]
        env_home = os.environ.get("JARVIS_HOME")
        if env_home:
            candidates.extend([Path(env_home).absolute(), Path(env_home).absolute() / "loop-data"])
        roots = candidates
    result: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        root = root.absolute()
        if root in seen:
            continue
        seen.add(root)
        if not root.exists():
            continue
        _safe_existing(root, directory=True)
        result.append(root)
    return result


@contextlib.contextmanager
def _lock(path: Path, *, create: bool) -> Generator[int, None, None]:
    path = _safe_child(path)
    flags = os.O_RDWR | (os.O_CREAT if create else 0) | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise MigrationError("migration lock is unavailable") from exc
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise MigrationError("migration lock is busy") from exc
        yield fd
    finally:
        os.close(fd)


def _lock_all(config_path: Path, roots: list[Path]) -> contextlib.ExitStack:
    stack = contextlib.ExitStack()
    try:
        # Stable order prevents two migrations from deadlocking each other.
        paths = [config_path.with_name("." + config_path.name + ".lock")]
        paths.extend(root / "launcher-session.lock" for root in roots)
        for path in sorted({p.absolute() for p in paths}, key=str):
            stack.enter_context(_lock(path, create=True))
        return stack
    except BaseException:
        stack.close()
        raise


def _check_ledgers(roots: list[Path]) -> None:
    for root in roots:
        directory = root / "launcher-attempts"
        if not directory.exists():
            continue
        _safe_existing(directory, directory=True)
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file():
                raise MigrationError("attempt ledger contains an unsafe entry")
            _private(path, directory=False)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise MigrationError("attempt ledger is corrupt") from exc
            if not isinstance(payload, dict):
                raise MigrationError("attempt ledger is corrupt")
            record = cast(dict[str, object], payload)
            if record.get("state") not in _ALLOWED_LEDGER_STATES:
                raise MigrationError("attempt ledger is corrupt")
            if record.get("state") == "started":
                raise MigrationError("a started attempt makes migration unsafe")


def _existing_engine_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        engine = root / ENGINE_RELATIVE
        if engine.exists():
            _safe_existing(engine)
            files.append(engine)
    return files


def _atomic_bytes(path: Path, payload: bytes) -> None:
    parent = _safe_existing(path.parent, directory=True)
    fd, temporary = tempfile.mkstemp(prefix=".migration-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    finally:
        Path(temporary).unlink(missing_ok=True)


def _backup_file(source: Path, destination: Path) -> None:
    payload = source.read_bytes()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    _atomic_bytes(destination, payload)


def _manifest_dir(config_path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime()) + "-" + os.urandom(3).hex()
    target = config_path.parent / "model-migrations" / stamp
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    return target


def _read_candidate(candidate_path: Path, test_port: int | None) -> product_config.ConfigSnapshot:
    try:
        return product_config.read_config(candidate_path, test_port=test_port)
    except (product_config.ConfigError, OSError, TypeError) as exc:
        raise MigrationError("candidate route is invalid") from exc


def _read_current(config_path: Path, test_port: int | None) -> product_config.ConfigSnapshot:
    try:
        return product_config.read_config(config_path, test_port=test_port)
    except (product_config.ConfigError, OSError, TypeError) as exc:
        raise MigrationError("current route is invalid") from exc


def migrate(
    candidate_path: Path,
    *,
    config_path: Path | None = None,
    data_roots: list[Path] | None = None,
    apply: bool = False,
    test_port: int | None = None,
) -> Report:
    """Plan or apply a route migration after strict precondition checks."""
    config_path = (config_path or product_config.default_config_path()).absolute()
    candidate_path = candidate_path.absolute()
    if apply:
        if not data_roots:
            raise MigrationError("--apply requires explicit --data-root values")
        _validate_apply_scope(config_path, candidate_path, [Path(p).absolute() for p in data_roots])
    roots = discover_runtime_roots(config_path, data_roots)
    if not roots:
        raise MigrationError("no existing runtime roots were discovered")
    current = _read_current(config_path, test_port)
    candidate = _read_candidate(candidate_path, test_port)
    old_values = dict(current.values)
    new_values = dict(candidate.values)
    old_engine = expected_engine(old_values)
    new_engine = expected_engine(new_values)
    engine_files = _existing_engine_files(roots)
    if any(path.read_bytes() != old_engine for path in engine_files):
        raise MigrationError("existing engine configuration does not match current route")
    report: Report = {
        "version": VERSION,
        "status": "apply_pending" if apply else "dry_run",
        "config_path": str(config_path),
        "roots": [str(root) for root in roots],
        "engine_files": [str(path) for path in engine_files],
        "old_config_sha256": current.sha256,
        "new_config_sha256": candidate.sha256,
        "old_engine_sha256": _sha_bytes(old_engine),
        "new_engine_sha256": _sha_bytes(new_engine),
    }
    if not apply:
        return report
    with _lock_all(config_path, roots):
        # Re-read all guarded state after acquiring every lock.
        _check_ledgers(roots)
        current = _read_current(config_path, test_port)
        if current.sha256 != report["old_config_sha256"]:
            raise MigrationError("current route changed while migration was preparing")
        engine_files = _existing_engine_files(roots)
        if any(path.read_bytes() != old_engine for path in engine_files):
            raise MigrationError("engine configuration changed or mismatches current route")
        migration_dir = _manifest_dir(config_path)
        backup_dir = migration_dir / "backup"
        backup_dir.mkdir(mode=0o700)
        backup_paths: dict[str, str] = {}
        _backup_file(config_path, backup_dir / "config.json")
        backup_paths[str(config_path)] = str(backup_dir / "config.json")
        for index, engine in enumerate(engine_files):
            backup = backup_dir / f"engine-{index:03d}.config.yaml"
            _backup_file(engine, backup)
            backup_paths[str(engine)] = str(backup)
        manifest = {
            **report,
            "status": "applied",
            "backup_paths": backup_paths,
        }
        manifest_path = migration_dir / "manifest.json"
        try:
            _atomic_bytes(config_path, _canonical_config(new_values))
            for engine in engine_files:
                _atomic_bytes(engine, new_engine)
            readback = _read_current(config_path, test_port)
            if readback.sha256 != candidate.sha256 or any(
                engine.read_bytes() != new_engine for engine in engine_files
            ):
                raise MigrationError("migration readback mismatch")
            _atomic_bytes(manifest_path, json.dumps(manifest, sort_keys=True).encode())
        except BaseException as exc:
            try:
                _atomic_bytes(config_path, (backup_dir / "config.json").read_bytes())
                for engine, backup in (
                    (path, Path(value))
                    for path, value in backup_paths.items()
                    if path != str(config_path)
                ):
                    _atomic_bytes(Path(engine), backup.read_bytes())
                if _read_current(config_path, test_port).sha256 != report["old_config_sha256"]:
                    raise MigrationError("rollback readback mismatch")
                if any(path.read_bytes() != old_engine for path in engine_files):
                    raise MigrationError("rollback engine readback mismatch")
            except BaseException as rollback_exc:
                raise MigrationError(
                    "migration failed and rollback could not be verified"
                ) from rollback_exc
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError("migration write failed; originals restored") from exc
        report = manifest
        report["manifest_path"] = str(manifest_path)
        return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path, help="validated route JSON")
    parser.add_argument("--config", type=Path, help="current config.json")
    parser.add_argument("--data-root", action="append", type=Path, dest="data_roots")
    parser.add_argument(
        "--apply", action="store_true", help="apply after explicit locks and precondition checks"
    )
    parser.add_argument("--test-port", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = migrate(
            args.candidate,
            config_path=args.config,
            data_roots=args.data_roots,
            apply=args.apply,
            test_port=args.test_port,
        )
    except MigrationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def self_evaluate() -> dict[str, object]:
    """Offline declaration only; never reads a live config or writes a data root."""
    return {
        "module": "switch_model",
        "domain": "One-shot engine-home model configuration migration CLI",
        "current_version": "1.0.0",
        "known_limitations": [
            "Dry-run is the default; nothing changes until --apply is passed explicitly.",
            "It never restarts a service or a daemon, so a running process keeps its old model.",
            "It refuses symbol links, damaged ledgers and in-flight started attempts; a stuck "
            "lock must be cleared by an operator.",
            "Offline tests use temporary data roots only; no production migration is verified.",
        ],
        "improvement_directions": [
            "Record an operator-visible migration receipt next to the config backup.",
            "Add a read-back diff summary so an operator can see exactly which roots changed.",
        ],
        "last_evaluated_at": None,
    }
