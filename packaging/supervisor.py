#!/usr/bin/env python3
"""Single-owner JARVIS process supervisor (POSIX, macOS/Linux).

run --data-root ROOT --components components.json
status --data-root ROOT  # read-only; never acquires the owner lock

Component JSON contains argv (no shell), health and restart policy. The default
manifest is web + a clearly labelled fixture worker, not the production inner
loop. This is process supervision, not a task queue or exactly-once executor.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TextIO, cast

from supervisor_components import Component, default_components, load_components, probe


@dataclass(frozen=True)
class Identity:
    pid: int
    ppid: int
    pgid: int
    uid: int
    started: str
    command_hash: str


def process_table() -> dict[int, Identity]:
    """Bind signals to UID + PGID + full start time + command, not just PID."""
    result = subprocess.run(
        ["/bin/ps", "-axww", "-o", "pid=,ppid=,pgid=,uid=,lstart=,command="],
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )
    table: dict[int, Identity] = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 9)
        if len(fields) < 10:
            continue
        pid, ppid, pgid, uid = (int(part) for part in fields[:4])
        table[pid] = Identity(
            pid,
            ppid,
            pgid,
            uid,
            " ".join(fields[4:9]),
            hashlib.sha256(fields[9].encode()).hexdigest(),
        )
    return table


def same_identity(expected: Identity, actual: Identity | None) -> bool:
    return actual is not None and (
        expected.pid,
        expected.pgid,
        expected.uid,
        expected.started,
        expected.command_hash,
    ) == (actual.pid, actual.pgid, actual.uid, actual.started, actual.command_hash)


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class AlreadyRunning(RuntimeError):
    pass


class OwnerLock:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.stream: TextIO | None = None
        self.identity: Identity | None = None

    def acquire(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.stream = (self.root / "supervisor.lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.stream.close()
            self.stream = None
            raise AlreadyRunning("data root already has a supervisor (owner lock held)") from exc
        try:
            owner_path = self.root / "supervisor-owner.json"
            if owner_path.exists():
                previous = Identity(**json.loads(owner_path.read_text()))
                if same_identity(previous, process_table().get(previous.pid)):
                    raise AlreadyRunning("verified previous owner is still alive")
            self.identity = process_table()[os.getpid()]
            atomic_json(owner_path, asdict(self.identity))
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        if self.stream is not None:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None


@dataclass
class Runtime:
    spec: Component
    process: subprocess.Popen[bytes] | None = None
    identity: Identity | None = None
    witnessed: dict[int, Identity] = field(default_factory=lambda: dict[int, Identity]())
    started: float = 0
    started_wall: float = 0
    last_ok: float = 0
    last_heartbeat: float | None = None
    last_error: str | None = None
    restart_count: int = 0
    failures: deque[float] = field(default_factory=lambda: deque[float]())
    streak: int = 0
    retry_at: float = 0
    next_probe: float = 0
    state: str = "pending"


class Supervisor:
    def __init__(self, root: Path, components: tuple[Component, ...], tick: float = 0.2) -> None:
        self.root = root.resolve()
        self.owner = OwnerLock(self.root)
        self.components = [Runtime(component) for component in components]
        self.tick = tick
        self.stopping = False
        self.started = time.monotonic()
        self.state = "starting"

    def event(self, event: str, **fields: object) -> None:
        row = (
            json.dumps(
                {"time": time.time(), "event": event, "supervisor_pid": os.getpid(), **fields},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        descriptor = os.open(
            self.root / "supervisor.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            os.write(descriptor, row.encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def snapshot(self) -> None:
        now = time.monotonic()
        atomic_json(
            self.root / "supervisor-status.json",
            {
                "state": self.state,
                "pid": os.getpid(),
                "owner": asdict(self.owner.identity) if self.owner.identity else None,
                "uptime": now - self.started,
                "updated_at": time.time(),
                "components": {
                    runtime.spec.name: {
                        "state": runtime.state,
                        "pid": runtime.process.pid if runtime.process else None,
                        "pgid": runtime.identity.pgid if runtime.identity else None,
                        "identity": asdict(runtime.identity) if runtime.identity else None,
                        "uptime": max(0, now - runtime.started) if runtime.process else 0,
                        "restart_count": runtime.restart_count,
                        "last_heartbeat": runtime.last_heartbeat,
                        "last_error": runtime.last_error,
                        "retry_in": max(0, runtime.retry_at - now),
                    }
                    for runtime in self.components
                },
            },
        )

    def launch(self, runtime: Runtime) -> None:
        runtime.started = time.monotonic()
        runtime.started_wall = time.time()
        runtime.last_ok = runtime.started
        runtime.last_heartbeat = None
        runtime.next_probe = 0
        with (self.root / (runtime.spec.name + ".output.log")).open("ab", buffering=0) as output:
            runtime.process = subprocess.Popen(
                runtime.spec.command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if key in {"LANG", "LC_ALL", "LC_CTYPE", "TZ"}
                }
                | {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            )
        try:
            runtime.identity = process_table().get(runtime.process.pid)
            if runtime.identity is None:
                # An immediate exit is a normal component failure, not startup success.
                runtime.process.wait(timeout=2)
            elif (
                runtime.identity.pgid != runtime.process.pid or runtime.identity.uid != os.getuid()
            ):
                raise RuntimeError("child identity mismatch")
            runtime.state = "starting"
            self.event(
                "component_started",
                component=runtime.spec.name,
                pid=runtime.process.pid,
                restart_count=runtime.restart_count,
            )
        except BaseException:
            # Popen ownership is established before identity/status operations.
            self.cleanup(runtime)
            raise

    def observe(self, runtime: Runtime, table: dict[int, Identity]) -> None:
        if runtime.process is None:
            return
        parents = {
            identity.pid
            for identity in runtime.witnessed.values()
            if same_identity(identity, table.get(identity.pid))
        }
        if runtime.identity is not None and same_identity(
            runtime.identity, table.get(runtime.identity.pid)
        ):
            parents.add(runtime.identity.pid)
        elif runtime.identity is None and runtime.process.poll() is None:
            parents.add(runtime.process.pid)
        changed = True
        while changed:
            changed = False
            for identity in table.values():
                if (
                    identity.pid not in parents
                    and identity.ppid in parents
                    and identity.uid == os.getuid()
                ):
                    runtime.witnessed[identity.pid] = identity
                    parents.add(identity.pid)
                    changed = True
        if runtime.identity is not None:
            for identity in table.values():
                if (
                    identity.pgid == runtime.identity.pgid
                    and identity.uid == os.getuid()
                    and identity.pid != runtime.identity.pid
                ):
                    runtime.witnessed[identity.pid] = identity

    def cleanup(self, runtime: Runtime) -> None:
        process = runtime.process
        if process is None:
            return
        table = process_table()
        self.observe(runtime, table)
        owned = dict(runtime.witnessed)
        if runtime.identity is not None:
            owned[runtime.identity.pid] = runtime.identity

        # Existing web/task launchers can create nested sessions. Also terminate
        # witnessed owned descendant groups, but never an unverified reused PID.
        def notify(signum: int) -> None:
            current = process_table()
            groups: set[int] = set()
            for identity in owned.values():
                if same_identity(identity, current.get(identity.pid)):
                    groups.add(identity.pgid)
            if runtime.identity is None and process.poll() is None:
                # Direct Popen handle, still unreaped: PID cannot yet be reused.
                groups.add(process.pid)
            for pgid in groups:
                if pgid == os.getpgrp():
                    raise RuntimeError("refusing to signal supervisor process group")
                try:
                    os.killpg(pgid, signum)
                except ProcessLookupError:
                    pass

        notify(signal.SIGTERM)
        deadline = time.monotonic() + runtime.spec.stop_timeout
        while time.monotonic() < deadline:
            process.poll()
            current = process_table()
            self.observe(runtime, current)
            owned.update(runtime.witnessed)
            if (
                not any(
                    same_identity(identity, current.get(identity.pid))
                    for identity in owned.values()
                )
                and process.poll() is not None
            ):
                break
            time.sleep(min(self.tick, 0.05))
        notify(signal.SIGKILL)
        process.wait(timeout=3)
        self.event(
            "component_stopped",
            component=runtime.spec.name,
            pid=process.pid,
            exit_code=process.returncode,
        )
        runtime.process = None
        runtime.identity = None
        runtime.witnessed.clear()

    def mark_healthy(self, runtime: Runtime, heartbeat: float | None) -> None:
        now = time.monotonic()
        heartbeat_age = (
            max(0, time.time() - heartbeat)
            if runtime.spec.health.kind == "heartbeat" and heartbeat is not None
            else 0
        )
        runtime.last_ok = now - heartbeat_age
        runtime.last_heartbeat = heartbeat
        runtime.state = "healthy"

    def failed(self, runtime: Runtime, reason: str) -> None:
        elapsed = time.monotonic() - runtime.started
        self.cleanup(runtime)
        now = time.monotonic()
        runtime.last_error = reason
        if elapsed >= runtime.spec.restart.stable_after:
            runtime.streak = 0
            runtime.failures.clear()
        runtime.streak += 1
        runtime.failures.append(now)
        while runtime.failures and now - runtime.failures[0] > runtime.spec.restart.crash_window:
            runtime.failures.popleft()
        if len(runtime.failures) >= runtime.spec.restart.crash_limit:
            runtime.state = "circuit_open"
            self.event(
                "circuit_open",
                component=runtime.spec.name,
                reason=reason,
                failures=len(runtime.failures),
            )
        else:
            delay = min(
                runtime.spec.restart.max_delay,
                runtime.spec.restart.base_delay * 2 ** min(runtime.streak - 1, 20),
            )
            runtime.retry_at = now + delay
            runtime.state = "backoff"
            self.event("restart_scheduled", component=runtime.spec.name, reason=reason, delay=delay)

    def run(self) -> int:
        self.owner.acquire()
        handlers: dict[int, Any] = {}
        exit_code = 0

        def stop(signum: int, frame: object) -> None:
            self.stopping = True

        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                handlers[signum] = signal.signal(signum, stop)
            self.event("supervisor_started")
            for runtime in self.components:
                if self.stopping:
                    break
                self.launch(runtime)
            self.state = "running"
            while not self.stopping:
                table = process_table()
                for runtime in self.components:
                    now = time.monotonic()
                    if self.stopping:
                        break
                    if runtime.process is None:
                        if runtime.state == "backoff" and now >= runtime.retry_at:
                            runtime.restart_count += 1
                            self.launch(runtime)
                        continue
                    self.observe(runtime, table)
                    code = runtime.process.poll()
                    if code is not None:
                        self.failed(runtime, f"process exited: {code}")
                        continue
                    if now >= runtime.next_probe:
                        ok, heartbeat, error = probe(runtime.spec.health, runtime.started_wall)
                        runtime.next_probe = time.monotonic() + runtime.spec.health.interval
                        if ok:
                            self.mark_healthy(runtime, heartbeat)
                        elif (
                            time.monotonic() - runtime.started >= runtime.spec.health.startup_grace
                            and time.monotonic() - runtime.last_ok >= runtime.spec.health.timeout
                        ):
                            self.failed(runtime, "health timeout: " + str(error))
                self.snapshot()
                time.sleep(self.tick)
        except Exception as exc:
            exit_code = 1
            self.event("supervisor_error", reason=type(exc).__name__ + ": " + str(exc))
        finally:
            self.state = "stopping"
            try:
                for runtime in reversed(self.components):
                    try:
                        self.cleanup(runtime)
                        if runtime.state != "circuit_open":
                            runtime.state = "stopped"
                    except Exception as exc:
                        exit_code = 1
                        self.event("cleanup_error", component=runtime.spec.name, reason=str(exc))
                self.state = "stopped" if exit_code == 0 else "failed"
                self.snapshot()
                self.event("supervisor_stopped", exit_code=exit_code)
            finally:
                for signum, handler in handlers.items():
                    signal.signal(signum, handler)
                self.owner.release()
        return exit_code


def read_status(root: Path) -> dict[str, Any]:
    """Never mkdir, flock, signal or rewrite state. Atomic snapshot may be stale."""
    path = root / "supervisor-status.json"
    try:
        value = cast(dict[str, Any], json.loads(path.read_text()))
        owner = value.get("owner")
        value["owner_alive"] = bool(
            owner and same_identity(Identity(**owner), process_table().get(int(owner["pid"])))
        )
        value["snapshot_age"] = max(0, time.time() - float(value["updated_at"]))
        return value
    except FileNotFoundError:
        return {"state": "not_started", "owner_alive": False}


def self_evaluate() -> dict[str, object]:
    """Exercise the real identity predicate; no ps, locks, state or signals."""
    from dataclasses import replace
    from datetime import datetime, timezone

    identity = Identity(101, 1, 101, 1000, "evaluation-generation", "evaluation-command")
    changes = (
        {"pid": 102},
        {"pgid": 102},
        {"uid": 1001},
        {"started": "other-generation"},
        {"command_hash": "other-command"},
    )
    checks = {
        "identity_matches_itself": same_identity(identity, identity),
        "pid_reuse_rejected": not same_identity(identity, None)
        and all(not same_identity(identity, replace(identity, **change)) for change in changes),
    }
    return {
        "module": "supervisor",
        "domain": "POSIX process supervision PID reuse identity lifecycle shutdown",
        "current_version": "1.0.0",
        "known_limitations": [
            "Single-owner POSIX supervisor, not a task queue, OS sandbox or exactly-once executor.",
            "PID identity predicate checks do not prove process health, owner liveness or descendant cleanup.",
            "Status snapshots can be stale; this hook does not read them or launch ps.",
        ],
        "improvement_directions": [
            "Continue fault-injection coverage for descendant creation during shutdown grace periods."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "ok": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "status"])
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--components", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--test-port", type=int)
    parser.add_argument("--acceptance-mode", action="store_true")
    parser.add_argument("--tick", type=float, default=0.2)
    args = parser.parse_args()
    try:
        if args.action == "status":
            print(json.dumps(read_status(args.data_root), ensure_ascii=False, sort_keys=True))
            return 0
        if not args.data_root.is_absolute() or not 0.01 <= args.tick <= 10:
            raise ValueError("absolute data root and tick 0.01..10 required")
        if args.components is not None:
            components = load_components(args.components)
        else:
            if args.config is None or args.key_file is None or not 1 <= args.port <= 65535:
                raise ValueError("--config, --key-file and fixed --port required")
            components = default_components(
                args.data_root.resolve(),
                args.config,
                args.key_file,
                args.port,
                acceptance_mode=args.acceptance_mode,
                test_port=args.test_port,
            )
        return Supervisor(args.data_root, components, args.tick).run()
    except (OSError, ValueError, KeyError, TypeError, AlreadyRunning) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
