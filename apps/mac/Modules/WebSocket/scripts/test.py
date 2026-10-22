#!/usr/bin/env python3
"""CLT-only verification: uses the real swift-frontend + clang, not a mock.
Xcode users can run `swift test` instead. Artifacts stay in Hermes scratch.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(os.environ.get('JARVIS_TEST_BUILD', '/Volumes/扩展硬盘/hermes/.hermes/cache/scratch/jarvis-websocket-gateway-build'))
DEPS = BUILD.parent / 'jarvis-websocket-deps' / 'Starscream'
TOOLS = Path('/Library/Developer/CommandLineTools/usr/bin')
SDK = '/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk'
BASE = [str(TOOLS / 'swift-frontend'), '-swift-version', '5', '-sdk', SDK, '-target', 'arm64-apple-macosx12.0']

def run(args):
    subprocess.run([str(a) for a in args], cwd=ROOT, check=True)

def build():
    BUILD.mkdir(parents=True, exist_ok=True)
    if not DEPS.exists():
        DEPS.parent.mkdir(parents=True, exist_ok=True)
        run(['git', 'clone', '--depth', '1', '--branch', '4.0.8', 'https://github.com/daltoniam/Starscream.git', DEPS])
    revision = subprocess.check_output(['git', '-C', str(DEPS), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != 'c6bfd1af48efcc9a9ad203665db12375ba6b145a':
        raise RuntimeError('Unexpected Starscream revision: ' + revision)
    if not (BUILD / 'Starscream.o').exists():
        run(BASE + ['-emit-module', '-emit-object', '-whole-module-optimization', '-parse-as-library',
                    '-module-name', 'Starscream', *DEPS.joinpath('Sources').rglob('*.swift'),
                    '-emit-module-path', BUILD / 'Starscream.swiftmodule', '-o', BUILD / 'Starscream.o'])
    run(BASE + ['-emit-module', '-emit-object', '-enable-testing', '-warnings-as-errors',
                '-whole-module-optimization', '-parse-as-library', '-module-name', 'JarvisWebSocket',
                '-I', BUILD, *ROOT.joinpath('Sources').rglob('*.swift'),
                '-emit-module-path', BUILD / 'JarvisWebSocket.swiftmodule', '-o', BUILD / 'JarvisWebSocket.o'])

def executable(name, sources, main):
    main_path = BUILD / 'main.swift'
    main_path.write_text(main)
    run(BASE + ['-emit-object', '-whole-module-optimization', '-module-name', 'BehaviorRunner',
                '-I', BUILD, *sources, main_path, '-o', BUILD / (name + '.o')])
    run([TOOLS / 'clang', BUILD / (name + '.o'), BUILD / 'JarvisWebSocket.o', BUILD / 'Starscream.o',
         '-L/Library/Developer/CommandLineTools/usr/lib/swift/macosx', '-L/usr/lib/swift',
         '-Wl,-rpath,/usr/lib/swift', '-isysroot', SDK, '-target', 'arm64-apple-macosx12.0', '-lz', '-o', BUILD / name])
    return BUILD / name

if __name__ == '__main__':
    build()
    tests = ROOT / 'Tests/JarvisWebSocketTests/BehaviorTests.swift'
    names = re.findall(r'^func (test\w+)\(\) throws', tests.read_text(), re.M)
    if len(sys.argv) > 1:
        names = [name for name in names if name == sys.argv[1]]
    assert names, 'No tests selected'
    main = '\n'.join('try ' + n + '(); print("PASS ' + n + '")' for n in names)
    binary = executable('behavior-tests', [tests], main)
    run([binary])
    print(f'PASS {len(names)} behavioral tests using real Combine/Starscream modules')
