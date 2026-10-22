#!/usr/bin/env python3
"""Real RFC6455 loopback, real gateway path, 30s outage, default 30s ping.
Speech/brain boundary uses explicit test fixtures: no cloud or timeline writes.
"""
import asyncio
import base64
import contextlib
import importlib.util
import json
import hashlib
import subprocess
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import test as compiler
from websockets.asyncio.server import serve
from websockets.server import ServerProtocol
from websockets.exceptions import ConnectionClosed

results = []
expected_pcm = bytes([0, 0, 255, 127])

async def next_line(process, predicate):
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), 60)
        if not line:
            raise RuntimeError('Swift process ended prematurely')
        text = line.decode().rstrip()
        print(text, flush=True)
        if predicate(text):
            return

async def drain(process):
    output, _ = await asyncio.wait_for(process.communicate(), 60)
    print(output.decode(), end='', flush=True)
    assert process.returncode == 0, f'Swift exited {process.returncode}'

async def spawn(binary, mode):
    return await asyncio.create_subprocess_exec(str(binary), mode, stdout=asyncio.subprocess.PIPE,
                                               stderr=asyncio.subprocess.STDOUT)

async def reply(ws, generation):
    await ws.send(json.dumps({'type': 'text', 'role': 'assistant', 'text': 'transport fixture reply', 'final': True, 'generation': generation}))
    await ws.send(json.dumps({'type': 'audio', 'data': 'AQID', 'format': 'mp3', 'final': False, 'generation': generation}))
    await ws.send(json.dumps({'type': 'audio', 'data': '', 'format': 'mp3', 'final': True, 'generation': generation}))

async def outage(binary):
    received = []
    timestamps = []
    async def peer(ws):
        timestamps.append(time.monotonic())
        with contextlib.suppress(ConnectionClosed):
            async for raw in ws:
                obj = json.loads(raw)
                received.append(obj)
                if obj['type'] == 'audio':
                    assert base64.b64decode(obj['data'], validate=True) == expected_pcm
                    assert obj['format'] == 'pcm_s16le' and obj['sample_rate'] == 16000
                if obj['type'] == 'audio_end':
                    await reply(ws, 1)
    server = await serve(peer, '127.0.0.1', 8765, ping_interval=None)
    process = await spawn(binary, 'outage')
    try:
        await next_line(process, lambda line: line == 'RESPONSE 1')
        server.close()
        await server.wait_closed()
        start = time.monotonic()
        await asyncio.sleep(30)
        async with serve(peer, '127.0.0.1', 8765, ping_interval=None):
            await drain(process)
        elapsed = timestamps[-1] - start
        assert elapsed >= 30, elapsed
        assert sum(message['type'] == 'audio' for message in received) == 2, received
        results.append({'test': '30_second_listener_outage', 'result': 'passed', 'reconnected_after_seconds': round(elapsed, 3), 'uploads_received': 2})
    finally:
        server.close(); await server.wait_closed()
        if process.returncode is None: process.kill(); await process.wait()

async def heartbeat(binary):
    pings = []
    original = ServerProtocol.recv_frame
    def instrumented(self, frame):
        if int(frame.opcode) == 9: pings.append(time.monotonic())
        return original(self, frame)
    ServerProtocol.recv_frame = instrumented
    async def peer(ws):
        with contextlib.suppress(ConnectionClosed):
            async for raw in ws: raise AssertionError('unexpected application message ' + str(raw))
    start = time.monotonic()
    try:
        async with serve(peer, '127.0.0.1', 8765, ping_interval=None):
            process = await spawn(binary, 'heartbeat')
            await drain(process)
        assert pings and pings[0] - start >= 29, pings
        results.append({'test': 'default_idle_30_second_ping', 'result': 'passed', 'first_ping_seconds': round(pings[0] - start, 3)})
    finally: ServerProtocol.recv_frame = original

async def gateway(binary):
    packaging = Path('/Volumes/扩展硬盘/jarvis/packaging')
    sys.path.insert(0, str(packaging))
    import voice_gateway
    transcribed = []
    brain_inputs = []
    class FixtureSpeech:
        async def transcribe(self, pcm):
            assert pcm == expected_pcm
            transcribed.append(pcm)
            return 'fixture question'
        async def synthesize(self, text):
            assert text == 'transport fixture reply'
            yield bytes([1, 2, 3])
    async def fixture_brain(text):
        brain_inputs.append(text)
        return 'transport fixture reply'
    gateway = voice_gateway.VoiceGateway(FixtureSpeech(), fixture_brain)
    async with gateway.serve():
        process = await spawn(binary, 'exchange')
        await drain(process)
    await gateway.close()
    assert len(transcribed) == 1 and brain_inputs == ['fixture question']
    results.append({'test': 'actual_voice_gateway_protocol', 'result': 'passed', 'brain_boundary': 'fixture_only_no_timeline_writes'})

async def main(binary):
    await gateway(binary)
    await outage(binary)
    await heartbeat(binary)
    report = {'tests': results, 'count': len(results), 'source_sha256': {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in ROOT.joinpath('Sources').rglob('*.swift')
    }}
    compiler.BUILD.joinpath('integration-results.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    compiler.build()
    source = ROOT / 'Tests/Integration/LoopbackClient.swift'
    binary = compiler.executable('loopback-client', [], source.read_text())
    asyncio.run(main(binary))
