#!/usr/bin/env python3
"""Validate project references without claiming xcodebuild success."""
import json
import pathlib
import plistlib
import subprocess
import xml.etree.ElementTree as ET

root = pathlib.Path(__file__).resolve().parents[1]
project_dir = root / "JarvisMacApp.xcodeproj"
project = json.loads(subprocess.check_output([
    "plutil", "-convert", "json", "-o", "-", str(project_dir / "project.pbxproj")
]))
objects = project["objects"]
project_object = objects[project["rootObject"]]
assert project_object["isa"] == "PBXProject"
target_id, = project_object["targets"]
target = objects[target_id]
assert target["name"] == "JarvisMacApp"
assert target["productType"] == "com.apple.product-type.application"
source_paths = []
for phase_id in target["buildPhases"]:
    phase = objects[phase_id]
    for build_file_id in phase["files"]:
        file = objects[objects[build_file_id]["fileRef"]]
        if phase["isa"] == "PBXSourcesBuildPhase":
            source = root / "App" / file["path"]
            assert source.is_file(), source
            source_paths.append(str(source.relative_to(root)))
assert sorted(source_paths) == ["App/AppDelegate.swift", "App/main.swift"]
for config_id in objects[target["buildConfigurationList"]]["buildConfigurations"]:
    settings = objects[config_id]["buildSettings"]
    assert (root / settings["INFOPLIST_FILE"]).is_file()
    assert (root / settings["CODE_SIGN_ENTITLEMENTS"]).is_file()
    assert settings["GENERATE_INFOPLIST_FILE"] == "NO"
    assert settings["ENABLE_APP_SANDBOX"] == "YES"
scheme = ET.parse(project_dir / "xcshareddata/xcschemes/JarvisMacApp.xcscheme")
for ref in scheme.iter("BuildableReference"):
    assert ref.attrib["BlueprintIdentifier"] == target_id
    assert ref.attrib["BuildableName"] == "JarvisMacApp.app"
ET.parse(project_dir / "project.xcworkspace/contents.xcworkspacedata")
with (root / "App/Info.plist").open("rb") as handle:
    info = plistlib.load(handle)
assert info["LSUIElement"] is True
assert info["NSPrincipalClass"] == "NSApplication"
with (root / "App/JarvisMacApp.entitlements").open("rb") as handle:
    entitlements = plistlib.load(handle)
assert entitlements == {"com.apple.security.app-sandbox": True}
print("PASS: application target, source paths, configurations, shared scheme, workspace, plist, entitlements")
