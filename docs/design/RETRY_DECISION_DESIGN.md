# K0 重试决策（Reconciliation Decision）候选设计 —— DRAFT，非契约、非实现

- 文件身份：`docs/design/RETRY_DECISION_DESIGN.md`，本轮**唯一新增文件**；作者不参与实现、无批准权；**未递归委派**。
- 状态：**DRAFT / PROPOSED / NOT_APPROVED / NOT_VERIFIED / NOT_FROZEN**。不批准任何条目，**不关闭 R11**，不关闭 OPEN-01/02/03/06/12 与 FS-053 / O-5。
- 效力：**不实现**确定性纯函数、**不写测试**、**不改** `contracts/`、协议文档、ADR、任务卡、台账、代码、测试；不新增或改名状态、字段、枚举、错误码；不声称任一配对「已允许」「已禁止」「可达」；本文件全部内容为**文本候选**，不含函数、算法、可执行物或已生效策略。
- 快照（写入前只读实测，2026-10-13；**引用前须重新实测**）：HEAD `949419e6`；`contracts/reconciliation.schema.json` `ad5a1166b19b…`；`docs/design/RECOVERY_PROTOCOL.md` `341c2bbebd0d…`；`docs/design/RECONCILIATION_PAIRING_PROPOSAL.md` `85458978ff3a…`（已完成独立评审/验收的停写版）；MP v1.1.1 `800565315537…`；`docs/reports/K0-002-FREEZE_PROPOSAL.md` `ef709ee4c43c…`；本文件写入时 `git status --porcelain` 仅本文件为新增（**写入时历史读数，非不变量**）。
- 目的：为 FREEZE §3 **工作项 9**（R11「确定性重试决策函数＋表驱动测试」，`docs/reports/K0-002-FREEZE_PROPOSAL.md:110`）给出其**前置候选设计**：输入候选域、候选决策表、拒绝原因候选、依赖与证明义务。**纯函数实现与表驱动测试属该工作项，不在本文件范围，本文件未做。**

## 1. OPEN-02 关闭前的保守候选输出（**文本层**）：`BLOCK`

- 兜底规则（**本设计的选择，不是 MP 原文格**）：任一前提不可判定 → 候选输出 `BLOCK`。
- **OPEN-02 关闭前**，`RETRY_SAFE` 的前两条前提（工具恢复策略已证明；Prepared 达 `DURABLE_PREPARED` 且复用前哈希复验）**无法由契约读出**：`contracts/tool_manifest.schema.json` 无恢复策略字段（其 `supports_reconciliation` 布尔**不等于**恢复策略已证明）；`contracts/operation_intent.schema.json` 无 prepared/expected 字段，只能经 `file_write_intent` 的 `prepared_relpath`/`expected_sha256`/`expected_size` 间接读出，而该绑定**未在契约侧登记**。
- 因此：**OPEN-02 关闭前 MUST NOT 声称 `RETRY_SAFE` 可达**，MUST NOT 把「满足严格条件时允许安全重试」写成已生效规则；该期间重试议题的确定性安全输出是 **`BLOCK`**（`docs/design/RECOVERY_PROTOCOL.md` §3 可达性禁令）。
- **`FAIL` 一律不生成**：§16.3 原表无 `FAIL` 行（MP L1739–1747 逐行核对），其充分条件与禁止条件**未定**（OPEN-12）；本设计不发明任何产生路径。
- `VERIFY` 不在本文件主张范围：其原文格条件「Intent、哈希、路径均有效」（MP L1743）的**判定算子载体未登记**（配对提案 #1），属 OPEN-03 待裁定；本文件既不据其主张可达，也不据其主张不可达。
- **本「保守 `BLOCK`」的性质（边界声明）**：它是**文本层设计立场**，**不是策略实现、不是纯函数、不是可执行代码**——本文件未给出任何函数签名、算法、判定过程或可运行逻辑，也未把 `BLOCK` 声明为已生效规则或已接通任何恢复流程；任何引用 MUST NOT 称其为「已实现的恢复策略」。其唯一作用是：在 OPEN-02 关闭前，重试议题**不得**产出 `RETRY_SAFE`。

## 2. 输入候选域（**全部为候选**，无一项因此取得契约或策略地位；「引用来源」列不声称仓库唯一）

| # | 输入候选 | 引用来源 | 契约侧现状（本回合实测） | 在本设计中的地位 |
|---|---|---|---|---|
| 1 | `Observation`（4 值） | `reconciliation.schema.json`；MP §16.1 | 仅有枚举，**不约束配对** | 输入候选 |
| 2 | `Decision`（4 值） | 同上；MP §16.2 | 仅有枚举 | 输出候选；本文件不改枚举 |
| 3 | `operation_intent`（`operation_id`/`operation_key`/`state`） | `contracts/operation_intent.schema.json` | 有；无 prepared/expected/恢复策略 | 输入候选 |
| 4 | `file_write_intent`（`prepared_relpath`/`expected_sha256`/`expected_size`） | `contracts/file_write_intent.schema.json` | 有；**未登记**与 Intent 的绑定 | 输入候选 |
| 5 | `DURABLE_PREPARED`、复用前哈希复验 | MP §13.3 L1444；REC §3 前提 2 | **无字段载体** | 候选前提；不可判定（OPEN-02） |
| 6 | 工具恢复策略已证明（INV-005） | MP §16.2；`execution_invariants.yaml` | 无字段载体 | 候选前提；不可判定（OPEN-02） |
| 7 | 取消（`cancel_requested_at_ms`/`_by`/`_reason`） | MP §9 L699–707 | **字段名精确命中 0；不将大小写/描述文本命中算载体**（`CANCELED` 19 处仅为状态/转换文本） | 候选前提；字段与判定方式未登记 |
| 8 | 紧急停止 | MP §9.3 | **字段名精确命中 0；不将大小写/描述文本命中算载体**（`execution_invariants.yaml:35`「紧急停止必须持久化」仅为不变量文本） | 候选前提；同上 |
| 9 | Epoch / 执行器所有权 | MP §17.2；ADR-0001 PROPOSED | `worker_epoch` **字段名精确命中 0**（`Worker Epoch` 不变量文本 1 处） | 候选前提；CAS 语义未定（OPEN-06） |

- **文本 ≠ 载体**：`contracts/` 中确实存在 `CANCELED`（状态/转换）、`Worker Epoch`、`紧急停止必须持久化` 等**状态/不变量文本**，但**不等于**相应**字段载体或判定载体已定义**；判据是**字段名精确命中**，不得把大小写不敏感命中或描述文本命中算作载体（OPEN-02 / OPEN-06 未闭合）。「引用来源」列亦然：只登记本文所引出处，**不声称**该处或仓库中唯一。

## 3. 候选决策表（候选，非策略；**不得**据此实现或测试）

| # | Observation | 条件候选 | 候选输出 | 依据层 | 文本层规则状态 |
|---|---|---|---|---|---|
| 1 | `EXPECTED_EFFECT_PRESENT` | 「Intent、哈希、路径均有效」 | （候选 `VERIFY`） | MP 原文 L1743 | 判定算子未登记；本文件不主张 |
| 2 | `EXPECTED_EFFECT_PRESENT` | 上述条件不成立或不可判定 | `BLOCK` | 设计兜底（非原文） | 文本层兜底 |
| 3 | `TARGET_ABSENT` | Prepared **缺失或损坏** | `BLOCK` | **MP 原文 L1745** | 「缺失或损坏」判定同受 OPEN-02 约束 |
| 4 | `TARGET_ABSENT` | Prepared **完整、允许重试、未取消、未停止** | 候选 `BLOCK`（**保守降级**） | **MP 原文 L1744 条件存在** ＋ REC §3 可达性禁令 | 前提不可判定（OPEN-02）；**不得声称 `RETRY_SAFE` 可达** |
| 5 | `TARGET_ABSENT` | 取消已持久化 / 停止激活 | `BLOCK` | REC §3 前提 4（草案整合；MP 只给相关原文）；INV-008..011 | 取消—发布线性化未证明（OPEN-01） |
| 6 | `TARGET_ABSENT` | Epoch 或执行器所有权不明/失效 | `BLOCK` | REC §3 前提 4（草案整合；MP 只给相关原文）；OPEN-06 | 未定 |
| 7 | `TARGET_ABSENT` | 无法区分 #3/#4/#5/#6 | `BLOCK` | 设计兜底 | 文本层兜底 |
| 8 | `CONFLICTING_EFFECT_PRESENT` | 「任意」 | `BLOCK` | **MP 原文 L1746**（条件列逐字「任意」） | 原文条件「任意」（非实现证据） |
| 9 | `INSUFFICIENT_EVIDENCE` | 「任意」 | `BLOCK` | **MP 原文 L1747**（条件列逐字「任意」） | 原文条件「任意」（非实现证据） |
| 10 | 任意 | 需要 `FAIL` | **不生成** | §16.3 无 `FAIL` 行；OPEN-12 | 不适用 |

- **「文本层规则状态」列义（列级定义）**：只表示**在文本层**——给定 Observation 与「前提不可判定」这一事实——是否**唯一确定**一个候选输出；**不表示**观察取得、证据取得、前提判定算子或任何实现已具备。
- #3 与 #4 MUST 按事实区分：`TARGET_ABSENT` 只表示「此刻未观察到」，**不证明历史上未执行**（MP §16.1 L1684–1688；v1.1.1 已取消 `CONFIRMED_NOT_EXECUTED`）。
- #4 的原文适用域**仅**为 K0 原子不覆盖文件发布，MUST NOT 推广到邮件、支付、浏览器提交等外部服务（MP L1749–1751）。
- #8/#9 的 `BLOCK` 直接来自已登记的原文条件，不依赖本文件的任何新增条款；`CONFLICTING_EFFECT_PRESENT` 下 MUST NOT 因冲突删除、改名或覆盖目标（§15.4）。

## 4. 拒绝原因候选（设计期标识，**未登记、未批准**）

`schema` 中 `reason_code` 是**开放字符串**（`minLength: 1`，无枚举）；MP §16.4 示例用 `EXPECTED_HASH_MATCH`。故下列仅为便于讨论的**候选标识**：**不得**写成契约枚举，**不得**当作错误码，**不得**作为未来表驱动测试的断言基线（命名须在 OPEN-03 关闭时另行裁定）。

| 候选标识 | 触发格 | 出处 |
|---|---|---|
| `RETRY_PRECONDITION_UNDECIDABLE` | #4 | OPEN-02；REC §3 L51–59 |
| `PREPARED_MISSING_OR_CORRUPT` | #3 | MP L1745 |
| `CONFLICTING_EFFECT_PRESENT` | #8 | MP L1746；§15.4 |
| `INSUFFICIENT_EVIDENCE` | #9 | MP L1747；§18.4 三元组 |
| `CANCEL_OR_STOP_ACTIVE` | #5 | MP §9/§9.3；INV-008..011 |
| `EPOCH_OR_OWNERSHIP_UNRESOLVED` | #6 | MP §17.2；OPEN-06 |
| `UNDECIDABLE_FALLBACK` | #2/#7 | 本设计兜底（非原文） |

## 5. 依赖与证明义务

- **阻塞依赖**（未关闭前本设计不得升格为策略）：OPEN-02（前提可判定算子＋契约侧登记 `file_write_intents.operation_id → operation_intents(id)`，MP §10.1 L985–986 草案 SQL 已有、`database_constraints.yaml` 未登记）；OPEN-03（＝ FS-053 / O-5，配对表与判定算子载体，须**合并裁定、同一次关闭**）；OPEN-01（取消—发布线性化；INV-010 仍 `NOT_IMPLEMENTED`）；OPEN-06（Epoch CAS）；OPEN-12（`FAIL`）。ADR-0001 / ADR-0002 均 **PROPOSED**。
- **证明义务**（若要 `RETRY_SAFE` 可达，须由**未参与本设计者**逐条证明；本文件不承担、未做）：(a) 前提 1 可判定；(b) 前提 2（`DURABLE_PREPARED` ＋复用前复验）可判定；(c) 前提 3（同一 `operation_id`/`operation_key` 复用、不静默改 Intent）可校验；(d) 前提 4 的取消/停止/Epoch 检查与发布启动之间的线性化；(e) 适用域限缩成立。
- 复验归属：本设计随 **K0-013**；纯函数与表驱动测试归 FREEZE 工作项 9，证据等级按 C10 标注 `evidence_level`，且不得用于关闭对应任务自身验收。

## 6. 明确不做（防泛化）

1. **R11 候选只文本**：不实现纯函数、不写测试、不新增 `contracts/examples/` 或 `tests/` 内容、不提供可运行判定逻辑——工作项 9 未开工；本文件的表格与 `BLOCK` 口径 MUST NOT 被当作该函数或其测试基线。
2. **schema 仅来源，不是许可**：4×4 组合结构上均可被 `reconciliation.schema.json` 解析，但**无任一格**因此获得许可；该 schema 只约束字段与枚举、**不约束配对**，与 MP／草案文本同属**来源**，**不构成**对任何 Observation×Decision 的许可或禁止（R10「可解析 ≠ 决策可判定」）。
3. **配对提案仅来源，不是许可**：`docs/design/RECONCILIATION_PAIRING_PROPOSAL.md`（`85458978…`）是**来源登记**（文本关系分类），不是正式来源表、不是机器契约、不是表驱动测试输入，**也不构成任何许可或禁止**；其 #9/#10/#13/#14「与原文冲突」格不构成本设计的输出，本文件也不新增禁止条款。
4. 不把未列配对推为禁止或允许（#2/#3/#5 与 `FAIL` 整列维持 `UNRESOLVED`）。
5. 不替 §16.3 声明完备或非完备（MP 全文「穷尽/完备」0 命中，本回合实测）。
6. 不把 `TARGET_ABSENT` 当「从未执行」；不静默重读原始输入造替代产物（§18.4 L1962）；悬空引用仍固定 `NEEDS_RECONCILIATION → INSUFFICIENT_EVIDENCE → BLOCKED`。
7. 不改 `contracts/`、协议、ADR、任务卡、台账、代码、测试；不关闭 R10/R11 或任何 OPEN；不出现 `FROZEN`/`VERIFIED`/`DONE` 声明。

## 7. 复核方式（只读）

- 原文比对：`sed -n '1739,1751p'` MP（五行决策表＋范围限定）；`sed -n '1651,1738p'` MP（§16.1–16.2 定义）。**本文件的契约侧主张只到「字段载体」层**：`grep -rin 'cancel\|epoch\|emergency\|stop\|halt' contracts/`（大小写不敏感）仅作**命中提示**，本回合实测 **20 处**均为状态/不变量**文本**（`CANCELED` 19 ＋ `Worker Epoch` 1；另见「紧急停止必须持久化」），**不得**当作字段或判定载体存在；字段层须按精确字段名计数：`grep -rin 'cancel_requested\|worker_epoch\|DURABLE' contracts/` 应 **0 命中**（本回合实测 0）。
- 本文件复核：`wc -l docs/design/RETRY_DECISION_DESIGN.md` ≤ 110。`git status --porcelain` 在**本文件写入时**仅本文件为 `??`——**历史读数，不是「应恒等」条件**：并行评审/验收产物随后在仓内出现属正常，不构成漂移。§首快照各 sha256 在引用前**重新实测**，本文件任何读数均不保证有效。
