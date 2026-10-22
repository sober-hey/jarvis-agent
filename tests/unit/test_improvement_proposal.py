"""K4-005 offline proposal fixtures, never live iMessage or execution grants."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from importlib import import_module, util
from pathlib import Path
from typing import Any

import pytest

from jarvis.outbox import OutboxChannel, OutboxRelay, OutboxStatus


def _module() -> Any:
    return import_module("jarvis.improvement_proposal")


def _proposal(**changes: Any) -> Any:
    module = _module()
    fields: dict[str, Any] = {
        "id": "proposal-1",
        "title": "Improve lookup cost",
        "description": "Repeated history scans grow with the ledger.",
        "proposed_changes": {"query": {"limit": 64}, "preserve_history": True},
        "target_component": "memory",
        "impact_assessment": "Bound read cost without deleting historical entries.",
        "risk_level": module.RiskLevel.MEDIUM,
        "proposed_version": "1.1.0",
        "created_at": datetime(2026, 10, 18, tzinfo=UTC),
    }
    fields.update(changes)
    return module.ImprovementProposal(**fields)


def test_proposal_draft_module_exists() -> None:
    assert util.find_spec("jarvis.improvement_proposal") is not None


def test_create_persists_detached_draft(tmp_path: Path) -> None:
    module = _module()
    proposal = _proposal()
    with module.ProposalManager(tmp_path / "proposals.db", draft_fixture=True) as manager:
        assert manager.create(proposal) == proposal.id
        assert manager.get(proposal.id) == proposal
        proposal.proposed_changes["query"]["limit"] = 1
        saved = manager.get(proposal.id)
        assert saved.proposed_changes["query"]["limit"] == 64
        assert saved.status is module.ProposalStatus.DRAFT
        assert saved.reviewer_notes == ""
        assert saved.proposed_version.to_str() == "1.1.0"
        assert manager.history("memory") == [
            replace(proposal, proposed_changes=saved.proposed_changes)
        ]
        assert module.RUNTIME_ENABLED is False
        assert manager.runtime_enabled is False


@pytest.mark.parametrize("risk", ["LOW", "MEDIUM", "HIGH"])
def test_submit_queues_imessage_and_survives_reopen(tmp_path: Path, risk: str) -> None:
    module = _module()
    database = tmp_path / "proposals.db"
    now = 1_800_000_000_000
    proposal = _proposal(risk_level=module.RiskLevel(risk))
    relay = OutboxRelay(draft_fixture=True, clock=lambda: now)
    with module.ProposalManager(
        database, draft_fixture=True, sir_recipient="sir-fixture", clock=lambda: now
    ) as manager:
        manager.create(proposal)
        manager.submit(proposal.id)
        assert manager.get(proposal.id).status is module.ProposalStatus.PENDING_REVIEW
        with sqlite3.connect(database) as observer:
            messages = relay.poll(observer)
            assert len(messages) == 1
            message = messages[0]
            assert message.channel is OutboxChannel.IMESSAGE
            assert message.recipient == "sir-fixture"
            assert message.status is OutboxStatus.PENDING
            assert message.created_at == now
            assert message.scheduled_at == now
            assert message.attempts == 0
            payload = json.loads(message.body)
            assert payload["event"] == "improvement_proposal.review_requested"
            assert payload["proposal_id"] == proposal.id
            assert payload["target_component"] == proposal.target_component
            assert payload["risk_level"] == risk
            assert payload["proposed_version"] == "1.1.0"
            assert payload["proposed_changes"] == proposal.proposed_changes
            assert payload["impact_assessment"] == proposal.impact_assessment
        with pytest.raises(ValueError, match="DRAFT"):
            manager.submit(proposal.id)
    with module.ProposalManager(database, draft_fixture=True) as reopened:
        assert reopened.get(proposal.id).status is module.ProposalStatus.PENDING_REVIEW
        with sqlite3.connect(database) as observer:
            assert relay.poll(observer) == messages


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_manual_review_only_changes_pending_and_persists_notes(
    tmp_path: Path, decision: str
) -> None:
    module = _module()
    database = tmp_path / "proposals.db"
    expected = (
        module.ProposalStatus.APPROVED if decision == "approve" else module.ProposalStatus.REJECTED
    )
    with module.ProposalManager(
        database, draft_fixture=True, sir_recipient="sir-fixture"
    ) as manager:
        manager.create(_proposal())
        with pytest.raises(ValueError, match="PENDING_REVIEW"):
            getattr(manager, decision)("proposal-1", "not submitted")
        manager.submit("proposal-1")
        result = getattr(manager, decision)("proposal-1", "Manual fixture review notes")
        assert result.status is expected
        assert result.reviewer_notes == "Manual fixture review notes"
        assert manager.list_pending() == []
        for method in ("approve", "reject", "submit"):
            with pytest.raises(ValueError):
                arguments = ("proposal-1",) if method == "submit" else ("proposal-1", "overwrite")
                getattr(manager, method)(*arguments)
    with module.ProposalManager(database, draft_fixture=True) as manager:
        assert manager.get("proposal-1") == result


def test_list_pending_and_component_history_are_filtered(tmp_path: Path) -> None:
    module = _module()
    with module.ProposalManager(
        tmp_path / "proposals.db", draft_fixture=True, sir_recipient="sir-fixture"
    ) as manager:
        for id in ("draft", "pending", "approved", "rejected", "other-pending"):
            component = "other" if id == "other-pending" else "memory"
            manager.create(_proposal(id=id, target_component=component))
            if id != "draft":
                manager.submit(id)
        manager.approve("approved", "Reviewed")
        manager.reject("rejected", "Insufficient benefit")
        assert [proposal.id for proposal in manager.list_pending()] == ["pending", "other-pending"]
        assert [proposal.id for proposal in manager.history("memory")] == [
            "draft",
            "pending",
            "approved",
            "rejected",
        ]
        assert manager.history("unknown") == []


def test_submit_outbox_failure_rolls_back_both_changes(tmp_path: Path) -> None:
    module = _module()
    database = tmp_path / "proposals.db"
    with module.ProposalManager(
        database, draft_fixture=True, sir_recipient="sir-fixture"
    ) as manager:
        manager.create(_proposal())
        with sqlite3.connect(database) as observer:
            observer.execute(
                "CREATE TRIGGER fail_enqueue BEFORE INSERT ON outbox "
                "BEGIN SELECT RAISE(ABORT,'injected outbox failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected outbox failure"):
            manager.submit("proposal-1")
        assert manager.get("proposal-1").status is module.ProposalStatus.DRAFT
        with sqlite3.connect(database) as observer:
            assert observer.execute("SELECT count(*) FROM outbox").fetchone() == (0,)
            assert observer.execute(
                "SELECT status FROM draft_improvement_proposals"
            ).fetchone() == ("DRAFT",)
            observer.execute("DROP TRIGGER fail_enqueue")
        manager.submit("proposal-1")
        assert len(manager.list_pending()) == 1


def test_unconfigured_recipient_cannot_lose_a_review_notification(tmp_path: Path) -> None:
    module = _module()
    database = tmp_path / "proposals.db"
    with module.ProposalManager(database, draft_fixture=True) as manager:
        manager.create(_proposal())
        with pytest.raises(ValueError, match="sir_recipient"):
            manager.submit("proposal-1")
        assert manager.get("proposal-1").status is module.ProposalStatus.DRAFT
        with sqlite3.connect(database) as observer:
            assert observer.execute("SELECT count(*) FROM outbox").fetchone() == (0,)


@pytest.mark.parametrize("status", ["PENDING_REVIEW", "APPROVED", "REJECTED", "IMPLEMENTED"])
def test_create_cannot_import_a_reviewed_or_implemented_status(tmp_path: Path, status: str) -> None:
    module = _module()
    with module.ProposalManager(tmp_path / "proposals.db", draft_fixture=True) as manager:
        with pytest.raises(ValueError, match="fresh DRAFT"):
            manager.create(_proposal(status=module.ProposalStatus(status)))
        assert manager.history("memory") == []


def test_duplicate_id_never_replaces_proposal(tmp_path: Path) -> None:
    module = _module()
    proposal = _proposal()
    with module.ProposalManager(tmp_path / "proposals.db", draft_fixture=True) as manager:
        manager.create(proposal)
        with pytest.raises(sqlite3.IntegrityError):
            manager.create(_proposal(title="Different payload"))
        assert manager.get(proposal.id) == proposal
        manager.create(_proposal(id="second"))
        assert len(manager.history("memory")) == 2


@pytest.mark.parametrize("method", ["get", "submit", "approve", "reject"])
def test_missing_proposal_is_explicit(tmp_path: Path, method: str) -> None:
    module = _module()
    with module.ProposalManager(tmp_path / "proposals.db", draft_fixture=True) as manager:
        arguments = ("missing", "Reviewed") if method in ("approve", "reject") else ("missing",)
        with pytest.raises(KeyError):
            getattr(manager, method)(*arguments)


def test_opt_in_and_standalone_database_are_required(tmp_path: Path) -> None:
    module = _module()
    database = tmp_path / "not-created.db"
    with pytest.raises(ValueError, match="draft_fixture"):
        module.ProposalManager(database)
    assert not database.exists()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO tasks VALUES('preserve')")
    with pytest.raises(ValueError, match="standalone"):
        module.ProposalManager(database, draft_fixture=True)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM tasks").fetchall() == [("preserve",)]
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == [
            ("tasks",),
            ("sqlite_autoindex_tasks_1",),
        ]


@pytest.mark.parametrize(
    "changes",
    [
        {"title": " "},
        {"description": ""},
        {"target_component": "memory\x00unsafe"},
        {"impact_assessment": ""},
        {"risk_level": "LOW"},
        {"proposed_version": "v1.1.0"},
        {"proposed_version": "1.01.0"},
        {"created_at": datetime(2026, 10, 18)},
        {"status": "APPROVED"},
        {"proposed_changes": {"value": float("nan")}},
        {"proposed_changes": {1: "non-string key"}},
        {"proposed_changes": {"value": (1, 2)}},
    ],
)
def test_proposal_validation_preserves_typed_boundary(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _proposal(**changes)


def test_create_revalidates_mutated_nested_json(tmp_path: Path) -> None:
    module = _module()
    proposal = _proposal()
    proposal.proposed_changes["mutated"] = float("inf")
    with module.ProposalManager(tmp_path / "proposals.db", draft_fixture=True) as manager:
        with pytest.raises(ValueError):
            manager.create(proposal)
        assert manager.history("memory") == []


def test_history_is_append_order_not_caller_time(tmp_path: Path) -> None:
    module = _module()
    with module.ProposalManager(tmp_path / "proposals.db", draft_fixture=True) as manager:
        manager.create(_proposal(id="first"))
        manager.create(_proposal(id="second", created_at=datetime(2020, 1, 1, tzinfo=UTC)))
        assert [proposal.id for proposal in manager.history("memory")] == ["first", "second"]


def test_manager_close_is_idempotent(tmp_path: Path) -> None:
    module = _module()
    manager = module.ProposalManager(tmp_path / "proposals.db", draft_fixture=True)
    manager.close()
    manager.close()
    with pytest.raises(RuntimeError, match="closed"):
        manager.list_pending()


@pytest.mark.parametrize("clock_value", [True, 1.5, -1, 2**63])
def test_invalid_clock_cannot_partially_submit(tmp_path: Path, clock_value: Any) -> None:
    module = _module()
    with module.ProposalManager(
        tmp_path / "proposals.db",
        draft_fixture=True,
        sir_recipient="sir-fixture",
        clock=lambda: clock_value,
    ) as manager:
        manager.create(_proposal())
        with pytest.raises(ValueError, match="clock"):
            manager.submit("proposal-1")
        assert manager.get("proposal-1").status is module.ProposalStatus.DRAFT


def test_cycles_are_rejected_before_recursive_validation() -> None:
    changes: dict[str, Any] = {}
    changes["self"] = changes
    with pytest.raises(ValueError, match="Circular"):
        _proposal(proposed_changes=changes)


def test_reopening_rejects_same_named_weakened_schema(tmp_path: Path) -> None:
    module = _module()
    database = tmp_path / "proposals.db"
    with module.ProposalManager(database, draft_fixture=True):
        pass
    with sqlite3.connect(database) as observer:
        observer.execute("DROP INDEX draft_proposal_pending")
    with pytest.raises(ValueError, match="schema mismatch"):
        module.ProposalManager(database, draft_fixture=True)


def test_review_cannot_rewrite_pending_payload_or_caller_snapshot(tmp_path: Path) -> None:
    module = _module()
    with module.ProposalManager(
        tmp_path / "proposals.db", draft_fixture=True, sir_recipient="sir-fixture"
    ) as manager:
        proposal = _proposal()
        manager.create(proposal)
        pending = manager.submit(proposal.id)
        pending.proposed_changes["query"]["limit"] = 999
        result = manager.approve(proposal.id, "Manual review")
        assert result.proposed_changes == proposal.proposed_changes
        assert proposal.status is module.ProposalStatus.DRAFT


@pytest.mark.parametrize("operation", ["submit", "review"])
def test_independent_managers_only_one_transition_wins(tmp_path: Path, operation: str) -> None:
    module = _module()
    database = tmp_path / "proposals.db"
    with module.ProposalManager(
        database, draft_fixture=True, sir_recipient="sir-fixture"
    ) as manager:
        manager.create(_proposal())
        if operation == "review":
            manager.submit("proposal-1")

    def attempt(decision: str) -> str:
        with module.ProposalManager(
            database, draft_fixture=True, sir_recipient="sir-fixture"
        ) as manager:
            try:
                if operation == "submit":
                    manager.submit("proposal-1")
                else:
                    getattr(manager, decision)("proposal-1", decision)
            except ValueError:
                return "lost"
            return "won"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, ["approve", "reject"]))
    assert sorted(outcomes) == ["lost", "won"]
    with sqlite3.connect(database) as observer:
        assert observer.execute("SELECT count(*) FROM outbox").fetchone() == (1,)
    with module.ProposalManager(database, draft_fixture=True) as manager:
        saved = manager.get("proposal-1")
        if operation == "submit":
            assert saved.status is module.ProposalStatus.PENDING_REVIEW
        else:
            assert saved.status in (module.ProposalStatus.APPROVED, module.ProposalStatus.REJECTED)
            assert saved.reviewer_notes in ("approve", "reject")
