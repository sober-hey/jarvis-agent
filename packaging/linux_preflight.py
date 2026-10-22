#!/usr/bin/env python3
"""Read-only environment preflight; standard library only, Python >= 3.12."""

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Literal, TypedDict

Status = Literal["PASS", "FAIL", "NOT_RUN", "NOT_VERIFIED", "NOT_APPLICABLE"]
type JSONValue = str | int | float | bool | None | list[JSONValue] | dict[str, JSONValue]


class Classification(TypedDict):
    category: str
    target_match: Status
    synthetic: bool
    evidence_kind: str
    production_ready: bool


def parse_os_release(text: str) -> dict[str, str]:
    """Parse literal assignments; never evaluate shell commands or variables."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            continue
        try:
            parts = shlex.split(value, comments=False, posix=True)
        except ValueError:
            continue
        if len(parts) <= 1:
            values[key] = parts[0] if parts else ""
    return values


def classify_environment(
    system: str, machine: str, release: dict[str, str], *, synthetic: bool
) -> Classification:
    """OS identity only; even a target match cannot establish ECS or RPM variant."""
    category = "unsupported"
    status: Status = "NOT_APPLICABLE"
    if system == "Darwin":
        category = "development_macos"
    elif system == "Linux":
        category = "linux_generic"
        if not release.get("ID") or (
            release.get("ID") == "alinux" and not release.get("VERSION_ID")
        ):
            status = "NOT_VERIFIED"
        elif (
            release.get("ID") == "alinux"
            and release.get("VERSION_ID", "").split(".")[0] == "4"
            and machine == "x86_64"
        ):
            category = "target_alibaba_linux4_x86_64"
            status = "PASS"
        else:
            status = "FAIL"
    return {
        "category": category,
        "target_match": status,
        "synthetic": synthetic,
        "evidence_kind": "fixture_branch_only" if synthetic else "local_observation",
        "production_ready": False,
    }


def collect_environment() -> dict[str, JSONValue]:
    """Observe this process and fixed system/build locations without creating files."""
    system = platform.system()
    cwd = Path.cwd()
    root = Path(__file__).resolve().parents[1]
    release: dict[str, str] = {}
    release_status: Status = "NOT_APPLICABLE"
    release_error: str | None = None
    if system == "Linux":
        try:
            parsed = parse_os_release(Path("/etc/os-release").read_text(encoding="utf-8"))
            # Export only OS identity fields, not arbitrary file contents.
            release = {
                key: parsed[key]
                for key in ("ID", "NAME", "VERSION_ID", "PRETTY_NAME", "VARIANT_ID", "BUILD_ID")
                if key in parsed
            }
            release_status = "PASS" if release.get("ID") else "NOT_VERIFIED"
        except (OSError, UnicodeError) as exc:
            release_status = "NOT_VERIFIED"
            release_error = type(exc).__name__
    classification = classify_environment(system, platform.machine(), release, synthetic=False)
    filesystem: dict[str, JSONValue] = {
        "status": "NOT_VERIFIED",
        "path": str(cwd),
        "type_status": "NOT_VERIFIED",
        "durability_status": "NOT_RUN",
    }
    try:
        usage = shutil.disk_usage(cwd)
        filesystem.update(
            status="PASS", total_bytes=usage.total, used_bytes=usage.used, free_bytes=usage.free
        )
        if hasattr(os, "statvfs"):
            stats = os.statvfs(cwd)
            filesystem["available_to_effective_user_bytes"] = stats.f_bavail * stats.f_frsize
    except OSError as exc:
        filesystem["status"] = "NOT_VERIFIED"
        filesystem["error"] = type(exc).__name__
    cgroup: dict[str, JSONValue] = {
        "status": "NOT_APPLICABLE",
        "v2_detected": False,
        "v1_detected": False,
        "membership_detected": False,
        "resource_limits_status": "NOT_RUN",
    }
    if system == "Linux":
        try:
            v2 = Path("/sys/fs/cgroup/cgroup.controllers").is_file()
            membership = Path("/proc/self/cgroup").read_text(encoding="utf-8")
            # No membership paths exported; these may carry tenant identifiers.
            lines = membership.splitlines()
            v1 = any(len(line.split(":")) == 3 and line.split(":")[1] for line in lines)
            cgroup.update(
                status="PASS", v2_detected=v2, v1_detected=v1, membership_detected=bool(lines)
            )
        except (OSError, UnicodeError) as exc:
            cgroup.update(status="NOT_VERIFIED", error=type(exc).__name__)
    artifacts: dict[str, JSONValue] = {}
    for name, relative in {
        "control_python": ".venv/bin/python",
        "control_lock": "uv.lock",
        "engine_python": "packaging/runtime/engine-venv/bin/python",
        "engine_venv_config": "packaging/runtime/engine-venv/pyvenv.cfg",
        "vendor_manifest": "packaging/VENDOR_MANIFEST.json",
        "engine_source": "components/agent_engine/upstream/run_agent.py",
        "engine_lock": "components/agent_engine/upstream/uv.lock",
    }.items():
        try:
            present = (root / relative).is_file()
            artifacts[name] = {
                "path": relative,
                "present": present,
                "status": "PASS" if present else "FAIL",
            }
        except OSError as exc:
            artifacts[name] = {
                "path": relative,
                "present": None,
                "status": "NOT_VERIFIED",
                "error": type(exc).__name__,
            }
    target_status = classification["target_match"]
    python_ok = sys.version_info[:2] >= (3, 12)
    gate: Status = target_status
    if gate == "PASS" and (not python_ok or filesystem["status"] != "PASS"):
        gate = "FAIL"
    checks: dict[str, JSONValue] = {
        "ecs_image_identity": "NOT_RUN",
        "standard_rpm_variant": "NOT_VERIFIED",
        "essd_ext4": "NOT_RUN",
        "sqlite_wal_full_foreign_keys": "NOT_RUN",
        "file_publish_fsync": "NOT_RUN",
        "power_loss_durability": "NOT_RUN",
        "product_fault_recovery": "NOT_RUN",
        "permissions_isolation": "NOT_RUN",
        "backup_restore_b1_b2": "NOT_RUN",
        "systemd_lifecycle": "NOT_RUN",
        "24_hour_stability": "NOT_RUN",
        "long_term_owner_trial": "NOT_RUN",
        "pg_l_production_acceptance": "NOT_RUN",
    }
    return {
        "schema_version": 1,
        "read_only": True,
        "synthetic": False,
        "evidence_kind": "local_observation",
        "production_ready": False,
        "environment": {
            "category": classification["category"],
            "target_match": target_status,
            "environment_gate": gate,
            "gate_scope": "OS identity, Python minimum and disk observation only",
            "development_macos": "PASS" if system == "Darwin" else "NOT_APPLICABLE",
            "target_linux_validation": "NOT_RUN",
            "preproduction_linux": "NOT_RUN",
            "production_evidence": "NOT_RUN",
        },
        "runtime": {
            "system": system,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "kernel": platform.release(),
            "python": platform.python_version(),
            "python_minimum": "3.12",
            "python_minimum_met": python_ok,
            "sqlite": sqlite3.sqlite_version,
            "effective_uid": os.geteuid() if hasattr(os, "geteuid") else None,
            "root_process": os.geteuid() == 0 if hasattr(os, "geteuid") else None,
        },
        "os_release": {
            "status": release_status,
            "values": {key: value for key, value in release.items()},
            "error": release_error,
        },
        "filesystem": filesystem,
        "cgroup": cgroup,
        "dependencies": {
            "status": "NOT_VERIFIED",
            "artifacts": artifacts,
            "build_verified": False,
            "reason": "Presence only; no imports, interpreter launches or installs.",
        },
        "acceptance_checks": checks,
        "limitations": [
            "Linux OS identity is not proof of an ECS image, RPM variant or product acceptance.",
            "No database opened; SQLite version is not a WAL, fsync or durability test.",
            "No credentials or profiles read; no remote login, installs or environment writes.",
            "Real product faults, durability, permissions, backup and long-term trial required.",
        ],
    }


def self_evaluate() -> dict[str, object]:
    """Exercise pure classification policy; do not collect the host environment."""
    from datetime import datetime, timezone

    release = parse_os_release('ID=alinux\nVERSION_ID="4.0.6"\nBUILD_ID="$HOME"\n')
    target = classify_environment("Linux", "x86_64", release, synthetic=True)
    other = classify_environment("Linux", "x86_64", {"ID": "ubuntu"}, synthetic=True)
    missing = classify_environment("Linux", "x86_64", {}, synthetic=True)
    checks = {
        "literal_os_parser": release
        == {"ID": "alinux", "VERSION_ID": "4.0.6", "BUILD_ID": "$HOME"},
        "target_classification": target["target_match"] == "PASS",
        "non_target_rejected": other["target_match"] == "FAIL",
        "unknown_identity_stays_unknown": missing["target_match"] == "NOT_VERIFIED",
        "no_production_acceptance_claim": all(
            not r["production_ready"] for r in (target, other, missing)
        ),
    }
    return {
        "module": "linux_preflight",
        "domain": "Linux environment preflight OS identity artifact observation evidence classification",
        "current_version": "1.0.0",
        "known_limitations": [
            "Standalone documented operator CLI; no resident product consumer or runtime dependency is wired.",
            "OS/artifact observations cannot prove ECS identity, durability, build correctness or production acceptance.",
            "Hook evaluates pure fixture branches only; it does not collect host or target-machine evidence.",
        ],
        "improvement_directions": [
            "If Linux packaging validation is approved, consume collect_environment in an explicit release/preproduction check, not cognition or daemon ticks."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_wiring": "pending",
        "production_ready": False,
        "checks": checks,
        "ok": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print the full JSON report")
    parser.add_argument(
        "--require-target",
        action="store_true",
        help="Exit 2 unless the narrow real target environment gate passes",
    )
    args = parser.parse_args()
    report = collect_environment()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    environment = report["environment"]
    assert isinstance(environment, dict)
    if not sys.version_info[:2] >= (3, 12):
        return 2
    return 2 if args.require_target and environment["environment_gate"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
