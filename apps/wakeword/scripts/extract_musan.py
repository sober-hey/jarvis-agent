"""Extract first 20 unselected MUSAN music records as a held-out stress corpus."""
import hashlib,json,wave,subprocess
from pathlib import Path
import pyarrow.parquet as pq
from pipeline import ROOT

def main():
    out=ROOT/'data/musan';out.mkdir(exist_ok=True);rows=[]
    table=pq.read_table(ROOT/'data/musan-music.parquet')
    for i,r in enumerate(table.slice(0,20).to_pylist()):
        p=out/Path(r['path']).name;p.write_bytes(r['audio']['bytes'])
        with wave.open(str(p)) as w: seconds=w.getnframes()/w.getframerate();rate=w.getframerate();channels=w.getnchannels()
        assert rate==16000 and channels==1
        rows.append({'source_id':p.stem,'audio':str(p.relative_to(ROOT/'data')),'category':'musan_music','split':'holdout','error':None,'original_path':r['path'],'source':r['source'],'seconds':seconds,'sha256_audio':hashlib.sha256(p.read_bytes()).hexdigest()})
    noise=ROOT/'data/musan-noise-0083.wav'
    with wave.open(str(noise)) as w: seconds=w.getnframes()/w.getframerate()
    rows.append({'source_id':'musan_noise_0083','audio':str(noise.relative_to(ROOT/'data')),'category':'musan_noise','split':'holdout','error':None,'seconds':seconds,'sha256_audio':hashlib.sha256(noise.read_bytes()).hexdigest()})
    result={'source_url':'https://hf-mirror.com/datasets/corypaik/musan/resolve/main/music/train-00000-of-00010.parquet','source_sha256':hashlib.sha256((ROOT/'data/musan-music.parquet').read_bytes()).hexdigest(),'license':'MUSAN dataset CC BY4, individual music rights in upstream licenses; personal research evaluation only; no music product bundle','items':rows}
    (ROOT/'data/musan.json').write_text(json.dumps(result,indent=2));print('files',len(rows),'seconds',sum(r['seconds'] for r in rows))
if __name__=='__main__':main()
