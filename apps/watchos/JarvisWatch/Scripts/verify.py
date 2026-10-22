#!/usr/bin/env python3
"""Freeze scoped evidence. Does not claim a watchOS build without a watchOS SDK."""

import datetime
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "Validation" / stamp
    output.mkdir(parents=True, exist_ok=False)
    results: dict[str, Any] = {
        "status": "NOT_YET_RUN",
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "watchos_build": "NOT_INCLUDED_IN_MACOS_SUITE_SEE_verify_watchos.py",
        "watchos_install": "NOT_RUN",
        "series8_hardware": "NOT_RUN",
        "brain_gateway_end_to_end": "NOT_RUN",
        "checks": [],
    }
    env = dict(
        os.environ, TMPDIR="/tmp", DEVELOPER_DIR="/Applications/Xcode.app/Contents/Developer"
    )
    failures = 0

    def run(name: str, command: list[str]) -> str:
        nonlocal failures
        result = subprocess.run(
            command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=100
        )
        (output / (name + ".log")).write_text(result.stdout + result.stderr)
        results["checks"].append({"name": name, "command": command, "exit_code": result.returncode})
        failures += result.returncode != 0
        print(f"{name}: exit={result.returncode}")
        return result.stdout

    core = run("swift_core_compiled", [sys.executable, "Scripts/test_core.py"])
    match = re.search(r"RESULT total=(\d+) passed=(\d+) failed=(\d+)", core)
    if match:
        results["core_assertions"] = dict(
            zip(["total", "passed", "failed"], map(int, match.groups()), strict=True)
        )
    else:
        failures += 1
    run("real_loopback_websocket", [sys.executable, "Scripts/test_connection.py"])
    run(
        "plist_lint",
        [
            "plutil",
            "-lint",
            "Info.plist",
            "JarvisWatch.entitlements",
            "JarvisWatch.xcodeproj/project.pbxproj",
        ],
    )
    run(
        "swift_source_parse_only",
        [
            "xcrun",
            "swift-frontend",
            "-parse",
            *[str(x.relative_to(ROOT)) for x in sorted((ROOT / "App").glob("*.swift"))],
            "Core/WatchCore.swift",
            "Tests/WatchCoreTests.swift",
        ],
    )
    run("python_ruff", [sys.executable, "-m", "ruff", "check", "Scripts"])
    pyright = ROOT.parents[2] / ".venv/bin/pyright"
    run("python_pyright", [str(pyright), "Scripts"])

    project = plistlib.loads((ROOT / "JarvisWatch.xcodeproj/project.pbxproj").read_bytes())
    objects = project["objects"]
    missing = [
        value["path"]
        for value in objects.values()
        if value["isa"] == "PBXFileReference"
        and value.get("sourceTree") == "<group>"
        and not (ROOT / value["path"]).is_file()
    ]
    assert not missing, missing
    scheme = ET.parse(ROOT / "JarvisWatch.xcodeproj/xcshareddata/xcschemes/JarvisWatch.xcscheme")
    for ref in scheme.findall(".//BuildableReference"):
        assert ref.attrib["BlueprintIdentifier"] in objects
    app_phase = next(
        value
        for value in objects.values()
        if value["isa"] == "PBXNativeTarget" and value["name"] == "JarvisWatch"
    )
    count = sum(
        len(objects[p]["files"])
        for p in app_phase["buildPhases"]
        if objects[p]["isa"] == "PBXSourcesBuildPhase"
    )
    assert count == len(list((ROOT / "App").glob("*.swift"))) + 1
    results["project_structure"] = {
        "objects": len(objects),
        "app_swift_sources": count,
        "missing_files": missing,
        "scheme_references": "valid",
    }
    files = [
        p
        for p in sorted(ROOT.rglob("*"))
        if p.is_file()
        and "Validation" not in p.relative_to(ROOT).parts
        and "__pycache__" not in p.parts
        and ".ruff_cache" not in p.parts
    ]
    results["source_sha256"] = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files
    }
    results["status"] = "PASS_MACOS_LOCAL_ONLY" if failures == 0 else "FAIL"
    results["failures"] = failures
    results["scope"] = (
        "Compiled native macOS core + loopback transport; Watch-only sources parsed, NOT built"
    )
    (output / "RESULTS.json").write_text(json.dumps(results, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": results["status"],
                "evidence": str(output),
                "core_assertions": results.get("core_assertions"),
            },
            indent=2,
        )
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
