# pyright: strict
"""Run a disposable default-component soak; real mode is explicit and opt-in."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

# Five wall seconds resolve short restart/degraded episodes while bounding read-only
# SQLite/ps/lsof overhead to 17,281 samples per 24h; independent of virtual speed.
SAMPLE_SECONDS = 5.0
COPY_TREES = ("packaging", "apps", "components/agent_engine/upstream")
COUNTERS = (
    "enqueued",
    "delivered",
    "mind_enqueued",
    "consumed",
    "suspended",
    "pending",
    "upstream_pending",
    "duplicate_processing",
    "duplicate_consumption",
    "thoughts",
    "scheduler_executions",
    "ask_sir_queue",
    "active_messages",
    "model_success",
    "model_failed",
    "model_unknown",
    "handoff_unaccounted",
)
Record = dict[str, Any]

# Never persist arbitrary exception text: even subprocess errors can contain argv
# with credentials. Known harness reasons are a closed vocabulary, not a regex.
SAFE_REASONS = frozenset(
    {
        "SupervisorExitedDuringStartup",
        "SupervisorExitedDuringSoak",
        "InjectionIdentityMismatch",
        "FrozenSnapshotHashMismatch",
        "StartupInterrupted",
        "DefaultComponentsStartupTimeout",
        "ModelOutcomeUnknown",
        "SoakTotalCallLimit",
        "SoakCallAlreadyAttempted",
    }
)


def exception_evidence(exc: BaseException) -> Record:
    frames: list[Record] = []
    tb = exc.__traceback__
    while tb is not None:
        frames.append(
            {
                "file": Path(tb.tb_frame.f_code.co_filename).name,
                "line": tb.tb_lineno,
                "function": tb.tb_frame.f_code.co_name,
            }
        )
        tb = tb.tb_next
    text = str(exc)
    result: Record = {
        "type": type(exc).__name__,
        "message": text if text in SAFE_REASONS else "[redacted; see type and frames]",
        "frames": frames,
    }
    if isinstance(exc, subprocess.TimeoutExpired):
        command: Any = exc.cmd
        program = (
            str(cast(list[Any], command)[0])
            if isinstance(command, (list, tuple)) and command
            else ""
        )
        result.update(operation=Path(str(program)).name, timeout_seconds=exc.timeout)
    if isinstance(exc, OSError):
        result["errno"] = exc.errno
    return result


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def read_object(path: Path) -> Record:
    try:
        value: object = json.loads(path.read_text())
        return cast(Record, value) if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def manifest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            result[path.relative_to(root).as_posix()] = digest.hexdigest()
        elif path.is_symlink():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                ("symlink:" + os.readlink(path)).encode()
            ).hexdigest()
    return result


def snapshot_repository(repo: Path, destination: Path) -> dict[str, str]:
    for name in COPY_TREES:
        shutil.copytree(repo / name, destination / name, symlinks=True)
    # Freeze executable trees, not git/history/private data or the development venv.
    return manifest(destination)


def read_database(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
    database.execute("PRAGMA query_only=ON")
    database.execute("BEGIN")
    return database


def collect_counters(root: Path) -> tuple[dict[str, int | None], list[str]]:
    counts: dict[str, int | None] = dict.fromkeys(COUNTERS)
    errors: list[str] = []
    delivered_ids: set[str] | None = None
    try:
        with closing(read_database(root / "daemon.db")) as db:
            counts["enqueued"], counts["delivered"] = map(
                int,
                db.execute("SELECT count(*),coalesce(sum(delivered=1),0) FROM inbox").fetchone(),
            )
            delivered_ids = {
                str(row[0]) for row in db.execute("SELECT id FROM inbox WHERE delivered=1")
            }
            counts["upstream_pending"] = counts["enqueued"] - counts["delivered"]
            table = (
                "soak_model_calls"
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='soak_model_calls'"
                ).fetchone()
                else "budget"
            )
            states = dict(db.execute(f"SELECT state,count(*) FROM {table} GROUP BY state"))
            counts["model_success"] = int(states.get("completed", 0))
            counts["model_failed"] = int(states.get("failed", 0))
            counts["model_unknown"] = int(states.get("dispatched_unknown", 0)) + int(
                states.get("unknown_abandoned", 0)
            )
    except (sqlite3.Error, OSError, TypeError) as exc:
        errors.append("daemon_counts:" + type(exc).__name__)
    try:
        with closing(read_database(root / "mind.db")) as db:
            # Extract only IDs/states/counts in SQL; NEVER export private payloads.
            senses = db.execute(
                "SELECT id,json_extract(payload,'$.data.daemon_event_id') FROM entries "
                "WHERE category='event' AND "
                "json_extract(payload,'$.data.daemon_event_id') IS NOT NULL"
            ).fetchall()
            mind_ids = {str(row[0]) for row in senses}
            upstream = [str(row[1]) for row in senses]
            consumed_rows = [
                str(row[0])
                for row in db.execute(
                    "SELECT json_extract(payload,'$.event_id') FROM entries "
                    "WHERE category='daemon_sense_consumed'"
                )
            ]
            suspended = {
                str(row[0])
                for row in db.execute(
                    "SELECT json_extract(payload,'$.event_id') FROM entries "
                    "WHERE category='daemon_sense_suspended'"
                )
            }
            consumed = set(consumed_rows)
            if delivered_ids is not None:
                counts["handoff_unaccounted"] = len(delivered_ids - set(upstream))
            counts["mind_enqueued"] = len(mind_ids)
            counts["consumed"] = len(consumed)
            counts["suspended"] = len(suspended)
            counts["pending"] = len(mind_ids - consumed - suspended)
            counts["duplicate_processing"] = len(upstream) - len(set(upstream))
            counts["duplicate_consumption"] = len(consumed_rows) - len(consumed)
            categories = dict(db.execute("SELECT category,count(*) FROM entries GROUP BY category"))
            counts["thoughts"] = int(categories.get("thought", 0))
            counts["scheduler_executions"] = int(categories.get("action", 0))
            # Latest question state per stable question ID, not historical transitions.
            questions: dict[str, str] = {}
            for identity, state in db.execute(
                "SELECT json_extract(payload,'$.question_id'),"
                "json_extract(payload,'$.state') FROM entries "
                "WHERE category='question_state' ORDER BY seq"
            ):
                questions[str(identity)] = str(state)
            counts["ask_sir_queue"] = sum(
                state in {"ask_sir", "pending_sir_permission"} for state in questions.values()
            )
    except (sqlite3.Error, OSError, TypeError) as exc:
        errors.append("mind_counts:" + type(exc).__name__)
    try:
        with closing(read_database(root / "outreach.db")) as db:
            counts["active_messages"] = int(
                db.execute("SELECT count(*) FROM messages").fetchone()[0]
            )
    except (sqlite3.Error, OSError, TypeError) as exc:
        errors.append("outreach_counts:" + type(exc).__name__)
    return counts, errors


def resources(pid: int | None) -> Record:
    result: Record = {"rss_bytes": None, "cpu_percent": None, "open_files": None}
    if pid is None:
        return result
    try:
        output = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "rss=,pcpu="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout.split()
        if len(output) == 2:
            result.update(rss_bytes=int(output[0]) * 1024, cpu_percent=float(output[1]))
        # Count numeric descriptors only: exclude cwd/txt/mem pseudo-descriptors.
        lsof = shutil.which("lsof", path="/usr/sbin:/usr/bin:/sbin:/bin")
        if lsof:
            output = subprocess.run(
                [lsof, "-nP", "-a", "-p", str(pid), "-Ff"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if output.returncode == 0:
                result["open_files"] = sum(
                    line.startswith("f") and line[1:].isdigit()
                    for line in output.stdout.splitlines()
                )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return result


def sample(root: Path, elapsed: float, speed: float) -> Record:
    status = read_object(root / "supervisor-status.json")
    heartbeat = read_object(root / "daemon.heartbeat")
    components: Record = {}
    for name in ("web", "jarvis-daemon"):
        component: Record = status.get("components", {}).get(name, {})
        pid = cast(int | None, component.get("pid"))
        health = "healthy" if component.get("state") == "healthy" else "unhealthy"
        if name == "jarvis-daemon":
            # Never accept a previous process generation's heartbeat.
            health = (
                str(heartbeat.get("status", "unknown"))
                if (
                    heartbeat.get("pid") == pid
                    and component.get("state") == "healthy"
                    and 0 <= time.time() - float(heartbeat.get("wall_time", 0)) <= 2130
                )
                else "unknown"
            )
        components[name] = {
            "pid": pid,
            "restart_count": component.get("restart_count"),
            "state": component.get("state", "unknown"),
            "heartbeat": health,
            **resources(pid),
        }
    counts, errors = collect_counters(root)
    sizes: dict[str, int] = {}
    for path in root.rglob("*"):
        if path.is_file() and any(
            path.name.endswith(suffix) for suffix in (".db", ".db-wal", ".db-shm")
        ):
            relative = path.relative_to(root).as_posix()
            # Nested filenames may be user-derived: keep only a stable opaque label.
            label = (
                path.name
                if path.parent == root
                else ("nested-" + hashlib.sha256(relative.encode()).hexdigest()[:20])
            )
            try:
                sizes[label] = path.stat().st_size
            except FileNotFoundError:
                # SQLite can retire WAL/SHM between directory listing and stat.
                errors.append("database_size:FileNotFoundError")
    return {
        "elapsed_seconds": elapsed,
        "logical_elapsed_seconds": elapsed * speed,
        "components": components,
        "counters": counts,
        "database_bytes": sizes,
        "observation_errors": errors,
    }


def _unknown_policy() -> tuple[float, frozenset[str]]:
    """Use the daemon's policy without starting work or opening its private inputs."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        import daemon_wiring

        # Old frozen soak snapshots predate these exports. New daemon policy wins
        # whenever present; the compatibility values must stay fail-closed/exact.
        seconds: float = getattr(daemon_wiring, "UNKNOWN_ABANDON_SECONDS", 1800)
        readonly: frozenset[str] = getattr(
            daemon_wiring,
            "READ_ONLY_MODEL_PHASES",
            frozenset(
                {
                    "thought",
                    "question",
                    "question_review",
                    "relevance",
                    "reflection",
                    "outreach",
                    "digest",
                }
            ),
        )
        return seconds, readonly
    finally:
        sys.path.pop(0)


def _call_metadata(prompt: str, phase: str, identity: str | None) -> tuple[str, str]:
    context: Record = {}
    try:
        # Reflection may prefix its JSON with an instruction, unlike other phases.
        raw: object = json.loads(prompt[prompt.index("{") :])
        if isinstance(raw, dict):
            context = cast(Record, raw)
    except (ValueError, TypeError):
        pass
    prompt_phase = context.get("phase")
    if phase == "thought" and isinstance(prompt_phase, str):
        phase = prompt_phase
    if identity is None:
        daemon_identity = context.get("_daemon_call_identity")
        if isinstance(daemon_identity, str) and daemon_identity:
            identity = daemon_identity
        elif (
            isinstance(prompt_phase, str)
            and type(context.get("round")) is int
            and context["round"] >= 0
        ):
            # Logical phase/round identity, not a hash of mutable/private prompt text.
            identity = (
                "soak-"
                + hashlib.sha256(
                    json.dumps([prompt_phase, context["round"]], ensure_ascii=False).encode()
                ).hexdigest()
            )
        else:
            # No safe semantic identity: a deliberate new call is a new operation.
            identity = uuid.uuid4().hex
    if not identity:
        raise ValueError("nonempty call identity required")
    return phase, identity


def _soak_schema(db: sqlite3.Connection, now: float) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS soak_model_calls(id INTEGER PRIMARY KEY,state TEXT NOT NULL)"
    )
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(soak_model_calls)")}
    additions = {
        "call_identity": "TEXT",
        "phase": "TEXT",
        "dispatched_at": "REAL",
        "daemon_budget_id": "INTEGER",
        "response_json": "TEXT",
        "error_type": "TEXT",
    }
    for name, kind in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE soak_model_calls ADD COLUMN {name} {kind}")
    # Two-column legacy reservations have no recoverable send time or phase. Start
    # their bounded wall-clock grace at migration; never delete/reset spent budget.
    db.execute(
        "UPDATE soak_model_calls SET call_identity='soak-legacy-'||id WHERE call_identity IS NULL"
    )
    db.execute("UPDATE soak_model_calls SET phase='legacy' WHERE phase IS NULL")
    db.execute("UPDATE soak_model_calls SET dispatched_at=? WHERE dispatched_at IS NULL", (now,))
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS soak_call_identity ON soak_model_calls(call_identity)"
    )


def _persisted_result(root: Path, identity: str) -> Record | None:
    # Never turn an arbitrary explicit identity into a path traversal or directory scan.
    if re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identity) is None:
        return None
    path = root / "launcher-attempts" / (identity + ".json")
    try:
        if path.is_symlink() or path.stat().st_size > 262144:
            return None
        record = read_object(path)
    except OSError:
        return None
    raw_result: object = record.get("result")
    result = cast(Record, raw_result) if isinstance(raw_result, dict) else None
    if (
        record.get("attempt_id") == identity
        and record.get("state") == "completed"
        and result is not None
        and result.get("ok") is True
        and not result.get("generation_degraded")
    ):
        return result
    return None


def budgeted_call(
    root: Path,
    maximum: int,
    invoke: Callable[[str], Record],
    prompt: str,
    *,
    phase: str = "thought",
    call_identity: str | None = None,
    clock: Callable[[], float] = time.time,
    result_probe: Callable[[str], Record | None] | None = None,
) -> Record:
    """Reserve once per identity; unknowns gate only same-phase side effects.

    All reservations (failed or abandoned included) spend the total soak budget.
    Reconciliation commits before a refusal: raising inside the transaction would
    silently roll back recovered responses and abandonment timestamps on retries.
    """
    abandon_seconds, readonly = _unknown_policy()
    now = clock()
    if not math.isfinite(now):
        raise ValueError("finite wall clock required")
    phase, call_identity = _call_metadata(prompt, phase, call_identity)
    refusal: str | None = None
    replay: Record | None = None
    identity: int | None = None
    with closing(sqlite3.connect(root / "daemon.db")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _soak_schema(db, now)
        # Correlate only the exact active reservation, never the newest unknown:
        # a concurrent or earlier read-only call may legitimately still be unknown.
        budget_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(budget)")}
        daemon_budget_id: int | None = None
        if {"id", "phase", "state", "call_identity"} <= budget_columns:
            active = db.execute(
                "SELECT id,phase FROM budget WHERE call_identity=? AND state='dispatched_unknown'",
                (call_identity,),
            ).fetchone()
            if active:
                daemon_budget_id, phase = int(active[0]), str(active[1])
        unknowns = db.execute(
            "SELECT id,call_identity,dispatched_at FROM soak_model_calls "
            "WHERE state IN ('dispatched_unknown','unknown_abandoned')"
        ).fetchall()
        for row_id, unknown_identity, dispatched_at in unknowns:
            response_json: str | None = None
            try:
                result: object = (
                    result_probe(str(unknown_identity))
                    if result_probe is not None
                    else _persisted_result(root, str(unknown_identity))
                )
                if isinstance(result, dict):
                    if result.get("ok") is not False and not result.get("generation_degraded"):
                        response_json = json.dumps(result, ensure_ascii=False, allow_nan=False)
            except Exception:
                pass  # An unreadable/invalid receipt is not proof of completion.
            child_alive = False
            if {"call_identity", "child_pid"} <= budget_columns:
                child = db.execute(
                    "SELECT child_pid FROM budget WHERE call_identity=?", (unknown_identity,)
                ).fetchone()
                if child and child[0] is not None:
                    try:
                        os.kill(int(child[0]), 0)
                        child_alive = True
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        child_alive = True
            if response_json is not None:
                db.execute(
                    "UPDATE soak_model_calls SET state='completed',response_json=? WHERE id=?",
                    (response_json, row_id),
                )
            elif not child_alive and now - float(dispatched_at) >= abandon_seconds:
                db.execute(
                    "UPDATE soak_model_calls SET state='unknown_abandoned' WHERE id=?", (row_id,)
                )
                if {"call_identity", "child_pid", "state"} <= budget_columns:
                    # Same transaction as the soak gate: a not-yet-registered
                    # child must not dispatch after we release that gate.
                    db.execute(
                        "UPDATE budget SET state='unknown_abandoned' "
                        "WHERE call_identity=? AND state='dispatched_unknown'",
                        (unknown_identity,),
                    )
        previous = db.execute(
            "SELECT state,response_json FROM soak_model_calls WHERE call_identity=?",
            (call_identity,),
        ).fetchone()
        if previous:
            if previous[0] == "completed" and previous[1] is not None:
                replay = cast(Record, json.loads(previous[1]))
            else:
                refusal = (
                    "ModelOutcomeUnknown"
                    if previous[0] in {"dispatched_unknown", "unknown_abandoned"}
                    else "SoakCallAlreadyAttempted"
                )
        elif (
            phase not in readonly
            and db.execute(
                "SELECT 1 FROM soak_model_calls WHERE state='dispatched_unknown' "
                "AND (phase=? OR phase='legacy') LIMIT 1",
                (phase,),
            ).fetchone()
        ):
            refusal = "ModelOutcomeUnknown"
        elif db.execute("SELECT count(*) FROM soak_model_calls").fetchone()[0] >= maximum:
            refusal = "SoakTotalCallLimit"
        else:
            identity = db.execute(
                "INSERT INTO soak_model_calls("
                "state,call_identity,phase,dispatched_at,daemon_budget_id) "
                "VALUES('dispatched_unknown',?,?,?,?)",
                (call_identity, phase, now, daemon_budget_id),
            ).lastrowid
    if refusal is not None:
        raise RuntimeError(refusal)
    if replay is not None:
        return replay
    assert identity is not None
    try:
        value = invoke(prompt)
    except Exception as exc:
        if (
            not any(cls.__name__ == "ModelOutcomeUnknown" for cls in type(exc).__mro__)
            and str(exc) != "ModelOutcomeUnknown"
        ):
            with closing(sqlite3.connect(root / "daemon.db")) as db, db:
                db.execute(
                    "UPDATE soak_model_calls SET state='failed',error_type=? WHERE id=?",
                    (type(exc).__name__, identity),
                )
        raise
    # BaseException deliberately bypasses classification, retaining unknown. A
    # post-response persistence failure also retains unknown for later reconciliation.
    with closing(sqlite3.connect(root / "daemon.db")) as db, db:
        db.execute(
            "UPDATE soak_model_calls SET state='completed',response_json=? WHERE id=?",
            (json.dumps(value, ensure_ascii=False, allow_nan=False), identity),
        )
    return value


def daemon_worker(options: Record) -> int:
    import daemon_wiring as wiring
    import jarvis_daemon as daemon

    root = Path(options["root"])
    if options["mode"] == "fake":
        original = wiring.FixtureModel

        class Model(original):
            def __call__(self, prompt: str) -> Record:
                if (root / "soak-fail-model").exists():
                    raise OSError("SyntheticSoakFailure")
                return super().__call__(prompt)

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            (root / "soak-forbidden-real-attempt").touch()
            raise AssertionError("RealModelOrCredentialAccessForbidden")

        def deny_network(event: str, arguments: tuple[object, ...]) -> None:
            if event == "socket.connect":
                (root / "soak-forbidden-real-attempt").touch()
                raise AssertionError("FakeSoakNetworkForbidden")

        sys.addaudithook(deny_network)

        original_source = wiring.FixtureAdapter
        clock = wiring.make_clock(root, float(options["speed"]))

        class Source(original_source):
            def poll(self, store: Any, since: Any = None) -> Any:
                from datetime import UTC, datetime

                bucket = int(clock() // 15)
                return super().poll(store, since) + [
                    self.event(
                        "speech",
                        datetime.fromtimestamp(bucket * 15, UTC),
                        "synthetic soak perception",
                        {"synthetic": True},
                        f"soak-{bucket}",
                    )
                ]

        wiring.FixtureAdapter = Source
        wiring.FixtureModel = Model
        wiring.launcher.read_key = forbidden
        wiring.launcher.launch = forbidden
        # Leaves only are fake; scheduler/DBs/consumption/retry/default supervisor real.
        sys.argv = [
            sys.argv[0],
            "--data-root",
            str(root),
            "--acceptance-mode",
            "--fixture",
            "--speed",
            str(options["speed"]),
        ]
    else:
        original_call = wiring.StableModel.__call__

        def capped(self: Any, prompt: str) -> Record:
            return budgeted_call(
                root,
                int(options["max_model_calls"]),
                lambda text: original_call(self, text),
                prompt,
            )

        wiring.StableModel.__call__ = capped
        sys.argv = [
            sys.argv[0],
            "--data-root",
            str(root),
            "--config",
            options["config"],
            "--key-file",
            options["key_file"],
        ]
    return int(daemon.main())


def supervisor_worker(options: Record) -> int:
    import daemon_wiring as wiring
    import supervisor

    original = wiring.component
    runner = Path(__file__).resolve()
    option_path = str(options["option_path"])

    def component(root: Path, **kwargs: Any) -> Any:
        spec = original(root, **kwargs)
        # Keep the real default component and all lifecycle policy. Only its entry
        # bootstrap wraps fake leaves / total send budget; no custom manifest.
        return replace(
            spec, command=(sys.executable, "-B", str(runner), "--worker-daemon", option_path)
        )

    wiring.component = component
    original_defaults = supervisor.default_components

    def defaults(*args: Any, **kwargs: Any) -> Any:
        specs = original_defaults(*args, **kwargs)
        # Expose real HTTP/static health, but never allow an incidental browser
        # POST to spend outside the daemon's persistent total call cap.
        return tuple(
            replace(
                spec,
                command=(
                    sys.executable,
                    "-B",
                    str(runner),
                    "--worker-web",
                    option_path,
                    *spec.command[3:],
                ),
            )
            if spec.name == "web"
            else spec
            for spec in specs
        )

    supervisor.default_components = defaults
    # The product catches its own errors and returns 1. Capture that active
    # exception here before it is swallowed, without modifying product sources
    # or exporting reason strings, source lines, locals, or raw output.
    original_event = supervisor.Supervisor.event

    def diagnostic_event(self: Any, event: str, **fields: object) -> None:
        if event in {"supervisor_error", "cleanup_error"}:
            exc = sys.exception()
            if exc is not None:
                with (Path(options["root"]) / "soak-diagnostics.jsonl").open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "event": event,
                                "time": time.time(),
                                "exception": exception_evidence(exc),
                            }
                        )
                        + "\n"
                    )
        original_event(self, event, **fields)

    supervisor.Supervisor.event = diagnostic_event
    original_signal = supervisor.signal.signal

    def recording_signal(signum: int, handler: Any) -> Any:
        if callable(handler) and signum in {signal.SIGTERM, signal.SIGINT}:

            def received(number: int, frame: Any) -> None:
                with (Path(options["root"]) / "soak-diagnostics.jsonl").open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "event": "supervisor_signal",
                                "time": time.time(),
                                "signal": signal.Signals(number).name,
                            }
                        )
                        + "\n"
                    )
                handler(number, frame)

            return original_signal(signum, received)
        return original_signal(signum, handler)

    supervisor.signal.signal = recording_signal
    sys.argv = [
        sys.argv[0],
        "run",
        "--data-root",
        options["root"],
        "--config",
        options["config"],
        "--key-file",
        options["key_file"],
        "--port",
        str(options["port"]),
    ]
    if options["mode"] == "fake":
        sys.argv += ["--acceptance-mode", "--test-port", str(options["fake_port"])]
    return int(supervisor.main())


def web_worker() -> int:
    import web_server

    def readonly(*args: Any, **kwargs: Any) -> Any:
        raise web_server.launcher.LauncherError("Soak web model writes disabled")

    web_server.launcher.launch = readonly

    def reject_post(self: Any) -> None:
        self.send(403, {"error": "soak_read_only"})

    web_server.Handler.do_POST = reject_post
    sys.argv = [sys.argv[0], *sys.argv[3:]]
    return int(web_server.main())


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def scoped_processes(snapshot: Path, data_parent: Path) -> list[int]:
    """Read process argv only in memory; export IDs, never raw command lines."""
    output = subprocess.run(
        ["/bin/ps", "-axww", "-o", "pid=,command="],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    found: list[int] = []
    for line in output.stdout.splitlines():
        fields = line.split(None, 1)
        if (
            len(fields) == 2
            and int(fields[0]) not in {os.getpid(), os.getppid()}
            and (str(snapshot) in fields[1] or str(data_parent) in fields[1])
        ):
            found.append(int(fields[0]))
    return found


def port_closed(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def reserve_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def run(args: argparse.Namespace) -> Record:
    evidence = Path(args.evidence_dir).expanduser().resolve()
    snapshot = Path(args.frozen_root).resolve()
    if Path(__file__).resolve().parents[2] != snapshot or not evidence.is_dir():
        raise ValueError("coordinator must execute from its frozen snapshot")
    parent = Path(tempfile.mkdtemp(prefix="jarvis-acceptance-soak-", dir="/tmp")).resolve()
    root = parent / "data"
    root.mkdir(mode=0o700)
    port = reserve_port()
    result: Record = {
        "ok": False,
        "mode": args.mode,
        "speed": args.speed,
        "requested_wall_seconds": args.duration_seconds,
        "sample_interval_seconds": SAMPLE_SECONDS,
        "snapshot_path": str(snapshot),
        "data_parent": str(parent),
        "max_model_calls": args.max_model_calls,
        "checks": {},
        "faults": [],
        "real_model_calls": 0 if args.mode == "fake" else None,
    }
    process: subprocess.Popen[bytes] | None = None
    before: dict[str, str] = {}
    tracked: set[int] = set()
    began = 0.0
    samples = 0
    interrupted = False
    previous_signals: dict[int, Any] = {}

    result["runner_pid"] = os.getpid()
    result["port"] = port
    result["started_at"] = time.time()
    result["received_signals"] = []

    def request_stop(signum: int, frame: object) -> None:
        nonlocal interrupted
        interrupted = True
        result["received_signals"].append(
            {"signal": signal.Signals(signum).name, "time": time.time()}
        )

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_signals[signum] = signal.signal(signum, request_stop)
    try:
        before = cast(dict[str, str], json.loads((evidence / "snapshot-sha256.json").read_text()))
        if manifest(snapshot) != before:
            raise RuntimeError("FrozenSnapshotHashMismatch")
        options: Record = {
            "mode": args.mode,
            "speed": args.speed,
            "root": str(root),
            "port": port,
            "max_model_calls": args.max_model_calls,
            "fake_port": reserve_port(),
        }
        if args.mode == "fake":
            config, key = parent / "config.json", parent / "fake-key"
            save(
                config,
                {
                    "model": "fake-test-model",
                    "provider": "custom",
                    "base_url": f"http://127.0.0.1:{options['fake_port']}/v1",
                    "api_mode": "chat_completions",
                    "context_length": 256000,
                    "reasoning_effort": "high",
                },
            )
            key.write_text("FAKE-SOAK-NONSECRET")
            config.chmod(0o600)
            key.chmod(0o600)
        else:
            # Paths only; only the existing product launcher reads real key material.
            config, key = Path(args.config).resolve(), Path(args.key_file).resolve()
        options.update(config=str(config), key_file=str(key))
        option_path = parent / "soak-options.json"
        options["option_path"] = str(option_path)
        save(option_path, options)
        option_path.chmod(0o600)
        runner = snapshot / "packaging/soak/run_soak.py"
        # Raw product output stays disposable with data, never copied to evidence.
        with (parent / "runtime.log").open("wb") as output:
            process = subprocess.Popen(
                [sys.executable, "-B", str(runner), "--worker-supervisor", str(option_path)],
                stdout=output,
                stderr=output,
                cwd=parent,
                start_new_session=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "en_US.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
        tracked.add(process.pid)
        save(evidence / "RUNNING.json", {**result, "supervisor_pid": process.pid})
        startup = time.monotonic() + 45
        while time.monotonic() < startup:
            current = read_object(root / "supervisor-status.json").get("components", {})
            if set(current) == {"web", "jarvis-daemon"} and all(
                c.get("state") == "healthy" for c in current.values()
            ):
                break
            if process.poll() is not None:
                raise RuntimeError("SupervisorExitedDuringStartup")
            if interrupted:
                raise InterruptedError("StartupInterrupted")
            time.sleep(0.1)
        else:
            raise TimeoutError("DefaultComponentsStartupTimeout")
        result["checks"]["default_components_healthy"] = True
        began = time.monotonic()
        next_sample = 0.0
        injected: set[str] = set()
        fault_plan = {
            "jarvis-daemon": args.kill_daemon_at
            if args.kill_daemon_at is not None
            else args.duration_seconds / 3,
            "web": args.kill_web_at
            if args.kill_web_at is not None
            else args.duration_seconds * 2 / 3,
        }
        with (evidence / "samples.jsonl").open("x") as stream:
            while True:
                elapsed = time.monotonic() - began
                if process.poll() is not None:
                    raise RuntimeError("SupervisorExitedDuringSoak")
                if args.inject_faults:
                    for name, due in fault_plan.items():
                        if elapsed >= due and name not in injected:
                            state = read_object(root / "supervisor-status.json")
                            component = state.get("components", {}).get(name, {})
                            if component.get("state") == "healthy" and component.get("pid"):
                                pid = int(component["pid"])
                                tracked.add(pid)
                                # SIGKILL only witnessed own child, verified via process start
                                # identity using the frozen supervisor's read-only helper.
                                sys.path.insert(0, str(snapshot / "packaging"))
                                import supervisor as frozen_supervisor

                                identity = component.get("identity")
                                if not identity or not frozen_supervisor.same_identity(
                                    frozen_supervisor.Identity(**identity),
                                    frozen_supervisor.process_table().get(pid),
                                ):
                                    raise RuntimeError("InjectionIdentityMismatch")
                                os.kill(pid, signal.SIGKILL)
                                result["faults"].append(
                                    {"component": name, "old_pid": pid, "elapsed_seconds": elapsed}
                                )
                                injected.add(name)
                    if args.mode == "fake":
                        fail_start = args.fail_model_at if args.fail_model_at is not None else 0
                        fail_end = fail_start + args.fail_model_seconds
                        marker = root / "soak-fail-model"
                        if fail_start <= elapsed < fail_end:
                            marker.touch(exist_ok=True)
                        else:
                            marker.unlink(missing_ok=True)
                if elapsed >= next_sample or elapsed >= args.duration_seconds or interrupted:
                    observation = sample(root, elapsed, args.speed)
                    tracked.update(
                        int(row["pid"]) for row in observation["components"].values() if row["pid"]
                    )
                    stream.write(json.dumps(observation, allow_nan=False) + "\n")
                    stream.flush()
                    samples += 1
                    next_sample += SAMPLE_SECONDS
                if elapsed >= args.duration_seconds or interrupted:
                    break
                time.sleep(min(0.1, max(0.001, args.duration_seconds - elapsed)))
        result["observed_wall_seconds"] = time.monotonic() - began
        result["checks"]["duration_reached"] = (
            result["observed_wall_seconds"] >= args.duration_seconds and not interrupted
        )
        final = read_object(root / "supervisor-status.json")
        for fault in result["faults"]:
            current = final.get("components", {}).get(fault["component"], {})
            fault["new_pid"] = current.get("pid")
            fault["recovered"] = (
                current.get("pid") != fault["old_pid"]
                and current.get("state") == "healthy"
                and current.get("restart_count", 0) >= 1
            )
            result["checks"][fault["component"] + "_recovered"] = fault["recovered"]
        if args.inject_faults:
            result["checks"]["both_kills_injected"] = len(result["faults"]) == 2
            if args.mode == "fake":
                counters, _ = collect_counters(root)
                heartbeat = read_object(root / "daemon.heartbeat")
                result["checks"]["fake_failure_observed"] = (counters["model_failed"] or 0) > 0
                result["checks"]["cognition_recovered"] = heartbeat.get("status") == "healthy"
        result["sample_count"] = samples
        if args.mode == "fake":
            result["checks"]["no_real_model_or_credential_attempt"] = not (
                root / "soak-forbidden-real-attempt"
            ).exists()
        sys.path.insert(0, str(snapshot / "packaging/soak"))
        from analyze_soak import write_summary

        write_summary(evidence / "samples.jsonl", evidence / "analysis")
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = exception_evidence(exc)
        result["error_message"] = result["error"]["message"]
        result["error_location"] = (result["error"]["frames"] or [None])[-1]
    finally:
        result["sample_count"] = samples
        result["observed_wall_seconds"] = time.monotonic() - began if began else 0.0
        if process is not None:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=100)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
            result["supervisor_exit_code"] = process.returncode
            result["checks"]["sigterm_exit_zero"] = process.returncode == 0
        # Include starts missed between samples, without exporting raw log fields.
        try:
            for line in (root / "supervisor.log").read_text().splitlines():
                event = cast(Record, json.loads(line))
                if event.get("event") == "component_started":
                    tracked.add(int(event["pid"]))
        except (OSError, ValueError, KeyError):
            pass
        scoped = scoped_processes(snapshot, parent)
        survivors = sorted({pid for pid in tracked if pid_exists(pid)} | set(scoped))
        result["scoped_residual_pids"] = scoped
        result["tracked_pids"] = sorted(tracked)
        result["surviving_pids"] = survivors
        result["checks"]["all_recorded_pids_gone"] = not survivors
        result["checks"]["no_scoped_processes"] = not scoped
        result["checks"]["port_closed"] = port_closed(port)
        final = read_object(root / "supervisor-status.json")
        result["final_component_states"] = {
            name: row.get("state") for name, row in final.get("components", {}).items()
        }
        result["checks"]["components_stopped"] = set(result["final_component_states"]) == {
            "web",
            "jarvis-daemon",
        } and all(state == "stopped" for state in result["final_component_states"].values())
        if before:
            after = manifest(snapshot)
            result["snapshot_changed"] = sorted(
                key for key in set(before) | set(after) if before.get(key) != after.get(key)
            )
            result["checks"]["snapshot_unchanged"] = not result["snapshot_changed"]
        # Retain only structured lifecycle fields and sanitized exception stacks.
        # Raw output/DBs/configs never leave their disposable parent directory.
        lifecycle: list[Record] = []
        try:
            for line in (root / "supervisor.log").read_text().splitlines():
                event = json.loads(line)
                lifecycle.append(
                    {
                        key: event[key]
                        for key in (
                            "time",
                            "event",
                            "supervisor_pid",
                            "component",
                            "pid",
                            "exit_code",
                        )
                        if key in event
                    }
                )
        except (OSError, ValueError, TypeError):
            pass
        save(evidence / "supervisor-events.json", lifecycle)
        diagnostics = root / "soak-diagnostics.jsonl"
        if diagnostics.is_file():
            shutil.copyfile(diagnostics, evidence / diagnostics.name)
        result["supervisor_diagnostics"] = diagnostics.is_file()
        # Do not delete live process working trees: failed cleanup is explicit.
        if not survivors:
            shutil.rmtree(snapshot)
            shutil.rmtree(parent)
        result["checks"]["snapshot_removed"] = not snapshot.exists()
        result["checks"]["data_removed"] = not parent.exists()
        for signum, previous in previous_signals.items():
            signal.signal(signum, previous)
        result["ok"] = "error_type" not in result and all(result["checks"].values())
        save(evidence / "RESULTS.json", result)
        hashes = manifest(evidence)
        save(evidence / "evidence-sha256.json", hashes)
    return result


def freeze_and_launch(args: argparse.Namespace) -> int:
    repo = Path(__file__).resolve().parents[2]
    evidence = Path(args.evidence_dir).expanduser().resolve()
    if evidence == repo or repo in evidence.parents and ".runs_01" not in evidence.parts:
        raise ValueError("evidence must be outside source (or in a fresh .runs_01 child)")
    evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
    snapshot = Path(tempfile.mkdtemp(prefix="jarvis-soak-snapshot-", dir="/tmp")).resolve()
    child: subprocess.Popen[bytes] | None = None
    previous: dict[int, Any] = {}

    def forward(signum: int, frame: object) -> None:
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    try:
        before = snapshot_repository(repo, snapshot)
        save(evidence / "snapshot-sha256.json", before)
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, forward)
        source_arguments = list(sys.argv[1:])
        evidence_index = source_arguments.index("--evidence-dir") + 1
        source_arguments[evidence_index] = str(evidence)
        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(snapshot / "packaging/soak/run_soak.py"),
                *source_arguments,
                "--frozen-root",
                str(snapshot),
            ],
            cwd=evidence,
        )
        code = child.wait()
        return code
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        # Only pre-launch errors are ours to delete; a launched coordinator owns
        # cleanup and retains roots on failure rather than deleting beneath survivors.
        if child is None:
            shutil.rmtree(snapshot)


def product_daily_call_limit() -> int:
    """Read the product's single daily ceiling without starting daemon work.

    Both direct CLI execution and the frozen coordinator need the sibling product
    module search path. Do not duplicate the numeric budget in this harness.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        from daemon_wiring import MODEL_CALLS_PER_DAY

        return MODEL_CALLS_PER_DAY
    finally:
        sys.path.pop(0)


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker-web":
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        return web_worker()
    if len(sys.argv) == 3 and sys.argv[1] in {"--worker-supervisor", "--worker-daemon"}:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        options = read_object(Path(sys.argv[2]))
        return (
            supervisor_worker(options)
            if sys.argv[1] == "--worker-supervisor"
            else (daemon_worker(options))
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=["fake", "real"], default="fake")
    parser.add_argument("--duration-seconds", type=float, default=24 * 60 * 60)
    parser.add_argument("--speed", type=float, default=1)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument(
        "--max-model-calls",
        type=int,
        default=product_daily_call_limit(),
        help="total real-mode dispatch cap (default: daemon_wiring.MODEL_CALLS_PER_DAY)",
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--inject-faults", action="store_true")
    parser.add_argument("--kill-daemon-at", type=float)
    parser.add_argument("--kill-web-at", type=float)
    parser.add_argument("--fail-model-at", type=float)
    parser.add_argument("--fail-model-seconds", type=float, default=15)
    args = parser.parse_args()
    if not math.isfinite(args.duration_seconds) or args.duration_seconds <= 0:
        parser.error("positive finite wall duration required")
    if not math.isfinite(args.speed) or not 0 < args.speed <= 3600:
        parser.error("speed must be in (0,3600]")
    if args.max_model_calls <= 0:
        parser.error("positive total call cap required")
    if args.mode == "real" and (args.config is None or args.key_file is None or args.speed != 1):
        parser.error("real mode requires existing --config/--key-file and speed 1")
    if args.mode == "real" and (not args.config.is_absolute() or not args.key_file.is_absolute()):
        parser.error("real config/key-file references must be absolute (never inline keys)")
    if args.mode == "fake" and (args.config is not None or args.key_file is not None):
        parser.error("fake mode forbids real config/credential paths")
    for value in (
        args.kill_daemon_at,
        args.kill_web_at,
        args.fail_model_at,
        args.fail_model_seconds,
    ):
        if value is not None and (not math.isfinite(value) or value < 0):
            parser.error("fault offsets must be finite nonnegative wall seconds")
    try:
        if args.frozen_root is None:
            return freeze_and_launch(args)
        result = run(args)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error_type": type(exc).__name__}))
        return 2
    print(json.dumps({"ok": result["ok"], "evidence_dir": str(args.evidence_dir)}))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
