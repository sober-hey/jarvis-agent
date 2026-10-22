#!/usr/bin/env python3
"""Validate the generated formal project without writing or compiling composition-root sources."""
from pathlib import Path
import json
import plistlib
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
project = ROOT/'JarvisMacApp.xcodeproj'
proc = subprocess.run(['plutil', '-convert', 'json', '-o', '-', str(project/'project.pbxproj')],
                      check=True, capture_output=True, text=True)
pbx = json.loads(proc.stdout)
objects = pbx['objects']
targets = {v['name']: v for v in objects.values() if v.get('isa') == 'PBXNativeTarget'}
expected_counts = {'AudioRecorderModule': 4, 'JarvisActivation': 6, 'JarvisWebSocket': 2,
                   'AudioAccessibilityModule': 3}

def source_paths(name: str) -> list[str]:
    target = targets[name]
    phases = [objects[k] for k in target['buildPhases'] if objects[k]['isa'] == 'PBXSourcesBuildPhase']
    return sorted(objects[objects[k]['fileRef']]['path'] for phase in phases for k in phase['files'])

for name, count in expected_counts.items():
    sources = source_paths(name)
    assert len(sources) == count, (name, sources)
    assert all((ROOT/p).is_file() for p in sources), (name, sources)
# All existing parent-written integration files must be discovered; empty before wiring is intentional.
existing = sorted(str(p.relative_to(ROOT)) for p in (ROOT/'Sources/JarvisIntegration').glob('*.swift'))
assert source_paths('JarvisIntegration') == existing, 'Rerun generator after composition-root edits'
app = targets['JarvisMacApp']
configs = [objects[k]['buildSettings'] for k in objects[app['buildConfigurationList']]['buildConfigurations']]
for settings in configs:
    assert settings['DEVELOPMENT_TEAM'] == '956UD85933'
    assert settings['CODE_SIGN_STYLE'] == 'Automatic'
    assert settings['CODE_SIGN_ENTITLEMENTS'] == 'App/JarvisMacApp.entitlements'
    assert settings['INFOPLIST_FILE'] == 'App/Info.plist'
linked = {objects[objects[k]['target']]['name'] for k in app['dependencies']}
assert linked == set(expected_counts) | {'JarvisCore', 'JarvisUI', 'JarvisIntegration'}, linked
integration_links = {objects[objects[k]['target']]['name'] for k in targets['JarvisIntegration']['dependencies']}
assert integration_links == set(expected_counts) | {'JarvisCore', 'JarvisUI'}, integration_links
remote = [o for o in objects.values() if o.get('isa') == 'XCRemoteSwiftPackageReference']
assert len(remote) == 1 and remote[0]['requirement'] == {'kind': 'exactVersion', 'version': '4.0.8'}
assert remote[0]['repositoryURL'] == 'https://github.com/daltoniam/Starscream.git'
info = plistlib.loads((ROOT/'App/Info.plist').read_bytes())
entitlements = plistlib.loads((ROOT/'App/JarvisMacApp.entitlements').read_bytes())
assert info['NSMicrophoneUsageDescription']
for entitlement in ['com.apple.security.device.audio-input', 'com.apple.security.network.client']:
    assert entitlements[entitlement] is True
schemes = project/'xcshareddata/xcschemes'
for name in ['JarvisMacApp', 'ModuleTests', 'AudioAccessibilityTests']:
    tree = ET.parse(schemes/f'{name}.xcscheme')
    assert any(e.get('Identifier') == 'ModuleXcodeTests/testExplicitSystemPermissionPrompt()'
               for e in tree.findall('.//SkippedTests/Test')), name
assert len(list((ROOT/'Vendor/Activation/Evidence/WAV').glob('*.wav'))) == 109
assert (ROOT/'Vendor/AudioAccessibility/Tests/Fixtures/fixture.mp3').is_file()
print(json.dumps({'result': 'PASS', 'moduleSourceCounts': expected_counts,
                  'integrationSources': existing, 'appLinks': sorted(linked),
                  'targetNames': sorted(targets), 'signingTeam': '956UD85933',
                  'signingStyle': 'Automatic', 'starscream': '4.0.8',
                  'activationWAVFiles': 109, 'explicitPermissionPrompt': 'excluded'}, indent=2))
