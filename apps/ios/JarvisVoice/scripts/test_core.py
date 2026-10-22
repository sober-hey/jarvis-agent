#!/usr/bin/env python3
"""Compile/run pure Swift tests on macOS; never claim an iOS target build.

Uses swiftc first. Some CLT driver builds trap on launch while swift-frontend
works; in that case use the same compiler frontend then clang to link an actual
Mach-O executable. Outputs only go to a fresh directory under TMPDIR.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(tempfile.mkdtemp(prefix="jarvis-ios-core-", dir=os.environ.get("TMPDIR")))
TOOL = Path("/Library/Developer/CommandLineTools/usr/bin")
SDK = Path("/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk")
SOURCES = [ROOT / "Tests/main.swift", *sorted((ROOT / "JarvisVoice/Core").glob("*.swift"))]
records: list[dict[str, object]] = []


def run(args: list[str], *, required: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    records.append(
        {
            "command": args,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    )
    if required and result.returncode != 0:
        save(False)
        raise RuntimeError(result.stderr or result.stdout or str(result.returncode))
    return result


def save(ok: bool, **extra: object) -> None:
    (OUT / "results.json").write_text(
        json.dumps(
            {
                "ok": ok,
                "scope": "MACOS_CORE_ONLY_NOT_IOS_TARGET",
                "ios_target_compiled": False,
                "records": records,
                **extra,
            },
            indent=2,
        )
    )


exe = OUT / "JarvisCoreTests"
probe = run([str(TOOL / "swiftc"), "--version"], required=False)
if probe.returncode == 0:
    run(
        [
            str(TOOL / "swiftc"),
            *map(str, SOURCES),
            "-module-name",
            "JarvisCoreTests",
            "-sdk",
            str(SDK),
            "-module-cache-path",
            str(OUT / "modules"),
            "-o",
            str(exe),
        ]
    )
    compiler = "swiftc"
else:
    obj = OUT / "JarvisCoreTests.o"
    run(
        [
            str(TOOL / "swiftc"),
            "-frontend",
            "-emit-object",
            "-whole-module-optimization",
            "-module-name",
            "JarvisCoreTests",
            *map(str, SOURCES),
            "-sdk",
            str(SDK),
            "-module-cache-path",
            str(OUT / "modules"),
            "-o",
            str(obj),
        ]
    )
    run(
        [
            str(TOOL / "clang"),
            str(obj),
            "-o",
            str(exe),
            "-isysroot",
            str(SDK),
            "-L",
            str(SDK / "usr/lib/swift"),
            "-L",
            str(TOOL.parent / "lib/swift/macosx"),
            "-Wl,-rpath,/usr/lib/swift",
            "-lswiftCore",
            "-framework",
            "Foundation",
        ]
    )
    compiler = "swiftc -frontend + clang (swiftc driver unavailable)"
output = run([str(exe)])
match = re.fullmatch(r"(\d+) checks, (\d+) failures\n?", output.stdout)
assert match and match[2] == "0", output.stdout
# Syntax parsing of platform sources is explicitly weaker than iOS typechecking.
platform = sorted((ROOT / "JarvisVoice").glob("*.swift"))
run(
    [
        str(TOOL / "swift-frontend"),
        "-parse",
        *map(str, platform),
        *map(str, SOURCES[1:]),
        "-sdk",
        str(SDK),
    ]
)
# Platform-independent production audio playback/transport/Keychain can additionally
# be typechecked against public macOS equivalents without any UIKit stubs.
shared = [
    ROOT / "JarvisVoice" / name
    for name in ["MP3Playback.swift", "VoiceSocket.swift", "GatewayCredential.swift"]
]
run(
    [
        str(TOOL / "swift-frontend"),
        "-typecheck",
        "-swift-version",
        "5",
        *map(str, shared),
        *map(str, SOURCES[1:]),
        "-sdk",
        str(SDK),
        "-module-cache-path",
        str(OUT / "modules"),
    ]
)
run(
    [
        "plutil",
        "-lint",
        str(ROOT / "JarvisVoice/Info.plist"),
        str(ROOT / "JarvisVoice.xcodeproj/project.pbxproj"),
    ]
)
save(
    True,
    compiler=compiler,
    passed=int(match[1]),
    failed=0,
    executable=str(exe),
    parsed_platform_files=len(platform),
    macos_typechecked_shared_files=len(shared),
)
print(output.stdout.strip())
print(f"Compiler: {compiler}")
print("This script builds macOS core only; see Evidence/xcode-27.1 for separate iOS target results")
print(f"Evidence: {OUT / 'results.json'}")
