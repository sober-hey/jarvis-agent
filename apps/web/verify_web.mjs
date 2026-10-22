// TEST ONLY: timeline UI fixture with isolated Chrome; no model calls.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {readFile,writeFile,mkdtemp,mkdir,rm} from 'node:fs/promises';
import {join,dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
const root=dirname(fileURLToPath(import.meta.url));
if(!process.env.EVIDENCE_DIR) throw Error('Set EVIDENCE_DIR to a new disposable evidence directory');
const evidence=resolve(process.env.EVIDENCE_DIR);
await mkdir(evidence,{recursive:true});
const profile=await mkdtemp(join(evidence,'chrome-'));
const entries=[],attempts=new Map(),requests=[],pending=new Map(),checks=[];
let chrome,socket,seq=0,debugPort;
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function wait(fn,ms=20000){const end=Date.now()+ms;while(Date.now()<end){if(await fn())return;await pause(40);}throw Error('wait timeout');}
const server=createServer(async(req,res)=>{
 if(req.url==='/api/timeline'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({timeline_id:'test-timeline',entries}));return;}
 if(req.url==='/api/timeline/chat'){
  let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);requests.push(body);
  assert.equal(body.session_id,undefined);
  entries.push({id:entries.length+1,role:'user',content:body.prompt,timestamp:Date.now()/1000,source_channel:'web'});
  entries.push({id:entries.length+1,role:'assistant',content:'TEST ONLY <img src=x onerror="window.bad=1">',timestamp:Date.now()/1000,source_channel:'web'});
  const result={text:entries.at(-1).content,attempt_id:body.attempt_id,session_id:'test-timeline'};attempts.set(body.attempt_id,result);
  res.setHeader('Content-Type','application/json');res.end(JSON.stringify(result));return;
 }
 if(req.url.startsWith('/api/attempts/')){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({state:'completed',result:attempts.get(req.url.split('/').at(-1))}));return;}
 if(req.url==='/api/memories'||req.url==='/api/tasks'){res.setHeader('Content-Type','application/json');res.end(req.url==='/api/tasks'?'{"tasks":[]}':'{"memories":[]}');return;}
 const names={'/':'index.html','/app.js':'app.js','/styles.css':'styles.css','/memory.js':'memory.js','/tasks.js':'tasks.js'};
 const name=names[req.url];if(!name){res.writeHead(404);res.end();return;}
 res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html');res.end(await readFile(join(root,name)));
});
function send(method,params={}){const id=++seq;return new Promise((ok,no)=>{const timer=setTimeout(()=>no(Error(method+' timeout')),15000);pending.set(id,{ok,no,timer});socket.send(JSON.stringify({id,method,params}));});}
async function ev(expression){const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error('JS exception');return r.result.value;}
async function click(selector){const box=await ev(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);for(const type of ['mousePressed','mouseReleased'])await send('Input.dispatchMouseEvent',{type,...box,button:'left',clickCount:1});}
try{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 chrome=spawn(process.env.CHROME||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',['--headless=new','--no-first-run','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,'--window-size=1280,900','about:blank'],{stdio:'ignore'});
 await wait(async()=>{try{debugPort=Number((await readFile(join(profile,'DevToolsActivePort'),'utf8')).split('\n')[0]);return debugPort>0;}catch{return false;}});
 const pages=await(await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();socket=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);await new Promise(r=>socket.addEventListener('open',r,{once:true}));
 socket.addEventListener('message',event=>{const m=JSON.parse(event.data),p=pending.get(m.id);if(p){clearTimeout(p.timer);pending.delete(m.id);if(m.error)p.no(Error(JSON.stringify(m.error)));else p.ok(m.result);}});
 await send('Page.enable');await send('Runtime.enable');await send('Page.navigate',{url:`http://127.0.0.1:${server.address().port}/`});await wait(()=>ev("document.querySelector('#connection')?.dataset.connected==='true'"));
 assert.equal(await ev("document.querySelector('#composer-panel').hidden"),true);assert.equal(await ev("!!document.querySelector('#new-chat')"),false);checks.push('collapsed input and no new-chat');
 await click('#composer-toggle');assert.equal(await ev("document.querySelector('#composer-panel').hidden"),false);await click('#prompt');await send('Input.insertText',{text:'fixture message'});await click('#send');await wait(()=>ev("document.querySelectorAll('.message').length===2"));checks.push('expand and send');
 assert.equal(await ev("document.querySelectorAll('#messages img').length"),0);assert.equal(await ev('window.bad||0'),0);checks.push('text-only rendering');
 await send('Page.reload');await wait(()=>ev("document.querySelectorAll('.message').length===2"));assert.equal(requests.length,1);checks.push('refresh reads same timeline without resend');
 assert.equal(await ev("/会话|对话历史|新对话/.test(document.body.innerText)"),false);checks.push('no public session wording');
 await writeFile(join(evidence,'timeline-ui.png'),Buffer.from((await send('Page.captureScreenshot',{format:'png'})).data,'base64'));
 await writeFile(join(evidence,'results.json'),JSON.stringify({ok:true,checks,requests},null,2));console.log(JSON.stringify({ok:true,checks}));
}finally{
 socket?.close();if(chrome&&chrome.exitCode===null){chrome.kill('SIGTERM');await wait(()=>chrome.exitCode!==null,5000).catch(()=>{});if(chrome.exitCode===null)chrome.kill('SIGKILL');}server.closeAllConnections();await new Promise(r=>server.close(r));await rm(profile,{recursive:true,force:true});
}
