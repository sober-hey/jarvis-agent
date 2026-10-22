# 主动联系：本地端到端链路

此分支接线经过离线验收；未部署、未调用真实模型、未外发消息。

## 数据流

`Daemon.tick → think → outreach_intents → MindStore/outreach_intent → contact → Outreach.enqueue_intent → dispatch_intents(render_outreach) → deliveries + delivery_metadata → .eml → publish_deliveries → 同一 SessionDB 时间线 → GET /api/timeline/events`。

- 意图包含 `id/reason/evidence/urgency/needs_response`。一般主动告知只能 `needs_response=false`；提问必须走已有 question/scheduler/question_review，引用持久 `question_state`，不能直接绕过复核。
- 第二次模型调用现场组织正文。内心提示和 outward 提示分开，内部 JSON 不写公开时间线。模型失败/无效输出/重启后结果未知时，不重复生成，只确认来源记录存在，`generation_degraded=true` 同时落时间线元数据和邮件头。
- evidence 解析事件、有效记忆与当前信念；找不到或已删除/更正的记忆不交给正文模型。合成数据仍标合成，不提升为真实观察。
- `source_channel=jarvis_proactive` 区分主动发起，不创建第二个对话。SSE 推的是权威时间线快照，重连可恢复；不是平行聊天记录。

## 节制常量

| 常量 | 取值 | 理由 |
|---|---:|---|
| `QUIET_END_MINUTE` | 450 | CST 00:00–07:30 不主动打扰，所有非 emergency（含 high）延后再生成 |
| `PROACTIVE_HOURLY_LIMIT` | 2 | rolling 1 小时，不在整点翻倍；高优先级仍不该持续打断 |
| `PROACTIVE_DAILY_LIMIT` | 8 | CST 日预算，避免小时额度累积成全天骚扰 |
| `PROACTIVE_OFFLINE_LIMIT` | 1 | 每次离线只积累一条非紧急联系，重启不能刷新额度；新鲜 SSE presence 才开启在线窗口 |
| `DEDUPE_WINDOW` | 6 小时 | 送达后同一规范化原因去重；同原因仍在队列时持续去重 |

emergency 由模型按身体安危或必须立即处理的急事标记，算法绕过静默、频额、离线额及原因去重；同一 intent id 仍只处理一次，`intent_events` 留 bypass 审计。这里不声称代码能独立验证紧急分类的语义，也不保证调度器运行/模型阻塞期间的实时响应。

## 持久化与崩溃边界

- 先持久 intent 再生成。跨进程文件锁序列化调度，模型不持有 SQLite 写锁。
- 生成结果写 `generated` 日志后再提交 delivery/metadata 同一事务。重启复用结果；若只留 `rendering` 则降级，不盲重试供应商。
- `.eml` 先写隐藏临时文件并 fsync，再用不覆盖的 hard-link 发布确定性文件名，最后 fsync 目录。已有目标必须逐字节一致。进程硬杀可留隐藏临时文件，但不会发布半封最终草稿。
- 时间线写与 once receipt 中断后按 delivery key 恢复，不重写消息。不同数据库/文件并非跨资源单事务；报告只保证本轮故障注入覆盖的本地恢复性质，不声称外部 exactly-once。

## 传输边界

主动意图固定 `channel=both`，只写本地 SSE 记录与 outbox 草稿；To 默认 `sir@example.invalid`，有 `X-JARVIS-Outbox-Only: yes`。`dispatch_intents/dispatch_due` 均不调用真实 transport。`send_email(message, sender, enabled=False)` 是保留的显式 opt-in 接口；只有调用方传 `enabled=True` 并提供 sender 才能产生外部副作用，产品接线没有调用它。voice/imessage 默认仍是 fake JSON adapter，不调用平台或网络。

## 验收入口

项目 Python 从本仓库外只读引用；`TMPDIR` 必须是自有 0700 且不在宿主 Hermes 目录。全量入口仍为 packaging cwd 下 `python -m unittest discover -s . -p 'test*.py'`。引擎的八项原断言在 `load_tests` 中逐项委派给产品 Python 3.14 + `-I -S -B`，不跳过、不关产品 interpreter guard。

本轮新证据目录：主仓库 `.runs_01/outreach-e2e-20261016/`。报告列真实命令、数字、初始失败、最终结果、源码哈希与清理状态。主仓库 packaging/apps 不参与本分支写入；合并与部署由主脑另行验收。
