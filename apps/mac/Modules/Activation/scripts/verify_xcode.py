#!/usr/bin/env python3
"""Build standalone activation schemes with Xcode, run every acceptance harness.
No microphone access and no modifications to the host app skeleton.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Dict, List

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
out = args.output.resolve()
out.mkdir(parents=True, exist_ok=False)
env = dict(os.environ, TMPDIR="/tmp", DEVELOPER_DIR="/Applications/Xcode.app/Contents/Developer")
commands: List[List[str]] = []
for scheme in ["activation-regression", "activation-replay"]:
    commands.append(["xcodebuild", "-scheme", scheme, "-destination", "platform=macOS,arch=arm64",
                     "-derivedDataPath", str(out / "DerivedData"), "CODE_SIGNING_ALLOWED=NO", "build"])
products = out / "DerivedData/Build/Products/Debug"
commands += [
    [str(products / "activation-regression"), "--report", str(out / "regression.json")],
    [str(products / "activation-replay"), "--manifest", str(root / "Evidence/WAV/manifest.json"),
     "--report", str(out / "gesture-replay.json")],
    [str(products / "activation-replay"), "--fallback", "--manifest", str(root / "Evidence/WAV/fallback-manifest.json"),
     "--report", str(out / "fallback-replay.json")]
]
records: List[Dict[str, Any]] = []
for index, command in enumerate(commands):
    started = time.monotonic()
    result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=240)
    stdout = out / (str(index) + "-stdout.log")
    stderr = out / (str(index) + "-stderr.log")
    stdout.write_text(result.stdout)
    stderr.write_text(result.stderr)
    records.append(dict(command=command, environment={"TMPDIR": env["TMPDIR"], "DEVELOPER_DIR": env["DEVELOPER_DIR"]},
                        exit_code=result.returncode, seconds=time.monotonic() - started,
                        stdout=str(stdout), stderr=str(stderr)))
    (out / "execution.json").write_text(json.dumps(records, indent=2))
    print(f"{command[0]}: exit {result.returncode}; log {stdout}")
    if result.returncode:
        print(result.stderr)
        raise SystemExit(result.returncode)
for name in ["regression", "gesture-replay", "fallback-replay"]:
    data = json.loads((out / (name + ".json")).read_text())
    print(name, {key: value for key, value in data.items() if key not in ["results", "passed", "failed", "limitations"]})
