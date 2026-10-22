"""K4-009 Controlled Release DRAFT; NOT_FROZEN / NOT_APPROVED.

runtime_enabled=False. Explicit trusted offline fixtures only. CANARY/STAGED run
caller-supplied trials without changing the active registry; FULL switches metadata.
The trial callback is NOT an OS sandbox or traffic router. ApprovalGate identity,
artifact binding, rollout cohorts and live Git/filesystem deployments remain pending.
K4-008 APPROVED is a quality preflight, not human approval: an independent explicit
approval_verifier is mandatory. SQLite persists intent before each external action.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from time import time_ns
from typing import ClassVar, Self
from uuid import uuid4

from jarvis.release_approval import ReleaseCandidate, ReleaseDecision, ReleaseGate
from jarvis.rollback import (
    RollbackManager,
    RollbackRecord,
    _Ledger,  # pyright: ignore[reportPrivateUsage]
    _text,  # pyright: ignore[reportPrivateUsage]
    _timestamp,  # pyright: ignore[reportPrivateUsage]
)
from jarvis.skill_manifest import SkillManifest, compare
from jarvis.skill_registry import (
    SkillRegistry,
    _encode,  # pyright: ignore[reportPrivateUsage]
)
from jarvis.version_manager import VersionManager

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


class ReleasePhase(StrEnum):
    CANARY = "CANARY"
    STAGED = "STAGED"
    FULL = "FULL"


@dataclass(frozen=True, slots=True)
class ReleaseStatus:
    """Last durable phase result, not a production health assertion (UTC ms)."""

    release_id: str
    component: str
    version: str
    previous_version: str
    phase: ReleasePhase
    result: str
    created_at: int
    updated_at: int
    error: str | None = None


_STATUS_COLUMNS = (
    "release_id,component,version,previous_version,phase,result,created_at,updated_at,error"
)


class ReleaseManager:
    """Serialized offline coordinator with standalone shared rollback ledger.

    No first install: a passing active baseline is required for a meaningful inverse.
    Every release starts at CANARY; promote only follows CANARY -> STAGED -> FULL.
    Approval/trials are injected trusted infrastructure, never derived from model
    strings. Callbacks must return exact True. A persisted PENDING/UNKNOWN blocks
    reuse; the K3 RecoveryManager cannot reconcile this independent draft ledger.
    """

    draft_status: ClassVar[str] = DRAFT_STATUS
    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(
        self,
        database: str | Path,
        registry: SkillRegistry,
        *,
        manifest_resolver: Callable[[ReleaseCandidate], SkillManifest],
        approval_verifier: Callable[[str, str, str], bool] | None = None,
        trial_runner: Callable[[SkillManifest, ReleasePhase], bool] | None = None,
        verifier: Callable[[SkillManifest], bool] | None = None,
        versions: VersionManager | None = None,
        clock: Callable[[], int] | None = None,
        draft_fixture: bool = False,
    ) -> None:
        if draft_fixture is not True:
            raise ValueError("explicit offline draft_fixture=True required")
        self._registry = registry
        self._resolver = manifest_resolver
        self._approval = approval_verifier
        self._trial = trial_runner
        self._verifier = verifier
        self._versions = versions
        self._clock = clock or (lambda: time_ns() // 1_000_000)
        self._ledger = _Ledger(database)
        try:
            self._rollback = RollbackManager(
                database,
                registry,
                verifier=verifier,
                versions=versions,
                clock=self._clock,
                draft_fixture=True,
            )
        except BaseException:
            self._ledger.connection.close()
            raise

    def _now(self) -> int:
        now = self._clock()
        _timestamp(now)
        return now

    def _authorize(self, component: str, version: str, sha: str) -> None:
        if self._approval is None or self._approval(component, version, sha) is not True:
            raise PermissionError("independent explicit release approval required")
        if self._trial is None or self._verifier is None:
            raise PermissionError("explicit trial runner and test verifier required")

    def deploy(self, candidate: ReleaseCandidate, phase: ReleasePhase) -> str:
        if type(phase) is not ReleasePhase or phase is not ReleasePhase.CANARY:
            raise ValueError("deployment must start at CANARY; use promote for later phases")
        candidate = copy.deepcopy(candidate)
        decision, reasons = ReleaseGate().evaluate(candidate)
        if decision is not ReleaseDecision.APPROVED:
            raise PermissionError("release quality gate rejected: " + "; ".join(reasons))
        self._authorize(candidate.component, candidate.version, candidate.git_sha)
        manifest = replace(self._resolver(copy.deepcopy(candidate)))
        if manifest.name != candidate.component or manifest.version != candidate.version:
            raise ValueError("candidate and resolved manifest identity mismatch")
        payload = _encode(manifest)
        with self._ledger.transaction():
            self._ledger.check_pending(candidate.component)
            old = self._registry.get(candidate.component)
            if compare(manifest.version, old.version) <= 0 or old.id != manifest.id:
                raise ValueError("candidate must be a newer version of the same skill")
            assert self._verifier is not None
            if self._verifier(replace(old)) is not True:
                raise ValueError("previous version is not a verified known-good baseline")
            if self._registry.get(candidate.component) != old:
                raise RuntimeError("baseline changed during verification; reconciliation required")
            if self._ledger.connection.execute(
                "SELECT 1 FROM draft_releases WHERE component=? AND result!='ROLLED_BACK' "
                "AND NOT (phase='FULL' AND result='SUCCESS')",
                (candidate.component,),
            ).fetchone():
                raise RuntimeError("component already has an unresolved release")
            release_id, now = str(uuid4()), self._now()
            self._ledger.connection.execute(
                "INSERT INTO draft_releases (release_id,component,version,previous_version,"
                "manifest,"
                "git_sha,phase,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'PENDING',?,?)",
                (
                    release_id,
                    manifest.name,
                    manifest.version,
                    old.version,
                    payload,
                    candidate.git_sha,
                    phase.value,
                    now,
                    now,
                ),
            )
            self._event(release_id, phase, "PENDING", now)
        self._run(release_id, manifest, phase)
        return release_id

    def _event(
        self, release_id: str, phase: ReleasePhase, result: str, now: int, error: str | None = None
    ) -> None:
        self._ledger.connection.execute(
            "INSERT INTO draft_release_events(release_id,phase,timestamp,result,error) "
            "VALUES(?,?,?,?,?)",
            (release_id, phase.value, now, result, error),
        )

    def _result(
        self, release_id: str, phase: ReleasePhase, result: str, error: str | None = None
    ) -> None:
        now = self._now()
        with self._ledger.transaction():
            self._ledger.connection.execute(
                "UPDATE draft_releases SET phase=?,result=?,updated_at=?,error=? "
                "WHERE release_id=?",
                (phase.value, result, now, error, release_id),
            )
            self._event(release_id, phase, result, now, error)

    def _run(self, release_id: str, manifest: SkillManifest, phase: ReleasePhase) -> None:
        assert self._trial is not None and self._verifier is not None
        try:
            passed = self._trial(replace(manifest), phase) is True
        except Exception as exc:
            self._result(release_id, phase, "UNKNOWN", str(exc))
            raise RuntimeError(f"trial outcome unknown for release {release_id}") from exc
        if not passed:
            self._result(release_id, phase, "FAILED", "trial rejected")
            raise RuntimeError(f"trial failed for release {release_id}")
        old = self.status(release_id).previous_version
        if self._registry.get(manifest.name).version != old:
            self._result(release_id, phase, "UNKNOWN", "active registry drift")
            raise RuntimeError("active registry drift; reconciliation required")
        if phase is ReleasePhase.FULL:
            try:
                self._registry.upgrade(manifest)
            except Exception as exc:
                self._result(release_id, phase, "UNKNOWN", str(exc))
                raise RuntimeError(f"registry switch unknown for release {release_id}") from exc
            try:
                passed = self._verifier(replace(manifest)) is True
                valid = passed and self._registry.get(manifest.name) == manifest
            except Exception as exc:
                self._result(release_id, phase, "FAILED", str(exc))
                raise RuntimeError(f"post-release tests failed for release {release_id}") from exc
            if not valid:
                self._result(release_id, phase, "FAILED", "post-release tests rejected")
                raise RuntimeError(f"post-release tests failed for release {release_id}")
        self._result(release_id, phase, "SUCCESS")

    def promote(self, release_id: str) -> None:
        status = self.status(release_id)
        if status.result != "SUCCESS" or status.phase is ReleasePhase.FULL:
            raise ValueError("only a successful CANARY or STAGED release can be promoted")
        row = self._ledger.connection.execute(
            "SELECT manifest,git_sha FROM draft_releases WHERE release_id=?",
            (release_id,),
        ).fetchone()
        manifest = SkillRegistry._decode(row[0])  # pyright: ignore[reportPrivateUsage]
        self._authorize(status.component, status.version, row[1])
        next_phase = (
            ReleasePhase.STAGED if status.phase is ReleasePhase.CANARY else ReleasePhase.FULL
        )
        with self._ledger.transaction():
            self._ledger.check_pending(status.component)
            if self.status(release_id) != status:
                raise RuntimeError("release changed; reconciliation required")
            if self._registry.get(status.component).version != status.previous_version:
                raise RuntimeError("active registry drift; reconciliation required")
            now = self._now()
            self._ledger.connection.execute(
                "UPDATE draft_releases SET phase=?,result='PENDING',updated_at=? "
                "WHERE release_id=?",
                (next_phase.value, now, release_id),
            )
            self._event(release_id, next_phase, "PENDING", now)
        self._run(release_id, manifest, next_phase)

    def rollback(self, release_id: str, reason: str) -> RollbackRecord:
        status = self.status(release_id)
        _text("reason", reason)
        if status.result in {"PENDING", "UNKNOWN", "ROLLED_BACK"}:
            raise ValueError("release cannot be rolled back without reconciliation")
        current = self._registry.get(status.component)
        latest = self._ledger.connection.execute(
            "SELECT release_id FROM draft_releases WHERE component=? "
            "ORDER BY sequence DESC LIMIT 1",
            (status.component,),
        ).fetchone()
        if latest is None or latest[0] != release_id:
            raise ValueError("stale release cannot roll back a newer release's baseline")
        if status.phase is not ReleasePhase.FULL:
            raise ValueError("CANARY/STAGED have no active switch; withdraw is DRAFT_PENDING_DP")
        if current.version != status.version:
            raise ValueError("stale release cannot roll back a newer or changed active version")
        record = self._rollback.execute(
            status.component,
            status.previous_version,
            release_id=release_id,
            reason=reason,
        )
        self._result(release_id, status.phase, "ROLLED_BACK")
        return record

    def status(self, release_id: str) -> ReleaseStatus:
        _text("release_id", release_id)
        row = self._ledger.connection.execute(
            f"SELECT {_STATUS_COLUMNS} FROM draft_releases WHERE release_id=?",
            (release_id,),
        ).fetchone()
        if row is None:
            raise KeyError(release_id)
        result = ReleaseStatus(
            row[0], row[1], row[2], row[3], ReleasePhase(row[4]), row[5], row[6], row[7], row[8]
        )
        # Read-only reconciliation projection: a crash between rollback commit and
        # release status commit must not hide a completed inverse or permit replay.
        rb = self._ledger.connection.execute(
            "SELECT result FROM draft_rollbacks WHERE release_id=?",
            (release_id,),
        ).fetchone()
        if rb is not None:
            if rb[0] == "SUCCESS":
                return replace(result, result="ROLLED_BACK")
            if rb[0] in {"PENDING", "APPLIED", "UNKNOWN", "VERIFICATION_FAILED"}:
                return replace(result, result="UNKNOWN", error="rollback requires reconciliation")
        return result

    def history(self, release_id: str) -> list[ReleaseStatus]:
        current = self.status(release_id)
        return [
            replace(
                current, phase=ReleasePhase(row[0]), result=row[1], updated_at=row[2], error=row[3]
            )
            for row in self._ledger.connection.execute(
                "SELECT phase,result,timestamp,error FROM draft_release_events "
                "WHERE release_id=? ORDER BY sequence",
                (release_id,),
            )
        ]

    def close(self) -> None:
        self._rollback.close()
        self._ledger.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
