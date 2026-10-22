# Owner Approval: Credential Broker Design Baseline

- 日期：2026-10-15
- 批准人：Sir（当前桌面 clarify 明确确认）
- 批准对象：`CRED-CAND-01` broker 候选，提升为正式设计基线
- 绑定规格：`/Volumes/扩展硬盘/jarvis/.runs_01/credential-broker-spec-20261015/SPEC.json`
- 规格状态：`DESIGN_BASELINE_APPROVED_DRAFT`
- `formal_acceptance`: `null`

## 本次批准

Sir 批准 broker 候选作为后续规格、契约拆分、独立审查和适用门禁冻结的**正式设计基线**。本批准确认设计方向，不确认实现已经存在，不确认任何运行时安全性质已经验收。

基线角色为 controller / broker / transport / worker：controller 负责可信授权、接纳、撤销、查询和 operation identity；broker 是唯一 credential 出口和固定 provider 执行者；transport 不得阻塞控制面；worker 只持有与 task、operation、attempt、epoch、payload digest、send scope、expiry、budget 和 IPC peer 绑定的 opaque handle。

基线必须保持 single dispatch、replay query-only、revoke 后停止新 admission、inflight `PENDING/UNKNOWN`、迟到结果归原 operation、crash/EOF/timeout 不 retry/fallback/换新 ID；固定 API/path，拒绝任意 URL、CONNECT、redirect、caller Authorization；receipt/usage/log 不含 secret；IPC 验证真实 peer identity 和 scope；key 生命周期、日志和 OS 边界如实分层。

## 明确未批准

本记录**不批准**：

- 任何产品实现、生产代码、测试、vendor/runtime/config 或服务器修改；
- 读取、注入、传递或使用真实 key/credential；
- 真实 provider、外网、DNS、云元数据或任何真实网络请求；
- 模型调用、真实业务任务、服务账号、生产账号或凭据轮换操作；
- Linux/OS UID、seccomp、namespace/LSM、proc、ptrace、core、swap、持久化、跨重启或 ECS 验收；
- 生产发布、生产部署、生产验收或“正式产品安全通过”；
- 适用 DP、ADR 或 Contract Pack 的自动批准、自动冻结或历史 PASS 迁移。

`formal_acceptance` 必须保持 `null`。transport 实验的 `PASS_LOCAL_EXPERIMENT` 仅是同 UID macOS 合成假 secret 私有 IPC 的局部实验结果，不是本设计的 formal PASS。

## 后续批准条件

在任何实现或真实接线之前，必须另行对精确对象完成具名批准并冻结：首批能力和 send scope、handle/expiry/budget 数值、control.db/epoch/recovery、IPC peer 身份、网络允许表、credential provider/version/轮换/撤销、Linux/OS 隔离配置、独立审查范围和正式验收集。任一条件未满足，相关动作保持 `NOT_APPROVED` 或 `NOT_RUN`。

本记录不改旧提案、旧 contracts、状态、生产代码、测试、vendor、runtime、config 或服务器。
