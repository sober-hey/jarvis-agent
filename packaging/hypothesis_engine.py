"""Persistent hypothesis lifecycle; callers adjudicate evidence relevance.

Each distinct supporting entry adds .08 confidence; each opposing entry
subtracts .15 (clamped to [0,1]). IDs are deduplicated and the score is always
recomputed from the original prior, so batching/retries cannot inflate it.
Conditions are retained as descriptions, NOT evaluated as executable text or
silently interpreted by a model. The caller must check them when selecting
evidence. No background collection or model/network call is installed here.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from sir_profile import (
    PrivateStore,
    Record,
    SirProfile,
    decode_record,
    require_text,
    utc_now,
    validate_confidence,
    validate_evidence_ids,
    validate_topic,
)

if TYPE_CHECKING:
    from knowledge_graph import KnowledgeGraph

logger = logging.getLogger(__name__)


class HypothesisEngine(PrivateStore):
    CONFIRM_THRESHOLD = 0.85
    REFUTE_THRESHOLD = 0.35
    SUPPORT_WEIGHT = 0.08
    OPPOSITION_WEIGHT = 0.15

    def __init__(self, data_root: Path, knowledge_graph: KnowledgeGraph | None = None) -> None:
        super().__init__(data_root, "hypotheses.db")
        self.kg = knowledge_graph
        self.profile = SirProfile(self.root, knowledge_graph=knowledge_graph)
        with self.connection(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS hypotheses (
                id TEXT PRIMARY KEY, category TEXT NOT NULL, key TEXT NOT NULL,
                claim TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                status TEXT NOT NULL CHECK(status IN
                    ('pending','confirmed','refuted','insufficient_evidence')),
                evidence_for TEXT NOT NULL, evidence_against TEXT NOT NULL,
                confirm_condition TEXT NOT NULL, refute_condition TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL)""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS hypotheses_status ON hypotheses(status,expires_at)"
            )
            db.execute("""CREATE TABLE IF NOT EXISTS hypothesis_priors (
                hypothesis_id TEXT PRIMARY KEY REFERENCES hypotheses(id),
                confidence REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS hypothesis_events (
                id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL REFERENCES hypotheses(id),
                status TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL)""")

    def self_evaluate(self) -> dict[str, Any]:
        """Describe implemented behavior and gaps; never inspect runtime records."""
        return {
            "module": "hypothesis_engine",
            "domain": "personal hypothesis evidence confidence lifecycle",
            "current_version": "SQLite hypothesis lifecycle with deduplicated evidence IDs, fixed +0.08/-0.15 weights, threshold confirmation and profile transactions",
            "known_limitations": [
                "Evidence relevance and textual confirmation/refutation conditions are adjudicated by the caller, not executed or verified here.",
                "Fixed weights and thresholds are heuristic, not calibrated probabilities or causal inference.",
                "Knowledge graph projection is best-effort after commit; this module installs no collection or expiry scheduler.",
            ],
            "improvement_directions": [
                "Evaluate confidence calibration against labelled longitudinal evidence without treating correlation as causation.",
                "Add caller-level evidence relevance and lifecycle coverage before enabling autonomous collection.",
                "Explore recoverable graph projection reconciliation for post-commit failures.",
            ],
            "last_evaluated_at": datetime.now(UTC).isoformat(),
        }

    def propose(
        self,
        category: str,
        key: str,
        claim: str,
        confidence: float,
        confirm_condition: str,
        refute_condition: str,
        evidence_ids: Sequence[str] | None = None,
        ttl_days: float = 30,
    ) -> str:
        validate_topic(category, key)
        require_text(claim, "Claim")
        require_text(confirm_condition, "Confirmation condition")
        require_text(refute_condition, "Refutation condition")
        confidence = validate_confidence(confidence)
        evidence = validate_evidence_ids(evidence_ids)
        if type(ttl_days) not in (int, float) or not math.isfinite(ttl_days) or ttl_days <= 0:
            raise ValueError("TTL must be a positive finite number of days")
        now = datetime.now(UTC)
        expires_at = (now + timedelta(days=ttl_days)).isoformat()
        identity = uuid.uuid4().hex
        # The supplied confidence already accounts for the initial evidence.
        prior = confidence - self.SUPPORT_WEIGHT * len(evidence)
        with self.connection(write=True) as db:
            db.execute(
                "INSERT INTO hypotheses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    category,
                    key,
                    claim,
                    confidence,
                    "pending",
                    json.dumps(evidence, ensure_ascii=False),
                    "[]",
                    confirm_condition,
                    refute_condition,
                    now.isoformat(),
                    now.isoformat(),
                    expires_at,
                ),
            )
            db.execute("INSERT INTO hypothesis_priors VALUES (?,?)", (identity, prior))
            self._event(db, identity, "pending", "proposed")
        self._sync_committed(identity)
        return identity

    def _sync_committed(self, hypothesis_id: str, *, sync_profile: bool = False) -> None:
        """Project committed state, including lifecycle changes made by evidence."""
        if self.kg is None:
            return
        try:
            row = self.get(hypothesis_id)
            support, oppose = row["evidence_for"], row["evidence_against"]
            self.kg.add_node(
                "hypothesis",
                row["category"],
                row["key"],
                row["claim"],
                row["confidence"],
                "hypothesis",
                support + oppose,
                origin_table="hypotheses",
                origin_id=row["id"],
                metadata={
                    "status": row["status"],
                    "confirm_condition": row["confirm_condition"],
                    "refute_condition": row["refute_condition"],
                    "expires_at": row["expires_at"],
                    "evidence_for": support,
                    "evidence_against": oppose,
                    "active": row["status"] in {"pending", "confirmed"},
                    "source_db": str(self.path),
                },
            )
        except Exception as exc:
            logger.warning("Knowledge graph hypothesis write failed: %s", type(exc).__name__)
        if sync_profile:
            # Confirmation/downgrade writes facts via the attached transaction,
            # not SirProfile.upsert; mirror them only after that commit too.
            try:
                row = self.get(hypothesis_id)
                for fact in self.profile.get(row["category"], row["key"]):
                    self.profile.sync_committed_fact(fact)
            except Exception as exc:
                logger.warning("Knowledge graph profile sync failed: %s", type(exc).__name__)

    @staticmethod
    def _row(db: sqlite3.Connection, hypothesis_id: str) -> sqlite3.Row:
        require_text(hypothesis_id, "Hypothesis ID")
        row = db.execute("SELECT * FROM hypotheses WHERE id=?", (hypothesis_id,)).fetchone()
        if row is None:
            raise KeyError(hypothesis_id)
        return row

    @staticmethod
    def _event(db: sqlite3.Connection, identity: str, status: str, reason: str) -> None:
        db.execute(
            "INSERT INTO hypothesis_events VALUES (?,?,?,?,?)",
            (uuid.uuid4().hex, identity, status, reason, utc_now()),
        )

    def _profile_note(self, identity: str) -> str:
        return f"Confirmed hypothesis: {identity}"

    def _downgrade_profile(
        self, db: sqlite3.Connection, row: sqlite3.Row, confidence: float
    ) -> None:
        # Do not weaken a newer, independent assertion or a direct self-report.
        db.execute(
            """UPDATE profile.facts SET confidence=?,updated_at=?
            WHERE category=? AND key=? AND value=? AND source='inferred' AND notes=?""",
            (
                confidence,
                utc_now(),
                row["category"],
                row["key"],
                row["claim"],
                self._profile_note(row["id"]),
            ),
        )

    def _confirm_in(self, db: sqlite3.Connection, row: sqlite3.Row) -> bool:
        already_confirmed = row["status"] == "confirmed"
        if row["status"] not in {"pending", "confirmed"}:
            return False
        if row["confidence"] <= self.CONFIRM_THRESHOLD:
            return False
        support = json.loads(row["evidence_for"])
        if not support or (not already_confirmed and row["expires_at"] <= utc_now()):
            return False
        fact = self.profile.upsert_in_transaction(
            db,
            row["category"],
            row["key"],
            row["claim"],
            row["confidence"],
            "inferred",
            support,
            self._profile_note(row["id"]),
            schema="profile",
        )
        if not fact["applied"]:
            if already_confirmed:
                db.execute(
                    "UPDATE hypotheses SET status='pending',updated_at=? WHERE id=?",
                    (utc_now(), row["id"]),
                )
                self._event(db, row["id"], "pending", "profile has a conflicting stronger source")
            return False
        if already_confirmed:
            return True
        db.execute(
            "UPDATE hypotheses SET status='confirmed',updated_at=? WHERE id=?",
            (utc_now(), row["id"]),
        )
        self._event(db, row["id"], "confirmed", "confidence threshold with supporting evidence")
        return True

    def update_evidence(
        self,
        hypothesis_id: str,
        supporting_ids: Sequence[str] | None = None,
        opposing_ids: Sequence[str] | None = None,
    ) -> Record:
        support, oppose = validate_evidence_ids(supporting_ids), validate_evidence_ids(opposing_ids)
        # ATTACH + rollback journals commit the profile and lifecycle together.
        with self.connection(write=True, profile_path=self.profile.path) as db:
            row = self._row(db, hypothesis_id)
            if row["status"] in {"refuted", "insufficient_evidence"}:
                return decode_record(row, ("evidence_for", "evidence_against"))
            all_for = validate_evidence_ids(json.loads(row["evidence_for"]) + support)
            all_against = validate_evidence_ids(json.loads(row["evidence_against"]) + oppose)
            if set(all_for) & set(all_against):
                raise ValueError("The same entry cannot support and oppose one hypothesis")
            if all_for == json.loads(row["evidence_for"]) and all_against == json.loads(
                row["evidence_against"]
            ):
                return decode_record(row, ("evidence_for", "evidence_against"))
            prior = db.execute(
                "SELECT confidence FROM hypothesis_priors WHERE hypothesis_id=?", (hypothesis_id,)
            ).fetchone()[0]
            confidence = min(
                1.0,
                max(
                    0.0,
                    prior
                    + len(all_for) * self.SUPPORT_WEIGHT
                    - len(all_against) * self.OPPOSITION_WEIGHT,
                ),
            )
            db.execute(
                """UPDATE hypotheses SET confidence=?,evidence_for=?,evidence_against=?,
                updated_at=? WHERE id=?""",
                (
                    confidence,
                    json.dumps(all_for, ensure_ascii=False),
                    json.dumps(all_against, ensure_ascii=False),
                    utc_now(),
                    hypothesis_id,
                ),
            )
            self._event(db, hypothesis_id, row["status"], "evidence updated")
            if all_against and confidence <= self.REFUTE_THRESHOLD:
                db.execute("UPDATE hypotheses SET status='refuted' WHERE id=?", (hypothesis_id,))
                self._downgrade_profile(db, row, 0.0)
                self._event(
                    db, hypothesis_id, "refuted", "opposing evidence below refutation threshold"
                )
            else:
                if row["status"] == "confirmed":
                    self._downgrade_profile(db, row, confidence)
                    if confidence <= self.CONFIRM_THRESHOLD:
                        db.execute(
                            "UPDATE hypotheses SET status='pending' WHERE id=?", (hypothesis_id,)
                        )
                        self._event(
                            db, hypothesis_id, "pending", "confidence no longer confirms claim"
                        )
                self._confirm_in(db, self._row(db, hypothesis_id))
            result = decode_record(
                self._row(db, hypothesis_id), ("evidence_for", "evidence_against")
            )
        self._sync_committed(hypothesis_id, sync_profile=True)
        return result

    def try_confirm(self, hypothesis_id: str) -> bool:
        with self.connection(write=True, profile_path=self.profile.path) as db:
            confirmed = self._confirm_in(db, self._row(db, hypothesis_id))
        self._sync_committed(hypothesis_id, sync_profile=True)
        return confirmed

    def try_refute(self, hypothesis_id: str, reason: str) -> bool:
        require_text(reason, "Refutation reason")
        with self.connection(write=True, profile_path=self.profile.path) as db:
            row = self._row(db, hypothesis_id)
            if row["status"] != "refuted":
                # A reason alone is not evidence; require a registered opposing entry.
                if row["status"] == "insufficient_evidence" or not json.loads(
                    row["evidence_against"]
                ):
                    return False
                db.execute(
                    "UPDATE hypotheses SET status='refuted',updated_at=? WHERE id=?",
                    (utc_now(), hypothesis_id),
                )
                self._event(db, hypothesis_id, "refuted", reason)
                self._downgrade_profile(db, row, 0.0)
        self._sync_committed(hypothesis_id, sync_profile=True)
        return True

    def expire_stale(self) -> int:
        now = utc_now()
        with self.connection(write=True) as db:
            rows = db.execute(
                """SELECT id FROM hypotheses WHERE status='pending'
                AND expires_at<=? AND confidence<0.5""",
                (now,),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE hypotheses SET status='insufficient_evidence',updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                self._event(
                    db, row["id"], "insufficient_evidence", "expired below confidence floor"
                )
        for row in rows:
            self._sync_committed(row["id"])
        return len(rows)

    def get(self, hypothesis_id: str) -> Record:
        with self.connection() as db:
            return decode_record(self._row(db, hypothesis_id), ("evidence_for", "evidence_against"))

    def _list(self, status: str) -> list[Record]:
        with self.connection() as db:
            return [
                decode_record(row, ("evidence_for", "evidence_against"))
                for row in db.execute(
                    "SELECT * FROM hypotheses WHERE status=? ORDER BY created_at,id", (status,)
                )
            ]

    def list_pending(self) -> list[Record]:
        return self._list("pending")

    def list_confirmed(self) -> list[Record]:
        return self._list("confirmed")
