# ADR-0001：K0 单执行器互斥

- Status: PROPOSED
- Engineering Plan: v1.1.1

## 候选决策

采用 flock 作为单执行器互斥基础，结合 SQLite Worker Epoch 和 CAS 防止旧执行器提交。

## 验证要求

双进程竞争、异常退出、旧 Epoch 提交、休眠唤醒。

## 冻结条件

- 契约与实现一致。
- 真实平台测试通过。
- 必需故障注入通过。
- 记录验证证据。
- 获得必要的人工批准。

当前不代表正式批准。
