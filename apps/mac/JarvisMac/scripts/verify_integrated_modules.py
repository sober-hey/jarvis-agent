#!/usr/bin/env python3
"""Fresh evidence for copied-module targets; never launch/request mic/AX permission."""
from pathlib import Path
import datetime
import hashlib
import json
import os
import subprocess
import tempfile
from typing import Any, Sequence, Union

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'Evidence' / (datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-project')
EVIDENCE.mkdir(parents=True, exist_ok=False)
ENV = dict(os.environ, TMPDIR='/tmp', DEVELOPER_DIR='/Applications/Xcode.app/Contents/Developer')
DERIVED = Path(tempfile.mkdtemp(prefix='jarvis-mac-project-', dir='/tmp'))
RESULTS: dict[str, Any] = {'evidence': str(EVIDENCE), 'derivedData': str(DERIVED), 'commands': [],
           'realMicrophone': 'NOT_RUN', 'permissionPrompt': 'EXPLICIT_ORIGINAL_XCTEST_API', 'audibleOutput': 'NOT_VERIFIED_VOLUME_ZERO'}

def run(name: str, command: Sequence[Union[str, Path]]) -> str:
    proc = subprocess.run([str(p) for p in command], cwd=ROOT, env=ENV, capture_output=True, text=True)
    (EVIDENCE / f'{name}.stdout.log').write_text(proc.stdout)
    (EVIDENCE / f'{name}.stderr.log').write_text(proc.stderr)
    RESULTS['commands'].append({'name': name, 'command': [str(p) for p in command], 'exitCode': proc.returncode})
    (EVIDENCE / 'execution.json').write_text(json.dumps(RESULTS, indent=2) + '\n')
    print(f'{name}: exit {proc.returncode}', flush=True)
    if proc.returncode:
        print('\n'.join((proc.stdout + '\n' + proc.stderr).splitlines()[-45:]), flush=True)
        raise SystemExit(proc.returncode)
    return proc.stdout

hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
          for base in ['Vendor', 'Tests/JarvisCoreTests'] for p in (ROOT/base).rglob('*')
          if p.is_file() and p.suffix in ['.swift', '.wav', '.mp3', '.json']}
(EVIDENCE / 'sources.json').write_text(json.dumps(hashes, indent=2) + '\n')
run('plist-lint', ['plutil', '-lint', 'JarvisMacApp.xcodeproj/project.pbxproj', 'App/Info.plist', 'App/JarvisMacApp.entitlements'])
out = run('xcode-module-tests', ['xcodebuild', '-project', 'JarvisMacApp.xcodeproj', '-scheme', 'AllModuleTests',
    '-configuration', 'Debug', '-destination', 'platform=macOS,arch=arm64',
    '-clonedSourcePackagesDirPath', '/tmp/jarvis-mac-final-derived/SourcePackages',
    '-skipPackageUpdates', '-disableAutomaticPackageResolution',
    '-derivedDataPath', DERIVED, '-resultBundlePath', EVIDENCE/'ModuleTests.xcresult',
    'CODE_SIGNING_ALLOWED=NO', 'test'])
RESULTS['xctestSummary'] = json.loads(run('xctest-summary', [
    'xcrun', 'xcresulttool', 'get', 'test-results', 'summary', '--path',
    EVIDENCE/'ModuleTests.xcresult', '--format', 'json']))
products = DERIVED/'Build/Products/Debug'
out = run('audio-recorder', [products/'AudioRecorderTests'])
RESULTS['audioRecorder'] = {'passed': sum(l.startswith('PASS ') for l in out.splitlines()),
                           'failed': sum(l.startswith('FAIL ') for l in out.splitlines())}
run('activation-regression', [products/'ActivationRegression', '--report', EVIDENCE/'activation-regression.json'])
wav = ROOT/'Vendor/Activation/Evidence/WAV'
run('activation-gesture-replay', [products/'ActivationReplay', '--manifest', wav/'manifest.json', '--report', EVIDENCE/'activation-gesture-replay.json'])
run('activation-fallback-replay', [products/'ActivationReplay', '--manifest', wav/'fallback-manifest.json', '--fallback', '--report', EVIDENCE/'activation-fallback-replay.json'])
# WAV is a fresh local encoding of the existing MP3 fixture; no fetched/synthetic response data.
mp3 = ROOT/'Vendor/AudioAccessibility/Tests/Fixtures/fixture.mp3'
run('decode-test-wav', ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', mp3, '-codec:a', 'pcm_s16le', EVIDENCE/'fixture.wav'])
for name, args in [('audio-player', [products/'AudioPlayerChecks', mp3, EVIDENCE/'fixture.wav']),
                   ('accessibility', [products/'AccessibilityChecks'])]:
    text = run(name, args)
    result = [l for l in text.splitlines() if l.startswith('RESULT ')]
    assert len(result) == 1, f'missing test result: {name}'
    RESULTS[name] = json.loads(result[0][7:])
    assert RESULTS[name]['failed'] == 0
RESULTS['activationRegression'] = json.loads((EVIDENCE/'activation-regression.json').read_text())
RESULTS['activationGesture'] = json.loads((EVIDENCE/'activation-gesture-replay.json').read_text())
RESULTS['activationFallback'] = json.loads((EVIDENCE/'activation-fallback-replay.json').read_text())
(EVIDENCE/'execution.json').write_text(json.dumps(RESULTS, indent=2) + '\n')
print(json.dumps({k:v for k,v in RESULTS.items() if k not in ['commands', 'activationRegression', 'activationGesture', 'activationFallback']}, indent=2))
print(f'EVIDENCE={EVIDENCE}')
