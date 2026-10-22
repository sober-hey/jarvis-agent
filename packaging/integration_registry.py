"""Explicit product wiring ledger; importing never starts product components.

A declaration is not proof of runtime delivery. Pending legacy debt is visible,
not silently upgraded to connected. New modules must be explicitly registered;
this module NEVER scans the directory to auto-register future files.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ComponentKind(str, Enum):
    FEATURE = "feature"
    SKILL = "skill"
    MEMORY = "memory"
    KNOWLEDGE = "knowledge"
    SENSE = "sense"
    TOOL = "tool"
    MODEL = "model"
    EXTERNAL = "external"
    GOAL = "goal"
    PERSONA = "persona"
    EVOLUTION = "evolution"
    NOTIFY = "notify"
    UI = "ui"
    OBSERVABILITY = "observability"


@dataclass(frozen=True)
class WiringContract:
    # None/empty collections are explicit missing wiring, never an invented ID.
    capability_id: str | None
    writes_mind_category: tuple[str, ...]
    kg_node_types: tuple[str, ...]
    reads_from: tuple[str, ...]
    self_evaluate: bool
    ui_entry: str | None
    tests: tuple[str, ...]
    ui_entry_reason: str = ""
    pending: bool = False
    pending_reason: str = ""
    pending_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class Component:
    component_id: str
    kind: ComponentKind
    wiring: WiringContract
    source_paths: tuple[str, ...]
    core: bool = False
    core_reason: str = ""

    @property
    def pending(self) -> bool:
        return self.wiring.pending

    @property
    def pending_reason(self) -> str:
        return self.wiring.pending_reason


# 语音子系统（voice/ 目录）的模块，路径与 packaging/ 下的组件不同。
VOICE_MODULES = frozenset(
    {
        "voice_dashscope",
        "voice_gateway",
        "voice_ledger",
        "voice_live",
        "voice_port",
        "voice_speaker",
        "voice_stream_tts",
        "voice_volcengine",
        "voice_xiaoai",
        "voice",
    }
)


class IntegrationRegistry:
    def __init__(
        self, source_root: Path | None = None, *, nonresident_orphans: dict[str, str] | None = None
    ):
        self.source_root = Path(source_root or Path(__file__).parent)
        # 语音子系统隔离在仓库根 voice/，路径形如 "voice/x.py"；
        # 统一按仓库根解析，避免只认 packaging/ 下的裸名。
        self.repo_root = self.source_root.parent
        self._components: dict[str, Component] = {}
        reviews = dict(nonresident_orphans or {})
        if reviews and (
            self.source_root.resolve() != Path(__file__).parent.resolve()
            or any(
                identity not in NONRESIDENT_ORPHAN_WHITELIST
                or reason != NONRESIDENT_ORPHAN_WHITELIST[identity]
                for identity, reason in reviews.items()
            )
        ):
            raise ValueError("nonresident orphans require exact reviewed product-root reasons")
        self._nonresident_orphans = reviews

    def register(
        self,
        component_id: str,
        kind: ComponentKind,
        wiring: WiringContract,
        *,
        source_paths: tuple[str, ...],
        core: bool = False,
        core_reason: str = "",
    ) -> Component:
        if not isinstance(component_id, str) or not component_id.strip():
            raise ValueError("component_id is required")
        if component_id in self._components:
            raise ValueError(f"duplicate component: {component_id}")
        if not isinstance(kind, ComponentKind) or not isinstance(wiring, WiringContract):
            raise TypeError("ComponentKind and WiringContract are required")
        for name in (
            "writes_mind_category",
            "kg_node_types",
            "reads_from",
            "tests",
            "pending_fields",
        ):
            values = getattr(wiring, name)
            if not isinstance(values, tuple) or any(
                not isinstance(v, str) or not v.strip() for v in values
            ):
                raise ValueError(f"{name} must be a tuple of nonempty strings")
            if len(values) != len(set(values)):
                raise ValueError(f"{name} contains duplicates")
        if type(wiring.self_evaluate) is not bool or type(wiring.pending) is not bool:
            raise ValueError("self_evaluate and pending must be explicit booleans")
        if wiring.capability_id is not None and (
            not isinstance(wiring.capability_id, str) or not wiring.capability_id.strip()
        ):
            raise ValueError("capability_id must be nonempty or explicitly pending")
        if wiring.ui_entry is not None and (
            not isinstance(wiring.ui_entry, str) or not wiring.ui_entry.strip()
        ):
            raise ValueError("ui_entry must be nonempty or None with a reason")
        if wiring.ui_entry is None and not wiring.ui_entry_reason.strip():
            raise ValueError("ui_entry_reason is required when there is no UI entry")
        if not source_paths or not isinstance(source_paths, tuple):
            raise ValueError("source_paths is required")
        if any(
            not isinstance(p, str) or not p or Path(p).is_absolute() or ".." in Path(p).parts
            for p in source_paths
        ):
            raise ValueError("source_paths must be product-relative paths")
        if len(source_paths) != 1:
            raise ValueError(
                "one explicit owning source per component; split resources into components"
            )
        for test in wiring.tests:
            path = Path(test)
            if (
                path.is_absolute()
                or ".." in path.parts
                or path.suffix != ".py"
                or not path.name.startswith("test_")
                or not (self.source_root / path)
                .resolve()
                .is_relative_to(self.source_root.resolve())
            ):
                raise ValueError("tests must be product-relative test_*.py files")
        if type(core) is not bool or (core and not core_reason.strip()):
            raise ValueError("core exemption requires a reason")
        missing = []
        if wiring.capability_id is None:
            missing.append("capability_id")
        if not wiring.writes_mind_category:
            missing.append("writes_mind_category")
        if not wiring.kg_node_types:
            missing.append("kg_node_types")
        if not wiring.tests:
            missing.append("tests")
        if not wiring.self_evaluate and not core:
            missing.append("self_evaluate")
        if wiring.pending:
            if not isinstance(wiring.pending_reason, str) or not wiring.pending_reason.strip():
                raise ValueError("pending_reason is required")
            if not wiring.pending_fields or not set(missing).issubset(wiring.pending_fields):
                raise ValueError(f"pending_fields must explain missing wiring: {missing}")
        elif missing or wiring.pending_reason or wiring.pending_fields:
            raise ValueError(f"incomplete non-pending contract: {missing}")
        if wiring.pending and not isinstance(wiring.ui_entry_reason, str):
            raise ValueError("ui_entry_reason must be a string")
        if wiring.capability_id is not None:
            descriptions = capability_descriptions(self.source_root)
            if not descriptions.get(wiring.capability_id, "").startswith("我"):
                raise ValueError("capability_id must have a first-person trusted descriptor")
        if not isinstance(wiring.ui_entry_reason, str) or not isinstance(
            wiring.pending_reason, str
        ):
            raise ValueError("reasons must be strings")
        if wiring.pending_fields and not set(wiring.pending_fields) <= {
            "capability_id",
            "writes_mind_category",
            "kg_node_types",
            "reads_from",
            "self_evaluate",
            "ui_entry",
            "tests",
            "runtime_wiring",
        }:
            raise ValueError("unknown pending field")
        component = Component(component_id, kind, wiring, source_paths, core, core_reason)
        self._components[component_id] = component
        return component

    def get(self, component_id: str) -> Component:
        return self._components[component_id]

    def list(self, kind: ComponentKind | None = None) -> tuple[Component, ...]:
        return tuple(
            c for _, c in sorted(self._components.items()) if kind is None or c.kind == kind
        )

    def dependents(self, component_id: str) -> tuple[Component, ...]:
        """Derive reverse consumer edges; never declare them as forward imports."""
        self.get(component_id)  # typo/unknown targets must not look like no consumers
        return tuple(
            c
            for c in self.list()
            if c.component_id != component_id and component_id in c.wiring.reads_from
        )

    def orphans(self, *, include_nonresident: bool = False) -> tuple[Component, ...]:
        """Unconnected components; reviewed nonresident islands remain auditable.

        Exemptions apply only to this exact product snapshot, never generic
        registries, renamed/borrowed sources, core modules, or missing hooks.
        They do not constitute dependency edges or runtime acceptance.
        """
        connected: set[str] = set()
        for c in self.list():
            for dependency in c.wiring.reads_from:
                if dependency in self._components and dependency != c.component_id:
                    connected.update((c.component_id, dependency))
        return tuple(
            c
            for c in self.list()
            if c.component_id not in connected
            and (include_nonresident or not self._reviewed_nonresident(c))
        )

    def _reviewed_nonresident(self, component: Component) -> bool:
        return (
            component.component_id in self._nonresident_orphans
            and component.source_paths == (component.component_id + ".py",)
            and component.kind
            == (
                ComponentKind.SENSE
                if component.component_id == "voice_port"
                else ComponentKind.OBSERVABILITY
            )
            and not component.core
            and component.pending
            and component.wiring.self_evaluate
            and self._has_self_evaluate(component)
            and not component.wiring.reads_from
            and bool(component.wiring.tests)
            and component.wiring.capability_id is None
            and not component.wiring.writes_mind_category
            and not component.wiring.kg_node_types
            and component.wiring.ui_entry is None
        )

    def validate_all(self) -> dict[str, tuple[str, ...]]:
        """Return every component and its missing/unverified integration points."""
        result = {}
        orphan_ids = {c.component_id for c in self.orphans()}
        for c in self.list():
            issues = [f"{field}: PENDING — {c.pending_reason}" for field in c.wiring.pending_fields]
            for dependency in c.wiring.reads_from:
                if dependency not in self._components:
                    issues.append(f"reads_from: unknown component {dependency}")
                if dependency == c.component_id:
                    issues.append("reads_from: self-dependency is not integration")
            for source in c.source_paths:
                if not self.resolve_source(source).is_file():
                    issues.append(f"source_paths: missing {source}")
            for test in c.wiring.tests:
                if not (self.source_root / test).is_file() and not (
                    self.repo_root / test
                ).is_file():
                    issues.append(f"tests: missing {test}")
            if not c.core and not c.wiring.self_evaluate:
                issues.append("self_evaluate: non-core component has no implementation")
            if c.wiring.self_evaluate and not self._has_self_evaluate(c):
                issues.append("self_evaluate: declared True but no implementation in source")
            if c.component_id in orphan_ids:
                issues.append("reads_from: orphan (no incoming or outgoing dependency)")
            result[c.component_id] = tuple(issues)
        return result

    def resolve_source(self, relative: str) -> Path:
        """把注册表里的相对路径解析成真实文件：先 packaging/，再仓库根。"""
        candidate = self.source_root / relative
        if candidate.is_file():
            return candidate
        return self.repo_root / relative

    def _has_self_evaluate(self, component: Component) -> bool:
        """Recognize an owned callable hook, not nested/placeholder returns.

        This is a static admission check, not execution or proof of runtime
        availability. Required arguments are counted after actual method binding;
        a module function named ``self`` or staticmethod receives no free argument.
        """
        function_types = (ast.FunctionDef, ast.AsyncFunctionDef)

        def own_returns(statements: list[ast.stmt]) -> list[ast.Return]:
            found: list[ast.Return] = []
            for statement in statements:
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                if isinstance(statement, ast.Return):
                    found.append(statement)
                    break  # following statements in this suite are unreachable
                if isinstance(statement, ast.Raise):
                    break
                if isinstance(statement, ast.If) and isinstance(statement.test, ast.Constant):
                    found.extend(
                        own_returns(statement.body if statement.test.value else statement.orelse)
                    )
                    continue
                suites: list[list[ast.stmt]] = []
                for _, value in ast.iter_fields(statement):
                    if isinstance(value, list):
                        suites.append([child for child in value if isinstance(child, ast.stmt)])
                        for child in value:
                            if isinstance(child, ast.ExceptHandler):
                                suites.append(child.body)
                for suite in suites:
                    found.extend(own_returns(suite))
            return found

        def live_definitions(statements: list[ast.stmt]) -> tuple[ast.stmt, ...]:
            # Last direct binding wins: a later placeholder/assignment must not
            # borrow the earlier definition's implementation.
            bindings: dict[str, ast.stmt] = {}
            for statement in statements:
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bindings[statement.name] = statement
                elif isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
                    targets = (
                        statement.targets
                        if isinstance(statement, (ast.Assign, ast.Delete))
                        else [statement.target]
                    )
                    for target in targets:
                        for name in ast.walk(target):
                            if isinstance(name, ast.Name):
                                bindings[name.id] = statement
                elif isinstance(statement, (ast.Import, ast.ImportFrom)):
                    for name in statement.names:
                        bindings[name.asname or name.name.split(".")[0]] = statement
            return tuple(bindings.values())

        for source in component.source_paths:
            path = self.resolve_source(source)
            if not path.is_file():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            definitions = live_definitions(tree.body)
            functions = [(node, False) for node in definitions if isinstance(node, function_types)]
            for node in definitions:
                if isinstance(node, ast.ClassDef):
                    functions.extend(
                        (child, True)
                        for child in live_definitions(node.body)
                        if isinstance(child, function_types)
                    )
            for function, method in functions:
                if function.name != "self_evaluate":
                    continue
                decorators = {n.id for n in function.decorator_list if isinstance(n, ast.Name)}
                if decorators - {"classmethod", "staticmethod"} or any(
                    not isinstance(n, ast.Name) for n in function.decorator_list
                ):
                    continue  # a wrapper/property may not be a callable evaluation hook
                positional = function.args.posonlyargs + function.args.args
                required = len(positional) - len(function.args.defaults)
                bound = int(method and "staticmethod" not in decorators and bool(positional))
                if (
                    required > bound
                    or (method and "staticmethod" not in decorators and not positional)
                    or any(v is None for v in function.args.kw_defaults)
                ):
                    continue
                for result in own_returns(function.body):
                    value = result.value
                    if value is None or isinstance(value, ast.Constant):
                        continue
                    if isinstance(value, ast.Dict) and not value.keys:
                        continue
                    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                        continue
                    return True
        return False

    def self_evaluate(self) -> dict[str, object]:
        """Registry's own offline audit hook; never evaluates/executes candidates."""
        validations = self.validate_all()
        return {
            "component": "integration_registry",
            "components": len(validations),
            "pending": [c.component_id for c in self.list() if c.pending],
            "orphans": [c.component_id for c in self.orphans()],
            "nonresident_orphans": {
                c.component_id: self._nonresident_orphans[c.component_id]
                for c in self.orphans(include_nonresident=True)
                if self._reviewed_nonresident(c)
            },
            "reverse_dependencies": {
                c.component_id: [item.component_id for item in self.dependents(c.component_id)]
                for c in self.list()
            },
            "missing": validations,
            "scope": "declarations and source presence, not runtime acceptance",
        }


# Core exemptions are narrow and reviewable; missing hooks are NOT hidden by
# marking all legacy modules core. Non-core legacy False still fails the gate.
CORE_MODULES = {
    "think_controller": "Ten-layer immutable thinking/safety controller",
    "inner_loop": "Authoritative append-only mind and belief version schema",
    "inner_runtime": "Core cognition and epistemic state machine",
    "inner_voice": "Core inner-persona/system prompt composition",
    "daemon_wiring": "Resident product assembly and trusted adapter boundary",
    "jarvis_daemon": "Resident lifecycle and core tick state machine",
    "affect_engine": "Core persistent emotion/personality state machine",
    "affect_rules": "Core emotion safety and expression constraints",
    "dialogue_affect": "Core personality-to-dialogue boundary",
}


def capability_descriptions(root: Path | None = None) -> dict[str, str]:
    """Read trusted descriptors without importing product modules or credentials."""
    base = root or Path(__file__).parent
    result: dict[str, str] = {}
    tree = ast.parse((base / "capabilities.py").read_text(encoding="utf-8"))
    approved: list[ast.AST] = []
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in ("_REAL", "_WEB")
            and node.value is not None
        ):
            approved.append(node.value)
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "_REAL"
            and node.value.func.attr == "append"
        ):
            approved.extend(node.value.args)
    device = ast.parse((base / "device_controller.py").read_text(encoding="utf-8"))
    for cls in device.body:
        if isinstance(cls, ast.ClassDef) and cls.name == "DeviceCapability":
            for node in cls.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if (
                        any(isinstance(t, ast.Name) and t.id == "descriptor" for t in targets)
                        and node.value is not None
                    ):
                        approved.append(node.value)
    for declaration in approved:
        for node in ast.walk(declaration):
            if isinstance(node, ast.Dict):
                fields = {
                    k.value: v.value
                    for k, v in zip(node.keys, node.values)
                    if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
                }
                identity, description = fields.get("id"), fields.get("persona_description")
                if isinstance(identity, str) and isinstance(description, str):
                    # Keep the real declaration, not later synthetic fixtures.
                    result.setdefault(identity, description)
            if isinstance(node, ast.Tuple) and len(node.elts) == 6:
                first, last = node.elts[0], node.elts[-1]
                if (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and isinstance(last, ast.Constant)
                    and isinstance(last.value, str)
                    and last.value.startswith("我")
                ):
                    result[first.value] = last.value
    return result


# Orphan-admission reviews ONLY, never self_evaluate/core or module-scan
# exemptions. These sources remain registered, tested, self-evaluated and PENDING.
# No dependency is invented to turn validation/optional interfaces into residents.
NONRESIDENT_ORPHAN_WHITELIST = {
    "evidence_support": "Test evidence-directory isolation utility; its only consumers are validation suites, not resident product modules. No runtime, mind or KG integration is claimed.",
    "linux_preflight": "Explicit one-shot Linux deployment/artifact inspection CLI; it is not a resident daemon consumer. Local offline tests do not verify Linux deployment or production readiness.",
}

# Only this reviewed one-shot migration is excluded from module admission.
MIGRATION_WHITELIST = {
    "migrate_beliefs.py": "One-shot read-only-source belief migration, not a resident component"
}

# Remaining declarations below are an explicit reviewed snapshot, not discovery.

# id, kind, dependencies, implemented hook, real capability, mind categories, KG types, UI, direct tests, audit note.
_DECLARATIONS = (
    (
        "__init__",
        "FEATURE",
        (),
        True,
        None,
        (),
        (),
        None,
        (),
        "Package/PyPA path bridge only, not a standalone resident feature.",
    ),
    (
        "absorb",
        "FEATURE",
        ("absorb_formats", "absorb_memory", "chat_launcher"),
        True,
        None,
        (),
        (),
        None,
        ("test_absorb.py", "test_absorb_e2e2.py", "test_absorb_memory.py"),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "absorb_formats",
        "FEATURE",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_absorb.py", "test_absorb_formats.py"),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "absorb_memory",
        "MEMORY",
        ("absorb", "product_memory"),
        True,
        None,
        (),
        (),
        None,
        ("test_absorb_memory.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "affect_engine",
        "PERSONA",
        ("affect_rules", "context_limits"),
        False,
        None,
        (),
        (),
        None,
        (
            "test_affect_engine.py",
            "test_avatar_signals.py",
            "test_avatar_stream.py",
            "test_chat_entry.py",
            "test_dialogue_affect.py",
        ),
        "Emotion state lives in affect.db, not mind.db; no KG projection or independent evolution hook.",
    ),
    (
        "affect_rules",
        "PERSONA",
        (),
        False,
        None,
        (),
        (),
        None,
        ("test_absorb_memory.py", "test_affect_engine.py"),
        "Core expression guards are consumed by affect/chat; no mind/KG output.",
    ),
    (
        "agent_protocol",
        "GOAL",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_agent_protocol.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "amap",
        "EXTERNAL",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_amap.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "avatar_signals",
        "UI",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_avatar_self_evaluate.py", "test_avatar_signals.py"),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "avatar_stream",
        "UI",
        ("affect_engine", "avatar_signals"),
        True,
        None,
        (),
        (),
        None,
        ("test_avatar_self_evaluate.py", "test_avatar_stream.py"),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "bootstrap",
        "FEATURE",
        ("jarvis",),
        True,
        None,
        (),
        (),
        None,
        ("test_bootstrap.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "capabilities",
        "FEATURE",
        ("amap", "citations", "device_controller", "search", "senses.api"),
        True,
        None,
        (),
        (),
        None,
        (
            "test_amap.py",
            "test_bounded_runtime.py",
            "test_curiosity.py",
            "test_inner_loop.py",
            "test_voi_requests.py",
        ),
        "Catalog has truthful existing first-person IDs, but new local modules lack descriptors; no universal integration check in product runtime.",
    ),
    (
        "chat_entry",
        "UI",
        (
            "affect_engine",
            "affect_rules",
            "context_limits",
            "dialogue_affect",
            "engine_entry",
            "inner_voice",
            "product_config",
            "product_home",
            "product_memory",
            "stream_response",
        ),
        True,
        None,
        (),
        (),
        "stdin:prompt",
        ("test_absorb_memory.py", "test_chat_entry.py", "test_ui_ops_self_evaluate.py"),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "chat_launcher",
        "UI",
        ("chat_entry", "context_limits", "product_config"),
        True,
        None,
        (),
        (),
        None,
        (
            "test_absorb_e2e2.py",
            "test_absorb_wire_e2e2.py",
            "test_bounded_wiring.py",
            "test_chat_launcher.py",
            "test_jarvis_daemon.py",
            "test_product_home.py",
            "test_ui_ops_self_evaluate.py",
            "test_unknown_gate.py",
        ),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "citations",
        "KNOWLEDGE",
        (),
        True,
        "citation_lookup",
        (),
        (),
        None,
        ("test_citations.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "code_search",
        "TOOL",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_code_search.py",),
        "ToolRouter adapters exist but no resident model tool-call execution loop.",
    ),
    (
        "config_launcher",
        "FEATURE",
        ("context_limits", "product_config", "product_home"),
        True,
        None,
        (),
        (),
        None,
        ("test_config_launcher.py", "test_config_launcher_evaluation.py"),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "context_limits",
        "FEATURE",
        (),
        True,
        None,
        (),
        (),
        None,
        (
            "test_affect_engine.py",
            "test_chat_entry.py",
            "test_chat_launcher.py",
            "test_config_launcher.py",
            "test_curiosity.py",
            "test_jarvis_daemon.py",
            "test_ops_self_evaluation.py",
            "test_web_server.py",
        ),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "critic_agent",
        "EVOLUTION",
        ("inner_model",),
        True,
        None,
        (),
        (),
        None,
        ("test_critic_agent.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "inner_loop_fts",
        "MEMORY",
        ("inner_loop",),
        True,
        None,
        (),
        (),
        None,
        ("test_curiosity.py",),
        "Derived disposable FTS5/bigram/profile-topic/entity recall index used by curiosity.context for task-directed RRF recall; reads mind entries via inner_loop and sir_profile.db/memory.db read-only by path (no import); rebuildable from source ledgers, writes no mind entries or KG nodes. Offline frozen-replay recall only; live daemon acceptance pending.",
    ),
    (
        "curiosity",
        "MEMORY",
        ("inner_loop", "inner_loop_fts", "product_memory"),
        True,
        None,
        (
            "recall",
            "knowledge_gap",
            "gap_retired",
            "belief_origin",
            "relevance_cache",
            "question_state",
            "conflict",
        ),
        (),
        None,
        ("test_bounded_runtime.py", "test_curiosity.py", "test_voi_requests.py"),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection and self-evaluation are not fully connected.",
    ),
    (
        "daemon_wiring",
        "FEATURE",
        (
            "affect_engine",
            "capabilities",
            "chat_launcher",
            "context_limits",
            "dialogue_affect",
            "evolution_scheduler",
            "goal_tracker",
            "health_scheduler",
            "hypothesis_engine",
            "inner_loop",
            "inner_model",
            "inner_runtime",
            "inner_voice",
            "jarvis_daemon",
            "jarvis_socket",
            "kg_projector",
            "knowledge_graph",
            "memory_distiller",
            "model_registry",
            "outreach",
            "reflection",
            "self_evolution",
            "senses.api",
            "session_resume",
            "sir_profile",
            "skill_distiller",
            "skill_loader",
            "supervisor_components",
            "think_controller",
            "tool_router",
        ),
        False,
        None,
        ("event", "daemon_sense_merged", "daemon_sense_suspended", "daemon_sense_consumed"),
        (),
        None,
        (
            "test_bounded_wiring.py",
            "test_jarvis_daemon.py",
            "test_outreach_acceptance.py",
            "test_outreach_e2e.py",
            "test_soak_unknown.py",
            "test_supervisor.py",
            "test_unknown_gate.py",
        ),
        "Non-fixture _wire_integrations constructs ToolRouter, GoalTracker, SessionResume, SelfEvolution, EvolutionScheduler, HealthScheduler and KGProjector; background ticks schedulers/projector and close_integrations closes health. Construction failures are logged; this ledger does not verify the running daemon, real tool calls, notification receipts or complete runtime integration.",
    ),
    (
        "device_controller",
        "TOOL",
        (),
        True,
        "device_control",
        (),
        (),
        None,
        (),
        "Capability dispatcher exists; no production device registration or real device delivery verified.",
    ),
    (
        "device_routing",
        "SENSE",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_device_routing.py", "test_voice_registry_web.py"),
        "Pure device-snapshot arbitration/output policy consumed by voice_xiaoai; "
        "no telemetry collection, winner lease enforcement, first-person capability, "
        "mind writes or KG projection. Resident routing and physical playback remain unverified.",
    ),
    (
        "dialogue_affect",
        "PERSONA",
        ("affect_engine", "reflection"),
        False,
        None,
        (),
        (),
        None,
        ("test_absorb_memory.py", "test_dialogue_affect.py"),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection and self-evaluation are not fully connected.",
    ),
    (
        "engine_entry",
        "FEATURE",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_engine_entry.py", "test_engine_entry_evaluation.py"),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "evidence_support",
        "OBSERVABILITY",
        (),
        True,
        None,
        (),
        (),
        None,
        (
            "test_avatar_e2e.py",
            "test_chat_launcher.py",
            "test_config_launcher.py",
            "test_evidence_isolation.py",
            "test_evidence_support.py",
            "test_jarvis_cli.py",
            "test_launcher.py",
            "test_web_server.py",
        ),
        "Test-only evidence-directory utility, with no production consumer or runtime dependency. Offline self-evaluation does not add resident mind/KG/runtime integration.",
    ),
    (
        "evolution_scheduler",
        "EVOLUTION",
        ("inner_loop", "outreach", "self_evolution"),
        True,
        None,
        ("self_evaluation", "evolution_cycle"),
        (),
        None,
        ("test_evolution_scheduler.py",),
        "Proposal-only weekly scheduling is constructed by daemon_wiring and ticked from background; enqueue is not receipt. Resident execution, complete module evaluation coverage and real notification delivery remain unverified.",
    ),
    (
        "file_ops",
        "TOOL",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_file_ops.py",),
        "ToolRouter has adapters but no resident model tool-call execution loop.",
    ),
    (
        "file_task_chain",
        "TOOL",
        ("task_api",),
        True,
        None,
        (),
        (),
        None,
        ("test_file_task_chain.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "git_ops",
        "TOOL",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_git_ops.py",),
        "ToolRouter adapters exist but no resident model tool-call execution loop.",
    ),
    (
        "goal_tracker",
        "GOAL",
        ("inner_loop", "sir_profile"),
        True,
        None,
        ("goal_set", "goal_progress", "goal_completed"),
        (),
        "socket:query=goals",
        ("test_goal_tracker.py",),
        "daemon_wiring constructs GoalTracker and explicit audited tools/read-only /goals exist. Task execution does not automatically provide verified progress updates; running goal lifecycle remains unverified.",
    ),
    (
        "health_alert",
        "SENSE",
        ("inner_loop", "knowledge_graph"),
        True,
        None,
        ("health_alert",),
        ("health_event",),
        None,
        ("test_health_alert.py", "test_health_scheduler.py"),
        "HealthScheduler/HealthAlertManager are constructed by daemon_wiring; health_event projection and critical mind writes exist. Live measurements, duplicate recovery across stores and actual channel dispatch remain unverified.",
    ),
    (
        "health_analyzer",
        "SENSE",
        (),
        True,
        "health_monitoring",
        (),
        (),
        None,
        ("test_health_scheduler.py",),
        "HealthScheduler with HealthAnalyzer is constructed by daemon_wiring and ticked in background; health_monitoring capability still polls metadata. Real source measurements and end-to-end health delivery remain unverified.",
    ),
    (
        "health_rules",
        "SENSE",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_health_rules.py", "test_health_scheduler.py"),
        "HealthScheduler consumes HealthRules in daemon_wiring construction; background ticks are present. Non-diagnostic threshold rules are not real health validation or delivered notifications.",
    ),
    (
        "health_scheduler",
        "SENSE",
        (
            "health_alert",
            "health_analyzer",
            "health_rules",
            "inner_loop",
            "knowledge_graph",
            "outreach",
            "product_home",
        ),
        True,
        "health_monitoring",
        ("health_daily_report",),
        ("behavior_pattern", "health_event"),
        None,
        ("test_health_scheduler.py",),
        "daemon_wiring constructs HealthScheduler with real adapters, ticks it from background, and closes its worker on shutdown. Enqueue is not notification delivery; live health ingestion, running-daemon state and actual receipts remain unverified.",
    ),
    (
        "hypothesis_engine",
        "MEMORY",
        ("knowledge_graph", "sir_profile"),
        True,
        None,
        (),
        ("hypothesis",),
        "socket:query=memory",
        ("test_hypothesis_engine.py", "test_knowledge_graph.py", "test_knowledge_graph_wal.py"),
        "Hypotheses use hypotheses.db; no resident evidence/update/confirm/expiry lifecycle observed.",
    ),
    (
        "inner_loop",
        "MEMORY",
        ("capabilities", "inner_runtime", "product_home"),
        True,
        None,
        ("event", "belief_score"),
        (),
        "socket:query=beliefs",
        (
            "test_avatar_stream.py",
            "test_bounded_history.py",
            "test_bounded_runtime.py",
            "test_curiosity.py",
            "test_evolution_scheduler.py",
            "test_health_scheduler.py",
            "test_inner_loop.py",
            "test_voi_requests.py",
        ),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection and self-evaluation are not fully connected.",
    ),
    (
        "inner_model",
        "MODEL",
        ("chat_launcher", "context_limits"),
        True,
        None,
        (),
        (),
        None,
        ("test_chat_entry.py", "test_inner_model.py", "test_jarvis_daemon.py"),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "inner_runtime",
        "FEATURE",
        (
            "capabilities",
            "chat_launcher",
            "context_limits",
            "curiosity",
            "inner_loop",
            "inner_model",
            "inner_voice",
            "product_memory",
        ),
        False,
        None,
        (
            "question_state",
            "action_request",
            "context_scan",
            "model_call",
            "model_response",
            "question_generation_intent",
            "action",
            "event",
            "context_error",
            "question_rejected",
            "action_blocked",
            "outreach_reconciled",
            "thought",
            "model_error",
            "question_generation_failure",
            "action_error",
            "validation_error",
            "thought_suppressed",
            "outreach_intent",
        ),
        (),
        "socket:query=24thoughts",
        (
            "test_bounded_runtime.py",
            "test_curiosity.py",
            "test_inner_loop.py",
            "test_outreach_e2e.py",
            "test_voi_requests.py",
        ),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection and self-evaluation are not fully connected.",
    ),
    (
        "inner_voice",
        "PERSONA",
        ("capabilities", "inner_loop", "skill_loader"),
        False,
        None,
        (),
        (),
        None,
        (
            "test_chat_entry.py",
            "test_chat_launcher.py",
            "test_curiosity.py",
            "test_jarvis_daemon.py",
        ),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection and self-evaluation are not fully connected.",
    ),
    (
        "jarvis",
        "UI",
        ("file_task_chain", "product_config", "product_home", "task_api"),
        True,
        None,
        (),
        (),
        "CLI:jarvis",
        ("test_jarvis_cli.py", "test_jarvis_evaluation.py"),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "jarvis_daemon",
        "FEATURE",
        ("context_limits", "daemon_wiring"),
        False,
        None,
        (),
        (),
        None,
        (
            "test_bounded_wiring.py",
            "test_jarvis_daemon.py",
            "test_outreach_e2e.py",
            "test_soak_unknown.py",
        ),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection and self-evaluation are not fully connected.",
    ),
    (
        "jarvis_socket",
        "UI",
        ("chat_entry", "stream_response"),
        True,
        None,
        (),
        (),
        "socket:query",
        ("test_ui_ops_self_evaluate.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "jarvis_ui",
        "UI",
        ("jarvis_socket",),
        True,
        None,
        (),
        (),
        "slash:/status",
        ("test_ui_ops_self_evaluate.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "knowledge_graph",
        "KNOWLEDGE",
        ("hypothesis_engine", "sir_profile"),
        True,
        None,
        (),
        ("fact", "hypothesis", "observation", "health_event", "behavior_pattern"),
        "socket:query=memory",
        ("test_health_scheduler.py", "test_knowledge_graph.py", "test_knowledge_graph_wal.py"),
        "Profile/hypothesis projection and daemon-owned KGProjector construction/background ticks exist. Projector is an incoming graph writer, not an import made by knowledge_graph; running projection, complete category coverage and bounded maintenance/retrieval remain unverified.",
    ),
    (
        "library_evaluator",
        "EVOLUTION",
        ("citations", "context_limits", "inner_model", "product_config", "search"),
        True,
        None,
        (),
        (),
        None,
        ("test_library_evaluator.py",),
        "Standalone evaluator has no product instance/injection/caller; no runtime optimization loop.",
    ),
    (
        "linux_preflight",
        "OBSERVABILITY",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_linux_preflight.py", "test_linux_preflight_evaluation.py"),
        "Explicit one-shot deployment/environment/artifact inspection CLI; no resident consumer or runtime dependency. Offline evaluation is not Linux deployment, production readiness or verified server operation.",
    ),
    (
        "mcp_bridge",
        "TOOL",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_mcp_bridge.py",),
        "MCP adapter exists but no resident tool-call loop or verified configured server connection.",
    ),
    (
        "memory_distiller",
        "MEMORY",
        (
            "agent_protocol",
            "context_limits",
            "hypothesis_engine",
            "model_registry",
            "sir_profile",
            "task_scheduler",
        ),
        True,
        None,
        (),
        (),
        None,
        ("test_memory_distiller.py",),
        "Background distillation is present; transactional pattern upsert does not project to KG; no direct mind entry output.",
    ),
    (
        "model_registry",
        "MODEL",
        (),
        True,
        "model_search",
        (),
        (),
        None,
        ("test_model_registry.py",),
        "model_search descriptor exists but its executor only searches/cites; it does not update ModelRegistry.",
    ),
    (
        "outreach",
        "NOTIFY",
        (),
        True,
        None,
        (),
        (),
        "HTTP:SSE",
        ("test_evolution_scheduler.py", "test_outreach.py", "test_outreach_intents.py"),
        "Durable queue/receipts use outreach.db; no direct mind/KG projection or resident evaluation delivery; an owned offline self_evaluate hook exists.",
    ),
    (
        "product_config",
        "EXTERNAL",
        (),
        True,
        None,
        (),
        (),
        None,
        (
            "test_config_launcher.py",
            "test_jarvis_cli.py",
            "test_product_config.py",
            "test_task_api.py",
        ),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "product_home",
        "FEATURE",
        (),
        True,
        None,
        (),
        (),
        None,
        (
            "test_evolution_scheduler.py",
            "test_health_scheduler.py",
            "test_jarvis_cli.py",
            "test_product_home.py",
            "test_product_home_evaluation.py",
        ),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "product_memory",
        "MEMORY",
        (),
        True,
        None,
        (),
        (),
        None,
        (
            "test_absorb_e2e2.py",
            "test_absorb_memory.py",
            "test_absorb_wire_e2e2.py",
            "test_dialogue_affect.py",
            "test_inner_loop.py",
            "test_product_memory.py",
        ),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "reflection",
        "PERSONA",
        ("chat_launcher",),
        True,
        None,
        (),
        (),
        None,
        ("test_dialogue_affect.py", "test_reflection.py"),
        "Reflections/lessons persist in reflect.db, not mind.db; no unified KG projection.",
    ),
    (
        "search",
        "TOOL",
        (),
        True,
        "web_search",
        (),
        (),
        None,
        ("test_search.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "self_evolution",
        "EVOLUTION",
        (
            "capabilities",
            "context_limits",
            "inner_loop",
            "search",
            "skill_loader",
            "skill_manifest",
        ),
        True,
        None,
        (
            "evolution_research_error",
            "evolution_search",
            "evolution_review",
            "evolution_research_session",
            "evolution_notification_queued",
            "evolution_proposal_approval",
            "evolution_proposal_processed",
            "evolution_weekly_completed",
            "evolution_proposal",
        ),
        (),
        "socket:query=evolution",
        ("test_evolution_scheduler.py", "test_self_evolution.py"),
        "daemon_wiring constructs SelfEvolution and EvolutionScheduler and background ticks weekly scheduling. Proposal-only research writes mind entries; running execution, proposal approval/activation and real delivered receipts remain unverified.",
    ),
    (
        "session_resume",
        "GOAL",
        ("inner_loop", "product_home"),
        True,
        None,
        (),
        (),
        "socket:query=sessions",
        ("test_session_resume.py",),
        "daemon_wiring constructs SessionResume and explicit tools/read-only /sessions exist. Construction is not automatic checkpoint/resume scheduling or verified execution continuity.",
    ),
    (
        "shell_executor",
        "TOOL",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_shell_executor.py",),
        "ToolRouter adapters exist but no resident model tool-call execution loop; argv execution is not an OS sandbox.",
    ),
    (
        "sir_profile",
        "MEMORY",
        ("knowledge_graph", "product_home"),
        True,
        None,
        (),
        ("fact",),
        "socket:query=memory",
        ("test_knowledge_graph.py", "test_knowledge_graph_wal.py", "test_sir_profile.py"),
        "SirProfile uses sir_profile.db, not mind.db; automatic self-report extraction/think profile consumption still unverified.",
    ),
    (
        "skill_distiller",
        "SKILL",
        ("context_limits", "memory_distiller", "skill_loader", "skill_manifest", "skill_registry"),
        True,
        None,
        (),
        (),
        None,
        ("test_skill_distiller.py",),
        "Task completion hook exists, but generated skills remain inert PENDING/inactive proposals; approval/implementation/activation is not closed.",
    ),
    (
        "skill_loader",
        "SKILL",
        ("search", "skill_manifest", "skill_registry"),
        True,
        None,
        (),
        (),
        None,
        ("test_skill_loader.py",),
        "Prompt skills are loaded; task match/execute and LibraryEvaluator injection are not observed; Wiring context API does not match loader API.",
    ),
    (
        "skill_manifest",
        "SKILL",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_skill_manifest.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "skill_registry",
        "SKILL",
        ("skill_manifest",),
        True,
        None,
        (),
        (),
        "socket:query=skills",
        ("test_skill_registry.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "stream_response",
        "UI",
        ("context_limits",),
        True,
        None,
        (),
        (),
        "socket:stream_chat",
        ("test_ui_ops_self_evaluate.py",),
        "Streaming protocol/UI path exists, but daemon socket construction has no stream_handler/config; defaults to stream_chat_unavailable.",
    ),
    (
        "supervisor",
        "OBSERVABILITY",
        ("supervisor_components",),
        True,
        None,
        (),
        (),
        None,
        ("test_jarvis_daemon.py", "test_supervisor.py", "test_supervisor_evaluation.py"),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "supervisor_components",
        "OBSERVABILITY",
        ("daemon_wiring",),
        True,
        None,
        (),
        (),
        None,
        ("test_supervisor.py", "test_supervisor_components_evaluation.py"),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "switch_model",
        "TOOL",
        ("product_config",),
        True,
        None,
        (),
        (),
        None,
        ("test_switch_model.py",),
        "One-shot engine-home migration CLI: dry-run by default, takes the existing "
        "launcher-session lock, refuses in-flight attempts and symbol links, backs up and "
        "verifies by read-back. It has no resident caller and never switches a live model "
        "by itself; the integration registry entry is declaration only.",
    ),
    (
        "task_api",
        "GOAL",
        ("product_config", "product_home"),
        True,
        None,
        (),
        (),
        None,
        ("test_task_api.py",),
        "Fixture-only candidate task ledger/CLI exists, not an approved execution kernel or TaskScheduler integration.",
    ),
    (
        "task_executor",
        "GOAL",
        ("chat_launcher", "context_limits", "model_registry"),
        True,
        None,
        (),
        (),
        None,
        ("test_task_executor.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "task_scheduler",
        "GOAL",
        ("agent_protocol", "critic_agent", "model_registry", "product_config", "task_executor"),
        True,
        None,
        (),
        (),
        None,
        ("test_task_scheduler.py",),
        "TaskScheduler has no daemon/inbox driver; existing daemon task hooks are a different scheduler.",
    ),
    (
        "think_controller",
        "FEATURE",
        ("inner_loop", "knowledge_graph", "model_registry", "sir_profile", "task_executor"),
        False,
        None,
        (),
        (),
        None,
        (),
        "Wiring constructs controller, but route/execute/feedback are not invoked by the resident cognition/chat loop; answer cache is not mind.db.",
    ),
    (
        "tool_router",
        "TOOL",
        (
            "code_search",
            "file_ops",
            "git_ops",
            "goal_tracker",
            "inner_loop",
            "mcp_bridge",
            "product_home",
            "session_resume",
            "shell_executor",
            "vision_input",
        ),
        True,
        None,
        ("event",),
        (),
        None,
        ("test_tool_router.py",),
        "daemon_wiring constructs ToolRouter, and audited adapters write execution events. Construction is not a resident model tool-call loop or end-to-end execution receipt; argv root restrictions are not an OS sandbox.",
    ),
    (
        "verify_avatar_web",
        "OBSERVABILITY",
        ("affect_engine", "inner_loop", "web_server"),
        True,
        None,
        (),
        (),
        None,
        ("test_ui_ops_self_evaluate.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "vision",
        "SENSE",
        ("product_config",),
        True,
        None,
        (),
        (),
        None,
        ("test_vision.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "vision_input",
        "SENSE",
        ("chat_entry", "inner_loop", "vision"),
        True,
        None,
        ("vision_input", "event"),
        (),
        None,
        ("test_vision_input.py",),
        "Image storage and mind event writes exist; resident tool-call loop and KG projection not connected.",
    ),
    (
        "voice",
        "SENSE",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_avatar_signals.py", "test_voice.py", "test_voice_live.py"),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "voice_dashscope",
        "EXTERNAL",
        ("voice_stream_tts",),
        True,
        None,
        (),
        (),
        None,
        ("test_voice_dashscope.py", "test_voice_registry_web.py"),
        "Consumed by voice_gateway's async off-loop Bailian adapter, including ASR emotion and "
        "bounded MP3 synthesis. No resident service, capability or KG projection is claimed; the "
        "owned offline self_evaluate reports declared limits only, never a live request.",
    ),
    (
        "voice_gateway",
        "SENSE",
        (
            "chat_launcher",
            "device_routing",
            "inner_loop",
            "voice_dashscope",
            "voice_ledger",
            "voice_live",
            "voice_port",
            "voice_speaker",
            "voice_volcengine",
        ),
        True,
        None,
        ("voice_user", "voice_assistant", "voice_playback"),
        (),
        "WebSocket:127.0.0.1:8765,100.67.229.1:8765",
        (
            "test_voice_gateway.py",
            "test_voice_gateway_integration.py",
            "test_voice_registry_web.py",
        ),
        "Authenticated explicit-private-address WebSocket CLI uses voice_port BRAIN sink, existing "
        "launcher/timeline, emotion-aware voice_dashscope and device_routing arbitration. "
        "VoiceLedger writes transcript/answer/TTS/playback/interrupt observations. "
        "No KG projection or first-person "
        "capability is claimed; resident startup and physical delivery remain unverified.",
    ),
    (
        "voice_ledger",
        "MEMORY",
        ("inner_loop",),
        True,
        None,
        ("voice_user", "voice_assistant", "voice_playback", "voice_interrupt"),
        (),
        None,
        ("test_voice_ledger.py", "test_voice_registry_web.py"),
        "Append-only voice observations write the existing MindStore entries; Web reads the "
        "same ledger and records playback observations. Caller metadata does not verify physical "
        "playback; gateway callback coverage, first-person capability, KG projection and resident "
        "delivery remain unverified.",
    ),
    (
        "voice_live",
        "SENSE",
        ("voice",),
        True,
        None,
        (),
        (),
        None,
        ("test_voice_live.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "voice_port",
        "SENSE",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_voice_port.py",),
        "Duplex protocol and BRAIN sink are consumed by voice_gateway. This dependency does not "
        "verify resident startup, microphone/speaker access, physical playback, first-person "
        "capability or mind/KG projection.",
    ),
    (
        "voice_speaker",
        "SENSE",
        (),
        True,
        None,
        (),
        (),
        None,
        (),
        "Owner speaker verification (sherpa-onnx CAM++ embedding, one enrolled reference) "
        "consumed by voice_gateway before a transcribed turn is attributed to Sir. Model and "
        "reference files stay host-local and a missing file yields 'cannot tell', never a "
        "false reject. The threshold is one-session evidence, not a calibrated "
        "false-accept rate; no mind provenance, KG projection or resident caller exists, "
        "and no dedicated test file covers it yet.",
    ),
    (
        "voice_stream_tts",
        "EXTERNAL",
        (),
        True,
        None,
        (),
        (),
        None,
        ("test_voice_stream_tts.py",),
        "Bailian CosyVoice duplex WebSocket adapter: incremental text in, raw PCM16 chunks "
        "out, cancellable, exposed as voice_dashscope.tts_stream_pcm(). No gateway caller "
        "yet (devices still take the REST MP3 path), so streaming playback is not claimed.",
    ),
    (
        "voice_volcengine",
        "EXTERNAL",
        (),
        True,
        None,
        (),
        (),
        None,
        (),
        "Volcengine seed-tts-2.0 bidirectional streaming MP3 TTS adapter consumed by "
        "voice_gateway as an alternative synthesis backend. Latency figures come from a "
        "single session, only MP3 chunks are returned (no duplex PCM path) and the voice id "
        "is a fixed enrollment. No resident caller, mind provenance, KG projection or "
        "dedicated test file exists yet.",
    ),
    (
        "voice_xiaoai",
        "EXTERNAL",
        ("device_routing",),
        True,
        None,
        (),
        (),
        None,
        ("test_voice_xiaoai.py", "test_voice_registry_web.py"),
        "Guarded single-LX05 cloud TTS uses device_routing's fresh home/room policy. Cloud "
        "submission is not heard confirmation; no resident caller, microphone, custom voice, "
        "mind/KG projection or first-person capability is claimed. The owned offline "
        "self_evaluate reports declared limits only.",
    ),
    (
        "web_server",
        "UI",
        (
            "avatar_stream",
            "chat_launcher",
            "context_limits",
            "product_memory",
            "voice_gateway",
            "voice_ledger",
            "web_tasks",
        ),
        True,
        None,
        (),
        (),
        "HTTP:loopback",
        (
            "test_avatar_stream.py",
            "test_voice_live.py",
            "test_voice_registry_web.py",
            "test_web_server.py",
            "test_web_tasks.py",
            "test_web_voice.py",
        ),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "web_tasks",
        "GOAL",
        ("file_task_chain",),
        True,
        None,
        (),
        (),
        None,
        ("test_web_tasks.py",),
        "Existing product adapter/consumer exists; unified first-person capability, mind provenance, KG projection are not fully connected; an owned offline self_evaluate hook exists but is not runtime acceptance.",
    ),
    (
        "integration_registry",
        "OBSERVABILITY",
        ("capabilities",),
        True,
        None,
        (),
        (),
        None,
        ("test_integration_contract.py",),
        "Offline admission ledger only; no runtime persistence or resident evolution scheduler integration.",
    ),
    (
        "senses.api",
        "SENSE",
        (
            "__init__",
            "senses.base",
            "senses.health",
            "senses.location",
            "senses.activitywatch",
            "senses.browser",
            "senses.speech",
        ),
        True,
        None,
        (),
        (),
        None,
        ("senses/test_senses.py",),
        "Poll emits metadata to caller; daemon owns mind event ingestion. No source-level mind/KG or resident evaluation delivery; an owned offline hook exists.",
    ),
    (
        "senses.base",
        "SENSE",
        (),
        True,
        None,
        (),
        (),
        None,
        ("senses/test_senses.py",),
        "Source/cursor abstractions and private senses_cursor.db; not mind.db or KG.",
    ),
    (
        "senses.health",
        "SENSE",
        ("senses.base",),
        True,
        "health_record",
        (),
        (),
        None,
        ("senses/test_senses.py",),
        "Read-only health metadata source; daemon owns event ingestion; metadata is not measurements/health analytics.",
    ),
    (
        "senses.location",
        "SENSE",
        ("senses.base",),
        True,
        "location_track",
        (),
        (),
        None,
        ("senses/test_senses.py",),
        "Read-only location source; daemon owns event ingestion; no source-level graph projection.",
    ),
    (
        "senses.activitywatch",
        "SENSE",
        ("senses.base",),
        True,
        "activity_usage",
        (),
        (),
        None,
        ("senses/test_senses.py",),
        "Read-only activity metadata source; daemon owns event ingestion; no source-level graph projection.",
    ),
    (
        "senses.browser",
        "SENSE",
        ("senses.base",),
        True,
        None,
        (),
        (),
        None,
        ("senses/test_senses.py",),
        "Browser metadata source participates in polling; lacks trusted first-person capability, mind/KG output and resident evaluation delivery; an owned offline self_evaluate hook exists.",
    ),
    (
        "senses.speech",
        "SENSE",
        ("senses.base",),
        True,
        "speech_record",
        (),
        (),
        None,
        ("senses/test_senses.py",),
        "Read-only speech metadata source; daemon owns event ingestion; no source-level graph projection.",
    ),
    (
        "imessage_input",
        "SENSE",
        ("daemon_wiring",),
        True,
        None,
        (),
        (),
        None,
        ("test_imessage_input.py", "test_jarvis_daemon.py"),
        "Owned offline evaluation lives in imessage_input.py; actual mailbox/inbox ingress remains daemon_wiring._poll_imessage. The documented resource-owner dependency is not a Python import or runtime migration; this module itself does not write mind/KG, and real mailbox permissions/delivery remain unverified.",
    ),
    (
        "mcp_servers",
        "EXTERNAL",
        ("mcp_bridge",),
        True,
        None,
        (),
        (),
        None,
        ("test_integration_contract.py",),
        "Configuration boundary in mcp_bridge; explicit server registration does not prove configured server delivery or model access.",
    ),
    (
        "api_credentials",
        "EXTERNAL",
        ("product_config",),
        True,
        None,
        (),
        (),
        None,
        ("test_api_credentials.py", "test_integration_contract.py", "test_product_config.py"),
        "Credential boundary in product_config; secrets must never be mirrored into mind or KG. Audit non-secret metadata only, not yet connected.",
    ),
    (
        "mind.entries",
        "MEMORY",
        ("inner_loop",),
        True,
        None,
        ("event", "belief_score"),
        (),
        "socket:query=status",
        ("test_inner_loop.py", "test_integration_contract.py"),
        "Authoritative entries in mind.db; KG fusion from entries is not universally connected.",
    ),
    (
        "mind.belief_versions",
        "MEMORY",
        ("inner_loop",),
        True,
        None,
        (),
        (),
        "socket:query=beliefs",
        ("test_inner_loop.py", "test_integration_contract.py"),
        "belief_versions in mind.db is versioned belief state, not an entries category; no direct KG projection.",
    ),
    (
        "kg_projector",
        "KNOWLEDGE",
        ("inner_loop", "knowledge_graph"),
        True,
        None,
        (),
        ("observation", "health_event"),
        None,
        ("test_kg_projector.py",),
        "daemon_wiring constructs KGProjector and background ticks its bounded MindStore-to-KG projection. Recorded intentions/assessments remain observations, not verified world facts; unmapped categories require explicit policy and acknowledged cursors require explicit replay. Running-daemon projection and exhaustive historical coverage are not verified.",
    ),
    (
        "replay_quota",
        "MEMORY",
        ("curiosity", "inner_runtime"),
        True,
        None,
        (),
        (),
        None,
        ("test_replay_quota.py", "test_recalled_aliases.py"),
        (
            "Pure fixed-quota request projection called by bound_context when two recalled "
            "rows fit; the baseline bounded allocator is the fallback for an indivisible "
            "floor. It writes no mind/KG row and calls no model. Offline paired evaluation "
            "with the current-round validator only: running-daemon effect, live delivery "
            "and semantic evidence truth remain unverified."
        ),
    ),
    (
        "context_metrics",
        "OBSERVABILITY",
        ("curiosity", "inner_runtime", "inner_voice"),
        True,
        None,
        (),
        (),
        None,
        ("test_context_metrics.py",),
        "Offline caller-supplied context/response observer uses inner_voice system and inner_runtime/curiosity validators. It does not observe live transport/cache receipts, commit mind/KG, start the daemon or prove semantic evidence truth; all resident runtime acceptance remains unverified.",
    ),
    (
        "replay_harness",
        "OBSERVABILITY",
        ("curiosity", "inner_loop", "knowledge_graph"),
        True,
        None,
        (),
        (),
        None,
        ("test_replay_harness.py",),
        "Offline frozen-replay evaluator over copied private snapshots; runs existing curiosity.context() and KG query as baseline. Not a resident daemon component; writes no live mind/KG and sends no model request. Scores are ledger-assertion recall, not answer correctness.",
    ),
    (
        "build_replay_set",
        "OBSERVABILITY",
        ("replay_harness",),
        True,
        None,
        (),
        (),
        None,
        ("test_replay_harness.py",),
        "One-shot deterministic replay-set builder from private backups with as_of filtering; no model question generation. Labels need independent human review; not resident runtime integration.",
    ),
)

_RESOURCE_SOURCES = {
    "mcp_servers": ("mcp_bridge.py",),
    "api_credentials": ("product_config.py",),
    "mind.entries": ("inner_loop.py",),
    "mind.belief_versions": ("inner_loop.py",),
}
_TEST_OVERRIDES = {
    "evolution_scheduler": ("test_evolution_scheduler.py",),
    "reflection": ("test_reflection.py", "test_dialogue_affect.py"),
    "avatar_stream": ("test_avatar_stream.py",),
    "vision": ("test_vision.py",),
    "file_task_chain": ("test_file_task_chain.py", "test_semantic_file_chain.py"),
    "web_tasks": ("test_web_tasks.py",),
    "absorb_memory": ("test_absorb_memory.py", "test_absorb_wire_e2e2.py"),
    "product_home": ("test_product_home.py",),
    "product_config": ("test_product_config.py",),
    "tool_router": ("tests/test_tool_router_real.py",),
    "file_ops": ("tests/test_tool_router_real.py",),
}


def build_registry() -> IntegrationRegistry:
    registry = IntegrationRegistry(nonresident_orphans=NONRESIDENT_ORPHAN_WHITELIST)
    for (
        identity,
        kind,
        dependencies,
        evaluate,
        capability,
        categories,
        nodes,
        ui,
        tests,
        note,
    ) in _DECLARATIONS:
        fields = []
        if capability is None:
            fields.append("capability_id")
        if not categories:
            fields.append("writes_mind_category")
        if not nodes:
            fields.append("kg_node_types")
        tests = tuple(sorted(set(tests + _TEST_OVERRIDES.get(identity, ()))))
        if not tests:
            fields.append("tests")
        core = identity in CORE_MODULES
        if not evaluate and not core:
            fields.append("self_evaluate")
        if not dependencies and not any(identity in row[2] for row in _DECLARATIONS):
            fields.append("reads_from")
        # 每个组件无条件带 runtime_wiring 缺口：注册表是「谁依赖谁」的台账，
        # 不是「做完没有」的完成表。任何"注册表已登记=已接线"的解读都是错的。
        # 真正的完成判据看对应测试与 docs/reports/REVIEW_*.md 的证据。
        fields.append("runtime_wiring")
        sources = _RESOURCE_SOURCES.get(identity, (identity.replace(".", "/") + ".py",))
        if identity in VOICE_MODULES:
            # 语音子系统已隔离到仓库根 voice/，模块不在 packaging/ 下。
            sources = (f"voice/{identity}.py",)
        # 测试文件同理：在 packaging/ 找不到、但在 voice/tests/ 里有的，改记带前缀的路径。
        # 这样引用语音测试的非语音组件（device_routing、web_server）也能校验通过。
        packaging_root = Path(__file__).parent
        repo_root = packaging_root.parent
        tests = tuple(
            f"voice/tests/{name}"
            if not (packaging_root / name).is_file()
            and (repo_root / "voice" / "tests" / name).is_file()
            else name
            for name in tests
        )
        registry.register(
            identity,
            ComponentKind[kind],
            WiringContract(
                capability_id=capability,
                writes_mind_category=categories,
                kg_node_types=nodes,
                reads_from=dependencies,
                self_evaluate=evaluate,
                ui_entry=ui,
                tests=tests,
                ui_entry_reason=""
                if ui
                else "Internal adapter/library or no dedicated public UI; see pending_reason for runtime gaps.",
                pending=True,
                pending_reason=note,
                pending_fields=tuple(fields),
            ),
            source_paths=sources,
            core=core,
            core_reason=CORE_MODULES.get(identity, ""),
        )
    return registry


REGISTRY = build_registry()
registry = REGISTRY


def self_evaluate() -> dict[str, object]:
    return REGISTRY.self_evaluate()
