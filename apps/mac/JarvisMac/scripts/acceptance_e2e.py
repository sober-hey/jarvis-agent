#!/usr/bin/env python3
"""Signed app -> original-rate WAV detector -> VAD PCM -> real WS -> real MP3 decoder.
No microphone, cloud key, model, TCC prompt, network changes or startup registration.
"""
import asyncio
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import wave
import math
import struct
import websockets

ROOT = Path(__file__).resolve().parents[1]
APP = Path(os.environ.get('JARVIS_TEST_APP',str(ROOT/'Output/JARVIS.app')))
EVIDENCE = ROOT/'Evidence'/(datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-e2e')


def prepare():
    out = ROOT/'Tests/ReplayFixtures'
    out.mkdir(parents=True, exist_ok=True)
    clap = ROOT/'Vendor/Activation/Evidence/WAV/double-clap-2.wav'
    with wave.open(str(clap)) as w:
        rate = w.getframerate(); frames = w.readframes(w.getnframes())
    # Explicit harmonic speech PROXY (not ASR/human speech validation).
    speech = b''.join(struct.pack('<h', int(32767*(0.11*math.sin(2*math.pi*220*i/rate)+0.04*math.sin(2*math.pi*440*i/rate)))) for i in range(int(rate*.8)))
    with wave.open(str(out/'acceptance.wav'), 'wb') as w:
        w.setparams((1,2,rate,0,'NONE','not compressed'))
        w.writeframes(frames+speech+bytes(int(rate*1.2)*2))
    return {'clap_source':str(clap), 'clap_sha256':hashlib.sha256(clap.read_bytes()).hexdigest(),
            'replay_wav':str(out/'acceptance.wav'), 'sample_rate':rate, 'speech':'synthetic harmonic proxy; 0.8s; not real speech'}


async def scenario(name, mp3):
    wire=[]; errors=[]; uploads=[]; playback_done=[]
    async def handler(ws):
        generation=0
        try:
            async for raw in ws:
                value=json.loads(raw); kind=value['type']
                wire.append({k:v for k,v in value.items() if k!='data'})
                if kind=='hello':
                    assert value['device_type']=='mac'
                    assert value['sample_rate']==16000 and value['channels']==1
                    assert value['state']['headphones_worn'] is None
                    await ws.send(json.dumps({'type':'suppressed' if name=='suppressed' else 'selected'}))
                elif kind=='audio':
                    assert any(x['type']=='hello' for x in wire)
                    assert value['format']=='pcm_s16le' and value['sample_rate']==16000
                    pcm=base64.b64decode(value['data'],validate=True)
                    assert 32000 < len(pcm) <= 800000 and len(pcm)%2==0
                    uploads.append(len(pcm))
                    (EVIDENCE/f'{name}-upload-{len(uploads)}.pcm').write_bytes(pcm)
                elif kind=='audio_end':
                    generation+=1
                    await ws.send(json.dumps({'type':'interrupt','generation':generation}))
                    for role,text in [('user','fixture transcript'),('assistant','fixture reply')]:
                        await ws.send(json.dumps({'type':'text','generation':generation,'role':role,'text':text}))
                    for offset in range(0,len(mp3),16000):
                        await ws.send(json.dumps({'type':'audio','format':'mp3','data':base64.b64encode(mp3[offset:offset+16000]).decode(),'generation':generation,'final':False}))
                    await ws.send(json.dumps({'type':'audio','format':'mp3','data':'','generation':generation,'final':True}))
                elif kind=='playback_done': playback_done.append(value['generation'])
        except websockets.exceptions.ConnectionClosed: pass
        except Exception as exc: errors.append(repr(exc))
    async with websockets.serve(handler,'127.0.0.1',8765,origins=[None]):
        proc=await asyncio.create_subprocess_exec(str(APP/'Contents/MacOS/JarvisMacApp'),
            '--replay-wav','bundle:acceptance.wav','--evidence-json','-', '--scenario',name,
            '--gateway','ws://127.0.0.1:8765', stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try: stdout,stderr=await asyncio.wait_for(proc.communicate(),45)
        except asyncio.TimeoutError:
            proc.kill(); stdout,stderr=await proc.communicate(); errors.append('app_timeout')
        (EVIDENCE/f'{name}.stdout.log').write_bytes(stdout)
        (EVIDENCE/f'{name}.stderr.log').write_bytes(stderr)
    records=[json.loads(line[len('JARVIS_ACCEPTANCE '):]) for line in stdout.decode(errors='replace').splitlines() if line.startswith('JARVIS_ACCEPTANCE ')]
    result=next((r for r in reversed(records) if 'result' in r),{})
    checks={'app_exit_zero':proc.returncode==0,'app_assertions':result.get('result',{}).get('passed') is True,
        'hello':any(x['type']=='hello' and x.get('device_type')=='mac' for x in wire),
        'device_state':any(x['type']=='device_state' for x in wire),'fake_server_no_errors':not errors,
        'upload_count':len(uploads)==({'single':1,'interrupt':2,'suppressed':0}[name]),
        'playback_ack':bool(playback_done) if name!='suppressed' else not playback_done}
    events=result.get('events',[])
    if name!='suppressed':
        checks['measured_vad_silence']=all(1.0<=e['silence_seconds']<=1.05 for e in events if e.get('event')=='vad_audio_end')
        checks['original_rate_activation']=any(e.get('event')=='activation' and e.get('original_sample_rate')==48000 for e in events)
        states={e.get('state') for e in events if e.get('event')=='ui_render'}
        checks['all_five_ui_states']={'待机','聆听','思考','说话','断线'} <= states
        checks['wake_word_disabled']=all(e['wake_unready'] and e['clap_enabled'] and e['snap_enabled'] for e in events if e.get('event')=='ui_render')
    if name=='interrupt': checks['no_old_drain_ack']=playback_done==[2]
    result.update({'checks':checks,'wire':wire,'upload_bytes':uploads,'server_errors':errors,'exit_code':proc.returncode})
    (EVIDENCE/f'{name}.json').write_text(json.dumps(result,indent=2,ensure_ascii=False))
    print(name,json.dumps(checks),flush=True)
    return all(checks.values())


async def main():
    EVIDENCE.mkdir(parents=True,exist_ok=False)
    metadata=prepare(); (EVIDENCE/'fixture.json').write_text(json.dumps(metadata,indent=2))
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=400:duration=7','-codec:a','libmp3lame','-q:a','7',str(EVIDENCE/'gateway-response.mp3')],check=True)
    mp3=(EVIDENCE/'gateway-response.mp3').read_bytes()
    # MP3 is actual ffmpeg encoding, never three-byte transport placeholder.
    passed=[]
    for name in ['single','interrupt','suppressed']: passed.append(await scenario(name,mp3))
    summary={'scope':'PASS_LOCAL_FIXTURE' if all(passed) else 'FAIL_LOCAL_FIXTURE', 'passed':sum(passed),'total':len(passed),
             'real_microphone':'NOT_RUN','cloud_asr_tts_brain':'NOT_RUN','audible_output':'NOT_VERIFIED_VOLUME_ZERO','evidence':str(EVIDENCE)}
    (EVIDENCE/'SUMMARY.json').write_text(json.dumps(summary,indent=2)); print(json.dumps(summary,indent=2))
    return 0 if all(passed) else 1

if __name__=='__main__':
    if '--prepare' in sys.argv: print(json.dumps(prepare(),indent=2))
    else: raise SystemExit(asyncio.run(main()))
