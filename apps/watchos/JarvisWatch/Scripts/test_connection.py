#!/usr/bin/env python3
"""Real Swift URLSession loopback, gateway protocol fixture only (no Brain calls)."""

import asyncio
import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    seen: list[str] = []
    turns: list[bytes] = []
    failures: list[str] = []
    done = asyncio.Event()
    env = dict(
        os.environ, TMPDIR="/tmp", DEVELOPER_DIR="/Applications/Xcode.app/Contents/Developer"
    )

    with tempfile.TemporaryDirectory(prefix="jarvis-watch-gateway-", dir="/tmp") as temp:
        executable = Path(temp) / "connection-test"
        mp3_path = Path(temp) / "fixture.mp3"
        # Real valid MP3, synthetic sine tone. Fixture bytes aren't invented provider output.
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=22050:duration=0.12",
                "-ac",
                "1",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "32k",
                str(mp3_path),
            ],
            check=True,
            env=env,
        )
        mp3 = mp3_path.read_bytes()
        command = [
            "xcrun",
            "swiftc",
            "-parse-as-library",
            "-swift-version",
            "5",
            "-strict-concurrency=targeted",
            "-warnings-as-errors",
            "Core/WatchCore.swift",
            "App/WatchConnection.swift",
            "Tests/WatchCoreSuite.swift",
            "Tests/ConnectionHarness.swift",
            "-o",
            str(executable),
        ]
        print("COMPILE", json.dumps(command), flush=True)
        subprocess.run(command, cwd=ROOT, check=True, env=env)

        async def gateway(socket: ServerConnection) -> None:
            pcm = bytearray()
            try:
                assert socket.request is not None
                assert (
                    socket.request.headers.get("Authorization")
                    == "Bearer watch-loopback-fixture-token"
                )
                async for raw in socket:
                    assert isinstance(raw, str), "gateway accepts JSON text only"
                    item: dict[str, Any] = json.loads(raw)
                    kind = item["type"]
                    seen.append(kind)
                    if kind in ("hello", "device_state"):
                        assert item["device_id"] == "watch-fixture"
                        assert item["device_type"] == "watch"
                        state = item["state"]
                        assert state["battery_level"] == 0.6 and state["clap_triggered"] is True
                        assert state["on_wrist"] is None and state["wearing"] is None
                        if kind == "hello":
                            assert len(seen) == 1
                            assert item["sample_rate"] == 16000 and item["channels"] == 1
                            assert item["encoding"] == "pcm_s16le"
                        elif len(turns) == 1 and seen.count("device_state") == 2:
                            await socket.send(json.dumps(dict(type="selected")))
                    elif kind == "audio":
                        assert item["sample_rate"] == 16000 and item["format"] == "pcm_s16le"
                        data = base64.b64decode(item["data"], validate=True)
                        assert len(data) == 640 and not data.startswith(b"RIFF")
                        pcm.extend(data)
                    elif kind == "audio_end":
                        assert bytes(pcm) == bytes(i % 251 for i in range(1280))
                        turns.append(bytes(pcm))
                        pcm.clear()
                        generation = len(turns)
                        # Initial server code sends no generation on interrupt; iOS doc
                        # permits one. Verify both cases using the real Watch transport.
                        await socket.send(
                            json.dumps(
                                dict(type="interrupt")
                                if generation == 1
                                else dict(type="interrupt", generation=generation)
                            )
                        )
                        for role, text in (
                            ("user", "fixture heard"),
                            ("assistant", "fixture reply"),
                        ):
                            await socket.send(
                                json.dumps(
                                    dict(type="text", role=role, text=text, generation=generation)
                                )
                            )
                        for start in range(0, len(mp3), 123):
                            await socket.send(
                                json.dumps(
                                    dict(
                                        type="audio",
                                        format="mp3",
                                        generation=generation,
                                        final=False,
                                        data=base64.b64encode(mp3[start : start + 123]).decode(),
                                    )
                                )
                            )
                        await socket.send(
                            json.dumps(
                                dict(
                                    type="audio",
                                    format="mp3",
                                    generation=generation,
                                    final=True,
                                    data="",
                                )
                            )
                        )
                    elif kind == "playback_done":
                        assert item["generation"] == len(turns)
                        if len(turns) == 1:
                            await socket.send(json.dumps(dict(type="suppressed")))
                            # In-flight old response must not be delivered to the app.
                            await socket.send(
                                json.dumps(
                                    dict(
                                        type="audio",
                                        format="mp3",
                                        generation=1,
                                        final=True,
                                        data=base64.b64encode(mp3).decode(),
                                    )
                                )
                            )
                        else:
                            done.set()
                    else:
                        raise AssertionError(f"unexpected custom protocol frame {kind}")
            except Exception as exc:
                failures.append(repr(exc))
                done.set()
                raise

        async with serve(gateway, "127.0.0.1", 0, max_size=2 * 1024 * 1024) as server:
            port = server.sockets[0].getsockname()[1]
            process = await asyncio.create_subprocess_exec(
                str(executable),
                f"ws://127.0.0.1:{port}",
                str(mp3_path),
                env=env,
            )
            try:
                result = await asyncio.wait_for(process.wait(), timeout=25)
                await asyncio.wait_for(done.wait(), timeout=3)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            assert result == 0, f"Swift integration exited {result}"
            assert not failures, failures
            assert seen == [
                "hello",
                "device_state",
                "audio",
                "audio",
                "audio_end",
                "playback_done",
                "device_state",
                "audio",
                "audio",
                "audio_end",
                "playback_done",
            ], seen
            print(
                "PASS",
                json.dumps(
                    dict(
                        received_types=seen,
                        pcm_utterances=len(turns),
                        pcm_bytes=[len(x) for x in turns],
                        mp3_fixture_bytes=len(mp3),
                        authorization_header_verified=True,
                        replay_count=0,
                    )
                ),
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(main())
