"""K4-005 Improvement Proposal DRAFT; NOT_FROZEN / NOT_APPROVED.

runtime_enabled=False. Standalone opt-in SQLite candidate ledger only. Proposal
approval is advisory, NOT an execution/release grant. No code is changed, no
VersionManager record is written and no packaging/ or core DB is migrated.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import time_ns
from typing import ClassVar, Self, cast

from jarvis.approval import RiskLevel
from jarvis.outbox import OutboxChannel, OutboxMessage, OutboxWriter, create_outbox_schema
from jarvis.version_manager import JSONValue, SemanticVersion

DRAFT_STATUS = "DRAFT_PENDING_DP"
RUNTIME_ENABLED = False


class ProposalStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    IMPLEMENTED = "IMPLEMENTED"


def _text(name: str, value: object, *, blank: bool = False) -> None:
    if type(value) is not str or (not blank and not value.strip()) or "\x00" in value:
        raise ValueError(f"{name} must be nonblank UTF-8 text without NUL")
    value.encode("utf-8", errors="strict")


def _json_value(value: object) -> None:
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        value.encode("utf-8", errors="strict")
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in cast(list[object], value):
            _json_value(item)
        return
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ValueError("proposed_changes keys must be strings")
            key.encode("utf-8", errors="strict")
            _json_value(item)
        return
    raise ValueError("proposed_changes must contain only finite exact JSON types")


def _changes_json(value: dict[str, JSONValue]) -> str:
    if type(value) is not dict:
        raise ValueError("proposed_changes must be a dict")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    _json_value(value)
    return encoded


@dataclass(frozen=True, slots=True)
class ImprovementProposal:
    """Detached proposal snapshot, not deeply immutable or verified impact/risk.

    created_at is an aware datetime normalized to UTC. proposed_version accepts
    strict SemVer text or SemanticVersion and is stored as SemanticVersion.
    """

    id: str
    title: str
    description: str
    proposed_changes: dict[str, JSONValue]
    target_component: str
    impact_assessment: str
    risk_level: RiskLevel
    proposed_version: SemanticVersion | str
    created_at: datetime
    status: ProposalStatus = ProposalStatus.DRAFT
    reviewer_notes: str = ""

    def __post_init__(self) -> None:
        for name in ("id", "title", "description", "target_component", "impact_assessment"):
            _text(name, getattr(self, name))
        _text("reviewer_notes", self.reviewer_notes, blank=True)
        if type(self.risk_level) is not RiskLevel:
            raise ValueError("risk_level must be RiskLevel")
        if type(self.status) is not ProposalStatus:
            raise ValueError("status must be ProposalStatus")
        version = self.proposed_version
        if type(version) is str:
            version = SemanticVersion.parse(version)
        if type(version) is not SemanticVersion:
            raise ValueError("proposed_version must be strict SemVer or SemanticVersion")
        object.__setattr__(self, "proposed_version", replace(version))
        if (
            not isinstance(cast(object, self.created_at), datetime)
            or self.created_at.utcoffset() is None
        ):
            raise ValueError("created_at must be a timezone-aware datetime")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        object.__setattr__(
            self, "proposed_changes", json.loads(_changes_json(self.proposed_changes))
        )


_SCHEMA = (
    "CREATE TABLE draft_proposal_schema ("
    "singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL CHECK(version=1))",
    "CREATE TABLE draft_improvement_proposals ("
    "sequence INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, title TEXT NOT NULL, "
    "description TEXT NOT NULL, proposed_changes TEXT NOT NULL "
    "CHECK(json_valid(proposed_changes) AND json_type(proposed_changes)='object'), "
    "target_component TEXT NOT NULL, impact_assessment TEXT NOT NULL, "
    "risk_level TEXT NOT NULL CHECK(risk_level IN ('LOW','MEDIUM','HIGH')), "
    "proposed_version TEXT NOT NULL, created_at TEXT NOT NULL, "
    "status TEXT NOT NULL CHECK(status IN "
    "('DRAFT','PENDING_REVIEW','APPROVED','REJECTED','IMPLEMENTED')), "
    "reviewer_notes TEXT NOT NULL)",
    "CREATE INDEX draft_proposal_component "
    "ON draft_improvement_proposals(target_component,sequence)",
    "CREATE INDEX draft_proposal_pending ON draft_improvement_proposals(status,sequence)",
)
_COLUMNS = (
    "id,title,description,proposed_changes,target_component,impact_assessment,"
    "risk_level,proposed_version,created_at,status,reviewer_notes"
)


def _schema_objects(connection: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    return {
        name: (kind, sql)
        for name, kind, sql in connection.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
        )
    }


def _expected_schema() -> dict[str, tuple[str, str]]:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in _SCHEMA:
            connection.execute(statement)
        create_outbox_schema(connection, draft_fixture=True)
        return _schema_objects(connection)
    finally:
        connection.close()


_EXPECTED_SCHEMA = _expected_schema()


class ProposalManager:
    """Offline candidate manager; serialized calls, no model/CLI tool registration.

    All risks require manual review. Python methods are not a trusted identity
    boundary; authenticated reviewer ingress remains DRAFT_PENDING_DP.
    """

    draft_status: ClassVar[str] = DRAFT_STATUS
    runtime_enabled: ClassVar[bool] = RUNTIME_ENABLED

    def __init__(
        self,
        database: str | Path,
        *,
        draft_fixture: bool = False,
        sir_recipient: str | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if draft_fixture is not True:
            raise ValueError("explicit offline draft_fixture=True required")
        if sir_recipient is not None:
            _text("sir_recipient", sir_recipient)
        self._sir_recipient = sir_recipient
        self._clock = clock if clock is not None else lambda: time_ns() // 1_000_000
        self._outbox = OutboxWriter(draft_fixture=True)
        self._closed = False
        self._connection = sqlite3.connect(database, isolation_level=None, timeout=5)
        try:
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA synchronous=FULL")
            with self._transaction():
                existing = _schema_objects(self._connection)
                if not existing:
                    for statement in _SCHEMA:
                        self._connection.execute(statement)
                    create_outbox_schema(self._connection, draft_fixture=True)
                    self._connection.execute("INSERT INTO draft_proposal_schema VALUES(1,1)")
                elif existing != _EXPECTED_SCHEMA:
                    raise ValueError("proposal schema mismatch; standalone candidate DB required")
                if self._connection.execute("SELECT * FROM draft_proposal_schema").fetchall() != [
                    (1, 1)
                ]:
                    raise ValueError("proposal schema metadata mismatch")
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("proposal manager is closed")

    @contextmanager
    def _transaction(self) -> Generator[None, None, None]:
        self._ensure_open()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    @staticmethod
    def _decode(row: tuple[object, ...]) -> ImprovementProposal:
        return ImprovementProposal(
            id=cast(str, row[0]),
            title=cast(str, row[1]),
            description=cast(str, row[2]),
            proposed_changes=json.loads(cast(str, row[3])),
            target_component=cast(str, row[4]),
            impact_assessment=cast(str, row[5]),
            risk_level=RiskLevel(row[6]),
            proposed_version=SemanticVersion.parse(cast(str, row[7])),
            created_at=datetime.fromisoformat(cast(str, row[8])),
            status=ProposalStatus(row[9]),
            reviewer_notes=cast(str, row[10]),
        )

    def create(self, proposal: ImprovementProposal) -> str:
        if type(proposal) is not ImprovementProposal:
            raise TypeError("proposal must be ImprovementProposal")
        checked = replace(proposal)
        if checked.status is not ProposalStatus.DRAFT or checked.reviewer_notes:
            raise ValueError("create requires a fresh DRAFT without reviewer notes")
        with self._transaction():
            self._connection.execute(
                f"INSERT INTO draft_improvement_proposals ({_COLUMNS}) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    checked.id,
                    checked.title,
                    checked.description,
                    _changes_json(checked.proposed_changes),
                    checked.target_component,
                    checked.impact_assessment,
                    checked.risk_level.value,
                    cast(SemanticVersion, checked.proposed_version).to_str(),
                    checked.created_at.isoformat(),
                    checked.status.value,
                    checked.reviewer_notes,
                ),
            )
        return checked.id

    def get(self, id: str) -> ImprovementProposal:
        _text("id", id)
        self._ensure_open()
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM draft_improvement_proposals WHERE id=?", (id,)
        ).fetchone()
        if row is None:
            raise KeyError(id)
        return self._decode(row)

    def submit(self, id: str) -> ImprovementProposal:
        """DRAFT -> PENDING_REVIEW plus Outbox enqueue in the same transaction.

        Recipient must be configured by the trusted fixture owner, never supplied
        by a proposal. Body is a structured notification intent; a future trusted
        renderer produces wording for Sir. No transport is called or receipt faked.
        """
        with self._transaction():
            proposal = self.get(id)
            if proposal.status is not ProposalStatus.DRAFT:
                raise ValueError("submit requires DRAFT")
            if self._sir_recipient is None:
                raise ValueError("sir_recipient must be configured before submit")
            now = self._clock()
            if type(now) is not int or not 0 <= now < 2**63:
                raise ValueError("clock must return exact nonnegative UTC epoch milliseconds")
            payload = {
                "event": "improvement_proposal.review_requested",
                "proposal_id": proposal.id,
                "title": proposal.title,
                "description": proposal.description,
                "proposed_changes": proposal.proposed_changes,
                "target_component": proposal.target_component,
                "impact_assessment": proposal.impact_assessment,
                "risk_level": proposal.risk_level.value,
                "proposed_version": cast(SemanticVersion, proposal.proposed_version).to_str(),
                "created_at": proposal.created_at.isoformat(),
                "status": ProposalStatus.PENDING_REVIEW.value,
            }
            self._connection.execute(
                "UPDATE draft_improvement_proposals SET status=? WHERE id=? AND status=?",
                (ProposalStatus.PENDING_REVIEW.value, id, ProposalStatus.DRAFT.value),
            )
            key = "improvement-proposal-review:" + id
            self._outbox.enqueue(
                OutboxMessage(
                    id=key,
                    channel=OutboxChannel.IMESSAGE,
                    recipient=self._sir_recipient,
                    subject=proposal.title,
                    body=json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
                    created_at=now,
                    scheduled_at=now,
                    idempotency_key=key,
                ),
                self._connection,
            )
            return replace(proposal, status=ProposalStatus.PENDING_REVIEW)

    def _review(self, id: str, notes: str, status: ProposalStatus) -> ImprovementProposal:
        _text("reviewer_notes", notes, blank=True)
        with self._transaction():
            proposal = self.get(id)
            if proposal.status is not ProposalStatus.PENDING_REVIEW:
                raise ValueError("human review requires PENDING_REVIEW")
            self._connection.execute(
                "UPDATE draft_improvement_proposals SET status=?,reviewer_notes=? "
                "WHERE id=? AND status=?",
                (status.value, notes, id, ProposalStatus.PENDING_REVIEW.value),
            )
            return replace(proposal, status=status, reviewer_notes=notes)

    def approve(self, id: str, notes: str) -> ImprovementProposal:
        """Offline trusted-caller assertion of manual approval, never auto-policy.

        This is not identity verification or permission to implement a change.
        NOT registered with model tools; production reviewer ingress is pending.
        """
        return self._review(id, notes, ProposalStatus.APPROVED)

    def reject(self, id: str, notes: str) -> ImprovementProposal:
        """Offline trusted-caller assertion of manual rejection; terminal decision."""
        return self._review(id, notes, ProposalStatus.REJECTED)

    def list_pending(self) -> list[ImprovementProposal]:
        self._ensure_open()
        return [
            self._decode(row)
            for row in self._connection.execute(
                f"SELECT {_COLUMNS} FROM draft_improvement_proposals "
                "WHERE status=? ORDER BY sequence",
                (ProposalStatus.PENDING_REVIEW.value,),
            )
        ]

    def history(self, component: str) -> list[ImprovementProposal]:
        """Current snapshots for a component, oldest append first; not deployments."""
        _text("component", component)
        self._ensure_open()
        return [
            self._decode(row)
            for row in self._connection.execute(
                f"SELECT {_COLUMNS} FROM draft_improvement_proposals "
                "WHERE target_component=? ORDER BY sequence",
                (component,),
            )
        ]
