"""Product-owned data boundaries; all data lives in disposable external roots."""

from __future__ import annotations

import importlib.util
import os
import pwd
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    import chat_launcher as launcher_types

spec = importlib.util.spec_from_file_location(
    "home_launcher_test", Path(__file__).with_name("chat_launcher.py")
)
assert spec and spec.loader
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
if TYPE_CHECKING:
    launcher = launcher_types


class HomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".jarvis-home-test-", dir=Path.home())
        self.base = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_acceptance_tmp_root_keeps_source_and_host_guards(self) -> None:
        home = launcher.chat.home
        # /tmp is a platform alias on macOS, not a caller-controlled symlink.
        with tempfile.TemporaryDirectory(prefix="jarvis-acceptance-", dir="/tmp") as folder:
            root = Path(folder)
            try:
                accepted = home.validate_root(root, validation_mode="acceptance")
            except (TypeError, ValueError) as exc:
                self.fail(f"acceptance temporary root rejected: {exc}")
            self.assertEqual(accepted, root.resolve())
            with self.assertRaises(ValueError):
                home.validate_root(root)
            for rejected in (
                home.PROJECT / "jarvis-acceptance-forbidden",
                home.HOST_ROOT / "jarvis-acceptance-forbidden",
                Path("/tmp/not-acceptance-data"),
            ):
                with self.subTest(path=rejected), self.assertRaises(ValueError):
                    home.validate_root(rejected, validation_mode="acceptance")
            with self.assertRaises(ValueError):
                home.validate_root(root, validation_mode="unknown")
            root.chmod(0o755)
            with self.assertRaises(ValueError):
                home.validate_root(root, validation_mode="acceptance")

    def test_external_dedicated_root_is_reusable_without_changing_data(self) -> None:
        root = self.base / "data"
        try:
            prepared = launcher.prepare_root(root)
        except launcher.LauncherError as exc:
            self.fail(f"dedicated external data root rejected: {exc}")
        self.assertEqual(prepared, root)
        marker = root / "user-note"
        marker.write_text("keep")
        self.assertEqual(launcher.prepare_root(root), root)
        self.assertEqual(marker.read_text(), "keep")

    def test_default_home_and_environment_home_reuse_sessions(self) -> None:
        from types import SimpleNamespace

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                launcher.chat.home.pwd,
                "getpwuid",
                return_value=SimpleNamespace(pw_dir=str(self.base)),
            ),
        ):
            root = launcher.prepare_root(None)
            self.assertEqual(root, self.base / ".jarvis/sessions")
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(launcher.prepare_root(None), root)
        with patch.dict(os.environ, {"JARVIS_HOME": str(self.base / "chosen-home")}):
            self.assertEqual(launcher.prepare_root(None), self.base / "chosen-home/sessions")
        with patch.dict(os.environ, {"JARVIS_HOME": "relative"}):
            with self.assertRaises(launcher.LauncherError):
                launcher.prepare_root(None)

    def test_source_and_host_overlap_are_denied_before_creation(self) -> None:
        home = launcher.chat.home
        # Host protection follows the account database even when HOME is isolated.
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        for path in (
            home.PROJECT,
            home.PROJECT / "packaging/data",
            home.PROJECT.parent,
            home.HOST_ROOT / "forbidden-product-data",
            account_home / ".hermes/data",
            home.LEGACY,
        ):
            existed = path.exists()
            with self.subTest(path=path), patch.dict(os.environ, {"HOME": str(self.base)}):
                with self.assertRaises(launcher.LauncherError):
                    launcher.prepare_root(path)
                self.assertEqual(path.exists(), existed)
        custom = self.base / "custom-hermes"
        with patch.dict(os.environ, {"HERMES_HOME": str(custom)}):
            for path in (custom, custom / "data", self.base):
                with self.subTest(path=path), self.assertRaises(launcher.LauncherError):
                    launcher.prepare_root(path)
        with patch.dict(os.environ, {"HERMES_ROOT": str(custom)}):
            with self.assertRaises(launcher.LauncherError):
                launcher.prepare_root(custom / "data")

    def test_links_owner_permissions_and_shared_parent_are_denied(self) -> None:
        root = launcher.prepare_root(self.base / "data")
        link = self.base / "link"
        link.symlink_to(root, target_is_directory=True)
        for path in (link, link / "descendant", Path("relative"), self.base / ".." / "bad"):
            with self.subTest(path=path), self.assertRaises(launcher.LauncherError):
                launcher.prepare_root(path)
        for mode in (0o755, 0o770):
            root.chmod(mode)
            with self.assertRaises(launcher.LauncherError):
                launcher.prepare_root(root)
            self.assertEqual(root.stat().st_mode & 0o777, mode)
        root.chmod(0o700)
        actual_user = launcher.chat.home.pwd.getpwuid(os.getuid())
        with (
            patch.object(launcher.chat.home.os, "getuid", return_value=os.getuid() + 1),
            patch.object(launcher.chat.home.pwd, "getpwuid", return_value=actual_user),
        ):
            with self.assertRaises(launcher.LauncherError):
                launcher.prepare_root(root)
        shared = self.base / "shared"
        shared.mkdir(mode=0o777)
        shared.chmod(0o777)
        with self.assertRaises(launcher.LauncherError):
            launcher.prepare_root(shared / "new-data")
        self.assertFalse((shared / "new-data").exists())
        child = root / "cache"
        child.chmod(0o755)
        with self.assertRaises(launcher.LauncherError):
            launcher.prepare_root(root)
        self.assertEqual(child.stat().st_mode & 0o777, 0o755)

    def test_child_revalidates_root_without_environment_allow_bypass(self) -> None:
        import json
        import subprocess

        home = launcher.chat.home
        root = launcher.prepare_root(self.base / "data")
        root.chmod(0o755)
        env = launcher.chat.engine.environment(root)
        env["JARVIS_ALLOW_ANY_DATA_DIR"] = "1"
        process = subprocess.run(
            [
                str(launcher.chat.engine.PYTHON),
                "-I",
                "-S",
                "-B",
                str(Path(__file__).with_name("chat_entry.py")),
                "--credentials-fd",
                "99",
                "--data-dir",
                str(root),
            ],
            input='{"prompt":"test"}',
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        result = json.loads(process.stdout)
        self.assertEqual(process.returncode, 1)
        self.assertIn("private_directory", [f["function"] for f in result["error_frames"]])
        root.chmod(0o700)
        with self.assertRaises(ValueError):
            home.validate_root(root, extra=(self.base,))
        # Caller-controlled legacy argument cannot allow another source subtree.
        with self.assertRaises(ValueError):
            home.validate_root(home.PROJECT / "packaging/data", legacy=home.PROJECT / "packaging")


if __name__ == "__main__":
    unittest.main(verbosity=2)
