# K0 内核设计稿

这一组文档是内核层（`src/jarvis/`，K0–K4）的设计推演。**它们不是当前实现的规格**，
`src/jarvis/` 仍停在草案阶段，没有全部接线。读的时候先看每份文件头部的状态标记
（`DRAFT` / `PROPOSAL` / `NOT_APPROVED` / `NOT_FROZEN`）。

真正生效的契约在仓库根的 `contracts/`，不要拿这里的提案当契约用。

## 领域模型

| 文档 | 内容 |
|---|---|
| `DOMAIN_CONTRACT_DESIGN.md` | Task / Plan / Step / Attempt / Intent 的领域契约设计 |
| `PLAN_ATTEMPT_FIELD_PROPOSAL.md` | Plan / Attempt 字段提案（DRAFT / NOT_APPROVED） |
| `GUARD_CONTRACT_DESIGN.md` | Guard 契约设计（核心 Task/Step 转换） |
| `GUARD_REGISTRY_DESIGN.md` | Guard 注册表 DRAFT 建议 |
| `DATABASE_FIXTURE_DESIGN.md` | SQLite 契约夹具的证据边界设计 |

## 执行与状态机

| 文档 | 内容 |
|---|---|
| `CANCEL_PUBLICATION_PROPOSAL.md` | 取消—发布线性化候选方案（见 ADR-0006） |
| `RECONCILIATION_PAIRING_PROPOSAL.md` | Reconciliation Observation×Decision 配对来源登记 |
| `RECONCILIATION_PAIRING_RULES.md` | Reconciliation 配对与重试前提规则 |
| `RETRY_DECISION_DESIGN.md` | 重试决策（Reconciliation Decision）候选设计 |
| `STEP_TRANSITION_SOURCE_PROPOSAL.md` | Step 转换边来源提案 |
| `SNAPSHOT_OUTPUT_MAPPING_PROPOSAL.md` | 快照输出映射提案 |

## 协议与约束

| 文档 | 内容 |
|---|---|
| `FILESYSTEM_PROTOCOL.md` | 文件系统协议（路径、原子发布、目录约定） |
| `PATH_CONTRACT_PROPOSAL.md` | 四类路径来源登记（PROPOSAL，非契约） |
| `BOOTSTRAP_PROTOCOL.md` | 启动引导协议 |
| `RECOVERY_PROTOCOL.md` | 恢复协议（DRAFT） |
| `CONTRACT_COMPATIBILITY_PROPOSAL.md` | 逐契约兼容性与变更策略 |

## 追溯与吸收

| 文档 | 内容 |
|---|---|
| `TRACEABILITY_MATRIX.md` | K0 §49 DOC-R 可追溯矩阵 |
| `ABSORB_PRODUCT_MEMORY.md` | 归档 → 产品长期记忆（显式 opt-in） |

## 更早的推演

K0/K1 的逐项设计草案与逐增量评审已移到 `archive/docs-legacy/`，
那里是过程记录，不是设计依据。
