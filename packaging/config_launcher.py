#!/usr/bin/env python3
"""JARVIS-owned read-only configuration check and future launcher handoff.

check/--check requires explicit config, data-dir and key-file/key-fd. No creation,
network or chat imports occur. The fake-only test adapter is an explicit Python
API, not a production launch command. Existing chat_launcher still uses its own
parser; a validated handoff does not mean its parser has been replaced.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, NoReturn, cast

import product_config as config
import product_home as home
from context_limits import CHAT_CONTEXT_BYTE_LIMIT

VERSION = "1.0.0-preparation"


class CheckedRoute(NamedTuple):
    config_path: Path
    binding: config.RouteBinding
    test_port: int | None

    def summary(self) -> dict[str, object]:
        return {
            "ok": True,
            "version": VERSION,
            "binding_version": self.binding.version,
            "config_sha256": self.binding.config_sha256,
            "data_root_sha256": self.binding.data_root_sha256,
            "binding_sha256": self.binding.binding_sha256,
            "route": dict(self.binding.values),
            "consumer": "chat_launcher independent parser; product_config wiring pending",
        }


def check_route(
    config_path: Path,
    data_dir: Path,
    *,
    key_file: Path | None = None,
    key_fd: int | None = None,
    expected_sha256: str | None = None,
    test_port: int | None = None,
) -> CheckedRoute:
    """Read only; key material is checked in memory and excluded from return."""
    if not config_path.is_absolute() or not data_dir.is_absolute():
        raise config.ConfigSecurityError("explicit absolute paths required")
    if test_port is not None and not 1 <= test_port <= 65535:
        raise config.ConfigError("invalid test port")
    snapshot = config.read_config(config_path, test_port=test_port)
    if expected_sha256 is not None and expected_sha256 != snapshot.sha256:
        raise config.ConfigConflictError("configuration changed")
    try:
        root = home.validate_root(data_dir)
    except (ValueError, OSError):
        raise config.ConfigSecurityError("invalid data root") from None
    binding = config.bind_route(snapshot.values, root, test_port=test_port)
    key = config.read_key(key_file=key_file, key_fd=key_fd)
    metadata = json.dumps(dict(binding.values), ensure_ascii=False) + str(root) + str(snapshot.path)
    if key in metadata or (test_port is not None and not key.startswith("FAKE-")):
        raise config.CredentialError("credential outside allowed channel")
    return CheckedRoute(snapshot.path, binding, test_port)


def launcher_arguments(
    checked: CheckedRoute,
    *,
    expected_sha256: str,
    key_fd: int,
    attempt_id: str,
    prompt: str,
    session: str | None = None,
) -> list[str]:
    """Recheck route/root, return non-secret args; caller supplies credential FD.

    FD ownership remains with caller. This is a preparation API, not atomic
    config-to-launch protection: the existing consumer rereads its config file.
    """
    current = config.read_config(checked.config_path, test_port=checked.test_port)
    if current.sha256 != expected_sha256 or expected_sha256 != checked.binding.config_sha256:
        raise config.ConfigConflictError("configuration changed")
    try:
        home.validate_root(checked.binding.data_root)
    except (ValueError, OSError):
        raise config.ConfigSecurityError("invalid data root") from None
    binding = config.bind_route(
        current.values, checked.binding.data_root, test_port=checked.test_port
    )
    if binding.binding_sha256 != checked.binding.binding_sha256:
        raise config.ConfigConflictError("binding changed")
    if key_fd < 0 or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", attempt_id):
        raise config.ConfigError("invalid credential FD or attempt")
    if (
        not prompt.strip()
        or len(prompt.encode("utf-8")) > CHAT_CONTEXT_BYTE_LIMIT
        or (session is not None and not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", session))
    ):
        raise config.ConfigError("invalid prompt or session")
    args = [
        "--config",
        str(checked.config_path),
        "--data-dir",
        str(checked.binding.data_root),
        "--key-fd",
        str(key_fd),
        "--attempt-id",
        attempt_id,
        "--prompt",
        prompt,
    ]
    if checked.test_port is not None:
        args.extend(["--test-local-port", str(checked.test_port)])
    if session is not None:
        args.extend(["--session", session])
    return args


def test_adapter(
    checked: CheckedRoute,
    *,
    key_file: Path | None = None,
    key_fd: int | None = None,
    attempt_id: str,
    prompt: str,
) -> dict[str, object]:
    """Explicit fake HTTP adapter; fixed consumer, clean env, key via pipe only."""
    if checked.test_port is None:
        raise config.ConfigSecurityError("adapter requires fake test mode")
    key = config.read_key(key_file=key_file, key_fd=key_fd)
    if not key.startswith("FAKE-"):
        raise config.CredentialError("adapter accepts fake credentials only")
    if key in prompt or key in attempt_id:
        raise config.CredentialError("credential outside allowed channel")
    read, write = os.pipe()
    try:
        args = launcher_arguments(
            checked,
            expected_sha256=checked.binding.config_sha256,
            key_fd=read,
            attempt_id=attempt_id,
            prompt=prompt,
        )
        if key in json.dumps(args):
            raise config.CredentialError("credential outside allowed channel")
        os.write(write, key.encode())
        os.close(write)
        write = -1
        interpreter = Path(__file__).resolve().parents[1] / ".venv/bin/python"
        process = subprocess.run(
            [str(interpreter), "-B", str(Path(__file__).with_name("chat_launcher.py")), *args],
            pass_fds=(read,),
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
            },
            cwd=checked.binding.data_root,
            capture_output=True,
            text=True,
            timeout=75,
        )
        if key in process.stdout + process.stderr or len(process.stdout) > 262144:
            raise config.ConfigSecurityError("unsafe consumer output suppressed")
        if process.returncode != 0:
            raise config.ConfigError("consumer rejected attempt; do not retry automatically")
        raw: object = json.loads(process.stdout)
        if not isinstance(raw, dict):
            raise config.ConfigError("invalid consumer response")
        result = cast(dict[str, object], raw)
        if result.get("ok") is not True:
            raise config.ConfigError("invalid consumer response")
        return result
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        if isinstance(exc, config.ConfigError):
            raise
        raise config.ConfigError("adapter failure; outcome may be unknown") from None
    finally:
        os.close(read)
        if write >= 0:
            os.close(write)


def self_evaluate() -> dict[str, object]:
    """Read public schema and prompt policy only; never check a route or key."""
    from datetime import datetime, timezone

    fields = sorted(config.CONFIG_FIELDS)
    checks = {
        "nonsecret_route_schema": set(fields)
        == {"model", "provider", "base_url", "api_mode", "context_length", "reasoning_effort"},
        "positive_prompt_budget": type(CHAT_CONTEXT_BYTE_LIMIT) is int
        and CHAT_CONTEXT_BYTE_LIMIT > 0,
        "handoff_record_fields": CheckedRoute._fields == ("config_path", "binding", "test_port"),
    }
    return {
        "module": "config_launcher",
        "domain": "configuration route binding credential FD handoff prompt validation",
        "current_version": VERSION,
        "known_limitations": [
            "Preparation API only; chat_launcher still has an independent parser.",
            "Config recheck and consumer reread are not an atomic config-to-launch transaction.",
            "Evaluation does not read a config, credential, data root or model.",
        ],
        "improvement_directions": [
            "Unify consumer handoff validation without placing credentials in argv or reports."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "route_fields": fields,
        "chat_byte_limit": CHAT_CONTEXT_BYTE_LIMIT,
        "checks": checks,
        "ok": all(checks.values()),
    }


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise config.ConfigError("invalid arguments; use --help; do not pass keys in argv")


def main(argv: list[str] | None = None) -> int:
    parser = Parser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    sub.add_parser("version")
    check = sub.add_parser("check")
    check.add_argument("--config", type=Path, required=True)
    check.add_argument("--data-dir", type=Path, required=True)
    source = check.add_mutually_exclusive_group(required=True)
    source.add_argument("--key-file", type=Path)
    source.add_argument("--key-fd", type=int)
    check.add_argument("--expected-sha256")
    check.add_argument("--test-local-port", type=int)
    supplied = list(sys.argv[1:] if argv is None else argv)
    if supplied and supplied[0] == "--check":
        supplied[0] = "check"
    try:
        args = parser.parse_args(supplied)
        if args.command == "version":
            result: dict[str, object] = {"ok": True, "version": VERSION}
        else:
            result = check_route(
                args.config,
                args.data_dir,
                key_file=args.key_file,
                key_fd=args.key_fd,
                expected_sha256=args.expected_sha256,
                test_port=args.test_local_port,
            ).summary()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (config.ConfigError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
