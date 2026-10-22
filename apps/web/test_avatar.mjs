import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, existsSync, mkdtempSync, mkdirSync, symlinkSync, rmSync, readdirSync} from 'node:fs';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';

const sourceURL = new URL('./avatar.js', import.meta.url);
function element() {
  const values = new Map();
  const attributes = new Map();
  let text = '';
  return {
    dataset: {}, values, attributes, writes: 0,
    style: {setProperty: (key, value) => values.set(key, value), getPropertyValue: key => values.get(key) || ''},
    setAttribute: (key, value) => attributes.set(key, value),
    getAttribute: key => attributes.get(key),
    get textContent() { return text; },
    set textContent(value) { text = value; this.writes++; },
  };
}
function load(extra = {}) {
  assert.ok(existsSync(sourceURL), 'avatar renderer implementation must exist');
  const window = {addEventListener() {}};
  const context = vm.createContext({window, document: {querySelector: () => null}, ...extra});
  vm.runInContext(readFileSync(sourceURL, 'utf8'), context, {filename: 'avatar.js'});
  return context.window.JarvisAvatar;
}
function frame(overrides = {}) {
  return {
    schema_version: 1, sequence: 1, timestamp: 123, degraded: false, missing_sources: [],
    activity: 'speaking', action: 'normal',
    channels: {
      expression: {brow_raise: .1, brow_knit: .05, mouth_corner: .1, eyelid_open: .1, lip_press: .05, care: .2, focus: .1, displeasure: .05},
      posture: {lean_forward: .04, head_pitch_rad: .05, head_yaw_rad: -.05, head_roll_rad: .02},
      gaze: {yaw_rad: .1, pitch_rad: -.04, engagement: .9},
      idle: {breath_offset: .005, head_sway_rad: .01, eyelid_micro: -.01},
      mouth: {open: .7, level: .6},
    }, ...overrides,
  };
}

test('AvatarRenderer applies a signal to a replaceable figure without dialogue text', () => {
  const {AvatarRenderer} = load();
  const figure = element();
  const status = element();
  const renderer = new AvatarRenderer(figure, status);
  assert.equal(renderer.applySignal(frame()), true);
  assert.equal(figure.dataset.activity, 'speaking');
  assert.equal(figure.dataset.action, 'normal');
  assert.equal(figure.dataset.connection, 'live');
  assert.equal(figure.style.getPropertyValue('--avatar-mouth-open'), '0.7');
  assert.equal(figure.style.getPropertyValue('--avatar-gaze-yaw'), '0.1rad');
  assert.equal(figure.style.getPropertyValue('--avatar-head-pitch'), '0.05rad');
  assert.ok(figure.getAttribute('aria-label'));
  assert.ok(status.textContent.length < 40);
});

test('all contract channels are finite, clamped, and reflected without CSS injection', () => {
  const {AvatarRenderer} = load();
  const figure = element();
  const renderer = new AvatarRenderer(figure, element());
  const value = frame();
  value.channels.expression.brow_raise = 999;
  value.channels.expression.displeasure = -999;
  value.channels.posture.head_pitch_rad = 999;
  value.channels.gaze.yaw_rad = -999;
  value.channels.mouth.open = 999;
  assert.equal(renderer.applySignal(value), true);
  assert.equal(figure.values.get('--avatar-brow-raise'), '0.25');
  assert.equal(figure.values.get('--avatar-displeasure'), '0');
  assert.equal(figure.values.get('--avatar-head-pitch'), '0.1rad');
  assert.equal(figure.values.get('--avatar-gaze-yaw'), '-0.12rad');
  assert.equal(figure.values.get('--avatar-mouth-open'), '1');
  assert.equal(figure.values.size, 20);
});

test('invalid metadata, channel shapes and non-finite values fall back to neutral', () => {
  const {AvatarRenderer} = load();
  const bad = [null, {}, frame({schema_version: 2}), frame({sequence: 1.5}),
    frame({sequence: -1}), frame({timestamp: Infinity}), frame({degraded: 'false'}),
    frame({activity: 'url(evil)'}), frame({action: '<script>'}), frame({missing_sources: null}),
    frame({channels: {}})];
  for (const value of [NaN, Infinity, -Infinity, '0.5; color:red', null, undefined]) {
    const signal = frame(); signal.channels.mouth.open = value; bad.push(signal);
  }
  for (const value of bad) {
    const figure = element();
    const renderer = new AvatarRenderer(figure, element());
    renderer.applySignal(frame());
    assert.equal(renderer.applySignal(value), false);
    assert.equal(figure.dataset.activity, 'idle');
    assert.equal(figure.dataset.connection, 'disconnected');
    assert.equal(figure.values.get('--avatar-mouth-open'), '0');
    assert.equal(figure.values.get('--avatar-head-pitch'), '0rad');
    for (const property of figure.values.values()) assert.doesNotMatch(property, /NaN|Infinity|evil|script/);
  }
});

test('polite status changes only on transport/degradation/activity transitions, not every frame', () => {
  const {AvatarRenderer} = load();
  const figure = element(); const status = element();
  const renderer = new AvatarRenderer(figure, status);
  renderer.applySignal(frame());
  const writes = status.writes;
  for (let sequence = 2; sequence < 20; sequence++) renderer.applySignal(frame({sequence}));
  assert.equal(status.writes, writes);
  renderer.applySignal(frame({activity: 'thinking'}));
  assert.match(status.textContent, /思考中/);
  assert.equal(status.writes, writes + 1);
  renderer.applySignal(frame({degraded: true, missing_sources: ['affect']}));
  assert.equal(figure.dataset.connection, 'degraded');
  assert.equal(status.writes, writes + 2);
  renderer.setDisconnected(); renderer.setDisconnected();
  assert.equal(status.writes, writes + 3);
  assert.equal(figure.values.get('--avatar-mouth-level'), '0');
});

function transportFixture() {
  const events = new Map(); const timers = new Map(); const sources = [];
  let id = 0;
  class EventSource {
    constructor(url) { this.url = url; this.events = new Map(); sources.push(this); }
    addEventListener(name, fn) { this.events.set(name, fn); }
    emit(name, data) { this.events.get(name)?.({data}); }
    close() { this.closed = true; }
  }
  const window = {addEventListener(name, fn) { events.set(name, fn); }};
  const api = load({window, EventSource,
    setTimeout(fn, ms) { timers.set(++id, {fn, ms}); return id; },
    clearTimeout(id) { timers.delete(id); }});
  const figure = element(); const status = element();
  const renderer = new api.AvatarRenderer(figure, status);
  const stream = api.createAvatarStream(renderer);
  return {stream, sources, events, timers, figure, status};
}

test('stream consumes only named avatar frames, suppresses stale sequence and resets after reopen', () => {
  const f = transportFixture(); f.stream.start(); f.stream.start();
  assert.equal(f.sources.length, 1);
  const source = f.sources[0];
  assert.equal(source.url, '/api/avatar/stream');
  assert.equal(source.events.has('message'), false);
  source.emit('open');
  source.emit('avatar', JSON.stringify(frame({sequence: 5})));
  assert.equal(f.figure.dataset.activity, 'speaking');
  source.emit('avatar', JSON.stringify(frame({sequence: 4, activity: 'idle'})));
  assert.equal(f.figure.dataset.activity, 'speaking');
  source.emit('avatar', JSON.stringify(frame({sequence: 5, activity: 'thinking'})));
  assert.equal(f.figure.dataset.activity, 'speaking');
  source.emit('error');
  assert.equal(f.figure.dataset.connection, 'disconnected');
  assert.equal(f.figure.values.get('--avatar-mouth-open'), '0');
  assert.equal(source.closed, undefined, 'native EventSource owns retry');
  source.emit('open');
  source.emit('avatar', JSON.stringify(frame({sequence: 0, activity: 'thinking'})));
  assert.equal(f.figure.dataset.activity, 'thinking');
});

test('malformed or oversized events neutralize without throwing, stale timer restores neutral', () => {
  const f = transportFixture(); f.stream.start(); const source = f.sources[0];
  source.emit('avatar', JSON.stringify(frame()));
  for (const data of ['{', 'x'.repeat(20000), JSON.stringify(frame({channels: {}}))]) {
    assert.doesNotThrow(() => source.emit('avatar', data));
    assert.equal(f.figure.dataset.connection, 'disconnected');
  }
  source.emit('avatar', JSON.stringify(frame({sequence: 2})));
  assert.equal(f.figure.dataset.connection, 'live');
  assert.equal(f.timers.size, 1);
  const timer = [...f.timers.values()][0];
  assert.ok(timer.ms >= 1000 && timer.ms <= 10000);
  timer.fn();
  assert.equal(f.figure.dataset.connection, 'disconnected');
});

test('pagehide closes and clears timers; pageshow restores bfcache exactly once; stale callbacks ignored', () => {
  const f = transportFixture(); f.stream.start();
  const first = f.sources[0]; first.emit('avatar', JSON.stringify(frame()));
  f.events.get('pagehide')({persisted: true});
  assert.equal(first.closed, true);
  assert.equal(f.timers.size, 0);
  assert.equal(f.figure.dataset.connection, 'disconnected');
  first.emit('avatar', JSON.stringify(frame({sequence: 100})));
  assert.equal(f.figure.dataset.connection, 'disconnected');
  f.events.get('pageshow')({persisted: true}); f.events.get('pageshow')({persisted: true});
  assert.equal(f.sources.length, 2);
  f.sources[1].emit('avatar', JSON.stringify(frame({sequence: 0})));
  assert.equal(f.figure.dataset.connection, 'live');
  f.stream.stop();
  assert.equal(f.sources[1].closed, true);
});

test('closed admission error retries once with bounded backoff and stop cancels retry', () => {
  const f = transportFixture(); f.stream.start();
  const first = f.sources[0]; first.readyState = 2;
  first.emit('error'); first.emit('error');
  assert.equal(first.closed, true);
  assert.equal(f.timers.size, 1);
  const timer = [...f.timers.values()][0];
  assert.ok(timer.ms >= 3000 && timer.ms <= 30000);
  f.timers.clear(); timer.fn();
  assert.equal(f.sources.length, 2);
  f.sources[1].readyState = 2; f.sources[1].emit('error');
  assert.equal(f.timers.size, 1);
  f.stream.stop(); assert.equal(f.timers.size, 0);
});

test('unsupported EventSource leaves a safe neutral figure', () => {
  const {AvatarRenderer, createAvatarStream} = load(); const figure = element();
  const renderer = new AvatarRenderer(figure, element());
  assert.doesNotThrow(() => createAvatarStream(renderer).start());
  assert.equal(figure.dataset.connection, 'disconnected');
});

test('verifier refuses missing parameters and external URLs before touching evidence', () => {
  const verifier = new URL('./verify_avatar.mjs', import.meta.url);
  const run = overrides => spawnSync(process.execPath, [fileURLToPath(verifier)], {
    env: {...process.env, URL: '', EVIDENCE_DIR: '', ...overrides}, encoding: 'utf8', timeout: 10000,
  });
  assert.match(run({}).stderr, /URL and EVIDENCE_DIR are required/);
  assert.match(run({URL: 'https://example.com/', EVIDENCE_DIR: '/not-created'}).stderr, /loopback/);
});

test('verifier refuses a historical symlink ancestor before even creating nested output', () => {
  assert.ok(process.env.TMPDIR, 'tests require the configured scratch TMPDIR');
  const root = mkdtempSync(join(process.env.TMPDIR, 'avatar-guards-'));
  try {
    const historical = join(root, '.runs_01'); mkdirSync(historical);
    symlinkSync(historical, join(root, 'alias'));
    const result = spawnSync(process.execPath, [fileURLToPath(new URL('./verify_avatar.mjs', import.meta.url))], {
      env: {...process.env, URL: 'http://127.0.0.1:1/', EVIDENCE_DIR: join(root, 'alias', 'new-output')},
      encoding: 'utf8', timeout: 10000,
    });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /Historical/);
    assert.deepEqual(readdirSync(historical), [], 'guard must not mutate historical output, even via a symlink ancestor');
  } finally { rmSync(root, {recursive: true, force: true}); }
});

test('markup, local script and reduced motion CSS preserve accessible controls and no network assets', () => {
  const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
  const css = readFileSync(new URL('./styles.css', import.meta.url), 'utf8');
  assert.match(html, /src="\.\/avatar\.js" defer/);
  assert.match(html, /id="avatar-figure"[^>]*role="img"[^>]*aria-label=/);
  assert.match(html, /id="avatar-status"[^>]*role="status"[^>]*aria-live="polite"/);
  assert.match(html, /id="composer-toggle"/);
  assert.match(css, /prefers-reduced-motion:\s*reduce/);
  assert.match(css, /transition:\s*none/);
  assert.match(css, /--avatar-mouth-open/);
  assert.doesNotMatch(html + css, /https?:\/\/|@import/);
});
