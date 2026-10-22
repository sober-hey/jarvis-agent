#!/usr/bin/env python3
"""Build signed local deliverable and retain command/signature evidence."""
import datetime
import json
import os
from pathlib import Path
import subprocess
ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT/'Evidence'/(datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-signed-build')
EVIDENCE.mkdir(parents=True, exist_ok=False)
ENV = dict(os.environ, TMPDIR='/tmp', DEVELOPER_DIR='/Applications/Xcode.app/Contents/Developer')
commands = []
def run(name, args):
    p = subprocess.run([str(x) for x in args],cwd=ROOT,env=ENV,capture_output=True,text=True)
    commands.append({'name':name,'command':[str(x) for x in args],'exit_code':p.returncode})
    (EVIDENCE/(name+'.stdout.log')).write_text(p.stdout)
    (EVIDENCE/(name+'.stderr.log')).write_text(p.stderr)
    (EVIDENCE/'execution.json').write_text(json.dumps(commands,indent=2))
    if p.returncode:
        print(p.stdout[-5000:]+p.stderr[-3000:]); raise SystemExit(p.returncode)
    print(name,'PASS',flush=True)
run('xcodebuild',['xcodebuild','-project','JarvisMacApp.xcodeproj','-scheme','JarvisMacApp',
    '-configuration','Debug','-derivedDataPath','/tmp/jarvis-mac-final-derived',
    'DEVELOPMENT_TEAM=956UD85933','CODE_SIGN_STYLE=Automatic','-allowProvisioningUpdates','build'])
source=Path('/tmp/jarvis-mac-final-derived/Build/Products/Debug/JarvisMacApp.app')
output=ROOT/'Output/JARVIS.app'
run('copy-deliverable',['ditto',source,output])
run('signature-verify',['codesign','--verify','--deep','--strict',output])
run('signature-details',['codesign','-dvv',output])
run('entitlements',['codesign','-d','--entitlements','-',output])
print('APP='+str(output)); print('EVIDENCE='+str(EVIDENCE))
