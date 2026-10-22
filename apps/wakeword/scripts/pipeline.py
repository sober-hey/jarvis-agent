"""Deterministic openWakeWord streaming frontend; exact 80ms hops, no VAD."""
import json, wave
from pathlib import Path
import numpy as np
import onnxruntime as ort
ROOT=Path(__file__).resolve().parents[1]
class Stream:
    def __init__(self):
        opt=ort.SessionOptions();opt.intra_op_num_threads=1;opt.inter_op_num_threads=1
        self.sessions=[ort.InferenceSession(str(ROOT/'models/original'/n),sess_options=opt,providers=['CPUExecutionProvider']) for n in ['melspectrogram.onnx','embedding_model.onnx','hey_jarvis_v0.1.onnx']]
        self.reset()
    def reset(self):
        self.raw=np.zeros(480,np.float32);self.mel=np.ones((76,32),np.float32);self.features=np.zeros((16,96),np.float32);self.frames=0
    def step(self,x):
        raw=np.concatenate([self.raw,x.astype(np.float32)])
        self.raw=raw[-480:]
        mel=self.sessions[0].run(None,{'input':raw[None,:]})[0].reshape(-1,32)/10+2
        self.mel=np.concatenate([self.mel,mel])[-76:]
        embedding=self.sessions[1].run(None,{'input_1':self.mel[None,:,:,None]})[0].reshape(1,96)
        self.features=np.concatenate([self.features,embedding])[-16:]
        self.frames+=1
        p=float(self.sessions[2].run(None,{'x.1':self.features[None]})[0].squeeze())
        return p if self.frames>=5 else 0., self.features.copy()
def load_audio(item):
    with wave.open(str(ROOT/'data'/item['audio'])) as w:
        assert w.getframerate()==16000 and w.getnchannels()==1 and w.getsampwidth()==2
        return np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').copy()
def evaluate_baseline():
    items=json.loads((ROOT/'data/manifest.json').read_text())['items']
    if (ROOT/'data/standalone.json').exists():items+=json.loads((ROOT/'data/standalone.json').read_text())['items']
    s=Stream();rows=[];features={}
    for item in items:
        if item.get('error'):continue
        s.reset();audio=load_audio(item);x=np.pad(audio,(16000,16000));scores=[];fs=[]
        for i in range(0,len(x)-1279,1280):
            p,f=s.step(x[i:i+1280]);scores.append(p);fs.append(f)
        times=(np.arange(len(scores))+1)*0.08-1
        row={**item,'max_score':max(scores),'hit_08':max(scores)>=0.8,'hit_05':max(scores)>=0.5,'first_08':float(times[np.where(np.array(scores)>=.8)[0][0]]) if max(scores)>=.8 else None}
        rows.append(row);features[item['source_id']]=np.array(fs)
    (ROOT/'evidence').mkdir(exist_ok=True)
    (ROOT/'evidence/baseline.json').write_text(json.dumps(rows,indent=2))
    np.savez_compressed(ROOT/'evidence/features.npz',**features)
    from collections import defaultdict
    groups=defaultdict(list)
    for r in rows:groups[r['category']].append(r)
    print(json.dumps({k:{'n':len(v),'hits08':sum(r['hit_08'] for r in v),'hits05':sum(r['hit_05'] for r in v)} for k,v in groups.items()},indent=2))
if __name__=='__main__':evaluate_baseline()
