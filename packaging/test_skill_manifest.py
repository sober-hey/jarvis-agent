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


class ManifestTests(unittest.TestCase):
    def test_01_runtime_module_and_roundtrip(self):
        self.assertTrue((ROOT / "skill_manifest.py").is_file(), "runtime manifest missing")
        module = importlib.import_module("skill_manifest")
        self.assertTrue(module.RUNTIME_ENABLED)
        manifest = make_manifest()
        self.assertEqual(
            module.SkillManifest.from_dict(manifest.to_dict(), require_checksum=True), manifest
        )
        self.assertEqual(manifest.to_dict()["capabilities"], ["NETWORK", "READ_ONLY"])
        self.assertTrue(manifest.matches("notes:today"))
        self.assertFalse(manifest.matches("Notes:today"))
        self.assertFalse(manifest.matches("prefix notes:today"))
        self.assertFalse(hasattr(manifest, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            manifest.version = "2.0.0"


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
        import skill_manifest as module

        self.assertTrue(callable(getattr(module, "self_evaluate", None)))
        with (
            patch.object(builtins, "open", side_effect=AssertionError("no files")),
            patch.object(Path, "home", side_effect=AssertionError("no user root")),
            patch("sqlite3.connect", side_effect=AssertionError("no database")),
        ):
            result = module.self_evaluate()
            self.assertEqual(module.SkillManifest.self_evaluate()["module"], "skill_manifest")
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
        self.assertEqual(result["module"], "skill_manifest")
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


class ManifestBehaviorTests(unittest.TestCase):
    def test_metadata_checksum_detects_container_mutation(self):
        manifest = make_manifest()
        detached = manifest.to_dict()
        detached["tags"].append("caller")
        self.assertEqual(manifest.tags, [])
        manifest.tags.append("tamper")
        self.assertFalse(manifest.verify_checksum())
        with self.assertRaises(ValueError):
            manifest.to_dict()
        with self.assertRaises(ValueError):
            manifest.matches("notes:today")

    def test_disk_decode_requires_checksum_and_rejects_unknown_fields(self):
        from skill_manifest import SkillManifest

        data = make_manifest().to_dict()
        data.pop("checksum")
        with self.assertRaises(ValueError):
            SkillManifest.from_dict(data, require_checksum=True)
        self.assertTrue(SkillManifest.from_dict(data).verify_checksum())
        data["unexpected"] = True
        with self.assertRaises(ValueError):
            SkillManifest.from_dict(data)

    def test_strict_semver_precedence(self):
        from skill_manifest import compare

        versions = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
            "2.0.0",
        ]
        for first, second in zip(versions, versions[1:]):
            with self.subTest(first=first, second=second):
                self.assertEqual(compare(first, second), -1)
                self.assertEqual(compare(second, first), 1)
        self.assertEqual(compare("1.0.0+build1", "1.0.0+build2"), 0)
        for invalid in ("01.0.0", "1.0", "1.0.0-01", " 1.0.0"):
            with self.subTest(version=invalid), self.assertRaises(ValueError):
                compare(invalid, "1.0.0")

    def test_names_reject_traversal_and_normalization(self):
        from skill_manifest import validate_name

        for name in ("../notes", "/notes", "notes/child", " notes", "笔记"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_name(name)
        validate_name("notes_1.0-test")

    def test_in_memory_registry_dependency_and_detached_snapshots(self):
        from skill_manifest import ManifestRegistry

        registry = ManifestRegistry()
        child = make_manifest("child", dependencies=["base"])
        with self.assertRaises(ValueError):
            registry.register(child)
        registry.register(make_manifest("base"))
        registry.register(child)
        self.assertEqual(
            [m.name for m in registry.find_by_trigger("notes:today")], ["base", "child"]
        )
        snapshot = registry.get("child")
        snapshot.tags.append("outside")
        self.assertEqual(registry.get("child").tags, [])
        with self.assertRaises(ValueError):
            registry.register(child)
