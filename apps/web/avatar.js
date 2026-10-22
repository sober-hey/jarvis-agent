'use strict';
(() => {
  const activities = new Set(['idle', 'thinking', 'speaking']);
  const actions = new Set(['normal', 'priority', 'chill_formal', 'defer', 'tease_answer', 'tease_only']);
  // Rig-independent wire ranges; normalized deltas are deliberately restrained.
  const fields = [
    ['expression', 'brow_raise', 'brow-raise', -.25, .25],
    ['expression', 'brow_knit', 'brow-knit', 0, .25],
    ['expression', 'mouth_corner', 'mouth-corner', -.25, .25],
    ['expression', 'eyelid_open', 'eyelid-open', -.25, .25],
    ['expression', 'lip_press', 'lip-press', 0, .25],
    ['expression', 'care', 'care', 0, .25],
    ['expression', 'focus', 'focus', 0, .25],
    ['expression', 'displeasure', 'displeasure', 0, .25],
    ['posture', 'lean_forward', 'lean', -.08, .08],
    ['posture', 'head_pitch_rad', 'head-pitch', -.10, .10, 'rad'],
    ['posture', 'head_yaw_rad', 'head-yaw', -.10, .10, 'rad'],
    ['posture', 'head_roll_rad', 'head-roll', -.08, .08, 'rad'],
    ['gaze', 'yaw_rad', 'gaze-yaw', -.12, .12, 'rad'],
    ['gaze', 'pitch_rad', 'gaze-pitch', -.08, .08, 'rad'],
    ['gaze', 'engagement', 'engagement', 0, 1],
    ['idle', 'breath_offset', 'breath', -.008, .008],
    ['idle', 'head_sway_rad', 'sway', -.015, .015, 'rad'],
    ['idle', 'eyelid_micro', 'eyelid-micro', -.025, .025],
    ['mouth', 'open', 'mouth-open', 0, 1],
    ['mouth', 'level', 'mouth-level', 0, 1],
  ];
  function normalize(signal) {
    if (!signal || signal.schema_version !== 1 || !Number.isSafeInteger(signal.sequence) ||
        signal.sequence < 0 || !Number.isFinite(signal.timestamp) || signal.timestamp < 0 ||
        typeof signal.degraded !== 'boolean' || !activities.has(signal.activity) ||
        !actions.has(signal.action) || !Array.isArray(signal.missing_sources) ||
        signal.missing_sources.length > 32 ||
        !signal.missing_sources.every(value => typeof value === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(value))) return null;
    const values = [];
    for (const [channel, key, css, minimum, maximum, unit = ''] of fields) {
      const value = signal.channels?.[channel]?.[key];
      if (typeof value !== 'number' || !Number.isFinite(value)) return null;
      values.push([css, `${Math.max(minimum, Math.min(maximum, value))}${unit}`]);
    }
    return {values, activity: signal.activity, action: signal.action,
      connection: signal.degraded || signal.missing_sources.length ? 'degraded' : 'live'};
  }

  // Thin, replaceable placeholder. A future 3D rig needs only applySignal(frame)
  // and setDisconnected(); it never receives dialogue or performs API writes.
  class AvatarRenderer {
    constructor(figure, status) {
      this.figure = figure;
      this.status = status;
      figure.setAttribute('aria-label', 'JARVIS 动态形象（占位渲染）');
      this.setDisconnected();
    }
    announce(connection, activity = 'idle') {
      const labels = {live: '形象信号已连接', degraded: '形象信号不完整', disconnected: '形象信号未连接'};
      const states = {idle: '空闲', thinking: '思考中', speaking: '正在说话'};
      const label = labels[connection] + (connection === 'disconnected' ? '' : `，${states[activity]}`);
      // Announce enum transitions only, never numeric motion or every SSE tick.
      if (this.status && this.status.textContent !== label) this.status.textContent = label;
    }
    applySignal(signal) {
      const normalized = normalize(signal);
      if (!normalized) { this.setDisconnected(); return false; }
      for (const [key, value] of normalized.values) this.figure.style.setProperty(`--avatar-${key}`, value);
      this.figure.dataset.activity = normalized.activity;
      this.figure.dataset.action = normalized.action;
      this.figure.dataset.connection = normalized.connection;
      this.announce(normalized.connection, normalized.activity);
      return true;
    }
    setDisconnected() {
      for (const [, , css, , , unit = ''] of fields) this.figure.style.setProperty(`--avatar-${css}`, `0${unit}`);
      this.figure.dataset.activity = 'idle';
      this.figure.dataset.action = 'normal';
      this.figure.dataset.connection = 'disconnected';
      this.announce('disconnected');
    }
  }

  function createAvatarStream(renderer) {
    let source = null;
    let lastSequence = -1;
    let staleTimer = null;
    let retryTimer = null;
    let retryDelay = 3000;
    const clearStale = () => { if (staleTimer !== null) clearTimeout(staleTimer); staleTimer = null; };
    const neutral = () => { clearStale(); renderer.setDisconnected(); };
    function start() {
      if (source || retryTimer !== null || typeof EventSource !== 'function') return;
      lastSequence = -1;
      try { source = new EventSource('/api/avatar/stream'); } catch { neutral(); return; }
      const current = source;
      current.addEventListener('open', () => {
        if (source !== current) return;
        retryDelay = 3000;
        lastSequence = -1;
        neutral(); // Wait for a valid frame, not just an open socket.
        staleTimer = setTimeout(neutral, 6000);
      });
      current.addEventListener('avatar', event => {
        if (source !== current) return;
        let signal;
        try {
          if (typeof event.data !== 'string' || event.data.length > 16384) throw Error('frame size');
          signal = JSON.parse(event.data);
          if (!normalize(signal)) throw Error('frame schema');
        } catch { neutral(); return; }
        if (signal.sequence <= lastSequence) return;
        if (!renderer.applySignal(signal)) { neutral(); return; }
        lastSequence = signal.sequence;
        clearStale();
        staleTimer = setTimeout(neutral, 6000);
      });
      // Native retry handles ordinary EOF. HTTP 503 admission closes the
      // EventSource permanently, so only CLOSED gets one bounded retry timer.
      current.addEventListener('error', () => {
        if (source !== current) return;
        neutral();
        if (current.readyState === 2) {
          current.close(); source = null;
          retryTimer = setTimeout(() => { retryTimer = null; start(); }, retryDelay);
          retryDelay = Math.min(retryDelay * 2, 30000);
        }
      });
    }
    function stop() {
      if (retryTimer !== null) clearTimeout(retryTimer);
      retryTimer = null;
      retryDelay = 3000;
      const previous = source;
      source = null; // Ignore late callbacks from a closed connection.
      if (previous) previous.close();
      neutral();
    }
    window.addEventListener('pagehide', stop);
    window.addEventListener('pageshow', start); // Includes back/forward cache restoration.
    return Object.freeze({start, stop});
  }
  window.JarvisAvatar = Object.freeze({AvatarRenderer, createAvatarStream});
  const figure = document.querySelector('#avatar-figure');
  if (figure) createAvatarStream(new AvatarRenderer(figure, document.querySelector('#avatar-status'))).start();
})();
