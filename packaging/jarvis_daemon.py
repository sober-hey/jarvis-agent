"""Bounded resident scheduler. No service is installed by this entrypoint."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import signal
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from context_limits import INNER_CONTEXT_BYTE_LIMIT

CST = ZoneInfo("Asia/Shanghai")
# Late enough to include the working day, before midnight to avoid date races.
REFLECTION_HOUR = 23
REFLECTION_MINUTE = 30
POLL_SECONDS = 15.0
# One thought per five idle minutes is 288/day; active input targets 15 seconds.
ACTIVE_SECONDS = 15.0
IDLE_SECONDS = 300.0
TASK_SECONDS = 15.0
DISPATCH_SECONDS = 5.0
TICK_SECONDS = 0.25
# Wall-clock limits; model work additionally has launcher's effort-specific bound.
LOCAL_BUDGET_SECONDS = 15.0
MODEL_BUDGET_SECONDS = 2100.0
CONTEXT_BYTE_LIMIT = INNER_CONTEXT_BYTE_LIMIT


class Hooks(Protocol):
    root: Path
    timeline_id: str
    interrupt_event: threading.Event
    thinking_event: threading.Event

    def poll_imessage(self) -> int: ...
    def poll(self) -> int: ...
    def think(self) -> bool: ...
    def cognition_health(self) -> dict[str, object]: ...
    def activity_pending(self) -> bool: ...
    def thought_delay(self, active: bool) -> float: ...
    def budget_status(self, phase: str = "thought") -> dict[str, object]: ...
    def contact(self) -> None: ...
    def tasks(self) -> None: ...
    def dispatch(self) -> None: ...
    def reflect(self, day: str) -> None: ...
    def once(self, key: str) -> bool: ...
    def log(self, event: str, **fields: object) -> None: ...


class StopRequested(BaseException):
    """Cooperative cancellation also unwinds launcher subprocess cleanup."""


class StepTimeout(BaseException):
    """A stage exhausted its wall-clock allowance."""


@contextmanager
def deadline(seconds: float) -> Generator[None]:
    """Bound main-thread stages without leaving timed-out worker threads alive."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("scheduler requires the main thread")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(signum: int, frame: object) -> None:
        raise StepTimeout()

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class Daemon:
    def __init__(self, hooks: Hooks, *, clock: Callable[[], float] = time.time) -> None:
        self.hooks = hooks
        self.clock = clock
        self.stopping = threading.Event()
        self._interrupt_event = hooks.interrupt_event
        # Alias keeps the cancellation vocabulary usable by future channels.
        self.interrupt_flag = self._interrupt_event
        self.next_due: dict[str, float] = {}
        self.last_active = False
        self.ticks = 0

    @property
    def interrupt_event(self) -> threading.Event:
        return self._interrupt_event

    @contextmanager
    def listening_during_think(self) -> Generator[None]:
        """Ingress only runs concurrently; deadlines/model/mind stay on main."""
        finished = threading.Event()

        def listen() -> None:
            while not finished.wait(0.25) and not self.stopping.is_set():
                try:
                    self.hooks.poll_imessage()
                except Exception as exc:
                    self.hooks.log("imessage_listener_error", error_type=type(exc).__name__)

        listener = threading.Thread(target=listen, name="jarvis-imessage-listener", daemon=True)
        self.hooks.thinking_event.set()
        try:
            listener.start()
            yield
        finally:
            finished.set()
            self.hooks.thinking_event.clear()
            if listener.ident is not None:
                # poll_imessage is bounded SQLite ingress, never model/transport.
                # Join before another round can clear its flag or drain its inbox.
                listener.join()

    def heartbeat(self, phase: str) -> None:
        target = self.hooks.root / "daemon.heartbeat"
        pending = target.with_suffix(".pending")
        cognition = self.hooks.cognition_health()
        pending.write_text(
            json.dumps(
                {
                    "status": cognition["status"],
                    "cognition": cognition,
                    "pid": os.getpid(),
                    "wall_time": time.time(),
                    "logical_time": self.clock(),
                    "ticks": self.ticks,
                    "phase": phase,
                    "timeline_id": self.hooks.timeline_id,
                }
            )
        )
        os.replace(pending, target)

    def stage(self, name: str, action: Callable[[], object], budget: float) -> object:
        if self.stopping.is_set():
            return None
        self.heartbeat(name)
        try:
            with deadline(budget):
                return action()
        except (Exception, StepTimeout) as exc:
            self.hooks.log("stage_error", stage=name, error_type=type(exc).__name__)
            return None
        finally:
            self.heartbeat("idle")

    def tick(self) -> None:
        now = self.clock()
        self.ticks += 1
        # Integration schedulers run first so think's early returns never skip them.
        background = getattr(self.hooks, "background", None)
        if callable(background):
            self.stage("background", background, LOCAL_BUDGET_SECONDS)
        if now >= self.next_due.get("poll", 0) or self.interrupt_event.is_set():
            self.next_due["poll"] = now + POLL_SECONDS
            count = self.stage("poll", self.hooks.poll, LOCAL_BUDGET_SECONDS)
            if isinstance(count, int):
                self.interrupt_event.clear()  # Inbox has reached the main-thread mind.
            active = self.stage("activity", self.hooks.activity_pending, LOCAL_BUDGET_SECONDS)
            if (isinstance(count, int) and count > 0) or active is True:
                self.last_active = True
                delay = self.hooks.thought_delay(True)
                self.next_due["think"] = min(self.next_due.get("think", now), now + delay)
        if now >= self.next_due.get("think", 0):
            if self.interrupt_event.is_set():
                # A failed inbox drain must not silently erase a pending interrupt.
                self.next_due["poll"] = 0
                return
            self.interrupt_event.clear()
            with self.listening_during_think():
                self.stage("think", self.hooks.think, MODEL_BUDGET_SECONDS)
            if self.interrupt_event.is_set():
                # Cooperative checkpoint, not a provider response.cancel: keep
                # existing durable thought/audit records, but skip this tick's
                # contact/tasks/dispatch and prioritize the newly committed inbox.
                self.hooks.log("think_interrupted")
                self.last_active = True
                self.next_due["poll"] = 0
                self.next_due["think"] = self.clock()
                self.stage("poll", self.hooks.poll, LOCAL_BUDGET_SECONDS)
                return
            self.next_due["think"] = self.clock() + self.hooks.thought_delay(self.last_active)
            self.last_active = False
        # Contact transfers durable intent only; model authoring occurs at dispatch.
        self.stage("contact", self.hooks.contact, LOCAL_BUDGET_SECONDS)
        for name, interval, action in (
            ("tasks", TASK_SECONDS, self.hooks.tasks),
            ("dispatch", DISPATCH_SECONDS, self.hooks.dispatch),
        ):
            if now >= self.next_due.get(name, 0):
                self.next_due[name] = now + interval
                self.stage(name, action, MODEL_BUDGET_SECONDS)
        local = datetime.fromtimestamp(now, CST)
        if (local.hour, local.minute) >= (REFLECTION_HOUR, REFLECTION_MINUTE):
            day = local.date().isoformat()
            self.stage("reflection", lambda: self.reflect_once(day), MODEL_BUDGET_SECONDS)
        self.heartbeat("idle")

    def reflect_once(self, day: str) -> None:
        # Reserve before model work. Unknown/failed days never auto-retry.
        if self.hooks.budget_status("reflection")["allowed"] and self.hooks.once(
            "reflection:" + day
        ):
            self.hooks.reflect(day)

    def stop(self, signum: int, frame: object) -> None:
        self.stopping.set()
        # BaseException bypasses module fallback/retry handlers while their finally
        # blocks still reap launcher children and close database transactions.
        raise StopRequested()

    def run(self) -> int:
        lock = os.open(self.hooks.root / "daemon.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, self.stop)
            self.hooks.log("daemon_started", pid=os.getpid(), timeline_id=self.hooks.timeline_id)
            try:
                while not self.stopping.is_set():
                    self.tick()
                    self.stopping.wait(TICK_SECONDS)
            except StopRequested:
                pass
            close = getattr(self.hooks, "close_integrations", None)
            if callable(close):
                close()
            self.hooks.log("daemon_stopped", pid=os.getpid())
            self.heartbeat("stopped")
            return 0
        finally:
            os.close(lock)


def main() -> int:
    from daemon_wiring import Wiring, make_clock

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--acceptance-mode", action="store_true")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--speed", type=float, default=1)
    parser.add_argument("--config", type=Path)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--key-file", type=Path)
    source.add_argument("--key-fd", type=int)
    args = parser.parse_args()
    if not math.isfinite(args.speed) or not 0 < args.speed <= 3600:
        parser.error("speed must be finite and in (0,3600]")
    if (args.fixture or args.speed != 1) and not args.acceptance_mode:
        parser.error("fixture and accelerated clock require acceptance-mode")
    clock = make_clock(args.data_root, args.speed) if args.fixture else time.time
    wiring = Wiring.create(
        args.data_root,
        fixture=args.fixture,
        acceptance=args.acceptance_mode,
        clock=clock,
        config=args.config,
        key_file=args.key_file,
        key_fd=args.key_fd,
    )
    return Daemon(wiring, clock=clock).run()


if __name__ == "__main__":
    raise SystemExit(main())
