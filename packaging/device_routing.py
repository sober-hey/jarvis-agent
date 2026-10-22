"""Deterministic offline policies for wake ownership and audio output.

This module does not collect device telemetry, coordinate Bluetooth peers, grant
wake leases or play audio. Callers must send the same candidate snapshot to one
coordinator and enforce its decision before any device responds.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import isfinite


@dataclass(frozen=True)
class DeviceState:
    """One device's calibrated snapshot.

    ``signal_strength`` and ``recently_used`` are normalized [0, 1] values;
    callers compute recency without this policy consulting a clock. DOA is
    already normalized across hardware; 1 means unknown/worst direction.
    ``wearing`` is device wear state, separate from ``headphones_worn``.
    Device kinds are watch/iphone/mac/xiaoai; identifiers are preserved as-is.
    """

    device_id: str
    device_type: str
    signal_strength: float = 0.0
    recently_used: float = 0.0
    wearing: bool = False
    normalized_doa_variance: float = 1.0
    online: bool = True
    face_down: bool = False
    locked: bool = False
    bluetooth_enabled: bool = True
    headphones: str | None = None
    headphones_worn: bool = True

    def __post_init__(self) -> None:
        if type(self.device_id) is not str or not self.device_id.strip():
            raise ValueError("device_id must be nonempty")
        if self.device_type not in ("watch", "iphone", "mac", "xiaoai"):
            raise ValueError("unknown device_type")
        for name in ("signal_strength", "recently_used", "normalized_doa_variance"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be a finite normalized value in [0, 1]")
        for name in (
            "wearing",
            "online",
            "face_down",
            "locked",
            "bluetooth_enabled",
            "headphones_worn",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if self.headphones not in (None, "airpods", "wired"):
            raise ValueError("headphones must be None, 'airpods', or 'wired'")


def should_participate(device: DeviceState) -> bool:
    """Hard wake exclusions; output eligibility is deliberately separate."""
    return device.online and not device.face_down and not device.locked and device.bluetooth_enabled


def _priority(device: DeviceState) -> int:
    if device.device_type == "watch" and device.wearing:
        return 0
    return {"iphone": 1, "mac": 2, "xiaoai": 3, "watch": 4}[device.device_type]


def _validate_unique(devices: list[DeviceState] | tuple[DeviceState, ...]) -> None:
    ids = [device.device_id for device in devices]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate device_id in snapshot")


def arbitrate(candidates: list[DeviceState]) -> str:
    """Return one winner; raise ValueError if no device is eligible.

    Decimal telemetry is compared as exact fractions, so a mathematical tie
    (.5*.4 + .1 versus 1*.3) is not decided by binary floating-point noise.
    Multiplying the specified score by ten preserves ordering and ties.
    """
    _validate_unique(candidates)
    eligible = [device for device in candidates if should_participate(device)]
    if not eligible:
        raise ValueError("no eligible wake device")
    return min(
        eligible,
        key=lambda device: (
            -(
                Fraction(str(float(device.signal_strength))) * 4
                + Fraction(str(float(device.recently_used))) * 3
                + int(device.wearing) * 2
                + (1 - Fraction(str(float(device.normalized_doa_variance))))
            ),
            _priority(device),
            device.device_id,
        ),
    ).device_id


def xiaoai_output_allowed(context: dict[str, object], *, now: object = None) -> bool:
    """Fail closed unless fresh home AND room evidence permits shared-room audio.

    Separate from wake ownership: LX05 does not supply a JARVIS microphone.
    The gateway must supply observed room evidence, not guess room from GPS.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    instant = datetime.now(ZoneInfo("Asia/Shanghai")) if now is None else now
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        return False
    if not 8 <= instant.astimezone(ZoneInfo("Asia/Shanghai")).hour < 23:
        return False
    if (
        context.get("state") != "home"
        or context.get("stale") is not False
        or context.get("pipeline_ok") is not True
    ):
        return False
    if context.get("room") not in ("卧室", "客厅", "bedroom", "living_room"):
        return False
    for field, max_age in (("last_point_ts", 5400), ("room_observed_at", 300)):
        stamp = context.get(field)
        if not isinstance(stamp, str):
            return False
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None or not 0 <= instant.timestamp() - parsed.timestamp() < max_age:
                return False
        except (ValueError, OverflowError, OSError):
            return False
    return True


@dataclass(frozen=True)
class DeviceContext:
    """Caller-supplied snapshot, not a location detector.

    Home is the requested policy proxy ``tailscale_online and mac_active``;
    it is not independent proof of physical presence. No network is queried.
    """

    devices: tuple[DeviceState, ...] = ()
    tailscale_online: bool = False
    mac_active: bool = False
    xiaoai_context: dict[str, object] | None = None

    def __post_init__(self) -> None:
        for name in ("tailscale_online", "mac_active"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        object.__setattr__(self, "devices", tuple(self.devices))
        _validate_unique(self.devices)


@dataclass(frozen=True)
class OutputPlan:
    """Exactly one device, or an explicit silent plan when none is usable."""

    device_id: str | None
    channel: str
    location: str
    reason: str


def select_output_device(state: DeviceContext, *, now: object = None) -> OutputPlan:
    """Choose one output independently of wake participation flags."""
    home = (state.tailscale_online and state.mac_active) or (
        state.xiaoai_context is not None and xiaoai_output_allowed(state.xiaoai_context, now=now)
    )
    location = "home" if home else "away"
    headphone_devices = [
        device
        for device in state.devices
        if device.online
        and device.headphones is not None
        and device.headphones_worn
        and (device.headphones == "wired" or device.bluetooth_enabled)
    ]
    if headphone_devices:
        device = min(
            headphone_devices,
            key=lambda item: (-item.recently_used, _priority(item), item.device_id),
        )
        return OutputPlan(device.device_id, "headphones", location, "headphones_worn")
    priority = ("mac", "xiaoai", "iphone", "watch") if home else ("iphone", "watch")
    for device_type in priority:
        if device_type == "xiaoai" and not xiaoai_output_allowed(
            state.xiaoai_context or {}, now=now
        ):
            continue
        available = [
            device
            for device in state.devices
            if device.online and device.device_type == device_type
        ]
        if available:
            device = min(available, key=lambda item: (-item.recently_used, item.device_id))
            return OutputPlan(device.device_id, "speaker", location, "location_priority")
    return OutputPlan(None, "none", location, "no_available_output")


def self_evaluate() -> dict[str, object]:
    """Offline declaration; no telemetry or evidence is collected here."""
    return {
        "module": "device_routing",
        "domain": "multi-device wake arbitration and audio output routing",
        "current_version": "1.0.0",
        "known_limitations": [
            "Pure policy only: no microphone/Bluetooth transport or wake lease enforcement.",
            "Signal strength and DOA must already be calibrated and normalized by callers.",
            "Tailscale plus Mac activity is a requested heuristic, not proof of presence.",
            "No live mind/KG or capability integration is provided by these functions.",
        ],
        "improvement_directions": [
            "Coordinate event-scoped winner leases and suppress losers before playback.",
            "Integrate calibrated device telemetry and observed location evidence.",
            "Attach auditable routing decisions to the unified runtime and graph.",
        ],
        "last_evaluated_at": None,
    }
