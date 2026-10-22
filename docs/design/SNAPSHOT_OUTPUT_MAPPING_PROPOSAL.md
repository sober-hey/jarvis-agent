# K0-002 快照输出映射提案（PROPOSAL，来源登记，非契约）

- 文件身份：`docs/design/SNAPSHOT_OUTPUT_MAPPING_PROPOSAL.md`——本轮**唯一新增文件**；**未递归委派**；作者不参与实现，无任何批准权。
- 状态：**DRAFT / PROPOSED / NOT_APPROVED / NOT_VERIFIED / NOT_FROZEN**。**不关闭 FS-016（= O-1 = D-09）、不关闭 DOC-R07、不关闭 D-14**。
- 效力：不改 `contracts/`（**不改 `snapshot_manifest.schema.json` / `file_write_intent.schema.json`**）、不改协议文档 / ADR / 任务卡 / **不改 `docs/design/TRACEABILITY_MATRIX.md` 台账** / 不改代码与测试；**不新增正式字段、不新建 schema、不实现、不测试**。
- 来源集（本轮只读）：**MP v1.1.1 是工程依据**（§11.2 L1184–1217、§12.5 L1362–1393），不因本文引用而降格；仓库侧来源 `contracts/snapshot_manifest.schema.json`、`contracts/file_write_intent.schema.json`、`docs/design/FILESYSTEM_PROTOCOL.md` FS-015 / FS-016、`docs/design/DOMAIN_CONTRACT_DESIGN.md` D-14 行**均为 DRAFT / 非正式契约**。
- 快照（写入前**一次性**只读实测，2026-10-13T01:45+08:00；不重复采样、采样值不再后改；引用前须重新实测）：HEAD `f43f1b443e2a6612d1264d50e7fe986423970178`；MP `800565315537d0c7…`（4734 行 / 107285 B）；`snapshot_manifest.schema.json` `edb3f081b6e647f5…`（61 行）；`file_write_intent.schema.json` `c710df0ec2557810…`（44 行）；`FILESYSTEM_PROTOCOL.md` `4de89886cf8d638c…`（117 行）；`DOMAIN_CONTRACT_DESIGN.md` `4d6258ea32766683…`（175 行）。

## 1. 三个对象必须分开说

| 对象 | 机器契约现状 | 依据 |
|---|---|---|
| 输入快照 Manifest（快照记录） | **有**：`snapshot_manifest.schema.json`（DRAFT） | FS-015；MP §12.5 |
| 最终 inventory 输出（发布产物内容） | **无**：仓库内不存在 inventory 输出 schema；本文**不称其已有 schema** | FS-016；MP §11.2 仅有示例 |
| Prepared 文件 / FINAL_OUTPUT 的**路径与哈希** | **有**：`file_write_intent.schema.json`（DRAFT）载 Intent 视角的路径与哈希 | FS-016；D-14 |

FS-016（DRAFT）原文口径：快照记录与最终 inventory 输出是**两个不同对象**，命名与映射关系**尚未定稿**（OPEN，O-1）。本节只登记该区分，不裁定映射。

## 2. 字段对照（逐字段，只登记来源已有文字）

| 字段 | MP §12.5 Manifest 示例（L1366–1379） | `snapshot_manifest.schema.json` | MP §11.2 输出示例（L1186–1199） |
|---|---|---|---|
| `schema_version` | `1` | `const 1`，required | `1` |
| `snapshot_id` | `"<snapshot_id>"` | `string` minLength 1，required | **示例中不出现** |
| `files[]` | 3 字段对象数组 | `array`，maxItems 1000；items `additionalProperties:false`，required `path`/`size_bytes`/`sha256` | 3 字段对象数组 |
| `files[].path` | `"a.txt"` | `string` minLength 1（**无排序 / 编码 / 规范化约束**） | `"a.txt"` |
| `files[].size_bytes` | `12` | `integer` ≥ 0 | `12` |
| `files[].sha256` | `"<actual_sha256>"` | `string`，`^[a-f0-9]{64}$` | `"<actual_sha256>"` |
| `file_count` | `1` | `integer` 0–1000 | `1` |
| `total_bytes` | `12` | `integer` 0–20971520 | `12` |

- 两侧示例**逐字段同形，唯一可见差异是 §12.5 多 `snapshot_id`**；`<actual_sha256>` 的"说明性占位符"声明**只在 MP §11.2 L1201 给出**，§12.5 未作同类声明——两侧同形的占位写法是示例层面的观察。**该同形不是映射已定稿的证据**（FS-016）。
- 该差异**不构成规范冲突**：`snapshot_id` 与 inventory 属**两个对象**，`snapshot_id` 是否进入 inventory、其取值是否含时间或随机量均未定；**不得据 §11.2 规则 9（输出不包含当前时间和随机数）断言二者冲突**（未证）。
- §11.2 示例**仅示例**：MP 未声明它是 inventory 输出的机器契约，仓库内亦无对应 schema；本文不据其推断字段合法性。

## 3. 路径与哈希（`file_write_intent.schema.json`，DRAFT）

| 字段 | 类型 / 约束 | 本文登记的口径（**不新增语义**） |
|---|---|---|
| `operation_id` | `string` minLength 1 | 指向 OperationIntent |
| `input_snapshot_sha256` | `string`，`^[a-f0-9]{64}$` | **输入快照 Manifest 的稳定哈希**（摘要 A；字段名 ↔ Manifest 的绑定是 DOMAIN 草案 L77 口径，非 MP 原文） |
| `prepared_relpath` | `string` minLength 1 | Prepared 文件位置；**基准根未在 schema 中表达** |
| `target_relpath` | `string` minLength 1 | 最终输出位置；**基准根未在 schema 中表达** |
| `expected_sha256` | `string`，`^[a-f0-9]{64}$` | **Prepared 产物内容的哈希**（摘要 C）；"= 预期最终输出字节"是**发布语义推论**，schema 未声明该等同 |
| `expected_size` | `integer` ≥ 0 | 预期字节数 |

D-14（DOMAIN DRAFT，**不关闭**）：`artifacts` 行与 `prepared_relpath` / `target_relpath` 的对应、写入时机（落在 §14 步骤序列哪一步）与 `retention_class` 取值均未定；本文不裁定。

## 4. 三种 SHA 不得互替

| 摘要 | 覆盖什么 | 出处 |
|---|---|---|
| **A** `input_snapshot_sha256` | 输入快照 **Manifest 的规范化字节序列**（"清单的哈希"） | MP §12.5 L1382 原文「Manifest 的规范化字节序列必须具有稳定哈希」；字段名绑定见 DOMAIN 草案 L77 / FS-015（DRAFT） |
| **B** `files[].sha256` | Manifest 中**每个快照文件的内容**（"被列举文件的哈希"） | MP §11.2 规则 8（L1212）；schema items |
| **C** `expected_sha256` | **Prepared 文件内容**（"输出文件的哈希"） | `file_write_intent.schema.json`；"= 预期最终输出字节"为**发布语义推论**（同 §3），MP §13.2 只描述产物生命周期 |

**禁止混淆**：A / B / C 互不替代——不得把 A 当作文件内容哈希或 Prepared 哈希，也不得把 C 当作 Manifest 哈希。两个 schema 只约束字段形状，**不表达 A、B、C 之间的任何推导或相等关系**。
「A 是 Manifest 稳定哈希」这一**字段命名绑定**属草案口径（DOMAIN L77、FS-015），MP 原文只规定 Manifest 规范化字节序列 MUST 具有稳定哈希；引用时须分栏区分原文与草案。

## 5. 当前 schema 不约束什么（缺口登记，本文件不修）

1. `file_count` 与 `len(files)`：schema 只给各自独立区间（0–1000 与 maxItems 1000），**无相等约束**；主代理实测：`file_count` 与数组长度不符的样例**可通过 schema 校验**（结构接受）。
2. `total_bytes` 与 `sum(files[].size_bytes)`：schema 只给 0–20971520，**无相等约束**；主代理实测：总和不符的样例同样**结构可接受**。
3. §11.2「确定性规则」逐条 L1205–1217（实测 **13 条编号项**；第 12 条即「JSON 编码规则必须固定」）中，**已有原文**的有：只含普通文件、默认含隐藏普通文件、默认拒绝符号链接、路径相对于快照根、分隔符统一 `/`、按相对路径的 UTF-8 字节序排序（规则 6，L1210）、实际字节数、文件内容 SHA-256、不含当前时间与随机数、同快照必须生成完全相同字节、不允许重复路径。
4. **本节另登记三项未决**（不穷尽全部缺口）：① Manifest 是否沿用 §11.2 同一套规则（§12.5 未写，排序规则本身已由 §11.2 定）；② 规则 12 要求"编码规则必须固定"但**未给具体序列化参数**（分隔符、键序、转义、末尾字节等）；③ 规则 13「不支持的文件名必须明确报错，不得静默改名」（L1217）**无判定算法**，Unicode 规范化形式亦未表达。
5. `path` 的**重复性**与**顺序**：§11.2 规则 6/11 有原文（排序、不允许重复路径），但 schema **无 `uniqueItems`、不约束数组顺序**，`path` 仅 `minLength 1`；主代理实测重复路径样例**结构可接受**（FS-026 侧另有深度与单文件上限缺口，本文不展开）。
6. **结构接受 ≠ 许可**：上述可校验通过只说明 schema 未表达该约束，**不构成对 count/sum 不一致或重复路径的允许**（§11.2 规则 11 已明确不允许重复路径）。本文不把任一项写成已定，也不新增字段或约束。

## 6. 最小候选映射（**候选，未裁定**；不发明字段）

- 语义链（依据 A / B / C 与 FS-015）：输入快照文件内容 →（B）→ 快照 Manifest（B 的逐文件列表 + 稳定哈希 A）→ 生成 inventory 内容 → Prepared 文件（内容哈希 C，经 `prepared_relpath`）→ 发布到 `target_relpath`。
- **候选 1**：inventory 输出沿用 §11.2 的四字段形态（`schema_version` / `files` / `file_count` / `total_bytes`），Manifest 在其上多 `snapshot_id` ⇒ 视为「inventory = Manifest 去掉 `snapshot_id` 的投影」。
- **候选 2**：Manifest 与 inventory 各自独立定义、不互为投影；`snapshot_id` 是否进入 inventory 另由契约评审决定。
- 两候选**都不是结论**：`snapshot_id` 是否进入 inventory、两份上限的关系、谁引用谁，均归 **FS-016 / O-1 / D-09** 裁定。
- 本文**不新增**任何正式字段、不提议修改 schema、不实现、不产出 fixture。

## 7. 未决依赖与不关闭声明

- **FS-016（= O-1 = D-09）**：快照记录与最终 inventory 的命名、字段映射与上限关系未定稿 → **本文件不关闭**。
- **DOC-R07**「输入快照保证范围明确」：`docs/design/TRACEABILITY_MATRIX.md` 记为 PARTIAL，其证据列为 **FS-010..FS-016 / FS-020..FS-026**、`docs/adr/ADR-0003-snapshot-consistency.md`（PROPOSED）→ **本文件不关闭，且不改该台账任何格**。
- **D-14**：`artifacts` 行 ↔ `prepared_relpath` / `target_relpath` 对应、写入时机、`retention_class` 取值 → **本文件不关闭**。
- 机器契约缺口（§5 第 1–5 项）：`file_count` / `total_bytes` 无相等约束、排序与重复性未入 schema、JSON 编码参数与文件名规范化算法未定 → **保留 OPEN**。
- 本文件不含代码、不含测试、不含 fixture，不声称任何条目「已允许」「已禁止」「可执行」。

## 8. 返修记录与 STOP-WRITE

- **两次显式返修（非静默改动，应主代理与评审要求）**：第一轮 ① 来源集表述改为「MP 为工程依据、仓库 schema/协议仍 DRAFT」，② §5 第 4 项限定为「本节另登记三项未决」；第二轮 ③「说明性占位符」声明只归 MP §11.2 L1201（不得称两侧均标），④「Prepared = 预期最终输出字节」标为**发布语义推论**（§3 / §4），⑤ DOC-R07 证据范围改为 FS-010..FS-016 / FS-020..FS-026。返修后**重新 final**，以本文 `sha256 / bytes / lines` 为准。
- 本文第二次返修后即**最终版本**：**STOP-WRITE**，此后不再修改（含措辞、格式、行尾、空格）；首部快照为**一次性采样**，不重复采样、采样值不再后改。
- 独立评审 / 验收须绑定第二次返修后最终的 `sha256 / bytes / lines`（由主代理转达，本文不自引自身哈希以避自指）；文件再改即失效，须重新实测。
