# K0-002 Step 转换边来源提案（PROPOSAL，非契约）

- 文件身份：`docs/design/STEP_TRANSITION_SOURCE_PROPOSAL.md`（唯一代理新增；作者不参与实现，无任何批准权）
- 状态：**PROPOSED / NOT_APPROVED / NOT_VERIFIED / NOT_FROZEN**——**设计提案，不是正式契约**
- 效力：本文件**不关闭 DOC-R02**、**不批准任何 Guard**、**不冻结**、**不实现状态机**；**不修改 `contracts/`**、ADR、任务卡、门禁、台账；**不删除任何边**
- 目的：为基线评审 F1（`docs/reviews/K0-002-baseline-review.md` §六）记录的「Step 37 边中 11 条在 MP §8.1–8.3 文本查无出处」逐条建立来源登记提案（`source_refs`），供后续判断是否升格为正式来源表；对应 `docs/design/GUARD_CONTRACT_DESIGN.md` §5 的 D-G1a，本文件**只写来源，不写 Guard 谓词**
- 快照（**写入前历史读数**，2026-10-12T23:09–23:15 +08:00，本代理只读实测）：Git HEAD `404b7c3`、`git status --porcelain` 为空——**该 clean 仅指本文件写入之前**；写入后复测：契约仍 37 边、sha1 仍 `6622e931cde7`，`git status --porcelain` 仅本文件为 `??`；`contracts/step_transitions.yaml` sha1 `6622e931cde7` / sha256 `661d36d08515…`——**与 F1 锚点一致，契约自 F1 以来未变**；MP sha256 `800565315537…`（4,734 行，亦与 F1 锚点一致）
- 复核口径：本代理**独立重跑计数脚本并逐名比对边名**（脚本与输出见 §5），不抄录 F1 文本；实测 **37 边 / 26 条有文本出处 / 11 条无出处**，与 F1 及 `GUARD_CONTRACT_DESIGN.md` §5 完全一致

## 1. 计数与边名核对（实测，非引用）

| 量 | 实测值 | 与既有记录 |
|---|---|---|
| 契约边行数 / 去重后 | **37 / 37**（无重复行） | F1 同为 37 |
| Step 状态 / 终态 / 非终态 | 11 / 3（`COMPLETED`,`FAILED`,`CANCELED`）/ 8 | `contracts/step_states.yaml` |
| MP「最宽文本解读」边集 | **26** | F1 `plan-widest: 26` |
| 有 MP 文本出处边 | **26**（37−11） | — |
| **无 MP 出处边（本提案对象）** | **11** | F1 `contract-only: 11` |
| 反向缺口（Plan 可推、契约缺失） | **0**（plan-only 为空） | F1 同为空 |

- 边名核对方法：取契约 `(from,to)` 集合，减去 §8.1–8.3 拼出的边集，差集排序后与 F1 的 11 条**逐名比对**：**11/11 完全一致**——无新增、无改名、无漏报（`CLAIMED→BLOCKED`、`CLAIMED→PENDING`、`EXECUTING→BLOCKED`、`EXECUTING→RETRY_WAIT`、`PREPARING→BLOCKED`、`PREPARING→PENDING`、`RETRY_WAIT→BLOCKED`、`RETRY_WAIT→PENDING`、`VERIFYING→BLOCKED`、`VERIFYING→NEEDS_RECONCILIATION`、`VERIFYING→RETRY_WAIT`）。
- 缺口方向仍为**单向**：契约比 MP 文本更宽。「26」是本提案沿用的 **F1 最宽解读**（兜底规则的边集属解读，MP 未逐条枚举），非 MP 明文计数。

## 2. 11 条无出处边的来源登记提案（`source_refs`）

列义：`source_refs.status = UNRESOLVED / PROPOSED`——「MP 文本无出处（UNRESOLVED）＋ 本提案给出的候选依据（PROPOSED，未经评审与批准，**MUST NOT** 被引用为权威）」。`proposed` 列全部是**本提案新写的候选说明**，依据为契约自身语义与 MP 邻近条款的类比外推。

| # | 边（契约实际边名） | MP §8.1–8.3 出处 | `source_refs` | `proposed`（候选依据族） | 待确认点 |
|---|---|---|---|---|---|
| 1 | `CLAIMED→PENDING` | 无 | UNRESOLVED / PROPOSED | 认领可撤销后回到待认领（与 §8.3 `BLOCKED→PENDING` 句式对称外推） | 撤销认领是否须明确授权（对齐 OPEN-07） |
| 2 | `CLAIMED→BLOCKED` | 无 | UNRESOLVED / PROPOSED | 前置检查发现冲突即阻塞（§8.2「存在冲突→`BLOCKED`」语义前移） | 阻塞理由载体（D-08） |
| 3 | `PREPARING→PENDING` | 无 | UNRESOLVED / PROPOSED | 准备期崩溃恢复后重新排队（§8.2 L638 恢复叙述，未给出边） | 恢复是否允许回到 `PENDING`（§18 归属） |
| 4 | `PREPARING→BLOCKED` | 无 | UNRESOLVED / PROPOSED | 准备工作不可完成 / 缺前置条件 | 同 #2 载体未决 |
| 5 | `EXECUTING→BLOCKED` | 无 | UNRESOLVED / PROPOSED | 执行中发现冲突（§8.2 冲突分支外推） | **须人工确认**：副作用可能已发生时能否直连 `BLOCKED`，而非先转 `NEEDS_RECONCILIATION`（INV-005） |
| 6 | `EXECUTING→RETRY_WAIT` | 无 | UNRESOLVED / PROPOSED | 结果已知且重试安全的短路（§8.3「必须证明重试安全」的简写形式） | **须人工确认**：允许跳过 `NEEDS_RECONCILIATION` 的前提（OPEN-02） |
| 7 | `VERIFYING→NEEDS_RECONCILIATION` | 无 | UNRESOLVED / PROPOSED | 验证期发现结果未知 / 与预期不符 | **须人工确认**：与 Observation×Decision 配对表的绑定（OPEN-03） |
| 8 | `VERIFYING→RETRY_WAIT` | 无 | UNRESOLVED / PROPOSED | 验证不通过且可安全重试 | **须人工确认**：验证失败→重试的判据（OPEN-02） |
| 9 | `VERIFYING→BLOCKED` | 无 | UNRESOLVED / PROPOSED | 验证发现冲突 | 与 #7 的择一条件未定 |
| 10 | `RETRY_WAIT→PENDING` | 无 | UNRESOLVED / PROPOSED | 重试等待结束回到待认领队列 | 退避/重试上限载体（未定义） |
| 11 | `RETRY_WAIT→BLOCKED` | 无 | UNRESOLVED / PROPOSED | 重试不安全或反复失败即阻塞 | 与 §8.3 `NEEDS_RECONCILIATION→RETRY_WAIT`「必须证明重试安全」的衔接 |

- `status` 一律保持 **UNRESOLVED**：MP 中不存在这 11 条的逐条明文，**本文件不使任何一条变为「已解决」**；`PROPOSED` 仅表示“候选依据已写下、待评审”。
- **禁止的处置（沿用 F1 §六末段）**：不得以「MP 没有这条边」为理由删除、合并或缩减这 11 条边；边集合是设计产物，只能由设计依据补齐或人工确认收口。本提案**未删任何边**，契约 sha1 写入前后均应为 `6622e931cde7`。

## 3. 有出处边（26 条）的引用族与需人工确认项

| 族 | 出处 | 边数 | 边集 |
|---|---|---|---|
| A | MP §8.1 正常路径主链 | 5 | `PENDING→CLAIMED`、`CLAIMED→PREPARING`、`PREPARING→EXECUTING`、`EXECUTING→VERIFYING`、`VERIFYING→COMPLETED` |
| B | MP §8.2 异常路径（1 条显式箭头 ＋ 4 条叙述分支） | 5 | `EXECUTING→NEEDS_RECONCILIATION` ＋ `NEEDS_RECONCILIATION→{VERIFYING,RETRY_WAIT,BLOCKED,FAILED}` |
| C | MP §8.3 七条具名约束 | 7 | A 的 5 条 ＋ `NEEDS_RECONCILIATION→RETRY_WAIT`、`BLOCKED→PENDING` |
| D | MP §8.3 兜底规则（任意非终态→`FAILED`/`CANCELED`） | 16 | 8 个非终态 × {`FAILED`,`CANCELED`} |
| — | A∪B∪C∪D（去重） | **26** | 与 §1 实测一致；其中 **7 条同属多族**（A∩C 5 条、B∩C 1 条 `NEEDS_RECONCILIATION→RETRY_WAIT`、B∩D 1 条 `NEEDS_RECONCILIATION→FAILED`） |

**有出处、但依据「条件」面未逐条记载的边**（下列「需人工确认」只约束「语义收口与批准」，不阻断常规来源整理与 DRAFT 候选设计）：

1. **族 D 独有 15 条**（7 个非终态 × {`FAILED`,`CANCELED`} ＋ `NEEDS_RECONCILIATION→CANCELED`）：兜底规则只声明「不得遗留未受控的活动副作用」，**未逐条给出条件**；其中 8 条 `→CANCELED` 与取消线性化耦合（OPEN-01 / DEC-004）——**登记为需人工确认项（语义收口）**；其 Guard 谓词**可继续候选设计（DRAFT）**，仅当把安全语义落为机器契约或声称生效时才需审议，且**任何情况下不得由本文件或候选设计自行批准/生效**。
2. **族 B 中仅由叙述支撑的 2 条**：`NEEDS_RECONCILIATION→VERIFYING`、`NEEDS_RECONCILIATION→BLOCKED`——§8.2 为叙述式分支，「结果符合预期 / 存在冲突」的判定载体未定（OPEN-02/03、D-13）。**登记为需人工确认项（语义收口）**；判据载体补齐属**常规来源整理与候选设计**，**可先行推进、不得因待确认停等**，同样不得自行批准/生效（族 B 另 3 条另有具名或兜底出处）。
3. **族 A/C 的 7 条**（5 条主链 ＋ `NEEDS_RECONCILIATION→RETRY_WAIT` ＋ `BLOCKED→PENDING`）：出处为具名条文，**边本身无需再确认**；其条件语义受未闭合依赖影响，归 `docs/design/GUARD_CONTRACT_DESIGN.md` §4 处理，不在本文件范围。

## 4. 效力边界（明确声明）

- 本文件是**设计提案（PROPOSAL）**，**不是正式契约**：不产生契约效力，不得被 `contracts/`、产品代码、测试或领域对象引用为权威来源。
- **不关闭 DOC-R02**：`docs/design/TRACEABILITY_MATRIX.md` R02 保持 `PARTIAL`；本文件只是该缺口的一项候选登记，关闭须走独立评审＋独立验收＋先生批准（MP §49 / S4）。
- **不批准任何 Guard**：不批准 Guard 语义、不批准 `source_refs` 字段、不批准 `GUARD_CONTRACT_DESIGN.md` §3 谓词草案；Guard 仍未实现、未注册、未测试、未生效。
- **不实现**：无代码；不新增/重命名状态、字段、Guard 名或错误码；不改 `contracts/`（`step_transitions.yaml` 保持 37 边、sha1 `6622e931cde7`）、不改 ADR / 任务卡 / 门禁 / 台账；不删除、不合并任何边。
- **升格路径（未执行，属契约变更，须另行审批）**：① 在契约中增加 `source_refs` 字段并逐边登记 §2 表内容；或 ② 在 MP 补一张机器可解析的 §8.x 来源表，再与 §7.2 同构做集合比对。本文件本身**不落任何机器契约**。
- 未做：未递归委派、未写入 `contracts/`、未跑变异测试、未声称任何转换已受保护或已验收。

## 5. 复核方式（只读，可复现）

- 脚本：一次性临时文件 `/tmp/step_edges_probe.py`（**未入仓库**）。做法：从 `contracts/step_transitions.yaml` 取 `(from,to)` 集合，减去四族来源——§8.1 主链（按行解析状态链折出相邻边）、§8.2（1 显式箭头 ＋ 4 叙述分支）、§8.3 反引号具名边、§8.3 兜底（非终态 × {`FAILED`,`CANCELED`}）——并打印 `contract-only` / `plan-only` 与逐条边名。
- 实测输出（23:10，`.venv/bin/python /tmp/step_edges_probe.py`；系统 `python3` 无 `yaml`，故用仓库 venv）：`edge_lines 37 / unique 37`、`plan-widest 26`、`contract-only 11`（11 条边名与 F1 §六逐名一致）、`plan-only []`、`total 37 / sourced 26 / unsourced 11`。
- **读数纪律**：本文件一切计数与哈希附实测时刻；契约、MP 或台账可能被他人改动，**引用前必须重新实测**，不得沿用本表读数。
