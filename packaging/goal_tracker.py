"""Persistent caller-reported goals with a WAL-compatible durable audit outbox.

A goals.db transaction commits each mutation and its audit envelope together.
Mind delivery is idempotent by persisted operation key, reconciled at startup,
subsequent mutations, or explicitly. No cross-database atomicity is assumed:
https://www.sqlite.org/lang_attach.html and https://www.sqlite.org/wal.html.
No model or work is executed; reported progress is not verified task success.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sir_profile import PrivateStore, require_text, utc_now

if TYPE_CHECKING:
    from inner_loop import MindStore

Record = dict[str, Any]


def self_evaluate() -> dict[str, Any]:
    """Report persistence limits without opening goals.db or the mind ledger."""
    return {
        "module": "goal_tracker",
        "domain": "persistent goal tracking SQLite atomic audit evidence based progress",
        "current_version": (
            "SQLite goals with transactional audit outbox and idempotent WAL mind delivery"
        ),
        "known_limitations": [
            (
                "Progress and completion are caller-reported; success criteria are "
                "stored, not independently evaluated."
            ),
            "Mind audit delivery is eventual; failures retain durable pending envelopes.",
            (
                "Reconciliation runs on construction, later mutations or explicit call; "
                "a permanently unavailable mind store requires operator recovery."
            ),
            (
                "Read-only goal UI and explicit tool adapters exist outside this "
                "module; no verified automatic scheduler progress or KG sync is "
                "implemented here."
            ),
        ],
        "improvement_directions": [
            ("Add bounded outbox monitoring and retention without deleting pending audits."),
            (
                "Link progress and completion to verifiable task receipts and "
                "evaluated success criteria."
            ),
            (
                "Add explicit knowledge-graph projection and user-facing goal "
                "controls with integration tests."
            ),
        ],
        "last_evaluated_at": utc_now(),
    }


class GoalTracker(PrivateStore):
    """Track task objectives separately from the continuous user timeline."""

    def __init__(self, data_root: Path, mind_store: MindStore) -> None:
        root = Path(data_root)
        if Path(mind_store.path) != root / "mind.db":
            raise ValueError("MindStore must belong to the goal data root")
        super().__init__(root, "goals.db")
        self.data_root = self.root
        self.mind_store = mind_store
        with self.connection(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS goals (
                goal_id TEXT PRIMARY KEY, goal TEXT NOT NULL,
                progress REAL NOT NULL CHECK(progress BETWEEN 0 AND 1),
                completed INTEGER NOT NULL CHECK(completed IN (0,1)),
                success_criteria TEXT NOT NULL, notes TEXT NOT NULL,
                summary TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                CHECK(completed = 0 OR progress = 1)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS goals_active ON goals(completed,created_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS goal_audit_outbox (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_key TEXT NOT NULL UNIQUE,
                goal_id TEXT NOT NULL REFERENCES goals(goal_id),
                category TEXT NOT NULL, created_at TEXT NOT NULL,
                payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
                    CHECK(delivered IN (0,1))
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS goal_audit_pending ON goal_audit_outbox(delivered,seq)"
            )
        self._try_reconcile()

    @staticmethod
    def _status(row: sqlite3.Row) -> Record:
        return {
            "goal_id": row["goal_id"],
            "goal": row["goal"],
            "progress": float(row["progress"]),
            "completed": bool(row["completed"]),
            "success_criteria": json.loads(row["success_criteria"]),
            "notes": json.loads(row["notes"]),
            "created_at": row["created_at"],
        }

    def _audit(self, db: sqlite3.Connection, category: str, payload: Record) -> None:
        """Enqueue before the goal transaction commits; no attached DB writes."""
        operation_key = uuid.uuid4().hex
        body = json.dumps(
            {**payload, "operation_key": operation_key}, ensure_ascii=False, allow_nan=False
        )
        if len(body.encode("utf-8")) > 524288:
            raise ValueError("Mind entry exceeds limit")
        db.execute(
            "INSERT INTO goal_audit_outbox(operation_key,goal_id,category,created_at,payload) "
            "VALUES(?,?,?,?,?)",
            (operation_key, payload["goal_id"], category, utc_now(), body),
        )

    def reconcile_audits(self) -> int:
        """Deliver pending envelopes exactly once by entry ID; return ack count.

        Hold the goals writer lock across read/delivery/ack to serialize owners.
        Mind commits FIRST, with FULL durability. A crash before local ack replays
        the identical envelope, not another insert. Never erase an envelope or
        silently accept an ID collision. This is eventual, not cross-DB atomicity.
        """
        count = 0
        with self.connection(write=True) as goals:
            for row in goals.execute(
                "SELECT * FROM goal_audit_outbox WHERE delivered=0 ORDER BY seq"
            ).fetchall():
                envelope = (
                    "goal-audit:" + row["operation_key"],
                    row["category"],
                    row["created_at"],
                    "goal-tracker",
                    row["payload"],
                    "[]",
                )
                with self.mind_store.connection() as mind:
                    mind.execute("PRAGMA synchronous=FULL")
                    mind.execute("BEGIN IMMEDIATE")
                    existing = mind.execute(
                        "SELECT id,category,created_at,source,payload,refs FROM entries WHERE id=?",
                        (envelope[0],),
                    ).fetchone()
                    if existing is None:
                        mind.execute(
                            "INSERT INTO entries(id,category,created_at,source,payload,refs) "
                            "VALUES(?,?,?,?,?,?)",
                            envelope,
                        )
                    elif tuple(existing) != envelope:
                        raise ValueError("Goal audit operation key conflict")
                # The context above commits before this acknowledgement. Holding
                # only a single DB writer at each commit is intentional under WAL.
                goals.execute("UPDATE goal_audit_outbox SET delivered=1 WHERE seq=?", (row["seq"],))
                count += 1
        return count

    def _try_reconcile(self) -> None:
        try:
            self.reconcile_audits()
        except sqlite3.Error as exc:
            logging.getLogger(__name__).warning(
                "Goal audit delivery pending; durable envelopes retained (%s)", type(exc).__name__
            )

    def _completion_recorded(self, db: sqlite3.Connection, goal_id: str, completed: bool) -> bool:
        if db.execute(
            "SELECT 1 FROM goal_audit_outbox WHERE goal_id=? AND category='goal_completed' LIMIT 1",
            (goal_id,),
        ).fetchone():
            return True
        # A still-active goal cannot have a legitimate legacy completion entry.
        # Completed legacy rows still consult the original immutable mind audit;
        # a newer progress envelope must not mask that earlier completion.
        if not completed:
            return False
        # Older DELETE-mode versions had no local outbox. Preserve their
        # completion idempotency without mutating/backfilling legacy entries.
        with self.mind_store.connection() as mind:
            return (
                mind.execute(
                    "SELECT 1 FROM entries WHERE category='goal_completed' "
                    "AND json_extract(payload,'$.goal_id')=? LIMIT 1",
                    (goal_id,),
                ).fetchone()
                is not None
            )

    def set_goal(self, goal: str, success_criteria: list[str] | None = None) -> str:
        """Create a goal and goal_set entry atomically. Criteria are not evaluated."""
        require_text(goal, "Goal")
        criteria = [] if success_criteria is None else success_criteria
        if type(criteria) is not list:
            raise ValueError("Success criteria must be a list of nonempty strings")
        criteria = [require_text(item, "Success criterion") for item in criteria]
        goal_id = uuid.uuid4().hex
        now = utc_now()
        with self.connection(write=True) as db:
            db.execute(
                "INSERT INTO goals(goal_id,goal,progress,completed,success_criteria,notes,"
                "summary,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    goal_id,
                    goal,
                    0.0,
                    0,
                    json.dumps(criteria, ensure_ascii=False),
                    "[]",
                    "",
                    now,
                    now,
                ),
            )
            self._audit(
                db,
                "goal_set",
                {
                    "goal_id": goal_id,
                    "goal": goal,
                    "success_criteria": criteria,
                },
            )
        self._try_reconcile()
        return goal_id

    def get_status(self, goal_id: str) -> Record:
        """Return persisted status; unknown IDs raise KeyError."""
        require_text(goal_id, "Goal ID")
        with self.connection() as db:
            row = db.execute("SELECT * FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
            if row is None:
                raise KeyError(goal_id)
            return self._status(row)

    def update_progress(self, goal_id: str, progress: float, note: str = "") -> Record:
        """Set reported progress; >=1 completes, negative/nonfinite values fail.

        Completed goals cannot be reopened by a stale progress update. A repeated
        identical update is a no-op (including its audit entry).
        """
        require_text(goal_id, "Goal ID")
        if (
            type(progress) not in (int, float)
            or not math.isfinite(progress)
            or progress < 0
            or type(note) is not str
        ):
            raise ValueError("Progress must be finite and nonnegative; note must be text")
        progress = min(float(progress), 1.0)
        completed = progress >= 1.0
        self._try_reconcile()
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
            if row is None:
                raise KeyError(goal_id)
            if row["completed"] and not completed:
                raise ValueError("Completed goals cannot be reopened")
            notes: list[str] = json.loads(row["notes"])
            changed_note = bool(note) and (not notes or notes[-1] != note)
            if row["progress"] == progress and not changed_note:
                return {"goal_id": goal_id, "progress": progress, "completed": completed}
            if changed_note:
                notes.append(note)
            db.execute(
                "UPDATE goals SET progress=?,completed=?,notes=?,updated_at=? WHERE goal_id=?",
                (
                    progress,
                    int(completed),
                    json.dumps(notes, ensure_ascii=False),
                    utc_now(),
                    goal_id,
                ),
            )
            self._audit(
                db,
                "goal_progress",
                {
                    "goal_id": goal_id,
                    "progress": progress,
                    "completed": completed,
                    "note": note,
                },
            )
        self._try_reconcile()
        return {"goal_id": goal_id, "progress": progress, "completed": completed}

    def list_active_goals(self) -> list[Record]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM goals WHERE completed=0 ORDER BY created_at DESC,goal_id DESC"
            ).fetchall()
            return [self._status(row) for row in rows]

    def complete(self, goal_id: str, summary: str = "") -> Record:
        """Manually complete and audit once; conflicting repeat summaries fail."""
        require_text(goal_id, "Goal ID")
        if type(summary) is not str:
            raise ValueError("Summary must be text")
        self._try_reconcile()
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
            if row is None:
                raise KeyError(goal_id)
            recorded = self._completion_recorded(db, goal_id, bool(row["completed"]))
            if recorded:
                if summary and summary != row["summary"]:
                    raise ValueError("Completion summary conflicts with previous completion")
                return {"goal_id": goal_id, "completed": True, "summary": row["summary"]}
            notes: list[str] = json.loads(row["notes"])
            if summary:
                notes.append(summary)
            db.execute(
                "UPDATE goals SET progress=1,completed=1,summary=?,notes=?,updated_at=? "
                "WHERE goal_id=?",
                (summary, json.dumps(notes, ensure_ascii=False), utc_now(), goal_id),
            )
            self._audit(db, "goal_completed", {"goal_id": goal_id, "summary": summary})
        self._try_reconcile()
        return {"goal_id": goal_id, "completed": True, "summary": summary}
