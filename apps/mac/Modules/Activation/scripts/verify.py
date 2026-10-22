#!/usr/bin/env python3
"""Build with swift-frontend directly when the Apple driver traps.
All build/cache/log files go to the caller's scratch output directory.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True, type=Path)
parser.add_argument("--target", choices=["regression", "replay"], default="regression")
parser.add_argument("--manifest", type=Path)
parser.add_argument("--fallback", action="store_true")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
out = args.output.resolve()
out.mkdir(parents=True, exist_ok=True)
if os.environ.get("DEVELOPER_DIR"):
    sdk = Path(subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip())
    frontend = subprocess.check_output(["xcrun", "--find", "swift-frontend"], text=True).strip()
    clang = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
else:
    sdk = Path(os.environ.get("SDKROOT", "/Library/Developer/CommandLineTools/SDKs/MacOSX27.0.sdk"))
    frontend = "/Library/Developer/CommandLineTools/usr/bin/swift-frontend"
    clang = "clang"
swift_libraries = Path(frontend).parent.parent / "lib/swift/macosx"
sources = sorted((root / "Sources/JarvisActivation").glob("*.swift"))
test_dir = "ActivationRegression" if args.target == "regression" else "ActivationReplay"
sources += [root / "Tests" / test_dir / "main.swift"]
if args.target == "replay" and args.manifest is None:
    parser.error("--target replay requires --manifest")
run_args = ["--report", str(out / "results.json")]
if args.target == "replay":
    run_args += ["--manifest", str(args.manifest.resolve())]
    if args.fallback:
        run_args += ["--fallback"]
commands = [
    [frontend, "-emit-object", "-whole-module-optimization", "-O", "-sdk", str(sdk),
     "-target", "arm64-apple-macosx13.0", "-module-name", "ActivationRegression",
     "-module-cache-path", str(out / "module-cache"), *map(str, sources), "-o", str(out / "regression.o")],
    [clang, str(out / "regression.o"), "-isysroot", str(sdk),
     "-L" + str(swift_libraries), "-L" + str(sdk / "usr/lib/swift"),
     "-Wl,-rpath,/usr/lib/swift", "-lswiftCore", "-framework", "Accelerate",
     "-framework", "AVFoundation", "-framework", "CoreML", "-framework", "Foundation",
     "-o", str(out / "activation-regression")],
    [str(out / "activation-regression"), *run_args]
]
records = []
for command in commands:
    started = time.monotonic()
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=180)
    record = dict(command=command, exit_code=result.returncode, stdout=result.stdout,
                  stderr=result.stderr, seconds=time.monotonic() - started)
    records.append(record)
    print(result.stdout, end="")
    print(result.stderr, end="")
    (out / "execution.json").write_text(json.dumps(records, indent=2))
    if result.returncode:
        raise SystemExit(result.returncode)
