#!/usr/bin/env python3
"""Check the checked-in iPhone/Watch packaging graph; no build/signing side effects."""

from __future__ import annotations

import json
import plistlib
import subprocess
import unittest
from pathlib import Path
from typing import Any

IOS = Path(__file__).resolve().parents[1]
WATCH = IOS.parents[1] / "watchos" / "JarvisWatch"


def project(path: Path) -> dict[str, Any]:
    data = subprocess.check_output(["plutil", "-convert", "json", "-o", "-", str(path)])
    return json.loads(data)


def target(objects: dict[str, Any], name: str) -> tuple[str, dict[str, Any]]:
    return next(
        (key, value)
        for key, value in objects.items()
        if value.get("isa") == "PBXNativeTarget" and value.get("name") == name
    )


class CompanionPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ios = project(IOS / "JarvisVoice.xcodeproj/project.pbxproj")
        cls.watch = project(WATCH / "JarvisWatch.xcodeproj/project.pbxproj")
        cls.io = cls.ios["objects"]
        cls.wo = cls.watch["objects"]
        cls.phone_id, cls.phone = target(cls.io, "JarvisVoice")
        cls.watch_id, cls.watch_app = target(cls.wo, "JarvisWatch")
        cls.info = plistlib.loads((WATCH / "Info.plist").read_bytes())

    def test_watch_metadata(self) -> None:
        self.assertTrue(self.info["WKApplication"])
        self.assertEqual(self.info["WKCompanionAppBundleIdentifier"], "app.jarvis.JarvisVoice")
        self.assertNotIn("WKWatchOnly", self.info)

    def test_versions_identifiers_and_watch_minimum(self) -> None:
        def configs(objects: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
            entries = objects[app["buildConfigurationList"]]["buildConfigurations"]
            return {objects[key]["name"]: objects[key]["buildSettings"] for key in entries}

        phone = configs(self.io, self.phone)
        watch = configs(self.wo, self.watch_app)
        for mode in ("Debug", "Release"):
            self.assertEqual(phone[mode]["PRODUCT_BUNDLE_IDENTIFIER"], "app.jarvis.JarvisVoice")
            self.assertEqual(
                watch[mode]["PRODUCT_BUNDLE_IDENTIFIER"], "app.jarvis.JarvisVoice.watchkitapp"
            )
            self.assertEqual(watch[mode]["WATCHOS_DEPLOYMENT_TARGET"], "11.0")
            self.assertEqual(watch[mode]["SKIP_INSTALL"], "YES")
            for key in ("MARKETING_VERSION", "CURRENT_PROJECT_VERSION"):
                self.assertEqual(phone[mode][key], watch[mode][key])
        _, tests = target(self.wo, "JarvisWatchTests")
        for settings in configs(self.wo, tests).values():
            self.assertEqual(settings["WATCHOS_DEPLOYMENT_TARGET"], "11.0")

    def test_embed_watch_copy_phase(self) -> None:
        phases = [self.io[key] for key in self.phone["buildPhases"]]
        embeds = [p for p in phases if p.get("name") == "Embed Watch Content"]
        self.assertEqual(len(embeds), 1)
        phase = embeds[0]
        self.assertEqual(phase["isa"], "PBXCopyFilesBuildPhase")
        self.assertEqual(str(phase["dstSubfolderSpec"]), "16")
        self.assertEqual(phase["dstPath"], "$(CONTENTS_FOLDER_PATH)/Watch")
        self.assertEqual(len(phase["files"]), 1)
        product = self.io[self.io[phase["files"][0]]["fileRef"]]
        self.assertEqual(product["isa"], "PBXReferenceProxy")
        self.assertEqual(product["path"], "JarvisWatch.app")
        proxy = self.io[product["remoteRef"]]
        self.assertEqual(str(proxy["proxyType"]), "2")
        self.assertEqual(proxy["remoteGlobalIDString"], self.watch_app["productReference"])

    def test_explicit_external_target_dependency(self) -> None:
        dependencies = [self.io[key] for key in self.phone["dependencies"]]
        watch_dependencies = [d for d in dependencies if d.get("name") == "JarvisWatch"]
        self.assertEqual(len(watch_dependencies), 1)
        proxy = self.io[watch_dependencies[0]["targetProxy"]]
        self.assertEqual(str(proxy["proxyType"]), "1")
        self.assertEqual(proxy["remoteGlobalIDString"], self.watch_id)
        reference = self.io[proxy["containerPortal"]]
        self.assertEqual((IOS / reference["path"]).resolve(), WATCH / "JarvisWatch.xcodeproj")
        entries = self.io[self.ios["rootObject"]]["projectReferences"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["ProjectRef"], proxy["containerPortal"])
        self.assertEqual(self.io[entries[0]["ProductGroup"]]["isa"], "PBXGroup")


if __name__ == "__main__":
    unittest.main(verbosity=2)
