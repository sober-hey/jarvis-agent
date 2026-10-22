"""Independent standalone Jarvis corpus (not Jarvis+command)."""
import concurrent.futures, hashlib, json, os
from pathlib import Path
from generate_tts import ROOT, DATA, WAV, KEY_PATH, QWEN_VOICES, CLONE, post, normalize, duration

def make(task):
    item, tempo, split=task
    source, model, original, usage, elapsed, error=post(item,KEY_PATH.read_text().strip(),90)
    result={**source,'tempo':tempo,'split':split,'model':model,'usage':usage,'error':error,'elapsed_seconds':elapsed}
    if original:
        wav=WAV/(item['source_id']+'.wav');normalize(original,wav,tempo)
        result.update(audio=str(wav.relative_to(DATA)),duration_seconds=duration(wav),sha256_audio=hashlib.sha256(wav.read_bytes()).hexdigest())
    print(result['source_id'],error,flush=True)
    return result
if __name__=='__main__':
    for k in list(os.environ):
        if k.upper() in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY'):os.environ.pop(k,None)
    tasks=[]
    for i,voice in enumerate(QWEN_VOICES+[CLONE]):
        for j,tempo in enumerate([0.85,1.0,1.15]):
            item={'source_id':f'bare_{i}_{j}','text':'Jarvis.','category':'bare','language_type':'English','voice':voice,'voice_kind':'clone' if voice==CLONE else 'system'}
            tasks.append((item,tempo,'holdout' if j==1 else 'calibration'))
    existing={x['source_id']:x for x in json.loads((DATA/'standalone.json').read_text())['items']} if (DATA/'standalone.json').exists() else {}
    rows=[]
    for task in tasks:
        sid=task[0]['source_id']
        row=existing.get(sid)
        if row is None or row.get('error'):
            import time
            for attempt in range(4):
                row=make(task)
                if not row.get('error'):break
                time.sleep(5*(attempt+1))
        rows.append(row)
        (DATA/'standalone.json').write_text(json.dumps({'items':rows+list(existing[k] for k in existing if k not in {r['source_id'] for r in rows})},indent=2))
    (DATA/'standalone.json').write_text(json.dumps({'items':rows},indent=2))
