#!/usr/bin/env python3
"""Compile/run standalone checks with CommandLineTools, no Xcode or SPM required."""
from pathlib import Path
from typing import Sequence, Union
import json
import hashlib
import shutil
import subprocess
import sys
import os
import tempfile

ROOT = Path(__file__).resolve().parent
SCRATCH = Path(tempfile.mkdtemp(prefix='audio-accessibility-', dir='/Volumes/扩展硬盘/hermes/.hermes/cache/scratch'))
SCRATCH.mkdir(parents=True, exist_ok=True)
(SCRATCH / 'module-cache').mkdir(exist_ok=True)
XCODE = Path('/Applications/Xcode.app/Contents/Developer')
TOOLS = (XCODE / 'Toolchains/XcodeDefault.xctoolchain/usr/bin'
         if XCODE.is_dir() else Path('/Library/Developer/CommandLineTools/usr/bin'))
SDK = str(XCODE / 'Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk'
          if XCODE.is_dir() else TOOLS.parents[1] / 'SDKs/MacOSX.sdk')
if not Path(SDK).is_dir():
    raise SystemExit('macOS SDK missing; install Xcode or CLT first.')
ENV = dict(os.environ)
ENV['TMPDIR'] = '/Users/sober/.cache/jarvis-swift-tmp'
if not Path(ENV['TMPDIR']).is_dir():
    raise SystemExit('Create the ASCII scratch symlink documented in README first.')
if XCODE.is_dir():
    ENV['DEVELOPER_DIR'] = str(XCODE)
source_hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in [*ROOT.glob('*.swift'), *ROOT.glob('Tests/*.swift'), Path(__file__)]}
(SCRATCH / 'sources.json').write_text(json.dumps(source_hashes, indent=2) + '\n')


def run(command: Sequence[Union[str, Path]]) -> str:
    result = subprocess.run([str(part) for part in command], capture_output=True, text=True, env=ENV)
    with (SCRATCH / 'commands.log').open('a') as log:
        log.write(json.dumps([str(part) for part in command]) + '\n')
        log.write(result.stdout + result.stderr + f'\nEXIT={result.returncode}\n')
    print(result.stdout, end='')
    print(result.stderr, end='', file=sys.stderr)
    if result.returncode:
        raise RuntimeError(f'command failed ({result.returncode}): {command}')
    return result.stdout


# Use ASCII TMPDIR before probing: non-ASCII driver paths can cause SIGTRAP.
probe = subprocess.run([str(TOOLS / 'swiftc'), '--version'], capture_output=True, text=True, env=ENV)
frontend = probe.returncode != 0
print(f'COMPILER={"swift-frontend+clang" if frontend else "swiftc"}; SWIFTC_PROBE_EXIT={probe.returncode}')


def compile_check(name: str, sources: Sequence[Path]) -> Path:
    binary = SCRATCH / name
    common = ['-parse-as-library', '-swift-version', '6', '-warnings-as-errors',
              '-sdk', SDK, '-module-cache-path', SCRATCH / 'module-cache', '-module-name', name]
    frameworks = ['Foundation', 'AppKit', 'AVFoundation', 'Combine', 'ApplicationServices', 'Carbon']
    link = [part for framework in frameworks for part in ('-framework', framework)]
    if frontend:
        obj = SCRATCH / f'{name}.o'
        run([TOOLS / 'swift-frontend', '-c', '-whole-module-optimization', *common, *sources, '-o', obj])
        run([TOOLS / 'clang', '-isysroot', SDK, obj, '-L', TOOLS.parent / 'lib/swift/macosx',
             '-Wl,-rpath,/usr/lib/swift', *link, '-o', binary])
    else:
        run([TOOLS / 'swiftc', *common, *sources, *link, '-o', binary])
    return binary


ffmpeg = shutil.which('ffmpeg')
if not ffmpeg:
    raise SystemExit('ffmpeg is required only to generate local test MP3/WAV fixtures; no network download is used.')
for extension, codec in [('mp3', 'libmp3lame'), ('wav', 'pcm_s16le')]:
    run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
         'sine=frequency=440:duration=1.0', '-codec:a', codec, '-y', SCRATCH / f'fixture.{extension}'])

sources = [ROOT / name for name in ('AudioPlayer.swift', 'AccessibilityHelper.swift', 'PlaybackMenuPresenter.swift')]
audio = compile_check('AudioChecks', [*sources, ROOT / 'Tests/AudioPlayerChecks.swift'])
ax = compile_check('AccessibilityChecks', [ROOT / 'AccessibilityHelper.swift', ROOT / 'Tests/AccessibilityChecks.swift'])
results = {}
for name, command in [('audio', [audio, SCRATCH / 'fixture.mp3', SCRATCH / 'fixture.wav']),
                      ('accessibility', [ax])]:
    output = run(command)
    (SCRATCH / f'{name}-output.txt').write_text(output)
    result_lines = [line for line in output.splitlines() if line.startswith('RESULT ')]
    if len(result_lines) != 1:
        raise RuntimeError(f'Missing unique RESULT in {name}')
    results[name] = json.loads(result_lines[0][7:])
    if results[name]['failed'] != 0:
        raise RuntimeError(f'{name} tests failed')
results['compiler'] = 'swift-frontend+clang' if frontend else 'swiftc'
results['live_accessibility'] = 'NOT_RUN_REQUIRES_MANUAL_PERMISSION'
results['audible_output'] = 'NOT_VERIFIED_TEST_VOLUME_ZERO'
(SCRATCH / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
print('SUMMARY ' + json.dumps(results))
print(f'EVIDENCE={SCRATCH}')
