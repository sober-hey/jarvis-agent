"""JARVIS-owned non-secret route configuration and credential boundary.

This module is deliberately independent of the host Hermes resolver/configuration.
It stores only six route fields. Provider credentials are accepted only from a
0600 dedicated key file or an already-open file descriptor and are never written
by this module. The single-user checks here are filesystem checks, not an OS
sandbox or a public-service security boundary.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import select
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple, cast
from urllib.parse import urlsplit

CONFIG_FIELDS = frozenset(
    {"model", "provider", "base_url", "api_mode", "context_length", "reasoning_effort"}
)
BINDING_VERSION = 1
MAX_CONFIG_BYTES = 65536
MAX_KEY_BYTES = 4096


class ConfigError(ValueError):
    """Configuration is malformed or outside the product route policy."""


class ConfigSecurityError(ConfigError):
    """A path, owner, permission, or endpoint violates the local boundary."""


class ConfigConflictError(ConfigError):
    """The caller attempted to overwrite a changed or unacknowledged config."""


class CredentialError(ConfigSecurityError):
    """Credentials were not supplied through an approved local channel."""


class ApiCredentials:
    """Metadata facade for the logical credential resource owned by this module.

    Actual credential ingress remains read_key; this hook never retrieves keys.
    """

    @staticmethod
    def self_evaluate() -> dict[str, object]:
        """Describe the credential boundary independently of route metadata."""
        from datetime import datetime, timezone

        return {
            "module": "api_credentials",
            "domain": "explicit key-file and anonymous-pipe credential ingress boundary",
            "current_version": (
                "product_config.read_key validates exclusive file/FD input, private file "
                "ownership and modes, bounded UTF-8 bytes and single-line key syntax"
            ),
            "known_limitations": [
                "Credentials are returned as Python text to the caller, not held in a separate broker process or guaranteed zeroized.",
                "FD reads bound each select wait but do not enforce a whole-read deadline; accepted pipe descriptors are a local trust boundary.",
                "The boundary validates credential syntax and filesystem metadata, not provider validity, revocation or rotation.",
            ],
            "improvement_directions": [
                "Introduce a total read deadline and distinguish syntax acceptance from provider-side authentication health.",
                "Move long-lived credential custody to an explicitly designed broker before making process-isolation claims.",
            ],
            "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        }


class ConfigSnapshot(NamedTuple):
    path: Path
    values: Mapping[str, object]
    sha256: str


class RouteBinding(NamedTuple):
    values: Mapping[str, object]
    data_root: Path
    config_sha256: str
    data_root_sha256: str
    binding_sha256: str
    version: int = BINDING_VERSION


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(values: Mapping[str, object]) -> bytes:
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def sha256_config(values: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical(values)).hexdigest()


def default_config_path() -> Path:
    return Path.home() / ".jarvis" / "config.json"


def _protected_roots() -> tuple[Path, ...]:
    roots = [Path("/Volumes/扩展硬盘/hermes"), Path.home() / ".hermes"]
    for name in ("HERMES_HOME", "HERMES_ROOT", "HERMES_DIR", "HERMES_REPO_ROOT"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value).absolute().resolve())
    return tuple(roots)


def _safe_path(path: Path) -> Path:
    path = path.absolute()
    if ".." in path.parts:
        raise ConfigSecurityError("路径不能包含目录遍历")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ConfigSecurityError("配置路径不能包含符号链接")
    if any(
        path == root or path in root.parents or root in path.parents for root in _protected_roots()
    ):
        raise ConfigSecurityError("配置路径不能指向宿主 Hermes")
    return path


def _check_owner_mode(path: Path, *, directory: bool) -> os.stat_result:
    try:
        info = path.stat()
    except OSError as exc:
        raise ConfigSecurityError("配置路径不可读") from exc
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode) or info.st_uid != os.getuid():
        raise ConfigSecurityError("配置路径必须是当前用户拥有的专用路径")
    mode = stat.S_IMODE(info.st_mode)
    if directory and mode != 0o700:
        raise ConfigSecurityError("配置目录必须为 0700")
    if not directory and (mode != 0o600 or info.st_nlink != 1):
        raise ConfigSecurityError("配置文件必须为无硬链接的 0600 文件")
    return info


def _validate_parent(path: Path, *, create: bool) -> None:
    parent = path.parent
    if create:
        missing: list[Path] = []
        current = parent
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
    _check_owner_mode(parent, directory=True)


def validate_config(
    values: Mapping[str, object], *, test_port: int | None = None
) -> dict[str, object]:
    if set(values) != set(CONFIG_FIELDS):
        raise ConfigError("配置必须只包含六个非 secret 模型字段")
    result = dict(values)
    for name in ("model", "provider", "base_url", "api_mode"):
        value = result[name]
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ConfigError("配置文本字段无效")
    if result["provider"] not in {"custom", "openai", "openrouter"}:
        raise ConfigError("provider 不在允许范围")
    if result["api_mode"] not in {"chat_completions", "responses"}:
        raise ConfigError("api_mode 不在允许范围")
    context_length = result["context_length"]
    if type(context_length) is not int or not 65536 <= context_length <= 2_000_000:
        raise ConfigError("context_length 必须是明确的有界整数")
    if result["reasoning_effort"] not in {
        None,
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    }:
        raise ConfigError("reasoning_effort 不在允许范围")
    endpoint = urlsplit(cast(str, result["base_url"]))
    if (
        endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or not endpoint.hostname
    ):
        raise ConfigError("base_url 不得含凭据、查询或片段")
    if test_port is None:
        if endpoint.scheme != "https" or (endpoint.port or 443) != 443:
            raise ConfigError("普通模式只允许 HTTPS 443 endpoint")
    elif (
        endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.port != test_port
    ):
        raise ConfigError("loopback endpoint 仅允许显式 test mode")
    return result


def read_config(path: Path | None = None, *, test_port: int | None = None) -> ConfigSnapshot:
    target = _safe_path(path or default_config_path())
    _validate_parent(target, create=False)
    info = _check_owner_mode(target, directory=False)
    if info.st_size > MAX_CONFIG_BYTES:
        raise ConfigError("配置超过大小限制")
    try:
        value: object = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("配置不是可读 JSON") from exc
    if not isinstance(value, dict):
        raise ConfigError("配置必须是 JSON object")
    values = validate_config(cast(Mapping[str, object], value), test_port=test_port)
    return ConfigSnapshot(target, MappingProxyType(values), sha256_config(values))


def _lock_path(target: Path) -> Path:
    return target.with_name("." + target.name + ".lock")


def write_config(
    path: Path | None,
    values: Mapping[str, object],
    *,
    expected_sha256: str | None = None,
    test_port: int | None = None,
) -> ConfigSnapshot:
    target = _safe_path(path or default_config_path())
    values = validate_config(values, test_port=test_port)
    _validate_parent(target, create=True)
    lock = _lock_path(target)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        current: ConfigSnapshot | None = None
        if target.exists():
            current = read_config(target, test_port=test_port)
            if expected_sha256 is None or expected_sha256 != current.sha256:
                raise ConfigConflictError("配置已存在或已改变，必须提供匹配的 expected_sha256")
        elif expected_sha256 is not None:
            raise ConfigConflictError("配置尚不存在，expected_sha256 不匹配")
        payload = _canonical(values)
        temporary_fd, temporary_name = tempfile.mkstemp(prefix=".config-", dir=target.parent)
        try:
            os.fchmod(temporary_fd, 0o600)
            with os.fdopen(temporary_fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
            directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
    finally:
        os.close(fd)
    return read_config(target, test_port=test_port)


def _read_key_fd(fd: int) -> str:
    if fd < 0:
        raise CredentialError("凭据 FD 无效")
    try:
        info = os.fstat(fd)
    except OSError as exc:
        raise CredentialError("凭据 FD 不可读") from exc
    if not stat.S_ISFIFO(info.st_mode) and not (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
    ):
        raise CredentialError("凭据 FD 必须是匿名管道或当前用户拥有的 0600 文件")
    data = bytearray()
    while True:
        if not select.select([fd], [], [], 5)[0]:
            raise CredentialError("凭据 FD 读取超时")
        block = os.read(fd, MAX_KEY_BYTES + 1 - len(data))
        if not block:
            break
        data.extend(block)
        if len(data) > MAX_KEY_BYTES:
            raise CredentialError("凭据超过大小限制")
    try:
        value = data.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError as exc:
        raise CredentialError("凭据必须是 UTF-8 文本") from exc
    if not 8 <= len(value) <= MAX_KEY_BYTES or any(c.isspace() or ord(c) < 32 for c in value):
        raise CredentialError("凭据必须是单行非空文本")
    return value


def read_key(*, key_file: Path | None = None, key_fd: int | None = None) -> str:
    if (key_file is None) == (key_fd is None):
        raise CredentialError("必须选择专用 key-file 或 key-fd")
    if key_file is not None:
        try:
            target = _safe_path(key_file)
        except ConfigSecurityError as exc:
            raise CredentialError("凭据路径必须是无符号链接的专用文件") from exc
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise CredentialError("凭据文件不可读") from exc
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise CredentialError("凭据文件必须是当前用户拥有的 0600 普通文件")
            return _read_key_fd(fd)
        finally:
            os.close(fd)
    return _read_key_fd(cast(int, key_fd))


def bind_route(
    values: Mapping[str, object], data_root: Path, *, test_port: int | None = None
) -> RouteBinding:
    validated = validate_config(values, test_port=test_port)
    root = _safe_path(data_root)
    _check_owner_mode(root, directory=True)
    config_digest = sha256_config(validated)
    data_digest = sha256_text(str(root))
    binding_digest = sha256_text(
        json.dumps(
            {
                "version": BINDING_VERSION,
                "config_sha256": config_digest,
                "data_root_sha256": data_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return RouteBinding(
        MappingProxyType(validated),
        root,
        config_digest,
        data_digest,
        binding_digest,
    )


def check(path: Path | None = None, *, test_port: int | None = None) -> dict[str, object]:
    try:
        target = _safe_path(path or default_config_path())
        if not target.exists():
            return {"ok": False, "error": "missing"}
        snapshot = read_config(target, test_port=test_port)
    except FileNotFoundError:
        return {"ok": False, "error": "missing"}
    except ConfigError as exc:
        return {"ok": False, "error": type(exc).__name__}
    return {"ok": True, "path": str(snapshot.path), "sha256": snapshot.sha256}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser(
        "check", help="read and validate config without side effects"
    )
    check_parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if args.command == "check":
        result = check(args.config)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    return 2


def self_evaluate() -> dict[str, object]:
    """Describe source capabilities and gaps without accessing private state."""
    from datetime import datetime, timezone

    return {
        "module": "product_config",
        "domain": "non-secret product model routing configuration and explicit credential channel "
        "boundary",
        "current_version": "strict six-field route schema, SHA-256 optimistic writes with "
        "flock/fsync, private key-file or file-descriptor input and route binding "
        "hashes",
        "known_limitations": [
            "Filesystem ownership and mode checks are a local single-user boundary, "
            "not an OS sandbox; path validation is not descriptor-relative for "
            "every ancestor.",
            "Configuration validation checks endpoint syntax and HTTPS policy, not "
            "DNS destination, provider availability or model compatibility.",
            "Credential FD reads use a five-second wait per block rather than a "
            "single whole-read deadline, and credentials remain Python text in "
            "caller memory.",
        ],
        "improvement_directions": [
            "Adopt descriptor-relative path traversal to narrow time-of-check "
            "versus time-of-use replacement races.",
            "Add explicit route compatibility diagnostics and bounded "
            "whole-read credential deadlines without persisting or logging "
            "secrets.",
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
