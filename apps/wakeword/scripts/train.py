"""Train a bare-Jarvis openWakeWord-style MLP; holdout is never optimized."""
import json, hashlib
import numpy as np
import torch
from torch import nn
from pathlib import Path
from convert import Head, export, ROOT

def main():
    torch.manual_seed(309);np.random.seed(309);torch.set_num_threads(2)
    rows=json.loads((ROOT/'evidence/baseline.json').read_text());features=np.load(ROOT/'evidence/features.npz')
    x=[];y=[];source_ids=[]
    for row in rows:
        if row['split']!='calibration' or row['category']=='control':continue
        fs=features[row['source_id']];times=(np.arange(len(fs))+1)*.08-1
        if row['category'] in ('positive','bare'):
            use=(times>=.72)&(times<=1.6)
            x.extend(fs[use]);y.extend([1]*int(use.sum()))
            # Warmup/silence are negative, not just speech negatives.
            use=times<0
            x.extend(fs[use]);y.extend([0]*int(use.sum()))
        else:
            x.extend(fs);y.extend([0]*len(fs))
        source_ids.append(row['source_id'])
    x=torch.tensor(np.array(x),dtype=torch.float32);y=torch.tensor(y,dtype=torch.float32)[:,None]
    model=Head();optim=torch.optim.AdamW(model.parameters(),lr=.001,weight_decay=.003)
    posweight=(1-y).sum()/y.sum()
    history=[]
    for epoch in range(90):
        model.train();optim.zero_grad();p=model(x).clamp(1e-6,1-1e-6)
        loss=(-(posweight*y*torch.log(p)+(1-y)*torch.log(1-p))).mean();loss.backward();optim.step()
        if epoch%15==0:print(epoch,float(loss.detach()),flush=True);history.append([epoch,float(loss.detach())])
    model.eval();torch.save(model.state_dict(),ROOT/'models/jarvis-head.pt')
    export('JarvisClassifier',model,torch.zeros(1,16,96),'features','confidence')
    evidence={'seed':309,'epochs':90,'training_sources':source_ids,'windows':len(y),'positive_windows':int(y.sum()),'loss':history,'labels':'English Jarvis word at clip start; positive window end 0.72..1.60s; pre-speech padding negative; control excluded; holdout excluded','weights_sha256':hashlib.sha256((ROOT/'models/jarvis-head.pt').read_bytes()).hexdigest()}
    results=[]
    with torch.no_grad():
        for row in rows:
            fs=features[row['source_id']];p=model(torch.tensor(fs)).numpy().flatten();p[:4]=0
            ix=np.where(p>=.8)[0]
            results.append({'source_id':row['source_id'],'split':row['split'],'category':row['category'],'voice':row['voice'],'max_score':float(p.max()),'hit':len(ix)>0,'first':float((ix[0]+1)*.08-1) if len(ix) else None,'audio_seconds':row['duration_seconds']})
    (ROOT/'evidence/training.json').write_text(json.dumps(evidence,indent=2));(ROOT/'evidence/python-evaluation.json').write_text(json.dumps(results,indent=2))
    from collections import defaultdict
    grouped=defaultdict(list)
    for r in results:grouped[(r['category'],r['split'])].append(r)
    print({str(k):(sum(r['hit'] for r in v),len(v)) for k,v in grouped.items()})
if __name__=='__main__':main()
