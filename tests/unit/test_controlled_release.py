"""K4-009 controlled-release offline fixtures."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jarvis.controlled_release import ReleaseManager, ReleasePhase
from jarvis.regression_evaluator import RegressionReport
from jarvis.release_approval import ReleaseCandidate
from jarvis.skill_manifest import SkillCapability, SkillManifest
from jarvis.skill_registry import SkillRegistry
from jarvis.test_runner import TestResult
from jarvis.version_manager import SemanticVersion


def manifest(version: str = "1.0.0") -> SkillManifest:
    return SkillManifest(
        id="notes",
        name="notes",
        version=version,
        description="fixture",
        trigger_patterns=["notes"],
        capabilities={SkillCapability.READ_ONLY},
        dependencies=[],
        created_at=1000,
        author="fixture",
    )


def candidate(version: str = "1.1.0") -> ReleaseCandidate:
    result = TestResult("notes", 2, 0, 0, 2, 1.0, 0.1, "", "", exit_code=0)
    report = RegressionReport(
        "notes",
        SemanticVersion.parse("1.0.0"),
        SemanticVersion.parse(version),
        result,
        result,
        [],
        [],
        "PASS",
    )
    return ReleaseCandidate(
        "notes",
        version,
        result,
        report,
        {"status": "completed", "findings": []},
        "fixture upgrade",
        "a" * 40,
    )


def test_deploy_promote_and_rollback(tmp_path: Path) -> None:
    assert importlib.util.find_spec("jarvis.controlled_release") is not None, (
        "release draft missing"
    )
    from jarvis.controlled_release import ReleaseManager, ReleasePhase

    phases: list[ReleasePhase] = []

    def trial(item: SkillManifest, phase: ReleasePhase) -> bool:
        assert item.version == "1.1.0"
        phases.append(phase)
        return True

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with ReleaseManager(
            tmp_path / "release.db",
            registry,
            manifest_resolver=lambda c: manifest(c.version),
            approval_verifier=lambda component, version, sha: True,
            trial_runner=trial,
            verifier=lambda _: True,
            clock=lambda: 1000,
            draft_fixture=True,
        ) as manager:
            release_id = manager.deploy(candidate(), ReleasePhase.CANARY)
            assert manager.status(release_id).phase is ReleasePhase.CANARY
            assert registry.get("notes").version == "1.0.0"
            manager.promote(release_id)
            assert manager.status(release_id).phase is ReleasePhase.STAGED
            assert registry.get("notes").version == "1.0.0"
            manager.promote(release_id)
            assert manager.status(release_id).phase is ReleasePhase.FULL
            assert registry.get("notes").version == "1.1.0"
            assert phases == [ReleasePhase.CANARY, ReleasePhase.STAGED, ReleasePhase.FULL]
            record = manager.rollback(release_id, "regression")
            assert record.release_id == release_id
            assert record.reason == "regression"
            assert record.to_version == "1.0.0"
            assert registry.get("notes").version == "1.0.0"
            assert manager.status(release_id).result == "ROLLED_BACK"
            assert manager.runtime_enabled is False


def test_release_history_survives_reopen(tmp_path: Path) -> None:
    from jarvis.controlled_release import ReleasePhase

    path = tmp_path / "release.db"
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(path, registry) as manager:
            release_id = manager.deploy(candidate(), ReleasePhase.CANARY)
            manager.promote(release_id)
            before = manager.status(release_id)
            events = manager.history(release_id)
        with make_manager(path, registry) as manager:
            assert manager.status(release_id) == before
            assert manager.history(release_id) == events
            assert [event.result for event in events] == ["PENDING", "SUCCESS"] * 2
            assert events[-1].phase is ReleasePhase.STAGED
            manager.promote(release_id)
            assert registry.get("notes").version == "1.1.0"


def make_manager(
    path: Path,
    registry: SkillRegistry,
    *,
    approval_verifier: Callable[[str, str, str], bool] | None = lambda c, v, s: True,
    trial_runner: Callable[[SkillManifest, ReleasePhase], bool] | None = lambda m, p: True,
    verifier: Callable[[SkillManifest], bool] | None = lambda m: True,
) -> ReleaseManager:
    return ReleaseManager(
        path,
        registry,
        manifest_resolver=lambda c: manifest(c.version),
        approval_verifier=approval_verifier,
        trial_runner=trial_runner,
        verifier=verifier,
        draft_fixture=True,
    )


def test_new_release_after_rollback(tmp_path: Path) -> None:
    from jarvis.controlled_release import ReleasePhase

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(tmp_path / "release.db", registry) as manager:
            for version in ("1.1.0", "1.2.0"):
                release_id = manager.deploy(candidate(version), ReleasePhase.CANARY)
                manager.promote(release_id)
                manager.promote(release_id)
                manager.rollback(release_id, "fixture")
            assert registry.get("notes").version == "1.0.0"


def test_full_release_then_new_release_then_rollback_then_new_release(tmp_path: Path) -> None:
    from jarvis.controlled_release import ReleasePhase

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(tmp_path / "release.db", registry) as manager:
            first = manager.deploy(candidate(), ReleasePhase.CANARY)
            manager.promote(first)
            manager.promote(first)
            second = manager.deploy(candidate("1.2.0"), ReleasePhase.CANARY)
            manager.promote(second)
            manager.promote(second)
            manager.rollback(second, "fixture")
            third = manager.deploy(candidate("1.3.0"), ReleasePhase.CANARY)
            assert manager.status(third).phase is ReleasePhase.CANARY


@pytest.mark.parametrize("phase", [ReleasePhase.STAGED, ReleasePhase.FULL, "CANARY"])
def test_cannot_skip_canary(tmp_path: Path, phase: Any) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(tmp_path / "release.db", registry) as manager:
            with pytest.raises(ValueError, match="CANARY"):
                manager.deploy(candidate(), phase)
            assert registry.get("notes").version == "1.0.0"


@pytest.mark.parametrize("answer", [None, False, 1])
def test_approval_missing_or_false_cannot_deploy(tmp_path: Path, answer: Any) -> None:
    def approval(c: str, v: str, s: str) -> Any:
        return answer

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(
            tmp_path / "release.db",
            registry,
            approval_verifier=None if answer is None else approval,
        ) as manager:
            with pytest.raises(PermissionError, match="approval"):
                manager.deploy(candidate(), ReleasePhase.CANARY)
            assert registry.get("notes").version == "1.0.0"


def test_quality_gate_cannot_be_bypassed_by_explicit_approval(tmp_path: Path) -> None:
    from dataclasses import replace

    bad = replace(candidate(), security_scan_result={"status": "incomplete"})
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(tmp_path / "release.db", registry) as manager:
            with pytest.raises(PermissionError, match="quality"):
                manager.deploy(bad, ReleasePhase.CANARY)


def test_trial_failure_is_durable_and_blocks_promotion(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "release.db"
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(path, registry, trial_runner=lambda m, p: False) as manager:
            with pytest.raises(RuntimeError, match="trial failed"):
                manager.deploy(candidate(), ReleasePhase.CANARY)
        with sqlite3.connect(path) as connection:
            release_id = connection.execute("SELECT release_id FROM draft_releases").fetchone()[0]
        with make_manager(path, registry) as manager:
            assert manager.status(release_id).result == "FAILED"
            with pytest.raises(ValueError):
                manager.promote(release_id)
            with pytest.raises(RuntimeError, match="unresolved"):
                manager.deploy(candidate("1.2.0"), ReleasePhase.CANARY)
            assert registry.get("notes").version == "1.0.0"


def test_approval_revocation_blocks_promotion(tmp_path: Path) -> None:
    approved = True
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(
            tmp_path / "release.db",
            registry,
            approval_verifier=lambda c, v, s: approved,
        ) as manager:
            release_id = manager.deploy(candidate(), ReleasePhase.CANARY)
            approved = False
            with pytest.raises(PermissionError):
                manager.promote(release_id)
            assert manager.status(release_id).phase is ReleasePhase.CANARY


def test_stale_release_cannot_rollback_newer_version(tmp_path: Path) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(tmp_path / "release.db", registry) as manager:
            first = manager.deploy(candidate(), ReleasePhase.CANARY)
            manager.promote(first)
            manager.promote(first)
            second = manager.deploy(candidate("1.2.0"), ReleasePhase.CANARY)
            manager.promote(second)
            manager.promote(second)
            with pytest.raises(ValueError, match="stale"):
                manager.rollback(first, "do not affect second")
            assert registry.get("notes").version == "1.2.0"


def test_old_release_same_version_reactivation_cannot_rollback_current_canary(
    tmp_path: Path,
) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(tmp_path / "release.db", registry) as manager:
            first = manager.deploy(candidate(), ReleasePhase.CANARY)
            manager.promote(first)
            manager.promote(first)
            second = manager.deploy(candidate("1.2.0"), ReleasePhase.CANARY)
            manager.promote(second)
            manager.promote(second)
            manager.rollback(second, "revert second")
            third = manager.deploy(candidate("1.3.0"), ReleasePhase.CANARY)
            with pytest.raises(ValueError, match="stale"):
                manager.rollback(first, "must not kill third baseline")
            assert manager.status(third).result == "SUCCESS"


def test_trial_cannot_mutate_persisted_candidate(tmp_path: Path) -> None:
    def mutate(item: SkillManifest, phase: ReleasePhase) -> bool:
        item.tags.append("mutated")
        return True

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(tmp_path / "release.db", registry, trial_runner=mutate) as manager:
            release_id = manager.deploy(candidate(), ReleasePhase.CANARY)
            manager.promote(release_id)
            manager.promote(release_id)
            assert registry.get("notes").tags == []


def test_post_release_verifier_drift_is_not_success(tmp_path: Path) -> None:
    def drift(item: SkillManifest) -> bool:
        if item.version == "1.1.0":
            registry.upgrade(manifest("1.2.0"))
        return True

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(tmp_path / "release.db", registry, verifier=drift) as manager:
            release_id = manager.deploy(candidate(), ReleasePhase.CANARY)
            manager.promote(release_id)
            with pytest.raises(RuntimeError, match="post-release"):
                manager.promote(release_id)
            assert manager.status(release_id).result == "FAILED"


def test_memory_database_cannot_silently_split_release_and_rollback_ledgers(tmp_path: Path) -> None:
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        with pytest.raises(ValueError, match="file-backed"):
            make_manager(Path(":memory:"), registry)


def test_failed_full_tests_can_rollback_known_good_baseline(tmp_path: Path) -> None:
    def verify(item: SkillManifest) -> bool:
        return item.version == "1.0.0"

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(tmp_path / "release.db", registry, verifier=verify) as manager:
            release_id = manager.deploy(candidate(), ReleasePhase.CANARY)
            manager.promote(release_id)
            with pytest.raises(RuntimeError, match="post-release"):
                manager.promote(release_id)
            assert manager.status(release_id).result == "FAILED"
            assert registry.get("notes").version == "1.1.0"
            manager.rollback(release_id, "post-release tests failed")
            assert manager.status(release_id).result == "ROLLED_BACK"
            assert registry.get("notes").version == "1.0.0"


def test_pending_intent_survives_interrupted_trial_and_is_never_retried(tmp_path: Path) -> None:
    import sqlite3

    def crash(item: SkillManifest, phase: ReleasePhase) -> bool:
        raise KeyboardInterrupt("simulated coordinator interruption")

    path = tmp_path / "release.db"
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(path, registry, trial_runner=crash) as manager:
            with pytest.raises(KeyboardInterrupt):
                manager.deploy(candidate(), ReleasePhase.CANARY)
        with sqlite3.connect(path) as connection:
            release_id = connection.execute("SELECT release_id FROM draft_releases").fetchone()[0]
        with make_manager(path, registry) as manager:
            assert manager.status(release_id).result == "PENDING"
            with pytest.raises(ValueError):
                manager.promote(release_id)
            with pytest.raises(RuntimeError, match="unresolved"):
                manager.deploy(candidate(), ReleasePhase.CANARY)
            assert registry.get("notes").version == "1.0.0"


def test_event_failure_rolls_back_deployment_intent_before_trial(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "release.db"
    trials: list[bool] = []

    def trial(item: SkillManifest, phase: ReleasePhase) -> bool:
        trials.append(True)
        return True

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(path, registry, trial_runner=trial) as manager:
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_event BEFORE INSERT ON draft_release_events "
                    "BEGIN SELECT RAISE(ABORT,'event failure'); END"
                )
            with pytest.raises(sqlite3.IntegrityError, match="event failure"):
                manager.deploy(candidate(), ReleasePhase.CANARY)
            assert trials == []
            assert registry.get("notes").version == "1.0.0"
            with sqlite3.connect(path) as connection:
                assert connection.execute("SELECT count(*) FROM draft_releases").fetchone() == (0,)
                assert connection.execute(
                    "SELECT count(*) FROM draft_release_events"
                ).fetchone() == (0,)


def test_status_recovers_completed_rollback_without_duplicate_switch(tmp_path: Path) -> None:
    from jarvis.rollback import RollbackManager

    path = tmp_path / "release.db"
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(path, registry) as manager:
            release_id = manager.deploy(candidate(), ReleasePhase.CANARY)
            manager.promote(release_id)
            manager.promote(release_id)
        # Committed inverse followed by crash before the release-status commit.
        with RollbackManager(
            path, registry, verifier=lambda _: True, draft_fixture=True
        ) as rollback:
            rollback.execute("notes", "1.0.0", release_id=release_id, reason="fixture crash window")
        with make_manager(path, registry) as manager:
            assert manager.status(release_id).result == "ROLLED_BACK"
            with pytest.raises(ValueError):
                manager.rollback(release_id, "no replay")
            assert registry.get("notes").version == "1.0.0"


def test_failed_rollback_verification_blocks_new_release(tmp_path: Path) -> None:
    from jarvis.rollback import RollbackManager

    path = tmp_path / "release.db"
    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(path, registry) as manager:
            release_id = manager.deploy(candidate(), ReleasePhase.CANARY)
            manager.promote(release_id)
            manager.promote(release_id)
        with RollbackManager(
            path, registry, verifier=lambda _: False, draft_fixture=True
        ) as rollback:
            with pytest.raises(RuntimeError, match="version or tests"):
                rollback.execute("notes", "1.0.0", release_id=release_id, reason="bad old tests")
        with make_manager(path, registry) as manager:
            assert manager.status(release_id).result == "UNKNOWN"
            with pytest.raises(RuntimeError, match="reconciliation"):
                manager.deploy(candidate("1.2.0"), ReleasePhase.CANARY)


def test_canary_can_use_fresh_exec_isolated_runner_without_active_switch(tmp_path: Path) -> None:
    import os

    from jarvis.isolated_runner import IsolatedRunner, RunConfig

    script = tmp_path / "trial.py"
    script.write_text("import os\nprint(os.getpid())\nassert 2 + 2 == 4\n")
    runner = IsolatedRunner(draft_fixture=True)
    observed: list[int] = []

    def trial(item: SkillManifest, phase: ReleasePhase) -> bool:
        assert phase is ReleasePhase.CANARY
        assert item.version == "1.1.0"
        result = runner.run_subprocess(
            script,
            RunConfig(
                timeout_s=10,
                max_memory_mb=None,
                allowed_capabilities={SkillCapability.PROCESS_EXEC},
            ),
        )
        assert result.success and result.return_code == 0
        observed.append(int(result.stdout.strip()))
        return result.success

    with SkillRegistry(tmp_path / "skills.db", draft_fixture=True) as registry:
        registry.install(manifest())
        with make_manager(tmp_path / "release.db", registry, trial_runner=trial) as manager:
            release_id = manager.deploy(candidate(), ReleasePhase.CANARY)
            assert manager.status(release_id).result == "SUCCESS"
            assert registry.get("notes").version == "1.0.0"
            assert len(observed) == 1 and observed[0] != os.getpid()
