import base64,json,os,subprocess
from pathlib import Path
from urllib.request import Request,urlopen
ROOT=Path(__file__).resolve().parents[1]
ENDPOINT='https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation'

def main():
    for k in list(os.environ):
        if k.upper() in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY'):os.environ.pop(k,None)
    results=[];meta={r['source_id']:r for r in json.loads((ROOT/'data/musan.json').read_text())['items']}
    for row in json.loads((ROOT/'evidence/swift-musan.json').read_text())['items']:
        for i,event in enumerate(row['events']):
            p=ROOT/'data/musan'/f"audit-{row['source_id']}-{i}.mp3"
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-ss',str(max(0,event-2)),'-t','5','-i',str(ROOT/'data'/meta[row['source_id']]['audio']),'-c:a','libmp3lame','-b:a','64k',str(p)],check=True)
            audio='data:audio/mp3;base64,'+base64.b64encode(p.read_bytes()).decode()
            body={'model':'qwen3-asr-flash','input':{'messages':[{'role':'user','content':[{'audio':audio}]}]}}
            req=Request(ENDPOINT,data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+(Path.home()/'.jarvis/dashscope.key').read_text().strip(),'Content-Type':'application/json'})
            try:
                result=json.loads(urlopen(req,timeout=90).read());text=result['output']['choices'][0]['message']['content'];record={'source_id':row['source_id'],'event':event,'asr_content':text,'usage':result.get('usage'),'contains_wake_word':any('jarvis' in x.get('text','').lower() or '贾维斯' in x.get('text','') for x in text)}
            except Exception as exc:record={'source_id':row['source_id'],'event':event,'error':type(exc).__name__}
            results.append(record);print(json.dumps(record,ensure_ascii=False),flush=True)
    (ROOT/'evidence/music-false-positive-asr-audit.json').write_text(json.dumps(results,indent=2,ensure_ascii=False))
if __name__=='__main__':main()
