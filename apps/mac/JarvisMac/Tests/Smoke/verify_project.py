#!/usr/bin/env python3
"""Structural checks; never presented as an Xcode compilation."""
import pathlib
import plistlib
import subprocess
import unittest
ROOT = pathlib.Path(__file__).resolve().parents[2]

class ProjectConfigurationTests(unittest.TestCase):
    def test_permission_keys_and_background_agent(self):
        info = plistlib.loads((ROOT / "Configuration/Info.plist").read_bytes())
        self.assertTrue(info["LSUIElement"])
        self.assertEqual(info["LSMinimumSystemVersion"], "13.0")
        for key in ("NSMicrophoneUsageDescription", "NSAccessibilityUsageDescription", "NSAudioCaptureUsageDescription"):
            self.assertTrue(info[key])
        # UIBackgroundModes is iOS-only; inventing one does not grant macOS background audio.
        self.assertNotIn("UIBackgroundModes", info)

    def test_audio_entitlement_has_no_private_permission_bypass(self):
        entitlements = plistlib.loads((ROOT / "Configuration/Jarvis.entitlements").read_bytes())
        self.assertTrue(entitlements["com.apple.security.device.audio-input"])
        self.assertNotIn("com.apple.security.accessibility", entitlements)
        self.assertNotIn("com.apple.security.cs.disable-library-validation", entitlements)

    def test_five_dynamic_modules_and_spm_dependency(self):
        package = (ROOT / "Package.swift").read_text()
        modules = ["JarvisCore", "JarvisAudio", "JarvisSpeech", "JarvisTransport", "JarvisUI"]
        self.assertEqual(package.count("type: .dynamic"), len(modules))
        for module in modules:
            self.assertTrue(any((ROOT / "Sources" / module).glob("*.swift")))
        self.assertIn('exact: "4.0.8"', package)
        self.assertIn('.linkedFramework("Accelerate")', package)

    def test_xcode_project_is_valid_plist(self):
        project = ROOT / "Jarvis.xcodeproj/project.pbxproj"
        self.assertTrue(project.is_file())
        self.assertEqual(subprocess.run(["plutil", "-lint", str(project)], capture_output=True).returncode, 0)

if __name__ == "__main__":
    unittest.main(verbosity=2)
