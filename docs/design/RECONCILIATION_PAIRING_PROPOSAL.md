# K0-002 Reconciliation Observation×Decision 配对来源登记（PROPOSAL，非契约）

- 文件身份：`docs/design/RECONCILIATION_PAIRING_PROPOSAL.md`——本轮**唯一新增文件**；作者不参与实现、无任何批准权；**未递归委派**。
- 状态：**DRAFT / PROPOSED / NOT_APPROVED / NOT_VERIFIED / NOT_FROZEN**。**不批准**任何条目，**不关闭 R10**，**不关闭 OPEN-02 / OPEN-03 / OPEN-12**（含 `docs/design/FILESYSTEM_PROTOCOL.md` FS-053 / O-5）。
- 效力：不改 `contracts/`（尤其 `reconciliation.schema.json`）、不改协议文档 / ADR / 任务卡 / 代码 / 测试；不新增状态、字段、枚举值或错误码；不声称任一格「已允许」「已禁止」「可达」。
- 目的：为 OPEN-03（＝ FS-053 / O-5）所需的 4 Observation × 4 Decision **完整 16 格**建立**来源登记**——只登记文本关系，不定策略。
- 快照（本代理只读实测，2026-10-13；**写入前读数，引用前须重新实测**）：HEAD `b9c5dda`；`contracts/reconciliation.schema.json` sha256 `ad5a1166b19b…`；`docs/design/RECOVERY_PROTOCOL.md` sha256 `341c2bbebd0d…`；MP v1.1.1 sha256 `800565315537…`（4,734 行）；`docs/design/FILESYSTEM_PROTOCOL.md` sha256 `4de89886cf8d…`。

## 1. 分层与标签口径（沿用既有文档用词，不是新契约状态）

| 层 | 含义 | 出处 |
|---|---|---|
| L1 原文 | MP §16.3 决策表逐行（含「条件」列原文）与 §16.2 的 Decision 定义 | §16.2 L1712–1737；§16.3 L1739–1751；`reconciliation.schema.json` |
| L2 原文限定＋草案限制（逐条标明） | **MP 原文**限定不得降级：K0 适用范围（§16.3 L1749–1751）、冲突禁止（§15.4）、悬空引用规则（§18.4）；**REC 草案**限制（OPEN-02/OPEN-03）：OPEN-02 可达性禁令（L59）、OPEN-03 配对表未定（L109）等，草案条款未生效 | MP §15.4 L1620–1630、§16.3 L1749–1751、§18.4 L1946–1962；`docs/design/RECOVERY_PROTOCOL.md` §2–§4（L24–68）、L59、L109 |
| L3 未决 | 未闭合 OPEN 项 | OPEN-02（REC L108）、OPEN-03（L109）、OPEN-12（L117） |

- 登记标签仅用 `原文` / `原文＋草案限制` / `与原文冲突` / `UNRESOLVED`：前两者以 MP 原文为主、草案增补逐条标明出处，`与原文冲突`指该格与 §16.3 该行原文相悖（文本关系），`UNRESOLVED` 沿用 `docs/design/STEP_TRANSITION_SOURCE_PROPOSAL.md` §2 的既有用词、对应 REC 的「未定」「设计意图」（L49、L101）。**这些是文本分类，不是 `reconciliation.schema.json` 的字段值，也不是新契约状态。**
- **§16.3 未自称完备或非完备**：MP 全文无「穷尽 / 非穷尽 / 完备」字样（本代理实测 0 命中）。`RECOVERY_PROTOCOL.md:109` 以「§16.3 非穷尽」登记 OPEN-03，那是**草案判断**；本文件不据任何一侧断言该表完备或非完备。
- **Schema 结构接受 ≠ 策略许可**：`reconciliation.schema.json` 只约束字段与枚举、`additionalProperties:false`，**不约束配对**，4×4 全部组合结构上可解析；可解析不等于可判定、可执行（REC L100；`docs/design/TRACEABILITY_MATRIX.md:27` R10「可解析 ≠ 决策可判定」）。

## 2. 五条原文行（§16.3 L1739–1751，逐字）

```text
| EXPECTED_EFFECT_PRESENT | Intent、哈希、路径均有效 | VERIFY |
| TARGET_ABSENT | Prepared 完整、允许重试、未取消、未停止 | RETRY_SAFE |
| TARGET_ABSENT | Prepared 缺失或损坏 | BLOCK |
| CONFLICTING_EFFECT_PRESENT | 任意 | BLOCK |
| INSUFFICIENT_EVIDENCE | 任意 | BLOCK |
```

表尾另有一句范围限定（**MP 原文**）：`TARGET_ABSENT → RETRY_SAFE` 只适用于 K0 的原子不覆盖文件发布协议（L1749–1751）。

## 3. 16 格来源登记（4 Observation × 4 Decision）

列义：`出处`＝§16.3 行号或「无」；`登记`＝L1/L2 文本关系与**原文逐字**条件；`待确认`＝L3 未决项。

| # | Observation × Decision | 出处 | 登记（原文条件逐字 / 限定） | 待确认 |
|---|---|---|---|---|
| 1 | `EXPECTED_EFFECT_PRESENT` × `VERIFY` | §16.3 L1743 | L1 原文 条件：「Intent、哈希、路径均有效」 → `VERIFY` | 条件载体（判定算子）未登记 |
| 2 | `EXPECTED_EFFECT_PRESENT` × `RETRY_SAFE` | 无 | L3 `UNRESOLVED`——§16.3 未规定；**不据此判为禁止**，也不判为允许 | OPEN-03 |
| 3 | `EXPECTED_EFFECT_PRESENT` × `BLOCK` | 无 | L3 `UNRESOLVED`——同上 | OPEN-03 |
| 4 | `EXPECTED_EFFECT_PRESENT` × `FAIL` | §16.2 L1735–1737（仅定义） | L3 `UNRESOLVED`——`FAIL` 产生条件未定 | OPEN-12 |
| 5 | `TARGET_ABSENT` × `VERIFY` | 无 | L3 `UNRESOLVED`——§16.3 未规定；不得由「未列」推为禁止 | OPEN-03 |
| 6 | `TARGET_ABSENT` × `RETRY_SAFE` | §16.3 L1744 | L1 原文 条件：「Prepared 完整、允许重试、未取消、未停止」；L2 **原文**限定：**仅**适用 K0 原子不覆盖文件发布、MUST NOT 推广到邮件/支付/浏览器提交等（L1749–1751）；L2 **REC 草案**限制：OPEN-02 关闭前 MUST NOT 声称本格可达（§3 L59，严格前提 5 条见 L53–57）；`TARGET_ABSENT` 只表示此刻未观察到、**不证明历史上未执行**（§16.1 L1684–1688，原文） | OPEN-02/03 |
| 7 | `TARGET_ABSENT` × `BLOCK` | §16.3 L1745 | L1 原文 条件：「Prepared 缺失或损坏」 → `BLOCK` | 条件可判定性（同 OPEN-02） |
| 8 | `TARGET_ABSENT` × `FAIL` | §16.2 L1735–1737（仅定义） | L3 `UNRESOLVED`——同 #4 | OPEN-12 |
| 9 | `CONFLICTING_EFFECT_PRESENT` × `VERIFY` | §16.3 L1746（条件「任意」→`BLOCK`） | **与原文冲突**：该观察条件列为「任意」，原文 Decision 为 `BLOCK`，本格与之相悖；本文件只登记冲突，**不新增禁止条款**、不并及其他格 | OPEN-03 |
| 10 | `CONFLICTING_EFFECT_PRESENT` × `RETRY_SAFE` | §16.3 L1746（同上） | **与原文冲突**（同 #9 口径） | OPEN-03 |
| 11 | `CONFLICTING_EFFECT_PRESENT` × `BLOCK` | §16.3 L1746 | L1 原文 条件：「任意」 → `BLOCK`；L2 **原文**：MUST NOT 因冲突自动删除、改名或覆盖目标（§15.4 L1620–1630）；REC §4 L65 系**草案**复述 | — |
| 12 | `CONFLICTING_EFFECT_PRESENT` × `FAIL` | §16.2 L1735–1737（仅定义） | L3 `UNRESOLVED`——同 #4 | OPEN-12 |
| 13 | `INSUFFICIENT_EVIDENCE` × `VERIFY` | §16.3 L1747（条件「任意」→`BLOCK`） | **与原文冲突**（同 #9 口径） | OPEN-03 |
| 14 | `INSUFFICIENT_EVIDENCE` × `RETRY_SAFE` | §16.3 L1747（同上） | **与原文冲突**（同 #9 口径） | OPEN-03 |
| 15 | `INSUFFICIENT_EVIDENCE` × `BLOCK` | §16.3 L1747 | L1 原文 条件：「任意」 → `BLOCK`；L2 **原文**：悬空引用固定走 `NEEDS_RECONCILIATION → INSUFFICIENT_EVIDENCE → BLOCKED`（§18.4 L1946–1960）、不得静默重读原始输入造替代产物（L1962）；「MUST NOT 以『目标实际匹配』改写为 `VERIFY`」为 REC §4 L66 **草案**增补 | — |
| 16 | `INSUFFICIENT_EVIDENCE` × `FAIL` | §16.2 L1735–1737（仅定义） | L3 `UNRESOLVED`——同 #4 | OPEN-12 |

合计：L1 有原文条件 **5** 格（#1、#6、#7、#11、#15）；`与原文冲突` **4** 格（#9、#10、#13、#14）；`FAIL` 整列 **4** 格（#4、#8、#12、#16）仅有定义、无产生条件；其余 **3** 格（#2、#3、#5）为 `UNRESOLVED`。5＋4＋4＋3＝16。

## 4. 明确不做的推断（防泛化）

1. **不把未列配对泛化为禁止**：#2、#3、#5 与 `FAIL` 整列仅登记 `UNRESOLVED`；「§16.3 未列」在本文件中**既不推为允许、也不推为禁止**。
2. **`FAIL` 整列不加充分条件**：§16.3 原表无 `FAIL` 行（L1739–1747 逐行核对），本文件不为任何一格发明产生路径或禁止条件（OPEN-12；`contracts/README.md`：AI 不得自行发明契约字段与状态）。
3. **不把 `TARGET_ABSENT` 当「从未执行」**：见 #6；v1.1.1 已取消 `CONFIRMED_NOT_EXECUTED`（§16 L1651–1659）。
4. **不把可达性写成策略**：#6 在 OPEN-02 关闭前维持保守口径，本文件不声称其可达，也不把「安全重试」写成已生效规则。
5. **不把结构接受当许可**：16 格均可被 schema 解析，但**无任一格**因此获得策略许可（§1 末条）。
6. **不替 §16.3 声明完备性**：既不写「§16.3 完备，故其余组合非法」，也不写「§16.3 非穷尽，故其余组合合法」。

## 5. 效力边界与复核方式

- 不批准、不冻结、不实现、不测试；R10（`TRACEABILITY_MATRIX.md:27`）维持 `PARTIAL`、R11（`:28`）维持 `OPEN`；OPEN-02/03/12 与 FS-053 / O-5 全部维持原状。
- 本表**不是**正式来源表、**不是**机器契约、**不是**表驱动测试输入；升格须独立评审＋独立验收＋先生批准，且与 OPEN-03＝FS-053/O-5 **合并裁定、同一次关闭**。
- 复核（只读）：`sed -n '1739,1751p'` MP 比对 5 行原文；`grep -c '穷尽\|完备'` MP 应得 **0**；「与原文冲突」以 §16.3 条件列「任意」覆盖该观察全部条件为依据。
- 写入后复测项（供复核者独立重跑）：`git status --porcelain` 应仅本文件为 `??`，契约与两份协议 sha256 不变。**本文件的任何读数在引用前均须重新实测。**
- 未做：未递归委派、未改 `contracts/`、未改 `docs/design/RECOVERY_PROTOCOL.md`、未跑测试、未新增 `contracts/examples/` 或 `tests/` 内容。
