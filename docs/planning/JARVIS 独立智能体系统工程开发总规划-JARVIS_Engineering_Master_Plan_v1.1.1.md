---
document: JARVIS Engineering Master Plan
title: JARVIS 独立智能体系统工程开发总规划
version: v1.1.1
revision_date: 2026-10-12
document_type: 工程主规格书 / AI 开发执行基线
previous_version: v1.1.0
revision_basis: 《JARVIS Engineering Master Plan v1.1 第三轮技术架构评审报告》
suggested_save_path: docs/00_MASTER_PLAN.md
status: 修订完成，待契约验证与工程基线冻结
language: zh-CN
format: AI-READABLE
---

# JARVIS Engineering Master Plan v1.1.1

**JARVIS 独立智能体系统工程开发总规划**

- **版本：** v1.1.1
- **修订日期：** 2026 年 10 月 12 日
- **文档类型：** 工程主规格书 / AI 开发执行基线
- **上一版本：** v1.1.0
- **修订依据：**《JARVIS Engineering Master Plan v1.1 第三轮技术架构评审报告》
- **建议保存路径：** `docs/00_MASTER_PLAN.md`

**文档状态：修订完成，待契约验证与工程基线冻结。**

本版本是对 v1.1 的正式修订，不是另起炉灶的架构设计。

v1.1 的总体架构、技术路线、K0—K4 阶段划分和 AI 开发管理体系继续保留。v1.1.1 重点修复第三轮评审发现的执行语义、数据库约束、文件系统安全、故障恢复、取消仲裁和备份一致性问题。

本规划面向两类读者：

1. **项目负责人：** 用于管理目标、控制风险、审批变更、验收成果和复盘开发。
2. **AI 编程代理：** 用于理解架构、领取任务、编写代码、执行测试、报告证据和交付版本。

本文件中的 **MUST、MUST NOT、SHOULD** 分别表示：

- **MUST：** 强制要求。
- **MUST NOT：** 禁止事项。
- **SHOULD：** 默认应当遵守，例外需要记录理由。

**重要边界：本文件是开发规格，不是已经实现、测试或获得生产认证的软件。任何未经过真实测试的功能，不得标记为已验证。**

---

## 第一部分：项目目标与工程原则

### 1. 项目定位

JARVIS 是一个独立开发的、长期运行的个人 AI 智能体系统。

它不以任何单一大语言模型或第三方 Agent 框架作为核心控制系统。

JARVIS 的控制权属于自身的确定性执行内核，而不是模型。

其长期目标是具备：

- 持久化任务管理。
- 可恢复的多步骤执行。
- 可替换的大语言模型。
- 严格的工具权限管理。
- 长期记忆与一致人格。
- 事件驱动的有限自主行为。
- 可观察、可暂停、可停止的运行机制。
- 受控的技能创建和系统改进。
- 可测试、可复盘、可发布、可回滚的工程体系。

#### 1.1 核心定位

JARVIS 不是一个持续运行的模型对话循环。

它应当是：

> **由确定性控制内核管理任务生命周期，由认知系统提出建议，由受控执行系统产生副作用，并通过持久化证据验证执行结果的个人智能体平台。**

#### 1.2 最高设计原则

按照优先级排序：

1. **安全性：** 未授权操作不得执行。
2. **可恢复性：** 进程异常退出后能够识别未完成工作。
3. **正确性：** 任务状态必须与可验证事实一致。
4. **可追溯性：** 重要状态变化和副作用必须有证据。
5. **可控制性：** 用户能够取消任务和阻止新操作。
6. **可维护性：** 模块职责清晰，避免重复实现。
7. **可扩展性：** 后续模型、记忆和自主功能不得绕过内核。
8. **性能与成本：** 在满足前述条件后进行优化。

当性能、便利性与安全约束冲突时，默认优先安全约束。

---

## 第二部分：项目范围与阶段边界

### 2. 阶段划分

| 阶段 | 名称 | 核心目标 | 正式交付 |
|---|---|---|---|
| K0 | Reliable Kernel | 可靠执行内核 | 可恢复的确定性任务系统 |
| K1 | Cognitive Execution | 模型驱动的受控任务执行 | 模型网关、规划、审批和预算 |
| K2 | Memory & Persona | 长期记忆与人格一致性 | 可追溯、可评测的认知记忆 |
| K3 | Controlled Autonomy | 事件驱动有限自主 | 可限额、可停止的主动任务 |
| K4 | Controlled Improvement | 受控技能与系统改进 | 隔离测试、审批、发布和回滚 |

K0 是其他阶段的基础。

K1—K4 **MUST** 复用 K0 的状态机、执行记录、权限系统、恢复机制和验证流程。

不得为模型工具调用建立第二套绕过 K0 的执行通道。

#### 2.1 K0 明确不做什么

K0 不实现：

- 真实大语言模型调用。
- 任意 Shell 命令执行。
- 浏览器自动操作。
- 邮件发送。
- 外部 API 写入。
- 长期记忆。
- 主动任务生成。
- 自我修改。
- 分布式调度。
- 多执行器并发执行。
- 通用 DAG 工作流引擎。

K0 只支持一个正式任务类型：

```text
inventory.generate
```

K0 只允许：

```text
1 个活动执行器
1 个活动任务步骤
1 个确定性计划
每个步骤最多 1 个逻辑外部副作用
```

这不是对未来能力的限制，而是第一阶段的可靠性边界。

---

## 第三部分：系统总体架构

### 3. 架构总览

```text
┌───────────────────────────────────────────────┐
│                 User Interface                │
│          CLI / UI / Voice / API               │
└───────────────────────┬───────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────┐
│                 Control Plane                 │
│                                               │
│  Task Intake                                  │
│  State Machine                                │
│  Scheduler                                    │
│  Execution Coordinator                        │
│  Cancellation / Emergency Stop                │
│  Recovery Manager                             │
│  Policy & Budget Enforcement                  │
└──────────────┬──────────────────┬─────────────┘
               │                  │
               ▼                  ▼
┌────────────────────────┐ ┌────────────────────┐
│    Cognitive Plane     │ │   Execution Plane  │
│                        │ │                    │
│ Model Gateway          │ │ Tool Registry      │
│ Planner                │ │ Safe File Tools    │
│ Context Builder        │ │ Operation Intent   │
│ Memory Retrieval       │ │ Attempt Manager    │
│ Persona                │ │ Reconciler         │
│ Verification Planning  │ │ Result Verifier    │
└────────────────────────┘ └────────────────────┘
               │                  │
               └────────┬─────────┘
                        ▼
┌───────────────────────────────────────────────┐
│                Persistent State               │
│                                               │
│ SQLite                                        │
│ Task / Plan / Step / Attempt / Intent         │
│ Event Log                                     │
│ Artifact Manifest                             │
│ Approval / Model Call / Budget                │
│ Memory / Skill Registry                       │
└───────────────────────────────────────────────┘

Cross-cutting:
Security / Observability / Testing / Release
```

#### 3.1 模块边界

| 模块 | 可以做什么 | 不可以做什么 |
|---|---|---|
| Control Plane | 决定状态转换、调度和恢复 | 将模型输出直接视为授权 |
| Cognitive Plane | 理解目标、提出计划、生成候选内容 | 直接修改任务数据库状态 |
| Security | 评估权限和审批 | 根据模型自述授予权限 |
| Execution | 执行经过授权的操作 | 自行扩大权限 |
| Storage | 提供事务和持久化 | 自行决定业务流程 |
| Reconciler | 观察实际副作用并提出恢复决策 | 在证据不足时声称操作未发生 |
| Verifier | 验证结果是否符合契约 | 仅凭模型回答认定任务完成 |
| Release Manager | 执行经过批准的部署 | 自行批准高风险更新 |

#### 3.2 技术基线

| 领域 | 默认技术 |
|---|---|
| 开发语言 | Python 3.12 |
| 包管理 | uv |
| 数据库 | SQLite |
| 数据验证 | Pydantic |
| CLI | Typer |
| 测试 | pytest |
| 代码检查 | Ruff |
| 类型检查 | Pyright |
| HTTP 客户端，K1 起 | HTTPX |
| macOS 服务管理 | launchd |
| 记忆检索，K2 起 | SQLite FTS5 |
| 密钥管理 | macOS Keychain |
| 版本管理 | Git |
| 结构化日志 | JSON |
| 追踪扩展 | OpenTelemetry，按需引入 |

不允许为了技术先进性而提前引入：

- Kubernetes。
- Redis。
- Kafka。
- 微服务集群。
- 分布式数据库。
- 复杂多 Agent 框架。

除非后续 ADR 证明其必要性。

---

## 第四部分：系统不可违反的不变量

### 4. 全局不变量

以下规则贯穿 K0—K4。

| ID | 不变量 |
|---|---|
| INV-001 | 没有有效授权，不允许执行工具副作用 |
| INV-002 | 没有持久化 Operation Intent，不允许执行对应目标副作用 |
| INV-003 | 一个逻辑副作用具有稳定且唯一的 Operation Key |
| INV-004 | 同一逻辑操作的重试不得改变原始操作意图 |
| INV-005 | 外部操作结果未知时，不得盲目重试 |
| INV-006 | 任务成功必须有独立验证证据 |
| INV-007 | 任务状态、步骤状态和相关事件必须事务一致 |
| INV-008 | 同一时刻最多一个 K0 执行器持有有效执行权 |
| INV-009 | 旧执行器不得提交过期执行结果 |
| INV-010 | 取消请求提交后不得启动新的目标副作用 |
| INV-011 | 紧急停止状态必须持久化 |
| INV-012 | 模型不得授予自身权限 |
| INV-013 | 模型不得自行批准高风险操作 |
| INV-014 | 自我改进不得绕过测试和发布门禁 |
| INV-015 | 已发生的副作用不得因重新规划而被当作不存在 |
| INV-016 | 未完成或未核对操作所需的恢复产物不得被清理 |
| INV-017 | 重要记忆必须保留来源和有效性信息 |
| INV-018 | 未执行的测试不得被标记为通过 |

#### 4.1 明确不承诺的能力

JARVIS v1.1.1 不承诺：

- 所有外部操作都能做到 Exactly Once。
- SQLite 与文件系统之间存在跨资源原子事务。
- 普通目录复制能够提供全局时间点快照。
- `flock` 能阻止不遵守锁协议的其他程序。
- 文件权限能够抵御同一 macOS 用户身份下的所有恶意进程。
- `launchd` 能保证 Mac 休眠或关机时继续执行任务。
- 通过有限次数测试就能证明系统永远不会失败。

这些边界 **MUST** 写入正式文档，不能在产品说明中被夸大。

---

## 第五部分：核心领域对象

### 5. 五个核心对象

#### 5.1 Task

Task 表示用户希望完成的目标。

它负责：

- 用户请求。
- 任务身份。
- 当前任务状态。
- 取消请求。
- 当前活动计划。
- 总体执行结果。
- 创建、完成和失败时间。

Task 不直接描述每次工具执行的细节。

#### 5.2 Plan

Plan 表示完成任务的具体方案。

它负责：

- 计划版本。
- 所属 Task。
- 步骤集合。
- 计划创建依据。
- 计划激活状态。
- 历史计划保留。

一个 Task 可以拥有多个历史 Plan。

**同一 Task 同一时间最多有一个活动 Plan。**

K0 只有一个确定性 Plan。

#### 5.3 Step

Step 是计划中的最小调度单位。

它负责：

- 步骤输入。
- 步骤类型。
- 前置条件。
- 执行状态。
- 预期结果。
- 验证要求。

K0 规定：

> 一个 Step 最多包含一个逻辑目标副作用。

K1 以后，如果任务需要多个外部副作用，原则上拆成多个 Step。

#### 5.4 Attempt

Attempt 是一次实际执行尝试。

它负责：

- 第几次尝试。
- 哪个执行器执行。
- 执行器 Epoch。
- 开始和结束时间。
- 实际执行结果。
- 错误信息。
- 中断和恢复证据。

同一个逻辑操作可以有多个 Attempt。

但多个 Attempt **MUST** 复用相同的 Operation Intent。

#### 5.5 Operation Intent

Operation Intent 是已经确定、准备执行的逻辑副作用。

它负责：

- 逻辑操作身份。
- 工具名称和版本。
- 操作类型。
- 规范化参数摘要。
- 预期结果。
- 恢复所需的稳定证据。
- 当前核对状态。

Operation Intent 不等于 Attempt。

**Intent 描述要执行什么；Attempt 描述某次实际尝试。**

---

## 第六部分：任务身份与幂等语义

### 6. TaskSpec

K0 正式请求格式：

```json
{
  "schema_version": 1,
  "request_key": "inventory-example-001",
  "task_kind": "inventory.generate",
  "input": {
    "root": "/absolute/authorized/input/path"
  },
  "output": {
    "name": "inventory.json"
  }
}
```

#### 6.1 request_key

`request_key` 由调用者提供。

规则：

1. 相同 `request_key`、相同规范化任务内容：返回原 Task。
2. 相同 `request_key`、不同规范化任务内容：返回 `REQUEST_CONFLICT`。
3. 不得创建第二个 Task。
4. 重复请求返回数据库中的真实当前状态。

#### 6.2 请求摘要

必须对规范化任务内容计算 SHA-256。

规范化规则：

- 固定字段集合。
- 固定 JSON 编码。
- 字典键排序。
- 禁止 NaN 和 Infinity。
- 不接受未定义字段。
- 路径先完成明确的格式验证。
- `request_key` 不参与任务内容摘要。
- 不包含当前时间或随机值。

建议在契约中明确使用：

```python
json.dumps(
    payload,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
)
```

其前提是所有输入类型和字符串处理规则已经由 Schema 固定。

不同语言之间的完全一致性不在 K0 范围内。

#### 6.3 Operation Key

K0：

```text
operation_key = stable_identifier(
    task_id,
    logical_effect_id
)
```

`logical_effect_id` 在创建确定性计划时生成并持久化。

它不能由每次 Attempt 重新生成。

K1 重新规划时：

- 已经存在的逻辑副作用继续使用原 Operation Key。
- 新的逻辑副作用使用新的 Operation Key。
- 无法确定是否属于同一副作用时，不允许自动重用或重新执行。

---

## 第七部分：正式任务状态机

### 7. 状态定义

#### K0 使用的状态

```text
CREATED
READY
RUNNING
VERIFYING
NEEDS_RECONCILIATION
RETRY_WAIT
BLOCKED
SUCCEEDED
FAILED
CANCELED
```

#### K1 扩展状态

```text
PLANNING
WAITING_APPROVAL
PAUSED
```

K0 不需要实现 K1 的全部行为，但数据库和契约必须明确版本兼容策略。

#### 7.1 状态含义

| 状态 | 含义 |
|---|---|
| CREATED | 请求已持久化，尚未准备完成 |
| PLANNING | 正在生成或验证计划 |
| READY | 可以领取执行 |
| WAITING_APPROVAL | 等待可信用户审批 |
| RUNNING | 存在活动执行工作 |
| VERIFYING | 正在核对预期结果 |
| NEEDS_RECONCILIATION | 结果需要与真实外部状态核对 |
| RETRY_WAIT | 满足重试条件，等待下一次尝试 |
| BLOCKED | 需要人工决策或外部条件恢复 |
| PAUSED | 受控暂停，尚未进入终态 |
| SUCCEEDED | 所有必需步骤通过验证 |
| FAILED | 已确认无法完成 |
| CANCELED | 取消流程已安全结束 |

终态：

```text
SUCCEEDED
FAILED
CANCELED
```

终态不得重新打开。

需要重新执行时创建新的 Task。

#### 7.2 K0 允许的任务状态转换

| 当前状态 | 允许转入 |
|---|---|
| CREATED | READY、FAILED、CANCELED |
| READY | RUNNING、BLOCKED、FAILED、CANCELED |
| RUNNING | VERIFYING、NEEDS_RECONCILIATION、RETRY_WAIT、BLOCKED、FAILED、CANCELED |
| VERIFYING | SUCCEEDED、NEEDS_RECONCILIATION、RETRY_WAIT、BLOCKED、FAILED、CANCELED |
| NEEDS_RECONCILIATION | VERIFYING、READY、BLOCKED、FAILED、CANCELED |
| RETRY_WAIT | READY、BLOCKED、FAILED、CANCELED |
| BLOCKED | READY、FAILED、CANCELED |
| SUCCEEDED | 无 |
| FAILED | 无 |
| CANCELED | 无 |

**注意：表中允许转换不代表任何情况下都可以转换。**

每条转换还必须满足 Guard 条件。

例如：

```text
RUNNING → FAILED
```

只有在不存在未核对的危险副作用时才允许。

如果外部操作结果未知，必须先进入：

```text
NEEDS_RECONCILIATION
```

#### 7.3 K1 扩展转换

```text
CREATED → PLANNING

PLANNING → READY
PLANNING → WAITING_APPROVAL
PLANNING → BLOCKED
PLANNING → FAILED
PLANNING → CANCELED

READY → WAITING_APPROVAL

WAITING_APPROVAL → READY
WAITING_APPROVAL → BLOCKED
WAITING_APPROVAL → FAILED
WAITING_APPROVAL → CANCELED
```

`PAUSED` 的完整转换表在 K1 专项契约中冻结。

在该契约完成前，不能把 `PAUSED` 当作已实现功能。

---

## 第八部分：Step 状态机与跨对象约束

### 8. Step 状态

K0 使用：

```text
PENDING
CLAIMED
PREPARING
EXECUTING
VERIFYING
NEEDS_RECONCILIATION
RETRY_WAIT
BLOCKED
COMPLETED
FAILED
CANCELED
```

#### 8.1 正常路径

```text
PENDING
   ↓
CLAIMED
   ↓
PREPARING
   ↓
EXECUTING
   ↓
VERIFYING
   ↓
COMPLETED
```

`EXECUTING` 只能在对应 Operation Intent 已经持久化之后进入。

#### 8.2 异常路径

```text
EXECUTING
   ↓
NEEDS_RECONCILIATION
   ↓
观察实际结果
   ├── 结果符合预期 → VERIFYING
   ├── 可以安全重试 → RETRY_WAIT
   ├── 存在冲突 → BLOCKED
   └── 已确认不可恢复 → FAILED
```

`PREPARING` 期间崩溃时，如果尚未提交 Intent，恢复程序只处理未完成准备工作和孤儿产物，不得假定已经发生目标副作用。

#### 8.3 Step 转换约束

- `PENDING → CLAIMED`：必须成功取得任务执行权。
- `CLAIMED → PREPARING`：必须通过前置条件检查。
- `PREPARING → EXECUTING`：必须已提交有效 Intent。
- `EXECUTING → VERIFYING`：必须已有可核对的执行结果。
- `VERIFYING → COMPLETED`：必须通过独立验证。
- `NEEDS_RECONCILIATION → RETRY_WAIT`：必须证明重试安全。
- `BLOCKED → PENDING`：必须有明确解除阻塞的授权和理由。
- 任意非终态进入 `FAILED` 或 `CANCELED`：不得遗留未受控的活动副作用。

#### 8.4 跨对象不变量

**STATE-001**

Task 为 `SUCCEEDED` 时，活动 Plan 的全部必需 Step 必须为 `COMPLETED`。

**STATE-002**

Step 为 `COMPLETED` 时，相关 Intent 必须已验证，且存在验证证据。

**STATE-003**

同一 Step 同一时间最多一个活动 Attempt。

**STATE-004**

Task 为终态时，不允许存在仍可继续执行的活动 Attempt。

**STATE-005**

Task 为 `CANCELED` 不代表外部副作用一定没有发生。

**STATE-006**

Task 为 `FAILED` 不代表系统已经自动撤销历史副作用。

**STATE-007**

状态变化与审计事件必须在同一个 SQLite 事务中提交。

**STATE-008**

任何状态转换必须通过统一 State Transition Service。

**STATE-009**

不能通过直接修改数据库字段绕过转换 Guard。

**STATE-010**

Plan 切换不得删除旧 Plan 的执行历史。

---

## 第九部分：取消与紧急停止

### 9. 取消请求不是任务状态

Task 必须持久化：

```text
cancel_requested_at_ms
cancel_requested_by
cancel_reason
```

取消请求可以与当前执行状态并存。

例如：

```text
state = VERIFYING
cancel_requested_at_ms = 1790000000000
```

#### 9.1 取消仲裁

K0 采用：

**谁先完成数据库提交，谁先获得状态决定权。**

#### 成功先提交

如果：

```text
SUCCEEDED 事务先提交
```

之后收到取消请求：

- 返回任务已经完成。
- 不改变终态。
- 不创建新的取消状态。

#### 取消先提交

如果：

```text
cancel_requested_at_ms 先持久化
```

则：

1. 不再启动新的目标副作用。
2. 已经开始的操作进入安全检查点。
3. 必须核对可能已经发生的副作用。
4. 允许只读验证和恢复诊断。
5. 完成安全核对后，默认进入 `CANCELED`。
6. 已发生的副作用必须保留在执行记录中。

最终成功提交必须使用带条件的状态更新，确保取消请求尚不存在。

#### 9.2 取消不能直接杀死数据一致性

不允许：

```text
收到取消
    ↓
直接把 RUNNING 改为 CANCELED
    ↓
忽略正在执行的文件操作
```

正确流程：

```text
收到取消
    ↓
持久化取消请求
    ↓
停止新副作用
    ↓
等待或中断到安全检查点
    ↓
核对已开始的操作
    ↓
提交 CANCELED 与事件
```

#### 9.3 紧急停止

紧急停止必须：

- 不依赖模型。
- 持久化。
- 禁止新任务领取。
- 禁止新目标副作用。
- 尽可能停止活动执行。
- 允许只读诊断。
- 允许必要的安全核对。
- 保留未解决操作。

紧急停止重置必须由可信用户操作。

**重置停止标志不等于自动恢复全部任务。**

恢复前必须先检查未完成 Intent。

---

## 第十部分：SQLite 持久化规范

### 10. 数据库配置

K0 使用 SQLite。

每个连接必须启用：

```sql
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

数据库初始化时配置：

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
```

要求：

- 使用短事务。
- 不在数据库事务中等待模型或外部工具。
- 不在 Git 仓库中存放运行数据库。
- 不在未经验证的云同步目录中运行活动数据库。
- 使用 Schema 迁移。
- 使用数据库完整性检查。
- 正式备份采用 SQLite 支持的一致性备份方式。

#### 10.1 K0 核心数据表

正式核心表：

```text
tasks
task_plans
task_steps
task_events
operation_intents
file_write_intents
step_attempts
system_control
artifacts
```

数据库 Schema 必须由迁移文件管理。

下面给出 v1.1.1 的关键关系基线；实际迁移还必须补充完整 CHECK、索引、时间字段和迁移测试。

#### tasks

```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,

    request_key TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,

    task_kind TEXT NOT NULL,
    state TEXT NOT NULL,

    active_plan_id TEXT,

    cancel_requested_at_ms INTEGER,
    cancel_requested_by TEXT,
    cancel_reason TEXT,

    version INTEGER NOT NULL DEFAULT 0,

    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,

    FOREIGN KEY (active_plan_id, id)
        REFERENCES task_plans(id, task_id)
);
```

#### task_plans

```sql
CREATE TABLE task_plans (
    id TEXT PRIMARY KEY,

    task_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    state TEXT NOT NULL,

    plan_digest TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,

    FOREIGN KEY (task_id)
        REFERENCES tasks(id),

    UNIQUE(task_id, plan_version),
    UNIQUE(id, task_id)
);
```

同一 Task 只能有一个活动 Plan：

```sql
CREATE UNIQUE INDEX ux_active_plan_per_task
ON task_plans(task_id)
WHERE state = 'ACTIVE';
```

#### task_steps

```sql
CREATE TABLE task_steps (
    id TEXT PRIMARY KEY,

    task_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,

    ordinal INTEGER NOT NULL,
    logical_effect_id TEXT,

    state TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1,

    version INTEGER NOT NULL DEFAULT 0,

    FOREIGN KEY (plan_id, task_id)
        REFERENCES task_plans(id, task_id),

    UNIQUE(plan_id, ordinal),
    UNIQUE(id, task_id)
);
```

组合外键确保：

> Step 所属的 Plan 必须属于同一个 Task。

#### operation_intents

```sql
CREATE TABLE operation_intents (
    id TEXT PRIMARY KEY,

    step_id TEXT NOT NULL UNIQUE,
    operation_key TEXT NOT NULL UNIQUE,

    operation_type TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,

    arguments_digest TEXT NOT NULL,
    state TEXT NOT NULL,

    created_at_ms INTEGER NOT NULL,

    FOREIGN KEY (step_id)
        REFERENCES task_steps(id),

    UNIQUE(id, step_id)
);
```

K0 保持：

```text
每个 Step 最多一个 Operation Intent
```

#### file_write_intents

```sql
CREATE TABLE file_write_intents (
    operation_id TEXT PRIMARY KEY,

    input_snapshot_sha256 TEXT NOT NULL,

    prepared_relpath TEXT NOT NULL,
    target_relpath TEXT NOT NULL,

    expected_sha256 TEXT NOT NULL,
    expected_size INTEGER NOT NULL,

    FOREIGN KEY (operation_id)
        REFERENCES operation_intents(id),

    CHECK(expected_size >= 0)
);
```

这样通用 Intent 不再强制包含文件写入专属字段。

#### step_attempts

```sql
CREATE TABLE step_attempts (
    id TEXT PRIMARY KEY,

    step_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,

    attempt_no INTEGER NOT NULL,
    worker_epoch INTEGER NOT NULL,

    state TEXT NOT NULL,

    started_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,

    FOREIGN KEY (step_id)
        REFERENCES task_steps(id),

    FOREIGN KEY (operation_id, step_id)
        REFERENCES operation_intents(id, step_id),

    UNIQUE(step_id, attempt_no)
);
```

禁止两个活动 Attempt：

```sql
CREATE UNIQUE INDEX ux_active_attempt_per_step
ON step_attempts(step_id)
WHERE state = 'ACTIVE';
```

恢复旧 Attempt 时，必须先将其转换为已中断或待核对状态，再创建新的 Attempt。

#### task_events

```sql
CREATE TABLE task_events (
    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,

    task_id TEXT NOT NULL,
    step_id TEXT,

    event_type TEXT NOT NULL,

    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,

    correlation_id TEXT,
    payload_json TEXT NOT NULL,

    created_at_ms INTEGER NOT NULL,

    FOREIGN KEY (task_id)
        REFERENCES tasks(id),

    FOREIGN KEY (step_id, task_id)
        REFERENCES task_steps(id, task_id)
);
```

使用数据库序号确定事件提交顺序。

不得只依赖毫秒时间戳排序。

#### system_control

至少记录：

```text
singleton_id
emergency_stop
backup_mode
worker_epoch
updated_at_ms
```

系统控制状态必须通过事务更新。

#### artifacts

至少记录：

```text
artifact_id
task_id
artifact_kind
relative_path
sha256
size_bytes
lifecycle_state
created_at_ms
retention_class
```

其中：

```text
artifact_kind:
    INPUT_SNAPSHOT
    SNAPSHOT_MANIFEST
    PREPARED_OUTPUT
    FINAL_OUTPUT
    VERIFICATION_EVIDENCE
```

#### 10.2 数据库实现规则

所有表必须增加适当的：

- `NOT NULL`。
- `CHECK`。
- `UNIQUE`。
- `FOREIGN KEY`。
- 必要索引。
- Schema 版本管理。

状态枚举由机器可读契约生成或验证，不允许数据库、Python 和 YAML 各自维护互相矛盾的状态集合。

#### 10.3 事务化状态转换

状态转换必须采用乐观并发控制。

示意：

```sql
UPDATE tasks
SET state = :new_state,
    version = version + 1,
    updated_at_ms = :now
WHERE id = :task_id
  AND state = :expected_state
  AND version = :expected_version;
```

如果更新行数不是 1：

```text
VERSION_CONFLICT
```

必须在同一事务中：

1. 验证状态转换。
2. 更新任务或步骤。
3. 更新相关 Intent 或 Attempt。
4. 写入审计事件。
5. 提交。

**任何跨对象不变量都不能只靠应用层的非事务性先查后改实现。**

---

## 第十一部分：K0 正式黄金任务

### 11. `inventory.generate`

K0 唯一正式黄金任务为：

```text
inventory.generate
```

目标：

对用户明确授权的输入目录建立受控快照，生成确定性的文件清单，并将结果发布到 JARVIS 管理的输出目录。

#### 11.1 输入范围

K0 默认限制：

| 参数 | 默认限制 |
|---|---|
| 最大输入文件数 | 1,000 |
| 最大输入总大小 | 20 MiB |
| 最大单文件大小 | 20 MiB |
| 最大目录深度 | 16 |
| 最大并发任务数 | 1 |
| 最大活动执行器数 | 1 |
| 符号链接 | 默认拒绝 |
| 特殊文件 | 拒绝 |
| 无法安全编码的文件名 | 拒绝 |

这些是初期工程默认值，可通过受控配置调整，但必须有对应测试。

不得由模型自行提高限制。

#### 11.2 输出格式

```json
{
  "schema_version": 1,
  "files": [
    {
      "path": "a.txt",
      "size_bytes": 12,
      "sha256": "<actual_sha256>"
    }
  ],
  "file_count": 1,
  "total_bytes": 12
}
```

`<actual_sha256>` 是说明性占位符，正式输出必须使用真实哈希。

#### 确定性规则

1. 只包含普通文件。
2. 默认包含隐藏普通文件。
3. 默认拒绝符号链接。
4. 路径相对于快照根目录。
5. 路径分隔符统一为 `/`。
6. 按相对路径的 UTF-8 字节序排序。
7. 文件大小使用实际字节数。
8. 文件内容使用 SHA-256。
9. 输出不包含当前时间和随机数。
10. 相同快照必须生成完全相同的输出字节。
11. 不允许重复路径。
12. JSON 编码规则必须固定。
13. 不支持的文件名必须明确报错，不得静默改名。

#### 11.3 K0 任务执行模型

K0 使用：

```text
1 Task
   └── 1 Plan
         └── 1 Step
               └── 1 FileWriteIntent
                     └── 1..N Attempts
```

这个 Step 内部包含：

```text
输入快照
    ↓
清单生成
    ↓
准备产物
    ↓
持久化 Intent
    ↓
发布文件
    ↓
结果核对
    ↓
独立验证
```

快照和准备产物属于**受控内部准备阶段**。

最终清单文件发布属于需要 Operation Intent 保护的**目标副作用**。

K0 不额外创建三个业务 Step，以避免不必要的状态复杂度。

---

## 第十二部分：输入快照协议

### 12. 快照一致性保证

v1.1.1 明确：

**K0 保证已提交快照中的文件内容具有确定的字节序列和哈希，但不保证整个源目录是同一时刻的原子快照。**

这是本次修订的重要边界。

#### 12.1 信任假设

K0 假设：

- 输入目录由可信用户授权。
- 系统需要防范普通并发修改和意外文件变化。
- 系统不承诺抵御具有同一 macOS 用户身份的恶意进程。
- 系统不承诺对恶意并发修改提供文件系统级事务隔离。

如果未来任务要求多个文件在同一业务时间点一致，必须采用额外的一致性机制。

#### 12.2 安全读取流程

```text
验证授权输入根目录
       ↓
打开受控目录文件描述符
       ↓
逐层安全遍历
       ↓
拒绝符号链接和特殊文件
       ↓
检查文件类型、大小和限制
       ↓
通过受控文件描述符读取
       ↓
复制到私有快照文件
       ↓
计算 SHA-256
       ↓
检查读取前后文件状态
       ↓
持久化快照文件
       ↓
生成 Snapshot Manifest
       ↓
验证 Manifest
       ↓
提交快照记录
```

#### 12.3 路径安全要求

不能仅使用：

```python
Path.resolve()
```

然后假定后续文件访问一定安全。

因为检查与打开之间可能发生路径替换。

K0 应优先使用：

- 目录文件描述符。
- 相对目录文件描述符的打开操作。
- `O_NOFOLLOW`。
- `O_DIRECTORY`。
- `fstat()`。
- 受控的逐层路径遍历。

所有依赖 macOS/Python 平台能力的操作，必须先进行运行时能力检测和测试。

如果目标平台不支持所需的安全访问语义：

**必须拒绝执行，而不是静默退化为不安全的路径操作。**

#### 12.4 快照变化处理

如果读取期间发现：

- 文件被替换。
- 文件被删除。
- 文件类型变化。
- 文件大小超限。
- 文件内容或相关元数据发生变化。
- 目录结构出现无法安全解释的变化。

则：

1. 丢弃本次未完成快照。
2. 记录错误。
3. 在规定次数内重新尝试。
4. 超过次数后失败或阻塞。
5. 不允许提交不完整 Manifest。

初期建议：

```text
max_snapshot_retries = 2
```

这不构成对全目录原子一致性的保证。

#### 12.5 快照提交

快照 Manifest 至少包含：

```json
{
  "schema_version": 1,
  "snapshot_id": "<snapshot_id>",
  "files": [
    {
      "path": "a.txt",
      "size_bytes": 12,
      "sha256": "<actual_sha256>"
    }
  ],
  "file_count": 1,
  "total_bytes": 12
}
```

`Snapshot Manifest` 的规范化字节序列必须具有稳定哈希。

只有在以下条件全部满足时，快照才可以标记为有效：

- 所有文件复制完成。
- 所有文件哈希验证通过。
- Manifest 写入完成。
- 必要的持久化操作完成。
- 数据库引用提交成功。

如果数据库尚未引用某个准备快照，该快照属于可识别的孤儿产物。

---

## 第十三部分：准备产物与 Operation Intent

### 13. 准备产物生命周期

正式生命周期：

```text
STAGING
   ↓
DURABLE_PREPARED
   ↓
REFERENCED_BY_INTENT
   ↓
PUBLISHED
   ↓
VERIFIED
```

这是一套产物生命周期语义，不要求每个状态都独立存成数据库枚举。

#### 13.1 STAGING

正在生成文件。

特点：

- 不能用于恢复重试。
- 不能作为有效 Intent 的依据。
- 崩溃后可以按孤儿清理规则处理。

#### 13.2 DURABLE_PREPARED

准备产物已经：

- 完整写入。
- 完成文件同步。
- 计算并验证哈希。
- 放置在稳定的受控路径。
- 完成平台支持的必要目录持久化步骤。

#### 13.3 REFERENCED_BY_INTENT

SQLite 已经提交引用该准备产物的 Operation Intent。

从此：

- 不能静默修改准备产物。
- 不能提前删除准备产物。
- 重试必须复用同一份准备产物。
- 不能重新读取变化后的原始输入来替代它。

#### 13.4 PUBLISHED

目标文件已经通过受控发布操作出现。

#### 13.5 VERIFIED

独立验证器确认目标结果符合预期。

### 14. 文件系统与 SQLite 的提交顺序

K0 正式顺序：

```text
1. 创建私有 Staging 文件

2. 写入全部输出字节

3. 同步文件内容

4. 计算并验证预期哈希

5. 将准备文件放置到稳定的 Prepared 路径

6. 执行平台支持的目录持久化操作

7. 开始 SQLite 事务

8. 插入 Operation Intent

9. 插入 FileWriteIntent

10. 更新 Step 状态

11. 写入审计事件

12. 提交 SQLite 事务

13. 创建执行 Attempt

14. 执行目标文件发布

15. 观察目标结果

16. 独立验证

17. 事务化提交最终状态和事件
```

#### 14.1 严格限制

步骤 12 完成前：

**MUST NOT 执行步骤 14。**

如果准备产物尚未达到所需持久化等级：

**MUST NOT 提交引用它的 Intent。**

#### 14.2 macOS 持久化说明

文件 `fsync()` 与目录项持久化并非同一个问题。

macOS 上不同文件系统和 API 的同步行为需要实测确认。

v1.1.1 要求：

- 运行时检测所需同步能力。
- 对关键系统调用的失败进行明确处理。
- 在目标文件系统上进行崩溃恢复测试。
- 不得把普通 `fsync()` 宣称为对所有断电场景的绝对保证。
- 若目标持久化要求无法满足，必须拒绝进入对应的生产等级。

是否使用 macOS 特定的更强同步机制，应通过 ADR 和平台测试确定。

---

## 第十四部分：原子不覆盖文件发布

### 15. 文件发布语义

v1.1.1 正式废除以下未经附加约束的方案：

```python
if not target.exists():
    os.replace(temp, target)
```

因为它不能原子保证目标不被覆盖。

#### 15.1 K0 正式选择

K0 采用：

**同一文件系统内的原子不覆盖发布。**

默认实现候选：

```python
os.link(
    prepared_path,
    target_path,
)
```

使用硬链接创建最终目标目录项。

如果目标名称已经存在，创建应失败，而不是替换已有目录项。

**这是一项需要在目标 macOS 文件系统和 Python 运行环境中验证的实施选择，而不是未经测试的跨平台保证。**

#### 15.2 目录布局

推荐：

```text
~/Library/Application Support/JARVIS/
    data/
        jarvis.db

    state/
        tasks/
            <task_id>/
                snapshot/
                prepared/
                output/
                evidence/
```

对于 K0：

```text
target:
state/tasks/<task_id>/output/inventory.json
```

要求：

- 目标目录由 JARVIS 创建。
- 目录权限受控。
- 目标名称固定或严格验证。
- 不接受任意相对路径穿越。
- Prepared 文件和目标位于同一文件系统。
- 发布前 Prepared 文件不再允许正常修改。

#### 15.3 正式发布流程

```text
读取已提交 Intent
       ↓
检查取消与紧急停止
       ↓
检查执行器所有权
       ↓
验证 Prepared 文件哈希
       ↓
通过安全目录句柄定位目标
       ↓
执行原子不覆盖创建
       ↓
如果成功：
    同步目标目录
    进入结果核对
       ↓
如果目标已存在：
    只读观察目标
       ├── 内容符合预期 → 进入验证
       └── 内容不符合预期 → BLOCKED
       ↓
如果出现其他错误：
    记录错误
    根据工具契约决定重试或阻塞
```

#### 15.4 禁止事项

不得：

- 先检查不存在，再使用普通覆盖式重命名。
- 因为目标冲突而自动删除目标。
- 因为目标冲突而自动改名。
- 在重试时生成不同的输出内容。
- 在验证失败后静默覆盖目标。
- 将符号链接当作普通目标文件接受。
- 把不受信任的路径字符串直接交给文件写入 API。

#### 15.5 文件发布后的安全边界

原子不覆盖发布只能保证：

> 发布时不会因为本次创建操作而覆盖已经存在的目标目录项。

它不保证：

- 发布后其他进程不会删除目标。
- 同一用户身份的恶意进程不能修改文件。
- 所有硬件断电场景下目录项一定保留。
- 文件系统不会损坏。

这些属于独立的威胁模型和运行可靠性问题。

---

## 第十五部分：结果核对与安全重试

### 16. Reconciliation 采用双层模型

v1.1.1 正式取消将以下概念直接作为唯一恢复事实：

```text
CONFIRMED_NOT_EXECUTED
```

因为观察不到目标，不等于能证明历史上从未执行过。

新的 Reconciliation 分成两层：

1. **Observation：实际观察。**
2. **Decision：恢复决策。**

#### 16.1 Observation

```text
EXPECTED_EFFECT_PRESENT
TARGET_ABSENT
CONFLICTING_EFFECT_PRESENT
INSUFFICIENT_EVIDENCE
```

#### EXPECTED_EFFECT_PRESENT

目标存在，且：

- 是允许的文件类型。
- 大小符合预期。
- SHA-256 符合预期。
- 必要的元数据检查通过。

#### TARGET_ABSENT

当前没有观察到目标。

**不得据此断言目标历史上从未存在。**

#### CONFLICTING_EFFECT_PRESENT

目标存在，但：

- 文件类型不符。
- 哈希不符。
- 大小不符。
- 是符号链接。
- 或其他关键属性冲突。

#### INSUFFICIENT_EVIDENCE

无法安全观察目标。

例如：

- 权限异常。
- I/O 错误。
- 路径状态不稳定。
- Prepared 文件缺失。
- 数据库记录与产物清单冲突。

#### 16.2 Decision

```text
VERIFY
RETRY_SAFE
BLOCK
FAIL
```

#### VERIFY

实际效果符合预期，继续独立验证。

#### RETRY_SAFE

工具恢复策略已经证明：

在当前操作契约和信任假设下，再次尝试不会产生不允许的副作用。

#### BLOCK

需要人工处理、修复环境或补充证据。

#### FAIL

已经确认任务无法完成，且不存在未解决的危险副作用。

#### 16.3 K0 决策表

| Observation | 条件 | Decision |
|---|---|---|
| EXPECTED_EFFECT_PRESENT | Intent、哈希、路径均有效 | VERIFY |
| TARGET_ABSENT | Prepared 完整、允许重试、未取消、未停止 | RETRY_SAFE |
| TARGET_ABSENT | Prepared 缺失或损坏 | BLOCK |
| CONFLICTING_EFFECT_PRESENT | 任意 | BLOCK |
| INSUFFICIENT_EVIDENCE | 任意 | BLOCK |

其中 `TARGET_ABSENT → RETRY_SAFE` 只适用于 K0 的原子不覆盖文件发布协议。

**不得把这条规则直接推广到邮件、支付、浏览器提交或其他外部服务。**

#### 16.4 正式结果格式

```json
{
  "schema_version": 1,
  "operation_id": "<operation_id>",
  "observation": "EXPECTED_EFFECT_PRESENT",
  "decision": "VERIFY",
  "evidence": {
    "target_relpath": "output/inventory.json",
    "observed_sha256": "<actual_sha256>",
    "observed_size": 128
  },
  "reason_code": "EXPECTED_HASH_MATCH"
}
```

恢复决策必须由确定性策略生成。

模型可以解释错误，但不能代替 Reconciler 决定是否安全重试。

---

## 第十六部分：执行器互斥、租约与 Fencing

### 17. K0 执行器模型

K0 只允许一个活动执行器。

正式采用：

```text
flock
    +
SQLite Worker Epoch
    +
CAS State Updates
```

Lease 用于健康观测，不用于自动并发接管。

#### 17.1 文件锁

执行器必须：

1. 打开固定锁文件。
2. 获取排他锁。
3. 在整个执行器生命周期持有文件描述符。
4. 不在运行过程中删除锁文件。
5. 所有执行入口使用相同锁路径。
6. 获取锁失败时拒绝成为活动执行器。

锁文件示例：

```text
~/Library/Application Support/JARVIS/state/worker.lock
```

`flock` 是协作式进程互斥机制，不是对所有外部程序的访问控制。

#### 17.2 Worker Epoch

成功获取文件锁后：

1. 开启 SQLite 事务。
2. 递增持久化 `worker_epoch`。
3. 记录执行器启动事件。
4. 提交。
5. 执行恢复扫描。
6. 再开始领取新任务。

每个 Attempt 记录其 `worker_epoch`。

过期 Epoch 不允许提交任务结果。

#### 17.3 Lease

Lease 记录：

```text
worker_id
worker_epoch
heartbeat_at_ms
lease_expires_at_ms
```

用途：

- 健康检查。
- 检测异常执行器。
- 辅助恢复诊断。

**Lease 到期不等于允许第二个执行器并发执行。**

新的执行器仍然必须先取得文件锁。

#### 17.4 时间语义

- 审计时间使用 UTC 时间戳。
- 进程内部超时尽量使用单调时钟。
- Lease 时间不能作为唯一的执行权证明。
- Mac 休眠或系统时间调整后，必须重新检查执行状态。

#### 17.5 子进程规则

K0 不提供任意子进程执行工具。

未来如果引入子进程：

- 必须记录进程身份。
- 必须明确停止和回收策略。
- 不得让工具进程脱离执行器继续产生副作用。
- 必须避免锁文件描述符被不受控地继承。
- 需要额外的进程生命周期测试。

---

## 第十七部分：故障恢复协议

### 18. 恢复入口

恢复由以下事件触发：

```text
PROCESS_STARTED
SYSTEM_RECOVERED
TASK_INTERRUPTED
TOOL_TIMEOUT
UNKNOWN_OUTCOME
MANUAL_RECOVERY_REQUEST
```

#### 18.1 恢复流程

```text
启动 JARVIS
      ↓
获取唯一执行器锁
      ↓
递增 Worker Epoch
      ↓
检查数据库完整性
      ↓
检查 Emergency Stop
      ↓
扫描未完成 Task
      ↓
扫描活动 Attempt
      ↓
扫描未完成 Intent
      ↓
扫描孤儿 Prepared Artifact
      ↓
逐项观察真实外部状态
      ↓
生成 Reconciliation Result
      ↓
VERIFY / RETRY_SAFE / BLOCK / FAIL
      ↓
事务化记录恢复结果
      ↓
在允许的条件下继续调度
```

#### 18.2 不同崩溃位置的处理

| 崩溃位置 | 恢复规则 |
|---|---|
| Staging 文件写入中 | 识别不完整产物，按安全清理规则处理 |
| Prepared 文件完成但未提交 Intent | 作为孤儿产物处理，不执行目标副作用 |
| Intent 事务提交前 | 事务回滚，不得视为已授权执行 |
| Intent 提交后、发布前 | 观察目标；满足条件时安全重试 |
| 发布后、数据库结果提交前 | 观察目标；正确则补记验证结果 |
| 验证中崩溃 | 重新执行只读验证 |
| 成功事务提交后崩溃 | 读取已提交终态，不重复执行 |
| 取消请求提交后崩溃 | 继续取消和安全核对流程 |
| 紧急停止后崩溃 | 保持停止，不自动恢复新副作用 |

#### 18.3 孤儿产物

孤儿产物定义：

> 文件系统中存在，但没有被有效数据库记录引用的准备文件或临时文件。

清理规则：

- 启动时可以扫描。
- 不立即删除。
- 必须确认不属于活动操作。
- 必须满足保留期限。
- 必须记录清理事件。
- 不能删除仍被 Intent 引用的产物。

#### 18.4 悬空引用

悬空引用定义：

> 数据库记录引用了不存在或不符合预期的恢复产物。

处理：

```text
NEEDS_RECONCILIATION
    ↓
INSUFFICIENT_EVIDENCE
    ↓
BLOCKED
```

不得静默重新读取原始输入并生成替代产物。

---

## 第十八部分：权限与安全模型

### 19. 权限等级

| 等级 | 操作示例 | 默认策略 |
|---|---|---|
| L0 | 读取非敏感内部状态 | 自动允许 |
| L1 | 读取授权工作区 | 按能力允许 |
| L2 | 在受控目录创建文件 | 需要明确任务授权 |
| L3 | 删除、覆盖、发送消息、修改外部系统 | 需要更严格审批 |

K0 只实现必要的 L0—L2 能力。

K0 不提供通用 L3 工具。

#### 19.1 Capability

工具授权必须至少绑定：

```text
task_id
step_id
tool_name
allowed_operation
allowed_resource
arguments_digest
expires_at
```

授权必须由确定性 Policy Engine 验证。

模型不得创建自己的有效 Capability。

#### 19.2 文件权限

K0 默认：

- 只读取明确授权的输入根目录。
- 只向 JARVIS 管理的任务输出目录写入。
- 禁止任意路径删除。
- 禁止覆盖已有不同内容。
- 禁止符号链接穿越。
- 禁止特殊文件。
- 禁止通过路径参数访问未授权目录。
- 禁止任意 Shell 执行。

#### 19.3 密钥管理

- API Key 存储在 macOS Keychain。
- 不写入 Git。
- 不写入普通日志。
- 不传给模型上下文。
- 测试使用假密钥。
- 不允许 AI 开发者主动读取生产密钥。

#### 19.4 威胁模型边界

K0 防范：

- 非法任务输入。
- 普通路径穿越。
- 意外符号链接。
- 非法状态转换。
- 重复请求。
- 执行器竞争。
- 普通崩溃恢复。
- 未授权工具调用。

K0 不承诺防范：

- 具有同一用户身份的完全恶意本地进程。
- root 权限攻击者。
- 已被攻陷的操作系统。
- 硬件级恶意修改。

更强隔离能力应在后续阶段引入独立进程、沙盒或虚拟机。

---

## 第十九部分：B1/B2 备份与恢复

### 20. 备份等级

#### B1：数据库备份

包含：

- SQLite 一致性备份。
- Schema 版本。
- 备份时间。
- 数据库完整性检查结果。
- 校验和。

#### B2：系统状态备份

包含：

- SQLite 数据库。
- 必需的输入快照。
- Prepared 文件。
- 输出产物。
- 验证证据。
- 必要配置。
- Artifact Manifest。
- 文件校验和。

#### 20.1 B2 备份冻结协议

正式流程：

```text
NORMAL
   ↓
BACKUP_REQUESTED
   ↓
QUIESCING
   ↓
BACKUP_IN_PROGRESS
   ↓
RESUMING
   ↓
NORMAL
```

具体步骤：

1. 持久化备份请求。
2. 停止领取新任务。
3. 阻止启动新的目标副作用。
4. 等待活动执行到安全检查点。
5. 确认被备份的产物不再变化。
6. 记录备份开始边界。
7. 使用 SQLite 一致性备份机制。
8. 复制数据库引用的必要产物。
9. 生成 Manifest。
10. 验证全部哈希。
11. 验证数据库引用关系。
12. 将完整备份原子发布。
13. 记录备份成功。
14. 恢复调度。

#### 20.2 超时规则

如果无法在规定时间内进入安全备份状态：

- 备份必须失败或推迟。
- 不允许复制变化中的产物并标记成功。
- 不允许无限等待。
- 必须记录原因。

#### 20.3 备份中断

如果备份过程中崩溃：

- 未完成备份不得标记有效。
- 临时备份目录不得被正式恢复程序接受。
- 恢复启动后检查备份 Manifest。
- 允许按保留策略清理无效备份。

#### 20.4 恢复

默认只允许恢复到独立目录。

恢复流程：

```text
验证备份 Manifest
       ↓
验证文件哈希
       ↓
验证数据库完整性
       ↓
检查 Schema 兼容性
       ↓
检查数据库与产物引用
       ↓
启动隔离测试实例
       ↓
执行恢复测试
       ↓
生成恢复报告
```

正式生产数据替换必须另行审批。

#### 20.5 恢复目标

| 指标 | 初期目标 |
|---|---|
| B1 备份 | 至少每日一次 |
| B2 备份 | 发布前强制执行，逐步实现每日备份 |
| RPO | 目标不超过 24 小时 |
| RTO | 目标 30 分钟 |
| 恢复演练 | 每次正式发布前至少一次 |

这些都是工程目标。

**没有实际演练证据时，不得声称已经达到。**

---

## 第二十部分：K1——模型驱动的受控执行

### 21. K1 目标

在 K0 可靠执行内核之上增加：

- 可替换模型网关。
- 结构化规划。
- 多步骤任务。
- 可信审批。
- 预算管理。
- 上下文管理。
- 模型故障处理。
- 安全回归评测。

K1 不允许绕过 K0 的 Operation Intent、Attempt、Reconciler 和 Verifier。

#### 21.1 Model Gateway

统一接口至少包含：

```python
class ModelProvider(Protocol):
    async def generate(
        self,
        request: ModelRequest,
    ) -> ModelResponse:
        ...
```

`ModelRequest` 至少包含：

```text
model
messages
response_schema
timeout_seconds
max_output_tokens
request_id
```

`ModelResponse` 至少包含：

```text
provider
model
content
structured_output
usage
finish_reason
provider_request_id
```

模型适配器负责：

- 请求格式转换。
- 超时。
- 速率限制。
- 错误归一化。
- Token 用量记录。
- 响应 Schema 验证。

模型适配器不负责：

- 执行工具。
- 授权。
- 修改 Task 状态。
- 判断外部副作用是否已经成功。

#### 21.2 模型输出的信任边界

模型输出始终属于：

```text
UNTRUSTED_PROPOSAL
```

执行前必须经过：

```text
Schema Validation
    ↓
Plan Validation
    ↓
Policy Validation
    ↓
Budget Validation
    ↓
Approval Validation
    ↓
Execution Coordinator
```

不得让模型直接生成 SQL 更新任务状态。

不得将模型文本中的“已完成”视为验证证据。

### 22. 结构化计划与重新规划

#### 22.1 Plan Version

一个 Task 可以拥有：

```text
Plan v1
Plan v2
Plan v3
```

但同一时间只能有一个活动 Plan。

计划版本由内核管理。

模型只能提出候选计划。

#### 22.2 计划切换协议

```text
请求重新规划
      ↓
暂停领取旧计划的新步骤
      ↓
等待活动步骤进入安全检查点
      ↓
保存旧计划执行历史
      ↓
生成新计划
      ↓
验证新计划 Schema
      ↓
检查历史副作用
      ↓
映射可复用结果
      ↓
检查权限和预算
      ↓
必要时请求审批
      ↓
事务化切换活动 Plan
      ↓
恢复调度
```

#### 22.3 历史副作用映射

重新规划必须区分：

```text
ALREADY_COMPLETED
IN_PROGRESS
UNKNOWN_OUTCOME
NEW_EFFECT
```

如果某个新步骤可能与历史副作用重复，但无法确定：

```text
BLOCKED
```

不得重新生成一个 Operation Key 后直接执行。

#### 22.4 计划不变量

- 旧 Plan 不得删除。
- 已完成副作用不得从历史中消失。
- 新 Plan 不得绕过旧 Intent。
- 活动 Plan 切换必须事务化。
- 未核对的历史操作必须先核对。
- 计划变化不能自动扩大权限。

### 23. 审批安全

#### 23.1 审批请求与审批决定分离

审批请求：

```text
approval_requests
```

审批决定：

```text
approval_decisions
```

模型只能提出需要审批的操作。

模型不得生成有效审批决定。

#### 23.2 审批绑定

审批摘要至少包含：

```text
task_id
plan_id
plan_version
step_id
tool_name
tool_version
canonical_arguments
target_resource
content_digest
risk_level
expires_at
```

执行时重新计算摘要。

如果实际操作与审批内容不一致：

```text
POLICY_DENIED
```

#### 23.3 可信身份边界

K1 初期：

- 不开放任意 Shell 工具。
- 审批由独立的可信用户交互入口产生。
- 模型工具不能调用审批决定接口。
- 审批凭据不得暴露给模型或工具执行器。
- 审批只能用于指定操作。
- 审批必须有有效期。
- 高风险审批默认一次性消费。

**不能仅因为命令叫作 `jarvis approval approve`，就认为它具有可信身份。**

在未来开放更强工具能力之前，必须通过专项安全设计证明审批入口无法被模型工具冒用。

### 24. 模型预算

K1 采用：

```text
RESERVE
    ↓
EXECUTE
    ↓
SETTLE
```

模型调用前预留预算。

调用结束后结算实际消耗。

必须支持：

```text
max_model_calls_per_task
max_model_calls_per_day
max_tokens_per_task
max_daily_model_cost
max_tool_calls_per_task
max_replans_per_task
max_task_runtime_seconds
```

供应商费用未知时：

- 按配置上限估算。
- 不得将未知费用视为零。
- 必须保留实际用量记录。
- 超过预算时停止新的模型调用。

模型预算与工具权限是两个独立控制维度。

### 25. K1 任务清单

| ID | 任务 | 关键交付 |
|---|---|---|
| K1-001 | Model Provider Protocol | 标准模型接口 |
| K1-002 | Mock Provider | 故障模拟与回归测试 |
| K1-003 | 首个真实模型适配器 | 可替换模型接入 |
| K1-004 | Provider Capability Registry | 模型能力描述 |
| K1-005 | 结构化规划器 | Plan Schema 与 Validator |
| K1-006 | 计划版本与历史副作用映射 | 安全 Replanning |
| K1-007 | 审批请求与可信决定 | Approval Service |
| K1-008 | 预算预留与结算 | Budget Manager |
| K1-009 | 上下文构建器 | Context Builder |
| K1-010 | Prompt Injection 安全评测 | Security Evals |
| K1-011 | 多步骤黄金任务 | Evaluation Suite |
| K1-012 | 长期运行与发布评审 | K1 Release Candidate |

#### K1 发布门禁

至少证明：

1. 模型不能绕过 Policy Engine。
2. 模型不能直接执行工具。
3. 模型不能自行批准高风险操作。
4. 重新规划不会重复执行已完成副作用。
5. 模型超时不会破坏任务状态。
6. 模型返回非法 Schema 时能够安全失败。
7. 预算耗尽时不再发起新调用。
8. 更换模型供应商不改变内核安全规则。
9. 全部指定安全回归测试通过。
10. 完成规定的长期运行测试。

---

## 第二十一部分：K2——长期记忆与人格

### 26. K2 目标

建立可追溯、可纠错、可删除、可评测的长期记忆系统。

记忆分为：

```text
Working Memory
Episodic Memory
Semantic Memory
Preference Memory
```

#### 26.1 记忆数据契约

至少包含：

```text
memory_id
memory_type
content
source_type
source_reference
confidence
verification_status
created_at
updated_at
valid_from
valid_until
supersedes_id
privacy_classification
retention_policy
```

#### verification_status

```text
USER_CONFIRMED
EXTERNALLY_VERIFIED
TOOL_OBSERVED
MODEL_INFERRED
UNVERIFIED
REJECTED
SUPERSEDED
```

模型推断不得自动提升为用户确认事实。

#### 26.2 记忆冲突

如果新记忆与旧记忆冲突：

1. 不得直接静默覆盖。
2. 记录冲突关系。
3. 检查来源和时间。
4. 判断是否存在明确更新。
5. 必要时请求用户确认。
6. 保留可追溯的修订记录。

#### 26.3 检索

K2 初期使用：

```text
SQLite FTS5
```

不强制引入向量数据库。

只有在检索评测证明有必要时，才增加向量检索。

#### 26.4 删除与隐私

必须支持：

- 用户请求删除。
- 停止在正常检索中返回已删除记忆。
- 处理派生摘要和索引。
- 明确备份保留与删除传播策略。
- 区分逻辑删除和物理删除。
- 不承诺未经验证的绝对不可恢复删除。

涉及敏感数据的 B2 备份必须有明确访问控制和加密策略。

#### 26.5 人格系统

人格由版本化规范管理。

至少包含：

```text
Persona Version
Communication Style
Behavioral Boundaries
Preference Rules
Safety Constraints
Regression Scenarios
```

人格不能改变权限系统。

人格不能授予工具执行权。

人格不能绕过审批。

#### 26.6 K2 任务清单

| ID | 任务 | 交付 |
|---|---|---|
| K2-001 | Memory Schema | 数据契约 |
| K2-002 | Working Memory | 工作记忆 |
| K2-003 | Episodic Memory | 情节记忆 |
| K2-004 | Semantic Memory | 语义记忆 |
| K2-005 | Preference Store | 用户偏好 |
| K2-006 | FTS5 Retrieval | 检索服务 |
| K2-007 | Provenance & Conflict | 来源与冲突管理 |
| K2-008 | Deletion & Retention | 删除与保留策略 |
| K2-009 | Persona Manager | 人格版本管理 |
| K2-010 | Memory & Persona Evals | 评测 |
| K2-011 | K2 Release Review | 发布评审 |

#### K2 发布门禁

必须有真实标注测试集，验证：

- 来源引用完整性。
- 冲突检测。
- 过期记忆排除。
- 删除后正常检索不可见。
- 用户确认与模型推断的区分。
- 人格回归一致性。
- 跨模型行为差异。

Recall@5、Precision@5 等指标必须建立测试集后再设定正式门槛。

不得凭空宣称记忆准确率。

---

## 第二十二部分：K3——事件驱动有限自主

### 27. K3 目标

允许 JARVIS 在明确授权、预算和时间约束下，主动处理有限类型的事件。

自主行为不采用无限模型循环。

采用：

```text
Event
   ↓
Policy Check
   ↓
Deduplication
   ↓
Task Creation
   ↓
K0/K1 Execution
   ↓
Verification
```

#### 27.1 事件类型

```text
USER_REQUEST
SCHEDULE_TRIGGER
TOOL_RESULT
TASK_FAILED
SYSTEM_RECOVERED
RESOURCE_WARNING
EXTERNAL_CHANGE
```

#### 27.2 事件持久化

事件至少包含：

```text
event_id
event_type
event_deduplication_key
payload
source
created_at
available_at
attempt_count
processing_state
```

#### 27.3 事件去重与任务幂等

事件重复处理必须使用稳定的派生任务请求键。

例如：

```text
event_id:
    schedule-health-check-2026-10-12T09:00

derived_task_request_key:
    scheduled-health-check-2026-10-12T09:00
```

同一个事件重复处理时，必须返回同一个 Task，而不是创建第二个任务。

**事件去重不等于外部副作用幂等。**

所有外部副作用仍然必须经过 K0 的 Operation Intent 和恢复协议。

#### 27.4 Outbox

需要从数据库事务中可靠发布后续事件时，使用 Outbox。

最低要求：

1. 业务状态与 Outbox 记录同事务提交。
2. 事件消费者允许重复接收。
3. 消费者使用幂等键。
4. 处理失败有次数限制。
5. 超过限制后进入阻塞或死信状态。

#### 27.5 自主等级

| 等级 | 能力 |
|---|---|
| A0 | 被动响应 |
| A1 | 定时执行低风险检查 |
| A2 | 有限主动提醒 |
| A3 | 有预算的多步骤目标 |
| A4 | 隔离环境中的改进提案 |
| A5 | 经过独立门禁的低风险自动发布，远期研究 |

K3 初期只开放 A1—A2。

A3 需要专项评审。

A4—A5 不属于 K3 正式能力。

#### 27.6 自主运行限制

必须支持：

```text
autonomy_enabled
quiet_hours
max_proactive_tasks_per_day
max_proactive_cost_per_day
max_event_attempts
max_event_age
max_concurrent_proactive_tasks
```

关闭主动模式后：

- 不得生成新的主动副作用。
- 积压事件必须按正式策略处理。
- 不允许关闭后再因旧事件自动执行。

#### 27.7 K3 任务清单

| ID | 任务 | 交付 |
|---|---|---|
| K3-001 | Event Schema | 事件契约 |
| K3-002 | Durable Event Store | 持久化事件 |
| K3-003 | Outbox | 可靠事件发布 |
| K3-004 | Event Deduplication | 去重与重试 |
| K3-005 | Persistent Scheduler | 定时调度 |
| K3-006 | Autonomy Budget | 主动行为预算 |
| K3-007 | Autonomy Controls | 主动模式控制 |
| K3-008 | Sleep/Wake Recovery | 休眠恢复 |
| K3-009 | K3 Security & Release | 安全发布评审 |

---

## 第二十三部分：K4——受控技能与系统改进

### 28. K4 分级

v1.1.1 将 K4 正式拆成：

#### K4-A：受控技能改进

允许在受限范围内：

- 创建新技能。
- 修改技能代码。
- 增加技能测试。
- 优化技能实现。
- 提交改进提案。

#### K4-B：核心系统改进

涉及：

- 状态机。
- 权限引擎。
- 数据库迁移。
- 执行协议。
- 发布门禁。
- 密钥管理。
- 恢复系统。

K4-B 必须采用更严格的独立评审。

第一版 K4 不允许自动批准核心系统修改。

#### 28.1 改进流程

```text
Improvement Proposal
        ↓
Scope Validation
        ↓
Isolated Workspace
        ↓
Code Generation
        ↓
Static Checks
        ↓
Unit Tests
        ↓
Security Tests
        ↓
Regression Evals
        ↓
Independent Review
        ↓
Human Approval
        ↓
Canary Release
        ↓
Monitoring
        ↓
Promote / Rollback
```

#### 28.2 隔离要求

Git Worktree 只提供代码工作区隔离。

它不是安全沙盒。

执行 AI 生成代码时，需要经过验证的隔离环境。

必须限制：

- 文件系统访问。
- 网络访问。
- 密钥访问。
- 进程权限。
- CPU 和内存。
- 运行时间。
- 可写目录。

K4-B 不得在生产 JARVIS 进程内直接执行未经验证的修改代码。

#### 28.3 技能 Manifest

至少包含：

```text
skill_id
version
entrypoint
input_schema
output_schema
required_capabilities
dependencies
artifact_hash
test_suite
risk_level
```

#### 28.4 K4 任务清单

| ID | 任务 | 交付 |
|---|---|---|
| K4-001 | Skill Manifest | 技能契约 |
| K4-002 | Skill Registry | 技能注册表 |
| K4-003 | Version Manager | 版本管理 |
| K4-004 | Isolated Runner | 隔离执行 |
| K4-005 | Improvement Proposal | 改进提案 |
| K4-006 | Automated Test Runner | 自动测试 |
| K4-007 | Regression Evaluator | 独立回归 |
| K4-008 | Release Approval | 人工审批 |
| K4-009 | Controlled Release | 受控发布 |
| K4-010 | Rollback Procedure | 回滚 |
| K4-011 | Security Review | 专项安全评审 |

---

## 第二十四部分：标准工程目录

### 29. 仓库结构

```text
jarvis/
│
├── AGENTS.md
├── README.md
├── PROJECT_STATE.md
├── DECISIONS_REQUIRED.md
├── pyproject.toml
├── uv.lock
├── .python-version
├── .gitignore
│
├── docs/
│   ├── 00_MASTER_PLAN.md
│   ├── 01_REQUIREMENTS.md
│   ├── 02_ARCHITECTURE.md
│   ├── 03_DATA_CONTRACTS.md
│   ├── 04_SECURITY_MODEL.md
│   ├── 05_TEST_STRATEGY.md
│   ├── 06_RELEASE_POLICY.md
│   ├── 07_OPERATIONS_RUNBOOK.md
│   │
│   ├── EXECUTION_SEMANTICS.md
│   ├── FILESYSTEM_PROTOCOL.md
│   ├── RECOVERY_PROTOCOL.md
│   ├── THREAT_MODEL.md
│   ├── BOOTSTRAP_PROTOCOL.md
│   ├── TRACEABILITY_MATRIX.md
│   ├── PRODUCTION_READINESS.md
│   │
│   ├── adr/
│   ├── archive/
│   ├── reviews/
│   ├── reports/
│   ├── retrospectives/
│   └── releases/
│
├── contracts/
│   ├── task_spec.schema.json
│   ├── task_states.yaml
│   ├── step_states.yaml
│   ├── task_transitions.yaml
│   ├── step_transitions.yaml
│   ├── execution_invariants.yaml
│   ├── operation_intent.schema.json
│   ├── file_write_intent.schema.json
│   ├── reconciliation.schema.json
│   ├── snapshot_manifest.schema.json
│   └── tool_manifest.schema.json
│
├── planning/
│   ├── BACKLOG.md
│   ├── TASK_TEMPLATE.md
│   └── tasks/
│
├── src/
│   └── jarvis/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       │
│       ├── domain/
│       │   ├── task.py
│       │   ├── plan.py
│       │   ├── step.py
│       │   ├── attempt.py
│       │   ├── operation.py
│       │   └── events.py
│       │
│       ├── kernel/
│       │   ├── state_machine.py
│       │   ├── scheduler.py
│       │   ├── coordinator.py
│       │   ├── worker_lock.py
│       │   ├── cancellation.py
│       │   └── recovery.py
│       │
│       ├── storage/
│       │   ├── database.py
│       │   ├── migrations.py
│       │   ├── repositories.py
│       │   ├── backup.py
│       │   └── migrations/
│       │
│       ├── security/
│       │   ├── policy.py
│       │   ├── paths.py
│       │   └── approvals.py
│       │
│       ├── tools/
│       │   ├── registry.py
│       │   └── filesystem.py
│       │
│       ├── execution/
│       │   ├── intents.py
│       │   ├── attempts.py
│       │   ├── snapshots.py
│       │   ├── publisher.py
│       │   ├── reconciler.py
│       │   └── verifier.py
│       │
│       ├── models/
│       │   ├── gateway.py
│       │   ├── planner.py
│       │   └── providers/
│       │
│       ├── memory/
│       ├── persona/
│       ├── autonomy/
│       ├── skills/
│       │
│       └── observability/
│           ├── logging.py
│           └── health.py
│
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── e2e/
│   ├── recovery/
│   ├── security/
│   ├── regression/
│   ├── fault_injection/
│   └── fixtures/
│
├── evals/
│
├── scripts/
│   ├── check_environment.py
│   ├── run_quality_checks.sh
│   └── verify_release.py
│
└── deploy/
    └── macos/
        └── launchd/
```

#### 29.1 目录约束

- `domain/` 不依赖 SQLite。
- `kernel/` 不直接调用模型供应商 SDK。
- `models/` 不直接修改任务状态。
- `tools/` 不自行批准权限。
- `security/` 不把模型输出当作授权。
- `execution/` 负责副作用、核对和验证。
- `storage/` 不负责自然语言规划。
- `contracts/` 是正式接口契约来源。
- `tests/` 不得包含生产密钥。
- 运行数据库和用户产物不进入 Git。

K0-001 不要求一次性实现全部目录中的未来模块。

---

## 第二十五部分：K0 Contract Pack

### 30. 机器可读契约包

v1.1.1 新增强制交付：

**JARVIS K0 Contract Pack v1.0**

它是 K0 核心开发的直接实施依据。

Master Plan 负责总体设计。

Contract Pack 负责精确行为。

#### 30.1 必需文件

| 文件 | 用途 |
|---|---|
| `task_spec.schema.json` | 任务请求验证 |
| `task_states.yaml` | Task 状态集合 |
| `step_states.yaml` | Step 状态集合 |
| `task_transitions.yaml` | Task 合法转换与 Guard |
| `step_transitions.yaml` | Step 合法转换与 Guard |
| `execution_invariants.yaml` | 跨对象一致性规则 |
| `operation_intent.schema.json` | 通用操作意图 |
| `file_write_intent.schema.json` | 文件副作用数据 |
| `reconciliation.schema.json` | 观察与恢复决策 |
| `snapshot_manifest.schema.json` | 快照格式 |
| `FILESYSTEM_PROTOCOL.md` | 文件读取和发布 |
| `RECOVERY_PROTOCOL.md` | 崩溃恢复 |

#### 30.2 契约规则

每个契约必须：

1. 有 Schema 版本。
2. 有合法示例。
3. 有非法示例。
4. 有自动化验证测试。
5. 有明确的兼容性策略。
6. 有对应的代码实现。
7. 有可追溯的变更记录。

AI 不得自行发明与正式契约冲突的字段和状态。

#### 30.3 状态转换契约示例

```yaml
schema_version: 1

transitions:
  - from: CREATED
    to: READY
    guards:
      - valid_task_spec
      - active_plan_committed

  - from: READY
    to: RUNNING
    guards:
      - worker_lock_held
      - worker_epoch_current
      - no_cancel_request
      - emergency_stop_inactive

  - from: VERIFYING
    to: SUCCEEDED
    guards:
      - all_required_steps_completed
      - all_required_effects_verified
      - no_cancel_request
      - no_unresolved_attempts

  - from: NEEDS_RECONCILIATION
    to: READY
    guards:
      - retry_safe
      - no_cancel_request
      - emergency_stop_inactive
```

这只是格式示例。

正式文件必须覆盖全部状态转换。

#### 30.4 契约测试

至少验证：

- 所有声明的状态都有效。
- 所有转换引用的状态存在。
- 所有 Guard 都有实现。
- 非法转换被拒绝。
- 所有终态不可重新打开。
- Schema 示例可通过验证。
- 非法示例被拒绝。
- Python 状态枚举与 YAML 一致。
- 数据库状态约束与契约一致。

---

## 第二十六部分：CLI 与机器可读接口

### 31. K0 CLI

必须实现：

```bash
jarvis --version

jarvis health

jarvis task submit --spec task.json

jarvis task list

jarvis task status <task_id>

jarvis task run --once

jarvis task cancel <task_id>

jarvis emergency-stop

jarvis emergency-reset

jarvis backup create

jarvis backup verify <backup_id>

jarvis restore --source <backup_id> --to <directory>
```

关键命令必须支持：

```bash
--json
```

#### 31.1 提交任务

```json
{
  "schema_version": 1,
  "accepted": true,
  "task_id": "<task_id>",
  "state": "CREATED",
  "duplicate_request": false
}
```

重复提交相同请求时：

```json
{
  "schema_version": 1,
  "accepted": true,
  "task_id": "<existing_task_id>",
  "state": "READY",
  "duplicate_request": true
}
```

这里的 `READY` 只是示例。

实际响应必须返回数据库中的真实状态。

#### 31.2 错误分类

```text
INVALID_REQUEST
REQUEST_CONFLICT
TASK_NOT_FOUND
INVALID_TRANSITION
VERSION_CONFLICT
POLICY_DENIED
PATH_DENIED
TOOL_TIMEOUT
TOOL_FAILED
UNKNOWN_OUTCOME
DATABASE_BUSY
DATABASE_ERROR
WORKER_ALREADY_RUNNING
EMERGENCY_STOP_ACTIVE
RECOVERY_REQUIRED
BACKUP_NOT_SAFE
UNSUPPORTED_FILESYSTEM
```

统一错误格式：

```json
{
  "schema_version": 1,
  "error": {
    "code": "REQUEST_CONFLICT",
    "message": "request_key 已用于不同任务内容",
    "retryable": false
  }
}
```

#### 31.3 CLI 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 输入或参数错误 |
| 3 | 任务不存在 |
| 4 | 状态或版本冲突 |
| 5 | 权限拒绝 |
| 6 | 执行失败 |
| 7 | 结果未知或需要恢复 |
| 8 | 数据库或内部错误 |
| 9 | 紧急停止生效 |
| 10 | 执行器已被占用 |

所有 CLI 错误必须：

- 使用稳定错误码。
- 不泄露密钥。
- 不输出未经清理的敏感内部数据。
- 在 JSON 模式下保持机器可解析。

---

## 第二十七部分：macOS 长期运行

### 32. 运行目录

推荐：

```text
~/Library/Application Support/JARVIS/
    data/
        jarvis.db

    state/
        worker.lock
        tasks/
        artifacts/

    backups/
    config/

~/Library/Logs/JARVIS/
    jarvis.log
    errors.log
```

要求：

- 代码目录与运行目录分离。
- 数据目录限制访问权限。
- 运行数据库不进入 Git。
- 不在未经验证的同步目录中存放活动数据库。
- 日志不能包含生产密钥。
- 备份具有保留和访问策略。

#### 32.1 launchd

K0-E 使用用户级 LaunchAgent。

必须测试：

1. 用户登录后启动。
2. 异常退出后的恢复。
3. 正常停止。
4. 主动停止不会无限重启。
5. 启动失败能够定位日志。
6. Mac 休眠后的状态核对。
7. Mac 唤醒后的任务恢复。
8. 用户退出登录后的预期行为。
9. 服务升级后的数据库兼容性。

#### 32.2 服务边界

必须明确：

- Mac 关机时不执行任务。
- Mac 休眠时不承诺持续执行。
- LaunchAgent 依赖用户会话。
- 进程存在不等于业务健康。
- `launchd` 不替代 JARVIS 内部恢复管理器。

#### 32.3 健康检查

至少提供：

```text
kernel_running
database_available
worker_lock_status
worker_epoch
scheduler_heartbeat
active_attempt_count
pending_task_count
blocked_task_count
unresolved_intent_count
emergency_stop
backup_mode
disk_usage
```

`jarvis health --json` 必须返回真实检测结果。

不能仅因为 Python 进程还在运行就返回健康。

---

## 第二十八部分：日志、指标与资源治理

### 33. 结构化日志

日志至少包含：

```text
timestamp
level
event_type
task_id
plan_id
step_id
operation_id
attempt_id
worker_epoch
correlation_id
duration_ms
error_code
```

必须避免记录：

- API Key。
- 密码。
- 未经授权的私人文件内容。
- 不必要的完整模型上下文。
- 敏感审批凭据。

#### 33.1 核心指标

| 指标 | 用途 |
|---|---|
| Task Success Rate | 任务成功率 |
| Task Failure Rate | 任务失败率 |
| Blocked Task Count | 阻塞任务数量 |
| Unresolved Intent Count | 未核对操作数量 |
| Recovery Success Rate | 恢复成功率 |
| Duplicate Effect Incidents | 重复副作用事件 |
| Unauthorized Action Incidents | 未授权操作事件 |
| Mean Recovery Time | 平均恢复时间 |
| Cost per Successful Task | 单次成功任务成本 |
| Disk Usage | 磁盘资源 |
| Model Calls per Task | 模型调用次数 |
| User Intervention Rate | 人工干预比例 |

指标必须定义分母、时间窗口和排除条件。

#### 33.2 资源限制

K0 至少支持：

```text
max_concurrent_tasks = 1
max_pending_tasks
max_task_runtime_seconds
max_input_files
max_input_bytes
max_artifact_bytes
max_log_size_mb
max_disk_usage_mb
```

K1 增加模型预算限制。

资源不足时：

1. 拒绝新的低优先级任务。
2. 停止新的主动行为。
3. 停止新的模型调用。
4. 保存当前执行状态。
5. 在安全检查点停止。
6. 通知用户。

不得通过删除仍需恢复的产物来临时释放空间。

---

## 第二十九部分：K0 正式开发任务清单

### 34. K0 开发阶段

K0 继续划分为：

```text
K0-A 工程基础
K0-B 持久化执行
K0-C 安全文件副作用
K0-D 故障恢复与控制
K0-E 运维与发布
```

v1.1.1 新增一项关键要求：

> **每个涉及执行语义的开发任务，必须先有已批准的机器可读契约。**

#### 34.1 K0-A：工程基础

| ID | 任务 | 前置依赖 | 主要交付 |
|---|---|---|---|
| K0-001 | 工程初始化与环境基线 | 无 | Python 工程、质量工具、基础文档 |
| K0-002 | 领域对象与 K0 Contract Pack | 001 | Task/Plan/Step/Attempt/Intent 契约 |
| K0-003 | Task/Step 状态机与不变量 | 002 | 状态转换服务、Guard 测试 |
| K0-004 | SQLite Schema 与迁移 | 002、003 | 数据库、组合外键、迁移测试 |

#### K0-002 新增验收

必须证明：

- 契约文件可解析。
- 状态集合唯一。
- 状态转换表完整。
- 合法和非法 Schema 示例可验证。
- 取消仲裁规则明确。
- Operation Intent 与 Attempt 的关系明确。
- 文件发布语义已形成正式 ADR。

#### K0-004 新增验收

必须证明：

- Step 不可能引用其他 Task 的 Plan。
- 活动 Plan 唯一。
- Operation Key 唯一。
- 同一 Step 不能有两个活动 Attempt。
- 外键检查实际开启。
- 非法跨对象引用被拒绝。
- 状态与事件可以原子提交。
- 数据库迁移可重复验证。
- 数据库版本升级和不兼容情况有明确处理。

#### 34.2 K0-B：持久化执行

| ID | 任务 | 前置依赖 | 主要交付 |
|---|---|---|---|
| K0-005 | 事务化 Task Repository | 003、004 | CAS、事件、幂等提交 |
| K0-006 | 确定性 Planner | 002、005 | `inventory.generate` Plan |
| K0-007 | 单执行器与 Worker Epoch | 005、006 | `flock`、执行器所有权 |
| K0-008 | CLI 基础接口 | 005、007 | 提交、查询、执行 |

#### K0-005 验收

- 相同请求不产生重复 Task。
- 相同 `request_key` 不同内容被拒绝。
- 非法状态转换被拒绝。
- 状态变化与事件同事务提交。
- 并发版本冲突可检测。
- 终态不可重新打开。

#### K0-007 验收

- 两个进程不能同时取得执行器锁。
- CLI 与后台服务不能同时执行。
- 旧 Worker Epoch 不能提交结果。
- 租约到期不触发盲目并发接管。
- 重启后先恢复，再领取新任务。

#### 34.3 K0-C：安全文件副作用

| ID | 任务 | 前置依赖 | 主要交付 |
|---|---|---|---|
| K0-009 | 路径与权限控制 | 001、002 | 安全文件访问 API |
| K0-010 | 输入快照与准备产物 | 006、009 | Snapshot、Manifest、Prepared Artifact |
| K0-011 | Operation Intent 与 Attempt | 005、007、010 | 持久化操作意图 |
| K0-012 | 原子不覆盖发布与验证 | 009—011 | 安全文件发布 |

#### K0-009 验收

- 路径穿越被拒绝。
- 符号链接被拒绝。
- 特殊文件被拒绝。
- 未授权目录访问被拒绝。
- 不支持的文件系统能力导致安全失败。
- 文件检查与打开之间的竞态有测试覆盖。

#### K0-010 验收

- 快照 Manifest 可验证。
- 快照文件哈希正确。
- 相同快照产生相同输出。
- 读取期间文件变化能够被处理。
- 快照未完成不得提交为有效。
- Prepared 文件未持久化不得被 Intent 引用。
- 孤儿产物可识别。

#### K0-011 验收

- Intent 在目标副作用前提交。
- Operation Key 稳定。
- Attempt 与 Intent 分离。
- 多次 Attempt 复用同一 Intent。
- 已提交 Intent 的关键参数不可静默修改。
- Prepared 文件缺失时不能继续执行。

#### K0-012 验收

- 目标不存在时可以发布。
- 目标已存在且内容相同时可以核对。
- 目标已存在且内容不同时不得覆盖。
- 检查后目标被其他进程创建时不得覆盖。
- 发布后崩溃可以恢复核对。
- 发布结果必须独立验证。
- macOS 目标文件系统能力必须经过真实测试。

#### 34.4 K0-D：恢复与控制

| ID | 任务 | 前置依赖 | 主要交付 |
|---|---|---|---|
| K0-013 | Reconciler 与 Recovery Manager | 011、012 | 观察与恢复决策 |
| K0-014 | 取消与紧急停止 | 007、013 | 持久化停止控制 |
| K0-015 | 故障注入与安全回归 | 008—014 | 故障测试矩阵 |

#### K0-013 验收

- 目标正确时可补记成功。
- 目标不存在时不声称历史上未执行。
- 目标冲突时阻塞。
- 证据不足时阻塞。
- 满足严格条件时允许安全重试。
- 不能安全判断时禁止盲目重试。
- 恢复事件完整记录。

#### K0-014 验收

- 取消请求持久化。
- 成功与取消的并发结果确定。
- 取消后不启动新的目标副作用。
- 取消后仍核对已开始操作。
- 紧急停止重启后保持。
- 重置停止标志需要可信操作。
- 重置后不会盲目恢复未核对任务。

#### 34.5 K0-E：运维与发布

| ID | 任务 | 前置依赖 | 主要交付 |
|---|---|---|---|
| K0-016 | 日志、健康与资源限制 | 007、013 | 可观测性基础 |
| K0-017 | B1/B2 备份与恢复 | 004、010、013 | 冻结协议、备份、恢复演练 |
| K0-018 | macOS 后台部署 | 014、016 | LaunchAgent、运维文档 |
| K0-019 | 长期运行与完整验收 | 015—018 | K0 测试报告 |
| K0-020 | 独立评审与正式发布 | 019 | K0 Release Candidate |

#### K0-017 验收

- B1 数据库备份一致。
- B2 包含必要产物。
- 备份期间不发生未受控写入。
- 备份中断不会产生被误认为有效的备份。
- 独立恢复环境能够启动。
- 数据库与产物引用一致。

#### K0-020 验收

- 所有 P0 开发任务通过。
- 无未解决 P0 缺陷。
- 所有规定故障点通过测试。
- 指定安全测试全部通过。
- 完成 24 小时稳定性测试。
- 完成备份恢复演练。
- 安装、停止、恢复、回滚文档可执行。
- 发布记录包含真实 Git Commit。
- 正式发布经过人工批准。

---

## 第三十部分：故障注入测试矩阵

### 35. 测试策略

v1.1.1 不再仅以“累计测试 100 次”作为可靠性依据。

采用：

**确定性故障点覆盖 + 竞态重复测试 + 长期运行测试。**

#### 35.1 必测故障点

| ID | 故障位置 | 预期行为 |
|---|---|---|
| FI-001 | Task 创建事务前 | 不产生不完整 Task |
| FI-002 | Task 创建事务后 | 重复请求返回原 Task |
| FI-003 | Plan 提交前 | 不执行未提交计划 |
| FI-004 | Step 领取后 | 恢复可识别中断状态 |
| FI-005 | Snapshot 写入中 | 不提交无效快照 |
| FI-006 | Snapshot 文件同步后 | 可识别孤儿产物 |
| FI-007 | Manifest 提交前 | 不使用不完整 Manifest |
| FI-008 | Prepared 文件写入中 | 不创建有效 Intent |
| FI-009 | Prepared 文件同步后 | 可识别孤儿 Prepared |
| FI-010 | Intent 事务提交前 | 不执行目标副作用 |
| FI-011 | Intent 事务提交后 | 恢复可识别待执行操作 |
| FI-012 | Attempt 创建后 | 不重复创建活动 Attempt |
| FI-013 | 文件发布前 | 满足条件时安全重试 |
| FI-014 | 文件发布后、结果提交前 | 核对成功后补记结果 |
| FI-015 | 目标冲突时 | 不覆盖，进入 BLOCKED |
| FI-016 | 验证中 | 重启后重新验证 |
| FI-017 | SUCCEEDED 提交后 | 不重复执行 |
| FI-018 | 取消请求提交后 | 不启动新副作用 |
| FI-019 | 紧急停止提交后 | 重启后保持停止 |
| FI-020 | 备份冻结中 | 不生成错误的有效备份 |
| FI-021 | B2 文件复制中 | 未完成备份无效 |
| FI-022 | 备份发布前 | 不接受临时备份 |
| FI-023 | 数据库繁忙 | 有界重试或明确失败 |
| FI-024 | 磁盘空间不足 | 保留可恢复状态 |
| FI-025 | Worker 异常退出 | 新执行器先核对旧工作 |
| FI-026 | Mac 休眠和唤醒 | 不盲目重复调度 |
| FI-027 | 目标文件被外部修改 | 检测冲突 |
| FI-028 | Prepared 文件被破坏 | 阻塞执行 |
| FI-029 | 两个执行器同时启动 | 最多一个取得执行权 |
| FI-030 | 成功提交与取消请求竞争 | 结果符合事务顺序 |

#### 35.2 测试要求

每个故障点必须：

1. 能被确定性触发。
2. 保存故障前数据库状态。
3. 保存故障前文件状态。
4. 注入故障。
5. 重启或执行恢复。
6. 检查最终状态。
7. 检查事件记录。
8. 检查目标副作用。
9. 检查是否存在重复操作。
10. 生成机器可读测试结果。

竞态测试可以重复执行。

但不能用“重复 100 次未失败”代替关键故障点覆盖。

---

## 第三十一部分：测试等级与发布门禁

### 36. 测试等级

#### T1：Unit Tests

验证：

- 数据对象。
- 状态转换。
- Guard。
- 哈希。
- Schema。
- 路径验证。
- 恢复决策。

#### T2：Contract Tests

验证：

- JSON Schema。
- YAML 契约。
- 数据库约束。
- API 响应格式。
- 状态集合一致性。

#### T3：Integration Tests

验证：

- SQLite 事务。
- 文件系统操作。
- Intent 与 Attempt。
- 状态与事件。
- 执行器锁。

#### T4：Fault Injection

验证：

- 崩溃。
- 重启。
- 结果核对。
- 安全重试。
- 取消。
- 紧急停止。

#### T5：Security Tests

验证：

- 越权路径。
- 符号链接。
- 非法工具调用。
- 非法状态转换。
- 重复副作用。
- 审批绕过，K1 起。

#### T6：Long-Running Tests

验证：

- 长时间任务处理。
- 内存和文件描述符使用。
- 数据库稳定性。
- 日志增长。
- 休眠与唤醒。
- 异常恢复。

#### 36.1 K0 质量门槛

| 指标 | K0 发布要求 |
|---|---|
| P0 缺陷 | 0 个未解决 |
| 必需契约测试 | 100% 通过 |
| 必需安全测试 | 100% 通过 |
| 指定故障点测试 | 100% 通过 |
| 黄金任务测试 | 100% 通过 |
| 非预期覆盖事件 | 测试中 0 次 |
| 非授权副作用 | 测试中 0 次 |
| 24 小时稳定性测试 | 完成并报告 |
| B2 恢复演练 | 成功 |
| Git 工作区 | 发布时状态明确 |
| 发布审批 | 必须有人工决定 |

这里的“0 次”仅表示**规定测试范围内没有观察到**。

不能被解释为生产环境中的绝对保证。

---

## 第三十二部分：AI 开发协作协议

### 37. 两种工作模式

#### Bootstrap Mode

适用于首次创建仓库。

AI 必须：

1. 阅读 Master Plan。
2. 检查目标目录。
3. 检查已有文件。
4. 检查 Git。
5. 检查 Python 环境。
6. 确认用户授权的修改范围。
7. 初始化工程。
8. 创建项目管理文件。
9. 执行基础测试。
10. 生成真实开发报告。

不得假定目录为空。

不得覆盖已有用户代码或 Git 历史。

#### Normal Development Mode

适用于已初始化仓库。

AI 必须先读取：

```text
AGENTS.md
PROJECT_STATE.md
DECISIONS_REQUIRED.md
planning/BACKLOG.md
docs/00_MASTER_PLAN.md
当前任务卡
相关契约
相关 ADR
```

然后检查：

```text
Git 分支
Git 工作区
现有代码
相关测试
依赖版本
未解决缺陷
```

文档与代码冲突时，先报告，不自行选择最方便的解释。

### 38. 规范权威顺序

建议：

```text
人工正式批准的变更
        ↓
安全与权限规范
        ↓
已批准 ADR
        ↓
正式数据和接口契约
        ↓
Master Plan
        ↓
阶段规格
        ↓
开发任务卡
        ↓
临时建议
```

其中：

人工批准的变更必须形成记录。

不能把普通聊天中的一句建议自动视为已批准的架构变更。

如果正式规范冲突：

1. 停止受影响工作。
2. 记录冲突。
3. 分析影响。
4. 提出修订方案。
5. 请求人工决策。
6. 更新正式文档。
7. 再继续实施。

### 39. AI 工程角色

| 角色 | 职责 |
|---|---|
| Architect AI | 架构、契约和技术决策建议 |
| Developer AI | 编码、测试、修复和开发报告 |
| Reviewer AI | 独立审查代码、测试和安全 |
| Human Owner | 目标确定、重大变更和发布批准 |

初期允许同一 AI 在不同阶段承担设计与开发工作。

但权限、密钥、发布门禁和核心安全修改不得由同一 AI 自我批准。

### 40. AI 标准开发流程

```text
读取项目状态
      ↓
确认当前任务
      ↓
检查任务依赖
      ↓
读取正式契约
      ↓
检查真实代码
      ↓
制定实施计划
      ↓
确认修改范围
      ↓
编写最小实现
      ↓
编写测试
      ↓
运行测试
      ↓
运行质量检查
      ↓
检查 Git Diff
      ↓
生成开发报告
      ↓
独立评审
      ↓
修复问题
      ↓
更新项目状态
      ↓
提交 Git
      ↓
进入下一任务
```

默认一次主要开发交付对应一张任务卡。

不得因为 AI 一次可以生成大量代码，就跳过任务依赖和测试门禁。

---

## 第三十三部分：开发任务卡

### 41. 标准模板

保存为：

```text
planning/TASK_TEMPLATE.md
```

```markdown
# JARVIS Development Task

## 基本信息
Task ID:
Phase:
Priority:
Status:
Dependencies:

## 开发目标

## 背景

## 前置条件

## 允许修改范围

## 禁止修改范围

## 输入契约

## 输出契约

## 相关 ADR

## 实施步骤

## 异常处理

## 安全要求

## 验收标准
AC-001:
AC-002:
AC-003:

## 测试要求

## 交付文件

## 回滚方式

## 完成证据
测试结果：
Git Commit：
评审报告：

## 最终状态
```

#### 41.1 开发任务状态

```text
TODO
READY
IN_PROGRESS
IMPLEMENTED
IN_REVIEW
VERIFIED
DONE
```

异常状态：

```text
BLOCKED
REWORK_REQUIRED
FAILED
CANCELED
```

#### 状态规则

- `IMPLEMENTED`：代码已实现，但不代表全部验收通过。
- `IN_REVIEW`：已经提交证据，等待评审。
- `VERIFIED`：通过规定测试和评审。
- `DONE`：代码、测试、文档、状态和 Git 记录完整。

**AI 写完代码后不得直接标记 DONE。**

---

## 第三十四部分：开发报告与复盘

### 42. 开发报告模板

保存为：

```text
docs/reports/DEVELOPMENT_REPORT_TEMPLATE.md
```

```markdown
# JARVIS Development Report

## Task ID

## 实施目标

## 实际完成内容

## 文件变更
新增：
修改：
删除：

## 关键技术实现

## 契约影响

## 安全影响

## 实际测试命令

## 实际测试结果

## 未执行测试

## 验收检查
AC-001:
AC-002:
AC-003:

## 已知问题

## 未解决风险

## 回滚方案

## Git 信息
分支：
提交：
工作区状态：

## 评审结论

## 下一任务
```

不得伪造：

- 测试命令。
- 测试结果。
- Git Commit。
- 发布版本。
- 文件哈希。
- 性能指标。

#### 42.1 PROJECT_STATE.md

至少包含：

```text
当前项目版本
当前开发阶段
当前任务
已完成任务
进行中任务
阻塞任务
最近通过的测试
最近一次发布
数据库 Schema 版本
当前 Contract Pack 版本
已知 P0/P1 缺陷
未解决 ADR
下一步任务
```

#### 42.2 阶段复盘

每个阶段形成：

```text
docs/retrospectives/
    K0_RETROSPECTIVE.md
    K1_RETROSPECTIVE.md
    K2_RETROSPECTIVE.md
```

复盘必须回答：

- 原计划完成了什么？
- 哪些验收未通过？
- 哪些设计假设被推翻？
- 哪些故障被发现？
- 哪些风险仍未解决？
- 是否引入技术债？
- 实际开发成本是多少？
- 下一阶段是否应该启动？

---

## 第三十五部分：发布与回滚

### 43. 版本管理

建议使用：

```text
Application Version
Database Schema Version
Contract Pack Version
Configuration Schema Version
```

例如：

```text
JARVIS Application: 0.1.0
Database Schema: 1
Contract Pack: 1.0
Configuration Schema: 1
```

不能将这些版本混为一个。

#### 43.1 Git 分支

```text
main
 ├── feature/K0-001-project-init
 ├── feature/K0-003-state-machine
 ├── feature/K0-011-operation-intent
 └── fix/K0-013-recovery
```

提交格式：

```text
feat(K0-003): implement task state machine
fix(K0-013): reconcile interrupted file publish
test(K0-014): add cancellation race tests
docs(adr): define atomic no-clobber publishing
```

#### 43.2 发布包

至少包含：

```text
release/
    manifest.json
    CHANGELOG.md
    TEST_REPORT.md
    SECURITY_REPORT.md
    KNOWN_ISSUES.md
    INSTALL.md
    OPERATIONS.md
    ROLLBACK.md
```

`manifest.json` 示例：

```json
{
  "project": "JARVIS",
  "version": "0.1.0",
  "phase": "K0",
  "git_commit": "<actual_commit>",
  "database_schema_version": 1,
  "contract_pack_version": "1.0",
  "release_status": "candidate",
  "artifact_sha256": "<actual_hash>"
}
```

占位符必须由真实构建过程替换。

#### 43.3 发布流程

```text
完成开发
    ↓
契约测试
    ↓
单元测试
    ↓
集成测试
    ↓
故障注入
    ↓
安全测试
    ↓
长期运行测试
    ↓
备份恢复演练
    ↓
独立评审
    ↓
人工批准
    ↓
发布候选
    ↓
部署验证
    ↓
正式发布
```

#### 43.4 回滚

必须区分：

**应用回滚：** 恢复旧版本代码和依赖。

**配置回滚：** 恢复经过验证的配置。

**数据恢复：** 根据数据库迁移兼容性和备份执行专门恢复。

不得假定应用回滚后，新数据库 Schema 一定兼容旧代码。

---

## 第三十六部分：开发时间规划

### 44. 建议时间表

以下是单人主导、AI 辅助、没有成熟代码基础情况下的粗略估算。

| 时间 | 目标 |
|---|---|
| 第 1 周 | K0-001、Contract Pack 初稿 |
| 第 2 周 | 状态机、数据库与事务化仓储 |
| 第 3—4 周 | 执行器、快照、Prepared Artifact |
| 第 5—6 周 | Intent、文件发布、结果验证 |
| 第 7—8 周 | 故障恢复、取消、紧急停止 |
| 第 9—10 周 | 备份、launchd、完整验收 |
| 第 11—16 周 | K1 模型网关、规划、审批、预算 |
| 第 17—20 周 | K1 安全回归与发布验证 |
| 后续 | K2 记忆与人格 |
| 再后续 | K3 有限自主 |
| 长期阶段 | K4 受控改进 |

这不是固定交付承诺。

实际阶段切换由质量门禁决定。

如果 K0 的文件发布和恢复协议尚未通过测试，不允许为了赶进度提前启动真实模型工具执行。

---

## 第三十七部分：风险登记

### 45. 主要风险

| 编号 | 风险 | 等级 | 缓解措施 |
|---|---|---|---|
| R-001 | 重复外部副作用 | P0 | Intent、稳定 Key、Reconciliation |
| R-002 | 状态与事件不一致 | P0 | 事务化转换 |
| R-003 | 文件覆盖竞态 | P0 | 原子不覆盖发布 |
| R-004 | 文件与数据库崩溃窗口 | P0 | Prepared Artifact、Intent、恢复协议 |
| R-005 | 输入快照不一致 | P0 | 明确一致性范围、受控读取 |
| R-006 | 路径穿越与符号链接 | P0 | 安全目录句柄、权限测试 |
| R-007 | 旧执行器提交结果 | P0 | 文件锁、Epoch、CAS |
| R-008 | 取消与成功竞争 | P0 | 事务化仲裁 |
| R-009 | 备份缺少必要产物 | P0 | B2 冻结协议 |
| R-010 | 模型绕过审批 | P0-K1 | 独立身份边界 |
| R-011 | 重新规划重复副作用 | P0-K1 | 历史副作用映射 |
| R-012 | Prompt Injection | P0-K1 | 信任边界、安全评测 |
| R-013 | 记忆错误升级为事实 | P1 | 来源与确认状态 |
| R-014 | 主动事件重复触发 | P1 | Event Dedup、稳定 Task Key |
| R-015 | 自我改进破坏内核 | P0-K4 | 隔离、独立评审、人工批准 |
| R-016 | macOS 平台语义差异 | P1 | 能力探测、真实环境测试 |
| R-017 | AI 接力开发理解不一致 | P1 | Contract Pack、ADR、任务卡 |
| R-018 | 过度工程化 | P1 | K0 严格收敛范围 |
| R-019 | 磁盘空间耗尽 | P1 | 资源限制、产物保留策略 |
| R-020 | 进度估算过于乐观 | P1 | 滚动规划、阶段门禁 |

---

## 第三十八部分：强制停止条件

### 46. STOP 规则

出现以下情况时，AI 必须停止受影响的工作：

```text
STOP-001 需要修改未授权真实用户数据
STOP-002 需要提升系统权限
STOP-003 需要绕过安全策略
STOP-004 出现无法解释的数据丢失
STOP-005 出现无法核对的重复副作用
STOP-006 状态机与数据库契约发生重大冲突
STOP-007 需要执行不可逆数据库迁移
STOP-008 需要修改核心发布门禁
STOP-009 需要把生产密钥提供给测试环境
STOP-010 需要部署未通过验收的版本
STOP-011 AI 无法确认真实仓库状态
STOP-012 关键测试失败且原因不明
STOP-013 目标文件系统不支持所需安全语义
STOP-014 准备产物与 Intent 记录不一致
STOP-015 发现未授权的活动执行器
STOP-016 备份与恢复结果无法验证
```

停止时必须：

1. 保存可保存的当前状态。
2. 记录问题。
3. 保留证据。
4. 报告影响范围。
5. 提出处理方案。
6. 请求人工决策。

不得通过无限重试、扩大权限或隐藏错误绕过阻塞。

---

## 第三十九部分：v1.1.1 待确认的 ADR

### 47. 决策登记

本版本已经确定了主要设计语义，但仍有少量平台相关问题必须通过真实测试或 ADR 冻结。

| ADR | 决策问题 | 当前建议 | 确认时机 |
|---|---|---|---|
| ADR-001 | 项目仓库位置 | 用户指定独立目录 | K0-001 |
| ADR-002 | Python 版本 | 3.12 | K0-001 |
| ADR-003 | SQLite Schema 基线 | 本规划关系模型 | K0-004 |
| ADR-004 | macOS 文件安全 API | 能力检测，失败时拒绝 | K0-009 |
| ADR-005 | 快照一致性等级 | 文件级确定性，不承诺全目录原子快照 | K0-010 |
| ADR-006 | Prepared Artifact 持久化 | 文件与目录同步、平台验证 | K0-010 |
| ADR-007 | 原子不覆盖发布 | 同文件系统硬链接发布候选 | K0-012 |
| ADR-008 | 文件系统断电恢复等级 | 根据实际 macOS/APFS 测试确定 | K0-012 |
| ADR-009 | 单执行器互斥 | `flock` + Worker Epoch | K0-007 |
| ADR-010 | B2 备份冻结 | Quiesce + SQLite Backup + Artifact Manifest | K0-017 |
| ADR-011 | launchd 运行模式 | 用户级 LaunchAgent | K0-018 |
| ADR-012 | 首个模型供应商 | K1 开始时选择 | K1-003 |
| ADR-013 | 审批身份边界 | 可信独立入口 | K1-007 |
| ADR-014 | 模型预算 | 用户批准的限额 | K1-008 |
| ADR-015 | K4 隔离环境 | 经验证的受限执行环境 | K4-004 |

其中 ADR-004、ADR-006、ADR-007 和 ADR-008 是 K0 文件安全协议的重要前置条件。

**如果实际平台测试证明候选方案无法满足要求，必须修订 ADR 和对应契约，而不是让 AI 私自换用覆盖式文件写入。**

---

## 第四十部分：v1.1.0 → v1.1.1 变更记录

### 48. 修订汇总

| 变更编号 | 修订内容 | 对应评审问题 |
|---|---|---|
| CHG-001 | 补充 Task 失败与终止路径 | P0-05 |
| CHG-002 | 正式定义 Step 状态与跨对象不变量 | P0-05 |
| CHG-003 | 增加组合外键与活动 Plan 唯一性 | P0-06 |
| CHG-004 | 拆分通用 Intent 和 FileWriteIntent | P0-07 |
| CHG-005 | 明确 Prepared Artifact 生命周期 | P0-02 |
| CHG-006 | 明确 SQLite 与文件系统非原子边界 | P0-02 |
| CHG-007 | 修订输入快照一致性保证 | P0-04 |
| CHG-008 | 废除不安全的先检查后 `os.replace()` 方案 | P0-01 |
| CHG-009 | 引入原子不覆盖发布协议 | P0-01 |
| CHG-010 | 将 Reconciliation 拆分为 Observation 和 Decision | P0-03 |
| CHG-011 | 明确 `flock`、Lease、Epoch 的职责 | P0-08 |
| CHG-012 | 明确取消与成功的事务仲裁 | P0-09 |
| CHG-013 | 明确紧急停止重置规则 | P0-09 |
| CHG-014 | 建立 B2 备份冻结协议 | P0-10 |
| CHG-015 | 明确 K1 审批身份边界 | P0-11 |
| CHG-016 | 明确 K1 重新规划与历史副作用映射 | P0-12 |
| CHG-017 | 明确 K0 同步单执行器优先 | P1-02 |
| CHG-018 | 将 `inventory.generate` 定义为 Task Kind | P1-03 |
| CHG-019 | 补充事件顺序与审计主体 | P1-04 |
| CHG-020 | 增加 Artifact Retention Policy | P1-05 |
| CHG-021 | 增加记忆验证状态 | P1-06 |
| CHG-022 | 增加记忆评测门禁 | P1-07 |
| CHG-023 | 明确事件幂等与任务幂等关系 | P1-08 |
| CHG-024 | 将 K4 拆分为技能改进和核心改进 | P1-09 |
| CHG-025 | 强制交付 K0 Contract Pack | P1-10 |
| CHG-026 | 将故障测试改为故障点覆盖驱动 | P1-11 |
| CHG-027 | 区分测试通过与生产可靠性保证 | P1-12 |

---

## 第四十一部分：v1.1.1 文档冻结门禁

### 49. 正式冻结条件

v1.1.1 规划文本形成后，还必须完成以下工作，才能将 K0 核心执行契约标记为 `FROZEN`。

| ID | 要求 |
|---|---|
| DOC-R01 | Task 状态转换表完整 |
| DOC-R02 | Step 状态转换表完整 |
| DOC-R03 | 跨对象不变量可验证 |
| DOC-R04 | SQLite 组合外键测试通过 |
| DOC-R05 | 活动 Plan 唯一性测试通过 |
| DOC-R06 | Prepared Artifact 协议明确 |
| DOC-R07 | 输入快照保证范围明确 |
| DOC-R08 | 原子不覆盖发布经过平台验证 |
| DOC-R09 | 文件同步与断电保证范围明确 |
| DOC-R10 | Reconciliation 结果契约可解析 |
| DOC-R11 | 重试决策有确定性测试 |
| DOC-R12 | Worker 锁与 Epoch 协议可测试 |
| DOC-R13 | 取消仲裁具有竞态测试 |
| DOC-R14 | 紧急停止恢复流程明确 |
| DOC-R15 | B2 备份冻结协议可执行 |
| DOC-R16 | K0 Contract Pack 完整 |
| DOC-R17 | 契约与代码状态枚举一致 |
| DOC-R18 | 所有 K0 任务映射到验收测试 |
| DOC-R19 | 未解决 ADR 已登记 |
| DOC-R20 | 无未解决的阻断性规范冲突 |

**本次在对话中完成的是 v1.1.1 主规划文本，不代表以上机器可读契约文件已经创建或通过测试。**

---

## 第四十二部分：正式开发启动任务

### 50. JARVIS-K0-001

- **任务名称：** 工程初始化与环境基线
- **优先级：** P0
- **状态：** READY
- **依赖：** 无
- **执行模式：** Bootstrap Mode

#### 50.1 开发目标

建立 JARVIS 正式 Python 工程和 AI 接力开发基础。

#### 50.2 输入

- 本《JARVIS Engineering Master Plan v1.1.1》。
- 用户指定的项目目录。
- 当前 Mac 环境。
- 已有文件与 Git 历史。

#### 50.3 实施步骤

1. 检查目标目录。
2. 检查已有 Git 仓库。
3. 检查 macOS 版本。
4. 检查 Python 3.12。
5. 检查 uv。
6. 初始化 `pyproject.toml`。
7. 配置 Ruff。
8. 配置 Pyright。
9. 配置 pytest。
10. 建立必要目录。
11. 创建 `AGENTS.md`。
12. 创建 `PROJECT_STATE.md`。
13. 创建 `DECISIONS_REQUIRED.md`。
14. 创建 `planning/BACKLOG.md`。
15. 保存 v1.1.1 Master Plan。
16. 建立环境检查脚本。
17. 建立基础测试。
18. 运行质量检查。
19. 生成开发报告。
20. 报告 K0-002 的前置条件。

#### 50.4 禁止事项

不得：

- 删除已有用户文件。
- 覆盖已有 Git 历史。
- 修改系统级 Python。
- 安装不必要的全局依赖。
- 接入真实模型。
- 访问私人数据。
- 提前开发 K1—K4。
- 将运行数据提交到 Git。

#### 50.5 验收标准

```text
AC-001 项目目录正确
AC-002 Python 版本符合基线
AC-003 uv.lock 有效
AC-004 Ruff 检查通过
AC-005 Pyright 检查通过
AC-006 pytest 至少一个真实测试通过
AC-007 Git 状态明确
AC-008 AGENTS.md 已建立
AC-009 PROJECT_STATE.md 已建立
AC-010 DECISIONS_REQUIRED.md 已建立
AC-011 BACKLOG.md 已建立
AC-012 v1.1.1 Master Plan 已保存
AC-013 未修改未授权用户数据
AC-014 开发报告包含真实证据
```

#### 50.6 建议测试命令

```bash
uv sync --frozen

uv run ruff check .

uv run pyright

uv run pytest -q
```

具体命令必须与实际 `pyproject.toml` 配置一致。

不能在没有真实执行的情况下报告测试通过。

---

## 第四十三部分：首次交给 AI 编程代理的指令

以下内容可以直接复制给具有文件读写和终端操作能力的 AI 编程代理。

```text
你现在正式接手 JARVIS 自研项目。

当前工程主规格书：

JARVIS Engineering Master Plan v1.1.1

项目目标：

建设一个独立于具体大语言模型、
具有可靠任务管理、受控工具执行、
持久化状态、故障恢复、长期记忆、
有限自主行为和受控改进能力的
个人 AI 智能体系统。

当前技术基线：

Python 3.12
SQLite
uv
Pydantic
Typer
pytest
Ruff
Pyright
Git
macOS launchd

架构原则：

1. 控制内核独立于模型。
2. 模型输出不是授权。
3. 工具副作用必须受控。
4. 重要状态必须持久化。
5. 操作结果未知时不得盲目重试。
6. 任务成功必须有验证证据。
7. 不得绕过状态机。
8. 不得绕过安全策略。
9. 不得伪造测试结果。
10. 不得未经批准发布。

当前只允许执行：

JARVIS-K0-001
工程初始化与环境基线。

执行要求：

一、进入 Bootstrap Mode。

二、完整阅读 v1.1.1 Master Plan。

三、检查真实 Mac 环境、目标目录、
Python、Git 和已有文件。

四、不得假定目录为空。

五、确认 K0-001 的开发范围和验收标准。

六、输出本次实施计划及预计修改文件。

七、在授权范围内初始化工程。

八、真实运行质量检查。

九、生成标准开发报告。

十、更新 PROJECT_STATE.md 和 BACKLOG.md。

十一、报告下一张任务卡 K0-002。

特别注意：

本版本新增 K0 Contract Pack。

在开发状态机、数据库、文件发布、
Operation Intent 和 Recovery 之前，
必须先冻结对应机器可读契约。

不得自行发明与 Master Plan 冲突的执行语义。

不得一次性开发整个 JARVIS。

不得提前接入真实模型。

不得未经批准访问或修改用户私人数据。

如果遇到阻塞性问题，
停止受影响操作并报告。

现在开始检查项目环境。
```

---

## 第四十四部分：最终工程判定

### 51. v1.1.1 的正式定位

《JARVIS Engineering Master Plan v1.1.1》保留了 v1.1 的总体架构，并针对第三轮评审发现的关键问题完成了设计层面的修订。

它明确回答了：

**任务如何管理？**

通过 Task、Plan、Step、Attempt 和 Operation Intent 的分层，以及事务化状态转换。

**文件如何安全写入？**

通过受控快照、Prepared Artifact、持久化 Intent、原子不覆盖发布和独立验证。

**崩溃后如何恢复？**

通过稳定操作身份、实际结果观察、确定性恢复决策和安全重试。

**如何避免重复执行？**

通过请求幂等、Operation Key、Attempt 记录、Worker Epoch 和 Reconciliation。

**如何停止系统？**

通过持久化取消请求、确定性事务仲裁和独立紧急停止机制。

**如何长期运行？**

通过单执行器、launchd、健康检查、资源限制、备份和恢复演练。

**如何让 AI 持续开发？**

通过 Master Plan、Contract Pack、任务卡、测试证据、开发报告、ADR、复盘和发布门禁。

**如何逐步获得认知和自主能力？**

在 K0 可靠内核之上依次建设 K1 模型执行、K2 记忆人格、K3 有限自主和 K4 受控改进。

### 52. 当前状态

| 项目 | 当前状态 |
|---|---|
| v1.1.1 主规划文本 | **已完成修订** |
| 第三轮评审问题 | **已纳入设计修订** |
| 总体架构 | **建议保留** |
| K0-001 | **允许启动** |
| K0 Contract Pack | 尚未实际创建 |
| SQLite 正式迁移 | 尚未实际实现 |
| macOS 文件发布协议 | 尚未经过平台测试 |
| 故障恢复 | 尚未经过代码验证 |
| K0 发布 | 尚未验收 |
| JARVIS 生产就绪 | **尚未达到** |

#### 最终开发原则

**先冻结契约，再实现内核；先验证副作用，再接入模型；先证明恢复可靠，再增加自主性。**

这应当成为 JARVIS 后续所有开发工作的共同约束。

### 53. 下一步执行顺序

```text
JARVIS Engineering Master Plan v1.1.1
                 ↓
        K0-001 工程初始化
                 ↓
       K0 Contract Pack v1.0
                 ↓
       K0-002 领域对象契约
                 ↓
       K0-003 状态机与不变量
                 ↓
       K0-004 SQLite 数据契约
                 ↓
       K0-005 事务化持久化
                 ↓
       K0-006 确定性 Planner
                 ↓
       K0-007 单执行器
                 ↓
       K0-008 CLI
                 ↓
       K0-009 文件权限
                 ↓
       K0-010 快照与准备产物
                 ↓
       K0-011 Operation Intent
                 ↓
       K0-012 原子文件发布
                 ↓
       K0-013 故障恢复
                 ↓
       K0-014 取消与紧急停止
                 ↓
       K0-015 故障与安全测试
                 ↓
       K0-016 日志与健康
                 ↓
       K0-017 B1/B2 备份
                 ↓
       K0-018 macOS 部署
                 ↓
       K0-019 长期运行验收
                 ↓
       K0-020 独立评审与发布
                 ↓
           K1 模型执行
                 ↓
           K2 记忆人格
                 ↓
           K3 有限自主
                 ↓
           K4 受控改进
```
