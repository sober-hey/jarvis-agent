# K0-002 Guard 契约设计（核心 Task/Step 转换）

- 文件身份：`docs/design/GUARD_CONTRACT_DESIGN.md`（本文件由唯一代理新增；作者不参与实现、无任何批准权）
- 状态：**DRAFT / NOT_APPROVED / NOT_VERIFIED**——设计草案，不是契约，不是批准，不是冻结，不是验收；**Guard 未实现、未注册、未测试、未生效**
- 版本：v4（2026-10-12；v3→v4 依独立评审 F-1/F-2/F-3 仅做文本修复：NRC 前置限定于机器契约已有 `→NEEDS_RECONCILIATION` 边的源态且标候选、其余源态停止推进/不提交 `FAILED`/调用 reconcile ≠ 新增边；补「不新增、不删除、不改写转换边」禁令；`任意→CANCELED`、`READY→RUNNING`、`NRC→READY` 的输入绑定与依赖补候选/OPEN-08；台账授权陈述按实测改写。候选谓词增加合法边限制；未改机器契约与计数。v4 定点修：Task/Step 兜底的 `BLOCKED` 分支统一限定为「仅当机器契约已有该源态 `→BLOCKED` 合法边时才可候选转，否则保持原状态拒绝推进」，消除对无边源态（如 `CREATED`、`PENDING`）可转 `BLOCKED` 的暗示）
- 观测快照（**历史记录，不是当前状态**）：计数与引用读取于 2026-10-12T17:19:31–17:21:05 +08:00，当时 Git HEAD 为 `cdccd32`；本文件 v1 之后已由他人提交进仓库（该提交动作非本会话所为）。引用、复验与冻结判定一律以**实际读取时**的仓库状态为准，不得把本行读数当作当前 HEAD 或提交状态
- 范围：为规划中**已明确**的核心 Task/Step 转换给出 Guard 的输入对象、谓词草案、拒绝理由与复验归属；不修改 `contracts/`，不实现状态机，不新增安全语义，不做任何 approve

## 1. 来源（全部只读）

| ID | 来源（读取时刻同第 6 节快照） | 读取到的内容 | 用途 |
|---|---|---|---|
| S1 | MP v1.1.1 §7.2（L528–559） | K0 Task 合法转换表（10 态 / 31 边）；"表中允许转换不代表任何情况下都可以转换"；`RUNNING→FAILED` 仅在无未核对危险副作用时允许 | Task 边集合与兜底条件 |
| S2 | MP §8.1–8.3（L606–649） | Step 正常路径主链、异常路径分支、§8.3 七条具名约束 + 一条兜底（任意非终态→`FAILED`/`CANCELED` 不得遗留未受控活动副作用） | Step 边与谓词原文 |
| S3 | MP §30.3（L3070–3107） | §30.3 自述"这只是格式示例"，且"正式文件必须覆盖全部状态转换"：仅取 10 个唯一 **guard 字段名**作参考（`valid_task_spec`、`active_plan_committed`、`worker_lock_held`、`worker_epoch_current`、`no_cancel_request`、`emergency_stop_inactive`、`all_required_steps_completed`、`all_required_effects_verified`、`no_unresolved_attempts`、`retry_safe`）；**不构成 Guard 语义来源** | 示例字段名（非规范条款） |
| S4 | MP §9.1（L716–753）、§10.3（L1116–1149）、§16.3（L1739–1747）、§17.1–17.2（L1794–1827）、§18.1（L1885–1915）、§31.2–31.3（L3192–3249） | 取消仲裁与"成功提交须带条件更新"、乐观并发与 `VERSION_CONFLICT`、Observation×Decision 决策表、flock 与 Worker Epoch、恢复前置顺序、稳定错误码集合 | 拒绝理由与依赖现状 |
| S5 | `contracts/task_transitions.yaml`、`contracts/step_transitions.yaml`（mtime 14:34:14） | 实测 **Task 31 边 / 93 Guard 槽**、**Step 37 边 / 111 槽**，唯一 Guard 名仅 3 个（`expected_state_and_version`、`transactional_event`、`domain_invariants_satisfied`），全部 `status: DRAFT`，文件 note 自述"Guard 目前仅为契约占位符" | 槽位现状 |
| S6 | `contracts/operation_intent.schema.json`、`contracts/step_states.yaml`、`contracts/execution_invariants.yaml` | Intent 6 态（PREPARED / PUBLISHING / NEEDS_RECONCILIATION / VERIFIED / BLOCKED / FAILED）；Step 11 态 / 3 终态；INV-001..018 全部 `NOT_IMPLEMENTED` | Guard 输入与效力 |
| S7 | `docs/design/RECOVERY_PROTOCOL.md`（mtime 17:06:15）OPEN-01/02/03/06/12/14；`docs/design/TRACEABILITY_MATRIX.md` R01/R02 | 取消线性化、`RETRY_SAFE` 可判定性、Observation×Decision 配对表、epoch CAS、`FAIL` 条件、取消条件更新失败分支均未闭合；R01/R02 已登记本文所需的 Guard 缺口 | 依赖标注 |
| S8 | `docs/reviews/K0-002-baseline-review.md` F1（L97–140） | Step 侧无权威上游转换表；11 条边在 MP 文本查无出处；**不得据此自动删边** | 未明确范围 |
| S9 | `planning/BACKLOG.md`、`DECISIONS_REQUIRED.md`、`planning/tasks/K0-002.md` | K0-003/004/005/007/009/011/012/013/014 责任归属；ADR-0001 PROPOSED、ADR-0002 PROPOSED、DEC-004 | 复验归属 |

## 2. 依据等级与规范词

- **【原文】**：MP 或 `contracts/` 明文可直接读出，并给出条款位置。
- **【候选】**：由原文推导的草案谓词或字段选择，未经评审与批准；**MUST NOT** 被产品代码、测试或领域对象引用为权威。
- §30.3 是**格式示例**而非规范条款：本文件只从其中取 guard **字段名**（在表中标为"示例字段"）；由这些名字组合出的复合谓词、输入字段绑定与拒绝理由一律是【候选】，不得标为原文。
- 拒绝理由的**错误码集合**为 §31.2 原文；"哪条 Guard 失败对应哪个码"是【候选】选择（MP 未逐条绑定）。
- 表中**所有**复合谓词与错误码绑定统一为【候选】；"原文要素"列只列可直接读出的边、不变量与条款，**不得把整段合取谓词称为原文**。
- **不新增、不删除、不改写任何转换边**：`contracts/task_transitions.yaml` / `step_transitions.yaml` 的边集合只能由设计依据或人工确认收口；`调用 reconcile`、保留核对状态或进入 `BLOCKED` 都**不等于**新增 `→NEEDS_RECONCILIATION` 边。
- 本文件的 `MUST` / `MUST NOT` 仅为草案内部约定，不产生契约效力（正式效力只来自 `contracts/`）。
- 不新增状态、字段、Guard 名或错误码；不新增或放宽任何安全语义；不做任何批准。

## 3. 核心转换 Guard DRAFT 映射（输入 / 谓词 / 拒绝理由 / 复验归属）

| 边（Task） | Guard 输入（只读对象·字段；**绑定为候选**） | 谓词草案（**整列合取均为候选**） | 拒绝理由（码取自 §31.2，**绑定为候选**） | 原文要素（含示例字段） | 复验归属 |
|---|---|---|---|---|---|
| CREATED → READY | `tasks`(state, version)；TaskSpec；`task_plans`(ACTIVE 行) | state=CREATED ∧ version=expected ∧ TaskSpec 校验通过 ∧ 该 Task 存在 ACTIVE Plan | INVALID_REQUEST / INVALID_TRANSITION / VERSION_CONFLICT | 原文：§7.2 该边合法、§30.2 正反例要求。示例字段：§30.3 `valid_task_spec` + `active_plan_committed` | K0-003（K0-004 出证据） |
| READY → RUNNING | `tasks`(state, version, cancel_requested_at_ms)；`system_control`(emergency_stop, worker_epoch)；worker 锁文件持有者 | state=READY ∧ version=expected ∧ 唯一执行器锁已持有 ∧ epoch 为当前纪元 ∧ 取消请求不存在 ∧ emergency_stop=0 | WORKER_ALREADY_RUNNING / EMERGENCY_STOP_ACTIVE / VERSION_CONFLICT | 原文：§17.1–17.2 锁与 Epoch、§9.3 停止态禁止新副作用。示例字段：§30.3 四条 guards。候选：`tasks.cancel_requested_at_ms` 与 worker 锁持有者的绑定（锁文件在 `contracts/` 无载体，ADR-0001 PROPOSED） | K0-007（依赖 ADR-0001 PROPOSED、OPEN-06） |
| VERIFYING → SUCCEEDED | `tasks`；`task_steps`(plan_id=active_plan_id, required=1, state)；`operation_intents`(state)；`step_attempts` | ∧ **活动 Plan** 的、`required=1` 的全部 Step state=COMPLETED ∧ 全部相关 Intent=VERIFIED ∧ 无活动 Attempt ∧ 最终成功提交带"取消请求不存在"的条件更新 | INVALID_TRANSITION / VERSION_CONFLICT / RECOVERY_REQUIRED | 原文：STATE-001/002/004、§9.1 条件更新。示例字段：§30.3 四条 guards | K0-003（取消侧 K0-014） |
| NEEDS_RECONCILIATION → READY | `tasks`(state, version)；Reconciliation 结果(observation, decision)；`system_control` | state=NEEDS_RECONCILIATION ∧ decision=RETRY_SAFE ∧ 取消请求不存在 ∧ emergency_stop=0 | POLICY_DENIED / EMERGENCY_STOP_ACTIVE / VERSION_CONFLICT | 原文：§16.3 决策表。示例字段：§30.3 `retry_safe`。候选：`system_control`(emergency_stop) 绑定与 Task 层聚合方式 | K0-013 / K0-014（OPEN-02 未关闭前有可达性禁令） |
| 任意非终态 → CANCELED | `tasks`(cancel_*)；活动 Attempt（读取属 D-02 未定）；「安全检查点」与副作用观察（定义属 OPEN-08 未定） | ∧ 已停止新副作用 ∧ 已完成安全检查点与已发生副作用的核对（§9.2 流程） | INVALID_TRANSITION / RECOVERY_REQUIRED | 原文：§9.1–§9.3、§7.1 终态不得重新打开。候选：Attempt 状态读取（D-02）与安全检查点定义（OPEN-08） | K0-014 |
| 任意非终态 → FAILED | 同上 + 结果是否已知 | ∧ 不存在未核对的危险副作用。结果未知时：源状态在机器契约中已有 `→NEEDS_RECONCILIATION` 边者（Task 实测仅 `RUNNING`、`VERIFYING`）**MUST** 先转该状态；无该边者 **MUST** 停止推进（拒绝本转换；仅当机器契约已有该源态的 `→BLOCKED` 边时，才可候选转 `BLOCKED`，否则保持原状态拒绝推进）并保留核对，**MUST NOT** 提交 `FAILED`、**MUST NOT** 发明边（`调用 reconcile` ≠ 新增边） | INVALID_TRANSITION / RECOVERY_REQUIRED | 原文：§7.2 注（该 NRC 前置在 MP 中绑定 `RUNNING→FAILED` 示例）、§8.3 兜底、INV-005。把 NRC 前置按源态限定为候选 | K0-003 / K0-013 |

| 边（Step） | Guard 输入（只读对象·字段；**绑定为候选**） | 谓词草案（**整列合取均为候选**） | 拒绝理由（码取自 §31.2，**绑定为候选**） | 原文要素（含示例字段） | 复验归属 |
|---|---|---|---|---|---|
| PENDING → CLAIMED | `task_steps`(state, version, plan_id)；`tasks`(state, active_plan_id)；worker 锁/epoch；`step_attempts` | state=PENDING ∧ version=expected ∧ Step 属活动 Plan ∧ Task=RUNNING ∧ 执行权已持有 ∧ 该 Step 无活动 Attempt | WORKER_ALREADY_RUNNING / VERSION_CONFLICT / INVALID_TRANSITION | 原文：§8.3"必须成功取得任务执行权"、STATE-003 | K0-003 / K0-007 |
| CLAIMED → PREPARING | `task_steps`；Step 前置条件载体（§5.3 五要素当前无列） | ∧ 前置条件检查通过 | POLICY_DENIED / INVALID_TRANSITION | 原文：§8.3。前置条件判定载体属 D-08，未定 | K0-003（路径/权限部分 K0-009） |
| PREPARING → EXECUTING | `operation_intents`(step_id, state)；`file_write_intents`(operation_id, prepared_relpath, expected_sha256)；准备产物持久化等级 | ∧ 已提交有效 Intent（state=PREPARED）∧ 存在对应 FileWriteIntent 行 ∧ 准备产物已达所需持久化等级 ∧ §14 步骤 12 的事务已提交 | POLICY_DENIED / INVALID_TRANSITION | 原文：§8.3、§14.1（步骤 12 完成前 **MUST NOT** 执行步骤 14）、§13.1–13.3、INV-002。"有效 Intent"的读出方式（专属行存在性）未定 | K0-011（FS-035 / O-3 Intent↔Attempt 线性化 OPEN） |
| EXECUTING → VERIFYING | `step_attempts`(state, 结果, error)；`task_steps` | ∧ 已有可核对的执行结果（Attempt 结果已持久化） | RECOVERY_REQUIRED / TOOL_FAILED / UNKNOWN_OUTCOME | 原文：§8.3、§16.3、INV-005。Attempt 结果载体属 D-13，未定 | K0-011 / K0-012 |
| EXECUTING → NEEDS_RECONCILIATION | 观察结果；Intent 态 | 结果未知或与预期不符（不得盲目重试） | UNKNOWN_OUTCOME | 原文：§8.2、§16.1–16.3、INV-005 | K0-013 |
| VERIFYING → COMPLETED | `operation_intents`(state)；`artifacts`(VERIFICATION_EVIDENCE) | ∧ 独立验证通过 ∧ Intent=VERIFIED ∧ 存在验证证据 | POLICY_DENIED / RECOVERY_REQUIRED | 原文：§8.3"必须通过独立验证"、STATE-002、INV-006 | K0-012 |
| NEEDS_RECONCILIATION → RETRY_WAIT | Reconciliation 行(observation, decision)；`file_write_intents` | ∧ decision=RETRY_SAFE 且 §16.2 严格前提全部成立（缺一降级 BLOCK） | POLICY_DENIED | 原文：§8.3、§16.2–16.3（OPEN-02 未关闭：前提当前不可判定） | K0-013 |
| BLOCKED → PENDING | 解除阻塞的授权与理由记录 | ∧ 存在明确授权与理由（缺授权不得自行解除） | POLICY_DENIED | 原文：§8.3（人工授权入口 OPEN-07） | K0-003 |
| 任意非终态 → FAILED / CANCELED（Step） | `step_attempts`、Intent、副作用观察 | ∧ 不得遗留未受控的活动副作用；结果未知时同 Task 侧规则：仅机器契约已有 `→NEEDS_RECONCILIATION` 边的源状态（实测 `EXECUTING`、`VERIFYING`）**MUST** 先转该状态，其余源状态 **MUST** 停止推进（拒绝本转换；仅当机器契约已有该源态的 `→BLOCKED` 边时，才可候选转 `BLOCKED`，否则保持原状态拒绝推进）并保留核对，**MUST NOT** 提交 `FAILED`、**MUST NOT** 发明边 | RECOVERY_REQUIRED / VERSION_CONFLICT | 原文：§8.3 兜底、STATE-004。源态限定为候选 | K0-003 / K0-014 |

说明：三个公共 Guard 名（`expected_state_and_version`、`transactional_event`、`domain_invariants_satisfied`）是契约现有槽位名，语义未定义；本文件不改名、不替换、不扩充，仅【候选】指出其输入为对应行 + 审计事件（§10.3、STATE-007/008/009）。另：表内**全部复合谓词与错误码绑定均为候选**，"原文要素"列只列可直接读出的边、不变量与条款。"结果未知先转 NEEDS_RECONCILIATION"只对**机器契约已有 `→NEEDS_RECONCILIATION` 边**的源状态成立（Task 实测 `RUNNING`、`VERIFYING`；Step 实测 `EXECUTING`、`VERIFYING`），该源态限定为【候选】；其余源状态 **MUST** 停止推进（拒绝本转换；仅当机器契约已有该源态的 `→BLOCKED` 边时，才可候选转 `BLOCKED`，否则保持原状态拒绝推进）并保留核对/诊断，**MUST NOT** 提交 `FAILED`、**MUST NOT** 发明边——`调用 reconcile` 不等于新增边。

## 4. 依赖仍未闭合（本文件不得视为已定）

| 依赖 | 现状（同上快照实测） | 受影响的行 |
|---|---|---|
| Worker / 执行权 | ADR-0001 `PROPOSED`；§17.4 Lease 时间不是唯一执行权证明；§17.5 K0 不提供任意子进程执行工具 | READY→RUNNING、PENDING→CLAIMED |
| CAS / 乐观并发 | §10.3 只给 `tasks`/`task_steps` 的 `version`；`task_plans`/`operation_intents`/`step_attempts`/`file_write_intents` 无 version（D-07） | 全部行的 `expected_state_and_version`，尤其 PREPARING→EXECUTING |
| Cancel | DEC-004 / OPEN-01 取消持久化与发布启动的线性化未证明；OPEN-14 条件更新失败分支未定；INV-010 `NOT_IMPLEMENTED` | VERIFYING→SUCCEEDED、任意→CANCELED（含 OPEN-08 未定的"安全检查点"）、→READY/→PENDING 的"取消请求不存在" |
| Safety / Reconciliation | OPEN-02 `RETRY_SAFE` 可判定性、OPEN-03 配对表、OPEN-12 `FAIL` 条件均未关闭 | NEEDS_RECONCILIATION→READY/RETRY_WAIT、EXECUTING→NEEDS_RECONCILIATION、任意→FAILED |

以上四项闭合前：本文件的谓词草案 **MUST NOT** 被实现为契约 Guard，且 **MUST NOT** 声称任何转换守卫已生效或已测试。

## 5. 未明确 Guard 范围（本文件不定义；不得据此修改或删除契约边）

1. **Step 37 边中 11 条在 MP 文本查无出处**（17:21:05 实测 `contract-only = 11`，与基线评审 F1 一致）：`CLAIMED→BLOCKED`、`CLAIMED→PENDING`、`EXECUTING→BLOCKED`、`EXECUTING→RETRY_WAIT`、`PREPARING→BLOCKED`、`PREPARING→PENDING`、`RETRY_WAIT→BLOCKED`、`RETRY_WAIT→PENDING`、`VERIFYING→BLOCKED`、`VERIFYING→NEEDS_RECONCILIATION`、`VERIFYING→RETRY_WAIT`。**这 11 条保留、不删除**；其 Guard 语义待设计依据补齐（D-G1a，已授权常规设计工作；F1 处置建议 1/2），本轮不编写谓词。
2. §3 共覆盖 Task 31 边中的 18 条（4 具名 + 7×2 兜底），其余 **13 条**不细化。
3. §3 共覆盖 Step 37 边中的 24 条（8 具名 + 8×2 兜底），其余 **13 条**不细化；这 13 条含上述全部 11 条无出处边，另含 `NEEDS_RECONCILIATION→VERIFYING`、`NEEDS_RECONCILIATION→BLOCKED`（此二条有 §8.2 出处，本轮同样不细化）。
4. Intent（6 态）、Plan、Attempt 的状态转换：无任何契约来源（D-01 / D-02、OPEN-11），本文件不设计。
5. §7.3 的 K1 转换（`PLANNING` / `WAITING_APPROVAL` / `PAUSED`）：K0 不实现，不在范围。
6. 紧急停止重置、`MANUAL_RECOVERY_REQUEST` 入口（OPEN-07）、QUIESCING 超时与"安全检查点"定义（OPEN-08）：不作为转换 Guard 定义，不在本文件范围（其被引用处已在 §3 标候选）。

## 6. 效力、禁止与自证

- 唯一写入即本文件；未修改 `contracts/`、ADR、任务卡、门禁、台账、协议文档、源码与测试；未执行任何状态转换或产生副作用的命令（仅只读读取、解析、计数与只读 Git 查询）。
- 未实现、未注册、未测试任何 Guard 或状态机；未声称任何转换已受保护或已验收。
- 未新增或重命名状态、字段、Guard 名、错误码；未新增或放宽任何安全语义；未新增、删除或改写任何转换边；未做任何批准、冻结、验收、`DONE` 声明。
- 台账现状（写入时实测）：`docs/design/TRACEABILITY_MATRIX.md` 的 R01/R02 **已登记**本文所需的 Guard 缺口（复验归属 K0-003），但**本文档自身尚未登记**；该登记属常规维护（依据 `docs/reports/K0-002-FREEZE_PROPOSAL.md` §9.2 Q7：台账核对属常规维护），本轮因"其余只读"边界未执行。
- 计数与引用均为 2026-10-12T17:19:31–17:21:05 +08:00 实测；工作区存在他人并行改动，快照过期须重新实测后更新本文件，不得沿用旧值。

## 7. 后续动作与真正需裁定的语义

常规设计工作（**已授权，不需要先生裁定**；依据 `docs/reports/K0-002-FREEZE_PROPOSAL.md` §9.2 对候选规则设计与文档组织的常规维护定性；本文不改契约、不新写机器契约、不自批）：

- **D-G1a**：先出 Step 边设计依据提案（来源表 / `source_refs` 提案，不删边、不改 `contracts/`）；据此再判断是否升格为**正式语义批准**申请。
- **D-G3a**：可撰写 Guard 注册表 **DRAFT 建议**（含失败码绑定与拒绝理由的整理），属文本组织与设计整理，不自批；是否落为 `contracts/` 机器契约只能另行走审批，本轮不写。
- 台账：本文档自身的登记与复验归属（R01/R02 的 Guard 缺口已登记；依据 FREEZE_PROPOSAL §9.2 Q7 属常规维护）。

**D-G2（需先生裁定的真实语义）**：`NEEDS_RECONCILIATION → READY` 的 `retry_safe` 主语——聚合 Step/Intent 层 Reconciliation 结果，还是另有 Task 级判定；以及 OPEN-02 关闭前该边是否一律禁用（保守输出 `BLOCK`）。此属安全语义而非文本组织，须先生裁定。
