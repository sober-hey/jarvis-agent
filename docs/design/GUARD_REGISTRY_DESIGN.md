# K0-002 Guard 注册表 DRAFT 建议（三个现有契约占位槽位名）

- 文件身份：`docs/design/GUARD_REGISTRY_DESIGN.md`（本文件由唯一代理新增；作者不参与实现、无任何批准权）
- 状态：**DRAFT / PROPOSED / NOT_APPROVED / NOT_VERIFIED / NOT_FROZEN**——文本层注册表建议；不是契约，不是正式注册表落地，不是批准，不是冻结，不是验收
- 效力：Guard **未实现、未注册、未测试、未生效**；本文件**不关闭** R01/R02、**不批准**任何安全语义、**不改变**任何契约 / ADR / 不变量 / 任务卡 / 门禁 / 台账
- 观测快照（只读实测，**历史记录，不是当前状态**）：2026-10-12T23:43–23:52 +08:00，Git HEAD `4322cf5`，`git status --porcelain` 为空；计数由本回合解析 `contracts/*.yaml` 得出
- 授权定性（非自批）：撰写 Guard 注册表 DRAFT 建议属文本组织与设计整理（`docs/design/GUARD_CONTRACT_DESIGN.md` D-G3a）；常规作业定性引自 `docs/reports/K0-002-FREEZE_PROPOSAL.md` §9.2 Q6/Q7
- 范围：只登记 `contracts/task_transitions.yaml` / `contracts/step_transitions.yaml` 中**已存在的三个占位槽位名**的候选输入 / 候选职责 / 候选失败码建议；不写产品代码，不写测试，不改契约

## 1. 来源（全部只读）

| ID | 来源 | 读取到的内容 | 用途 |
|---|---|---|---|
| G1 | `contracts/task_transitions.yaml`、`contracts/step_transitions.yaml` | Task 31 边 / 93 槽、Step 37 边 / 111 槽、无重复边；唯一槽位名 3 个；两文件 `status: DRAFT`，note 自述「所有 Guard 目前仅为契约占位符」 | 槽位名与计数的唯一来源 |
| G2 | MP v1.1.1 §10.3（L1116–1146） | 乐观并发条件更新 SQL（L1123）；「如果更新行数不是 1 → `VERSION_CONFLICT`」（L1135）；同一事务五步（L1138–1144：验证转换 / 更新任务或步骤 / 更新相关 Intent 或 Attempt / 写入审计事件 / 提交）；「任何跨对象不变量都不能只靠应用层的非事务性先查后改实现」（L1146） | 三个槽位候选语义的最强原文依据 |
| G3 | MP §8.4 STATE-001..010（L653–691） | STATE-007 状态变化与审计事件必须同一 SQLite 事务提交（L677–679）；STATE-008 必须通过统一 State Transition Service（L681–683）；STATE-009 不得绕过转换 Guard（L685–687）；STATE-001..006/010 跨对象不变量 | 跨对象不变量面与事务面 |
| G4 | MP §9.1（L716–753）、§14.1（L1495–1503）、§31.2（L3192 起） | 取消仲裁「谁先完成数据库提交谁先获得状态决定权」；「最终成功提交必须使用带条件的状态更新」（L753）；步骤 12 完成前 MUST NOT 执行步骤 14；17 个稳定错误码 | 失败码集合与提交前置 |
| G5 | `contracts/execution_invariants.yaml` | INV-001..018 共 18 条，`verification_status` 全部 `NOT_IMPLEMENTED`（18/18） | 不变量现状与 `verification_status` 轴先例 |
| G6 | `docs/design/DOMAIN_CONTRACT_DESIGN.md`（v4） | L120：Guard 唯一名 3 个、输入为 Task/Step 行 + 相关 Intent/Attempt 行 + 审计事件，**该文不定义 Guard 语义**；L139：M2 输入内存元组 {Task, Plan, Step, Intent, Attempt}；D-02 Attempt 无契约来源 | 输入对象与未决依赖 |
| G7 | `docs/design/GUARD_CONTRACT_DESIGN.md`（v4，sha256 `95043f3c…`） | §3 逐边候选谓词 / 输入 / 拒绝理由 / 复验归属；覆盖 Task 18/31、Step 24/37（union）；§4 四项未闭合依赖；§5 未覆盖 13/13 | 本注册表的范围上界与依赖 |
| G8 | `docs/reports/K0-002-FREEZE_PROPOSAL.md` §2.4 与 §30.4-3（L85–87）、§9.2（L237–243） | 注册表逐条给出语义、失败模式、`implemented_in_task`、`verification_status`（沿用 `execution_invariants.yaml` 轴）；纯谓词实现与产品接线分开登记；候选规则设计与台账核对属常规维护 | 注册表字段与授权定性 |
| G9 | `docs/design/TRACEABILITY_MATRIX.md` R01 / R02 | 两行仍 `PARTIAL`，明列「仍无正式 Guard 注册表、逐边完整失败规则或实现（FR-02）」；复验归属 K0-003 | 不关闭声明与复验归属 |

## 2. 依据等级、字段来源与禁令

- **【原文】**：G2–G4 明文可直接读出并给出条款 / 行号者。**【候选】**：由原文推导的草案选择，未经评审与批准，**MUST NOT** 被产品代码、测试或领域对象引用为权威。
- 三个槽位名是 `contracts/` 现有占位符名（G1）；本文件不改名、不替换、不扩充。**它们不是可独立调用的接口、Service 方法或函数**——它们是契约槽位，按边**复合**使用（同一契约中每条边同时列出三者；复合方式属【候选】），本文件不给出调用签名、参数对象或返回结构。
- **失败码映射全部为【候选】**：§31.2（G4）只给稳定错误码集合，MP **未逐边绑定** Guard 失败与错误码；下表给出的码是**候选集合，不是唯一映射**，逐边完整失败规则仍缺（G9 / FR-02）。
- 注册表条目字段取自 G8（`implemented_in_task`、`verification_status`）与 G5（`verification_status` 轴先例）；本文件不新增其它字段、字段名或状态值。
- **MP §30.3 的 guard 字段名只是格式示例**（该节自述「这只是格式示例」，G7 S3 已登记）：本文件**不**把 §30.3 字段示例当作正式语义来源，也不引用其字段名作依据。
- 禁令：不新增 / 删除 / 改写任何转换边、状态、字段、槽位名或错误码；不新增或放宽任何安全语义；不做任何批准、冻结、验收、`DONE` 声明。`MUST` / `MUST NOT` 仅为草案内部约定。

## 3. 槽位级 DRAFT 条目（三个现有占位名，非逐边、非接口）

条目字段口径：`implemented_in_task` = **未指派**（无实现；产品级实现归属由各任务门禁另行确定）；`verification_status` = `NOT_IMPLEMENTED`（沿用 G5 轴，与 INV-001..018 现状一致）；`复验归属` = K0-003（与 G9 R01/R02 一致）。

| 槽位名（G1 现有） | 候选输入（【候选】绑定） | 候选职责（【候选】） | 候选失败码（【候选】，**非唯一映射**） | 原文依据（可直接读出） | 未决依赖 |
|---|---|---|---|---|---|
| `expected_state_and_version` | 被转换对象当前行的 `state` 与其 `version`：`tasks`、`task_steps`；调用方提供的 expected_state / expected_version | 以「合法边 + 期望状态 + 期望版本」为条件的原子更新；影响行数 ≠ 1 即拒绝该转换 | `INVALID_TRANSITION`、`VERSION_CONFLICT` | G2 §10.3（L1123 条件更新 SQL；L1135「如果更新行数不是 1 → `VERSION_CONFLICT`」）；G3 STATE-009（L685–687） | D-07：`task_plans` / `operation_intents` / `step_attempts` / `file_write_intents` 无 `version`；OPEN-06：`worker_epoch` 与旧 Epoch 拒提交的 CAS 语义未闭合 |
| `transactional_event` | 同一事务内的四类写入：转换验证、被转换行更新、相关 Intent 或 Attempt 行更新、审计事件写入；相关 `operation_intents`(state)、`file_write_intents`（经 G7 §3 Step 行引用） | 把上述五步（含提交）放进**同一 SQLite 事务**（**仅限数据库内**：状态行 / 相关 Intent 或 Attempt 行 / 审计事件；不含任何文件系统操作）；跨对象不变量不得靠应用层非事务性先查后改；成功提交须带条件更新 | `INVALID_TRANSITION`、`VERSION_CONFLICT`、`DATABASE_BUSY`、`DATABASE_ERROR`、`RECOVERY_REQUIRED` | G2 §10.3（L1138–1146：同事务五步 + 末句）；G3 STATE-007（L677–679）、STATE-008（L681–683）；G4 §9.1（L753）、§14.1（**仅作提交前置的时序依据**：步骤 12 完成前 MUST NOT 执行步骤 14；**不等于**把文件系统操作并入该事务） | 事务边界与崩溃点、`busy_timeout` 超时行为（G9 §3 仍列为缺）；OPEN-14 条件更新失败分支；INV-007 无判定器 |
| `domain_invariants_satisfied` | 与该边相关的跨对象不变量判定结果：INV-001..018（G5）；跨对象元组 {Task, Plan, Step, Intent, Attempt}（G6 L139） | 在提交前核对相关跨对象不变量未被违反（STATE-001..010）；当前无可判定规则形式 | `POLICY_DENIED`、`RECOVERY_REQUIRED`、`INVALID_TRANSITION` | G3 §8.4 STATE-001..010（L653–691）；G2 §10.3 末句（L1146）；G5 INV-001..018 现状 | R03：18/18 `NOT_IMPLEMENTED`、无判定器；D-02：Attempt 无契约来源；OPEN-02 / OPEN-03 / OPEN-12（`RETRY_SAFE` 可判定性、配对表、`FAIL` 条件）未关闭 |

说明：本表只绑定**槽位名**，不声明任何**逐边**谓词已确定；逐边谓词草案见 G7 §3（其整列合取同样全部为【候选】）。上表三行**不构成**「每条边只用一个 Guard」的暗示——同一条边可同时受三者约束，且三者语义均未定义、未实现。

边界（限定，避免误读）：`transactional_event` **只覆盖数据库内事务**——被转换状态行、相关 Intent / Attempt 行与审计事件的同一 SQLite 事务提交；**不包含**文件系统副作用、准备（prepare）产物持久化、原子发布 / 硬链接或 `fsync`，也**不表示** SQLite 与文件系统之间的跨资源原子性（该面在 `docs/design/TRACEABILITY_MATRIX.md` R04 明确列于局部 PASS 之外）。行内引用 §14.1 只为「提交前置」的时序依据，不构成把文件操作并入数据库事务的依据。

## 4. 覆盖统计（union 口径；本回合实测，非表内全部 31/37）

| 项 | 实测值 | 口径与说明 |
|---|---|---|
| Task 契约边 / Guard 槽位 | 31 / 93 | G1 解析实测；无重复边 |
| Step 契约边 / Guard 槽位 | 37 / 111 | 同上；两文件合计 204 槽位 |
| DRAFT 槽位名 | **3** | `expected_state_and_version`、`transactional_event`、`domain_invariants_satisfied`；93 + 111 个槽位**复用**这 3 条槽位级条目——本文件**不**为 204 个槽位各写独立语义 |
| 本注册表覆盖（union） | Task **18/31**、Step **24/37** | 与 G7 §3 相同，**不扩大**：Task 4 具名 + 7 非终态 × 2 兜底（两族交集 0 → union 18）；Step 8 具名 + 8 非终态 × 2 兜底（交集 0 → union 24）。**union 18/24 为上限，不得写成 31/37** |
| 未覆盖边 | Task **13/13**、Step **13/13** | Task 13 条：`BLOCKED→READY`、`READY→BLOCKED`、`RETRY_WAIT→{READY,BLOCKED}`、`RUNNING→{VERIFYING,NEEDS_RECONCILIATION,RETRY_WAIT,BLOCKED}`、`VERIFYING→{NEEDS_RECONCILIATION,RETRY_WAIT,BLOCKED}`、`NEEDS_RECONCILIATION→{VERIFYING,BLOCKED}`；Step 13 条：11 条 MP 无出处边（`CLAIMED→{BLOCKED,PENDING}`、`PREPARING→{BLOCKED,PENDING}`、`EXECUTING→{BLOCKED,RETRY_WAIT}`、`RETRY_WAIT→{BLOCKED,PENDING}`、`VERIFYING→{BLOCKED,NEEDS_RECONCILIATION,RETRY_WAIT}`）＋`NEEDS_RECONCILIATION→{VERIFYING,BLOCKED}` |
| 未覆盖边处置 | 保留、不删、不细化 | 未覆盖边不因本文件获得 Guard 语义；Step 的 11 条无出处边仍 UNRESOLVED（G9 R02），**不得据此自动删边** |
| 逐边失败规则 | 仍缺 | 本文件只给槽位级候选失败码集合；G9 R01/R02 所述「逐边完整失败规则」仍缺 |
| 每条边的 Guard 数 | 3（占位复用） | 两契约中每条边均列同一组三个槽位名（G1）；这不等于三者已各自有独立语义或可独立调用 |

## 5. 效力、禁止与自证

- 唯一写入即本文件；未修改 `contracts/`、MP、ADR、任务卡、门禁、台账、协议文档、源码与测试；未执行任何状态转换或产生副作用的命令（仅只读读取、解析、计数与只读 Git / 哈希查询）。
- **未实现、未注册、未测试**任何 Guard、注册表运行时、状态机或判定器；未运行 Ruff / Pyright / pytest（本文件非代码、非测试，门禁与 Ruff 对其不适用）；未声称任何转换已受保护或已验收。
- **不关闭 R01 / R02**：两行在 `docs/design/TRACEABILITY_MATRIX.md` 仍为 `PARTIAL`；本文件属设计文本层建议，不构成 §49 条目满足。
- **不批准任何安全语义**：不批准 Guard 语义、不批准契约、不批准 ADR（5 份仍 `PROPOSED` / `PENDING_VALIDATION`）、不批准取消 / 发布线性化、不批准 INV-010。
- 仍未闭合、须另行处理的事项：D-G2（`NEEDS_RECONCILIATION → READY` 的 `retry_safe` 主语，以及 OPEN-02 关闭前该边是否一律禁用）属安全语义，须先生裁定，本文件不自决；G7 §4 四项依赖（执行权、CAS、Cancel、Safety/Reconciliation）与 G7 §5 未覆盖范围均维持原状。
- **审批边界（两分）**：① 把本注册表落为 **`contracts/` 正式机器契约**、或改动 / 放宽任何**安全语义**（含逐边谓词升格为规范条款、失败码成为唯一绑定），**必须另行走审批**（G7 D-G3a），本文件不做、也不申请该批准；② 继续在 **DRAFT 文本层**整理逐边失败规则与拒绝理由，属**已授权的常规设计工作**（定性依据 G8 §9.2 Q6/Q7：候选规则先设计、台账核对属常规维护），**无需审批**——本条**不构成**停工理由，不得据此停止 DRAFT 层的规则整理。
- 台账现状（写入时实测）：本文件**自身尚未登记**于 `docs/design/TRACEABILITY_MATRIX.md`；登记属常规维护（G8 §9.2 Q7），本轮因「唯一写入」边界未执行，留给台账持有者。
- 快照过期须重新实测后更新本文件，不得沿用上述计数与哈希。

## 6. 收束声明（停写版，供复核直接引用）

1. **本文只是候选注册表**：`docs/design/GUARD_REGISTRY_DESIGN.md` 是 DRAFT 建议，不是正式 Guard 注册表，**不属于 `contracts/`**、不是契约、不写入任何机器可读契约；`contracts/` 本轮零改动。落为机器契约须另行走审批（G7 D-G3a）。
2. **当前 Guard 计数：实现 0 个 / 注册 0 个 / 测试 0 个**（`0 / 0 / 0`）。三个槽位名在 `contracts/` 中仅为占位符（G1）；`verification_status` 全部 `NOT_IMPLEMENTED`、`implemented_in_task` 未指派。
3. **覆盖统计（union 口径，不扩大）**：Task **18/31**、Step **24/37**；**未覆盖 Task 13/13、Step 13/13**（逐名清单见 §4）。本注册表不改变该覆盖，也不为未覆盖边提供 Guard 语义。
4. **效力**：不关闭 R01/R02，不批准任何安全语义，不冻结、不验收、不 `DONE`；D-G2 仍待先生裁定。
5. 本文件为**停写版**；此后任何改动都会使本节的哈希 / 计数绑定失效，须重新实测并更新。
