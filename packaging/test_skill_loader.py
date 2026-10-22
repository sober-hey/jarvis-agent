import sys
from pathlib import Path
import unittest


class LoaderTracer(unittest.TestCase):
    def test_runtime_loader_exists(self):
        self.assertTrue(
            Path(__file__).with_name("skill_loader.py").exists(),
            "runtime SkillLoader must exist, not only a draft",
        )


class LoaderBehaviorTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from unittest.mock import patch
        from skill_loader import SkillLoader
        from skill_registry import SkillRegistry

        scratch = Path("/Volumes/扩展硬盘/hermes/.hermes/cache/scratch/wave2-cognition")
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = SkillRegistry(self.root / "registry.db")
        self.addCleanup(self.registry.close)
        with patch.object(Path, "home", return_value=self.root):
            self.loader = SkillLoader(self.root / "skills", registry=self.registry)
        self.addCleanup(self.loader.close)

    def manifest(self, **changes):
        from skill_manifest import SkillCapability, SkillManifest

        values = dict(
            id="echo-id",
            name="echo",
            version="1.0.0",
            description="fixture",
            trigger_patterns=["echo:*"],
            capabilities={SkillCapability.READ_ONLY},
            dependencies=[],
            created_at=0,
            author="unit-test",
        )
        values.update(changes)
        return SkillManifest(**values)

    def package(self, manifest, code="raise AssertionError('scan must not execute code')"):
        import json

        folder = self.loader.root / manifest.name / manifest.version
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps(manifest.to_dict()))
        (folder / "skill.py").write_text(code)
        return folder

    def test_scan_load_match_without_executing_package(self):
        item = self.manifest()
        self.package(item)
        self.assertEqual(self.loader.scan(), [item])
        self.assertEqual(self.loader.load("echo"), item)
        self.assertEqual(self.registry.get("echo"), item)
        self.assertEqual(self.loader.match("echo:today"), [item])
        self.assertEqual(self.loader.match("Echo:today"), [])

    def test_symlinked_code_is_rejected(self):
        folder = self.package(self.manifest())
        (folder / "skill.py").unlink()
        (folder / "skill.py").symlink_to(folder / "manifest.json")
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.loader.scan()

    def test_tampered_manifest_is_not_registered(self):
        import json

        folder = self.package(self.manifest())
        data = json.loads((folder / "manifest.json").read_text())
        data["description"] = "tampered"
        (folder / "manifest.json").write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.loader.load("echo")
        self.assertEqual(self.registry.list_installed(), [])

    def test_read_api_is_allowlisted(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        recent = Mock(return_value=[{"category": "fixture"}])
        self.loader.context = SimpleNamespace(mind_store=SimpleNamespace(recent=recent))
        item = self.manifest()
        self.assertEqual(
            self.loader._api(item, "mind_store.recent", ["event"], {"limit": 1}),
            [{"category": "fixture"}],
        )
        recent.assert_called_once_with("event", limit=1)
        with self.assertRaises(PermissionError):
            self.loader._api(item, "mind_store.append", [], {})
        with self.assertRaises(PermissionError):
            self.loader._api(self.manifest(capabilities=set()), "mind_store.recent", [], {})

    def test_missing_network_capability_does_not_dispatch_search(self):
        from unittest.mock import Mock

        search = Mock(side_effect=AssertionError("must not dispatch"))
        self.loader.search_fn = search
        with self.assertRaises(PermissionError):
            self.loader._api(self.manifest(), "search", ["fixture"], {})
        search.assert_not_called()

    def test_unavailable_sandbox_fails_closed(self):
        from unittest.mock import patch

        item = self.manifest()
        self.package(item)
        self.loader.load(item.name)
        self.loader._sandbox_available = False
        with patch("skill_loader.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "execution refused"):
                self.loader.execute(item.name)
        spawn.assert_not_called()

    def test_discover_uses_injected_search_and_filters_non_github(self):
        from unittest.mock import Mock

        self.loader.search_fn = Mock(
            return_value=[
                {"url": "https://github.com/example/fixture", "title": "fixture"},
                {"url": "http://github.com/example/fixture"},
                {"url": "https://github.com.evil.invalid/repo"},
            ]
        )
        results = self.loader.discover("fixture")
        self.assertEqual([r["url"] for r in results], ["https://github.com/example/fixture"])
        self.loader.search_fn.assert_called_once_with("fixture", max_results=10)
        with self.assertRaises(ValueError):
            self.loader.discover(" ")


class LoaderEvaluationTests(unittest.TestCase):
    def test_self_evaluate_is_detached_metadata_without_io_or_instance(self):
        import builtins
        from datetime import datetime
        from unittest.mock import patch
        import skill_loader as module

        self.assertTrue(callable(getattr(module, "self_evaluate", None)))
        with (
            patch.object(builtins, "open", side_effect=AssertionError("no files")),
            patch.object(Path, "home", side_effect=AssertionError("no user root")),
            patch("skill_loader.subprocess.Popen", side_effect=AssertionError("no process")),
        ):
            result = module.self_evaluate()
            self.assertEqual(module.SkillLoader.self_evaluate()["module"], "skill_loader")
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
        self.assertEqual(result["module"], "skill_loader")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
