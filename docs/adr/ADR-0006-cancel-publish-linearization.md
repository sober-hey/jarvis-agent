# ADR-0006：取消提交与文件发布启动的并发线性化

- Status: **DRAFT / PROPOSED / NOT_APPROVED / NOT_VERIFIED / NOT_FROZEN**
- Date: 2026-10-18
- Engineering Plan: v1.1.1 §9.1、§15、§16、§17（§ 行号以 v1.1.1 原文为准；v1.1.2 未改这些条款）
- 关联：`contracts/execution_invariants.yaml` INV-010（`NOT_IMPLEMENTED`）、INV-011；`DECISIONS_REQUIRED.md` DEC-004；`docs/design/FILESYSTEM_PROTOCOL.md` FS-042/FS-047（O-4）；`docs/design/RECOVERY_PROTOCOL.md` OPEN-01/OPEN-03；`docs/design/CANCEL_PUBLICATION_PROPOSAL.md` r3.1；ADR-0001、ADR-0002、ADR-0004
- 机器可读附件：`contracts/draft/cancel_publish_linearization.yaml`（同时 DRAFT，非冻结，非表驱动测试输入）

> **效力声明**：本文件是**设计草案**。它不批准自身、不改 `contracts/` 顶层契约、不改不变量状态、不冻结、不构成实现或测试证据，也不关闭 DEC-004 / O-4 / OPEN-01 / DOC-R13。INV-010 在本 ADR 存续期间保持 `NOT_IMPLEMENTED`。批准权在项目所有者；AI 不得自批核心安全语义。
>
> **过程自述（不可由文件核对）**：本轮只写本文与两份 DRAFT 附件，未写产品代码、未做平台探针、未做竞态实验。文中"可达/不可达"均为**协议层判定**，不是实测结论。

## 1. 背景

INV-010 原文：**"取消提交后不得启动新目标副作用"**（`contracts/execution_invariants.yaml:31-33`）。

现有文本只覆盖终态仲裁：§9.1 要求最终成功提交使用"取消请求尚不存在"的条件更新。该条件更新保证**终态不被后来的取消改写**，但不保证"取消已提交之后不再发出 `link(2)`"。二者之间存在检查—发布窗口：

```
发布:   [读取消=无] ................................ [ os.link ] [记账]
取消:            [begin] [写 cancel_requested] [COMMIT 返回]
                                   ↑ L_cancel
```

若 `L_cancel` 落在方括号读与 `os.link` 之间，条件更新仍会正常成功（因为终态提交发生在 `link` 之后），而目标副作用已经发生。这正是 DEC-004 与 FS-047 / O-4 所指的缺口，也是 `RECOVERY_PROTOCOL.md` 风险 R-03。

本 ADR 的任务**不是**证明 INV-010 已成立，而是把一个含糊的证明义务变成：一组精确符号、一个有限状态集、一个全域单值结果函数、一组可机器判定的断言。

## 2. 范围与非目标

**范围内**：单个发布单元（`operation_type = file.publish.no_clobber`）对单个目标目录项 `state/tasks/<task_id>/output/inventory.json` 的启动时点，与同一 Task 的取消提交之间的顺序关系。

**非目标（明确不主张）**：

1. 不主张 SQLite 与文件系统之间存在跨资源原子事务（FS-034、MP §4.1）。
2. 不主张通用 exactly-once；不主张 `link` 之后可用回滚撤销副作用（§15.4 禁止删除/改名）。
3. 不定义 Emergency Stop 与仲裁锁的优先序（stop 冲突见 `CANCEL_PUBLICATION_PROPOSAL.md` §4，仍 OPEN）。
4. 不裁定 `mkdir` / 目录同步是否属"目标副作用"（见 §6.4 与 §12 U-3）。
5. 不裁定 §14 步骤 13（创建 Attempt）相对临界区的位置（U-2）。
6. 不覆盖多执行器、跨主机、跨卷；A4 假设单主机单执行器。
7. 不涉及 `packaging/`、`apps/`、`subprojects/` 的任何内容。

## 3. 符号与术语

| 符号 | 定义 | 判定方式 |
|---|---|---|
| `u` | 一个发布单元，由 `operation_id` 标识；对同一 `TGT` 至多产生一次副作用 | `operation_intents.operation_id` |
| `TGT(u)` | `state/tasks/<task_id>/output/inventory.json` | FS-045 |
| `CS_pub(u)` | 发布方临界区：取仲裁排他 → 锁内检查 → `mkdir`（如需）→ `link` → 目录同步 → 结果短写提交 → 释放 | 桩内事件 |
| `S_pub(u)` / `E_pub(u)` | `CS_pub(u)` 的起止（获得/释放排他） | 锁获取/释放标记 |
| `S_check(u)` | 锁内**新建事务**的取消/停止/所有权/epoch 读**完成**时刻 | 事务开始序号 + 读返回标记 |
| `S_link(u)` / `E_link(u)` | `link(2)` 进入内核 / 返回的时刻 | 系统调用桩 |
| `S_sync(u)` | 目标目录同步完成时刻 | `fsync` 返回标记 |
| `L_pub(u)` | 发布结果短写事务 COMMIT **成功返回**时刻 | 提交返回标记 |
| `CS_cxl` | 取消方临界区：同一把仲裁排他 → 写 `cancel_requested_*` → COMMIT 返回 → 释放 | 桩内事件 |
| `L_cancel` | `CS_cxl` 内取消写事务 COMMIT **成功返回**的时刻（§9.1 L753 口径） | 提交返回标记 |
| `EFFECT(u)` | 内核为 `TGT(u)` 创建了目录项（副作用已发生） | `lstat(TGT)` 可见 + `link` 返回值 |
| `RECORDED(u)` | 该次发布结果已由**提交成功**的短写事务记录 | 库内行 |

**口径约定（关键）**：`L_cancel` 是**提交成功返回**，不是"开始提交"；`E_pub` 是**释放排他**，必然晚于 `L_pub`。所有先后关系以**锁序/屏障标记**判定，不以跨进程 wall clock 比较（`CANCEL_PUBLICATION_PROPOSAL.md` §5；A5）。

## 4. 决策（候选）：线性化点必须前置于文件系统操作

### 4.1 选定

- **取消的线性化点**：`L_cancel`（取消写事务 COMMIT 返回）。
- **发布"启动"的线性化点**：`S_pub`——即取得覆盖"检查→副作用→记账"全段的仲裁排他的时刻；该临界区的**第一个动作**是锁内新建事务的取消/停止读 `S_check`。
- **副作用的线性化点**：`S_link`（内核创建目录项）。

即：**发布方的决策点前置于文件系统操作**；文件系统操作后不允许再作"是否还要发布"的决策。`L_pub`（结果提交）**不是**启动线性化点。

### 4.2 为什么不能后置到 `link` 之后

若把"启动"线性化到 `E_link` 或 `L_pub`，则下面这个执行是"合法"的：取消在 `E_link` 之前提交，`link` 仍发生，只是记账时才发现取消。该执行满足"提交顺序仲裁"，但违反 INV-010 的字面要求（"不得**启动**新副作用"），并且由于 §15.4 禁止删除目标，副作用无法撤销。所以后置线性化会让 INV-010 变成不可满足的命题。

### 4.3 为什么不是"锁进入顺序"

`flock` 只提供互斥，不提供排队顺序（§4.1、§17.1）。因此不能写"先请求锁者先线性化"。本 ADR 只用两件事：**区间互斥**与**锁内新建读**。公平性、超时、重试都不参与正确性论证。

## 5. 取消到达时刻的发布状态集

`L_cancel` 发生的那一瞬间，发布单元 `u` 只能处于下列 12 个状态之一。**"可达"指在该协议下允许出现，不是指一定出现。**

| # | 状态 | 可观察判据 | 在 `L_cancel` 时刻可达 | 依据 |
|---|---|---|---|---|
| P-1 | `NO_INTENT` | 无引用 `TGT` 的 `operation_intent` 行 | 是 | 无发布单元，INV-010 平凡成立 |
| P0 | `PREPARED_NOT_STARTED` | Intent 已提交（`PREPARED`），从未进入 `CS_pub` | 是 | FS-033：步骤 12 已提交、步骤 14 未开始 |
| P1 | `ACQUIRING` | 发布方已发起取锁，**未持有** | 是 | 取消方持有 `CS_cxl` 时发布方只能等待 |
| P2 | `IN_CS_CHECKED` | 持锁，`S_check` 已完成且判定"无取消" | **否** | 与 `CS_cxl` 互斥；出现即协议违例 |
| P3 | `IN_CS_MKDIR` | 持锁，目标目录已建，`TGT` 未创建 | **否** | 同上；`mkdir` 归属另见 U-3 |
| P4 | `IN_CS_LINK_INFLIGHT` | 持锁，`link` 已进入未返回 | **否** | 同上 |
| P5 | `IN_CS_LINK_OK` | 持锁，`EFFECT = true`，目录同步未完成 | **否** | 同上 |
| P6 | `IN_CS_SYNCED` | 持锁，`EFFECT = true` 且已同步，结果未提交 | **否** | 同上 |
| P7 | `IN_CS_RECORDING` | 持锁，结果短写事务在途 | **否** | `L_pub < E_pub`，取消无法在 `E_pub` 前提交 |
| P8 | `TERMINAL_RECORDED` | `RECORDED = true` 且已释放排他 | 是 | 发布先完成；取消只能作为事后记录 |
| P9 | `ORPHAN_EFFECT_NO_RECORD` | `EFFECT = true`、`RECORDED = false`、无存活持有者 | 是 | 进程在 `link` 后、记账前死亡（§18.2 L1925 类） |
| P10 | `PUBLISHING_NO_EFFECT_NO_RECORD` | `state = PUBLISHING`、无记录、未观察到 `TGT` | 是 | 进程在 `link` 前死亡；与"将发布"不可区分 |

**结论（合法状态集合）**：在 `L_cancel` 时刻，合法集合为 `{P-1, P0, P1, P8, P9, P10}`（6 个）；`{P2…P7}`（6 个）为**禁止集合**。禁止集合非空即说明 A1/A2/A3/A5 中至少一项失效——这本身就是一个可检测的违例信号，而不是需要"再判一次"的模糊情形。

**"部分发布"的定义**：`PARTIAL(u) ≜ EFFECT(u) ∧ ¬RECORDED(u)`，含 P5、P6、P9。该概念**不构成一个成功结果**，也不构成"取消成功且无副作用"。

## 6. 每个状态下的确定性结果

### 6.1 结果枚举（5 值，互斥且覆盖）

| 结果 | 含义 |
|---|---|
| `CANCEL_WINS_NO_EFFECT` | 取消生效为终态，且未观察到该 `TGT` 的任何副作用 |
| `CANCEL_WINS_EFFECT_RETAINED` | 取消生效为终态，但副作用已发生；副作用**保留并记录**，不得删除/改名 |
| `PUBLISH_WINS` | 发布已完成并记录；取消请求被持久化但不改变终态 |
| `RECONCILE_BLOCK` | 证据不足或需人工处置；不重试、不补写、不猜 |
| `PROTOCOL_VIOLATION` | 命中了本应不可达的交错；fail-closed |

### 6.2 结果函数 `f(state, observation) → outcome`

`Observation` 取 `reconciliation.schema.json` 的四个值：`EXPECTED_EFFECT_PRESENT` / `TARGET_ABSENT` / `CONFLICTING_EFFECT_PRESENT` / `INSUFFICIENT_EVIDENCE`。表对 6 个可合法状态 × 4 个 Observation ＝ 24 格**逐一给出结果**；`P2…P7` 全 24 格统一为 `PROTOCOL_VIOLATION`。合计 48 格，无省略格、无默认值。

| state \ obs | `EXPECTED_EFFECT_PRESENT` | `TARGET_ABSENT` | `CONFLICTING_EFFECT_PRESENT` | `INSUFFICIENT_EVIDENCE` |
|---|---|---|---|---|
| P-1 `NO_INTENT` | `RECONCILE_BLOCK` | `CANCEL_WINS_NO_EFFECT` | `RECONCILE_BLOCK` | `RECONCILE_BLOCK` |
| P0 `PREPARED_NOT_STARTED` | `RECONCILE_BLOCK` | `CANCEL_WINS_NO_EFFECT` | `RECONCILE_BLOCK` | `RECONCILE_BLOCK` |
| P1 `ACQUIRING` | `RECONCILE_BLOCK` | `CANCEL_WINS_NO_EFFECT` | `RECONCILE_BLOCK` | `RECONCILE_BLOCK` |
| P8 `TERMINAL_RECORDED` | `PUBLISH_WINS` | `RECONCILE_BLOCK` | `RECONCILE_BLOCK` | `RECONCILE_BLOCK` |
| P9 `ORPHAN_EFFECT_NO_RECORD` | `CANCEL_WINS_EFFECT_RETAINED` | `CANCEL_WINS_NO_EFFECT` | `RECONCILE_BLOCK` | `RECONCILE_BLOCK` |
| P10 `PUBLISHING_NO_EFFECT_NO_RECORD` | `CANCEL_WINS_EFFECT_RETAINED` | `CANCEL_WINS_NO_EFFECT` | `RECONCILE_BLOCK` | `RECONCILE_BLOCK` |
| P2–P7 | `PROTOCOL_VIOLATION`（全格） | 同左 | 同左 | 同左 |

要点：

1. **P1 的确定性**：取消方此刻持有 `CS_cxl` 并已 COMMIT。发布方获锁后 `S_check` 必然读到已提交的取消（A5/A6），MUST NOT `link`。结果不依赖谁"更早请求"。
2. **P-1/P0/P1 观察到目标存在 ⇒ `RECONCILE_BLOCK`**：既有目录项或未记录的历史副作用都可能造成该观察，二者都不能判为本次发布成功；也不构成协议违例（INV-010 只管"新"副作用）。
3. **P9/P10 的 `TARGET_ABSENT` 不得写成"从未执行"**：只表示"此刻未观察到"，`CANCEL_WINS_NO_EFFECT` 的语义是"无副作用需保留"，不是"历史清白"（§16.1 L1684–1688）。
4. **P9 的 `EXPECTED_EFFECT_PRESENT`**：这是"取消已提交 + 副作用已发生"的唯一合法表达。处置固定为：保留目标、补记副作用、Task 终态 `CANCELED`、不自动重试（FS-050、§16.3）。
5. **P10 出现 `EXPECTED_EFFECT_PRESENT`**：说明 `link` 实际已发生但记账丢失，等价于 P9，故同格同结果。
6. **`PROTOCOL_VIOLATION` 不是兜底放行**：它 MUST 记审计事件、MUST NOT 继续发布、MUST NOT 把目标当作成功产物；仅用于 P2–P7 这类结构上不可达的交错。

### 6.3 全域性要求

`f` 必须对 `State × Observation` 的**全部可达组合**有且仅有一个定义，且**不得存在 default 分支**。未定义组合 → `PROTOCOL_VIOLATION` + `reason_code = PAIRING_UNDEFINED`，不得回落到 `CANCEL_WINS_*` 或 `PUBLISH_WINS`。

### 6.4 目录副作用（未裁定项的处理）

若 `mkdir` 被裁定为"目标副作用"，则 `P3` 需从禁止集合移入合法集合，并新增结果格 `P3 × TARGET_ABSENT → CANCEL_WINS_EFFECT_RETAINED`（目录保留，不删）。在此之前，本 ADR 采取保守读法：**`mkdir` 与 `link` 同锁、同在 `CS_pub` 内**，从而不影响 §5 的禁止集合。

### 6.5 与 Reconciliation `decision` 枚举的边界（候选；U-8）

§6.1 的五个结果是**仲裁标签**，不是 `contracts/reconciliation.schema.json` 的 `decision` 枚举值（`VERIFY` / `RETRY_SAFE` / `BLOCK` / `FAIL`）。二者之间不存在一对一映射，本文也不主张存在。但存在一个必须堵住的孔：`reconciliation.schema.json` 的 `additionalProperties:false` 与枚举只约束取值域，**不约束语义搭配**，因此"结果 = `PUBLISH_WINS`，同时 `decision = RETRY_SAFE`"这样的记录在结构上完全合法、在语义上却是重复发布。规则如下（候选，须与 `docs/design/RECONCILIATION_PAIRING_RULES.md` 同一次裁定）：

1. **Observation 绑定固定**：`CANCEL_WINS_NO_EFFECT` 只在 `TARGET_ABSENT` 下成立；`CANCEL_WINS_EFFECT_RETAINED` 与 `PUBLISH_WINS` 只在 `EXPECTED_EFFECT_PRESENT` 下成立；`RECONCILE_BLOCK` 可配任一 Observation；`PROTOCOL_VIOLATION` 不对应任何 Observation。
2. **Decision 只收窄**：按结果限定可落库的 Decision 集合。集合外的组合 **MUST NOT 落库**，输出 `BLOCK` + `PAIRING_FORBIDDEN`，而不是改写成一个能通过的 Decision。
3. **`PROTOCOL_VIOLATION` 不产生 reconciliation 记录**：它是审计事件。`decision` 枚举里没有对应值，硬塞进 `BLOCK` 会让"结构性违例"与"正常阻塞"不可区分（也违反 AST-08 的可判定性要求）。
4. 该约束是**单向**的：本 ADR 不因某格 `ADMISSIBLE` 就主张该结果正确；配对表也不因本表改变任何格的判定。

| 结果 | 允许的 Observation | 允许落库的 Decision | 依据 |
|---|---|---|---|
| `CANCEL_WINS_NO_EFFECT` | `TARGET_ABSENT` | `BLOCK` | 落在 R-07（ADMISSIBLE，条件「Prepared 缺失或损坏」）；条件不成立时按 `PRECONDITION_UNMET` 降级仍为 `BLOCK`，故本行结论不依赖该条件。R-06 `RETRY_SAFE` 因 RP-P1 不可判定而不可达 |
| `CANCEL_WINS_EFFECT_RETAINED` | `EXPECTED_EFFECT_PRESENT` | `VERIFY` 或 `BLOCK` | **U-8**：二者都只做只读确认/阻塞，均不产生新副作用；不得用 `RETRY_SAFE`/`FAIL` |
| `PUBLISH_WINS` | `EXPECTED_EFFECT_PRESENT` | `VERIFY` | R-01 是 16 格中唯一同时 ADMISSIBLE 且与"发布已完成"相容的格 |
| `RECONCILE_BLOCK` | 任一 | `BLOCK` | 与 §6.1 "不重试、不补写、不猜"一致 |
| `PROTOCOL_VIOLATION` | 无 | 无（不落库） | 审计事件，非 Decision |

## 7. 与 INV-010 的对应

### 7.1 形式化

> **(C-1)** 对任一发布单元 `u`：`L_cancel < S_link(u) ⇒ ¬EFFECT(u)`。

这是 INV-010 的可用形式。"启动"＝`S_link` 进入内核（副作用开始生效的时刻），不是记账、不是准备。

### 7.2 机制与证明草图

依赖四条前提：

- **(I) 互斥**：`[S_pub, E_pub]` 与 `[S_cxl, E_cxl]` 不相交（A2 锁身份、A3 flock 排他、A4 单执行器）。
- **(II) 包含**：`S_link ∈ (S_pub, E_pub)`（协议结构：`link` 在锁内）。
- **(III) 取消提交在临界区内**：`S_cxl < L_cancel ≤ E_cxl`（`L_cancel` 后释放锁）。
- **(IV) 锁内新读 + 可见性**：`S_check` 由锁内**新建**事务执行，且 `L_cancel < S_check` 时该读必须看见取消（A5、A6）。

证明：设 `L_cancel < S_link`，且反设 `EFFECT(u)` 成立（即 `link` 已执行）。比较 `S_pub` 与 `L_cancel`：

- 若 `L_cancel < S_pub`：由 (III) `S_cxl < L_cancel < S_pub`，取消临界区在发布临界区开始前已结束；由 (I) 无重叠，故 `S_check` 发生在 `L_cancel` 之后，由 (IV) 必见取消 ⇒ 按协议 MUST NOT `link`，与 `EFFECT` 矛盾。
- 若 `S_pub < L_cancel`：由 (II) `S_pub < S_link < E_pub`，故 `S_pub < L_cancel < E_pub`；又 `S_cxl < L_cancel`，得 `S_cxl < E_pub` 且 `S_pub < E_cxl`，即两临界区重叠，违反 (I)。

两分支均矛盾，故 `¬EFFECT(u)`。∎

注意该证明**不使用**锁公平性、超时、时钟、重试次数，也不依赖"谁先请求"。它把 INV-010 的成立条件压缩为 A1–A6 六项可独立验证的假设。

### 7.3 INV-010 当前状态

| INV-010 分句 | 形式对应 | 状态 |
|---|---|---|
| "取消提交" | `L_cancel`（COMMIT 返回） | 已定义；未实现 |
| "不得启动" | `S_link` 进入内核 | 已定义；未实现 |
| "新目标副作用" | `EFFECT(u)`（目录项创建） | 已定义；`mkdir` 归属 U-3 未定 |
| 整条不变量 | (C-1) | **`NOT_IMPLEMENTED`，未证明，不因本 ADR 改变** |

A1–A6 全部未证：

| 假设 | 内容 | 现状 |
|---|---|---|
| A1 无旁路 | 所有能创建 `TGT` 目录项的路径都在 `CS_pub` 内（清单见提案 §3；非穷举证明） | 未审计，`SITES = ∅`（无实现） |
| A2 锁身份 | 两方锁路径固定、运行中不删除、各自独立 `open` fd、同进程需线程级互斥 | 未验证 |
| A3 排他语义 | 目标卷（含外接硬盘）上 flock 排他且进程终止自动释放 | 未探针（ADR-0001 仅 PROPOSED） |
| A4 单执行器 | 单主机、单执行器、`worker_epoch` 有效 | 未实现（INV-008/009） |
| A5 协议义务 | 检查读必须是获锁后新建事务；写方 COMMIT 返回后才释放锁 | 未实现 |
| A6 平台语义 | WAL + `synchronous=FULL` 下"COMMIT 返回 ⇒ 其后新建事务可见" | 未探针 |

## 8. 可机器验证的断言

以下断言是**验证计划的形式化**，不是已通过的测试。状态列统一为 `NOT_IMPLEMENTED`。

| ID | 类型 | 断言 | 检查方法 |
|---|---|---|---|
| AST-01 | 竞态 | 不存在事件序 `S_pub < L_cancel < E_pub` | 桩内标记 + 共享屏障；禁止跨进程时钟比较 |
| AST-02 | 结构 | `S_pub < S_check`，且 `S_check` 的事务开始序号 > 取锁序号 | 事件日志序号校验 |
| AST-03 | 结构 | `S_check < S_link`（单进程全序） | 事件日志 |
| AST-04 | 可见性 | 若 `L_cancel < S_pub`，则 `S_check` 读到 `cancel_requested_at_ms ≠ NULL` | 注入取消后发布，断言读值 |
| AST-05 | 静态 | `TGT` 目录项创建调用点集合有限，且每个调用点被 `S_pub` 支配；未登记点数为 0 | 调用点清单 + 支配性检查；当前 `SITES = ∅` |
| AST-06 | 表驱动 | 终态集合中任一状态在 `L_cancel` 后不再改变 | 状态转换表 + 条件更新负例 |
| AST-07 | 静态/运行时 | 取消与恢复路径不包含对 `TGT` 的 `unlink`/`rename`/`rmdir` | 系统调用白名单 + 追踪 |
| AST-08 | 表驱动 | `f` 在可达组合上存在且仅存在一个定义；无 default 分支 | 枚举 `State × Observation`，断言匹配数 = 1 |
| AST-09 | 故障注入 | 在 P4/P5/P6 各注入 `kill -9`，恢复后归类与崩溃前状态一致 | 故障注入夹具 |
| AST-10 | 竞态（弱形式） | 若 `L_stop < S_pub`，则 `S_link` 不发生 | 同 AST-01；强形式待 stop 冲突裁定 |
| AST-11 | 一致性 | 若 U-3 裁定 `mkdir` 属副作用，则 AST-01 区间下界替换为 `S_mkdir` 且 `S_mkdir > S_pub` | 契约—实现一致性检查 |
| AST-12 | 单调性 | 取消字段一旦 COMMIT，后续事务不得将其置空 | schema/语句白名单 + 负例 |
| AST-13 | 确定性 | 同一 `(state, observation)` 重复求值结果恒等，不读时钟/模型/环境 | 纯函数属性测试 |
| AST-14 | 崩溃等价 | P9 与 P6 在相同 `observation` 下结果相同（差异仅在 `RECORDED`） | 对照夹具 |
| AST-15 | 跨文档 | 不存在已落库的 `(outcome, observation, decision)` 使 `observation ∉ §6.5 允许集` 或 `decision ∉ §6.5 允许集` | 表驱动联合校验；`PROTOCOL_VIOLATION` 的记录数为 0 |

负例必须具备**真红能力**：删除互斥（AST-01 应红）、改为锁外读（AST-02/AST-04 应红）、加入 default 放行分支（AST-08 应红）。仅"干净对照通过"不构成验证。

## 9. 崩溃、失败与未知结果

| 场景 | 处置（候选规则） |
|---|---|
| 发布方在 `S_check` 前死亡 | 无副作用、无记录（P0/P10）。释放锁后取消可提交；重启按权威读判定，MUST NOT 声称"从未执行" |
| `S_check` 后、`S_link` 前死亡 | 同上，P10；Intent 停在 `PUBLISHING` → 进入 Reconcile，不得因"看起来没做"直接判 `RETRY_SAFE` |
| `link` 已进入、结果未知 | P4/P9 区间。`TARGET_ABSENT` 不足以断言未执行；`INSUFFICIENT_EVIDENCE → BLOCK` |
| `link` 成功、结果短写失败（BUSY/IO，进程存活） | P9 等价：不重试（INV-005）、不删改目标、上报未知/BLOCK |
| `link` 成功、目录同步失败 | 副作用已发生，MUST NOT 称"未发布"；按 FS-063 阻断进入生产等级；进入 Reconcile |
| 取消方在 COMMIT 返回前被 `kill -9` | 持久化状态未知。重启后权威读判定，需同时满足"WAL 恢复完成 + 读本身成功 + 读的是权威库"，否则 `INSUFFICIENT_EVIDENCE → BLOCK` |
| 锁不可得/超时/不支持 flock | 发布方 fail-closed：MUST NOT `link`；超时**不得**被当作锁已释放 |

**不主张**用崩溃后的观察补齐线性化证明：崩溃后只能确定"当前观察"，不能重建"当时顺序"。线性化由协议结构保证，崩溃路径只保证**不产生新的副作用**与**不谎报**。

## 10. 备选方案与取舍

| 方案 | 机制 | 结论 |
|---|---|---|
| C1′ 独立仲裁锁 + DB 两段短事务 | 锁跨 FS 全程；锁内短读、锁外 `link`、`link` 后短写 | **本 ADR 采用**（与提案 r3.1 一致）；偏离 §14 步骤 13 顺序，需单独裁定 |
| C2 写事务跨 `link` 全程 | `BEGIN IMMEDIATE` 跨越文件系统操作 | 可预防，但违反 §10 的短事务要求；若与 Intent 同事务则违反 FS-033/INV-002。不选 |
| C3 纯 DB CAS 令牌 | 检查后写令牌，`link` 在 DB 外 | **不能**预防；窗口无界，仅可作审计补充。不选 |
| 取消经 IPC 交给执行器 | 单一写者 | 执行器不存活时取消无法提交 → 活性不可接受。不选 |

## 11. 后果

**正面**：INV-010 从"无法证明"变为"条件可枚举、可逐条验证"；`P2…P7` 提供运行时违例检测点；结果函数全域单值，取消与发布不再需要事后猜测。

**代价**：发布路径必须持锁跨越 `mkdir`/`link`/目录同步，慢速卷上会延长取消延迟；stop 与取消争用同一把锁可能损害"停止必须及时持久化"（INV-011），该冲突**未裁定**。

**风险**：若 A1 存在清单外旁路，本 ADR 的结论整体失效且不会自证；若 A2 的 fd 语义不成立（同进程重复 flock 不互斥），互斥假象会导致静默违例。两项均需独立审计/探针。

## 12. 本 ADR 不裁定的未决项

| ID | 事项 | 归属 |
|---|---|---|
| U-1 | 形态选择 C1′ vs C2，及对 §10 短事务要求的修订 | 所有者（DEC-004 语境） |
| U-2 | §14 步骤 13（创建 Attempt）在临界区内/外的位置 | FS-032/§14 修订 |
| U-3 | `mkdir`/目录同步是否属"目标副作用" | FS-047 语境 |
| U-4 | 锁持有上界、超时语义、stop 与取消的优先序（INV-011） | §9.3 / §17.1 |
| U-5 | A1 旁路清单的穷举责任与审计时点 | 安全审计 |
| U-6 | 哈希复验相对取锁的顺序 | FS-042 |
| U-7 | `link` 后取消提交时的终态归属与副作用记录主语 | OPEN-03/OPEN-14 合并裁定 |
| U-8 | `CANCEL_WINS_EFFECT_RETAINED` 落库时用 `VERIFY` 还是 `BLOCK`（两者都 ADMISSIBLE/UNRESOLVED，语义不同：前者为只读确认后收尾，后者为阻断待人工） | §6.5；与 OPEN-03 合并裁定 |

## 13. 验证要求与冻结条件

**验证要求**（全部满足前不得声称 INV-010 成立）：

1. A2/A3/A6 平台探针：目标卷（含至少一块外接硬盘）上的 flock 排他、终止释放、同进程 fd 语义、WAL 可见性。
2. AST-01…AST-15 实现并有真红负例；AST-15 需以「向 §6.5 允许集加入 `RETRY_SAFE`」作变异。
3. 独立评审 + 独立验收；评审对象须绑定提交与文件哈希。
4. 产品代码中的 `SITES` 清单与 A1 审计结论一致。

**冻结条件**：

- 上述验证证据齐备且可复现；
- U-1…U-8 已由所有者裁定；
- 契约与实现一致，`contracts/execution_invariants.yaml` 中 INV-010 的 `verification_status` 有证据支撑；
- 获得项目所有者具名批准。

当前均未满足。**本 ADR 不得被引用为 INV-010 通过、DEC-004 关闭、O-4/OPEN-01 关闭或任何门禁通过。**

## 14. 状态与审批记录

| 日期 | 事件 | 结果 |
|---|---|---|
| 2026-10-18 | 建立草案（DRAFT） | 仅文本；无实现、无探针、无竞态实验 |
| — | 独立设计评审 | 待进行 |
| 2026-10-18 | 补 §6.5 outcome→Decision 边界与 AST-15（文本） | 仅文本；新增 U-8，仍 DRAFT |
| — | 所有者裁定 U-1…U-8 | 未发生 |
| — | 批准 | **未批准（NOT_APPROVED）** |

## 15. 来源

MP v1.1.1 §4.1 L272–284、§9.1 L697–760、§10 L810–832、§11.3 L1249–1251、§13 L1398–1454、§14 L1455–1504、§15.1–15.5 L1536–1648、§16.1–16.4 L1666–1772、§17.1–17.2 L1794–1826、§18.2 L1924–1925；`contracts/execution_invariants.yaml:31-33`；`contracts/operation_intent.schema.json`、`contracts/file_write_intent.schema.json`、`contracts/reconciliation.schema.json`；`docs/design/FILESYSTEM_PROTOCOL.md` FS-032/033/034/040/042/043/045/047/050/063；`docs/design/RECOVERY_PROTOCOL.md` §3、§5、OPEN-01、OPEN-03、R-03；`docs/design/CANCEL_PUBLICATION_PROPOSAL.md` r3.1 §2–§6；`docs/adr/ADR-0001`、`ADR-0002`、`ADR-0004`；`DECISIONS_REQUIRED.md` DEC-004；`planning/tasks/K0-002.md:33,41`。
