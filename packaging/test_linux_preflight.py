"""Synthetic branches are fixtures; only the real collector observes this host."""

import json
import platform
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import linux_preflight as module


def fixture_report(report: dict[str, module.JSONValue]) -> dict[str, module.JSONValue]:
    """Explicitly label mocked collector output; never treat it as host evidence."""
    report["synthetic"] = True
    report["evidence_kind"] = "fixture_branch_only"
    return report


class ParserTests(unittest.TestCase):
    def test_quoted_values(self) -> None:
        self.assertEqual(
            module.parse_os_release(
                "ID=alinux\nNAME=\"Alibaba Cloud Linux\"\nVERSION_ID='4.0.6'\n"
            ),
            {"ID": "alinux", "NAME": "Alibaba Cloud Linux", "VERSION_ID": "4.0.6"},
        )

    def test_no_expansion_and_malformed_input(self) -> None:
        result = module.parse_os_release(
            '#comment\nID=ubuntu\nVERSION_ID="24.04"\nEMPTY=\n'
            'BUILD_ID="$HOME"\nINVALID-KEY=x\nBROKEN="unclosed\nNOT_ASSIGNMENT\n'
        )
        self.assertEqual(
            result, {"ID": "ubuntu", "VERSION_ID": "24.04", "EMPTY": "", "BUILD_ID": "$HOME"}
        )

    def test_missing_version_stays_missing(self) -> None:
        self.assertNotIn("VERSION_ID", module.parse_os_release("ID=alinux\nNAME=Linux"))


class ClassificationTests(unittest.TestCase):
    def test_synthetic_platform_matrix(self) -> None:
        cases: list[tuple[str, str, dict[str, str], str, str]] = [
            ("Darwin", "arm64", {}, "development_macos", "NOT_APPLICABLE"),
            ("Linux", "x86_64", {"ID": "ubuntu", "VERSION_ID": "24.04"}, "linux_generic", "FAIL"),
            ("Linux", "x86_64", {"ID": "alinux"}, "linux_generic", "NOT_VERIFIED"),
            ("Linux", "x86_64", {}, "linux_generic", "NOT_VERIFIED"),
            (
                "Linux",
                "x86_64",
                {"ID": "alinux", "VERSION_ID": "4.0.6"},
                "target_alibaba_linux4_x86_64",
                "PASS",
            ),
            ("Linux", "aarch64", {"ID": "alinux", "VERSION_ID": "4.0.6"}, "linux_generic", "FAIL"),
            ("Linux", "x86_64", {"ID": "alinux", "VERSION_ID": "3"}, "linux_generic", "FAIL"),
            ("Linux", "x86_64", {"ID": "alinux", "VERSION_ID": "40"}, "linux_generic", "FAIL"),
            ("Windows", "AMD64", {}, "unsupported", "NOT_APPLICABLE"),
        ]
        for system, machine, release, category, status in cases:
            with self.subTest(system=system, machine=machine, release=release):
                result = module.classify_environment(system, machine, release, synthetic=True)
                self.assertEqual(result["category"], category)
                self.assertEqual(result["target_match"], status)
                self.assertTrue(result["synthetic"])
                self.assertEqual(result["evidence_kind"], "fixture_branch_only")
                self.assertFalse(result["production_ready"])


class CollectorTests(unittest.TestCase):
    def test_real_host_identity(self) -> None:
        report = module.collect_environment()
        runtime = report["runtime"]
        environment = report["environment"]
        assert isinstance(runtime, dict) and isinstance(environment, dict)
        self.assertEqual(runtime["system"], platform.system())
        self.assertEqual(runtime["machine"], platform.machine())
        self.assertFalse(report["synthetic"])
        self.assertFalse(report["production_ready"])
        self.assertEqual(environment["production_evidence"], "NOT_RUN")
        if platform.system() == "Darwin":
            self.assertEqual(environment["category"], "development_macos")
            self.assertEqual(environment["target_match"], "NOT_APPLICABLE")
            self.assertEqual(environment["target_linux_validation"], "NOT_RUN")

    def test_fixture_macos_never_reads_linux_system_files(self) -> None:
        with (
            patch.object(module.platform, "system", return_value="Darwin"),
            patch.object(Path, "read_text", side_effect=AssertionError("unexpected read")),
        ):
            report = fixture_report(module.collect_environment())
        # This mocked result is fixture evidence only and is not saved as a real report.
        self.assertEqual(
            report["os_release"], {"status": "NOT_APPLICABLE", "values": {}, "error": None}
        )

    def test_fixture_missing_linux_release_fails_closed(self) -> None:
        with (
            patch.object(module.platform, "system", return_value="Linux"),
            patch.object(Path, "read_text", side_effect=PermissionError),
        ):
            report = fixture_report(module.collect_environment())
        environment = report["environment"]
        assert isinstance(environment, dict)
        self.assertEqual(environment["target_match"], "NOT_VERIFIED")
        self.assertFalse(report["production_ready"])

    def test_no_database_or_process_is_launched_by_collector(self) -> None:
        with (
            patch.object(module.sqlite3, "connect", side_effect=AssertionError("database")),
            patch.object(subprocess, "run", side_effect=AssertionError("process")),
        ):
            report = fixture_report(module.collect_environment())
        dependencies = report["dependencies"]
        assert isinstance(dependencies, dict)
        self.assertFalse(dependencies["build_verified"])
        self.assertEqual(dependencies["status"], "NOT_VERIFIED")

    def test_disk_error_remains_visible_fixture(self) -> None:
        with patch.object(module.shutil, "disk_usage", side_effect=OSError):
            report = fixture_report(module.collect_environment())
        filesystem = report["filesystem"]
        assert isinstance(filesystem, dict)
        self.assertEqual(filesystem["status"], "NOT_VERIFIED")

    def test_real_json_cli_and_target_exit(self) -> None:
        script = Path(__file__).with_name("linux_preflight.py")
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(script), "--json"],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report: dict[str, module.JSONValue] = json.loads(result.stdout)
        self.assertFalse(report["synthetic"])
        self.assertFalse(report["production_ready"])
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(script), "--json", "--require-target"],
            check=False,
            text=True,
            capture_output=True,
        )
        environment = report["environment"]
        assert isinstance(environment, dict)
        self.assertEqual(result.returncode, 0 if environment["environment_gate"] == "PASS" else 2)


if __name__ == "__main__":
    unittest.main()
