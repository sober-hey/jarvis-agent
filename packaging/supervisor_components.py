"""Component definitions and bounded health adapters for web and resident daemon."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Health:
    kind: str
    target: str
    timeout: float = 30
    interval: float = 2
    startup_grace: float = 30
    request_timeout: float = 0.5


@dataclass(frozen=True)
class Restart:
    base_delay: float = 1
    max_delay: float = 60
    crash_limit: int = 5
    crash_window: float = 120
    stable_after: float = 120


@dataclass(frozen=True)
class Component:
    name: str
    command: tuple[str, ...]
    health: Health
    restart: Restart = field(default_factory=Restart)
    stop_timeout: float = 75


def load_components(path: Path) -> tuple[Component, ...]:
    document = cast(dict[str, Any], json.loads(path.read_text()))
    components: list[Component] = []
    names: set[str] = set()
    for row in document["components"]:
        command: object = row["command"]
        if not isinstance(command, list):
            raise ValueError("command must be an argv array")
        argv = cast(list[object], command)
        if not argv or not all(isinstance(arg, str) and arg for arg in argv):
            raise ValueError("command arguments must be nonempty strings")
        component = Component(
            str(row["name"]),
            tuple(cast(list[str], argv)),
            Health(**row["health"]),
            Restart(**row.get("restart", {})),
            float(row.get("stop_timeout", 75)),
        )
        if (
            not component.name
            or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for c in component.name
            )
            or component.name in names
        ):
            raise ValueError("invalid or duplicate component name")
        if not component.command or not all(component.command):
            raise ValueError("command must be a nonempty argv array")
        if component.health.kind not in {"http", "heartbeat"}:
            raise ValueError("unsupported health adapter")
        numbers = (
            component.health.timeout,
            component.health.interval,
            component.health.startup_grace,
            component.health.request_timeout,
            component.restart.base_delay,
            component.restart.max_delay,
            component.restart.crash_window,
            component.restart.stable_after,
            component.stop_timeout,
        )
        if (
            any(not math.isfinite(n) or n <= 0 for n in numbers)
            or not 1 <= component.restart.crash_limit <= 1000
            or not component.restart.base_delay <= component.restart.max_delay <= 60
        ):
            raise ValueError("invalid lifecycle policy")
        if component.health.kind == "http":
            parsed = urlsplit(component.health.target)
            if (
                parsed.scheme != "http"
                or parsed.hostname != "127.0.0.1"
                or parsed.port is None
                or parsed.username
                or parsed.password
            ):
                raise ValueError("health HTTP must use an explicit loopback port")
        elif not Path(component.health.target).is_absolute():
            raise ValueError("heartbeat path must be absolute")
        names.add(component.name)
        components.append(component)
    if not components:
        raise ValueError("at least one component is required")
    return tuple(components)


def probe(health: Health, launched_wall: float) -> tuple[bool, float | None, str | None]:
    """Heartbeat freshness never accepts a previous generation's stale file."""
    try:
        if health.kind == "heartbeat":
            stamp = Path(health.target).stat().st_mtime
            if stamp < launched_wall or stamp > time.time() + 1:
                return False, None, "heartbeat is from another generation"
            if time.time() - stamp > health.timeout:
                return False, stamp, "heartbeat expired"
            return True, stamp, None
        parsed = urlsplit(health.target)
        connection = http.client.HTTPConnection(
            "127.0.0.1", parsed.port, timeout=health.request_timeout
        )
        try:
            connection.request("GET", parsed.path or "/")
            response = connection.getresponse()
            payload: object = json.loads(response.read(4096))
            if (
                response.status == 200
                and isinstance(payload, dict)
                and cast(dict[str, object], payload).get("status") == "ok"
            ):
                return True, time.time(), None
            return False, None, "HTTP health rejected"
        finally:
            connection.close()
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return False, None, type(exc).__name__


def default_components(
    data_root: Path,
    config: Path,
    key_file: Path,
    port: int,
    *,
    acceptance_mode: bool = False,
    test_port: int | None = None,
) -> tuple[Component, ...]:
    """Launch web directly and the resident worker with its own progress heartbeat."""
    # daemon_wiring imports Component/Health; defer until these are fully defined.
    from daemon_wiring import component

    command = [
        sys.executable,
        "-B",
        str(Path(__file__).with_name("web_server.py")),
        "--config",
        str(config),
        "--key-file",
        str(key_file),
        "--data-dir",
        str(data_root),
        "--port",
        str(port),
    ]
    if acceptance_mode:
        command.append("--acceptance-mode")
    if test_port is not None:
        command.extend(["--test-local-port", str(test_port)])
    return (
        Component("web", tuple(command), Health("http", f"http://127.0.0.1:{port}/api/health")),
        component(data_root, config=config, key_file=key_file),
    )


def self_evaluate() -> dict[str, object]:
    """Inspect actual dataclass defaults; never build components or probe IO."""
    from dataclasses import asdict
    from datetime import datetime, timezone

    health = Health("heartbeat", "/jarvis-evaluation-not-created")
    restart = Restart()
    checks = {
        "positive_health_policy": all(
            math.isfinite(v) and v > 0
            for v in (health.timeout, health.interval, health.startup_grace, health.request_timeout)
        ),
        "bounded_restart_policy": 0 < restart.base_delay <= restart.max_delay <= 60
        and 1 <= restart.crash_limit <= 1000
        and all(
            math.isfinite(v) and v > 0
            for v in (
                restart.base_delay,
                restart.max_delay,
                restart.crash_window,
                restart.stable_after,
            )
        ),
    }
    health_policy = asdict(health)
    health_policy.pop("target")
    return {
        "module": "supervisor_components",
        "domain": "process component manifests heartbeat freshness bounded restart policies",
        "current_version": "1.0.0",
        "known_limitations": [
            "Dataclass defaults do not validate every direct constructor; load_components validates manifests.",
            "Default factory builds web and resident daemon argv; evaluation neither calls the factory nor probes health.",
        ],
        "improvement_directions": ["Unify direct constructor and manifest lifecycle validation."],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "health_policy": health_policy,
        "restart_policy": asdict(restart),
        "checks": checks,
        "ok": all(checks.values()),
    }


def fake_main() -> int:
    parser = argparse.ArgumentParser(description="Fixture heartbeat worker; never calls a model")
    parser.add_argument("mode", choices=["fake"])
    parser.add_argument("--heartbeat", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1)
    args = parser.parse_args()
    stopping = False

    def stop(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    while not stopping:
        args.heartbeat.write_text(str(time.time()))
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(fake_main())
