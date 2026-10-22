"""K0-015 DRAFT: controlled fault injection for offline fixtures only.

NOT_APPROVED / NOT_FROZEN / runtime_enabled=False.
Only used with explicit draft_fixture=True callers; never wired to production.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass


@dataclass
class FaultEvent:
    """Record of one injected fault."""

    fault: str
    layer: str
    step: str


class FaultInjector:
    """Arms controlled faults at specific (layer, step) points.

    Each armed fault fires once when ``raise_if_armed`` matches its key,
    then is consumed.  Events are retained in ``events`` for assertions.
    """

    def __init__(self, *, draft_fixture: bool = False) -> None:
        if not draft_fixture:
            raise ValueError("explicit draft_fixture=True required")
        self._armed: dict[str, FaultEvent] = {}
        self.events: list[FaultEvent] = []

    def arm(self, fault: str, *, layer: str, step: str) -> None:
        key = f"{layer}:{step}"
        self._armed[key] = FaultEvent(fault=fault, layer=layer, step=step)

    def raise_if_armed(self, layer: str, step: str) -> None:
        key = f"{layer}:{step}"
        event = self._armed.pop(key, None)
        if event is None:
            return
        self.events.append(event)
        if event.fault == "disk_full":
            raise OSError(errno.ENOSPC, "injected disk_full")
        if event.fault == "permission_denied":
            raise PermissionError("injected permission_denied")
        if event.fault == "network_timeout":
            raise TimeoutError("injected network_timeout")
        if event.fault == "db_locked":
            raise BlockingIOError("injected db_locked")
        if event.fault == "sigkill":
            raise SystemExit("injected sigkill")
        raise RuntimeError(f"unknown fault: {event.fault}")
