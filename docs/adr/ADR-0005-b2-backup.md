# ADR-0005：B2 冻结备份与恢复

- Status: PROPOSED
- Engineering Plan: v1.1.1

## 候选决策

备份前停止接纳新目标副作用，等待安全检查点，备份 SQLite 和必要 Artifact，生成 Manifest，并执行隔离恢复验证。

## 验证要求

备份中断、Manifest 校验、数据库完整性、Artifact 引用、恢复演练。

## 冻结条件

- 契约与实现一致。
- 真实平台测试通过。
- 必需故障注入通过。
- 记录验证证据。
- 获得必要的人工批准。

当前不代表正式批准。
