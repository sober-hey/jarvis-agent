"""Compare our deterministic stream against official openWakeWord inference."""
import json
import numpy as np
from openwakeword.model import Model
from pipeline import ROOT,load_audio

def main():
    np.random.seed(42)
    m=Model(wakeword_models=[str(ROOT/'models/original/hey_jarvis_v0.1.onnx')],inference_framework='onnx',melspec_model_path=str(ROOT/'models/original/melspectrogram.onnx'),embedding_model_path=str(ROOT/'models/original/embedding_model.onnx'))
    rows=json.loads((ROOT/'evidence/baseline.json').read_text());results=[]
    for r in rows:
        np.random.seed(42);m.reset();x=np.pad(load_audio(r),(16000,16000));scores=[]
        for i in range(0,len(x)-1279,1280):scores.append(float(list(m.predict(x[i:i+1280]).values())[0]))
        results.append({'source_id':r['source_id'],'category':r['category'],'split':r['split'],'max_score':max(scores),'hit':max(scores)>=.8,'deterministic_max_score':r['max_score']})
    (ROOT/'evidence/upstream-baseline.json').write_text(json.dumps(results,indent=2))
    from collections import defaultdict
    groups=defaultdict(list)
    for r in results:groups[r['category']].append(r)
    print({k:(sum(r['hit'] for r in v),len(v)) for k,v in groups.items()})
if __name__=='__main__':main()
