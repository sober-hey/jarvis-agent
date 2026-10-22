#!/usr/bin/env python3
"""Compile/execute the actual Swift core using the selected full Xcode macOS SDK."""

import platform
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path("/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain")
compiler = TOOLS / "usr/bin/swiftc"
clang = TOOLS / "usr/bin/clang"
sdk = Path(
    subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
)
if not compiler.exists():
    compiler = Path(subprocess.check_output(["xcrun", "--find", "swiftc"], text=True).strip())
    clang = Path(subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip())
    sdk = Path(
        subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    )
architecture = platform.machine()
if architecture == "aarch64":
    architecture = "arm64"
target = f"{architecture}-apple-macosx14.0"
scratch = "/tmp"
with tempfile.TemporaryDirectory(prefix="jarvis-watch-core-", dir=scratch) as temp:
    obj, executable = Path(temp) / "core.o", Path(temp) / "watch-core-tests"
    # Compile to a real object and link with Xcode clang; no Python algorithm replica.
    compile_command = [
        str(compiler),
        "-frontend",
        "-emit-object",
        "-whole-module-optimization",
        "-sdk",
        str(sdk),
        "-target",
        target,
        "-module-name",
        "WatchCoreHarness",
        "Core/WatchCore.swift",
        "Tests/WatchCoreSuite.swift",
        "Tests/main.swift",
        "-o",
        str(obj),
    ]
    subprocess.run(compile_command, cwd=ROOT, check=True)
    swift_libraries = compiler.parent.parent / "lib/swift/macosx"
    subprocess.run(
        [
            str(clang),
            "-isysroot",
            str(sdk),
            "-target",
            target,
            str(obj),
            "-L" + str(swift_libraries),
            "-L/usr/lib/swift",
            "-lswiftCore",
            "-framework",
            "Foundation",
            "-o",
            str(executable),
        ],
        cwd=ROOT,
        check=True,
    )
    print("Compiled Swift core -> native macOS executable; executing:", flush=True)
    result = subprocess.run(
        [str(executable), str(ROOT / "Tests/Fixtures/wisdm_1600_clapping_excerpt.txt")], cwd=ROOT
    )
    raise SystemExit(result.returncode)
