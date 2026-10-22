# PLAN / ATTEMPT 字段提案（DRAFT / NOT_APPROVED）

- 效力：**DRAFT / NOT_APPROVED**。非契约、非冻结、非实现证据。
- 未改动：`contracts/`、`docs/adr/`、迁移、Schema、源码、测试、门禁、台账，均零写入。
- 唯一写入：本文件 `docs/design/PLAN_ATTEMPT_FIELD_PROPOSAL.md`（无子目录、无递归、无其它文件）。
- 未关闭任何项：D-01 / D-02 / D-04 / D-07 / D-13 / D-15，`RECOVERY_PROTOCOL.md` OPEN-06 / OPEN-10，DOC-R03 / R10 / R11 / R16。
- 状态占位不合法化：本文不新增、不命名、不推荐任何状态取值。D-01 / D-02 未闭合前，Plan 与 Attempt **无状态集合来源**；`state TEXT NOT NULL` 只是已发布列，不构成枚举授权。
- 状态集合不得跨对象复用：TASK 与 STEP 均含 `NEEDS_RECONCILIATION` / `RETRY_WAIT`；Intent 的 `NEEDS_RECONCILIATION` 来源为 `contracts/operation_intent.schema.json`，Intent 集合不含 `RETRY_WAIT`。同名值来自不同对象契约；Attempt 的命名重叠由 D-02 裁定，不得据此推定 Plan / Attempt 取值。
- §6 候选恢复关联**仅为候选**：不含实现、不含 SQL、不含代码、不含迁移。

## 1. 读取基线（fresh read，2026-10-13）

| 来源 | 位置 | 用途 |
|---|---|---|
| Master Plan v1.1.1 | §10.1 `task_plans` L885–900、`task_steps` L914–933、`operation_intents` L943–962、`step_attempts` L997–1018；§10.2 L1103–1114；L906–908 / L1024–1026 / L1029 | 已发布列、约束、恢复旧 Attempt 前提 |
| `docs/design/DOMAIN_CONTRACT_DESIGN.md` v4 | §2 职责边界、§3 字段映射、§9 决定项（D-01/02/04/07/13/15）、§10 效力 | 语义层、缺列、未决 ID |
| `docs/design/RECOVERY_PROTOCOL.md` | §9 OPEN-06、OPEN-10 | 恢复侧未决项 |
| `contracts/` | `task_states.yaml`（TASK，10 态 + 3 终态）、`step_states.yaml`（STEP，11 态 + 3 终态）、`operation_intent.schema.json`（Intent，6 态） | 既有状态集合唯一来源（MP L1114） |

计数口径：本文只登记 **DDL 原文实测列**，不补列、不占位、不推定。

## 2. 已发布列（逐字段）

列义：`MP或DDL来源`＝DDL 行号或契约文件；`现状`＝已发布 / 已发布列但集合无来源 / 无列；`未决ID` 为空即当前无未决项。

### 2.1 `task_plans`（Plan，6 列；MP L885–900）

| 对象/字段 | MP或DDL来源 | 现状 | 语义层 | 未决ID | 候选方向 |
|---|---|---|---|---|---|
| Plan.id | L886 `TEXT PRIMARY KEY` | 已发布 | 身份 | — | 无 |
| Plan.task_id | L888 `NOT NULL`；FK→`tasks(id)` | 已发布 | 归属 | — | 无 |
| Plan.plan_version | L889 `INTEGER NOT NULL`；UNIQUE(task_id, plan_version) | 已发布 | 计划版本轴（非 CAS 版本） | D-04 | 是否纳入 `plan_digest` 覆盖范围（仅候选） |
| Plan.state | L890 `TEXT NOT NULL`；部分唯一索引 `state='ACTIVE'`（L906–908） | 已发布列，**集合无来源** | 激活状态 | D-01 | 枚举取值须由先生裁定；本文件不提名 |
| Plan.plan_digest | L892 `NOT NULL` | 已发布 | 计划内容摘要 | D-04 | 覆盖范围候选：是否含 `plan_version`、激活状态、§5.2「创建依据」 |
| Plan.created_at_ms | L893 `NOT NULL` | 已发布 | 时间轴 | — | 无 |
| （缺列）创建依据 / 历史保留 | DOMAIN §3 Plan 行 | 无列 | 计划依据 | D-04 | 并入 D-04 覆盖范围裁定 |
| （缺列）version | DOMAIN §4 并发轴段 | 无列 | 并发控制轴 | D-07 | 候选：新增 `version` 列 vs 其它并发轴（不推荐、不实现） |

### 2.2 `task_steps`（Step，8 列；MP L914–933）

> DDL 原文实测为 **8 列**（L915–926），包含 required 与 version；字段计数不包括表级 FK/UNIQUE 约束。

| 对象/字段 | MP或DDL来源 | 现状 | 语义层 | 未决ID | 候选方向 |
|---|---|---|---|---|---|
| Step.id | L915 `TEXT PRIMARY KEY` | 已发布 | 身份 | — | 无 |
| Step.task_id | L917 `NOT NULL` | 已发布 | 归属 | — | 无 |
| Step.plan_id | L918 `NOT NULL`；组合 FK→`task_plans(id, task_id)`（L928–929） | 已发布 | 归属（与 Task 同源约束） | — | 无 |
| Step.ordinal | L920 `INTEGER NOT NULL`；UNIQUE(plan_id, ordinal) | 已发布 | 顺序 | — | 无 |
| Step.logical_effect_id | L921 `TEXT`（**可空**） | 已发布 | 逻辑副作用身份 | D-11 | K0 是否必填（仅候选；D-11 不在本轮 6 项内，不关闭） |
| Step.state | L923 `TEXT NOT NULL` | 已发布列，集合来源 `step_states.yaml` | 执行状态 | — | 不改集合、不跨对象复用 |
| Step.required | L924 `INTEGER NOT NULL DEFAULT 1` | 已发布 | 必需标记 | — | 无 |
| Step.version | L926 `INTEGER NOT NULL DEFAULT 0` | 已发布 | 并发版本（CAS，§10.3） | — | 无 |
| （缺列）§5.3 五要素 | DOMAIN §3 Step 缺列行 | 无列 | 输入/类型/前置/预期/验证 | D-08 | 候选：并入 `plan_digest` 覆盖范围 vs 新增列 |

### 2.3 `operation_intents`（Intent，9 列；MP L943–962）

| 对象/字段 | MP或DDL来源 | 现状 | 语义层 | 未决ID | 候选方向 |
|---|---|---|---|---|---|
| Intent.id | L944 `TEXT PRIMARY KEY`；UNIQUE(id, step_id) | 已发布 | 身份 | — | 无 |
| Intent.step_id | L946 `NOT NULL UNIQUE`；FK→`task_steps(id)` | 已发布 | 归属 + 基数（每 Step 至多一个，L965–969） | — | 无 |
| Intent.operation_key | L947 `NOT NULL UNIQUE` | 已发布 | 稳定逻辑操作键 | D-06 | 构造与编码算法（仅候选） |
| Intent.operation_type | L949 `NOT NULL` | 已发布 | 操作类型（K0 单值） | — | 无 |
| Intent.tool_name | L950 `NOT NULL` | 已发布 | 工具身份 | — | 无 |
| Intent.tool_version | L951 `NOT NULL` | 已发布 | 工具版本 | — | 无 |
| Intent.arguments_digest | L953 `NOT NULL`；契约侧 SHA-256 hex | 已发布 | 规范化参数摘要（参数原文不落库） | D-05 | 参数集合边界（仅候选；未闭合则 OPEN-02 亦不可闭合） |
| Intent.state | L954 `NOT NULL` | 已发布列，集合来源 `operation_intent.schema.json`（6 态） | 核对状态 | OPEN-11 | `NEEDS_RECONCILIATION` 终止条件未定；Attempt 命名重叠另属 D-02，不作为 Intent 字段未决归属；不改集合 |
| Intent.created_at_ms | L956 `NOT NULL` | 已发布 | 时间轴 | — | 无 |
| （缺列）version | DOMAIN §4 并发轴段 | 无列 | 并发控制轴 | D-07 | 候选：新增 `version` 列 vs 其它并发轴 |
| （缺列）预期结果 / 稳定证据通用承载 | DOMAIN §3 Intent 缺列行 | 无列 | 恢复证据 | D-16 | 候选：通用 Intent 侧字段 vs 依赖专属物化 |

### 2.4 `step_attempts`（Attempt，8 列；MP L997–1018）

| 对象/字段 | MP或DDL来源 | 现状 | 语义层 | 未决ID | 候选方向 |
|---|---|---|---|---|---|
| Attempt.id | L998 `TEXT PRIMARY KEY` | 已发布 | 身份 | — | 无 |
| Attempt.step_id | L1000 `NOT NULL`；FK→`task_steps(id)`；UNIQUE(step_id, attempt_no) | 已发布 | 归属 + 次序唯一 | — | 无 |
| Attempt.operation_id | L1001 `NOT NULL`；组合 FK→`operation_intents(id, step_id)`（L1014–1015） | 已发布 | 关联 Intent（复用，见 §4） | — | 不得改写成新字段 |
| Attempt.attempt_no | L1003 `INTEGER NOT NULL` | 已发布 | 尝试次序 | — | 无 |
| Attempt.worker_epoch | L1004 `INTEGER NOT NULL` | 已发布 | 全局 Worker 纪元（epoch / CAS） | OPEN-06 | 递增与旧 Epoch 拒提交的字段与 CAS 语义（仅候选；**与 D-15 对象不同**） |
| Attempt.state | L1006 `TEXT NOT NULL`；部分唯一索引 `state='ACTIVE'`（L1024–1026） | 已发布列，**集合无来源** | 尝试状态 | D-02 | L1029 至少暗示「已中断」「待核对」两值；命名与 Intent/Step 重叠须一并裁定 |
| Attempt.started_at_ms | L1008 `INTEGER NOT NULL` | 已发布 | 起止时间 | — | 无 |
| Attempt.finished_at_ms | L1009 `INTEGER`（**可空**） | 已发布 | 起止时间（未结束可空） | — | 无 |
| （缺列）worker_id / 执行器身份 | DOMAIN §3 Attempt 行（`step_attempts` 无 `worker_id`） | 无列 | 执行器身份 | D-15 | 候选：落 `step_attempts` vs `system_control` / Lease 侧（不实现） |
| （缺列）结果 / 错误 / 中断恢复证据 | DOMAIN §3 Attempt 缺列行 | 无列 | 结果与证据 | D-13 | 候选：`step_attempts` 新列 vs `task_events.payload_json`（须与 OPEN-10 同一次关闭） |

## 3. 缺失项登记（6 项，本轮不关闭）

| 未决ID | 缺失内容 | 承载对象 | 现状 | 候选方向（仅候选） |
|---|---|---|---|---|
| D-01 | Plan 状态枚举无任何契约来源 | Plan | `state` 列已发布，取值集合无来源 | 规范澄清；取值须先生裁定，本文件不提名 |
| D-02 | Attempt 状态枚举无来源；L1029 暗示「已中断」「待核对」 | Attempt | 同上 | 须与 Task/Step/Intent 同名值重叠一并裁定 |
| D-04 | `plan_digest` 覆盖范围 | Plan | 列已发布，边界未定 | 是否含 `plan_version`、激活状态、创建依据 |
| D-07 | 四个无 `version` 列对象的并发控制轴 | Plan / Intent / Attempt / FileWriteIntent | 四表均无 `version` 列 | 新增 `version` 列 vs 其它轴；禁「先查后改」（§10.3） |
| D-13 | Attempt 实际结果 / 错误 / 中断恢复证据承载 | Attempt / 恢复 | 无列 | `step_attempts` 新列 vs `task_events.payload_json` |
| D-15 | Attempt 执行器身份（`worker_id` 或等价） | Attempt | 无列 | 落 `step_attempts` vs `system_control` / Lease 侧 |

**D-15 ≠ OPEN-06**：`system_control.worker_epoch` 是被 Attempt 行引用的全局纪元，只承载 OPEN-06 的 epoch/CAS 语义，不能替代 per-Attempt 执行器身份；两者对象不同、相互独立，不得合并关闭。

## 4. 复用关系（不得写成新字段）

- `step_attempts.operation_id` + 组合 FK `(operation_id, step_id) → operation_intents(id, step_id)` 表达的是 **Attempt 复用同一 Step 的 Intent**，属既有关系，不是新字段、不是新表、不新增基数。
- `operation_intents.step_id UNIQUE` 与 PK/FK 只保证「每 Step 至多一个 Intent」，**不保证存在性**；存在性归 K0 目标设计与发布前流程 / Guard，不得由本文件声称。
- 因此：D-13 / D-15 的候选**不得**以「新增 Attempt→Intent 关联字段」或「复制 Intent 语义到 Attempt」的方式表达。

## 5. 状态占位与不改动声明

- 未新增、未命名、未推荐任何状态值；未把 `state TEXT NOT NULL` 读作枚举授权；未用占位符（如 `TBD` 取值）充当已定值。
- 未改 `contracts/task_states.yaml`、`step_states.yaml`、`operation_intent.schema.json` 及任何 Schema / ADR / 源码 / 测试 / 迁移。
- 未关闭 D-01 / D-02 / D-04 / D-07 / D-13 / D-15，未关闭 OPEN-06 / OPEN-10，未关闭 DOC-R03 / R10 / R11 / R16。
- 未声明冻结、批准、已验证、完成；未代独立评审出具结论。

## 6. 候选恢复关联（仅候选，不实现）

以下仅登记**候选关联方向**，不含实现、不含 SQL、不含代码，且须在 D-13 / D-15 / D-02 / OPEN-06 / OPEN-10 关闭后才可讨论落地：

1. 中断旧 Attempt：L1029 要求先把旧 Attempt 转为「已中断」或「待核对」再建新 Attempt —— 该前提依赖 D-02 取值裁定，当前**不可实现**。
2. 结果与证据：D-13 的落点（Attempt 新列 vs `task_events.payload_json`）决定恢复核对读取路径；`event_sequence` 定序是两种候选的共同约束。
3. 执行器身份：D-15 登记 per-Attempt 身份载体候选，不能由该身份本身推出旧 Epoch 拒提交。`RECOVERY_PROTOCOL.md` R-06 的锁/纪元失效拒收仍依赖 OPEN-06 与 §17.1/17.2，不能归入 D-15 身份字段的已定行为。
4. 纪元语义：OPEN-06 的递增与拒提交 CAS 语义只在 `worker_epoch` 轴上，与 D-15 身份轴分离，不得互相替代。
5. 以上均为**候选**；本文件不实现、不测试、不接入任何恢复路径。

## 7. 自证

- 唯一写入为本文件；写入前后未改动其它文件。
- 本文件计数：Plan 6 列、Step 8 列、Intent 9 列、Attempt 8 列，均逐字段对照 DDL 原文行号。
- 下一步（不属于本子任务）：由未参与本文件撰写的评审者按最新哈希复评；D-01 / D-02 的取值裁定须由先生作出。
