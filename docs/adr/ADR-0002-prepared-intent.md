# ADR-0002：Prepared Artifact 与 Intent 顺序

- Status: PROPOSED
- Engineering Plan: v1.1.1

## 候选决策

先准备并持久化 Artifact，再提交 Intent，随后发布目标文件，最后独立验证。SQLite 与文件系统不具备跨资源原子事务。

## 验证要求

Intent 前后、发布前后、验证前后的崩溃注入。

## 冻结条件

- 契约与实现一致。
- 真实平台测试通过。
- 必需故障注入通过。
- 记录验证证据。
- 获得必要的人工批准。

当前不代表正式批准。
