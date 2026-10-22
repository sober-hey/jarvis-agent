"""Held-out perturbations of five standalone validation files; no retraining."""
import json,hashlib,wave
import numpy as np
from scipy.signal import fftconvolve
from pipeline import ROOT,load_audio
from generate_stress import wav

def main():
    rng=np.random.default_rng(6921);rows=[];out=ROOT/'data/perturbed';out.mkdir(exist_ok=True)
    source=json.loads((ROOT/'data/standalone.json').read_text())['items']
    for r in source:
        if r['split']!='holdout':continue
        x=load_audio(r).astype(np.float64)/32768
        for mode in ['quiet_gain','noise_10db','reverb']:
            if mode=='quiet_gain':xx=x*.2
            elif mode=='noise_10db':xx=x+rng.normal(0,np.sqrt(np.mean(x*x)/10),len(x))
            else:
                impulse=np.zeros(6401);impulse[0]=1;impulse[1120]=.35;impulse[2400]=.18;impulse[6400]=.06;xx=fftconvolve(x,impulse)[:len(x)]*.65
            p=out/(r['source_id']+'_'+mode+'.wav');wav(p,xx)
            rows.append({'source_id':p.stem,'parent_source_id':r['source_id'],'category':mode,'split':'holdout_augmented','audio':str(p.relative_to(ROOT/'data')),'error':None,'sha256_audio':hashlib.sha256(p.read_bytes()).hexdigest()})
    (ROOT/'data/perturbed.json').write_text(json.dumps({'items':rows},indent=2))
if __name__=='__main__':main()
