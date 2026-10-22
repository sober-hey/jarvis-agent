# K0-002 领域契约设计：Task / Plan / Step / Attempt / Intent

- 文件身份：`docs/design/DOMAIN_CONTRACT_DESIGN.md`（本文件由唯一代理新增，作者不参与实现、无任何批准权）
- 状态：**DRAFT / NOT_APPROVED / NOT_VERIFIED**——设计草案，不是契约，不是批准，不是冻结，不是验收
- 版本：v4（2026-10-12；v2 修 H1/M1/M2 与 L1–L5；v3 修正"1 Step → 1 FileWriteIntent"存在性的过度声明；v4 收敛：删除 D-02 ↔ RECOVERY OPEN-11 的误交叉引用、厘清 `worker_epoch`（Attempt 引用的全局纪元，OPEN-06 只管 epoch/CAS）与 D-15 执行器身份、合并重复的 artifacts 行、来源表标注为历史快照。未关闭任何决定项，未新增 D-ID）
- 范围：K0 六个领域对象的职责、字段映射、关联键、幂等摘要与 Operation Key、状态与版本轴；**不实现产品代码、不修改 contracts/、不批准任何事项**
- 快照时刻（**历史快照：写入时点的观测，不是当前状态**）：2026-10-12T16:38–16:40 (+08:00)；Git HEAD `38b301d3cead347e4d33999cbb9d08f61fcfd831`；工作区另有他人正在修订的未提交文件与未跟踪评审文件，其数量不固定计数（§10）
- 本文所有新增字段名、枚举值、算法与列建议一律为提案，不构成任何正式契约（§7 规范词声明）

## 1. 来源表（读取时刻为**历史快照**：S1–S9/S11 为 2026-10-12T16:38–16:40 +08:00，S10 为 16:50；其中的状态、mtime 与数量均为写入时点观测，引用时以实际读取为准）

| ID | 来源 | 读取到的状态 | 本文件用途 |
|---|---|---|---|
| S1 | Master Plan v1.1.1 §5（L290–382） | §5.1–5.5 五个核心对象职责与"Intent ≠ Attempt" | 职责基线 |
| S2 | 同上 §6（L386–466） | request_key 规则、请求摘要规范化、Operation Key | 幂等基线 |
| S3 | 同上 §10.1（L834–1101） | 9 张核心表 DDL 基线；§10.3 乐观并发 | 字段映射 |
| S4 | 同上 §11（L1152–1253） | 输入限制、输出格式、`1 Task→1 Plan→1 Step→1 FileWriteIntent→1..N Attempts` | 对象基数 |
| S5 | 同上 §8（L586–691）、§12–§16 | Step 状态与 STATE-001..010、快照/准备/发布/核对决策表 | 关联与状态 |
| S6 | `contracts/*.yaml` 状态与约束契约：`task_states.yaml`（10 态 + 3 终态）、`step_states.yaml`（11 态 + 3 终态）、`task_transitions.yaml`、`step_transitions.yaml`、`database_constraints.yaml`、`execution_invariants.yaml` | 全部 `status: DRAFT`（mtime 14:34） | 状态集合与转换来源 |
| S7 | `planning/tasks/K0-002.md` | `IN_PROGRESS`、Contract Pack `1.0-draft / NOT_FROZEN`（mtime 16:26） | 允许/禁止范围 |
| S8 | `contracts/README.md`、`docs/reports/K0-002_BASELINE_REPORT.md`、`docs/design/FILESYSTEM_PROTOCOL.md` | README：1.0-draft / 未冻结 / 未实现验证；基线 pytest 34 passed（该报告自述，未由本文件复跑）；协议文件他人正在修订（mtime 16:36） | 效力轴、其 O-1 缺口与证据等级 |
| S9 | `contracts/*.json` 六个 Schema：`task_spec`、`operation_intent`（Intent 6 态）、`file_write_intent`、`reconciliation`、`snapshot_manifest`、`tool_manifest` | 全部 DRAFT、`schema_version: 1`（mtime 14:34） | 字段与枚举来源 |
| S10 | `docs/reviews/K0-002-domain-design-review.md`（他人独立评审，非本作者） | 基线哈希 `28786034…`；发现 H1、M1、M2 与 L1–L5（mtime 16:50） | 本轮修订依据 |
| S11 | `docs/design/RECOVERY_PROTOCOL.md`（他人正在修订，非本作者） | OPEN-01…OPEN-15 编号表（mtime 16:38:37） | 交叉引用与同次关闭记账 |

## 2. 职责边界（MUST / MUST NOT）

口径：§5 称"五个核心对象"（Task/Plan/Step/Attempt/OperationIntent）；本文把 **FileWriteIntent** 列为第六个对象，它是 §10.1 的物理拆分（L992），不是 §5 的第六个核心对象。

| 对象 | 负责（来源） | 不负责 | 状态集合来源 | 现状 |
|---|---|---|---|---|
| Task | 用户请求、任务身份、当前状态、取消请求、活动 Plan、总体结果、时间（S1 §5.1） | 每次工具执行细节（§5.1） | `contracts/task_states.yaml`（10 态 + 3 终态） | 已明确 |
| Plan | 计划版本、所属 Task、步骤集合、创建依据、激活状态、历史保留（§5.2） | 不承载任何目标副作用 | **无任何契约来源** | 需设计决定 D-01 |
| Step | 输入、类型、前置条件、执行状态、预期结果、验证要求（§5.3） | 一个 Step 最多一个逻辑目标副作用；不描述某次尝试（§5.3） | `contracts/step_states.yaml`（11 态 + 3 终态） | 已明确 |
| Attempt | 第几次、哪个执行器、Worker Epoch、起止时间、实际结果、错误、中断/恢复证据（§5.4） | 不定义"要做什么"（§5.5） | **无任何契约来源** | 需设计决定 D-02 / D-13 / D-15 |
| OperationIntent | 逻辑操作身份、工具名与版本、操作类型、规范化参数摘要、预期结果、恢复证据、核对状态（§5.5） | 不等于 Attempt；K0 每个 Step 最多一个 | `contracts/operation_intent.schema.json`（6 态） | 需设计决定 D-05 / D-16 |
| FileWriteIntent | `operation_id` 主键、快照摘要、准备/目标相对路径、预期哈希与大小（S3） | 不持有独立状态；不引入第二个逻辑操作 | 无 `state` 字段（状态归 Intent） | 已明确；路径基准待定 D-10 |

图例：「现状」列只说明**职责与状态集合是否有来源**，不代表字段已完整映射；凡无列者一律另列 D-ID（各对象的缺列行见 §3）。

## 3. 字段映射（领域字段 ↔ 契约 ↔ 物理列）

| 对象.字段 | 语义 | 来源 | 键/约束 | 状态 |
|---|---|---|---|---|
| Task.id | 任务身份 | §5.1 | PK | 已明确 |
| Task.request_key | 调用者提供的幂等键 | §6.1 | UNIQUE, NOT NULL | 已明确 |
| Task.request_digest | 规范化任务内容 SHA-256 | §6.2 | NOT NULL | 规则已明确；**字段集合待定 D-03** |
| Task.task_kind | `inventory.generate` | task_spec.schema.json | NOT NULL，K0 单值 | 已明确 |
| Task.state | 任务状态 | task_states.yaml | NOT NULL | 已明确 |
| Task.active_plan_id | 当前活动 Plan | §5.1 | 组合外键 → `task_plans(id, task_id)` | 已明确 |
| Task.cancel_requested_at_ms / _by / _reason | 取消请求（与任务状态正交） | §9 / §10.1 | 可空 | 已明确 |
| Task.version + created_at_ms + updated_at_ms | 乐观并发版本与时间轴 | §10.3 | NOT NULL，version DEFAULT 0 | 已明确 |
| Task（缺列） | §5.1「总体执行结果」与完成/失败时间无列；承载方式为 `task_events` 聚合还是新列 | §5.1 | — | **需设计决定（与 D-13 同族）** |
| Task（缺列） | TaskSpec 原文 `input.root` / `output.name` 无任何列，仅存 digest | task_spec.schema.json | — | **需设计决定 D-12** |
| Plan.id / task_id / plan_version | 身份与所属 Task | §5.2 | PK；FK `tasks(id)`；UNIQUE(task_id, plan_version)；UNIQUE(id, task_id) | 已明确 |
| Plan.state | 激活状态 | §5.2 | 部分唯一索引：每 Task 至多一个 ACTIVE | 约束已明确；**枚举待定 D-01** |
| Plan.plan_digest / created_at_ms | 计划内容摘要与创建时间 | S3 | NOT NULL | 列已明确；**覆盖范围待定 D-04** |
| Plan（缺列） | §5.2「计划创建依据」「历史计划保留」无字段 | §5.2 | 历史保留 = 不删除旧行 + STATE-010 | **并入 D-04** |
| Step.id / task_id / plan_id / ordinal | 身份、归属与顺序 | §5.3 | PK；组合外键 `task_plans(id, task_id)`；UNIQUE(plan_id, ordinal) | 已明确 |
| Step.logical_effect_id | 逻辑副作用身份（Operation Key 输入） | §6.3 | 可空 | 列已明确；**是否必填待定 D-11** |
| Step.state / required / version | 状态、必需标记、并发版本 | §8 / §10.3 | NOT NULL；required DEFAULT 1 | 已明确 |
| Step（缺列） | §5.3 输入/类型/前置条件/预期结果/验证要求五要素 | §5.3 | — | **需设计决定 D-08** |
| Attempt.id / step_id | 身份与所属 Step | §5.4 | PK；FK `task_steps(id)` | 已明确 |
| Attempt.operation_id | 本次尝试对应的逻辑操作 | §5.4 | FK `(operation_id, step_id)` → `operation_intents(id, step_id)` | 已明确（保证同 Step） |
| Attempt.attempt_no | 第几次尝试 | §5.4 | UNIQUE(step_id, attempt_no) | 已明确 |
| Attempt.worker_epoch（+ 缺列：执行器身份） | 全局 Worker 纪元；§5.4「哪个执行器执行」的 per-Attempt 身份无列（`step_attempts` 实测无 `worker_id`） | §5.4 / §17.2 | worker_epoch NOT NULL | 纪元已明确；**执行器身份待定 D-15** |
| Attempt.state | 尝试状态 | §10.1 L1029 提及"已中断或待核对" | 部分唯一索引：每 Step 至多一个 ACTIVE | **枚举无来源，待定 D-02** |
| Attempt.started_at_ms / finished_at_ms | 起止时间（未结束可空） | §5.4 | started NOT NULL | 已明确 |
| Attempt（缺列） | 实际执行结果、错误信息、中断/恢复证据 | §5.4 | — | **需设计决定 D-13** |
| Intent.id / step_id | 身份与所属 Step | §5.5 | PK；`step_id` UNIQUE（K0 每 Step 至多一个 Intent） | 已明确 |
| Intent.operation_key | 稳定逻辑操作键 | §6.3 | UNIQUE | 唯一性已明确；**构造算法待定 D-06** |
| Intent.operation_type / tool_name / tool_version | `file.publish.no_clobber` / `filesystem.publisher` / 工具版本 | operation_intent.schema.json / §5.5 | 前两项 const | 已明确 |
| Intent.arguments_digest | 规范化参数摘要（参数原文不落库） | §5.5 | SHA-256 hex | **参数集合待定 D-05**（未闭合则 RECOVERY_PROTOCOL OPEN-02 亦不可闭合） |
| Intent.state / created_at_ms | 核对状态与创建时间 | operation_intent.schema.json | 6 态 | 已明确 |
| Intent（缺列） | §5.5「预期结果」「恢复所需的稳定证据」的通用承载未定；`file_write_intents.expected_*` 是文件专属物化（§10.1 L992 的通用/专属拆分），不能自动充当通用 Intent 字段 | §5.5 / §10.1 L992 | — | **需设计决定 D-16** |
| FileWriteIntent.operation_id | 与 Intent **至多** 1:1（K0 目标设计为 1:1；PK+FK 只给上界 ≤1，存在性由发布前流程/Guard 保证） | S3 | PK + FK → `operation_intents(id)` | 列已明确；**存在性属流程约束** |
| FileWriteIntent.input_snapshot_sha256 | 输入快照 Manifest 的稳定哈希 | S8 FS-015（§12.5） | SHA-256 hex | 已明确 |
| FileWriteIntent.prepared_relpath / target_relpath | 准备产物与目标位置 | §13.2 / §15.2 | NOT NULL | **基准根待定 D-10** |
| FileWriteIntent.expected_sha256 / expected_size | 预期内容哈希与字节数 | §13.2 / §14 | CHECK(expected_size >= 0) | 已明确 |

## 4. 关联键与基数

| 关联 | 键 | 约束层级 | 来源 |
|---|---|---|---|
| Task ↔ Plan | `task_plans.task_id` → `tasks.id`；UNIQUE(task_id, plan_version) | 数据库 | §10.1 L885–901 |
| Task 活动 Plan 唯一 | 部分唯一索引 `ux_active_plan_per_task(state='ACTIVE')` | 数据库 | §10.1 L905–909 |
| active_plan_id 必属同一 Task | `tasks(active_plan_id, id)` → `task_plans(id, task_id)` | 数据库 | §10.1 L877–879 |
| Plan ↔ Step | `task_steps(plan_id, task_id)` → `task_plans(id, task_id)` | 数据库 | §10.1 L928–931 |
| Step ↔ Intent | `operation_intents.step_id` UNIQUE（K0 每 Step 至多一个） | 数据库 | §10.1 L946 / L965–969 |
| Step ↔ FileWriteIntent | **至多 1；DDL 不保证存在性**：`operation_intents.step_id` UNIQUE 只给"每 Step 至多一个 Intent"的上界，`file_write_intents.operation_id` PK+FK 只给"有 Intent 时至多一行 FileWriteIntent"；"每 Step 持有一个 FileWriteIntent" 属 K0 目标设计，存在性由发布前流程与 Guard 负责 | DDL（上界 ≤1）＋ 流程/Guard（存在性） | §10.1 L946 / L973–990；§8.3 L644；§14 步骤 8→9→13→14 L1474–1486 |
| Step ↔ Attempt | `step_attempts.step_id`；UNIQUE(step_id, attempt_no) | 数据库 | §10.1 L1000–1017 |
| Attempt ↔ Intent 同 Step | `step_attempts(operation_id, step_id)` → `operation_intents(id, step_id)` | 数据库 | §10.1 L1014–1015 + database_constraints.yaml |
| 活动 Attempt 唯一 | 部分唯一索引 `ux_active_attempt_per_step(state='ACTIVE')` | 数据库 | §10.1 L1023–1027 |
| Intent ↔ FileWriteIntent | `file_write_intents.operation_id` PK+FK：每个 Intent **至多**一行 FileWriteIntent（K0 目标设计为 1:1，存在性不由 DDL 保证） | 数据库（上界 ≤1）＋ 流程 | §10.1 L973–990 |
| 事件表 `task_events` ↔ 六对象 | `task_id` → `tasks.id`；`(step_id, task_id)` → `task_steps(id, task_id)`；`event_sequence` 为提交顺序权威（不得只依赖毫秒时间戳）；`correlation_id` / `payload_json` 承载事件内容 | 数据库 | §10.1 L1031–1060 |
| 产物表 `artifacts` ↔ 六对象 | `task_id` → `tasks.id`；`artifact_kind ∈ {INPUT_SNAPSHOT, SNAPSHOT_MANIFEST, PREPARED_OUTPUT, FINAL_OUTPUT, VERIFICATION_EVIDENCE}`，它决定 `relative_path` 与 `prepared_relpath` / `target_relpath` 的语义对应；对应关系、写入时机与 `retention_class` 取值属应用层职责 | 数据库（外键）＋ 应用（语义对应）＋ **待定 D-14** | §10.1 L1074–1101 |
| 控制表 `system_control` ↔ 六对象 | 单例全局控制状态（`singleton_id` / `emergency_stop` / `backup_mode` / `worker_epoch` / `updated_at_ms`），不含对象外键；`worker_epoch` 是被 Attempt 行引用的**全局纪元**，只承载 epoch/CAS 语义（= `RECOVERY_PROTOCOL.md` OPEN-06）；Attempt 的执行器身份是**独立议题 D-15**，不由此列承载 | 数据库（无外键） | §10.1 L1062–1074 |
| 逻辑副作用身份 | `step.logical_effect_id` → `operation_key`（派生，非外键） | 应用（来源 §5.4 / §6.3） | §5.4 L362；§6.3 L446–465 |

K0 目标基数（§11.3 L1221–1227 原文）：`1 Task → 1 Plan → 1 Step → 1 FileWriteIntent → 1..N Attempts`。这是**目标设计链，不是 DDL 单独保证的事实**：数据库只提供上界（"每 Step 至多一个 OperationIntent"§10.1 L946，及"每个 Intent 至多一行 FileWriteIntent"L973–990），"每 Step 必有其 FileWriteIntent"的存在性由发布前流程与 Guard 负责（§8.3 L644 `PREPARING → EXECUTING` 必须已提交有效 Intent；§14 步骤 8→9→13→14 L1474–1486）。多 Attempt **MUST** 复用同一 Intent 行（§5.4 L362），不新增 Intent 行、不新增 FileWriteIntent 行。

## 5. 幂等 request digest、Operation Key 与 Attempt 复用

| 摘要 | 覆盖对象 | 规范化 | 存储列 | 状态 |
|---|---|---|---|---|
| `request_digest` | TaskSpec 任务内容（不含 `request_key`） | 固定字段集合 + `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False)` 后 SHA-256 | `tasks.request_digest` | 规则已明确；字段集合待定 D-03 |
| `plan_digest` | Plan 内容 | 同上（提案） | `task_plans.plan_digest` | 覆盖范围待定 D-04 |
| `arguments_digest` | 工具调用规范化参数 | 同上，参数原文不落库 | `operation_intents.arguments_digest` | 参数集合待定 D-05 |
| `input_snapshot_sha256` | 输入快照 Manifest 稳定字节 | Manifest 规范化后哈希 | `file_write_intents.input_snapshot_sha256` | 已明确 |
| `expected_sha256` | 准备产物（= 最终输出字节） | 文件内容 SHA-256 | `file_write_intents.expected_sha256` | 已明确 |

- `operation_key = stable_identifier(task_id, logical_effect_id)`（§6.3）；`logical_effect_id` 在确定性计划创建时生成并持久化于 `task_steps.logical_effect_id`，**MUST NOT** 由每次 Attempt 重新生成（§6.3，对应 INV-003）。
- Attempt 复用规则（§5.4 + §10.1 L1029）：首次执行插入 `(step_id, OP1, attempt_no=1)`；重试只插入 `attempt_no=2..N` 且 `operation_id` 恒为 OP1；崩溃恢复须先把旧行转为"中断/待核对"，再插入新行。在 K0「每 Step 至多一个 Intent」前提下，数据库层 `operation_intents.step_id UNIQUE` 与组合外键共同保证"重试不新建 Intent 行"（该 UNIQUE 只给"每 Step 至多一行"的上界；"该 Step 必有 Intent"的存在性由发布前流程与 Guard 保证，见 §4），与"Intent 必须先于目标副作用提交"（§14 步骤 8→12→14，INV-002）一致。
- K1 重新规划不在本文范围；§6.3 的"已存在副作用续用原 Operation Key、无法判定同一性时不得自动重用"为后续约束。

## 6. 状态与版本

- 版本轴现状：`tasks.version`、`task_steps.version` 已存在并用于 CAS（§10.3：`WHERE state=:expected AND version=:expected`，影响行数 ≠ 1 → `VERSION_CONFLICT`）；`task_plans`、`operation_intents`、`step_attempts`、`file_write_intents` **无 version 列** → D-07。禁止用应用层"先查后改"替代（§10.3）。
- 状态集合唯一来源：Task / Step 取 YAML（S6），Intent 取 JSON Schema（S9）；Plan / Attempt 无来源（D-01 / D-02）。状态枚举 MUST NOT 由数据库、Python、YAML 各自维护（§10.2）。本文不新增或修改任何枚举值。
- Guard：实测 `task_transitions.yaml` 31 条边 / 93 个 Guard 槽位、`step_transitions.yaml` 37 条边 / 111 个 Guard 槽位，唯一 Guard 名仅 3 个（`expected_state_and_version`、`transactional_event`、`domain_invariants_satisfied`，2026-10-12T16:40 实测）。**本文不定义 Guard 语义**（属 K0-002 其他工作项），仅标注其输入对象为 Task/Step 行 + 相关 Intent/Attempt 行 + 审计事件；Guard 的 Epoch/CAS 部分与 `RECOVERY_PROTOCOL.md` OPEN-06（`worker_epoch` 递增与旧 Epoch 拒提交的字段与 CAS 语义）为同一议题，须合并裁定、同一次关闭。
- 跨对象不变量 STATE-001..010（§8.4）由 §4 的关联键与 §8 M2 的内存检查器覆盖；INV-001..018 现状全部 `NOT_IMPLEMENTED`（`contracts/execution_invariants.yaml`，18 条实测），本文不改变其状态。

## 7. 禁止事项（防止新字段自成正式契约）

- 规范词声明：本文 `MUST` / `MUST NOT` 仅为草案内部约定，不产生契约效力；正式效力只来自 `contracts/`（§29）。
- N1 本文出现的任何新字段、新枚举、新算法仅为 DRAFT 提案，写入 `contracts/` 前 **MUST NOT** 被产品代码、测试或领域对象引用为权威。
- N2 本文不新增契约文件、不修改 `contracts/`、`docs/adr/`、任务卡、门禁或台账（作者无此权限）。
- N3 不以"领域模型"为名引入新抽象：不加 Aggregate / Repository / DomainService / EventBus 等模式层，不新增数据库表，不新增状态机。
- N4 不把 D-01..D-16 的任何选项写成已定；本文不关闭任何 D-ID，也不出现对自身产物的冻结/批准/已验证声明。
- N5 不放宽 §11.1 限额、不改 INV-001..018、不改 §16.3 决策表、不改 §15.4 禁止事项。

## 8. 最小下一步方案（可生成 Schema / 领域纯验证对象）

原则：只做现有契约可直接判定的部分；产物一律标记 `evidence_level: contract_fixture`，不接 SQLite、不接执行器、不写运行目录与用户资料（依 FREEZE_PROPOSAL C3/C10）。

| 步 | 产物（建议路径，尚未创建） | 内容 | 前置决定 | 复验归属 |
|---|---|---|---|---|
| M1 | `tests/contract/` 纯函数 + 表驱动测试 | `request_digest(payload)`、`operation_key(task_id, logical_effect_id)`、§16.3 决策表纯映射函数 | D-03 / D-05 / D-06 | K0-011 / K0-013 |
| M2 | `tests/contract/` 内存不变量检查器 | 输入内存元组 {Task, Plan, Step, Intent, Attempt}，断言 STATE-001..004、Attempt↔Intent 同 Step、多 Attempt 复用同一 Intent | 无需新字段 | K0-003 |
| M3 | `tests/contract/` 枚举一致性测试 | 从 `task_states.yaml` / `step_states.yaml` / `operation_intent.schema.json` 读取并比对 Python 常量（单一来源，§10.2） | D-01 / D-02 未闭合前仅覆盖已存在集合 | K0-003+ |
| M4 | Schema 骨架（**本文不创建、本作者不执行**） | inventory 输出 schema、Plan / Attempt 状态集合 —— 对应 §49 DOC-R16 / DOC-R20 的冻结前置条目 | D-01 / D-02 / D-09 | K0-004 |

M1–M3 不需要产品模块、不需要契约冻结，且 MUST NOT 依赖 SQLite（§29 对 `domain/` 的目录约束）；M4 属新增契约，必须先由先生或独立评审决定并计入冻结前置。夹具证据不得用于关闭 K0-011/012/013 的验收测试。

## 9. OPEN / 需设计决定（AI 不得自决；本轮不关闭任何一项，交叉引用仅为"同一次关闭"的记账要求）

| ID | 问题 | 影响对象 | 阻塞 | 建议闭合 |
|---|---|---|---|---|
| D-01 | `task_plans.state` 的枚举无任何契约来源（仅知每 Task 至多一个 ACTIVE） | Plan | M3 / M4 | 规范澄清 |
| D-02 | Attempt 状态枚举无来源；§10.1 L1029 至少暗示"已中断""待核对"两值（后者的命名与 `task_states.yaml` / `step_states.yaml` / Intent 的 `NEEDS_RECONCILIATION` 重叠，须一并裁定） | Attempt | M4 / K0-007 恢复 | 规范澄清 |
| D-03 | `request_digest` 字段集合是否含 `schema_version`（§6.2 仅排除 `request_key`） | Task 幂等 | M1 | 规范澄清 |
| D-04 | `plan_digest` 覆盖范围（是否含 plan_version、激活状态、§5.2 的"计划创建依据"） | Plan | M4 | 规范澄清 |
| D-05 | `arguments_digest` 的参数集合边界（`file.publish.no_clobber` 参数定义）（未闭合则 `RECOVERY_PROTOCOL.md` OPEN-02 亦不可闭合，须同一次关闭） | Intent | M1 / K0-011 | 规范澄清 |
| D-06 | `stable_identifier` 的构造与编码（前缀、拼接、哈希、长度） | Intent 唯一性 / INV-003 | M1 | 规范澄清 |
| D-07 | 四个无 `version` 列对象（plans / intents / attempts / file_write_intents）的并发控制轴 | 全部 | K0-005 | 设计批准 |
| D-08 | §5.3 五要素（输入/类型/前置条件/预期结果/验证要求）是并入 `plan_digest` 覆盖范围还是新增列 | Step | K0-006 | 设计批准 |
| D-09 | Snapshot Manifest ↔ inventory 输出映射：`snapshot_id` 是否进入 inventory、两份上限的关系（= `FILESYSTEM_PROTOCOL.md` 的 O-1，须与该 O-1 同一次关闭） | FileWriteIntent / 产物 | K0-010 / K0-012 | 契约评审 |
| D-10 | `prepared_relpath` / `target_relpath` 的基准根（相对 `state/` 还是 `state/tasks/<task_id>/`） | FileWriteIntent | K0-010 / K0-012 | 契约评审 |
| D-11 | `logical_effect_id` 在 K0 是否对每个 Step 必填（当前 DDL 允许 NULL） | Step ↔ Intent | K0-006 | 设计批准 |
| D-12 | TaskSpec 原文（`input.root` / `output.name`）是否落库——当前 9 表无列，恢复时无法还原授权根；授权根属安全语义（§19.2 L2003"只读取明确授权的输入根目录"、§12.1 L1271"输入目录由可信用户授权"），故属设计决定而非缺陷 | Task / 恢复 | K0-004 / K0-005 | 设计批准 |
| D-13 | Attempt 的实际结果、错误信息、中断/恢复证据落在 `step_attempts` 新列还是 `task_events.payload_json`（与 `RECOVERY_PROTOCOL.md` OPEN-10 同一张表的同一组语义，`event_sequence` 定序为共同约束，须同一次关闭） | Attempt / 恢复 | K0-007 / K0-013 | 设计批准 |
| D-14 | `artifacts` 行与 `prepared_relpath` / `target_relpath` 的对应、写入时机（在 §14 步骤序列的哪一步插入）与 `retention_class` 取值 | artifacts / 恢复核对 | K0-010 / K0-012 / K0-017 | 设计批准 |
| D-15 | Attempt 的执行器身份（`worker_id` 或等价字段）落在 `step_attempts` 还是 `system_control` / Lease 侧——`system_control.worker_epoch` 只是被 Attempt 行引用的全局纪元，不能替代 per-Attempt 身份；本议题与 `RECOVERY_PROTOCOL.md` OPEN-06（epoch 递增与旧 Epoch 拒提交的字段与 CAS 语义）**对象不同、相互独立** | Attempt / K0-007 | K0-007 | 设计批准 |
| D-16 | OperationIntent 的「预期结果」与「恢复所需的稳定证据」的通用承载（通用 Intent 侧字段 vs 依赖专属物化）——`file_write_intents` 只覆盖文件发布 | Intent / 恢复 | K0-011 / K0-013 | 设计批准 |
| DEC-003 | 外接卷平台能力（硬链接 / `O_NOFOLLOW` / 目录 fd / fsync 与目录同步） | 发布与快照协议 | K0-009 / K0-012 | 既有 DEC，本文不重复设计 |
| DEC-004 | 取消请求提交与发布启动的线性化规则（INV-010；= `RECOVERY_PROTOCOL.md` OPEN-01） | Task / Step | K0-014 | 既有 DEC，本文不重复设计 |

## 10. 效力、边界与自证

- §29 效力：`contracts/` 是正式接口契约的唯一来源，`docs/` 下的本文件与 `domain/` 均无契约效力；`domain/` 不依赖 SQLite 的目录约束适用于 §8 的 M1–M3。本文件不具备 `docs/design/FILESYSTEM_PROTOCOL.md`、`docs/design/RECOVERY_PROTOCOL.md` 同级的 Contract Pack 必需文件地位。
- §49 效力：本文件不关闭 §49 的任何 DOC-R 条目（20 项冻结条件只能由先生按 §49 与 §36.1 判定）；M4 对应 DOC-R16（Contract Pack 完整）与 DOC-R20（无未解决阻断性规范冲突）的冻结前置。§30.2-6「对应代码实现」的 `implementation_form` 归属不由本文件裁定。
- 唯一写入为本文件（`docs/design/DOMAIN_CONTRACT_DESIGN.md`，新增）；未修改 `contracts/`、ADR、任务卡、门禁、台账、源码、测试；未运行产生副作用的命令（仅读取、解析、只读 Git 查询与计数）。
- 未标记任何冻结 / 批准 / 已验证 / 完成状态；未代独立评审或独立验收出具结论；未向第三方发送数据。
- 引用规则：本文所有状态与计数均标注读取时刻；工作区并行文件与未跟踪文件数量不固定计数，引用时以实际读取为准。若快照过期，正确处理是重新实测并更新本文件，不得沿用旧值。
- 版本记录：v1（哈希 `287860346e10245329ce53b279bb1e237f3abaa4128fd69764675603ef0148d2`）→ v2（哈希 `bd31e2dd9a8c192dbba3b25fcea0d0f2f1a4f5ffcd47ae9488b99782a1ae14e2`），修订 H1（基数链与三张辅助表）、M1（5 项职责缺列与 D-15/D-16）、M2（REC OPEN 交叉引用）、L1–L5（基数来源、图例、D-12 依据、M4 ↔ DOC-R、规范词声明）与快照计数表述；→ v3，修正 §3 `FileWriteIntent.operation_id` 行、§4 `Step ↔ FileWriteIntent` 与 `Intent ↔ FileWriteIntent` 两行及基数段、§5 复用说明：`step_id UNIQUE`＋PK/FK 只保证"至多 1"，存在性归 K0 目标设计与发布前流程/Guard，本文不再声称 DDL 单独保证存在性；→ v4（本文件 175 行），仅做四处收敛：① 删除 D-02 与 `RECOVERY_PROTOCOL.md` OPEN-11 的交叉引用（两者对象不同：Attempt 状态枚举 vs `operation_intents.state` 的终止条件）；② 厘清 `system_control.worker_epoch` 为 Attempt 行引用的全局纪元、只承载 OPEN-06 的 epoch/CAS 语义，D-15 的执行器身份是独立议题；③ 来源表与快照时刻标注为历史快照；④ 合并 §4 中重复的 artifacts 行。未新增 D-ID，未关闭任何决定项。v1–v4 均为 DRAFT；修复后须由未参与修复者按最新哈希复评。
