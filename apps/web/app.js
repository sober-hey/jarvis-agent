'use strict';
(() => {
  const connection = document.querySelector('#connection');
  const prompt = document.querySelector('#prompt');
  const notice = document.querySelector('#notice');
  const messages = document.querySelector('#messages');
  const toggle = document.querySelector('#composer-toggle');
  const panel = document.querySelector('#composer-panel');
  const send = document.querySelector('#send');
  const stop = document.querySelector('#stop');
  const query = document.querySelector('#query-result');
  const abandon = document.querySelector('#new-request');
  const pendingKey = 'jarvis.pending.timeline.v1';
  const oldPendingKey = 'jarvis.pending.v1';
  const idPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  const channels = {jarvis_proactive: '主动联系', web: '网页', voice: '语音', email: '邮件', imessage: 'iMessage', legacy: '旧记录'};
  let pending = null;
  let active = null;
  let storageBlocked = false;
  function controls() {
    stop.hidden = !active;
    query.hidden = !pending || !idPattern.test(pending.id) || Boolean(active);
    abandon.hidden = (!pending && !storageBlocked) || Boolean(active);
    send.disabled = Boolean(pending || active || storageBlocked);
  }
  function savePending(value) {
    const encoded = value ? JSON.stringify(value) : null;
    try {
      if (value) sessionStorage.setItem(pendingKey, encoded);
      else sessionStorage.removeItem(pendingKey);
      if (sessionStorage.getItem(pendingKey) !== encoded) throw Error('storage readback');
      if (!value) {
        sessionStorage.removeItem(oldPendingKey);
        if (sessionStorage.getItem(oldPendingKey) !== null) throw Error('storage readback');
      }
    } catch (error) {
      storageBlocked = true;
      controls();
      throw error;
    }
    pending = value;
    storageBlocked = false;
    controls();
  }
  async function json(path, options = {}) {
    const response = await fetch(path, {
      ...options, redirect: 'error', cache: 'no-store',
      signal: options.signal || AbortSignal.timeout(60000)
    });
    if (!response.ok) throw Error('服务不可用');
    const reader = response.body.getReader();
    const chunks = [];
    let count = 0;
    const maximum = path === '/api/timeline' ? 8388608 : 262144;
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      count += part.value.byteLength;
      if (count > maximum) { await reader.cancel(); throw Error('响应过大'); }
      chunks.push(part.value);
    }
    const bytes = new Uint8Array(count);
    let offset = 0;
    for (const part of chunks) { bytes.set(part, offset); offset += part.length; }
    return JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(bytes));
  }
  function draw(rows) {
    if (!Array.isArray(rows)) throw Error('记录不可读');
    const cards = [];
    for (const row of rows) {
      if (!['user', 'assistant'].includes(row.role) || typeof row.content !== 'string') continue;
      const article = document.createElement('article');
      article.className = 'message'; article.dataset.role = row.role;
      const label = document.createElement('p'); label.className = 'message-label';
      const timestamp = new Date(Number(row.timestamp) * 1000).toLocaleString('zh-CN', {hour12: false});
      label.textContent = `${row.role === 'user' ? 'Sir' : 'JARVIS'} · ${channels[row.source_channel] || '旧记录'} · ${timestamp}`;
      const body = document.createElement('p'); body.className = 'message-text';
      body.textContent = row.content;
      article.append(label, body); cards.push(article);
    }
    messages.replaceChildren(...cards);
    if (!cards.length) { const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = '暂无记录'; messages.append(empty); }
    messages.scrollTop = messages.scrollHeight;
  }
  const timelineEvents = new EventSource('/api/timeline/events');
  timelineEvents.addEventListener('timeline', event => {
    try {
      draw(JSON.parse(event.data).entries);
      connection.textContent = '已连接'; connection.dataset.connected = 'true';
    } catch { connection.dataset.connected = 'false'; }
  });
  window.addEventListener('pagehide', () => timelineEvents.close());
  async function loadTimeline() {
    try {
      const data = await json('/api/timeline'); draw(data.entries);
      connection.textContent = '已连接'; connection.dataset.connected = 'true';
    } catch {
      connection.textContent = '服务尚未连接'; connection.dataset.connected = 'false';
      notice.textContent = '暂时无法读取连续记录。';
    }
  }
  function validResult(result, id) {
    return result && result.attempt_id === id && typeof result.text === 'string' &&
      result.text.trim() && new TextEncoder().encode(result.text).length <= 131072;
  }
  async function poll(intent) {
    if (!idPattern.test(intent.id)) return;
    try {
      const result = await json('/api/attempts/' + intent.id);
      if (pending?.id !== intent.id) return;
      if (result.state === 'completed' && validResult(result.result, intent.id)) {
        savePending(null); notice.textContent = ''; await loadTimeline();
      } else notice.textContent = '结果尚未抵达，请稍后查询。';
    } catch { notice.textContent = '查询失败，稍后可继续查询。'; }
    controls();
  }
  async function submit(event) {
    event.preventDefault();
    const value = prompt.value.trim();
    if (!value || value.length > 16000 || pending || active || storageBlocked) return;
    const intent = {id: crypto.randomUUID()};
    try { savePending(intent); } catch { notice.textContent = '查询编号无法保存，尚未发送。'; return; }
    active = intent; controls();
    try {
      const body = await json('/api/timeline/chat', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({prompt: value, attempt_id: intent.id})
      });
      if (active !== intent || pending?.id !== intent.id) return;
      if (body.state) await poll(intent);
      else {
        if (!validResult(body, intent.id)) throw Error('回复身份无效');
        savePending(null); await loadTimeline();
        if (prompt.value.trim() === value) prompt.value = '';
      }
    } catch {
      if (pending?.id === intent.id) notice.textContent = '发送结果未知，请查询，不会自动重发。';
    } finally { if (active === intent) active = null; controls(); }
  }
  toggle.addEventListener('click', () => {
    panel.hidden = !panel.hidden;
    toggle.setAttribute('aria-expanded', String(!panel.hidden));
    if (!panel.hidden) prompt.focus();
  });
  document.querySelector('#composer').addEventListener('submit', submit);
  prompt.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault(); document.querySelector('#composer').requestSubmit();
    }
  });
  stop.addEventListener('click', () => {
    active = null; notice.textContent = '已停止等待；远端请求未撤回。'; controls();
  });
  query.addEventListener('click', () => { if (pending) poll(pending); });
  abandon.addEventListener('click', () => {
    if (!confirm('远端请求仍可能完成。确认放弃查询？')) return;
    try { savePending(null); notice.textContent = '已放弃查询身份；连续记录未受影响。'; }
    catch { notice.textContent = '本地记录无法清除。'; }
  });
  document.querySelector('#reconnect').addEventListener('click', loadTimeline);
  try {
    const raw = sessionStorage.getItem(pendingKey);
    const oldRaw = sessionStorage.getItem(oldPendingKey);
    if (raw || oldRaw) {
      const value = JSON.parse(raw || oldRaw);
      const id = raw ? value?.id : value?.attempt_id;
      if (typeof id !== 'string' || !idPattern.test(id)) throw Error('invalid pending');
      pending = {id};
    }
  } catch {
    storageBlocked = true;
    notice.textContent = '本地查询记录不可读；请先确认后放弃查询。';
  }
  controls(); loadTimeline();
})();
