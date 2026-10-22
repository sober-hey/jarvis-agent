#!/usr/bin/env python3
"""CLT fallback: build the same Swift sources as signed native macOS frameworks.
Does not replace an xcodebuild acceptance check. No system/toolchain files change.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = "4.0.8"
ARCHIVE_SHA = "a0ec0772dffc14243bff125df4ad03819364e094e4ca4b28ba6b019511f3b8a5"


def run(args, env=None):
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), check=True, cwd=ROOT, env=env)


def dependency():
    cache = ROOT / ".build/dependencies"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / ("Starscream-" + VERSION + ".tar.gz")
    if not archive.exists():
        urllib.request.urlretrieve("https://codeload.github.com/daltoniam/Starscream/tar.gz/refs/tags/" + VERSION, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA:
        raise RuntimeError("Starscream archive checksum mismatch; refusing to compile")
    source = cache / ("Starscream-" + VERSION)
    if not source.exists():
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                parts = Path(member.name).parts
                if not parts or parts[0] != source.name or ".." in parts or member.issym() or member.islnk():
                    raise RuntimeError("Unsafe archive member")
            bundle.extractall(cache)
    return source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", choices=["arm64", "x86_64"], default=platform.machine())
    parser.add_argument("--identity", default="-", help="Local ad-hoc by default, or an installed Developer ID")
    args = parser.parse_args()
    # Ad-hoc signatures have no Team ID: hardened library validation rejects
    # embedded ad-hoc frameworks. Developer ID builds retain hardened runtime.
    signing_options = [] if args.identity == "-" else ["--options", "runtime"]
    developer = subprocess.check_output(["xcode-select", "-p"], text=True).strip()
    toolroot = Path(developer) / "usr/bin"
    if not (toolroot / "swiftc").exists():
        toolroot = Path(developer) / "Toolchains/XcodeDefault.xctoolchain/usr/bin"
    swift = toolroot / "swiftc"
    env = dict(os.environ)
    version = subprocess.run([str(swift), "--version"], capture_output=True, text=True, env=env)
    if version.returncode != 0:
        env["SWIFT_USE_OLD_DRIVER"] = "1"
        print("Default Swift driver failed; using legacy driver for this build only.", flush=True)
        subprocess.run([str(swift), "--version"], check=True, env=env)
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    bundle = ROOT / "output/Jarvis.app"
    if bundle.exists():
        shutil.rmtree(bundle)
    contents = bundle / "Contents"
    frameworks = contents / "Frameworks"
    frameworks.mkdir(parents=True)
    (contents / "MacOS").mkdir()
    resources = contents / "Resources"
    resources.mkdir()
    source = dependency()
    compile_flags = ["-swift-version", "5", "-target", args.arch + "-apple-macosx13.0", "-sdk", sdk,
                     "-F", str(frameworks), "-enable-testing", "-g"]
    sdk_links = {
        "Starscream": ["-framework", "Network", "-framework", "Security", "-lz"],
        "JarvisCore": [],
        "JarvisAudio": ["-framework", "AVFoundation", "-framework", "Accelerate", "-framework", "AudioToolbox"],
        "JarvisSpeech": [],
        "JarvisTransport": ["-framework", "Starscream", "-framework", "Security", "-lz"],
        "JarvisUI": ["-framework", "AppKit", "-framework", "AVFoundation", "-framework", "ApplicationServices"],
    }
    for name in ["Starscream", "JarvisCore", "JarvisAudio", "JarvisSpeech", "JarvisTransport", "JarvisUI"]:
        fw = frameworks / (name + ".framework")
        version_dir = fw / "Versions/A"
        module_dir = version_dir / "Modules" / (name + ".swiftmodule")
        module_dir.mkdir(parents=True)
        (version_dir / "Resources").mkdir()
        (fw / "Versions/Current").symlink_to("A")
        (fw / name).symlink_to("Versions/Current/" + name)
        (fw / "Resources").symlink_to("Versions/Current/Resources")
        (fw / "Modules").symlink_to("Versions/Current/Modules")
        info = {"CFBundleIdentifier": "com.jarvis.framework." + name, "CFBundleExecutable": name,
                "CFBundleName": name, "CFBundlePackageType": "FMWK", "CFBundleVersion": "1",
                "CFBundleShortVersionString": "1.0.0", "CFBundleInfoDictionaryVersion": "6.0",
                "LSMinimumSystemVersion": "13.0"}
        (version_dir / "Resources/Info.plist").write_bytes(plistlib.dumps(info))
        sources = sorted((source / "Sources").rglob("*.swift")) if name == "Starscream" else sorted((ROOT / "Sources" / name).glob("*.swift"))
        links = sdk_links[name][:]
        if name not in ("Starscream", "JarvisCore"):
            links += ["-framework", "JarvisCore"]
        run([swift, *compile_flags, "-emit-library", "-emit-module", "-module-name", name,
             "-emit-module-path", module_dir / (args.arch + "-apple-macos.swiftmodule"),
             "-Xlinker", "-install_name", "-Xlinker", "@rpath/" + name + ".framework/Versions/A/" + name,
             "-Xlinker", "-rpath", "-Xlinker", "@loader_path/../../..",
             *sources, *links, "-o", version_dir / name], env)
        if name == "Starscream":
            shutil.copy2(source / "LICENSE", version_dir / "Resources/LICENSE")
            shutil.copy2(source / "Sources/PrivacyInfo.xcprivacy", version_dir / "Resources/PrivacyInfo.xcprivacy")
        run(["codesign", "--force", "--sign", args.identity, *signing_options, "--timestamp=none", fw])
    links = [part for name in sdk_links for part in ["-framework", name]]
    run([swift, *compile_flags, "-parse-as-library", *sorted((ROOT / "Sources/JarvisApp").glob("*.swift")),
         *links, "-framework", "AppKit", "-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks",
         "-o", contents / "MacOS/Jarvis"], env)
    shutil.copy2(ROOT / "Configuration/Info.plist", contents / "Info.plist")
    run(["codesign", "--force", "--sign", args.identity, *signing_options, "--timestamp=none",
         "--entitlements", ROOT / "Configuration/Jarvis.entitlements", bundle])
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", bundle])
    print("Built", bundle)


if __name__ == "__main__":
    main()
