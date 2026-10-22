"""Read-only integration admission gate; legacy debt is never hidden as green.

Run: PYTHONPATH=. ./runtime/engine-venv/bin/python3 -B -m unittest test_integration_contract -v
No product imports, credentials, source DB reads, model calls or runtime starts.
"""

from __future__ import annotations

from dataclasses import fields, replace
import importlib.util
from pathlib import Path
import tempfile
import unittest

from integration_registry import (
    CORE_MODULES,
    MIGRATION_WHITELIST,
    REGISTRY,
    ComponentKind,
    IntegrationRegistry,
    WiringContract,
    capability_descriptions,
)

ROOT = Path(__file__).parent


def source_path(component) -> Path:
    """语音子系统隔离在仓库根 voice/，路径形如 "voice/x.py"；其余仍相对 packaging/。"""
    declared = ROOT / component.source_paths[0]
    if declared.is_file():
        return declared
    return ROOT.parent / component.source_paths[0]


def unregistered_modules(root: Path, registry: IntegrationRegistry) -> list[str]:
    """Exact top-level product scan, not recursive runtime/vendor discovery."""
    discovered = {
        p.name
        for p in root.glob("*.py")
        if not p.name.startswith("test_")
        and p.name not in ("conftest.py",)  # 测试引导文件，不是产品模块
        and p.name not in MIGRATION_WHITELIST
    }
    registered = {
        c.component_id + ".py"
        for c in registry.list()
        if c.component_id + ".py" in c.source_paths
    }
    return sorted(discovered - registered)


def pending_manifest(registry: IntegrationRegistry = REGISTRY) -> list[dict]:
    return [
        dict(
            component=c.component_id,
            kind=c.kind.value,
            pending_fields=list(c.wiring.pending_fields),
            reason=c.pending_reason,
            self_evaluate=c.wiring.self_evaluate,
            core=c.core,
            reads_from=list(c.wiring.reads_from),
            capability_id=c.wiring.capability_id,
            writes_mind_category=list(c.wiring.writes_mind_category),
            kg_node_types=list(c.wiring.kg_node_types),
            ui_entry=c.wiring.ui_entry,
            ui_entry_reason=c.wiring.ui_entry_reason,
            tests=list(c.wiring.tests),
            source_paths=list(c.source_paths),
        )
        for c in registry.list()
        if c.pending
    ]


class RegistryBootstrapTests(unittest.TestCase):
    def test_integration_registry_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("integration_registry"))


class RegistryAPITests(unittest.TestCase):
    def contract(self):
        return WiringContract(
            capability_id="web_search",
            writes_mind_category=("event",),
            kg_node_types=("fact",),
            reads_from=("dependency",),
            self_evaluate=True,
            ui_entry="socket:query=tools",
            tests=("test_integration_contract.py",),
        )

    def test_required_contract_fields_cannot_be_omitted(self):
        values = {f.name: getattr(self.contract(), f.name) for f in fields(WiringContract)}
        required = (
            "capability_id",
            "writes_mind_category",
            "kg_node_types",
            "reads_from",
            "self_evaluate",
            "ui_entry",
            "tests",
        )
        for name in required:
            with self.subTest(field=name), self.assertRaises(TypeError):
                WiringContract(**{k: v for k, v in values.items() if k != name})

    def test_register_rejects_missing_nonpending_wiring(self):
        for field in (
            "capability_id",
            "writes_mind_category",
            "kg_node_types",
            "tests",
            "self_evaluate",
        ):
            value = None if field == "capability_id" else False if field == "self_evaluate" else ()
            with self.subTest(field=field), self.assertRaises(ValueError):
                IntegrationRegistry().register(
                    "bad",
                    ComponentKind.FEATURE,
                    replace(self.contract(), **{field: value}),
                    source_paths=("integration_registry.py",),
                )

    def test_register_requires_ui_absence_reason(self):
        with self.assertRaises(ValueError):
            IntegrationRegistry().register(
                "bad",
                ComponentKind.FEATURE,
                replace(self.contract(), ui_entry=None),
                source_paths=("integration_registry.py",),
            )

    def test_pending_requires_reason_and_field_coverage(self):
        for change in (
            dict(pending=True),
            dict(
                pending=True,
                pending_reason="not connected",
                pending_fields=("runtime_wiring",),
                capability_id=None,
            ),
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                IntegrationRegistry().register(
                    "bad",
                    ComponentKind.FEATURE,
                    replace(self.contract(), **change),
                    source_paths=("integration_registry.py",),
                )

    def test_register_get_list_filter_and_duplicate(self):
        registry = IntegrationRegistry()
        component = registry.register(
            "one", ComponentKind.FEATURE, self.contract(), source_paths=("integration_registry.py",)
        )
        self.assertIs(component, registry.get("one"))
        self.assertEqual(registry.list(), (component,))
        self.assertEqual(registry.list(ComponentKind.TOOL), ())
        with self.assertRaises(ValueError):
            registry.register(
                "one",
                ComponentKind.FEATURE,
                self.contract(),
                source_paths=("integration_registry.py",),
            )
        with self.assertRaises(KeyError):
            registry.get("missing")

    def test_source_ownership_cannot_borrow_another_components_hook(self):
        with self.assertRaises(ValueError):
            IntegrationRegistry().register(
                "amap",
                ComponentKind.FEATURE,
                self.contract(),
                source_paths=("amap.py", "integration_registry.py"),
            )

    def test_tests_must_be_product_relative_test_files(self):
        for path in ("../packaging/amap.py", "amap.py", "/private/test_x.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                IntegrationRegistry().register(
                    "bad",
                    ComponentKind.FEATURE,
                    replace(self.contract(), tests=(path,)),
                    source_paths=("integration_registry.py",),
                )

    def test_obvious_placeholder_hook_is_not_an_implementation(self):
        scratch = Path("/Users/sober/.hermes/cache/scratch/integration-registry")
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="hook-", dir=scratch) as folder:
            root = Path(folder)
            for body in ("pass", "return None", "raise NotImplementedError", "..."):
                (root / "candidate.py").write_text("def self_evaluate():\n    " + body + "\n")
                from integration_registry import Component

                c = Component(
                    "candidate", ComponentKind.FEATURE, self.contract(), ("candidate.py",)
                )
                self.assertFalse(IntegrationRegistry(root)._has_self_evaluate(c), body)

    def test_fixture_capability_metadata_is_not_trusted(self):
        scratch = Path("/Users/sober/.hermes/cache/scratch/integration-registry")
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="capability-", dir=scratch) as folder:
            root = Path(folder)
            (root / "capabilities.py").write_text(
                "_REAL: list = []\n_WEB: list = []\n"
                "CATALOG = [{'id':'fixture','persona_description':'我是假能力'}]\n"
                "def fake():\n    return {'id':'hidden','persona_description':'我未启用'}\n"
            )
            (root / "device_controller.py").write_text("")
            self.assertEqual(capability_descriptions(root), {})

    def test_unknown_capability_is_rejected(self):
        with self.assertRaises(ValueError):
            IntegrationRegistry().register(
                "bad",
                ComponentKind.FEATURE,
                replace(self.contract(), capability_id="invented_local_tool"),
                source_paths=("integration_registry.py",),
            )

    def test_orphan_detection_uses_both_directions_and_ignores_self_edges(self):
        registry = IntegrationRegistry()
        for identity, deps in (("a", ("b",)), ("b", ()), ("isolated", ()), ("self", ("self",))):
            registry.register(
                identity,
                ComponentKind.FEATURE,
                replace(self.contract(), reads_from=deps),
                source_paths=("integration_registry.py",),
            )
        self.assertEqual([c.component_id for c in registry.orphans()], ["isolated", "self"])
        issues = registry.validate_all()
        self.assertTrue(any("self-dependency" in s for s in issues["self"]))

    def test_validate_all_reports_missing_sources_tests_dependencies_and_hook(self):
        registry = IntegrationRegistry()
        registry.register(
            "bad",
            ComponentKind.FEATURE,
            replace(self.contract(), tests=("test_missing.py",)),
            source_paths=("missing.py",),
        )
        issues = "\n".join(registry.validate_all()["bad"])
        for text in (
            "unknown component dependency",
            "missing missing.py",
            "missing test_missing.py",
            "no implementation",
        ):
            self.assertIn(text, issues)

    def test_scan_rejects_a_future_module_without_registration(self):
        scratch = Path("/Users/sober/.hermes/cache/scratch/integration-registry")
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="admission-", dir=scratch) as folder:
            root = Path(folder)
            (root / "future_feature.py").write_text("")
            (root / "test_future_feature.py").write_text("")
            (root / "migrate_beliefs.py").write_text("")
            (root / "runtime").mkdir()
            (root / "runtime" / "vendor.py").write_text("")
            self.assertEqual(
                unregistered_modules(root, IntegrationRegistry(root)), ["future_feature.py"]
            )

    def test_registry_self_evaluation_returns_real_audit(self):
        result = REGISTRY.self_evaluate()
        self.assertEqual(result["components"], len(REGISTRY.list()))
        self.assertEqual(result["missing"], REGISTRY.validate_all())


class ProductIntegrationContractTests(unittest.TestCase):
    def test_all_product_modules_registered(self):
        self.assertEqual(
            unregistered_modules(ROOT, REGISTRY),
            [],
            "Unregistered product module: add an explicit wiring contract",
        )

    def test_all_component_kinds_have_declarations(self):
        self.assertEqual({c.kind for c in REGISTRY.list()}, set(ComponentKind))
        self.assertEqual(
            {k.name for k in ComponentKind},
            {
                "FEATURE",
                "SKILL",
                "MEMORY",
                "KNOWLEDGE",
                "SENSE",
                "TOOL",
                "MODEL",
                "EXTERNAL",
                "GOAL",
                "PERSONA",
                "EVOLUTION",
                "NOTIFY",
                "UI",
                "OBSERVABILITY",
            },
        )

    def test_pending_inventory_is_visible_but_not_a_failure(self):
        manifest = pending_manifest()
        print(f"\nPENDING {len(manifest)}")
        for item in manifest:
            print(
                f"PENDING {item['component']} [{','.join(item['pending_fields'])}]: {item['reason']}"
            )
        self.assertTrue(all(item["reason"] and item["pending_fields"] for item in manifest))

    def test_noncore_components_implement_self_evaluate(self):
        missing = [
            c.component_id for c in REGISTRY.list() if not c.core and not c.wiring.self_evaluate
        ]
        self.assertEqual(
            missing, [], "Non-core self_evaluate is mandatory even for pending legacy components"
        )

    def test_no_orphan_components(self):
        self.assertEqual(
            [c.component_id for c in REGISTRY.orphans()],
            [],
            "Orphans need real integration, not invented dependency edges",
        )

    def test_declared_sources_tests_dependencies_and_hooks_exist(self):
        hard = {
            identity: [
                issue
                for issue in issues
                if ": PENDING" not in issue
                and not issue.startswith("self_evaluate: non-core")
                and "orphan (" not in issue
            ]
            for identity, issues in REGISTRY.validate_all().items()
        }
        self.assertEqual({k: v for k, v in hard.items() if v}, {})

    def test_capability_ids_have_real_first_person_descriptions(self):
        descriptions = capability_descriptions()
        for c in REGISTRY.list():
            if c.wiring.capability_id is not None:
                with self.subTest(component=c.component_id):
                    self.assertTrue(descriptions[c.wiring.capability_id].startswith("我"))

    def test_contract_dependency_declarations_have_source_evidence(self):
        # Static evidence is deliberately narrow. Runtime injection still needs
        # dedicated behavior tests; these imports are not delivery proof.
        import ast

        dynamic = {
            ("bootstrap", "jarvis"),  # runpy.run_path(with_name('jarvis.py'))
            ("jarvis_ui", "jarvis_socket"),  # socket wire consumer, no Python import
            ("senses.api", "__init__"),  # package import path bridge
            ("daemon_wiring", "senses.api"),  # senses.__init__ reexports poll_all
            ("integration_registry", "capabilities"),  # AST file reader, no import
            ("evolution_scheduler", "inner_loop"),  # injected self_evolution.mind_store
            ("evolution_scheduler", "outreach"),  # injected enqueue/queue reader
            ("evolution_scheduler", "self_evolution"),  # injected research/weekly methods
            ("health_scheduler", "health_analyzer"),  # injected analyzer.daily_summary
            ("health_scheduler", "health_rules"),  # injected rules.evaluate
            ("health_scheduler", "health_alert"),  # injected alert_manager.process_alerts
            ("health_scheduler", "inner_loop"),  # injected mind_store.append/path
            ("health_scheduler", "knowledge_graph"),  # alert_manager.knowledge_graph
            ("health_scheduler", "outreach"),  # injected outreach.enqueue
        }
        missing = []
        for component in REGISTRY.list():
            if component.component_id in {
                "imessage_input",
                "mcp_servers",
                "api_credentials",
                "mind.entries",
                "mind.belief_versions",
            }:
                continue  # logical resource owned by the declared real source
            tree = ast.parse(source_path(component).read_text())
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(
                        (
                            "senses."
                            if node.level and component.component_id.startswith("senses.")
                            else ""
                        )
                        + node.module
                    )
            for dependency in component.wiring.reads_from:
                if (component.component_id, dependency) in dynamic:
                    continue
                if not any(
                    name == dependency or name.startswith(dependency + ".") for name in imports
                ):
                    missing.append(f"{component.component_id} -> {dependency}")
        self.assertEqual(
            missing, [], "Dependency declaration has no source evidence; do not invent wiring"
        )

    def test_core_exemptions_cannot_silently_expand(self):
        self.assertEqual({c.component_id for c in REGISTRY.list() if c.core}, set(CORE_MODULES))
        self.assertEqual(
            set(CORE_MODULES),
            {
                "think_controller",
                "inner_loop",
                "inner_runtime",
                "inner_voice",
                "daemon_wiring",
                "jarvis_daemon",
                "affect_engine",
                "affect_rules",
                "dialogue_affect",
            },
        )


class RegistryClosureTests(unittest.TestCase):
    """Additional fail-closed checks; the original admission tests stay intact."""

    def fixture_root(self):
        # mkdtemp creates a new 0700 directory; never reuse product data.
        parent = Path.home() / ".jarvis"
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
        folder = tempfile.TemporaryDirectory(prefix="registry-contract-", dir=parent)
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        return root

    def test_declarations_are_literal_and_match_their_own_source_ast(self):
        import ast

        from integration_registry import _DECLARATIONS

        tree = ast.parse((ROOT / "integration_registry.py").read_text())
        assignment = next(
            n
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_DECLARATIONS" for t in n.targets)
        )
        literal = ast.literal_eval(assignment.value)
        self.assertEqual(literal, _DECLARATIONS, "No runtime flag synthesis or auto-registration")
        for component in REGISTRY.list():
            with self.subTest(component=component.component_id):
                self.assertEqual(
                    component.wiring.self_evaluate, REGISTRY._has_self_evaluate(component)
                )
                if component.wiring.self_evaluate:
                    self.assertNotIn("self_evaluate", component.wiring.pending_fields)

    def test_noncore_flags_cannot_hide_missing_implementations(self):
        for component in REGISTRY.list():
            if not component.core:
                with self.subTest(component=component.component_id):
                    self.assertTrue(REGISTRY._has_self_evaluate(component))

    def test_nested_returns_do_not_turn_an_empty_hook_into_implementation(self):
        from integration_registry import Component

        root = self.fixture_root()
        cases = (
            'def self_evaluate():\n    def hidden():\n        return {"ok": True}\n',
            'def self_evaluate():\n    class Hidden:\n        def result(self):\n            return {"ok": True}\n',
            'def self_evaluate():\n    if False:\n        return {"ok": True}\n    return None\n',
            "def self_evaluate():\n    return {}\n",
            "def self_evaluate():\n    return True\n",
        )
        component = Component(
            "candidate", ComponentKind.FEATURE, RegistryAPITests().contract(), ("candidate.py",)
        )
        for code in cases:
            with self.subTest(code=code):
                (root / "candidate.py").write_text(code)
                self.assertFalse(IntegrationRegistry(root)._has_self_evaluate(component))

    def test_required_positional_and_keyword_parameters_are_not_offline_hooks(self):
        from integration_registry import Component

        root = self.fixture_root()
        component = Component(
            "candidate", ComponentKind.FEATURE, RegistryAPITests().contract(), ("candidate.py",)
        )
        for signature in ("value", "*, value", "value, /", "self, value"):
            with self.subTest(signature=signature):
                (root / "candidate.py").write_text(
                    f'def self_evaluate({signature}):\n    return {{"ok": True}}\n'
                )
                self.assertFalse(IntegrationRegistry(root)._has_self_evaluate(component))

    def test_real_no_argument_and_bound_method_hooks_are_recognized(self):
        from integration_registry import Component

        root = self.fixture_root()
        component = Component(
            "candidate", ComponentKind.FEATURE, RegistryAPITests().contract(), ("candidate.py",)
        )
        for code in (
            'def self_evaluate():\n    return {"ok": True}\n',
            'class Candidate:\n    def self_evaluate(self):\n        return {"ok": True}\n',
            'class Candidate:\n    def self_evaluate(self, /):\n        return {"ok": True}\n',
            'class Candidate:\n    @staticmethod\n    def self_evaluate():\n        return {"ok": True}\n',
            'class Candidate:\n    @classmethod\n    def self_evaluate(cls):\n        return {"ok": True}\n',
        ):
            with self.subTest(code=code):
                (root / "candidate.py").write_text(code)
                self.assertTrue(IntegrationRegistry(root)._has_self_evaluate(component))

    def test_unbound_or_static_self_parameter_is_not_implicitly_supplied(self):
        from integration_registry import Component

        root = self.fixture_root()
        component = Component(
            "candidate", ComponentKind.FEATURE, RegistryAPITests().contract(), ("candidate.py",)
        )
        for code in (
            'def self_evaluate(self):\n    return {"ok": True}\n',
            'class Candidate:\n    @staticmethod\n    def self_evaluate(self):\n        return {"ok": True}\n',
        ):
            with self.subTest(code=code):
                (root / "candidate.py").write_text(code)
                self.assertFalse(IntegrationRegistry(root)._has_self_evaluate(component))

    def test_shadowed_or_unbindable_methods_are_not_admitted(self):
        from integration_registry import Component

        root = self.fixture_root()
        component = Component(
            "candidate", ComponentKind.FEATURE, RegistryAPITests().contract(), ("candidate.py",)
        )
        for code in (
            'class Candidate:\n    def self_evaluate():\n        return {"ok": True}\n',
            'class Candidate:\n    @classmethod\n    def self_evaluate():\n        return {"ok": True}\n',
            'def self_evaluate():\n    return {"ok": True}\nself_evaluate = None\n',
            'def self_evaluate():\n    return {"ok": True}\ndef self_evaluate():\n    return None\n',
            'class Candidate:\n    def self_evaluate(self):\n        return {"ok": True}\n    self_evaluate = None\n',
            'class Candidate:\n    def self_evaluate(self):\n        return {"ok": True}\nCandidate = None\n',
        ):
            with self.subTest(code=code):
                (root / "candidate.py").write_text(code)
                self.assertFalse(IntegrationRegistry(root)._has_self_evaluate(component))

    def test_new_components_have_owned_sources_and_direct_tests(self):
        for identity in ("imessage_input", "kg_projector"):
            with self.subTest(component=identity):
                component = REGISTRY.get(identity)
                self.assertEqual(component.source_paths, (identity + ".py",))
                self.assertIn("test_" + identity + ".py", component.wiring.tests)
                self.assertTrue(component.wiring.self_evaluate)
                self.assertFalse(component.core)

    def test_offline_context_observer_is_registered_without_runtime_acceptance_claim(self):
        component = REGISTRY.get("context_metrics")
        self.assertEqual(component.source_paths, ("context_metrics.py",))
        self.assertEqual(component.wiring.reads_from, ("curiosity", "inner_runtime", "inner_voice"))
        self.assertEqual(component.wiring.tests, ("test_context_metrics.py",))
        self.assertTrue(component.wiring.self_evaluate)
        self.assertTrue(REGISTRY._has_self_evaluate(component))
        self.assertIn("runtime_wiring", component.wiring.pending_fields)
        self.assertEqual(component.wiring.writes_mind_category, ())
        self.assertEqual(component.wiring.kg_node_types, ())

    def test_reverse_dependencies_are_derived_and_unknown_targets_rejected(self):
        registry = IntegrationRegistry()
        helper = RegistryAPITests()
        registry.register(
            "upstream",
            ComponentKind.FEATURE,
            replace(helper.contract(), reads_from=()),
            source_paths=("integration_registry.py",),
        )
        registry.register(
            "consumer",
            ComponentKind.FEATURE,
            replace(helper.contract(), reads_from=("upstream", "consumer")),
            source_paths=("integration_registry.py",),
        )
        self.assertEqual([c.component_id for c in registry.dependents("upstream")], ["consumer"])
        self.assertEqual(registry.dependents("consumer"), ())
        with self.assertRaises(KeyError):
            registry.dependents("unknown")
        audit = registry.self_evaluate()
        self.assertEqual(audit["reverse_dependencies"]["upstream"], ["consumer"])

    def test_projector_dependencies_form_the_real_knowledge_graph_incoming_edge(self):
        component = REGISTRY.get("kg_projector")
        self.assertEqual(component.wiring.reads_from, ("inner_loop", "knowledge_graph"))
        incoming = {
            c.component_id for c in REGISTRY.list() if "knowledge_graph" in c.wiring.reads_from
        }
        self.assertIn("kg_projector", incoming)
        self.assertTrue(
            callable(getattr(REGISTRY, "dependents", None)),
            "Reverse consumers must be queryable without fabricating reads_from edges",
        )
        self.assertIn(
            "kg_projector", {c.component_id for c in REGISTRY.dependents("knowledge_graph")}
        )
        self.assertNotIn(
            "kg_projector",
            REGISTRY.get("knowledge_graph").wiring.reads_from,
            "Reverse consumers are not imports performed by the graph implementation",
        )
        self.assertIn("kg_projector", REGISTRY.get("daemon_wiring").wiring.reads_from)
        self.assertEqual(component.wiring.writes_mind_category, ())
        self.assertEqual(set(component.wiring.kg_node_types), {"observation", "health_event"})
        self.assertIn("runtime_wiring", component.wiring.pending_fields)
        self.assertIn("unmapped", component.pending_reason.lower())

    def test_daemon_dependencies_are_explicit_and_backed_by_construction(self):
        import ast

        expected = {
            "tool_router",
            "goal_tracker",
            "session_resume",
            "self_evolution",
            "evolution_scheduler",
            "health_scheduler",
            "kg_projector",
        }
        self.assertTrue(expected <= set(REGISTRY.get("daemon_wiring").wiring.reads_from))
        tree = ast.parse((ROOT / "daemon_wiring.py").read_text())
        wiring = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Wiring")
        factory = next(
            n
            for n in wiring.body
            if isinstance(n, ast.FunctionDef) and n.name == "_wire_integrations"
        )
        imported = {n.module for n in ast.walk(factory) if isinstance(n, ast.ImportFrom)}
        built = {
            n.args[0].value
            for n in ast.walk(factory)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "build"
            and n.args
            and isinstance(n.args[0], ast.Constant)
        }
        self.assertTrue(expected <= imported)
        self.assertTrue(expected <= built)

    def test_imessage_evaluation_owner_is_not_claimed_as_the_resident_poll_implementation(self):
        component = REGISTRY.get("imessage_input")
        self.assertEqual(component.wiring.reads_from, ("daemon_wiring",))
        self.assertEqual(component.wiring.writes_mind_category, ())
        self.assertIn("daemon_wiring", component.pending_reason)
        self.assertIn("not", component.pending_reason.lower())
        self.assertIn("runtime_wiring", component.wiring.pending_fields)
        self.assertIn("def _poll_imessage(", (ROOT / "daemon_wiring.py").read_text())
        # A documented resource-owner link is not a fictitious Python import.
        self.assertIn("daemon_wiring", (ROOT / "imessage_input.py").read_text())

    def test_current_assembly_is_not_described_as_missing(self):
        forbidden = (
            "daemon construction/tick/close is not wired",
            "daemon assembly/tick is not wired",
            "resident assembly/tick is not wired",
            "resident SelfEvolution/EvolutionScheduler assembly is not present",
        )
        for identity in (
            "health_alert",
            "health_analyzer",
            "health_rules",
            "health_scheduler",
            "evolution_scheduler",
            "self_evolution",
            "daemon_wiring",
        ):
            note = REGISTRY.get(identity).pending_reason
            for phrase in forbidden:
                self.assertNotIn(phrase, note)
            self.assertIn("runtime_wiring", REGISTRY.get(identity).wiring.pending_fields)

    def test_nonresident_orphan_reviews_are_exact_narrow_and_reasoned(self):
        from integration_registry import NONRESIDENT_ORPHAN_WHITELIST

        self.assertEqual(set(NONRESIDENT_ORPHAN_WHITELIST), {"evidence_support", "linux_preflight"})
        raw = {c.component_id for c in REGISTRY.orphans(include_nonresident=True)}
        self.assertEqual(raw, set(NONRESIDENT_ORPHAN_WHITELIST))
        audit = REGISTRY.self_evaluate()
        self.assertEqual(audit["nonresident_orphans"], NONRESIDENT_ORPHAN_WHITELIST)
        for identity, reason in NONRESIDENT_ORPHAN_WHITELIST.items():
            with self.subTest(component=identity):
                self.assertGreaterEqual(len(reason.strip()), 50)
                component = REGISTRY.get(identity)
                self.assertFalse(component.core)
                self.assertTrue(component.pending)
                self.assertTrue(component.wiring.self_evaluate)
                self.assertTrue(REGISTRY._has_self_evaluate(component))
                self.assertEqual(component.wiring.reads_from, ())
                self.assertIn("reads_from", component.wiring.pending_fields)
                self.assertIn("runtime_wiring", component.wiring.pending_fields)
                self.assertEqual(component.wiring.kg_node_types, ())
                self.assertEqual(component.wiring.writes_mind_category, ())
                self.assertIsNone(component.wiring.capability_id)
                self.assertTrue(component.wiring.tests)

    def test_reviews_do_not_apply_to_generic_registries_or_future_modules(self):
        registry = IntegrationRegistry()
        for identity in ("evidence_support", "linux_preflight", "voice_port", "future_feature"):
            registry.register(
                identity,
                ComponentKind.FEATURE,
                replace(RegistryAPITests().contract(), reads_from=()),
                source_paths=("integration_registry.py",),
            )
        self.assertEqual(
            {c.component_id for c in registry.orphans()},
            {"evidence_support", "linux_preflight", "voice_port", "future_feature"},
        )

    def test_reviews_reject_unknown_blank_and_fixture_root_exemptions(self):
        from integration_registry import NONRESIDENT_ORPHAN_WHITELIST

        for reviews in (
            {"future_feature": "Not reviewed"},
            {"voice_port": ""},
            {"voice_port": "Generic adapter, approved without evidence"},
        ):
            with self.subTest(reviews=reviews), self.assertRaises(ValueError):
                IntegrationRegistry(nonresident_orphans=reviews)
        with self.assertRaises(ValueError):
            IntegrationRegistry(
                self.fixture_root(), nonresident_orphans=NONRESIDENT_ORPHAN_WHITELIST
            )

    def test_reviewed_islands_have_no_runtime_import_consumers(self):
        import ast

        from integration_registry import NONRESIDENT_ORPHAN_WHITELIST

        consumers = []
        for component in REGISTRY.list():
            if (
                component.component_id in NONRESIDENT_ORPHAN_WHITELIST
                or component.component_id == "integration_registry"
            ):
                continue
            tree = ast.parse(source_path(component).read_text())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                elif (
                    isinstance(node, ast.Call)
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and (
                        (isinstance(node.func, ast.Name) and node.func.id == "__import__")
                        or (
                            isinstance(node.func, ast.Attribute)
                            and node.func.attr == "import_module"
                        )
                    )
                ):
                    names = [node.args[0].value]
                for name in names:
                    if name.split(".")[0] in NONRESIDENT_ORPHAN_WHITELIST:
                        consumers.append((component.component_id, name))
        self.assertEqual(
            consumers,
            [],
            "A new production consumer requires removing/reviewing the island exemption",
        )

    def test_review_cannot_mask_wrong_kind_source_core_or_missing_hook(self):
        from integration_registry import NONRESIDENT_ORPHAN_WHITELIST

        identity = "voice_port"
        original = REGISTRY.get(identity)
        cases = (
            replace(original, source_paths=("integration_registry.py",)),
            replace(original, kind=ComponentKind.FEATURE),
            replace(original, core=True, core_reason="must not reuse orphan review"),
            replace(
                original,
                wiring=replace(
                    original.wiring,
                    self_evaluate=False,
                    pending_fields=original.wiring.pending_fields + ("self_evaluate",),
                ),
            ),
            replace(
                original,
                wiring=replace(
                    original.wiring,
                    tests=(),
                    pending_fields=original.wiring.pending_fields + ("tests",),
                ),
            ),
            replace(original, wiring=replace(original.wiring, capability_id="web_search")),
        )
        for candidate in cases:
            with self.subTest(candidate=candidate):
                registry = IntegrationRegistry(nonresident_orphans=NONRESIDENT_ORPHAN_WHITELIST)
                registry.register(
                    candidate.component_id,
                    candidate.kind,
                    candidate.wiring,
                    source_paths=candidate.source_paths,
                    core=candidate.core,
                    core_reason=candidate.core_reason,
                )
                self.assertEqual([c.component_id for c in registry.orphans()], [identity])
        from unittest.mock import patch

        registry = IntegrationRegistry(nonresident_orphans=NONRESIDENT_ORPHAN_WHITELIST)
        registry.register(
            identity, original.kind, original.wiring, source_paths=original.source_paths
        )
        with patch.object(registry, "_has_self_evaluate", return_value=False):
            self.assertEqual([c.component_id for c in registry.orphans()], [identity])

    def test_whitelist_does_not_relax_module_scan_or_self_evaluation_gate(self):
        from integration_registry import NONRESIDENT_ORPHAN_WHITELIST

        root = self.fixture_root()
        for identity in NONRESIDENT_ORPHAN_WHITELIST:
            (root / (identity + ".py")).write_text("")
        self.assertEqual(
            unregistered_modules(root, IntegrationRegistry(root)),
            sorted(identity + ".py" for identity in NONRESIDENT_ORPHAN_WHITELIST),
        )
        validations = REGISTRY.validate_all()
        for identity in NONRESIDENT_ORPHAN_WHITELIST:
            self.assertTrue(
                any(issue.startswith("runtime_wiring: PENDING") for issue in validations[identity])
            )
            self.assertTrue(
                any(issue.startswith("reads_from: PENDING") for issue in validations[identity])
            )

    def test_migration_and_core_exemptions_are_not_expanded_for_hook_debt(self):
        self.assertEqual(set(MIGRATION_WHITELIST), {"migrate_beliefs.py"})
        self.assertEqual(
            set(CORE_MODULES),
            {
                "think_controller",
                "inner_loop",
                "inner_runtime",
                "inner_voice",
                "daemon_wiring",
                "jarvis_daemon",
                "affect_engine",
                "affect_rules",
                "dialogue_affect",
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
