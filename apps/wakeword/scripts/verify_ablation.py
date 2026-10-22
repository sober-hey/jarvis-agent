"""Ablate learned classifier; unchanged frontend/audio must cease word detections."""
import json,os,subprocess,tempfile
from pathlib import Path
import torch
from convert import Head,export,ROOT

def main():
    model=Head()
    with torch.no_grad():
        for p in model.parameters():p.zero_()
    export('AblatedClassifier',model,torch.zeros(1,16,96),'features','confidence')
    case=Path(tempfile.mkdtemp(prefix='ablation-',dir=ROOT/'evidence'))
    (case/'models').mkdir();(case/'data').symlink_to(ROOT/'data')
    for name in ['Melspectrogram','SpeechEmbedding']:(case/'models'/(name+'.mlpackage')).symlink_to(ROOT/'models'/(name+'.mlpackage'))
    (case/'models/JarvisClassifier.mlpackage').symlink_to(ROOT/'models/AblatedClassifier.mlpackage')
    output=ROOT/'evidence/swift-ablation.json'
    env={**os.environ,'TMPDIR':'/tmp','DEVELOPER_DIR':'/Applications/Xcode.app/Contents/Developer'}
    subprocess.run([str(ROOT/'.build/release/wakeword-replay'),str(case),str(ROOT/'data/standalone.json'),str(output)],env=env,check=True)
    rows=json.loads(output.read_text())['items'];assert len(rows)==15 and all(not r['events'] for r in rows)
    print('Ablation PASS: 0/15 vs learned model 15/15; same waveforms/frontend')
if __name__=='__main__':main()
