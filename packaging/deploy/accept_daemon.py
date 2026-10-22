"""Offline, accelerated process acceptance with SIGKILL and supervisor recovery."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chat_launcher as launcher  # noqa: E402
from daemon_wiring import component  # noqa: E402


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_for(
    path: Path, predicate: Callable[[dict[str, Any]], bool], timeout: float = 15
) -> dict[str, Any]:
    expires = time.monotonic() + timeout
    while time.monotonic() < expires:
        try:
            state = json.loads(path.read_text())
            if predicate(state):
                return dict(state)
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.1)
    raise TimeoutError(str(path))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--seconds", type=float, default=120)
    args = parser.parse_args()
    evidence: Path = args.evidence_dir
    evidence.mkdir(parents=True, exist_ok=False)
    sandbox = Path(tempfile.mkdtemp(prefix="jarvis-acceptance-daemon-", dir="/tmp")).resolve()
    root = sandbox / "data"
    root.mkdir(mode=0o700)
    manifest = sandbox / "components.json"
    definition = component(root, fixture=True, speed=60)
    manifest.write_text(json.dumps({"components": [asdict(definition)]}))
    script = Path(__file__).resolve().parents[1] / "supervisor.py"
    state_path = root / "supervisor-status.json"
    result: dict[str, Any] = {"ok": False, "fake_model_only": True, "real_model_calls": 0}
    process: subprocess.Popen[str] | None = None
    pids: list[int] = []
    began = time.monotonic()
    try:
        with (evidence / "supervisor.log").open("w") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(script),
                    "run",
                    "--data-root",
                    str(root),
                    "--acceptance-mode",
                    "--components",
                    str(manifest),
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            state = wait_for(state_path, lambda s: s["components"]["jarvis-daemon"]["pid"])
            first = int(state["components"]["jarvis-daemon"]["pid"])
            pids.append(first)
            wait_for(root / "daemon.heartbeat", lambda s: s["ticks"] >= 2)
            # Real time is ~2 min; logical time runs 60x and crosses 23:30.
            kill_at = began + args.seconds / 2
            while time.monotonic() < kill_at:
                time.sleep(0.25)
            os.kill(first, signal.SIGKILL)
            state = wait_for(
                state_path,
                lambda s: (
                    s["components"]["jarvis-daemon"]["pid"]
                    and int(s["components"]["jarvis-daemon"]["pid"]) != first
                ),
            )
            second = int(state["components"]["jarvis-daemon"]["pid"])
            pids.append(second)
            wait_for(root / "daemon.heartbeat", lambda s: s["pid"] == second and s["ticks"] >= 2)
            while time.monotonic() < began + args.seconds:
                time.sleep(0.25)
            os.kill(process.pid, signal.SIGTERM)
            exit_code = process.wait(timeout=20)
            result.update(supervisor_exit_code=exit_code, first_pid=first, restarted_pid=second)
        with sqlite3.connect(root / "mind.db") as db:
            rows = db.execute("SELECT category,payload FROM entries").fetchall()
        events = [
            json.loads(r[1])
            for r in rows
            if r[0] == "event" and json.loads(r[1])["data"].get("daemon_event_id")
        ]
        with sqlite3.connect(root / "outreach.db") as db:
            queued = db.execute("SELECT id,text,dedupe_key FROM messages").fetchall()
            deliveries = db.execute("SELECT id,text FROM deliveries").fetchall()
        with sqlite3.connect(root / "daemon.db") as db:
            logs = db.execute("SELECT event,fields FROM log ORDER BY seq").fetchall()
            budgets = db.execute("SELECT day,phase,state FROM budget ORDER BY id").fetchall()
        with sqlite3.connect(root / "reflection.db") as db:
            reflections = db.execute("SELECT day,status FROM reflections").fetchall()
        timeline = launcher.read_timeline(root)
        questions = [
            json.loads(r[1])
            for r in rows
            if r[0] == "question_state" and json.loads(r[1]).get("state") == "ask_sir"
        ]
        heartbeat = json.loads((root / "daemon.heartbeat").read_text())
        final_state = json.loads(state_path.read_text())
        checks = {
            "supervisor_restarted_after_sigkill": first != second,
            "one_perception_after_restart": len(events) == 1,
            "one_mind_outreach_message": sum(str(r[2]).startswith("mind:") for r in queued) == 1,
            "generated_question_exactly_enqueued": bool(queued)
            and bool(questions)
            and queued[0][1] == questions[0]["text"],
            "one_public_timeline_message": len(timeline) == 1
            and timeline[0]["content"] == queued[0][1],
            "reflection_once_completed": len(reflections) == 1 and reflections[0][1] == "completed",
            "heartbeat_stopped": heartbeat["phase"] == "stopped",
            "supervisor_clean_exit": exit_code == 0 and final_state["state"] == "stopped",
            "all_processes_gone": not any(alive(pid) for pid in pids),
            "no_stage_error": not any(event == "stage_error" for event, _ in logs),
        }
        result.update(
            checks=checks,
            events=events,
            messages=queued,
            public_timeline=timeline,
            questions=questions,
            deliveries=deliveries,
            budget=budgets,
            reflections=reflections,
            logical_clock=heartbeat,
            runtime_seconds=round(time.monotonic() - began, 3),
        )
        (evidence / "daemon-events.json").write_text(json.dumps(logs, ensure_ascii=False, indent=2))
        (evidence / "final-supervisor-state.json").write_text(json.dumps(final_state, indent=2))
        result["ok"] = all(checks.values())
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
    finally:
        if process is not None and process.poll() is None:
            os.kill(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        for pid in pids:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
        # Preserve only logs needed to diagnose; never retain the product data root.
        for log in root.glob("*.log"):
            shutil.copy2(log, evidence / log.name)
        log_root = root / "supervisor-logs"
        if log_root.is_dir():
            shutil.copytree(log_root, evidence / "component-logs")
        shutil.rmtree(sandbox)
        result["temporary_root_removed"] = not sandbox.exists()
        result["ok"] = result["ok"] and result["temporary_root_removed"]
        (evidence / "RESULTS.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
