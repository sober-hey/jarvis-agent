"""Offline evolution regression tests with real, thread-affine SQLite stores."""

from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import product_home
from inner_loop import MindStore
from self_evolution import SelfEvolution
from skill_manifest import SkillManifest
from skill_registry import SkillRegistry

SCRATCH = Path("/Volumes/扩展硬盘/hermes/.hermes/cache/scratch/evolution-review-fix/fixtures")


class SelfEvolutionThreadTests(unittest.TestCase):
    def setUp(self) -> None:
        SCRATCH.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="root-", dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Allow this test-owned host scratch fixture only; all real path/mode
        # validation remains in place. Never open the operator's product root.
        with patch.object(product_home, "protected_paths", return_value=()):
            self.mind = MindStore(self.root)
        self.registry = SkillRegistry(self.root / "skills" / "registry.db")
        self.addCleanup(self.registry.close)
        self.messages: list[dict[str, Any]] = []

        def enqueue(text: str, **kwargs: Any) -> str:
            self.messages.append({"text": text, **kwargs})
            return str(len(self.messages))

        def search_fixture(*_args: Any, **_kwargs: Any) -> list[dict[str, str]]:
            return []

        def no_preload(_evolution: Any) -> None:
            pass

        self.loader = SimpleNamespace(
            root=self.root / "skills",
            registry=self.registry,
            search_fn=search_fixture,
            context=SimpleNamespace(outreach=SimpleNamespace(enqueue=enqueue)),
        )
        with patch.object(SelfEvolution, "_preload_modules", no_preload):
            self.evolution = SelfEvolution(self.root, self.mind, cast(Any, self.loader))
        self.evolution.register_module(
            SimpleNamespace(
                self_evaluate=lambda: {
                    "module": "fixture_module",
                    "domain": "sleep data validation",
                    "current_version": "1.0.0",
                    "known_limitations": ["fixture missing adapter"],
                    "improvement_directions": ["read only adapter"],
                    "last_evaluated_at": datetime.now(UTC).isoformat(),
                }
            )
        )
        self.review_prompts: list[str] = []

        def reviewer(prompt: str) -> dict[str, str]:
            self.review_prompts.append(prompt)
            return {
                "text": json.dumps(
                    {
                        "estimated_value": 0.9,
                        "summary": "Read-only fixture data validation adapter.",
                        "skill_name": "fixture_adapter",
                        "useful": True,
                        "novel": True,
                        "evidence_sufficient": True,
                        "requires_core_change": False,
                        "rationale": "Addresses the supplied fixture gap.",
                    }
                )
            }

        self.evolution.reviewer = reviewer
        http_patch = patch.object(
            SelfEvolution, "_http", side_effect=AssertionError("network forbidden")
        )
        self.addCleanup(http_patch.stop)
        http_patch.start()
        candidates_patch = patch.object(
            SelfEvolution,
            "_candidates",
            return_value=[
                {
                    "url": "https://github.com/example/fixture-adapter",
                    "updated": datetime.now(UTC).isoformat(),
                    "title": "Fixture adapter",
                    "content": "Offline metadata only",
                }
            ],
        )
        self.addCleanup(candidates_patch.stop)
        candidates_patch.start()

    def worker(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(method, *args, **kwargs).result(timeout=5)

    def assert_research_succeeded(self, proposals: list[Any]) -> None:
        # Research catches review exceptions, so asserting only no thread crash
        # or a completion row misses ProgrammingError and yields false greens.
        self.assertEqual(self.evolution.last_session_errors, [])
        self.assertEqual(self.mind.recent("evolution_research_error", 100), [])
        self.assertEqual(len(proposals), 1)
        self.assertEqual(len(self.review_prompts), 1)
        self.assertTrue(proposals[0].requires_human_approval)
        self.assertEqual(self.mind.count("evolution_review"), 1)
        self.assertEqual(self.mind.count("evolution_proposal_approval"), 0)
        self.assertEqual(self.mind.count("evolution_proposal_processed"), 0)

    def test_background_research_uses_real_registry_created_on_main_thread(self) -> None:
        proposals = self.worker(self.evolution.research_session)
        self.assert_research_succeeded(proposals)
        self.assertEqual(self.registry.list_installed(), [])

    def test_background_weekly_uses_real_registry_created_on_main_thread(self) -> None:
        monday = datetime(2026, 10, 19, tzinfo=UTC)
        proposals = self.worker(self.evolution.run_weekly_if_due, now=monday)
        self.assert_research_succeeded(proposals)
        self.assertEqual(self.mind.count("evolution_weekly_completed"), 1)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.worker(self.evolution.run_weekly_if_due, now=monday), [])
        self.assertEqual(self.mind.count("evolution_research_session"), 1)

    def test_manual_research_can_recover_a_completed_week_without_resetting_it(self) -> None:
        monday = datetime(2026, 10, 19, tzinfo=UTC)
        reviewer = self.evolution.reviewer
        with patch.object(self.evolution, "reviewer", side_effect=ValueError("fixture failure")):
            self.assertEqual(self.worker(self.evolution.run_weekly_if_due, now=monday), [])
        completed = self.mind.recent("evolution_weekly_completed", 1)
        self.assertEqual(
            completed[0]["payload"]["errors"], [{"stage": "review", "error_type": "ValueError"}]
        )
        # Normal weekly scheduling must remain idempotent, even after repair.
        self.evolution.reviewer = reviewer
        self.assertEqual(self.worker(self.evolution.run_weekly_if_due, now=monday), [])
        proposals = self.worker(self.evolution.research_session)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(self.evolution.last_session_errors, [])
        self.assertEqual(self.mind.recent("evolution_weekly_completed", 1), completed)
        self.assertEqual(self.mind.count("evolution_research_session"), 2)
        self.assertEqual(self.mind.count("evolution_proposal_approval"), 0)
        self.assertEqual(self.mind.count("evolution_proposal_processed"), 0)

    def manifest(self) -> SkillManifest:
        return SkillManifest(
            id="fixture-skill",
            name="fixture_adapter",
            version="1.0.0",
            description="Fixture data validation",
            trigger_patterns=[],
            capabilities=set(),
            dependencies=[],
            created_at=0,
            author="fixture",
        )

    def test_closed_loader_registry_is_not_reused(self) -> None:
        self.registry.close()
        proposals = self.worker(self.evolution.research_session)
        self.assert_research_succeeded(proposals)

    def test_registry_handles_are_closed_after_success_and_exception(self) -> None:
        import sqlite3

        def check() -> None:
            evolution: Any = self.evolution
            factory = evolution._registry
            with factory() as registry:
                connection = registry._connection
                self.assertEqual(registry.list_installed(), [])
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
            failed_connection: Any = None
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                with factory() as registry:
                    failed_connection = registry._connection
                    raise RuntimeError("fixture failure")
            with self.assertRaises(sqlite3.ProgrammingError):
                failed_connection.execute("SELECT 1")

        self.worker(check)
        self.assertEqual(self.registry.list_installed(), [])

    def test_background_operator_install_keeps_exact_approval_gate(self) -> None:
        proposals = self.worker(self.evolution.research_session)
        self.assert_research_succeeded(proposals)
        proposal_id = proposals[0].proposal_id
        code = "def run():\n    return 'fixture'\n"
        manifest = self.manifest()
        with self.assertRaises(PermissionError):
            self.worker(self.evolution.apply_proposal, proposal_id, code, manifest)
        with self.assertRaises(PermissionError):
            self.worker(
                self.evolution.approve_proposal, proposal_id, code, manifest, reviewer="untrusted"
            )
        self.assertFalse((self.loader.root / manifest.name).exists())
        self.worker(self.evolution.approve_proposal, proposal_id, code, manifest, reviewer="Hermes")
        with self.assertRaises(PermissionError):
            self.worker(self.evolution.apply_proposal, proposal_id, code + "# changed\n", manifest)
        installed = self.worker(self.evolution.apply_proposal, proposal_id, code, manifest)
        self.assertEqual((installed / "skill.py").read_text(), code)
        self.assertEqual(self.registry.get(manifest.name), manifest)
        self.assertEqual(self.evolution.get_pending_proposals(), [])
        self.assertEqual(self.mind.count("evolution_proposal_approval"), 1)
        self.assertEqual(self.mind.count("evolution_proposal_processed"), 1)
        with self.assertRaises(ValueError):
            self.worker(self.evolution.apply_proposal, proposal_id, code, manifest)

    def test_inventory_is_fresh_and_installed_duplicates_are_rejected(self) -> None:
        # Evolution was already constructed; a cached inventory would miss this.
        manifest = self.manifest()
        self.registry.install(manifest)
        self.assertEqual(self.worker(self.evolution.research_session), [])
        self.assertEqual(self.evolution.last_session_errors, [])
        self.assertIn(manifest.name, self.review_prompts[0])
        self.assertEqual(self.mind.count("evolution_review"), 1)
        self.assertEqual(self.mind.count("evolution_proposal"), 0)

    def test_existing_pending_proposal_research_is_deduplicated_across_workers(self) -> None:
        first = self.worker(self.evolution.research_session)
        self.assert_research_succeeded(first)
        self.assertEqual(self.worker(self.evolution.research_session), [])
        self.assertEqual(self.evolution.last_session_errors, [])
        self.assertEqual(self.mind.count("evolution_proposal"), 1)
        self.assertEqual(len(self.review_prompts), 1)


class SelfEvolutionEvaluationTests(unittest.TestCase):
    def test_self_evaluate_returns_valid_public_metadata_with_real_timestamp(self) -> None:
        # Metadata evaluation must not require live stores or private module state.
        evolution = object.__new__(SelfEvolution)
        evaluate = getattr(evolution, "self_evaluate", None)
        self.assertTrue(callable(evaluate), "SelfEvolution needs a real self_evaluate hook")
        before = datetime.now(UTC)
        typed_evolution: Any = evolution
        metadata = typed_evolution._evaluation(evolution)
        after = datetime.now(UTC)
        self.assertEqual(metadata["module"], "self_evolution")
        self.assertEqual(
            set(metadata),
            {
                "module",
                "domain",
                "current_version",
                "known_limitations",
                "improvement_directions",
                "last_evaluated_at",
            },
        )
        self.assertTrue(metadata["known_limitations"])
        self.assertTrue(metadata["improvement_directions"])
        evaluated = datetime.fromisoformat(metadata["last_evaluated_at"])
        self.assertIsNotNone(evaluated.utcoffset())
        self.assertLessEqual(before, evaluated)
        self.assertLessEqual(evaluated, after)

    def test_self_evaluate_does_not_read_or_write_stores_or_call_services(self) -> None:
        import socket
        import sqlite3
        import subprocess
        from contextlib import ExitStack

        evolution = object.__new__(SelfEvolution)
        evaluate = getattr(evolution, "self_evaluate")
        blocked = AssertionError("self_evaluate must be metadata-only")
        with ExitStack() as guards:
            for target, method in (
                (Path, "open"),
                (sqlite3, "connect"),
                (socket, "create_connection"),
                (subprocess, "run"),
                (SelfEvolution, "_model"),
                (SelfEvolution, "_http"),
                (SelfEvolution, "_registry"),
                (MindStore, "append"),
                (MindStore, "recent"),
            ):
                guards.enter_context(patch.object(target, method, side_effect=blocked))
            guards.enter_context(patch("builtins.open", side_effect=blocked))
            metadata = evaluate()
        self.assertEqual(metadata["module"], "self_evolution")
        self.assertEqual(vars(evolution), {})

    def test_self_evaluate_returns_fresh_mutable_collections(self) -> None:
        evolution = object.__new__(SelfEvolution)
        evaluate = getattr(evolution, "self_evaluate")
        first = evaluate()
        original_limitations = list(first["known_limitations"])
        original_directions = list(first["improvement_directions"])
        first["known_limitations"].clear()
        first["improvement_directions"].clear()
        second = evaluate()
        self.assertEqual(second["known_limitations"], original_limitations)
        self.assertEqual(second["improvement_directions"], original_directions)

    def test_self_evaluate_is_owned_by_component_and_passes_admission_hook(self) -> None:
        from integration_registry import REGISTRY

        registry: Any = REGISTRY
        self.assertTrue(registry._has_self_evaluate(REGISTRY.get("self_evolution")))
        self.assertIn("self_evaluate", SelfEvolution.__dict__)


if __name__ == "__main__":
    unittest.main()
