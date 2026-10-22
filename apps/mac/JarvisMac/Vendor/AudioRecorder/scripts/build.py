#!/usr/bin/env python3
"""Standalone Swift harness build; Xcode or CLT, no app skeleton changes."""
import os
import pathlib
import platform
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = ROOT / ".build"
BUILD.mkdir(exist_ok=True)
ENV = os.environ.copy()
# This host's Swift driver traps when TMPDIR contains non-ASCII characters.
ENV["TMPDIR"] = "/tmp"
if pathlib.Path("/Applications/Xcode.app/Contents/Developer").exists():
    ENV["DEVELOPER_DIR"] = "/Applications/Xcode.app/Contents/Developer"
SDK = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True, env=ENV).strip()
MODE = sys.argv[1] if len(sys.argv) > 1 else "cli"
if MODE not in ("cli", "test"):
    raise SystemExit("usage: build.py [cli|test]")
ENTRY = ROOT / ("Tests/main.swift" if MODE == "test" else "CLI/main.swift")
OUTPUT = BUILD / ("audio-tests" if MODE == "test" else "jarvis-audio")
SOURCES = sorted((ROOT / "Sources").glob("*.swift")) + [ENTRY]
cmd = ["xcrun", "swiftc", "-g", "-swift-version", "5", "-module-name", "JarvisAudio",
       "-sdk", SDK, "-target", platform.machine() + "-apple-macosx13.0",
       "-module-cache-path", str(BUILD / "ModuleCache")]
cmd += list(map(str, SOURCES)) + ["-o", str(OUTPUT), "-framework", "AVFoundation"]
if MODE == "cli":
    cmd += ["-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist",
            "-Xlinker", str(ROOT / "Resources/Info.plist")]
subprocess.run(cmd, check=True, env=ENV)
subprocess.run(["codesign", "--force", "--sign", "-", str(OUTPUT)], check=True)
print(OUTPUT)
