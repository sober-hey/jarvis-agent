# JARVIS Engineering Backlog

> **说明**：本文是项目内部的待办台账，部分链接指向未随本仓库公开的内部文档
> （授权记录、3D 子项目等）。看不到的条目请忽略。


> **相对路径按 2026-10-13 的布局写的，仓库重组后多数已失效。**
> `decisions/` 现为 `docs/planning/`，`subprojects/` 在仓库根，K0 设计稿在 `docs/design/`。


> 独立三维子项目增量（2026-10-17 肤唇与眉睫）：[AV-015](../subprojects/jarvis-avatar/docs/tasks/AV-015.md) 已保存外观候选及局部测试，下一步低盘发和外观细化；整体未完成，不计 K/P 验收。

> 独立三维子项目增量（2026-10-17 下颌后继）：[AV-015](../subprojects/jarvis-avatar/docs/tasks/AV-015.md) 的下颌弧线候选取得局部几何和实时证据，下一步外观材质、眉睫及发型；整体仍在进行，不改变核心任务状态。

> 独立三维子项目恢复指针（2026-10-17）：[AV-015 眼口结构](../subprojects/jarvis-avatar/docs/tasks/AV-015.md) 仍在进行；口腔随动取得局部证据，下颌刚性运动待修。本条不加入 K/P 完成计数，不改变下方核心任务状态。

> **2026-10-14 CR-01 候选索引**：路线/调度真源为 [`docs/decisions/CR-01_20261014_HERMES_PRODUCT_ROUTE.md`](../docs/decisions/CR-01_20261014_HERMES_PRODUCT_ROUTE.md)，授权为 [`docs/decisions/OWNER_AUTHORIZATION_20261014_HERMES.md`](../docs/decisions/OWNER_AUTHORIZATION_20261014_HERMES.md)。P0-P6 为并行工程里程碑候选，服务完整 v1.0；P 里程碑与下方原 K 任务分开计数、分开验收，不自动改变任何 K ID/状态。原 K 任务验收仍逐项遵守适用契约和发布门禁；P 阶段允许跨原阶段准备与有界实现，不自动计 K 验收，涉及核心执行协议仍须相关契约批准。Contract Pack 仍 `NOT_FROZEN`。
>
> P 工程里程碑候选：`P0` 基线、资源/许可/依赖与范围核验；`P1` 独立交付目录与干净源码候选；`P2` 控制服务（Python 3.12）与持久化入口准备；`P3` 派生引擎（Python 3.14 候选）窄 RPC、依赖与安全评审准备；`P4` 中文 Web 与旧 CLI 入口准备；`P5` 内部可操作样机与真实模型调用/usage 基线；`P6` 独立审查、验收与人工生产发布准备。以上均为候选/准备登记，未通过即保持 `NOT_VERIFIED`，不得写成实现、验收或生产发布。
>
> 调度边界：本轮远端数字以真实 delegation transcript `/Volumes/扩展硬盘/hermes/.hermes/cache/delegation/live/deleg_39fe2529/task-1.log` 为 SSH 取证来源；三个旧报告仅作工程背景。腾讯云本轮不创建用户/目录、不安装运行时、不部署、不清理外卖资源、不改服务。开发先在本机项目隔离；首次产品模型真调用尚未执行，基线测试待跑。研发测试无金额上限，限制无效重试并记录 usage。

- Plan: v1.1.2（继承旧任务编号与依赖，不等于新增门禁已通过）
- Status: ACTIVE
- Current Task: K0-002
- Checkpoint 2026-10-13: 内存关系夹具 v2（5b1ff01e…）独立评审/局部验收 PASS；官方门禁 140 passed。核心决定包 v2（3177cfaf…）独立复评 REWORK_REQUIRED：P1 F-NEW-EPOCH-COVERAGE。保留 K0-002 IN_PROGRESS / NOT_VERIFIED 与 Pack NOT_FROZEN。已创建并行准备卡 `planning/tasks/K0-001-v1.1.2-increment.md`，不改变当前主任务；Linux/ECS 证据保持 NOT_RUN。

## 状态定义

TODO / READY / IN_PROGRESS / IMPLEMENTED /
IN_REVIEW / VERIFIED / DONE / BLOCKED /
REWORK_REQUIRED / FAILED / CANCELED

IMPLEMENTED 不等于 VERIFIED。
没有真实测试证据不得标记 VERIFIED。
没有完整验收记录不得标记 DONE。

## K0：可靠执行内核

| ID | 任务 | 依赖 | 状态 |
|---|---|---|---|
| K0-001 | 工程初始化与环境基线 | - | VERIFIED |
| K0-002 | 领域对象与 Contract Pack | 001 | IN_PROGRESS |
| K0-003 | Task/Step 状态机与不变量 | 002 | TODO |
| K0-004 | SQLite Schema 与迁移 | 002,003 | TODO |
| K0-005 | 事务化 Task Repository | 003,004 | TODO |
| K0-006 | 确定性 Planner | 002,005 | TODO |
| K0-007 | 单执行器与 Worker Epoch | 005,006 | TODO |
| K0-008 | CLI 基础接口 | 005,007 | TODO |
| K0-009 | 路径与权限控制 | 001,002 | TODO |
| K0-010 | 输入快照与准备产物 | 006,009 | TODO |
| K0-011 | Operation Intent 与 Attempt | 005,007,010 | TODO |
| K0-012 | 原子不覆盖发布与验证 | 009,010,011 | TODO |
| K0-013 | Reconciler 与 Recovery Manager | 011,012 | TODO |
| K0-014 | 取消与紧急停止 | 007,013 | TODO |
| K0-015 | 故障注入与安全回归 | 008-014 | TODO |
| K0-016 | 日志、健康与资源限制 | 007,013 | TODO |
| K0-017 | B1/B2 备份与恢复 | 004,010,013 | TODO |
| K0-018 | 阿里云 ECS Linux 后台部署与受控恢复 | 014,016 | TODO |
| K0-019 | 长期运行与完整验收 | 015-018 | TODO |
| K0-020 | 独立评审与正式发布 | 019 | TODO |

## K1：模型驱动的受控执行

| ID | 任务 | 状态 |
|---|---|---|
| K1-001 | Model Provider Protocol | TODO |
| K1-002 | Mock Provider | TODO |
| K1-003 | 首个真实模型适配器 | TODO |
| K1-004 | Provider Capability Registry | TODO |
| K1-005 | 结构化规划器 | TODO |
| K1-006 | 计划版本与历史副作用映射 | TODO |
| K1-007 | 可信审批 | TODO |
| K1-008 | 预算预留与结算 | TODO |
| K1-009 | 上下文构建器 | TODO |
| K1-010 | Prompt Injection 安全评测 | TODO |
| K1-011 | 多步骤黄金任务 | TODO |
| K1-012 | K1 Release Candidate | TODO |

启动条件：上一阶段发布门禁通过。

## K2：记忆与人格

| ID | 任务 | 状态 |
|---|---|---|
| K2-001 | Memory Schema | TODO |
| K2-002 | Working Memory | TODO |
| K2-003 | Episodic Memory | TODO |
| K2-004 | Semantic Memory | TODO |
| K2-005 | Preference Store | TODO |
| K2-006 | FTS5 Retrieval | TODO |
| K2-007 | Provenance & Conflict | TODO |
| K2-008 | Deletion & Retention | TODO |
| K2-009 | Persona Manager | TODO |
| K2-010 | Memory & Persona Evals | TODO |
| K2-011 | K2 Release Review | TODO |

启动条件：上一阶段发布门禁通过。

## K3：有限自主

| ID | 任务 | 状态 |
|---|---|---|
| K3-001 | Event Schema | TODO |
| K3-002 | Durable Event Store | TODO |
| K3-003 | Outbox | TODO |
| K3-004 | Event Deduplication | TODO |
| K3-005 | Persistent Scheduler | TODO |
| K3-006 | Autonomy Budget | TODO |
| K3-007 | Autonomy Controls | TODO |
| K3-008 | Host Restart / Resume Recovery（云主机重启/恢复，Mac休眠仅开发测试） | TODO |
| K3-009 | K3 Security & Release | TODO |

启动条件：上一阶段发布门禁通过。

## K4：受控改进

| ID | 任务 | 状态 |
|---|---|---|
| K4-001 | Skill Manifest | TODO |
| K4-002 | Skill Registry | TODO |
| K4-003 | Version Manager | TODO |
| K4-004 | Isolated Runner | TODO |
| K4-005 | Improvement Proposal | TODO |
| K4-006 | Automated Test Runner | TODO |
| K4-007 | Regression Evaluator | TODO |
| K4-008 | Release Approval | TODO |
| K4-009 | Controlled Release | TODO |
| K4-010 | Rollback Procedure | TODO |
| K4-011 | Security Review | TODO |

启动条件：上一阶段发布门禁通过。

## K0 发布门禁

- 无未解决 P0 缺陷。
- 必需契约测试全部通过。
- 必需安全测试全部通过。
- 必需故障注入测试全部通过。
- 24 小时稳定性测试完成。
- B2 备份恢复演练成功。
- 人工批准发布。

## 任务选择规则

1. 优先处理阻断性缺陷。
2. 检查任务依赖。
3. 选择最高优先级 READY 任务。
4. 一次聚焦一个主要任务。
5. 实现后运行真实测试。
6. 完成评审并更新状态。
7. 再选择下一任务。
