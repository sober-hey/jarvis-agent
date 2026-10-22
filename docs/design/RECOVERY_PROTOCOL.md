# JARVIS 恢复协议（DRAFT）

- 状态：**DRAFT / NOT_FROZEN / NOT_APPROVED / NOT_VERIFIED**
- 归属：K0-002-DOC-REC；任务卡 `planning/tasks/K0-002.md`；Contract Pack 1.0-draft / NOT_FROZEN
- 范围：K0 崩溃恢复的**目标语义**。不实现 Reconciler / Recovery Manager，不启用 K0-013 及以后能力
- 效力声明：本文件是设计草案。它不批准任何 ADR、不标记任何契约 `FROZEN`、不构成实现或测试证据；不得被引用为 §49 DOC-R10/11/13/14/15 或任何门禁已通过
- 本次实际完成：仅撰写本文件（只读核对来源）。未运行测试、未改契约/ADR/任务卡、未产生运行数据

## 1. 恢复入口与前置顺序（MP §18、§17）

触发事件（§18 L1876–1883）：`PROCESS_STARTED`、`SYSTEM_RECOVERED`、`TASK_INTERRUPTED`、`TOOL_TIMEOUT`、`UNKNOWN_OUTCOME`、`MANUAL_RECOVERY_REQUEST`。

恢复扫描 MUST 在前置全部完成后才开始（§18.1 L1887–1905；§17.1/17.2）：

1. 取得唯一执行器 `flock`（§17.1；ADR-0001 PROPOSED）。
2. 同一事务内递增持久化 `worker_epoch` 并记录执行器启动事件（§17.2）。
3. 检查数据库完整性。
4. 检查 Emergency Stop；处于停止态则保持停止，只允许只读诊断与必要安全核对（§9.3）。
5. 依序扫描：未完成 Task → 活动 Attempt → 未完成 Intent → 孤儿 Prepared Artifact。
6. 逐项观察真实外部状态 → 生成 Reconciliation Result → 事务化记录 → 再决定是否调度。

Lease 仅用于健康观测；到期不等于允许第二个执行器接管（§17.3）。时间语义见 §17.4（UTC 审计时间、单调时钟超时、休眠后重查）。

## 2. Observation 与 Decision 分离（MP §16.1–16.4）

两层 MUST NOT 合并成单一事实：Observation＝实际观察（只描述"此刻看到什么"），Decision＝恢复决策（由确定性策略产生）。

| Observation | 含义要点（§16.1） |
|---|---|
| `EXPECTED_EFFECT_PRESENT` | 目标存在，类型、大小、SHA-256 与必要元数据均符合预期 |
| `TARGET_ABSENT` | 当前未观察到目标 |
| `CONFLICTING_EFFECT_PRESENT` | 目标存在但类型/哈希/大小不符、是符号链接或其他关键属性冲突 |
| `INSUFFICIENT_EVIDENCE` | 无法安全观察：权限异常、I/O 错误、路径状态不稳定、Prepared 缺失、记录与产物清单冲突 |

Decision：`VERIFY` / `RETRY_SAFE` / `BLOCK` / `FAIL`（§16.2）。字段与枚举见 `contracts/reconciliation.schema.json`。

**核心禁令**：`TARGET_ABSENT` 只表示"此刻没看到"，MUST NOT 据此断言目标历史上从未存在或"未执行"（§16.1 L1684–1688；K0-013 验收 L3550）。v1.1.1 因此取消 `CONFIRMED_NOT_EXECUTED` 作为恢复事实。

## 3. K0 决策表与严格重试前提（MP §16.3）

| Observation | 条件（前提） | Decision |
|---|---|---|
| `EXPECTED_EFFECT_PRESENT` | Intent、哈希、路径均有效 | `VERIFY` |
| `TARGET_ABSENT` | Prepared 完整、允许重试、未取消、未停止 | `RETRY_SAFE` |
| `TARGET_ABSENT` | Prepared 缺失或损坏 | `BLOCK` |
| `CONFLICTING_EFFECT_PRESENT` | 任意 | `BLOCK` |
| `INSUFFICIENT_EVIDENCE` | 任意 | `BLOCK` |

`FAIL` 的产生条件：本表不产生 `FAIL`，这是 MP §16.3 原表的原样（L1739–1747 无 `FAIL` 行）；§16.2 仅给出定义"已经确认任务无法完成，且不存在未解决的危险副作用"（L1735–1737）。故 `FAIL` 的充分条件与禁止条件**未定**（OPEN-12）；关闭前实现 MUST NOT 自行发明该分支的产生路径（`contracts/README.md`：AI 不得自行发明契约字段与状态）。

`RETRY_SAFE` 的严格前提（须全部成立，缺一即降级为 `BLOCK`；§16.2 L1725–1729、§13.3、§15.4）。**可判定性尚未关闭**：第 1、2 条当前无法由契约读出——`contracts/tool_manifest.schema.json` 无恢复策略字段；`operation_intent.schema.json` 无 prepared/expected 字段（须经 `file_write_intent` 的 `prepared_relpath`/`expected_sha256` 间接读出）。该外键绑定 MP §10.1 草案 SQL 已有（`file_write_intents.operation_id → operation_intents(id)`，L985–986），**缺的是契约侧登记**（`database_constraints.yaml` 未登记），属 OPEN-02：

1. 工具恢复策略已证明：在当前操作契约与信任假设下再次尝试不会产生不允许的副作用（INV-005）。
2. Prepared 已达 DURABLE_PREPARED 且复用前哈希复验通过；重试 MUST 复用同一份 Prepared（§13.3 L1444；INV-004）。
3. 重试 MUST 复用同一 `operation_id` 及同一 `operation_key`，不得静默改变原始 Intent（INV-003/004；`operation_key` 唯一约束见 `contracts/database_constraints.yaml` L27）。
4. 取消请求未持久化、Emergency Stop 未激活、执行器所有权与 Epoch 当前有效（INV-008/009/010/011）。
5. 该规则**仅**适用于 K0 原子不覆盖文件发布；MUST NOT 推广到邮件、支付、浏览器提交等外部服务（§16.3 L1749–1751）。

**可达性禁令**：在 OPEN-02 关闭前，MUST NOT 据本草案声称 `RETRY_SAFE` 可达、或声称"满足严格条件时允许安全重试"（§18.2 L1924 与 K0-013 验收 L3553 所要求的路径仍不可执行）；该期间恢复应保守输出 `BLOCK`。

决策 MUST 由确定性策略生成；模型可解释错误，但不得代替 Reconciler 决定是否安全重试（§16.4 L1770–1772）。

## 4. 冲突与证据不足一律阻塞（MP §16.3、§18.4）

- `CONFLICTING_EFFECT_PRESENT` → `BLOCK`：禁止因冲突删除、改名或覆盖目标（§15.4）。
- `INSUFFICIENT_EVIDENCE` → `BLOCK`，含悬空引用路径：`NEEDS_RECONCILIATION` → `INSUFFICIENT_EVIDENCE` → `BLOCKED`（§18.4 L1954–1960）。该三元组是 §18.4 的**明确要求**，MUST NOT 以"目标实际匹配"为由改写为 `VERIFY`。
- `BLOCK` 的语义是"需要人工处理、修复环境或补充证据"（§16.2 L1731–1733）；恢复流程 MUST NOT 以猜测代替证据。
- 两种形态按**事实**区分，MUST NOT 混为一谈：§18.4 的**悬空引用**（数据库记录引用了不存在或不符合预期的恢复产物）固定走三元组 `BLOCKED`；§18.2 L1925 的**结果未记录**（目标已发布、数据库结果尚未提交）是另一类对象，按该条观察目标、正确则补记验证结果。后者不是 §18.4 的例外，也不改变悬空引用仍然 `BLOCK`。

## 5. 取消与紧急停止的持久化（MP §9；INV-010/011）

- 取消请求**不是**任务状态：持久化字段 `cancel_requested_at_ms` / `cancel_requested_by` / `cancel_reason`，可与任意执行状态并存（§9 L699–707）。
- 仲裁：谁先完成数据库提交谁获得状态决定权（§9.1）。`SUCCEEDED` 先提交 → 取消不改变终态；取消先持久化 → 不再启动新的目标副作用、已开始操作进入安全检查点、必须核对可能已发生的副作用、核对后默认 `CANCELED`、已发生副作用保留在执行记录。
- 最终成功提交 MUST 使用"取消请求尚不存在"的条件更新（§9.1 L753；MP 原文为"必须"）。该条件更新只保障**成功/取消的状态仲裁**（终态不被后来的取消请求改写），**不**等于 INV-010 成立：INV-010 另需"取消已持久化后不得启动任何新的目标副作用"，这依赖取消检查与发布启动之间的线性化（见 OPEN-01，当前**未证明**）。
- 条件更新**失败**时：**本条为据 §9.1 推得的草案推断，不是 MP 明文**。仅当失败被确认为"取消请求已存在"所致时，成功路径才 MUST 放弃终态提交并转入取消核对流程（§9.1：不再启动新副作用 → 核对已发生的副作用 → 默认 `CANCELED`）；非取消原因（其他状态/版本冲突）的处置**未定**，MUST NOT 一律按取消处理。该分支的判定方式、对外状态与事件序列、Task 终态主语在 Intent/Step 层的对应，均待 K0-014 定义（OPEN-14）。
- 紧急停止 MUST：不依赖模型、持久化、禁止新任务领取与新目标副作用、尽可能停止活动执行、允许只读诊断与必要安全核对、保留未解决操作（§9.3）。重启后保持停止；重置 MUST 由可信用户操作；重置不等于自动恢复，恢复前先检查未完成 Intent。
- MUST NOT 因取消/停止直接把 RUNNING 改终态并忽略正在进行的文件操作（§9.2）。

## 6. 孤儿产物与悬空引用（MP §18.3–18.4；INV-016）

- 孤儿＝文件系统中存在、但没有被有效数据库记录引用的准备文件或临时文件。
- 清理规则：可在启动时扫描；**不立即删除**；必须确认不属于活动操作；必须满足保留期限；必须记录清理事件；不能删除仍被 Intent 引用的产物。
- 悬空引用＝数据库记录引用不存在或不符合预期的恢复产物；处理固定为 `NEEDS_RECONCILIATION → INSUFFICIENT_EVIDENCE → BLOCKED`。
- **MUST NOT 静默重新读取原始输入并生成替代产物**（§18.4 L1962；§13.3 L1445）。替代产物只能走显式人工授权路径。

## 7. B2 冻结（MP §20；ADR-0005 PROPOSED）

状态机：`NORMAL → BACKUP_REQUESTED → QUIESCING → BACKUP_IN_PROGRESS → RESUMING → NORMAL`（§20.1）。

1. 持久化备份请求；2. 停止领取新任务；3. 阻止启动新的目标副作用；4. 等待活动执行到安全检查点；5. 确认被备份产物不再变化；6. 记录备份开始边界；7. SQLite 一致性备份；8. 复制数据库引用的必要产物；9. 生成 Manifest；10. 验证全部哈希；11. 验证数据库引用关系；12. 原子发布完整备份；13. 记录备份成功；14. 恢复调度。

- 超时：无法在规定时间内进入安全状态时，备份 MUST 失败或推迟；不得复制变化中的产物并标记成功；不得无限等待；必须记录原因（§20.2；原文用"必须"，此处 `MUST` 为其规范化写法，授权见 §4.1 L284）。
- 中断：未完成备份不得标记有效；临时备份目录不得被正式恢复程序接受；恢复启动时检查备份 Manifest；无效备份按保留策略清理（§20.3）。
- 恢复默认只允许恢复到独立目录，按 §20.4 顺序执行；正式生产数据替换必须另行审批（人工保留授权）。
- 本文件不声称已完成任何备份或恢复演练（§20.5 L2163）。

## 8. 与状态机的接口（当前契约现状）

- `NEEDS_RECONCILIATION` 是进入恢复流程的状态（`step_states.yaml` / `task_states.yaml`）；其出边 Guard 仍为占位符（两个 transitions 文件的 notes）。
- `reconciliation.schema.json` 只约束字段与枚举，未约束 Observation×Decision 合法配对；`operation_intents.state` 与恢复结果的映射亦未定义（`K0-002_CONTRACT_GAP_REPORT.md` L14）。
- 因此本文件的决策表是**设计意图**，不是已冻结机器契约；配对表与 Guard 精确化属 K0-002/K0-003 待办。

## 9. OPEN：未确定语义（不得按本文件默认值实现）

| ID | 未确定事项 | 依据/关联 |
|---|---|---|
| OPEN-01 | 取消持久化与"启动新目标副作用"之间的线性化点；检查—发布竞态尚无证明 | §9.1；缺口报告 L19；DEC-004 |
| OPEN-02 | `RETRY_SAFE` 前提的可判定算子（"Prepared 完整""允许重试"如何由 Intent/工具契约读出） | §16.2/§16.3；ADR-0002 |
| OPEN-03 | Observation×Decision 合法配对表与显式禁止配对清单（与 `FILESYSTEM_PROTOCOL.md` FS-053 / O-5 为**同一议题**，须合并裁定、同一次关闭） | §16.3 非穷尽；schema 允许任意组合 |
| OPEN-04 | 孤儿保留期限数值、扫描时机与清理事件字段 | §18.3 只要求"保留期限" |
| OPEN-05 | 悬空引用的显式人工修复路径（授权下是否可重建 Prepared） | §18.4 仅禁止"静默重读输入" |
| OPEN-06 | `worker_epoch` 递增与旧 Epoch 拒提交的字段与 CAS 语义 | §17.2；ADR-0001 PROPOSED |
| OPEN-07 | `MANUAL_RECOVERY_REQUEST` 的调用入口、身份与权限 | §18 L1882；K0-008 未实现 |
| OPEN-08 | QUIESCING 超时值、"安全检查点"定义 | §20.2 |
| OPEN-10 | 恢复审计事件 schema、事件顺序与幂等 | §33；CHG-019/023 |
| OPEN-11 | `operation_intents.state = NEEDS_RECONCILIATION` 的终止条件 | `operation_intent.schema.json` |
| OPEN-12 | `FAIL` 的充分条件与禁止条件（K0 决策表不产生 `FAIL`，其可达性待定） | §16.2 L1735–1737；§16.3 L1739–1747；K0-013 |
| OPEN-14 | 取消条件更新失败分支的判定方式、对外状态与事件序列；Task 终态仲裁在 Intent/Step 层的对应（非取消原因的冲突处置亦未定） | §9.1 L753；K0-014 验收 L3557–3565 |
| OPEN-15 | 数据库完整性检查失败后是否继续恢复扫描（§18.1 只列步骤，未给失败处置） | §18.1 L1894 |

注：原 OPEN-09（本文件仓库归属）**已裁决、不再是未决项**：MP §29 仓库结构（L2894）明确 `docs/design/RECOVERY_PROTOCOL.md`，本文件位置由目录规范确定；§30.1 只规定它是 Contract Pack 必需文件，不改变其路径。原 OPEN-13（§18.2 L1925 与 §18.4 的"分派优先级"）**已撤回**：二者不是同一对象——§18.4 指**恢复产物悬空引用**，§18.2 L1925 指**结果未记录**，不存在优先级冲突；按事实区分，悬空引用仍固定 `BLOCKED`（见 §4）。两条编号均保留不重排，以免已发出的引用失效。

## 10. 风险登记（设计期）

| ID | 风险 | 触发 | 现有缓解条款 |
|---|---|---|---|
| R-01 | 把 `TARGET_ABSENT` 当"从未执行"，重复产生副作用 | 误用 Observation 作法 | §16.1；INV-005 |
| R-02 | 重试时生成不同内容或改动 Intent | 未复验 Prepared / 未复用 Intent | §13.3；§15.4；INV-004 |
| R-03 | 取消提交后仍启动发布 | 检查—发布竞态 | §9.1 条件更新；INV-010（**未证明**） |
| R-04 | 过早清理孤儿，摧毁合法重试依据 | 启动即删除 | §18.3；INV-016 |
| R-05 | 悬空引用时重读原始输入造替代产物 | 静默补产 | §18.4；§13.3 |
| R-06 | 旧 Epoch 或第二执行器提交结果 | 锁/纪元失效 | §17.1/17.2；INV-008/009 |
| R-07 | B2 在写入进行中完成并被当作有效 | 未静默期即备份 | §20.2/20.3 |
| R-08 | 把本草案当作冻结/实现/测试证据 | 引用失控 | 本文件效力声明；§49 DOC-R10/11/13/14/15 |
| R-09 | 决策表被当作完备函数，实现者自行发明 `FAIL` 分支或把不可判定前提当作已判定 | OPEN-02/OPEN-12 未关闭 | 本文件 §3 的显式声明与可达性禁令 |

## 11. 来源与后续

来源（逐条只读核对）：Master Plan v1.1.1 §4.1、§9、§10.1、§13–§16、§17、§18、§20、§29、§30.1、§34.4、§49；`contracts/execution_invariants.yaml`、`reconciliation.schema.json`、`operation_intent.schema.json`、`file_write_intent.schema.json`、`tool_manifest.schema.json`、`step_states.yaml`、`step_transitions.yaml`、`task_states.yaml`、`task_transitions.yaml`、`database_constraints.yaml`、`README.md`；`docs/adr/ADR-0001`、`ADR-0004`、`ADR-0005`（PROPOSED / PENDING_VALIDATION）；`docs/reports/K0-002_CONTRACT_GAP_REPORT.md`、`K0-002-FREEZE_PROPOSAL.md`；`docs/reviews/K0-002-protocol-design-review.md`、`K0-002-protocol-design-review-r2.md`（独立设计评审与复评，修订依据）；`planning/tasks/K0-002.md`、`PROJECT_STATE.md`。

修订记录：2026-10-12 依独立设计评审（r1）修订——采纳 HD-01（`FAIL` 显式化并登记 OPEN-12，不发明路径）、HD-02（`RETRY_SAFE` 可判定性标注 + 可达性禁令）、MD-05（与 FS O-5 交叉引用）、MD-06（条件更新失败分支 + OPEN-14）、MD-02（`MUST` 授权链）、LD-05（表头统一）、LD-06（OPEN-15）；未采纳 MD-01 的改写建议：悬空引用的 `BLOCKED` 是 §18.4 明确要求，不得以目标匹配改写为 `VERIFY`。针对 `FILESYSTEM_PROTOCOL.md` 的 MD-03/MD-04/LD-01..04 不在本文件范围。

r2 复评修订（2026-10-12）：N-01 条件更新失败分支标注为**草案推断**、限定"仅在确认取消请求已存在时"才转取消核对，非取消原因的冲突处置未定（OPEN-14）；N-02 前提 4 引用补 `INV-008`；N-03 注明该外键绑定 MP §10.1 L985–986 草案 SQL 已有、缺口仅在契约侧登记；**撤回原 OPEN-13 的"优先级冲突"**（r2 更正-1：§18.2 L1925 是"结果未记录"、§18.4 是"产物悬空"，非同一对象），§4 改为两形态按事实区分、悬空仍固定 `BLOCKED`。本轮不新增策略，保持 DRAFT。

后续（不属于本子任务）：由**未参与本次修订**的评审者按评审文件 §3 逐条复核（原评审基线哈希已失效）→ 与 `FILESYSTEM_PROTOCOL.md` 的边界划分 → Observation×Decision 配对表的机器契约与表驱动测试（K0-002 范围）→ 恢复流程实现与故障注入（K0-013/K0-014/K0-017，须先满足契约批准与冻结门禁）。
