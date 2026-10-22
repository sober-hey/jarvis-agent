#!/usr/bin/env python3
"""Disposable real-web acceptance: zero model requests, FAKE credentials only."""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[2]


def wait(predicate: Callable[[], bool], timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("acceptance condition timed out")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    evidence: Path = args.evidence
    evidence.mkdir(parents=True, exist_ok=True)
    requests: list[str] = []

    class Provider(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(self.path)
            self.send_response(500)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    provider = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    # Product acceptance explicitly requires /tmp, not the host's scratch root.
    temporary = Path(tempfile.mkdtemp(prefix="jarvis-acceptance-supervisor-", dir="/tmp")).resolve()
    temporary.chmod(0o700)
    data = temporary / "data"
    data.mkdir(mode=0o700)
    config = temporary / "config.json"
    config.write_text(
        json.dumps(
            {
                "model": "fake-test-model",
                "provider": "custom",
                "base_url": f"http://127.0.0.1:{provider.server_port}/v1",
                "api_mode": "chat_completions",
                "context_length": 256000,
                "reasoning_effort": "high",
            }
        )
    )
    config.chmod(0o600)
    key = temporary / "fake-key"
    key.write_text("FAKE-SUPERVISOR-ACCEPTANCE-NONSECRET")
    key.chmod(0o600)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process: subprocess.Popen[bytes] | None = None
    result: dict[str, Any] = {
        "real_model_calls": 0,
        "temporary_root": str(temporary),
        "port": port,
        "checks": {},
    }
    all_pids: set[int] = set()
    try:
        with (evidence / "acceptance-supervisor.stdout.log").open("wb") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(PROJECT / "packaging/supervisor.py"),
                    "run",
                    "--data-root",
                    str(data),
                    "--config",
                    str(config),
                    "--key-file",
                    str(key),
                    "--port",
                    str(port),
                    "--test-port",
                    str(provider.server_port),
                    "--acceptance-mode",
                    "--tick",
                    "0.05",
                ],
                stdout=output,
                stderr=output,
            )

        def status() -> dict[str, Any]:
            try:
                return json.loads((data / "supervisor-status.json").read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                return {}

        def healthy() -> bool:
            value = status()
            return len(value.get("components", {})) == 2 and all(
                row["state"] == "healthy" for row in value["components"].values()
            )

        wait(healthy)
        initial = status()
        all_pids.update(row["pid"] for row in initial["components"].values())
        result["initial_status"] = initial
        result["checks"]["real_web_and_fixture_healthy"] = True
        for name in ("web", "inner-placeholder"):
            old = status()["components"][name]["pid"]
            os.kill(old, signal.SIGKILL)
            wait(
                lambda name=name, old=old: healthy() and status()["components"][name]["pid"] != old
            )
            current = status()["components"][name]
            all_pids.add(current["pid"])
            result["checks"][name + "_sigkill_restarted"] = current["restart_count"] == 1
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        result["checks"]["health_after_restarts"] = response.status == 200
        response.read()
        connection.close()
        read = subprocess.run(
            [
                sys.executable,
                str(PROJECT / "packaging/supervisor.py"),
                "status",
                "--data-root",
                str(data),
            ],
            capture_output=True,
            check=True,
            timeout=2,
        )
        result["live_status"] = json.loads(read.stdout)
        result["checks"]["status_owner_verified"] = result["live_status"]["owner_alive"]
        process.terminate()
        result["supervisor_exit"] = process.wait(timeout=15)
        result["checks"]["sigterm_exit_zero"] = result["supervisor_exit"] == 0
        result["final_status"] = status()
        for pid in all_pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            raise AssertionError(f"owned child {pid} survived")
        result["checks"]["all_owned_pids_gone"] = True
        with socket.socket() as probe:
            result["checks"]["port_closed"] = probe.connect_ex(("127.0.0.1", port)) != 0
        result["checks"]["provider_received_zero_requests"] = requests == []
        for name in ("supervisor.log", "web.output.log", "inner-placeholder.output.log"):
            shutil.copy2(data / name, evidence / ("acceptance-" + name))
        result["ok"] = all(result["checks"].values())
    except Exception as exc:
        result["ok"] = False
        result["error"] = repr(exc)
        for path in data.glob("*.log"):
            shutil.copy2(path, evidence / ("acceptance-failed-" + path.name))
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=2)
        shutil.rmtree(temporary)
        result["checks"]["temporary_root_removed"] = not temporary.exists()
        result["fake_provider_requests"] = len(requests)
        (evidence / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
