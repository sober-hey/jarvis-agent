#!/usr/bin/env python3
"""Acceptance against actual wscat CLI on ws://localhost:8765."""
import json
import os
from pathlib import Path
import pty
import re
import select
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import test as compiler

compiler.build()
binary = compiler.executable('loopback-client', [], (ROOT / 'Tests/Integration/LoopbackClient.swift').read_text())
master, slave = pty.openpty()
server = subprocess.Popen(['/usr/local/bin/npx', '--yes', '--package=wscat', 'wscat', '--no-color', '--listen', '8765'],
                          stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
os.close(slave)
client = None
transcript = b''

def await_output(needle, timeout=30):
    global transcript
    deadline = time.monotonic() + timeout
    while needle not in transcript and time.monotonic() < deadline:
        if select.select([master], [], [], 0.1)[0]:
            transcript += os.read(master, 65_536)
    assert needle in transcript, transcript.decode(errors='replace')

try:
    await_output(b'Listening on port 8765')
    client = subprocess.Popen([str(binary), 'exchange'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    await_output(b'audio_end')
    clean = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', transcript.decode(errors='replace'))
    assert 'AP//fw==' not in clean  # Sentinel: test bytes are 00 00 ff 7f, not this wrong encoding.
    assert 'AAD/fw==' in clean.replace('\\/', '/'), clean
    assert 'pcm_s16le' in clean and '16000' in clean, clean
    replies = [
        {'type': 'text', 'role': 'assistant', 'text': 'transport fixture reply', 'final': True, 'generation': 1},
        {'type': 'audio', 'data': 'AQID', 'format': 'mp3', 'final': False, 'generation': 1},
        {'type': 'audio', 'data': '', 'format': 'mp3', 'final': True, 'generation': 1},
    ]
    for message in replies:
        os.write(master, (json.dumps(message) + '\n').encode())
        time.sleep(0.1)
    output, _ = client.communicate(timeout=15)
    assert client.returncode == 0, output
    print(clean)
    print(output)
    compiler.BUILD.joinpath('wscat-transcript.txt').write_text(clean + '\n' + output)
    compiler.BUILD.joinpath('wscat-results.json').write_text(json.dumps({'test': 'actual_wscat_loopback', 'passed': True, 'pcm_json_observed': True, 'text_audio_received': True}, indent=2))
    print('PASS actual wscat: JSON PCM observed, text + Base64 audio received')
finally:
    if client and client.poll() is None: client.kill(); client.wait()
    import signal
    with __import__('contextlib').suppress(ProcessLookupError): os.killpg(server.pid, signal.SIGTERM)
    server.wait(timeout=5)
    os.close(master)
