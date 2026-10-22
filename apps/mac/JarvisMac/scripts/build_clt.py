#!/usr/bin/env python3
"""Local CLT fallback; does not replace verification with xcodebuild."""
import os
import pathlib
import platform
import plistlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def run(*args):
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), check=True, env=ENV)


if not os.environ.get("TMPDIR"):
    raise SystemExit("Set TMPDIR to your writable scratch directory first.")
ENV = {key: os.environ[key] for key in ("HOME", "TMPDIR")}
ENV["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
developer = pathlib.Path(subprocess.check_output(["xcode-select", "-p"], text=True).strip())
sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
frontend = pathlib.Path(subprocess.check_output(["xcrun", "--find", "swift-frontend"], text=True).strip())
clang = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
arch = platform.machine()
if arch not in ("arm64", "x86_64"):
    raise SystemExit(f"Unsupported architecture: {arch}")
output = ROOT / "build" / "CLT"
app = output / "JarvisMacApp.app"
contents = app / "Contents"
macos = contents / "MacOS"
macos.mkdir(parents=True, exist_ok=True)
obj = output / "JarvisMacApp.o"
with tempfile.TemporaryDirectory(prefix="jarvis-clt-") as scratch:
    run(frontend, "-emit-object", "-whole-module-optimization", "-swift-version", "5",
        "-sdk", sdk, "-target", f"{arch}-apple-macosx13.0", "-module-name", "JarvisMacApp",
        "-module-cache-path", pathlib.Path(scratch) / "ModuleCache",
        ROOT / "App/main.swift", ROOT / "App/AppDelegate.swift", "-o", obj)
    run(clang, "-target", f"{arch}-apple-macosx13.0", "-isysroot", sdk, obj,
        "-L" + str(frontend.parent.parent / "lib/swift/macosx"),
        "-L" + sdk + "/usr/lib/swift", "-rpath", "/usr/lib/swift",
        "-framework", "AppKit", "-o", macos / "JarvisMacApp")
with (ROOT / "App/Info.plist").open("rb") as handle:
    info = plistlib.load(handle)
info.update(CFBundleExecutable="JarvisMacApp", CFBundleIdentifier="local.jarvis.JarvisMacApp",
            CFBundleName="JarvisMacApp", LSMinimumSystemVersion="13.0")
with (contents / "Info.plist").open("wb") as handle:
    plistlib.dump(info, handle)
run("/usr/bin/codesign", "--force", "--sign", "-", "--options", "runtime",
    "--entitlements", ROOT / "App/JarvisMacApp.entitlements", app)
run("/usr/bin/codesign", "--verify", "--deep", "--strict", app)
print(f"CLT build complete (not xcodebuild): {app}")
