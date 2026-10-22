"use strict";
(() => {
  const list = document.querySelector("#memory-list");
  const notice = document.querySelector("#memory-notice");
  const refresh = document.querySelector("#memory-refresh");
  async function load() {
    try {
      const response = await fetch("/api/memories", {cache: "no-store"});
      if (!response.ok) throw new Error("读取失败");
      const data = await response.json();
      list.replaceChildren();
      for (const fact of data.memories) {
        const article = document.createElement("article");
        const input = document.createElement("textarea");
        input.value = fact.content;
        input.maxLength = 1000;
        input.setAttribute("aria-label", "记忆内容");
        const source = document.createElement("p");
        source.textContent = `来源记录：${fact.source_message} · 版本 ${fact.version} · ${fact.created_at}`;
        const save = document.createElement("button");
        save.type = "button";
        save.textContent = "改正并保存新版本";
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "删除记忆";
        async function edit(action, payload) {
          save.disabled = remove.disabled = true;
          try {
            const result = await fetch(`/api/memories/${fact.id}/${action}`, {
              method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)
            });
            if (!result.ok) {
              const error = await result.json();
              throw new Error(error.detail || "修改失败，请刷新后再试。");
            }
            notice.textContent = action === "delete" ? "已删除，后续回答不再使用。历史聊天和外部模型留存不会被抹除。" : "已改正。后续回答使用新版本。";
            await load();
          } catch (error) {
            notice.textContent = error.message;
          } finally {
            save.disabled = remove.disabled = false;
          }
        }
        save.addEventListener("click", () => edit("correct", {content: input.value}));
        remove.addEventListener("click", () => edit("delete", {}));
        article.append(input, source, save, remove);
        list.append(article);
      }
      if (!data.memories.length) list.textContent = "暂无长期记忆。";
    } catch (_) {
      notice.textContent = "无法读取长期记忆，请检查服务连接。";
    }
  }
  refresh.addEventListener("click", load);
  load();
})();
