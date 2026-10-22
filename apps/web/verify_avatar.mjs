// Controlled FRONTEND-ONLY acceptance: no backend mutation, models, or messages.
// URL=http://127.0.0.1:PORT/ EVIDENCE_DIR=/new/disposable/root node apps/web/verify_avatar.mjs
// Requires Node with global WebSocket and local Chrome (override CHROME if needed).
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdir, mkdtemp, readFile, writeFile, rm, realpath} from 'node:fs/promises';
import {join, resolve, dirname} from 'node:path';

if (!process.env.URL?.trim() || !process.env.EVIDENCE_DIR?.trim()) throw Error('URL and EVIDENCE_DIR are required; no historical output default');
if (typeof WebSocket !== 'function') throw Error('Node with global WebSocket is required');
const target = new URL(process.env.URL);
if (!['http:', 'https:'].includes(target.protocol) || !['localhost', '127.0.0.1', '[::1]'].includes(target.hostname) || target.username || target.password) {
  throw Error('URL must be a credential-free loopback HTTP(S) frontend');
}
const root = resolve(process.env.EVIDENCE_DIR);
if (root.split(/[\\/]/).includes('.runs_01')) throw Error('Historical evidence roots are forbidden');
// Resolve the nearest existing ancestor BEFORE mkdir: resolving after mkdir can
// already mutate history when a non-existing child lies below a symlink alias.
let ancestor = root;
for (;;) {
  try {
    if ((await realpath(ancestor)).split(/[\\/]/).includes('.runs_01')) throw Error('Historical evidence symlink target forbidden');
    break;
  } catch (error) {
    if (error.code !== 'ENOENT' || dirname(ancestor) === ancestor) throw error;
    ancestor = dirname(ancestor);
  }
}
await mkdir(root, {recursive: true});
if ((await realpath(root)).split(/[\\/]/).includes('.runs_01')) throw Error('Historical evidence symlink target forbidden');
const evidence = await mkdtemp(join(root, 'frontend-probe-'));
const profile = await mkdtemp(join(evidence, 'chrome-profile-'));
const checks = [], consoleErrors = [], networkErrors = [], blockedRequests = [], requests = [], pending = new Map();
let chrome, socket, commandId = 0, spawnError;
const result = {ok: false, scope: 'CONTROLLED_FRONTEND_ONLY', target: target.href, evidence,
  checks, consoleErrors, networkErrors, blockedRequests, requests, snapshots: {}};
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function wait(fn, milliseconds = 15000) {
  const end = Date.now() + milliseconds;
  while (Date.now() < end) { if (await fn()) return; await pause(40); }
  throw Error('condition timed out');
}
function send(method, params = {}) {
  const id = ++commandId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(Error(`${method} timed out`)); }, 10000);
    pending.set(id, {resolve, reject, timer});
    socket.send(JSON.stringify({id, method, params}));
  });
}
async function ev(expression) {
  const response = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  if (response.exceptionDetails) throw Error(response.exceptionDetails.text + ': ' + (response.exceptionDetails.exception?.description || ''));
  return response.result.value;
}
const controlledFrame = {
  schema_version: 1, sequence: 1, timestamp: 123, degraded: false, missing_sources: [], activity: 'speaking', action: 'priority',
  channels: {
    expression: {brow_raise: .18, brow_knit: .06, mouth_corner: .15, eyelid_open: .2, lip_press: .03, care: .22, focus: .18, displeasure: .02},
    posture: {lean_forward: .07, head_pitch_rad: .09, head_yaw_rad: .09, head_roll_rad: .07},
    gaze: {yaw_rad: .11, pitch_rad: -.07, engagement: .9},
    idle: {breath_offset: .007, head_sway_rad: .014, eyelid_micro: .02},
    mouth: {open: .85, level: .8},
  },
};
// This constructor exists only in the isolated verifier tab, never in avatar.js.
const bootstrap = `(() => {
  const sources = [];
  class ControlledEventSource extends EventTarget {
    constructor(url) { super(); this.url = url; this.closed = false; sources.push(this); }
    close() { this.closed = true; }
    emit(type, data) { this.dispatchEvent(new MessageEvent(type, {data})); }
  }
  window.EventSource = ControlledEventSource;
  window.__avatarProbe = {sources, emit(type, data) { sources.at(-1).emit(type, data); }};
})();`;
async function emit(type, data = '') {
  await ev(`window.__avatarProbe.emit(${JSON.stringify(type)}, ${JSON.stringify(data)})`);
}
async function apply(frame) { await emit('avatar', JSON.stringify(frame)); await pause(320); }
async function snapshot(name) {
  const state = await ev(`(() => {
    const figure = document.querySelector('#avatar-figure');
    const css = selector => {const s=getComputedStyle(document.querySelector(selector));return {transform:s.transform,height:s.height,color:s.borderTopColor,transition:s.transitionDuration,animation:s.animationName}};
    const rect = selector => {const r=document.querySelector(selector).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right}};
    return {connection:figure.dataset.connection,activity:figure.dataset.activity,action:figure.dataset.action,
      label:figure.getAttribute('aria-label'),role:figure.getAttribute('role'),status:document.querySelector('#avatar-status').textContent,
      statusLive:document.querySelector('#avatar-status').getAttribute('aria-live'),mouth:css('.avatar-mouth'),eye:css('.avatar-eye'),
      head:css('.avatar-head'),body:css('.avatar-body'),gaze:css('.avatar-pupil'),figure:rect('#avatar-figure'),stage:rect('#avatar-stage'),
      reduced:matchMedia('(prefers-reduced-motion: reduce)').matches,overflow:document.documentElement.scrollWidth>innerWidth,
      composerVisible:!!document.querySelector('#composer-toggle'),mouthValue:figure.style.getPropertyValue('--avatar-mouth-open')};
  })()`);
  result.snapshots[name] = state;
  return state;
}
async function screenshot(name) {
  const {cssContentSize} = await send('Page.getLayoutMetrics');
  const height = Math.ceil(cssContentSize.height);
  assert.ok(height <= 16000, 'page screenshot height exceeds explicit safety limit');
  const png = await send('Page.captureScreenshot', {format: 'png', captureBeyondViewport: true,
    clip: {x: 0, y: 0, width: Math.ceil(cssContentSize.width), height, scale: 1}});
  await writeFile(join(evidence, `${name}.png`), Buffer.from(png.data, 'base64'), {flag: 'wx'});
}
async function click(selector) {
  const point = await ev(`(() => {const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);
  for (const type of ['mousePressed', 'mouseReleased']) await send('Input.dispatchMouseEvent', {type, ...point, button: 'left', clickCount: 1});
}
async function intercept(params) {
  const {requestId, request} = params;
  const url = new URL(request.url);
  const fulfill = (body, mime = 'application/json', responseCode = 200) => send('Fetch.fulfillRequest', {
    requestId, responseCode, responseHeaders: [{name: 'Content-Type', value: mime}, {name: 'Cache-Control', value: 'no-store'}],
    body: Buffer.from(body).toString('base64'),
  });
  if (request.method !== 'GET' || url.origin !== target.origin) {
    blockedRequests.push({url: url.href, method: request.method});
    await fulfill('', 'text/plain', 403); return;
  }
  // Read endpoints are stubbed client-side too: never expose existing messages/data.
  const fixtures = {'/api/timeline': {entries: [], timeline_id: 'frontend-probe-only'}, '/api/tasks': {tasks: []}, '/api/memories': {memories: []}};
  if (Object.hasOwn(fixtures, url.pathname)) { await fulfill(JSON.stringify(fixtures[url.pathname])); return; }
  if (url.pathname === '/favicon.ico') { await fulfill('', 'image/x-icon', 204); return; }
  if (url.pathname.startsWith('/api/')) {
    blockedRequests.push({url: url.href, method: request.method});
    await fulfill('{}', 'application/json', 403); return;
  }
  await send('Fetch.continueRequest', {requestId});
}
try {
  chrome = spawn(process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
    '--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
    '--disable-component-update', '--disable-sync', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
    `--user-data-dir=${profile}`, '--window-size=1280,900', 'about:blank',
  ], {stdio: 'ignore'});
  chrome.on('error', error => { spawnError = error; });
  let debugPort;
  await wait(async () => {
    if (spawnError) throw spawnError;
    if (chrome.exitCode !== null) throw Error(`Chrome exited ${chrome.exitCode}`);
    try { debugPort = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); return debugPort > 0; } catch { return false; }
  });
  const pages = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
  const page = pages.find(page => page.type === 'page');
  assert.ok(page, 'Chrome page target');
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, {once: true}); socket.addEventListener('error', reject, {once: true}); });
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    const command = pending.get(message.id);
    if (command) {
      clearTimeout(command.timer); pending.delete(message.id);
      if (message.error) command.reject(Error(JSON.stringify(message.error))); else command.resolve(message.result);
      return;
    }
    const p = message.params;
    if (message.method === 'Fetch.requestPaused') intercept(p).catch(error => consoleErrors.push({interception: String(error)}));
    if (message.method === 'Runtime.exceptionThrown') consoleErrors.push(p.exceptionDetails);
    if (message.method === 'Runtime.consoleAPICalled' && p.type === 'error') consoleErrors.push(p);
    if (message.method === 'Log.entryAdded' && p.entry.level === 'error') consoleErrors.push(p.entry);
    if (message.method === 'Network.loadingFailed' && !p.canceled) networkErrors.push(p);
    if (message.method === 'Network.responseReceived' && p.response.status >= 400) networkErrors.push({url: p.response.url, status: p.response.status});
    if (message.method === 'Network.requestWillBeSent') requests.push({url: p.request.url, method: p.request.method});
  });
  await Promise.all([send('Page.enable'), send('Runtime.enable'), send('Network.enable'), send('Log.enable')]);
  await send('Fetch.enable', {patterns: [{urlPattern: '*', requestStage: 'Request'}]});
  await send('Page.addScriptToEvaluateOnNewDocument', {source: bootstrap});
  await send('Emulation.setEmulatedMedia', {features: [{name: 'prefers-reduced-motion', value: 'no-preference'}]});
  await send('Emulation.setDeviceMetricsOverride', {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false});
  const navigation = await send('Page.navigate', {url: target.href});
  assert.equal(navigation.errorText, undefined);
  await wait(() => ev("!!window.JarvisAvatar && window.__avatarProbe.sources.length===1 && document.readyState==='complete'"));
  const neutral = await snapshot('neutral');
  assert.equal(neutral.connection, 'disconnected'); assert.equal(neutral.mouthValue, '0');
  assert.equal(neutral.role, 'img'); assert.ok(neutral.label); assert.equal(neutral.statusLive, 'polite');
  await screenshot('neutral'); checks.push('accessible neutral initial state');

  await emit('open'); await apply(controlledFrame);
  const speaking = await snapshot('speaking');
  assert.equal(speaking.activity, 'speaking'); assert.equal(speaking.connection, 'live'); assert.equal(speaking.action, 'priority');
  assert.ok(parseFloat(speaking.mouth.height) > parseFloat(neutral.mouth.height) + 10);
  for (const part of ['head', 'body', 'gaze', 'eye']) assert.notEqual(speaking[part].transform, neutral[part].transform, `${part} changed`);
  assert.notEqual(speaking.head.color, neutral.head.color);
  await screenshot('speaking'); checks.push('controlled frame changes mouth, eyes, gaze, expression hue and posture');

  await ev(`window.__statusWrites=0; window.__statusObserver=new MutationObserver(rows=>window.__statusWrites+=rows.length); window.__statusObserver.observe(document.querySelector('#avatar-status'),{childList:true,characterData:true,subtree:true})`);
  for (let sequence = 2; sequence <= 5; sequence++) await apply({...controlledFrame, sequence});
  assert.equal(await ev('window.__statusWrites'), 0); checks.push('per-frame changes do not announce status');
  await apply({...controlledFrame, sequence: 6, degraded: true, missing_sources: ['affect']});
  assert.equal((await snapshot('degraded')).connection, 'degraded');
  assert.equal(await ev('window.__statusWrites'), 1); checks.push('degradation is a brief technical status');

  await send('Emulation.setEmulatedMedia', {features: [{name: 'prefers-reduced-motion', value: 'reduce'}]});
  await apply({...controlledFrame, sequence: 7});
  const reduced = await snapshot('reduced');
  assert.equal(reduced.reduced, true);
  for (const part of ['head', 'body', 'gaze']) { assert.equal(reduced[part].transform, 'none'); assert.equal(reduced[part].transition, '0s'); }
  assert.ok(parseFloat(reduced.mouth.height) > 10); await screenshot('reduced'); checks.push('reduced motion suppresses spatial drift and transitions, retains speech cue');

  await emit('error'); await pause(320);
  assert.equal((await snapshot('disconnected')).connection, 'disconnected');
  assert.equal(await ev("document.querySelector('#avatar-figure').style.getPropertyValue('--avatar-mouth-open')"), '0');
  await emit('open'); await apply({...controlledFrame, sequence: 0});
  assert.equal((await snapshot('reconnected')).connection, 'live'); checks.push('disconnect neutralizes; native stream reopen accepts reset sequence');
  await emit('avatar', '{');
  assert.equal((await snapshot('malformed')).connection, 'disconnected');
  await apply({...controlledFrame, sequence: 1});
  await ev("dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}))");
  assert.equal(await ev('window.__avatarProbe.sources[0].closed'), true);
  assert.equal((await snapshot('pagehide')).connection, 'disconnected');
  await ev("dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true})); dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))");
  assert.equal(await ev('window.__avatarProbe.sources.length'), 2);
  await apply({...controlledFrame, sequence: 0});
  assert.equal((await snapshot('pageshow')).connection, 'live'); checks.push('malformed frame and pagehide are neutral; simulated bfcache lifecycle reconnects once');
  await wait(() => ev("document.querySelector('#avatar-figure').dataset.connection==='disconnected'"), 8000);
  assert.equal((await snapshot('stale')).mouthValue, '0'); checks.push('real six-second stale timeout returns to neutral');
  await apply({...controlledFrame, sequence: 1});

  await send('Emulation.setDeviceMetricsOverride', {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
  const mobile = await snapshot('mobile');
  assert.equal(mobile.overflow, false); assert.equal(mobile.composerVisible, true);
  assert.ok(mobile.figure.y >= mobile.stage.y && mobile.figure.bottom <= mobile.stage.bottom, 'entire figure remains in mobile stage');
  await click('#composer-toggle');
  assert.equal(await ev("document.querySelector('#composer-panel').hidden"), false);
  await screenshot('mobile'); checks.push('mobile figure fits and existing composer still opens without submission');
  await ev("dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}))");
  await pause(100);
  assert.equal(blockedRequests.length, 0, 'no external, mutating, or unexpected API requests');
  assert.equal(consoleErrors.length, 0, 'no console/runtime errors');
  assert.equal(networkErrors.length, 0, 'no failed frontend asset requests');
  checks.push('zero console, network, external, mutation or unexpected API errors');
  result.ok = true;
} catch (error) {
  result.error = error.stack || String(error);
  process.exitCode = 1;
} finally {
  socket?.close();
  for (const command of pending.values()) { clearTimeout(command.timer); command.reject(Error('probe closed')); }
  if (chrome?.pid && chrome.exitCode === null && chrome.signalCode === null) {
    chrome.kill('SIGTERM');
    await wait(() => chrome.exitCode !== null || chrome.signalCode !== null, 5000).catch(() => {});
    if (chrome.exitCode === null && chrome.signalCode === null) { chrome.kill('SIGKILL'); await wait(() => chrome.signalCode !== null, 5000).catch(() => {}); }
  }
  result.cleanup = {chromePid: chrome?.pid, chromeExited: !chrome?.pid || chrome.exitCode !== null || chrome.signalCode !== null};
  try { await rm(profile, {recursive: true, force: true, maxRetries: 3, retryDelay: 100}); result.cleanup.profileRemoved = true; }
  catch (error) { result.cleanup.profileRemoved = false; result.cleanup.error = String(error); }
  if (!result.cleanup.chromeExited || !result.cleanup.profileRemoved) { result.ok = false; process.exitCode = 1; }
  await writeFile(join(evidence, 'results.json'), JSON.stringify(result, null, 2), {flag: 'wx'});
  console.log(JSON.stringify({ok: result.ok, checks, evidence, error: result.error, cleanup: result.cleanup}, null, 2));
}
