"""Non-blocking, proposal-only weekly research scheduling.

Call tick() once per daemon iteration. Normal ticks compare in-memory time only;
all filesystem/SQLite/search/message work happens in a single background thread.
Monday 00:00 Asia/Shanghai matches SelfEvolution.run_weekly_if_due(). Overdue
runs/retries retain that Monday as their logical date, even after Monday ends.

No approval, installation, core patching or recipient configuration is exposed.
Enqueue is not delivery: the host remains responsible for dispatch and receipts.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
WEEK_SECONDS = 7 * 24 * 60 * 60
RETRY_SECONDS = 6 * 60 * 60


def _number(value: Any) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("finite numeric schedule timestamp required")
    return float(value)


def _monday(now: float) -> float:
    local = datetime.fromtimestamp(now, CST)
    monday = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if local.weekday() != 0:
        monday += timedelta(days=7 - local.weekday())
    return monday.timestamp()


class EvolutionScheduler:
    def __init__(
        self,
        root: Path | str,
        self_evolution: Any,
        outreach: Any,
        clock: Callable[[], float] = time.time,
        *,
        interval_weeks: int | None = None,
    ) -> None:
        """Configure an integral 1..52 week cadence; None keeps persisted config.

        Fresh roots wait until Monday. Existing overdue state catches up once,
        not once per missed week. Failed attempts retry after six hours. A claim
        is persisted before research, so a crashed attempt also backs off.
        """
        self.root = Path(root).absolute()
        if ".." in self.root.parts:
            raise ValueError("schedule root cannot contain traversal")
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise ValueError("schedule root cannot traverse symlinks")
        info = self.root.stat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("schedule root must be user owned with mode 0700")
        if self.root != Path(self_evolution.mind_store.root).absolute():
            raise ValueError("scheduler and MindStore must share the data root")
        if interval_weeks is not None and (
            type(interval_weeks) is not int or not 1 <= interval_weeks <= 52
        ):
            raise ValueError("interval_weeks must be an integer from 1 to 52")
        self.self_evolution = self_evolution
        self.mind_store = self_evolution.mind_store
        self.outreach = outreach
        self.clock = clock
        self.state_path = self.root / "evolution_schedule.json"
        self._configured_interval = interval_weeks
        now = _number(clock())
        self._state = self._read_state(now)
        # Persist even a not-yet-due first schedule so a restart after Monday
        # does not silently postpone that week's research. Startup may do IO;
        # tick() never takes this lock or writes on its normal path.
        fd = os.open(
            self.root / "evolution_schedule.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            self._private_file(os.fstat(fd))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._state = self._read_state(now)
            else:
                self._state = self._read_state(now)
                self._save_state(self._state)
        finally:
            os.close(fd)
        self._next_due = self._state["next_due"]
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self.last_error_type: str | None = None

    def _read_state(self, now: float) -> dict[str, Any]:
        try:
            fd = os.open(self.state_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            if hasattr(self, "_state"):
                return self._state.copy()
            due = _monday(now)
            return {
                "version": 1,
                "interval_weeks": self._configured_interval or 1,
                "phase": "idle",
                "next_due": due,
                "scheduled_for": due,
            }
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            self._private_file(os.fstat(stream.fileno()))
            text = stream.read(262145)
        if len(text.encode("utf-8")) > 262144:
            raise ValueError("schedule state exceeds size limit")
        raw: Any = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("invalid evolution schedule state")
        state = cast(dict[str, Any], raw)
        if (
            state.get("version") != 1
            or state.get("phase") not in {"idle", "research", "notify"}
            or type(state.get("interval_weeks")) is not int
            or not 1 <= state["interval_weeks"] <= 52
        ):
            raise ValueError("invalid evolution schedule state")
        state["next_due"] = _number(state.get("next_due"))
        state["scheduled_for"] = _number(state.get("scheduled_for"))
        if datetime.fromtimestamp(state["scheduled_for"], CST).weekday() != 0:
            raise ValueError("logical research date must be Monday")
        if state["phase"] == "notify" and not isinstance(state.get("notification"), dict):
            raise ValueError("missing pending notification")
        if self._configured_interval is not None:
            state["interval_weeks"] = self._configured_interval
        return state

    @staticmethod
    def _private_file(info: os.stat_result) -> None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("unsafe schedule file")

    def _save_state(self, state: dict[str, Any]) -> None:
        """Atomic private same-directory replace, including directory fsync."""
        data = json.dumps(state, ensure_ascii=False, allow_nan=False, sort_keys=True)
        if len(data.encode("utf-8")) > 262144:
            raise ValueError("schedule state exceeds size limit")
        fd, name = tempfile.mkstemp(prefix=".evolution-schedule-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.state_path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)
        self._state = state

    def tick(self) -> bool:
        """Return immediately; True means a worker started, not completion."""
        now = _number(self.clock())
        if now < self._next_due:
            return False
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if now < self._next_due or (self._worker is not None and self._worker.is_alive()):
                return False
            self._next_due = math.inf
            self._worker = threading.Thread(
                target=self._run, args=(now,), name="jarvis-evolution", daemon=True
            )
            try:
                self._worker.start()
            except Exception as exc:
                self.last_error_type = type(exc).__name__
                self._worker = None
                self._next_due = now + RETRY_SECONDS
                return False
            return True
        finally:
            self._lock.release()

    def _evaluate_modules(self) -> list[str]:
        domains: list[str] = []
        modules = list(self.self_evolution._modules.items())
        if not any(instance is self for _, instance in modules):
            modules.append(("evolution_scheduler", self))
        for name, instance in modules:
            try:
                validate = getattr(self.self_evolution, "_evaluation", None)
                raw_evaluation: Any = (
                    validate(instance) if callable(validate) else instance.self_evaluate()
                )
                if not isinstance(raw_evaluation, dict):
                    raise ValueError("invalid registered module evaluation")
                evaluation = cast(dict[str, Any], raw_evaluation)
                if evaluation.get("module") != name:
                    raise ValueError("invalid registered module evaluation")
                # Snapshot JSON metadata only, not arbitrary mutable module state.
                snapshot = json.loads(json.dumps(evaluation, allow_nan=False))
                self.mind_store.append("self_evaluation", snapshot, source="evolution_scheduler")
                if instance is not self and snapshot["domain"] not in domains:
                    domains.append(snapshot["domain"])
            except Exception as exc:
                self.mind_store.append(
                    "self_evaluation",
                    {
                        "module": name,
                        "error_type": type(exc).__name__,
                        "last_evaluated_at": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
                    },
                    source="evolution_scheduler",
                )
        return domains

    def _research(self, scheduled_for: float) -> tuple[list[Any], dict[str, Any]]:
        # The legacy hook also queues a machine JSON notification to Hermes.
        # Isolate only that hook on a shallow per-cycle facade: never mutate the
        # shared SelfEvolution/outreach and never fabricate a notification receipt.
        research = copy.copy(self.self_evolution)
        research._notify_pending = lambda: None
        candidates: int | None = 0
        candidate_method = getattr(research, "_candidates", None)
        if callable(candidate_method):

            def counted(topic: str) -> list[Any]:
                nonlocal candidates
                result = cast(Callable[[str], list[Any]], candidate_method)(topic)
                candidates = (candidates or 0) + len(result)
                return result

            research._candidates = counted
        previous = self.mind_store.recent("evolution_research_session", 1)
        proposals = research.run_weekly_if_due(now=datetime.fromtimestamp(scheduled_for, CST))
        latest = self.mind_store.recent("evolution_research_session", 1)
        session: dict[str, Any] = latest[0]["payload"] if latest and latest != previous else {}
        # Completion may have committed before a crash saved the notification.
        if not proposals and not session:
            week = datetime.fromtimestamp(scheduled_for, CST).date().isoformat()
            completed = self.mind_store.recent(
                "evolution_weekly_completed", 1, where={"week": week}
            )
            if completed:
                ids = completed[0]["payload"].get("proposal_ids", [])
                proposals = [p for p in research.get_pending_proposals() if p.proposal_id in ids]
                # The legacy completion record does not retain rejected candidate
                # counts. Do not misreport recovery as a fresh zero-candidate run.
                candidates = None
                session = previous[0]["payload"] if previous else {}
        if "candidate_count" in session:
            candidates = session["candidate_count"]
        return proposals, {
            "domains": session.get("topics", []),
            "candidate_count": candidates,
            "errors": session.get("errors", getattr(research, "last_session_errors", [])),
        }

    def _notification(self, proposals: list[Any], now: float) -> dict[str, Any]:
        ids = [p.proposal_id for p in proposals]
        digest = hashlib.sha256(json.dumps(ids, ensure_ascii=False).encode()).hexdigest()
        lines = [f"本轮自进化研究形成 {len(proposals)} 项新提案，已写入提案记录："]
        lines.extend(
            f"{i}. {str(p.topic)[:120]}：{str(p.summary)[:220]}（提案 {p.proposal_id}）"
            for i, p in enumerate(proposals[:8], 1)
        )
        if len(proposals) > 8:
            lines.append(f"其余 {len(proposals) - 8} 项见 mind.db 提案记录。")
        lines.append("提案须先由 Hermes 评审，再由 Sir 授权；我不会自行修改核心代码或安装技能。")
        return {
            "text": "Sir：\n\n" + "\n".join(lines) + "\n\nJARVIS",
            "proposal_ids": ids,
            "dedupe_key": "evolution-cycle:" + digest,
            "created_at": now,
        }

    def _run(self, now: float) -> None:
        fd: int | None = None
        acquired = False
        state = self._state.copy()
        cycle: dict[str, Any] = {}
        try:
            fd = os.open(
                self.root / "evolution_schedule.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
            self._private_file(os.fstat(fd))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._next_due = now + 60
                return
            acquired = True
            state = self._read_state(now)
            if now < state["next_due"]:
                self._state = state
                return
            if state["phase"] == "idle":
                state.update(
                    phase="research",
                    cycle_id=uuid.uuid4().hex,
                    baseline_proposal_ids=[
                        p.proposal_id for p in self.self_evolution.get_pending_proposals()
                    ],
                )
            state["next_due"] = now + RETRY_SECONDS
            self._save_state(state)
            domains = self._evaluate_modules()
            if state["phase"] == "research":
                proposals, facts = self._research(state["scheduled_for"])
                # A prior attempt may have committed proposals before failing.
                # Include those new pending IDs, never the pre-cycle backlog.
                recovered = self.self_evolution.get_pending_proposals()
                by_id = {p.proposal_id: p for p in [*recovered, *proposals]}
                baseline = set(state.get("baseline_proposal_ids", []))
                proposals = [p for identity, p in by_id.items() if identity not in baseline]
                cycle = {
                    "cycle_id": state["cycle_id"],
                    "scheduled_for": state["scheduled_for"],
                    "started_at": now,
                    **facts,
                    "proposal_count": len(proposals),
                    "proposal_ids": [p.proposal_id for p in proposals],
                    "requires_human_approval": True,
                    "delivery_verified": False,
                }
                if not cycle["domains"]:
                    cycle["domains"] = domains
                state.update(
                    phase="notify",
                    cycle=cycle,
                    notification=self._notification(proposals, now) if proposals else {},
                )
                self._save_state(state)
            else:
                cycle = state["cycle"]
            notification = state["notification"]
            if notification and not notification.get("message_id"):
                message_id = self.outreach.enqueue(
                    notification["text"],
                    reason="JARVIS 自进化提案待评审与授权",
                    urgency="normal",
                    channel="imessage",
                    studying=False,
                    dedupe_key=notification["dedupe_key"],
                    now=datetime.fromtimestamp(now, UTC),
                )
                notification["message_id"] = str(message_id)
                self._save_state(state)
            cycle.update(
                status="completed",
                completed_at=_number(self.clock()),
                notification_id=notification.get("message_id"),
            )
            if not self.mind_store.recent(
                "evolution_cycle", 1, where={"cycle_id": state["cycle_id"], "status": "completed"}
            ):
                self.mind_store.append(
                    "evolution_cycle",
                    cycle,
                    source="evolution_scheduler",
                    refs=cycle["proposal_ids"],
                )
            next_due = state["scheduled_for"] + state["interval_weeks"] * WEEK_SECONDS
            finished = _number(self.clock())
            while next_due <= finished:
                next_due += state["interval_weeks"] * WEEK_SECONDS
            completed_state: dict[str, Any] = {
                **state,
                "phase": "idle",
                "next_due": next_due,
                "scheduled_for": next_due,
                "last_completed_at": finished,
                "last_error_type": None,
            }
            completed_state.pop("notification", None)
            completed_state.pop("cycle", None)
            self._save_state(completed_state)
            state = completed_state
            self.last_error_type = None
        except Exception as exc:
            self.last_error_type = type(exc).__name__
            if acquired:
                state.update(
                    next_due=_number(self.clock()) + RETRY_SECONDS,
                    last_error_type=self.last_error_type,
                )
                try:
                    self._save_state(state)
                    self.mind_store.append(
                        "evolution_cycle",
                        {
                            **cycle,
                            "status": "failed",
                            "error_type": self.last_error_type,
                            "cycle_id": state.get("cycle_id"),
                            "domains": cycle.get("domains", []),
                            "candidate_count": cycle.get("candidate_count"),
                            "proposal_count": cycle.get("proposal_count"),
                        },
                        source="evolution_scheduler",
                    )
                except Exception:
                    # Never leak exception strings/credentials or crash the daemon.
                    pass
        finally:
            if fd is not None:
                os.close(fd)
            if acquired:
                self._next_due = state["next_due"]
            elif math.isinf(self._next_due):
                self._next_due = now + RETRY_SECONDS

    def wait(self, timeout: float | None = None) -> bool:
        """Bounded shutdown/test helper; not part of the daemon's tick path."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
        return worker is None or not worker.is_alive()

    def self_evaluate(self) -> dict[str, Any]:
        """Public module metadata only: no database, private data or network IO."""
        return {
            "module": "evolution_scheduler",
            "domain": "durable periodic background scheduling proposal notifications",
            "current_version": "1.0.0",
            "known_limitations": [
                "需宿主每轮 tick 并调度 outreach；入队不代表 iMessage 已送达",
                "通知入队与状态文件不是跨库事务，崩溃边界依赖 outreach 去重",
                "仅研究提案；Hermes 评审及 Sir 授权始终由可信操作方完成",
            ],
            "improvement_directions": ["统一跨库通知事务与送达回执对账"],
            "last_evaluated_at": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
        }
