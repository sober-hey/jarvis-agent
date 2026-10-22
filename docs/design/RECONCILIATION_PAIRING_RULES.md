# K0-002 Reconciliation 配对与重试前提规则（DRAFT / 候选）

- 状态：**DRAFT / PROPOSED / NOT_APPROVED / NOT_VERIFIED / NOT_FROZEN**
- 归属：`docs/design/RECOVERY_PROTOCOL.md` OPEN-03（＝`docs/design/FILESYSTEM_PROTOCOL.md` FS-053 / O-5，同一议题，须合并裁定、同一次关闭）、OPEN-02、OPEN-12；任务卡 `planning/tasks/K0-002.md:30,33`
- 机器可读附件：`contracts/draft/reconciliation_pairing.yaml`（同为 DRAFT，非冻结，非测试许可输入）
- 与既有材料的关系：`docs/design/RECONCILIATION_PAIRING_PROPOSAL.md` 只做**来源登记**；本文在其登记之上补一层**可判定的配对判定函数**与**重试前提谓词**，并明确哪些格仍然无解。本文**不改变**任何格的未决状态，**不关闭** OPEN-02/03/12、R10/R11 或 FS-053/O-5。
- 效力声明：不改 `contracts/reconciliation.schema.json`、不改状态机、不实现 Reconciler、不构成测试证据。批准权在项目所有者。

> 过程自述（不可由文件核对）：本轮只写文本与 DRAFT 附件，未实现、未跑恢复夹具、未做表驱动测试。

## 1. 问题

`reconciliation.schema.json` 只约束字段与枚举（`additionalProperties: false`），**不约束配对**：4 Observation × 4 Decision = 16 种组合在结构上全部可解析。可解析不等于可执行——`VERIFY` 与 `RETRY_SAFE` 的后果不同（前者只读确认，后者会产生新的副作用），因此"任意组合都能落库"是安全缺口。

需要补的不是新枚举，而是一个**判定函数**：给定 `(observation, decision, context)`，返回该配对是"文本允许""文本禁止"还是"未定"，并规定未定时运行时的行为。

## 2. 三层模型与判定函数

| 层 | 内容 | 载体 | 现状 |
|---|---|---|---|
| L0 结构 | 字段、枚举、`additionalProperties:false` | `reconciliation.schema.json` | 已存在 |
| L1 配对 | 本文 §3 的 16 格判定 | 本文 + `contracts/draft/reconciliation_pairing.yaml` | **DRAFT 候选** |
| L2 全域化 | 未定格在运行时的保守输出 | 本文 §4 | **DRAFT 候选，需批准** |

判定函数：

```
pair(observation, decision, ctx) -> PairingVerdict
PairingVerdict ::= ADMISSIBLE | INADMISSIBLE | UNRESOLVED
```

语义严格限定：

- `ADMISSIBLE`＝原文为这一配对写出了 Decision，**且**该行的条件在 `ctx` 下成立。它不表示"该决策正确"，只表示"这个格子允许被考虑"。
- `INADMISSIBLE`＝原文对该 Observation 给出的 Decision 与本次不同，且条件列为"任意"（即无条件覆盖）。这是**原文冲突**，不是新增禁令。
- `UNRESOLVED`＝原文未覆盖该组合，或该组合依赖未定的产生条件。**既不推为允许，也不推为禁止。**

## 3. 16 格判定表

`来源`列：`§16.3 Lnnnn` 为 MP 原文行；`无` 表示原文未覆盖。条件逐字引自 §16.3 条件列。

| # | Observation × Decision | 判定 | 条件（原文） | 来源 | 待确认 |
|---|---|---|---|---|---|
| 1 | `EXPECTED_EFFECT_PRESENT` × `VERIFY` | `ADMISSIBLE` | "Intent、哈希、路径均有效" | §16.3 L1743 | 条件判定算子未登记 |
| 2 | `EXPECTED_EFFECT_PRESENT` × `RETRY_SAFE` | `UNRESOLVED` | — | 无 | OPEN-03 |
| 3 | `EXPECTED_EFFECT_PRESENT` × `BLOCK` | `UNRESOLVED` | — | 无 | OPEN-03 |
| 4 | `EXPECTED_EFFECT_PRESENT` × `FAIL` | `UNRESOLVED` | — | §16.2 仅定义 | OPEN-12 |
| 5 | `TARGET_ABSENT` × `VERIFY` | `UNRESOLVED` | — | 无 | OPEN-03 |
| 6 | `TARGET_ABSENT` × `RETRY_SAFE` | `ADMISSIBLE` | "Prepared 完整、允许重试、未取消、未停止" | §16.3 L1744 | OPEN-02；范围仅限 K0 no-clobber（L1749–1751） |
| 7 | `TARGET_ABSENT` × `BLOCK` | `ADMISSIBLE` | "Prepared 缺失或损坏" | §16.3 L1745 | 条件可判定性 |
| 8 | `TARGET_ABSENT` × `FAIL` | `UNRESOLVED` | — | §16.2 仅定义 | OPEN-12 |
| 9 | `CONFLICTING_EFFECT_PRESENT` × `VERIFY` | `INADMISSIBLE` | "任意 → BLOCK" | §16.3 L1746 | — |
| 10 | `CONFLICTING_EFFECT_PRESENT` × `RETRY_SAFE` | `INADMISSIBLE` | "任意 → BLOCK" | §16.3 L1746 | — |
| 11 | `CONFLICTING_EFFECT_PRESENT` × `BLOCK` | `ADMISSIBLE` | "任意" | §16.3 L1746 | 禁止删/改名/覆盖（§15.4） |
| 12 | `CONFLICTING_EFFECT_PRESENT` × `FAIL` | `UNRESOLVED` | — | §16.2 仅定义 | OPEN-12 |
| 13 | `INSUFFICIENT_EVIDENCE` × `VERIFY` | `INADMISSIBLE` | "任意 → BLOCK" | §16.3 L1747 | — |
| 14 | `INSUFFICIENT_EVIDENCE` × `RETRY_SAFE` | `INADMISSIBLE` | "任意 → BLOCK" | §16.3 L1747 | — |
| 15 | `INSUFFICIENT_EVIDENCE` × `BLOCK` | `ADMISSIBLE` | "任意" | §16.3 L1747 | 悬空引用固定三元组（§18.4） |
| 16 | `INSUFFICIENT_EVIDENCE` × `FAIL` | `UNRESOLVED` | — | §16.2 仅定义 | OPEN-12 |

合计：`ADMISSIBLE` 5（#1、#6、#7、#11、#15）＋`INADMISSIBLE` 4（#9、#10、#13、#14）＋`UNRESOLVED` 7（#2、#3、#4、#5、#8、#12、#16）＝16。

**已知张力（不自行消解，登记为 PR-01）**：`CONFLICTING_EFFECT_PRESENT` / `INSUFFICIENT_EVIDENCE` 两行的条件列是"任意"，字面覆盖 `FAIL`，故 #12/#16 也可读作 `INADMISSIBLE`。本文沿用既有登记的 `UNRESOLVED`（理由：`FAIL` 的产生条件本身未定，OPEN-12），但把两种读法都写出来，交由裁定。**本文不据此判 #12/#16 为允许。**

## 4. 运行时全域化（候选，fail-closed）

配对表在 7 格上无解，但运行时必须总能给出结论。候选规则（**需批准**，不是已生效语义）：

| 判定 | 运行时行为 | `reason_code` |
|---|---|---|
| `ADMISSIBLE` 且条件成立 | 允许产生该 Decision | 由决策本身给出 |
| `ADMISSIBLE` 但条件不成立或不可判定 | 降级为 `BLOCK` | `PRECONDITION_UNMET` |
| `INADMISSIBLE` | 拒绝该 Decision，改写为 `BLOCK` | `PAIRING_FORBIDDEN` |
| `UNRESOLVED` | 一律 `BLOCK` | `PAIRING_UNRESOLVED` |
| 表外输入（非法枚举值） | 拒绝，不落库 | `INPUT_INVALID` |

要点：

1. 未定格一律**降级为阻塞**，不是"按最可能的决策继续"。这是**收紧**方向，但仍属策略变更，需批准。
2. `FAIL` 在 K0 决策表中不可达（§16.3 无 `FAIL` 行）。因此任何产生 `FAIL` 的路径都必须是 `PAIRING_UNRESOLVED` → `BLOCK`，不得实现一个"看起来合理"的 `FAIL` 分支（OPEN-12）。
3. 禁止 default-allow 分支。函数必须对 16 格逐一显式覆盖，缺格即缺陷。
4. 判定函数**不得**读取时钟、模型输出、环境变量或上一条记录；同一输入必须恒等输出。

## 5. 重试前提（`RETRY_SAFE`）的可判定谓词

`TARGET_ABSENT → RETRY_SAFE` 的前提是 5 条**合取**（`RECOVERY_PROTOCOL.md` §3）。本文把它们改写成可判定的谓词，并标出当前可判定性：

| ID | 谓词 | 需要的证据字段 | 当前可判定 | 阻塞项 |
|---|---|---|---|---|
| RP-P1 | 工具恢复策略已证明：再次执行不产生不允许的副作用 | `tool_manifest` 的恢复策略字段 | **否** | schema 无该字段（OPEN-02） |
| RP-P2 | Prepared 达 `DURABLE_PREPARED` 且复用前哈希复验通过 | `file_write_intent.prepared_relpath` + `expected_sha256` + 实测 sha256 | **部分是**：字段可读，持久化等级未落库 | FS-031；需实读 |
| RP-P3 | 复用同一 `operation_id` 与 `operation_key`，不改原始 Intent | `operation_intents.operation_key`（唯一） | 是 | 无 |
| RP-P4 | 未取消、未停止、所有权与 epoch 有效 | `cancel_requested_at_ms` 为空；stop 未激活；`worker_epoch` 匹配 | **部分是**：取消可读；epoch CAS 未实现（OPEN-06） | INV-008/009/010 |
| RP-P5 | 仅适用于 K0 原子不覆盖文件发布 | `operation_type = file.publish.no_clobber` | 是 | 不得推广到邮件/支付/浏览器提交 |

规则：

- 任一谓词为"否"或"不可判定" ⇒ **`RETRY_SAFE` 不可产生**，输出 `BLOCK` + `RETRY_PRECONDITION_UNDECIDABLE`。
- 因此**当前 K0 下 `RETRY_SAFE` 不可达**（RP-P1 不可判定、RP-P4 依赖未证明的 INV-010）。该结论是保守输出，不构成"永久禁止重试"的语义。
- `RETRY_SAFE` 不豁免取消：若 `cancel_requested_at_ms` 非空，即使其余四条成立也必须 `BLOCK`（并与 `docs/adr/ADR-0006-cancel-publish-linearization.md` 的 P9/P10 结果一致）。

## 6. 结果记录的形状（不改 schema）

`reconciliation.schema.json` 为 `additionalProperties:false`，因此**不能**直接新增顶层字段。配对判定的留证放在 `evidence`（`type: object`，未加约束）内：

```json
{
  "schema_version": 1,
  "operation_id": "op-...",
  "observation": "TARGET_ABSENT",
  "decision": "BLOCK",
  "reason_code": "PAIRING_UNRESOLVED",
  "evidence": {
    "pairing": {
      "rule_id": "R-05",
      "verdict": "UNRESOLVED",
      "table_version": "draft-2026-10-18",
      "predicates": {"RP-P1": "UNDECIDABLE", "RP-P2": "TRUE", "RP-P3": "TRUE", "RP-P4": "TRUE", "RP-P5": "TRUE"}
    }
  }
}
```

约束：

1. `pairing.verdict` 必须等于用 `(observation, decision)` 对表求值的结果；不自洽的记录必须被拒绝落库。
2. `evidence.pairing` 是**留证**，不是新的领域状态；不得被状态机读取为转移条件。
3. 判定必须先于落库执行（先校验后写），不允许"先写再补判"。

### 6.1 与 ADR-0006 结果集的联合约束（候选）

`docs/adr/ADR-0006-cancel-publish-linearization.md` §6.5 定义的是**取消—发布仲裁结果**（`CANCEL_WINS_NO_EFFECT` / `CANCEL_WINS_EFFECT_RETAINED` / `PUBLISH_WINS` / `RECONCILE_BLOCK` / `PROTOCOL_VIOLATION`），不是本表的 `decision` 枚举值。两张表互不改判，但同一条记录不得自相矛盾：

| 结果 | 允许的 Observation | 允许落库的 Decision | 落到本表的格 |
|---|---|---|---|
| `CANCEL_WINS_NO_EFFECT` | `TARGET_ABSENT` | `BLOCK` | #7（ADMISSIBLE）；#6 `RETRY_SAFE` 因 RP-P1 不可判定而不可达 |
| `CANCEL_WINS_EFFECT_RETAINED` | `EXPECTED_EFFECT_PRESENT` | `VERIFY` 或 `BLOCK` | #1（ADMISSIBLE）或 #3（UNRESOLVED→BLOCK）；**U-8 未裁定** |
| `PUBLISH_WINS` | `EXPECTED_EFFECT_PRESENT` | `VERIFY` | #1（ADMISSIBLE） |
| `RECONCILE_BLOCK` | 任一 | `BLOCK` | #7/#11/#15（ADMISSIBLE）或对应 UNRESOLVED 格 |
| `PROTOCOL_VIOLATION` | 无 | 无（不落库） | —（审计事件） |

要点：

1. 该约束是**单向**的：本表不因某格 `ADMISSIBLE` 就主张某仲裁结果正确；ADR 也不因本表改变任何格判定。
2. 出现"结果要求 `RETRY_SAFE`"即说明某处把"发布已完成"与"可重试"混为一谈——按 `PAIRING_FORBIDDEN` 拒绝落库，不改写成 `BLOCK` 后放行（拒绝 ≠ 降级，前者留证）。
3. `PROTOCOL_VIOLATION` 在 `decision` 枚举中无对应值，因此**必须**走独立审计通道；把它塞进 `BLOCK` 会让结构性违例与正常阻塞不可区分。
4. 机器可读形态：`contracts/draft/cancel_publish_linearization.yaml` 的 `outcome_decision_boundary`；联合断言见本文 RP-11 与 ADR-0006 AST-15。

## 7. 可机器验证的断言

| ID | 断言 | 检查方法 | 状态 |
|---|---|---|---|
| RP-01 | schema 顶层字段与 4 个枚举值不变 | 与 `reconciliation.schema.json` 逐字段比对 | NOT_IMPLEMENTED |
| RP-02 | 判定函数是 `(observation, decision, ctx)` 的纯函数 | 属性测试：相同输入恒等输出 | NOT_IMPLEMENTED |
| RP-03 | 16 格全部有且仅有一个判定 | 枚举 4×4，断言匹配数 = 1 | NOT_IMPLEMENTED |
| RP-04 | 判定计数恒为 5 / 4 / 7 | 表驱动计数断言 | NOT_IMPLEMENTED |
| RP-05 | 无 default-allow 分支；缺格 → `INPUT_INVALID` | 变异：删一格必须变红 | NOT_IMPLEMENTED |
| RP-06 | `INADMISSIBLE` 的 4 格在运行时改写为 `BLOCK`/`PAIRING_FORBIDDEN` | 表驱动负例 | NOT_IMPLEMENTED |
| RP-07 | `UNRESOLVED` 一律 `BLOCK`，不得产出 `FAIL` | 表驱动负例 | NOT_IMPLEMENTED |
| RP-08 | 任一 RP-Pn 不可判定 ⇒ 不产生 `RETRY_SAFE` | 谓词桩 + 负例 | NOT_IMPLEMENTED |
| RP-09 | 落库记录的 `pairing.verdict` 与表求值一致 | 写入前后比对 | NOT_IMPLEMENTED |
| RP-10 | 判定不读时钟/模型/环境 | 依赖注入白名单 | NOT_IMPLEMENTED |
| RP-11 | 落库 `(observation, decision)` 与 ADR-0006 结果的允许集不冲突（XR-01/XR-02），且 `PROTOCOL_VIOLATION` 落库数为 0 | 跨文档表驱动联合校验 | NOT_IMPLEMENTED |

负例必须具备真红能力：把 #9 改成 `ADMISSIBLE`、给 `UNRESOLVED` 加放行分支、删除 #16，三种变异都应导致测试失败。

## 8. 不做的事

1. 不把"未列"推为允许或禁止；#2/#3/#5 与 `FAIL` 列保持 `UNRESOLVED`。
2. 不为 `FAIL` 发明产生条件（OPEN-12）。
3. 不把 `TARGET_ABSENT` 当"从未执行"（v1.1.1 已取消 `CONFIRMED_NOT_EXECUTED`）。
4. 不声称 `RETRY_SAFE` 已可达；OPEN-02 关闭前维持保守 `BLOCK`。
5. 不改 `reconciliation.schema.json`，不加枚举值、不加状态。
6. 不实现 §6.1 的联合约束，也不替 `U-8`（`CANCEL_WINS_EFFECT_RETAINED` 用 `VERIFY` 还是 `BLOCK`）作裁定。
7. 不关闭 OPEN-02/03/12、R10/R11、FS-053/O-5；升格须独立评审＋独立验收＋所有者批准，并与 FS-053/O-5 合并裁定、同一次关闭。

## 9. 来源

MP v1.1.1 §15.4、§16.1 L1684–1688、§16.2 L1712–1737、§16.3 L1739–1751、§18.4 L1946–1962；`contracts/reconciliation.schema.json`；`contracts/tool_manifest.schema.json`（无恢复策略字段）；`contracts/operation_intent.schema.json`、`contracts/file_write_intent.schema.json`、`contracts/database_constraints.yaml`；`docs/design/RECOVERY_PROTOCOL.md` §2–§5、OPEN-02/03/06/12、R-01/R-09；`docs/design/FILESYSTEM_PROTOCOL.md` FS-052/FS-053；`docs/design/RECONCILIATION_PAIRING_PROPOSAL.md`；`docs/adr/ADR-0006-cancel-publish-linearization.md`；`planning/tasks/K0-002.md:30,33`。
