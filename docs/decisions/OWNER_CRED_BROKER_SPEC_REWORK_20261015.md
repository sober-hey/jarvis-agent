# Owner Decision: Credential Broker Specification Rework

- 日期：2026-10-15
- 决策人：Sir
- 决策对象：`CRED-BROKER-DESIGN-BASELINE-20261015` 的机器可执行规格返工草案
- 新返工目录：`/Volumes/扩展硬盘/jarvis/.runs_01/credential-broker-spec-rework-20261015/`
- 结论：`REWORK_REQUIRED_NOT_IMPLEMENTED`

## 决策内容

本文件只登记规格返工范围：统一 controller/broker/transport/worker 的 credential 字节边界，明确 transport 不得见 key；把先前 synthetic fake-secret transport 实验标为局部反例，不迁移到正式拓扑；统一预算字段、单位、计数点和零负规则；增加 request/grant/response/error 字段级 schema 及未知键、重复键、类型/长度/编码、审批和 `formal_acceptance` 负控；定义 `boot_id`、handle、issuer、peer、epoch、PENDING/UNKNOWN/REJECTED 状态机和 control.db 持久化要求；把 send scope/route/API/redirect/proxy/host allowlist 列为待批准字段。

修前独立复审的 22 条均在 `MATRIX.json` 保留逐条映射，20 条返工项不得静默删除。所有未批准数值和网络值保持 `null`、`NOT_APPROVED` 或 `NOT_RUN`。

## 不授权

本决策不授权任何产品实现、真实凭据读取/注入/传递、真实 provider 或网络、模型调用、control.db 部署、Linux/OS 隔离、生产、测试/vendor/runtime/config 修改或旧规格/旧批准改写。`formal_acceptance` 必须保持 `null`，实现状态必须保持 `NOT_IMPLEMENTED`。

## 验收边界

本轮只接受新目录内的机器自校验、来源哈希、ID 唯一性、schema 负控和报告计数。校验通过只表示返工包自洽，不表示实现、正式合同冻结或运行时安全性质通过。
