"""Real curl-to-DevServer SSE acceptance; isolated state and no model calls."""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from evidence_support import evidence_directory


def _lines(stream: TextIO, output: queue.Queue[str | None], saved: list[str]) -> None:
    try:
        for line in stream:
            saved.append(line)
            output.put(line)
    finally:
        output.put(None)


class AvatarCurlE2ETests(unittest.TestCase):
    def test_live_curl_stream_transports_affect_and_voice_without_reconnect(self) -> None:
        curl = shutil.which("curl")
        self.assertIsNotNone(curl, "curl is required for real transport acceptance")
        evidence = evidence_directory("avatar-curl-e2e")
        frames: list[dict[str, Any]] = []
        fixture_output: list[str] = []
        raw_stream: list[str] = []
        fixture_queue: queue.Queue[str | None] = queue.Queue()
        curl_queue: queue.Queue[str | None] = queue.Queue()
        workers: list[threading.Thread] = []
        server: subprocess.Popen[str] | None = None
        client: subprocess.Popen[str] | None = None
        result: dict[str, Any] = {"ok": False, "scope": "REAL_LOOPBACK_CURL_SSE"}

        def line_from(channel: queue.Queue[str | None]) -> str:
            try:
                line = channel.get(timeout=5)
            except queue.Empty:
                self.fail("live subprocess output timed out")
            self.assertIsNotNone(line, "live subprocess ended unexpectedly")
            assert line is not None
            return line

        def command(value: str) -> dict[str, Any]:
            assert server is not None and server.stdin is not None
            server.stdin.write(json.dumps({"command": value}) + "\n")
            server.stdin.flush()
            return json.loads(line_from(fixture_queue))

        def frame() -> dict[str, Any]:
            fields: dict[str, str] = {}
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                line = line_from(curl_queue).strip()
                if not line:
                    if "data" not in fields:
                        continue
                    self.assertEqual(fields.get("event"), "avatar")
                    payload = json.loads(fields["data"])
                    self.assertEqual(int(fields["id"]), payload["sequence"])
                    frames.append({"received_monotonic": time.monotonic(), "frame": payload})
                    return payload
                if ": " in line:
                    key, value = line.split(": ", 1)
                    fields[key] = value
            self.fail("no complete SSE avatar event")

        def until(predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                payload = frame()
                if predicate(payload):
                    return payload
            self.fail("live SSE state transition timed out")

        with tempfile.TemporaryDirectory(prefix=".jarvis-avatar-curl-", dir=Path.home()) as folder:
            fixture_root = Path(folder)
            with (
                (evidence / "server.stderr.log").open("w") as server_errors,
                (evidence / "curl.stderr.log").open("w") as curl_errors,
            ):
                try:
                    env = dict(os.environ)
                    env["PYTHONPATH"] = str(Path(__file__).resolve().parent)
                    env["PYTHONDONTWRITEBYTECODE"] = "1"
                    server = subprocess.Popen(
                        [
                            sys.executable,
                            "-B",
                            str(Path(__file__).with_name("verify_avatar_web.py")),
                            folder,
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=server_errors,
                        text=True,
                        bufsize=1,
                        env=env,
                    )
                    assert server.stdout is not None
                    worker = threading.Thread(
                        target=_lines, args=(server.stdout, fixture_queue, fixture_output)
                    )
                    worker.start()
                    workers.append(worker)
                    endpoint = json.loads(line_from(fixture_queue))
                    result["server"] = {**endpoint, "pid": server.pid}
                    headers = evidence / "curl.headers.txt"
                    curl_command = [
                        str(curl),
                        "--silent",
                        "--show-error",
                        "--no-buffer",
                        "--noproxy",
                        "*",
                        "--connect-timeout",
                        "2",
                        "--max-time",
                        "12",
                        "--dump-header",
                        str(headers),
                        endpoint["url"] + "/api/avatar/stream",
                    ]
                    result["curl_command"] = curl_command
                    client = subprocess.Popen(
                        curl_command,
                        stdout=subprocess.PIPE,
                        stderr=curl_errors,
                        text=True,
                        bufsize=1,
                    )
                    assert client.stdout is not None
                    worker = threading.Thread(
                        target=_lines, args=(client.stdout, curl_queue, raw_stream)
                    )
                    worker.start()
                    workers.append(worker)
                    baseline = [frame() for _ in range(6)]
                    before = baseline[-1]
                    arrivals = [row["received_monotonic"] for row in frames]
                    self.assertGreaterEqual(arrivals[-1] - arrivals[0], 0.9)
                    result["baseline_arrival_intervals_seconds"] = [
                        b - a for a, b in zip(arrivals, arrivals[1:], strict=False)
                    ]
                    self.assertEqual(before["channels"]["mouth"]["open"], 0)
                    self.assertEqual(before["missing_sources"], ["voice_level"])
                    sent = time.monotonic()
                    self.assertEqual(command("priority"), {"ack": "priority"})
                    speaking = until(
                        lambda item: item["action"] == "priority" and item["activity"] == "speaking"
                    )
                    result["priority_latency_seconds"] = time.monotonic() - sent
                    self.assertLess(result["priority_latency_seconds"], 1)
                    for channel in ("expression", "posture", "gaze"):
                        self.assertNotEqual(
                            before["channels"][channel], speaking["channels"][channel]
                        )
                    self.assertEqual(speaking["channels"]["mouth"], {"open": 0.85, "level": 0.85})
                    self.assertFalse(speaking["degraded"])
                    expired = until(lambda item: "voice_level" in item["missing_sources"])
                    self.assertEqual(expired["channels"]["mouth"]["open"], 0)
                    self.assertEqual(expired["activity"], "thinking")
                    self.assertEqual(command("priority"), {"ack": "priority"})
                    until(lambda item: item["channels"]["mouth"]["open"] == 0.85)
                    self.assertEqual(command("silence"), {"ack": "silence"})
                    silent = until(lambda item: item["channels"]["mouth"]["open"] == 0)
                    self.assertNotIn("voice_level", silent["missing_sources"])
                    self.assertEqual(
                        set(speaking["channels"]),
                        {"expression", "posture", "gaze", "idle", "mouth"},
                    )
                    sequences = [row["frame"]["sequence"] for row in frames]
                    self.assertEqual(
                        sequences, list(range(sequences[0], sequences[0] + len(sequences)))
                    )
                    self.assertNotIn("胸痛", "".join(raw_stream))
                    header_text = headers.read_text()
                    self.assertEqual(header_text.splitlines()[0].split()[1], "200")
                    self.assertIn("Content-Type: text/event-stream", header_text)
                    self.assertIn("Cache-Control: no-store", header_text)
                    result.update(before=before, speaking=speaking, expired=expired, silent=silent)
                    assert server.stdin is not None
                    server.stdin.write('{"command":"stop"}\n')
                    server.stdin.flush()
                    stopped = json.loads(line_from(fixture_queue))
                    self.assertEqual(stopped, {"clean": True, "connections": 0})
                    self.assertEqual(server.wait(timeout=8), 0)
                    self.assertEqual(client.wait(timeout=5), 0)
                    with socket.socket() as probe:
                        probe.settimeout(0.5)
                        result["server_port_closed"] = (
                            probe.connect_ex(("127.0.0.1", endpoint["port"])) != 0
                        )
                        self.assertTrue(result["server_port_closed"])
                    result.update(ok=True, frame_count=len(frames), cleanup=stopped)
                finally:
                    for process in (client, server):
                        if process is not None and process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=5)
                    for worker in workers:
                        worker.join(3)
                    result["processes_exited"] = all(
                        process is None or process.poll() is not None
                        for process in (client, server)
                    )
                    result["reader_threads_exited"] = all(
                        not worker.is_alive() for worker in workers
                    )
                    result["process_returncodes"] = {
                        "server": server.returncode if server is not None else None,
                        "curl": client.returncode if client is not None else None,
                    }
                    result["fixture_root"] = str(fixture_root)
                    (evidence / "curl.sse.txt").write_text("".join(raw_stream))
                    (evidence / "server.stdout.jsonl").write_text("".join(fixture_output))
                    (evidence / "received_frames.json").write_text(json.dumps(frames, indent=2))
                    (evidence / "results.json").write_text(json.dumps(result, indent=2))
                    for process in (client, server):
                        if process is not None:
                            for stream in (process.stdin, process.stdout):
                                if stream is not None:
                                    stream.close()
            self.assertTrue(result["processes_exited"])
            self.assertTrue(result["reader_threads_exited"])
            self.assertEqual((evidence / "server.stderr.log").read_text(), "")
            self.assertEqual((evidence / "curl.stderr.log").read_text(), "")
            self.assertFalse((fixture_root / "data" / "daemon.db").exists())
        self.assertFalse(fixture_root.exists(), "disposable product state was not removed")


if __name__ == "__main__":
    unittest.main()
