"""入口隔离测试；所有夹具和 venv 均在本轮证据目录内创建。"""

import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest

from evidence_support import evidence_directory

HERE = pathlib.Path(__file__).resolve().parent
RUNS = evidence_directory("launcher")


class LauncherTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("jarvis_launcher", HERE / "jarvis.py")
        assert spec is not None and spec.loader is not None
        self.launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.launcher)
        RUNS.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=RUNS)
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name) / "project"
        self.root.mkdir()
        self.home = pathlib.Path(self.temp.name) / "home"

    def test_home_and_environment_isolation(self):
        host = self.root / "host-hermes"
        host.mkdir()
        home = self.root / "jarvis-home"
        env = {
            "HOME": str(self.root),
            "HERMES_HOME": str(host),
            "HERMES_DIR": str(host),
            "PYTHONPATH": str(host),
            "PATH": str(host),
            "OPENAI_API_KEY": "must-not-pass",
        }
        self.assertEqual(self.launcher.resolve_home(str(home), env, self.root / "project"), home)
        child = self.launcher.child_environment(home, self.root / "engine-venv", env)
        self.assertEqual(child["HERMES_HOME"], str(home))
        self.assertEqual(child["HOME"], str(home))
        self.assertNotIn("PYTHONPATH", child)
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertNotIn(str(host), repr(child))
        with self.assertRaises(ValueError):
            self.launcher.resolve_home(str(host / "profiles" / "x"), env, self.root / "project")
        alias = self.root / "alias"
        alias.symlink_to(host, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.launcher.resolve_home(str(alias), env, self.root / "project")

    def fixture_vendor(self):
        import json

        vendor = self.root / self.launcher.VENDOR_REL
        vendor.mkdir(parents=True)
        for relative in self.launcher.RESOURCES:
            path = vendor / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative in ("tools", "agent", "skills", "web", "pm"):
                path.mkdir()
            else:
                path.write_text("fixture resource\n")
        digest, count = self.launcher.tree_digest(vendor)
        packaging = self.root / "packaging"
        packaging.mkdir(exist_ok=True)
        # Real resource-only fixture; zero dependency requirements, no invented metadata.
        (packaging / "VENDOR_MANIFEST.json").write_text(
            json.dumps(
                {
                    "commit": self.launcher.COMMIT,
                    "tree_sha256": digest,
                    "file_count": count,
                    "required_metadata": [],
                }
            )
        )
        return vendor

    def test_missing_venv_does_not_run_installer_or_python(self):
        from unittest.mock import patch

        self.fixture_vendor()
        with patch.object(
            self.launcher.subprocess, "run", side_effect=AssertionError("must not execute")
        ):
            result = self.launcher.check(self.root, {}, str(self.home))
        self.assertFalse(result["ok"])
        self.assertFalse((self.home).exists())
        self.assertIn("未构建", result["checks"][-1]["detail"])

    def test_vendor_symlink_and_modified_bytes_rejected(self):
        vendor = self.fixture_vendor()
        (vendor / "LICENSE").write_text("modified")
        result = self.launcher.check(self.root, {}, str(self.home))
        self.assertFalse(next(x for x in result["checks"] if x["name"] == "vendor_integrity")["ok"])
        moved = vendor.with_name("other")
        vendor.rename(moved)
        vendor.symlink_to(moved, target_is_directory=True)
        result = self.launcher.check(self.root, {}, str(self.home))
        self.assertFalse(next(x for x in result["checks"] if x["name"] == "vendor_path")["ok"])

    def test_success_check_with_real_isolated_314_venv(self):
        self.fixture_vendor()
        python = pathlib.Path(
            "/Users/sober/.local/share/uv/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"
        )
        self.assertTrue(
            python.exists(), "该本机证据测试需要现有、非 Hermes 的 Python 3.14；不联网安装"
        )
        venv = self.root / self.launcher.VENV_REL
        result = subprocess.run(
            [str(python), "-I", "-B", "-m", "venv", "--without-pip", "--copies", str(venv)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.launcher.check(self.root, {}, str(self.home))
        self.assertTrue(report["ok"], report)
        self.assertFalse(report["launch_ready"])
        self.assertEqual(report["runtime"]["version"][:2], [3, 14])
        self.assertFalse((self.home).exists())
        import json

        (RUNS / "real-fixture-success.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        manifest_path = self.root / "packaging/VENDOR_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["required_metadata"] = ["openai", "rich"]
        manifest_path.write_text(json.dumps(manifest))
        missing = self.launcher.check(self.root, {}, str(self.home))
        self.assertFalse(missing["ok"])
        self.assertIn(
            "openai", next(x for x in missing["checks"] if x["name"] == "core_metadata")["detail"]
        )
        (RUNS / "real-fixture-missing-dependencies.json").write_text(
            json.dumps(missing, ensure_ascii=False, indent=2)
        )

    def fixture_metadata_runtime(self):
        import json

        self.fixture_vendor()
        python = pathlib.Path(
            "/Users/sober/.local/share/uv/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"
        )
        venv = self.root / self.launcher.VENV_REL
        built = subprocess.run(
            [str(python), "-I", "-B", "-m", "venv", "--without-pip", "--copies", str(venv)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        manifest_path = self.root / "packaging/VENDOR_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["required_metadata"] = ["fixture-core"]
        manifest_path.write_text(json.dumps(manifest))
        purelib = venv / "lib/python3.14/site-packages"
        host = pathlib.Path(self.temp.name) / "declared-host-hermes"
        dist = host / "site-packages/fixture_core-0.0.0.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: fixture-core\nVersion: 0.0.0\n"
        )
        return venv, purelib, host, dist

    def test_metadata_links_cannot_borrow_declared_host(self):
        import shutil

        for kind in ("purelib", "dist-info", "METADATA"):
            with self.subTest(kind=kind):
                _, purelib, host, dist = self.fixture_metadata_runtime()
                local_dist = purelib / dist.name
                if kind == "purelib":
                    purelib.rmdir()
                    purelib.symlink_to(dist.parent, target_is_directory=True)
                elif kind == "dist-info":
                    local_dist.symlink_to(dist, target_is_directory=True)
                else:
                    local_dist.mkdir()
                    (local_dist / "METADATA").symlink_to(dist / "METADATA")
                report = self.launcher.check(self.root, {"HERMES_HOME": str(host)}, str(self.home))
                shutil.rmtree(self.root / "packaging")
                shutil.rmtree(self.root / "components")
                shutil.rmtree(host)
                self.assertFalse(report["ok"], "borrowed " + kind + " must be rejected")
                self.assertFalse(report["launch_ready"])

    def test_metadata_internal_links_and_plain_files_allowed(self):
        venv, purelib, _, dist = self.fixture_metadata_runtime()
        local_dist = purelib / dist.name
        local_dist.mkdir()
        (local_dist / "METADATA").write_bytes((dist / "METADATA").read_bytes())
        plain = self.launcher.check(self.root, {}, str(self.home))
        self.assertTrue(plain["ok"], plain)
        stored = venv / "local-metadata"
        local_dist.rename(stored)
        local_dist.symlink_to(stored, target_is_directory=True)
        internal = self.launcher.check(self.root, {}, str(self.home))
        self.assertTrue(internal["ok"], internal)
        saved = stored / "saved-metadata"
        (stored / "METADATA").rename(saved)
        (stored / "METADATA").symlink_to(saved)
        self.assertTrue(self.launcher.check(self.root, {}, str(self.home))["ok"])

    def test_metadata_external_links_without_host_declaration_rejected(self):
        _, purelib, _, dist = self.fixture_metadata_runtime()
        local_dist = purelib / dist.name
        local_dist.symlink_to(dist, target_is_directory=True)
        report = self.launcher.check(self.root, {}, str(self.home))
        self.assertFalse(report["ok"], report)
        self.assertFalse(
            next(x for x in report["checks"] if x["name"] == "metadata_boundary")["ok"]
        )

    def test_metadata_pkg_info_external_link_and_internal_cycle(self):
        _, purelib, _, dist = self.fixture_metadata_runtime()
        local_dist = purelib / "fixture_core.egg-info"
        local_dist.mkdir()
        (local_dist / "PKG-INFO").symlink_to(dist / "METADATA")
        self.assertFalse(self.launcher.check(self.root, {}, str(self.home))["ok"])
        (local_dist / "PKG-INFO").unlink()
        (local_dist / "PKG-INFO").write_bytes((dist / "METADATA").read_bytes())
        (purelib / "internal-cycle").symlink_to(purelib, target_is_directory=True)
        report = self.launcher.check(self.root, {}, str(self.home))
        self.assertTrue(report["ok"], report)

    def test_parent_revalidates_child_metadata_paths(self):
        import json
        from unittest.mock import patch

        _, purelib, _, dist = self.fixture_metadata_runtime()
        local_dist = purelib / dist.name
        local_dist.mkdir()
        (local_dist / "METADATA").write_bytes((dist / "METADATA").read_bytes())
        runtime = self.launcher.check(self.root, {}, str(self.home))["runtime"]
        runtime["metadata_paths"][0]["file"] = str(dist / "METADATA")
        returned = subprocess.CompletedProcess([], 0, json.dumps(runtime), "")
        with patch.object(self.launcher.subprocess, "run", return_value=returned):
            report = self.launcher.check(self.root, {}, str(self.home))
        self.assertFalse(report["ok"], report)
        self.assertFalse(next(x for x in report["checks"] if x["name"] == "core_metadata")["ok"])

    def test_venv_symlink_to_host_rejected(self):
        self.fixture_vendor()
        host = self.root / "host-hermes"
        host.mkdir()
        venv = self.root / self.launcher.VENV_REL
        venv.parent.mkdir(parents=True, exist_ok=True)
        venv.symlink_to(host, target_is_directory=True)
        result = self.launcher.check(self.root, {"HERMES_HOME": str(host)}, str(self.home))
        self.assertFalse(next(x for x in result["checks"] if x["name"] == "engine_venv")["ok"])

    def test_home_defaults_do_not_use_hermes(self):
        host = self.root / "host-hermes"
        project = self.root / "project"
        self.assertEqual(
            self.launcher.resolve_home(None, {"HERMES_HOME": str(host)}, project),
            pathlib.Path.home() / ".jarvis",
        )
        for bad in ("", "relative", str(project), str(pathlib.Path.home() / ".hermes")):
            with self.assertRaises(ValueError):
                self.launcher.resolve_home(bad, {}, project)

    def test_help_without_engine(self):
        result = subprocess.run(
            [sys.executable, "-I", str(HERE / "jarvis.py"), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("JARVIS", result.stdout)
        self.assertIn("check", result.stdout)

    def test_bootstrap_and_unsupported_command(self):
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(HERE / "bootstrap.py"), "--version", "--json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0.1.0-b01", result.stdout)
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(HERE / "jarvis.py"), "chat"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)

    def test_engine_binary_symlink_to_hermes_rejected(self):
        self.fixture_vendor()
        host = self.root / "host-hermes"
        host.mkdir()
        fake = host / "python3"
        fake.write_text("#!/bin/sh\nexit 77\n")
        fake.chmod(0o755)
        venv = self.root / self.launcher.VENV_REL
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        (venv / "bin/python3").symlink_to(fake)
        from unittest.mock import patch

        with patch.object(
            self.launcher.subprocess,
            "run",
            side_effect=AssertionError("host interpreter must not execute"),
        ):
            result = self.launcher.check(self.root, {"HERMES_HOME": str(host)}, str(self.home))
        self.assertFalse(next(x for x in result["checks"] if x["name"] == "engine_python")["ok"])


class WarmEngineTests(unittest.TestCase):
    """Real isolated children and persistence; only the HTTP provider is fake."""

    def setUp(self):
        import chat_launcher
        from test_chat_entry import TurnTests, route

        self.launcher = chat_launcher
        self.provider = TurnTests()
        self.provider.setUp()
        self.addCleanup(self.provider.tearDown)
        self.root = chat_launcher.prepare_root(self.provider.root / "warm-data")
        self.config = route(self.provider.server.server_port)
        self.key = self.config.pop("api_key")

    def tearDown(self):
        close = getattr(self.launcher, "close_warm_engines", None)
        if close is not None:
            close()

    def send(self, **kwargs):
        return self.launcher.launch(
            self.config, self.key, "next-user", self.root, "warm-session",
            test_port=self.provider.server.server_port, **kwargs,
        )

    def test_two_turns_reuse_one_isolated_child_and_preserve_history(self):
        from unittest.mock import patch

        spawn = self.launcher.subprocess.Popen
        with patch.object(self.launcher.subprocess, "Popen", wraps=spawn) as started:
            first = self.send()
            second = self.send()
        self.assertEqual(started.call_count, 1, "hot turns must reuse the product child")
        self.assertEqual([len(row["messages"]) for row in self.provider.requests], [2, 4])
        self.assertEqual(first["text"], second["text"])
        self.assertEqual(first["session_id"], second["session_id"])
        command = started.call_args.args[0]
        self.assertEqual(command[:4], [str(self.launcher.chat.engine.PYTHON), "-I", "-S", "-B"])
        self.assertNotIn(self.key, repr(started.call_args))
        self.assertEqual(len(started.call_args.kwargs["pass_fds"]), 1)
        for file in self.root.rglob("*"):
            if file.is_file():
                self.assertNotIn(self.key.encode(), file.read_bytes(), str(file))
    def test_binding_change_restarts_pool_child(self):
        from unittest.mock import patch

        spawn = self.launcher.subprocess.Popen
        with patch.object(self.launcher.subprocess, "Popen", wraps=spawn) as started:
            self.send()
            changed = dict(self.config)
            changed["reasoning_effort"] = "low"
            self.config = changed
            self.send()
        self.assertEqual(started.call_count, 2)

    def test_dead_child_is_replaced_without_replaying_prior_turn(self):
        from unittest.mock import patch

        first = self.send()
        child = next(iter(self.launcher._WARM_POOL.values()))
        assert child.process is not None
        self.launcher.kill_group(child.process)
        child.process.wait(timeout=5)
        spawn = self.launcher.subprocess.Popen
        with patch.object(self.launcher.subprocess, "Popen", wraps=spawn) as started:
            second = self.send()
        self.assertEqual(started.call_count, 1)
        self.assertEqual([len(row["messages"]) for row in self.provider.requests], [2, 4])
        self.assertEqual(first["session_id"], second["session_id"])


if __name__ == "__main__":
    unittest.main()
