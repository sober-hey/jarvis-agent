"""Generate deterministic held-out signal stress tests; not recorded ambient audio."""
import json,wave,hashlib
import numpy as np
from scipy.signal import lfilter
from pipeline import ROOT

def wav(path,x):
    with wave.open(str(path),'wb') as w:w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes(np.clip(x*32768,-32768,32767).astype('<i2').tobytes())
def main():
    out=ROOT/'data/stress';out.mkdir(exist_ok=True);rng=np.random.default_rng(62801);rows=[]
    n=16000*600;t=np.arange(n)/16000
    for name in ['silence','white_noise','pink_noise','procedural_music']:
        if name=='silence':x=np.zeros(n)
        elif name=='white_noise':x=rng.normal(0,.025,n)
        elif name=='pink_noise':
            x=lfilter([.049922,.095993,-.050612,-.004408],[1,-2.494956,2.017265,-.522189],rng.normal(size=n));x=x/np.std(x)*.04
        else:
            # Original random four-chord instrumental proxy, not a real music benchmark.
            x=np.zeros(n)
            for k in range(1200):
                start=k*8000;tt=np.arange(8000)/16000;root=[48,53,55,57][(k//8)%4];freq=440*2**((root+int(rng.choice([0,4,7,12,16]))-69)/12)
                note=(np.sin(2*np.pi*freq*tt)+.3*np.sin(4*np.pi*freq*tt))*np.exp(-tt*5)
                x[start:start+8000]=note*.07
            x+=rng.normal(0,.005,n)
        path=out/(name+'.wav');wav(path,x)
        rows.append({'source_id':name,'category':'stress_'+name,'split':'holdout','audio':str(path.relative_to(ROOT/'data')),'error':None,'synthetic':True,'seconds':600,'sha256_audio':hashlib.sha256(path.read_bytes()).hexdigest()})
    (ROOT/'data/stress.json').write_text(json.dumps({'items':rows},indent=2))
if __name__=='__main__':main()
