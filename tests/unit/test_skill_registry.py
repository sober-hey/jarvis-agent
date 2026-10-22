"""Offline K4-002 registry candidate tests; not production approval."""

from pathlib import Path

import pytest

from jarvis.skill_manifest import SkillCapability, SkillManifest
from jarvis.skill_registry import SkillRegistry


def manifest(
    name: str = "notes",
    version: str = "1.0.0",
    *,
    dependencies: list[str] | None = None,
    tags: list[str] | None = None,
) -> SkillManifest:
    return SkillManifest(
        id=f"skill-{name}",
        name=name,
        version=version,
        description="Offline fixture",
        trigger_patterns=[name],
        capabilities={SkillCapability.READ_ONLY},
        dependencies=[] if dependencies is None else dependencies,
        created_at=1000,
        author="fixture",
        tags=[] if tags is None else tags,
    )


def test_install_get_and_list(tmp_path: Path) -> None:
    candidate = manifest()
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(candidate)
        assert registry.get("notes") == candidate
        assert registry.get("notes", "1.0.0") == candidate
        assert registry.list_installed() == [candidate]
        assert registry.runtime_enabled is False


def test_upgrade_and_failed_upgrade_keep_previous_active_version(tmp_path: Path) -> None:
    first = manifest()
    second = manifest(version="1.1.0")
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(first)
        registry.upgrade(second)
        assert registry.get("notes") == second
        assert registry.get("notes", "1.0.0") == first
        with pytest.raises(ValueError, match="depend"):
            registry.upgrade(manifest(version="2.0.0", dependencies=["missing>=1.0.0"]))
        assert registry.get("notes") == second
        assert registry.get("notes", "1.0.0") == first
        with pytest.raises(KeyError):
            registry.get("notes", "2.0.0")


def test_dependency_graph_and_conflicting_versions(tmp_path: Path) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest("base", "1.5.0"))
        registry.install(manifest(dependencies=["base>=1.0.0,<2.0.0"]))
        assert registry.dependency_graph() == {"base": [], "notes": ["base"]}
        with pytest.raises(ValueError, match="depend"):
            registry.install(manifest("bad", dependencies=["base>=2.0.0"]))
        assert [item.name for item in registry.list_installed()] == ["base", "notes"]
        with pytest.raises(ValueError, match="depend"):
            registry.upgrade(manifest("base", "2.0.0"))
        assert registry.get("base").version == "1.5.0"


def test_uninstall_soft_deletes_but_keeps_history(tmp_path: Path) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        registry.uninstall("notes")
        assert registry.list_installed() == []
        assert registry.dependency_graph() == {}
        with pytest.raises(KeyError):
            registry.get("notes")
        assert registry.get("notes", "1.0.0") == manifest()


def test_system_skill_cannot_be_uninstalled(tmp_path: Path) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest(tags=["SYSTEM"]))
        with pytest.raises(PermissionError, match="SYSTEM"):
            registry.uninstall("notes")
        assert registry.get("notes").tags == ["SYSTEM"]


def test_reopen_preserves_active_and_historical_versions(tmp_path: Path) -> None:
    path = tmp_path / "skills.db"
    with SkillRegistry(path, draft_fixture=True) as registry:
        registry.install(manifest())
        registry.upgrade(manifest(version="1.2.0"))
        registry.install(manifest("removed"))
        registry.uninstall("removed")
    with SkillRegistry(path, draft_fixture=True) as registry:
        assert registry.get("notes").version == "1.2.0"
        assert registry.get("notes", "1.0.0") == manifest()
        assert registry.get("removed", "1.0.0") == manifest("removed")
        assert [item.name for item in registry.list_installed()] == ["notes"]


def test_sqlite_failure_after_deactivation_rolls_back_every_write(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "skills.db"
    with SkillRegistry(path, draft_fixture=True) as registry:
        registry.install(manifest())
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TRIGGER fail_upgrade BEFORE INSERT ON skill_versions "
                "WHEN NEW.version = '2.0.0' BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            registry.upgrade(manifest(version="2.0.0"))
        assert registry.get("notes") == manifest()
        with pytest.raises(KeyError):
            registry.get("notes", "2.0.0")
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT version, active, deleted FROM skill_versions"
            ).fetchall() == [("1.0.0", 1, 0)]
            connection.execute("DROP TRIGGER fail_upgrade")
    with SkillRegistry(path, draft_fixture=True) as registry:
        assert registry.get("notes") == manifest()


def test_uninstall_dependency_is_blocked_without_partial_deletion(tmp_path: Path) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest("base"))
        registry.install(manifest(dependencies=["base"]))
        with pytest.raises(ValueError, match="depend"):
            registry.uninstall("base")
        assert registry.get("base") == manifest("base")
        registry.uninstall("notes")
        registry.uninstall("base")
        assert registry.list_installed() == []


def test_cycles_rejected_on_install_and_upgrade(tmp_path: Path) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        with pytest.raises(ValueError, match="cycle"):
            registry.install(manifest(dependencies=["notes@*"]))
        assert registry.list_installed() == []
        registry.install(manifest("base"))
        registry.install(manifest(dependencies=["base"]))
        with pytest.raises(ValueError, match="cycle"):
            registry.upgrade(manifest("base", "2.0.0", dependencies=["notes"]))
        assert registry.get("base").version == "1.0.0"


def test_system_lock_survives_upgrade_and_cannot_be_stripped(tmp_path: Path) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest(tags=["SYSTEM"]))
        with pytest.raises(PermissionError, match="SYSTEM"):
            registry.upgrade(manifest(version="2.0.0"))
        assert registry.get("notes").version == "1.0.0"
        registry.upgrade(manifest(version="2.0.0", tags=["SYSTEM"]))
        with pytest.raises(PermissionError, match="SYSTEM"):
            registry.uninstall("notes")


@pytest.mark.parametrize("version", ["1.0.0", "0.9.0", "1.0.0+build"])
def test_upgrade_rejects_equal_or_older_precedence(tmp_path: Path, version: str) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with pytest.raises(ValueError, match="newer"):
            registry.upgrade(manifest(version=version))
        assert registry.get("notes") == manifest()


@pytest.mark.parametrize(
    ("requirement", "accepted"),
    [
        ("base", True),
        ("base@1.2.0", True),
        ("base==1.2.0", True),
        ("base@>=1.0.0,<2.0.0", True),
        ("base>=1.2.0", True),
        ("base<=1.2.0", True),
        ("base>1.2.0", False),
        ("base<1.2.0", False),
        ("base!=1.2.0", False),
        ("base==2.0.0", False),
        ("base@*", True),
    ],
)
def test_dependency_requirement_boundaries(
    tmp_path: Path, requirement: str, accepted: bool
) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest("base", "1.2.0"))
        if accepted:
            registry.install(manifest(dependencies=[requirement]))
            assert registry.get("notes").dependencies == [requirement]
        else:
            with pytest.raises(ValueError, match="depend"):
                registry.install(manifest(dependencies=[requirement]))
            assert [item.name for item in registry.list_installed()] == ["base"]


@pytest.mark.parametrize(
    "declaration", ["base@", "base>=1", "base=>1.0.0", "base^1.0.0", "base>=1.0.0,", " base"]
)
def test_malformed_dependencies_rejected_before_any_write(tmp_path: Path, declaration: str) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest("base"))
        with pytest.raises(ValueError):
            registry.install(manifest(dependencies=[declaration]))
        assert [item.name for item in registry.list_installed()] == ["base"]


def test_reinstall_immutable_history_and_duplicate_install(tmp_path: Path) -> None:
    from dataclasses import replace

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with pytest.raises(ValueError, match="already installed"):
            registry.install(manifest())
        registry.uninstall("notes")
        with pytest.raises(ValueError, match="immutable"):
            registry.install(replace(manifest(), description="different bytes", checksum=""))
        assert registry.list_installed() == []
        registry.install(manifest())
        assert registry.get("notes") == manifest()


def test_input_and_returned_collections_cannot_mutate_persistence(tmp_path: Path) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        candidate = manifest()
        registry.install(candidate)
        candidate.tags.append("SYSTEM")
        registry.get("notes").tags.append("SYSTEM")
        registry.list_installed()[0].dependencies.append("missing")
        registry.dependency_graph()["notes"].append("missing")
        assert registry.get("notes") == manifest()
        registry.uninstall("notes")


def test_standalone_database_guard_and_explicit_opt_in(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "core.db"
    with pytest.raises(ValueError, match="draft_fixture"):
        SkillRegistry(path)
    assert not path.exists()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO tasks VALUES('original')")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="standalone"):
        SkillRegistry(path, draft_fixture=True)
    assert path.read_bytes() == before


def test_unknown_skill_errors_and_empty_graph(tmp_path: Path) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        assert registry.dependency_graph() == {}
        for operation in (
            lambda: registry.get("missing"),
            lambda: registry.uninstall("missing"),
            lambda: registry.upgrade(manifest()),
        ):
            with pytest.raises(KeyError):
                operation()


def test_second_connection_observes_committed_upgrades_and_uninstalls(tmp_path: Path) -> None:
    path = tmp_path / "skills.db"
    with (
        SkillRegistry(path, draft_fixture=True) as writer,
        SkillRegistry(path, draft_fixture=True) as reader,
    ):
        writer.install(manifest())
        assert reader.get("notes") == manifest()
        writer.upgrade(manifest(version="1.3.0"))
        assert reader.get("notes").version == "1.3.0"
        writer.uninstall("notes")
        assert reader.list_installed() == []
        assert reader.get("notes", "1.0.0") == manifest()


def test_racing_installs_leave_one_active_version(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "skills.db"
    gate = Barrier(2)

    def install_once(version: str) -> str:
        with SkillRegistry(path, draft_fixture=True) as registry:
            gate.wait(timeout=5)
            try:
                registry.install(manifest(version=version))
            except ValueError as error:
                assert "already installed" in str(error)
                return "conflict"
            return "installed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(install_once, ["1.0.0", "2.0.0"]))
    assert sorted(results) == ["conflict", "installed"]
    with SkillRegistry(path, draft_fixture=True) as registry:
        assert len(registry.list_installed()) == 1


def test_semver_dependencies_compare_numbers_and_prereleases(tmp_path: Path) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest("base", "1.0.0-rc.1"))
        registry.install(manifest(dependencies=["base>=1.0.0-alpha,<1.0.0"]))
        with pytest.raises(ValueError, match="depend"):
            registry.upgrade(manifest("base", "1.0.0"))
        assert registry.get("base").version == "1.0.0-rc.1"
        registry.uninstall("notes")
        registry.upgrade(manifest("base", "1.2.10"))
        registry.install(manifest("numeric", dependencies=["base>1.2.9,<1.10.0"]))


def test_duplicate_dependency_names_rejected(tmp_path: Path) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest("base"))
        with pytest.raises(ValueError, match="duplicate"):
            registry.install(manifest(dependencies=["base", "base>=1.0.0"]))
        assert [item.name for item in registry.list_installed()] == ["base"]


def test_stale_manifest_checksum_rejected_on_write(tmp_path: Path) -> None:
    candidate = manifest()
    candidate.tags.append("SYSTEM")
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        with pytest.raises(ValueError, match="checksum"):
            registry.install(candidate)
        assert registry.list_installed() == []


def test_identity_change_during_upgrade_rejected(tmp_path: Path) -> None:
    from dataclasses import replace

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with pytest.raises(ValueError, match="id"):
            registry.upgrade(replace(manifest(version="2.0.0"), id="replacement", checksum=""))
        assert registry.get("notes") == manifest()


def test_sql_layout_drift_is_rejected_on_reopen(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "skills.db"
    with SkillRegistry(path, draft_fixture=True) as registry:
        registry.install(manifest())
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX one_active_skill")
    with pytest.raises(ValueError, match="standalone"):
        SkillRegistry(path, draft_fixture=True)


def test_sqlite_like_wildcard_cannot_hide_foreign_table(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "foreign.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sqliteXforeign(id TEXT)")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="standalone"):
        SkillRegistry(path, draft_fixture=True)
    assert path.read_bytes() == before


def test_new_process_reads_current_and_historical_metadata(tmp_path: Path) -> None:
    import json
    import subprocess
    import sys

    path = tmp_path / "skills.db"
    with SkillRegistry(path, draft_fixture=True) as registry:
        registry.install(manifest())
        registry.upgrade(manifest(version="1.1.0"))
    code = (
        "import json,sys; from jarvis.skill_registry import SkillRegistry; "
        "r=SkillRegistry(sys.argv[1],draft_fixture=True); "
        "print(json.dumps([r.get('notes').version,r.get('notes','1.0.0').version])); r.close()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == ["1.1.0", "1.0.0"]
