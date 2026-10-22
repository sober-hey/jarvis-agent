"""Offline strict-TDD coverage for the fail-closed route migration worker."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import product_config

ENTRY = Path(__file__).with_name("switch_model.py")


def route(model: str = "old-model") -> dict[str, Any]:
    return {
        "model": model,
        "provider": "custom",
        "base_url": "https://example.invalid/v1",
        "api_mode": "chat_completions",
        "context_length": 256000,
        "reasoning_effort": "medium",
    }


def engine_bytes(config: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "model": {
                "default": config["model"],
                "provider": config["provider"],
                "base_url": config["base_url"],
                "context_length": config["context_length"],
            },
            "compression": {"enabled": False},
        }
    ).encode()


class SwitchModelTests(unittest.TestCase):
    def setUp(self) -> None:
        parent = Path.home() / ".jarvis" / "tmp-test"
        parent.mkdir(mode=0o700, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="switch-model-", dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = self.base / "config.json"
        self.candidate = self.base / "candidate.json"
        self.put(self.config, json.dumps(route()).encode())
        self.put(self.candidate, json.dumps(route("new-model")).encode())
        self.root = self.base / "data"
        self.root.mkdir(mode=0o700)
        (self.root / "engine-home").mkdir(mode=0o700)
        self.engine = self.root / "engine-home/config.yaml"
        self.put(self.engine, engine_bytes(route()))

    @staticmethod
    def put(path: Path, payload: bytes) -> None:
        path.write_bytes(payload)
        path.chmod(0o600)

    def module(self) -> ModuleType:
        self.assertTrue(ENTRY.is_file(), "safe migration CLI is not implemented")
        spec = importlib.util.spec_from_file_location("switch_model_under_test", ENTRY)
        if spec is None or spec.loader is None:
            raise AssertionError("unable to load migration CLI")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def apply(self) -> dict[str, Any]:
        return self.module().migrate(
            self.candidate, config_path=self.config, data_roots=[self.root], apply=True
        )

    def test_default_is_dry_run_and_preserves_exact_bytes(self) -> None:
        module = self.module()
        before = {p: p.read_bytes() for p in (self.config, self.engine, self.candidate)}
        result = module.migrate(self.candidate, config_path=self.config, data_roots=[self.root])
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["old_config_sha256"], product_config.sha256_config(route()))
        self.assertEqual(
            result["new_config_sha256"], product_config.sha256_config(route("new-model"))
        )
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertFalse((self.base / "model-migrations").exists())

    def test_apply_updates_route_engine_and_keeps_backup_manifest(self) -> None:
        result = self.apply()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(
            self.config.read_bytes(),
            json.dumps(route("new-model"), sort_keys=True, separators=(",", ":")).encode(),
        )
        self.assertEqual(self.engine.read_bytes(), engine_bytes(route("new-model")))
        manifest = Path(result["manifest_path"])
        self.assertTrue(manifest.is_file())
        payload = json.loads(manifest.read_text())
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["old_config_sha256"], product_config.sha256_config(route()))
        self.assertEqual(
            payload["new_config_sha256"], product_config.sha256_config(route("new-model"))
        )
        for backup in payload["backup_paths"].values():
            self.assertTrue(Path(backup).is_file())
        self.assertEqual(
            Path(payload["backup_paths"][str(self.config)]).read_bytes(),
            json.dumps(route()).encode(),
        )
        self.assertEqual(
            Path(payload["backup_paths"][str(self.engine)]).read_bytes(), engine_bytes(route())
        )

    def test_apply_then_real_launcher_engine_precheck_reaches_popen(self) -> None:
        self.apply()
        launcher_spec = importlib.util.spec_from_file_location(
            "launcher_for_switch_test", Path(__file__).with_name("chat_launcher.py")
        )
        if launcher_spec is None or launcher_spec.loader is None:
            raise AssertionError("unable to load launcher")
        launcher = importlib.util.module_from_spec(launcher_spec)
        launcher_spec.loader.exec_module(launcher)
        with patch.object(
            launcher.subprocess, "Popen", side_effect=RuntimeError("stop before child")
        ) as popen:
            with self.assertRaisesRegex(RuntimeError, "stop before child"):
                launcher._launch_once(
                    route("new-model"),
                    "FAKE-ONLY-TEST-KEY",
                    "hello",
                    self.root,
                    "session",
                    timeout=1,
                )
            # The engine precheck must reach a real spawn. A warm-engine start is
            # attempted first and falls back to the cold child when the spawn
            # fails, so this is "at least one pre-dispatch spawn", not exactly one.
            self.assertGreaterEqual(popen.call_count, 1)
            self.assertNotIn("--serve", popen.call_args_list[-1].args[0])

    def test_pre_migration_launcher_rejects_mismatch_without_popen(self) -> None:
        launcher_spec = importlib.util.spec_from_file_location(
            "launcher_precheck_for_switch_test", Path(__file__).with_name("chat_launcher.py")
        )
        if launcher_spec is None or launcher_spec.loader is None:
            raise AssertionError("unable to load launcher")
        launcher = importlib.util.module_from_spec(launcher_spec)
        launcher_spec.loader.exec_module(launcher)
        with patch.object(launcher.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(launcher.LauncherError, "配置与已有数据根冲突"):
                launcher._launch_once(
                    route("new-model"),
                    "FAKE-ONLY-TEST-KEY",
                    "hello",
                    self.root,
                    "session",
                    timeout=1,
                )
            popen.assert_not_called()

    def test_started_attempt_refuses_before_any_write(self) -> None:
        attempts = self.root / "launcher-attempts"
        attempts.mkdir(mode=0o700)
        self.put(
            attempts / "inflight.json",
            json.dumps({"attempt_id": "inflight", "state": "started"}).encode(),
        )
        before = (self.config.read_bytes(), self.engine.read_bytes())
        module = self.module()
        with self.assertRaisesRegex(module.MigrationError, "started"):
            module.migrate(
                self.candidate, config_path=self.config, data_roots=[self.root], apply=True
            )
        self.assertEqual(before, (self.config.read_bytes(), self.engine.read_bytes()))

    def test_busy_launcher_lock_refuses_without_writes(self) -> None:
        lock_path = self.root / "launcher-session.lock"
        fd = lock_path.open("a+b")
        try:
            lock_path.chmod(0o600)
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = (self.config.read_bytes(), self.engine.read_bytes())
            module = self.module()
            with self.assertRaisesRegex(module.MigrationError, "busy"):
                module.migrate(
                    self.candidate, config_path=self.config, data_roots=[self.root], apply=True
                )
            self.assertEqual(before, (self.config.read_bytes(), self.engine.read_bytes()))
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            fd.close()

    def test_corrupt_or_symlink_attempt_ledger_refuses(self) -> None:
        attempts = self.root / "launcher-attempts"
        attempts.mkdir(mode=0o700)
        self.put(attempts / "broken.json", b"not-json")
        module = self.module()
        with self.assertRaisesRegex(module.MigrationError, "corrupt"):
            module.migrate(
                self.candidate, config_path=self.config, data_roots=[self.root], apply=True
            )
        (attempts / "broken.json").unlink()
        (attempts / "linked.json").symlink_to(self.config)
        module = self.module()
        with self.assertRaisesRegex(module.MigrationError, "unsafe"):
            module.migrate(
                self.candidate, config_path=self.config, data_roots=[self.root], apply=True
            )

    def test_write_failure_rolls_back_and_retains_originals(self) -> None:
        module = self.module()
        original_atomic = module._atomic_bytes
        new_engine = engine_bytes(route("new-model"))

        def fail_engine(path: Path, payload: bytes) -> None:
            if path == self.engine and payload == new_engine:
                raise OSError("injected engine write failure")
            original_atomic(path, payload)

        with patch.object(module, "_atomic_bytes", side_effect=fail_engine):
            with self.assertRaisesRegex(module.MigrationError, "restored"):
                module.migrate(
                    self.candidate, config_path=self.config, data_roots=[self.root], apply=True
                )
        self.assertEqual(self.config.read_bytes(), json.dumps(route()).encode())
        self.assertEqual(self.engine.read_bytes(), engine_bytes(route()))
        self.assertTrue(
            list((self.config.parent / "model-migrations").glob("*/backup/config.json"))
        )

    def test_completed_attempt_history_is_preserved(self) -> None:
        attempts = self.root / "launcher-attempts"
        attempts.mkdir(mode=0o700)
        history = attempts / "completed.json"
        original = json.dumps({"attempt_id": "completed", "state": "completed"}).encode()
        self.put(history, original)
        self.apply()
        self.assertEqual(history.read_bytes(), original)

    def test_apply_rejects_symlinked_scope(self) -> None:
        module = self.module()
        link = self.base / "root-link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(module.MigrationError, "symlink"):
            module.migrate(
                self.candidate,
                config_path=self.config,
                data_roots=[link],
                apply=True,
            )


if __name__ == "__main__":
    unittest.main()
