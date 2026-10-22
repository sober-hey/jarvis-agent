#!/usr/bin/env python3
"""Unsigned device SDK build + actual watchOS Simulator XCTest execution."""

import argparse
import datetime
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulator-id", required=True)
    args = parser.parse_args()
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "Validation" / (stamp + "-watchos")
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    env.update(
        TMPDIR="/tmp", DEVELOPER_DIR="/Applications/Xcode.app/Contents/Developer"
    )
    env.pop("SWIFT_USE_OLD_DRIVER", None)
    failures = 0
    checks: list[dict[str, Any]] = []

    def run(name: str, command: list[str], timeout: int = 600) -> int:
        nonlocal failures
        result = subprocess.run(
            command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout
        )
        (output / (name + ".log")).write_text(result.stdout + result.stderr)
        checks.append({"name": name, "command": command, "exit_code": result.returncode})
        if result.returncode:
            failures += 1
        print(f"{name}: exit={result.returncode}", flush=True)
        return result.returncode

    run("xcode_version", ["xcodebuild", "-version"])
    common = [
        "xcodebuild",
        "-project",
        "JarvisWatch.xcodeproj",
        "-scheme",
        "JarvisWatch",
        "-configuration",
        "Debug",
        "-derivedDataPath",
        "/tmp/jarvis-watch-gateway-dd-" + stamp,
        "CODE_SIGNING_ALLOWED=NO",
    ]
    build = run(
        "watchos_device_unsigned_build",
        common + ["-sdk", "watchos27.0", "CODE_SIGNING_ALLOWED=NO", "build"],
    )
    bundle = Path(env["TMPDIR"]) / ("JarvisWatchTests-" + stamp + ".xcresult")
    tests = run(
        "watchos_simulator_tests",
        common
        + [
            "-sdk",
            "watchsimulator27.0",
            "-destination",
            "platform=watchOS Simulator,id=" + args.simulator_id,
            "-parallel-testing-enabled",
            "NO",
            "-resultBundlePath",
            str(bundle),
            "test",
        ],
        timeout=600,
    )
    summary: dict[str, Any] = {}
    if bundle.exists():
        result = subprocess.run(
            ["xcrun", "xcresulttool", "get", "test-results", "summary", "--path", str(bundle)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        (output / "xcresult_summary.json").write_text(result.stdout or "{}")
        if result.returncode == 0:
            summary = json.loads(result.stdout)
    if tests == 0 and (
        summary.get("passedTests") != 1
        or summary.get("failedTests") != 0
        or summary.get("skippedTests") != 0
    ):
        failures += 1
        print("FAIL: test summary must show exactly one executed XCTest, zero failed/skipped")
    source = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(ROOT.rglob("*"))
        if p.is_file()
        and not any(
            x in p.relative_to(ROOT).parts for x in ("Validation", "__pycache__", ".ruff_cache")
        )
    }
    report = {
        "status": "PASS_WATCHOS_COMPILE_AND_SIMULATOR_ONLY" if failures == 0 else "FAIL",
        "checks": checks,
        "source_sha256": source,
        "watchos_build": "PASS_UNSIGNED" if build == 0 else "FAIL",
        "watchos_simulator": "PASS" if tests == 0 else "FAIL",
        "simulator_id": args.simulator_id,
        "xcresult_path": str(bundle),
        "test_summary": summary,
        "device_signing_install": "NOT_RUN_NO_SIGNING_IDENTITY",
        "series8_hardware_audio_motion_background": "NOT_RUN",
        "production_brain_gateway_end_to_end": "NOT_RUN",
    }
    (output / "RESULTS.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "evidence": str(output)}, indent=2))
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
