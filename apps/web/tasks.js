'use strict';
(() => {
  const form = document.querySelector('#task-form'), file = document.querySelector('#task-file');
  const instruction = document.querySelector('#task-instruction'), processing = document.querySelector('#task-processing');
  const notice = document.querySelector('#task-notice'), list = document.querySelector('#task-list'), submit = document.querySelector('#task-submit');
  let loading = false;
  const stages = {planned:'任务已记录',snapshotted:'资料快照已保存',model_started:'真实模型处理中',model_outcome_unknown:'模型结果未知，仅可核对；不会重发',completed:'结果已保存'};
  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {cache:'no-store'} : {method:'POST', headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const value = await response.json();
    if (!response.ok) throw new Error(value.detail || value.error || '任务服务不可用');
    return value;
  }
  async function resume(id) {
    try { await api('/api/tasks/'+id+'/resume', {}); notice.textContent = '已请求恢复；已有模型调用不会重发。'; await refresh(); }
    catch (e) { notice.textContent = e.message; }
  }
  async function refresh() {
    if (loading) return;
    loading = true;
    try {
      const data = await api('/api/tasks');
      list.replaceChildren();
      for (const task of data.tasks) {
        if (!/^chain-[a-f0-9]{64}$/.test(task.task_id)) continue;
        const row = document.createElement('article'); row.className = 'task-row'; row.dataset.taskId = task.task_id; row.dataset.status = task.status;
        const title = document.createElement('strong'); title.textContent = ({summary:'摘要',translate:'翻译',extract:'结构化提取'}[task.processing] || task.processing)+' · '+task.task_id.slice(-10);
        const status = document.createElement('p'); status.textContent = (stages[task.stage] || task.stage)+' · '+task.progress+'%'+(task.active?' · 正在运行':'');
        const progress = document.createElement('progress'); progress.max=100; progress.value=task.progress; progress.setAttribute('aria-label','任务进度');
        row.append(title,status,progress);
        if (task.status === 'completed') {
          const link = document.createElement('a'); link.href='/api/tasks/'+task.task_id+'/download'; link.download=task.task_id+'.md'; link.textContent='下载结果'; row.append(link);
        } else {
          const button = document.createElement('button'); button.type='button'; button.textContent='恢复 / 核对结果'; button.disabled=task.active; button.addEventListener('click',()=>resume(task.task_id)); row.append(button);
        }
        if (task.error) { const error = document.createElement('p'); error.textContent=task.error; row.append(error); }
        list.append(row);
      }
    } catch { notice.textContent='任务服务连接中断。刷新或重新连接后会从服务器账本恢复，不会自动重复调用模型。'; }
    finally { loading=false; }
  }
  processing.addEventListener('change', () => { instruction.value = {summary:'用中文忠实摘要，保留关键数字与不确定性',translate:'翻译成英文，保留全部事实与数字',extract:'提取事实、实体、行动项，以 JSON 代码块输出；缺失信息写 null'}[processing.value]; });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const selected=file.files[0];
    if (!selected || !selected.size || selected.size>1048576) { notice.textContent='请选择非空文件，最大 1 MiB。'; return; }
    if (!instruction.value.trim()) { notice.textContent='请填写处理要求。'; return; }
    submit.disabled=true;
    try {
      let pending;
      const signature=[selected.name,selected.size,selected.lastModified,processing.value,instruction.value].join('|');
      const old=sessionStorage.getItem('jarvis.upload.pending.v1');
      if (old) { pending=JSON.parse(old); if (pending.signature!==signature) throw new Error('上次上传结果未确认。请先刷新任务列表核对；如需新任务请清除此标签页的上传暂存。'); }
      else { pending={signature,key:crypto.randomUUID()}; sessionStorage.setItem('jarvis.upload.pending.v1',JSON.stringify(pending)); }
      const encoded=await new Promise((resolve,reject)=>{ const reader=new FileReader(); reader.onerror=reject; reader.onload=()=>resolve(reader.result.split(',')[1]); reader.readAsDataURL(selected); });
      const task=await api('/api/tasks',{request_key:pending.key,filename:selected.name,content_base64:encoded,processing:processing.value,instruction:instruction.value});
      sessionStorage.removeItem('jarvis.upload.pending.v1');
      notice.textContent='资料已保存，正在启动真实模型处理。';
      await resume(task.task_id);
    } catch(e) { notice.textContent=e.message || '上传未确认，请查询现有任务，不要盲目重复提交。'; }
    finally { submit.disabled=false; await refresh(); }
  });
  refresh(); setInterval(refresh,1000);
})();
