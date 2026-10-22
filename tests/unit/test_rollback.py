"""K4-010 offline rollback fixtures, never a production release grant."""

from pathlib import Path

import pytest

from jarvis.skill_manifest import SkillCapability, SkillManifest
from jarvis.skill_registry import SkillRegistry


def manifest(version: str = "1.0.0", name: str = "notes") -> SkillManifest:
    return SkillManifest(
        id=f"skill-{name}",
        name=name,
        version=version,
        description="fixture",
        trigger_patterns=[name],
        capabilities={SkillCapability.READ_ONLY},
        dependencies=[],
        created_at=1000,
        author="fixture",
    )


def test_rollback_executes_through_registry_and_verifies(tmp_path: Path) -> None:
    # Local import makes RED an explicit missing-feature assertion, not collection failure.
    import importlib.util

    assert importlib.util.find_spec("jarvis.rollback") is not None, "rollback draft missing"
    from jarvis.rollback import RollbackManager

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        registry.upgrade(manifest("1.1.0"))
        with pytest.raises(ValueError, match="newer"):
            registry.upgrade(manifest())
        with RollbackManager(
            tmp_path / "rollback.db",
            registry,
            verifier=lambda _: True,
            clock=lambda: 2000,
            draft_fixture=True,
        ) as manager:
            record = manager.execute("notes", "1.0.0", reason="regression")
            assert registry.get("notes").version == "1.0.0"
            assert (record.from_version, record.to_version) == ("1.1.0", "1.0.0")
            assert record.reason == "regression"
            assert record.timestamp == 2000
            assert record.git_sha_before is None and record.git_sha_after is None
            assert manager.history("notes") == [record]
            assert manager.verify("notes") is True
            assert manager.runtime_enabled is False


def test_history_and_verify_survive_reopen(tmp_path: Path) -> None:
    from jarvis.rollback import RollbackManager

    path = tmp_path / "rollback.db"
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        registry.upgrade(manifest("1.1.0"))
        with RollbackManager(
            path, registry, verifier=lambda _: True, draft_fixture=True
        ) as manager:
            record = manager.execute("notes", "1.0.0")
        with RollbackManager(
            path, registry, verifier=lambda _: True, draft_fixture=True
        ) as manager:
            assert manager.history("notes") == [record]
            assert manager.history("other") == []
            assert manager.verify("notes") is True
            registry.upgrade(manifest("1.2.0"))
            assert manager.verify("notes") is False


def test_failed_tests_do_not_report_success(tmp_path: Path) -> None:
    import sqlite3

    from jarvis.rollback import RollbackManager

    path = tmp_path / "rollback.db"
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        registry.upgrade(manifest("1.1.0"))
        with RollbackManager(
            path, registry, verifier=lambda _: False, draft_fixture=True
        ) as manager:
            with pytest.raises(RuntimeError, match="tests failed"):
                manager.execute("notes", "1.0.0")
            assert registry.get("notes").version == "1.0.0"
            assert manager.verify("notes") is False
            assert len(manager.history("notes")) == 1
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT result FROM draft_rollbacks").fetchall() == [
                ("VERIFICATION_FAILED",)
            ]


@pytest.mark.parametrize("version", ["1.1.0", "2.0.0", "0.9.0"])
def test_unknown_same_or_future_target_cannot_switch(tmp_path: Path, version: str) -> None:
    from jarvis.rollback import RollbackManager

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        registry.upgrade(manifest("1.1.0"))
        with RollbackManager(
            tmp_path / "rollback.db",
            registry,
            verifier=lambda _: True,
            draft_fixture=True,
        ) as manager:
            with pytest.raises((ValueError, KeyError)):
                manager.execute("notes", version)
            assert registry.get("notes").version == "1.1.0"
            assert manager.history("notes") == []


def test_explicit_draft_and_verifier_required(tmp_path: Path) -> None:
    from jarvis.rollback import RollbackManager

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        registry.upgrade(manifest("1.1.0"))
        with pytest.raises(ValueError, match="draft_fixture"):
            RollbackManager(tmp_path / "reject.db", registry)
        assert not (tmp_path / "reject.db").exists()
        with RollbackManager(tmp_path / "rollback.db", registry, draft_fixture=True) as manager:
            with pytest.raises(PermissionError, match="verifier"):
                manager.execute("notes", "1.0.0")
            assert manager.verify("notes") is False
            assert registry.get("notes").version == "1.1.0"


def test_version_ledger_shas_are_observed_not_invented(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from jarvis.rollback import RollbackManager
    from jarvis.version_manager import SemanticVersion, VersionManager, VersionRecord

    with (
        SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry,
        VersionManager(tmp_path / "versions.db") as versions,
    ):
        registry.install(manifest())
        registry.upgrade(manifest("1.1.0"))
        for v, sha in [("1.0.0", "a" * 40), ("1.1.0", "b" * 40)]:
            versions.record(
                VersionRecord(
                    "notes",
                    SemanticVersion.parse(v),
                    "fixture",
                    "fixture",
                    datetime(2026, 1, 1, tzinfo=UTC),
                    sha,
                )
            )
        with RollbackManager(
            tmp_path / "rollback.db",
            registry,
            versions=versions,
            verifier=lambda _: True,
            draft_fixture=True,
        ) as manager:
            record = manager.execute("notes", "1.0.0")
            assert record.git_sha_before == "b" * 40
            assert record.git_sha_after == "a" * 40


def test_refuse_existing_unrelated_database(tmp_path: Path) -> None:
    import sqlite3

    from jarvis.rollback import RollbackManager

    path = tmp_path / "core.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE private_core(value TEXT)")
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        with pytest.raises(ValueError, match="standalone"):
            RollbackManager(path, registry, draft_fixture=True)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == [
            ("private_core",)
        ]


def test_blank_release_id_is_rejected_not_replaced(tmp_path: Path) -> None:
    from jarvis.rollback import RollbackManager

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        registry.upgrade(manifest("1.1.0"))
        with RollbackManager(
            tmp_path / "rollback.db",
            registry,
            verifier=lambda _: True,
            draft_fixture=True,
        ) as manager:
            with pytest.raises(ValueError, match="release_id"):
                manager.execute("notes", "1.0.0", release_id="")
            assert registry.get("notes").version == "1.1.0"


def test_registry_rollback_cannot_rewrite_historical_manifest(tmp_path: Path) -> None:
    from dataclasses import replace

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        first = manifest()
        registry.install(first)
        registry.upgrade(manifest("1.1.0"))
        with pytest.raises(ValueError, match="historical"):
            registry.upgrade(
                replace(first, description="tampered", checksum=""), allow_rollback=True
            )
        with pytest.raises(KeyError):
            registry.upgrade(manifest("0.9.0"), allow_rollback=True)
        assert registry.get("notes").version == "1.1.0"


def test_verifier_cannot_mutate_active_registry_and_claim_rollback_success(tmp_path: Path) -> None:
    from jarvis.rollback import RollbackManager

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        registry.upgrade(manifest("1.1.0"))

        def drift(_: SkillManifest) -> bool:
            registry.upgrade(manifest("1.2.0"))
            return True

        with RollbackManager(
            tmp_path / "rollback.db",
            registry,
            verifier=drift,
            draft_fixture=True,
        ) as manager:
            with pytest.raises(RuntimeError, match="version or tests"):
                manager.execute("notes", "1.0.0")
            assert manager.verify("notes") is False


def test_unknown_outcome_is_durable_and_never_blindly_retried(tmp_path: Path) -> None:
    import sqlite3

    from jarvis.rollback import RollbackManager

    class CommitThenRaise(SkillRegistry):
        def upgrade(self, manifest: SkillManifest, *, allow_rollback: bool = False) -> None:
            super().upgrade(manifest, allow_rollback=allow_rollback)
            if allow_rollback:
                raise RuntimeError("ack lost after commit")

    path = tmp_path / "rollback.db"
    with CommitThenRaise(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        registry.upgrade(manifest("1.1.0"))
        with RollbackManager(
            path, registry, verifier=lambda _: True, draft_fixture=True
        ) as manager:
            with pytest.raises(RuntimeError, match="ack lost"):
                manager.execute("notes", "1.0.0")
        with RollbackManager(
            path, registry, verifier=lambda _: True, draft_fixture=True
        ) as manager:
            assert registry.get("notes").version == "1.0.0"
            assert manager.verify("notes") is False
            with pytest.raises(RuntimeError, match="reconciliation"):
                manager.execute("notes", "1.0.0")
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT result FROM draft_rollbacks").fetchall() == [
                ("UNKNOWN",)
            ]


def test_pending_intent_precedes_registry_mutation(tmp_path: Path) -> None:
    import sqlite3

    from jarvis.rollback import RollbackManager

    path = tmp_path / "rollback.db"
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        registry.upgrade(manifest("1.1.0"))
        with RollbackManager(
            path, registry, verifier=lambda _: True, draft_fixture=True
        ) as manager:
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_intent BEFORE INSERT ON draft_rollbacks "
                    "BEGIN SELECT RAISE(ABORT,'intent rejected'); END"
                )
            with pytest.raises(sqlite3.IntegrityError, match="intent rejected"):
                manager.execute("notes", "1.0.0")
            assert registry.get("notes").version == "1.1.0"


def test_dependency_incompatible_rollback_is_atomic(tmp_path: Path) -> None:
    from dataclasses import replace

    from jarvis.rollback import RollbackManager

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        registry.upgrade(manifest("1.1.0"))
        registry.install(
            replace(
                manifest(name="consumer"),
                dependencies=["notes>=1.1.0"],
                checksum="",
            )
        )
        with RollbackManager(
            tmp_path / "rollback.db",
            registry,
            verifier=lambda _: True,
            draft_fixture=True,
        ) as manager:
            with pytest.raises(ValueError, match="depend"):
                manager.execute("notes", "1.0.0")
            assert registry.get("notes").version == "1.1.0"
            assert manager.history("notes") == []
            assert manager.verify("notes") is False


def test_multiple_rollbacks_preserve_order_and_component_filter(tmp_path: Path) -> None:
    from jarvis.rollback import RollbackManager

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        registry.upgrade(manifest("1.1.0"))
        registry.upgrade(manifest("1.2.0"))
        with RollbackManager(
            tmp_path / "rollback.db",
            registry,
            verifier=lambda _: True,
            draft_fixture=True,
        ) as manager:
            first = manager.execute("notes", "1.1.0")
            second = manager.execute("notes", "1.0.0")
            assert manager.history("notes") == [first, second]
            assert manager.history("elsewhere") == []
            assert manager.verify("notes") is True


def test_real_test_runner_can_verify_after_rollback(tmp_path: Path) -> None:
    from jarvis.rollback import RollbackManager
    from jarvis.test_runner import AutomatedTestRunner, TestSpec

    registry_path = tmp_path / "skills.db"
    test_path = tmp_path / "test_active_version.py"
    test_path.write_text(
        "from jarvis.skill_registry import SkillRegistry\n"
        "def test_active():\n"
        f"    with SkillRegistry({str(registry_path)!r}, draft_fixture=True) as registry:\n"
        "        assert registry.get('notes').version == '1.0.0'\n"
    )
    runner = AutomatedTestRunner()

    def verify(item: SkillManifest) -> bool:
        result = runner.run_tests(TestSpec(item.name, test_path, timeout_s=30))
        return result.passed == result.total == 1 and result.exit_code == 0

    with SkillRegistry(registry_path, draft_fixture=True) as registry:
        registry.install(manifest())
        registry.upgrade(manifest("1.1.0"))
        assert verify(registry.get("notes")) is False
        with RollbackManager(
            tmp_path / "rollback.db",
            registry,
            verifier=verify,
            draft_fixture=True,
        ) as manager:
            manager.execute("notes", "1.0.0")
            assert manager.verify("notes") is True
