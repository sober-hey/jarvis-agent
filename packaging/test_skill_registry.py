"""Migrated isolated runtime acceptance tests; no production databases."""

from __future__ import annotations
import importlib
from pathlib import Path
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace

ROOT = Path(__file__).resolve().parent
SCRATCH = Path("/Volumes/扩展硬盘/hermes/.hermes/cache/scratch/wave2-cognition")
SCRATCH.mkdir(parents=True, exist_ok=True)


class RegistryTests(unittest.TestCase):
    def test_01_persistent_install_upgrade_history(self):
        self.assertTrue((ROOT / "skill_registry.py").is_file(), "runtime registry missing")
        module = importlib.import_module("skill_registry")
        self.assertTrue(module.RUNTIME_ENABLED)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            path = Path(directory) / "skills.db"
            with module.SkillRegistry(path) as registry:
                self.assertTrue(registry.runtime_enabled)
                first = make_manifest()
                second = make_manifest(version="2.0.0")
                registry.install(first)
                registry.upgrade(second)
                self.assertEqual(registry.get("notes"), second)
                self.assertEqual(registry.get("notes", "1.0.0"), first)
                self.assertEqual(registry.list_installed(), [second])
                self.assertEqual(registry.dependency_graph(), {"notes": []})
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with module.SkillRegistry(path) as registry:
                self.assertEqual(registry.get("notes"), second)
                registry.uninstall("notes")
                with self.assertRaises(KeyError):
                    registry.get("notes")
                self.assertEqual(registry.get("notes", "1.0.0"), first)


def make_manifest(name="notes", version="1.0.0", **changes):
    module = importlib.import_module("skill_manifest")
    data = dict(
        id="skill-" + name,
        name=name,
        version=version,
        description="读取 notes",
        trigger_patterns=["notes:*"],
        capabilities={module.SkillCapability.READ_ONLY, module.SkillCapability.NETWORK},
        dependencies=[],
        created_at=1000,
        author="tester",
        tags=[],
    )
    data.update(changes)
    return module.SkillManifest(**data)


class MetadataEvaluationTests(unittest.TestCase):
    def test_self_evaluate_is_detached_metadata_without_io_or_instance(self):
        import builtins
        from datetime import datetime
        from unittest.mock import patch
        import skill_registry as module

        self.assertTrue(callable(getattr(module, "self_evaluate", None)))
        with (
            patch.object(builtins, "open", side_effect=AssertionError("no files")),
            patch.object(Path, "home", side_effect=AssertionError("no user root")),
            patch("sqlite3.connect", side_effect=AssertionError("no database")),
        ):
            result = module.self_evaluate()
            self.assertEqual(module.SkillRegistry.self_evaluate()["module"], "skill_registry")
        self.assertEqual(
            set(result),
            {
                "module",
                "domain",
                "current_version",
                "known_limitations",
                "improvement_directions",
                "last_evaluated_at",
            },
        )
        self.assertEqual(result["module"], "skill_registry")
        for key in ("domain", "current_version"):
            self.assertIsInstance(result[key], str)
            self.assertTrue(result[key].strip())
        for key in ("known_limitations", "improvement_directions"):
            self.assertTrue(result[key])
            self.assertTrue(all(isinstance(item, str) and item.strip() for item in result[key]))
        self.assertEqual(
            datetime.fromisoformat(result["last_evaluated_at"]).utcoffset().total_seconds(), 0
        )
        result["known_limitations"].append("caller mutation")
        self.assertNotIn("caller mutation", module.self_evaluate()["known_limitations"])


class RegistryBehaviorTests(unittest.TestCase):
    def setUp(self):
        from skill_registry import SkillRegistry

        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "registry.db"
        self.registry = SkillRegistry(self.path)
        self.addCleanup(self.registry.close)

    def test_dependency_failures_roll_back_install_upgrade_uninstall(self):
        base = make_manifest("base")
        child = make_manifest("child", dependencies=["base>=1.0.0,<2.0.0"])
        with self.assertRaisesRegex(ValueError, "dependency"):
            self.registry.install(child)
        self.assertEqual(self.registry.list_installed(), [])
        self.registry.install(base)
        self.registry.install(child)
        for operation in (
            lambda: self.registry.uninstall("base"),
            lambda: self.registry.upgrade(make_manifest("base", "2.0.0")),
        ):
            with self.assertRaisesRegex(ValueError, "dependency"):
                operation()
            self.assertEqual(self.registry.get("base"), base)
        self.assertEqual(self.registry.dependency_graph(), {"base": [], "child": ["base"]})

    def test_cycle_upgrade_is_rejected_atomically(self):
        self.registry.install(make_manifest("base"))
        self.registry.install(make_manifest("child", dependencies=["base"]))
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.registry.upgrade(make_manifest("base", "2.0.0", dependencies=["child"]))
        self.assertEqual(self.registry.get("base").version, "1.0.0")
        with self.assertRaises(KeyError):
            self.registry.get("base", "2.0.0")

    def test_system_tag_is_sticky_and_blocks_uninstall(self):
        self.registry.install(make_manifest(tags=["SYSTEM"]))
        with self.assertRaises(PermissionError):
            self.registry.uninstall("notes")
        with self.assertRaises(PermissionError):
            self.registry.upgrade(make_manifest(version="2.0.0"))
        self.assertEqual(self.registry.get("notes").version, "1.0.0")

    def test_historical_rollback_requires_exact_recorded_manifest(self):
        first = make_manifest()
        second = make_manifest(version="2.0.0")
        self.registry.install(first)
        self.registry.upgrade(second)
        with self.assertRaises(ValueError):
            self.registry.upgrade(first)
        with self.assertRaises(ValueError):
            self.registry.upgrade(
                replace(first, description="changed", checksum=""), allow_rollback=True
            )
        self.assertEqual(self.registry.get("notes"), second)
        self.registry.upgrade(first, allow_rollback=True)
        self.assertEqual(self.registry.get("notes"), first)
        self.assertEqual(self.registry.get("notes", "2.0.0"), second)

    def test_get_returns_detached_metadata(self):
        self.registry.install(make_manifest())
        self.registry.get("notes").tags.append("caller")
        self.assertEqual(self.registry.get("notes").tags, [])

    def test_foreign_database_is_not_modified(self):
        import sqlite3
        from skill_registry import SkillRegistry

        foreign = self.path.with_name("foreign.db")
        with sqlite3.connect(foreign) as db:
            db.execute("CREATE TABLE unrelated(value TEXT)")
        before = foreign.read_bytes()
        with self.assertRaisesRegex(ValueError, "standalone"):
            SkillRegistry(foreign)
        self.assertEqual(foreign.read_bytes(), before)
