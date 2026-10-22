"""Aggregate saved native/CoreML results, never infer counts by hand."""
import json,hashlib,collections,platform,subprocess,datetime
from pathlib import Path
import numpy as np
from pipeline import ROOT

def main():
    read=lambda p:json.loads((ROOT/p).read_text())
    native=read('evidence/swift-main.json')['items']+read('evidence/swift-standalone.json')['items']
    baseline=read('evidence/upstream-baseline.json');pytorch={r['source_id']:r for r in read('evidence/python-evaluation.json')}
    def group(rows):
        groups=collections.defaultdict(list)
        for r in rows:groups[r['category']+' / '+r['split']].append(r)
        return {k:{'clips':len(v),'hit_clips':sum(bool(r.get('events',[])) if 'events' in r else r['hit'] for r in v),'events':sum(len(r.get('events',[])) for r in v)} for k,v in groups.items()}
    results={'native':group(native),'baseline':group(baseline),'perturbed':group(read('evidence/swift-perturbed.json')['items'])}
    for key,path in [('music','evidence/swift-musan.json'),('signal_stress','evidence/swift-stress.json')]:
        d=read(path);rows=d['items'];seconds=sum(r['audio_seconds'] for r in rows);events=sum(len(r['events']) for r in rows)
        results[key]={'clips':len(rows),'seconds':seconds,'hours':seconds/3600,'events':events,'false_activations_per_hour':events/seconds*3600,'hop_latency_ms':d['latency_ms']}
    neg=[r for r in native if r['category'].startswith('negative')];sec=sum(r['audio_seconds'] for r in neg)
    results['speech_negatives']={'clips':len(neg),'seconds':sec,'events':sum(len(r['events']) for r in neg),'false_activations_per_hour':sum(len(r['events']) for r in neg)/sec*3600}
    results['max_native_torch_clip_score_error']=max(abs(r['max_score']-pytorch[r['source_id']]['max_score']) for r in native)
    results['packages']={name:sum(p.stat().st_size for p in (ROOT/'models'/name).rglob('*') if p.is_file()) for name in ['Melspectrogram.mlpackage','SpeechEmbedding.mlpackage','JarvisClassifier.mlpackage']}
    results['packages_total_bytes']=sum(results['packages'].values())
    results['host']={'platform':platform.platform(),'cpu':subprocess.check_output(['sysctl','-n','machdep.cpu.brand_string'],text=True).strip(),'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    results['offline_network_denied']=read('evidence/swift-offline-sandbox.json')['contract_checks']
    results['ablation_hits']=sum(bool(r['events']) for r in read('evidence/swift-ablation.json')['items'])
    latency=read('evidence/audio-latency.json');results['standalone_energy_onset_to_event_seconds']={'min':min(r['from_start'] for r in latency),'median':float(np.median([r['from_start'] for r in latency])),'max':max(r['from_start'] for r in latency)}
    train=read('evidence/training.json')['training_sources'];allrows=read('data/manifest.json')['items']+read('data/standalone.json')['items']
    assert not set(train)&{r['source_id'] for r in allrows if r['split']=='holdout'}
    assert len(allrows)==111 and len(native)==111
    artifacts={str(p.relative_to(ROOT)):{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for name in results['packages'] for p in sorted((ROOT/'models'/name).rglob('*')) if p.is_file()}
    (ROOT/'evidence/model-sha256.json').write_text(json.dumps(artifacts,indent=2))
    (ROOT/'evidence/SUMMARY.json').write_text(json.dumps(results,indent=2));print(json.dumps(results,indent=2))
if __name__=='__main__':main()
