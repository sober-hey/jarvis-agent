# K0 路径契约提案（PROPOSAL，四类路径来源登记，非契约）

- 文件身份：`docs/design/PATH_CONTRACT_PROPOSAL.md`——本轮**唯一新增文件**；**未递归委派、未递归扫描目录**；作者不参与实现，无任何批准权。
- 状态：**DRAFT / PROPOSED / NOT_APPROVED / NOT_VERIFIED / NOT_FROZEN**。**不关闭 FS-026、O-10、D-10、D-12**，不关闭 DOC-R07、FS-016/O-1/D-09/D-14。
- 效力：**不改** `contracts/` 任何 schema、**不改**代码与测试、**不改** `PROJECT_STATE.md` / `planning/BACKLOG.md` / `docs/design/TRACEABILITY_MATRIX.md` 台账、**不改**任何 ADR 状态（ADR-0002/0003 保持 PROPOSED、ADR-0004 保持 PENDING_VALIDATION）；**不新增字段、不新建 schema、不实现、不测试**（含不发明 `limits_digest` 之类字段）。
- 来源集（本轮只读）：MP v1.1.1 §11.1 L1164–1182、§11.2 L1184–1217（13 条确定性规则）、§12.3 L1308–1334、§15.2 L1557–1590、§19.2 L1999–2010；`contracts/task_spec.schema.json`、`contracts/snapshot_manifest.schema.json`、`contracts/file_write_intent.schema.json`；`docs/design/FILESYSTEM_PROTOCOL.md` FS-020..FS-026；`docs/design/DOMAIN_CONTRACT_DESIGN.md` D-10/D-12；`docs/design/SNAPSHOT_OUTPUT_MAPPING_PROPOSAL.md`。MP 是**工程依据**；仓库侧 schema / 协议 / DOMAIN 及 ADR **均为 DRAFT 或未批准**，不因被本文引用而升格。
- 快照（写入前**一次性**只读实测，2026-10-13T03:05+08:00；不重复采样、采样值不再后改）：HEAD `229890c342ae75ed…`；MP `800565315537d0c7…`（4734 行）；`task_spec.schema.json` `7e1e4b78f1a14e75…`（54 行）；`snapshot_manifest.schema.json` `edb3f081b6e647f5…`（61 行）；`file_write_intent.schema.json` `c710df0ec2557810…`（44 行）；`FILESYSTEM_PROTOCOL.md` `4de89886cf8d638c…`（117 行）；`DOMAIN_CONTRACT_DESIGN.md` `4d6258ea32766683…`（175 行）；`SNAPSHOT_OUTPUT_MAPPING_PROPOSAL.md` `40a16a5c…`（89 行）。

## 1. 四类路径必须分开说（逐项归类）

| # | 出现处 | 基准 | shape 现状（§2 分栏实测） | 原文口径 | 归类 |
|---|---|---|---|---|---|
| P1 | `task_spec.schema.json` → `input.root` | **绝对授权根** | 仅 `pattern "^/"`（**前缀匹配、无 minLength**） | §19.2 L2003「只读取明确授权的输入根目录」；§12.1 L1271 授权 | **绝对根**（不得泛拒首斜杠） |
| P2 | `snapshot_manifest.schema.json` → `files[].path` | **相对快照根（按§11.2候选继承；D-09未裁定）** | 仅 `minLength 1`（无形态约束） | §11.2 规则 4 L1208「路径相对于快照根目录」、规则 5 L1209 分隔符 `/`；§12.5 Manifest 是否继承该规则归 D-09 | **相对候选**（继承待裁定，约束未表达） |
| P3 | `file_write_intent.schema.json` → `prepared_relpath` / `target_relpath` | **基准根未定（D-10）** | 与 P2 同形（仅 `minLength 1`） | §15.2 L1579 只给 K0 目标 `state/tasks/<task_id>/output/inventory.json`；DOMAIN D-10「相对 `state/` 还是 `state/tasks/<task_id>/`」待定 | **基准未定** |
| P4 | `task_spec.schema.json` → `output.name` | **固定字面量** | `const "inventory.json"`（shape 层唯一已固化者，**无 minLength**） | §15.2 L1586「目标名称固定或严格验证」 | **固定 inventory** |

- 四类不得互相套用：P1 的「必须绝对」**不是** P2/P3 的规则；P4 名称已固定**不推出** P3 基准已定；P2 的相对语义只是§11.2候选继承，**不推出** Manifest 已完成D-09裁定，也不推出其约束已表达。

## 2. shape 实测（分类分栏）vs MP 原文 / 仓库草案 / 候选：四栏分开

- **shape — P2 / P3**（仅 `minLength 1`）：`a.txt`、`../x`、`a//b`、`/abs`、含 NUL、含反斜杠、孤立代理项**全部接受**；**仅空串被拒**（由 `minLength 1`）。
- **shape — P1**（仅 `^/`）：`/`、`/abs`、`/../x`、`/a//b`、`/a\u0000b`、`/lone-surrogate` 接受（以 `/` 开头即通过）；**不以 `/` 开头者一律拒绝**；空串 root 亦**由 `^/` 拒绝**（P1 无 `minLength`，不得写成 minLength 拒）。
- **shape — P4**（仅 `const "inventory.json"`）：仅该字面量通过，其余一律拒绝；**空串由 `const` 拒，不由 minLength 拒**。
- shape 只表达**前缀/字面量通过**：`^/` **不等于**「是合法绝对路径」，也**不证明**真实路径、目录存在或已授权；JSON Schema 无文件系统语义。
- **MP 原文（工程依据）**：规则 4/5/6/11/13（相对快照根、`/` 分隔符、UTF-8 字节序、不允许重复路径、不支持文件名必须明确报错不得静默改名）；§15.2 L1586–1587「目标名称固定或严格验证」「不接受任意相对路径穿越」；§19.2 八条禁止。
- **仓库草案（DRAFT，非 MP 明文）**：FS-020（不得只依赖 `Path.resolve()`）、FS-021（dirfd **优先**，明示未升格 MUST）、FS-022（拒绝符号链接与特殊文件）、FS-023（**「路径穿越与空组件 MUST 被拒绝」——「空组件」系该草案表述，MP 无此字样**）、FS-024、FS-025、FS-026（机器契约缺口，OPEN）；DOMAIN D-10 / D-12。
- **候选（原文未出现者）**：判定矩阵、边界取值、Unicode 形式、`\` 与 drive-like 地位，一律只作候选。
- 四栏不得互为证据：结构接受只说明「schema 未表达该约束」，**既不构成许可、也不构成已被拒绝**。

## 3. 数值与规则口径：原文已有 vs 未定

| 项 | 原文 | 未定部分（不得断言） |
|---|---|---|
| 深度 16 | §11.1 L1173「最大目录深度 16」 | **计数口径未成文**；**候选口径（未裁定）**：根计 0、leaf（文件）不计，只计根与叶之间的目录组件数 |
| 20 MiB | §11.1 L1171–1172（总大小 / 单文件）＋L1180「初期工程默认值，**可通过受控配置调整**，但必须有对应测试」 | **可受控调整**；schema `total_bytes ≤ 20971520` 与之同值（FS-015），二者是**兼容缺口**（上限写死 ≠ 禁止受控调整），**不构成必然规范冲突** |
| 字节排序 | §11.2 规则 6 L1210「按相对路径的 UTF-8 字节序排序」 | 该规则原文位于 **§11.2 inventory 输出**语境；**Manifest 是否沿用 §11.2 同一套规则未定**（FS-016 / O-1 / D-09；`SNAPSHOT_OUTPUT_MAPPING_PROPOSAL.md` §5 第 4 条①）→ 不得写成「排序已作用于 P2 Manifest」 |
| JSON 编码 | §11.2 规则 12 L1216「JSON 编码规则必须固定」 | **具体序列化参数未给**（键序、转义、末尾字节等）；归 O-2 侧 |

## 4. 字符与组件：MP 原文 / 草案扩展 / 候选（不强制定）＋实际样例表

- **MP 原文已定**：相对快照根（4）、`/` 分隔符（5）、UTF-8 字节序排序（6，限 §11.2 输出语境）、不允许重复路径（11）、不支持文件名必须报错（13）、「不接受任意相对路径穿越」（§15.2 L1587，覆盖 `..`）。
- **草案扩展（非 MP 明文）**：FS-023 将「**空组件**」一并写成 MUST 拒绝；MP 全文无「空组件」字样，引用须分栏。
- **候选（无原文依据，归 O-2 / FS-026）**：`.`、NUL、反斜杠、drive-like 的接受/拒绝判定。**不得把反斜杠全局改写为分隔符**（规则 5 只固定 `/`，未授权 `\`→`/`）；`\0` 在 Python/POSIX 通常被拒，但**拒绝 ≠ 截断**，亦非原文裁决。
- **候选（不强制定，不加正式规则）**：严格 UTF-8、拒绝孤立代理项、NFC 规范化。规则 13 无判定算法，MP 未指定任何规范化形式（同 `SNAPSHOT_OUTPUT_MAPPING_PROPOSAL.md` §5 第 4 条）——强制任一形式都会新增原文没有的语义。
- **实际样例行为表（正反例，全部为候选用例位：未实测、未实现、未裁定、不写 fixture）**：

| # | 样例 | 候选预期 | scope / 依据层 |
|---|---|---|---|
| 1 | `a.txt`、`b/c.txt` | 接受 | P2 有原文（规则 4/5）；**P3 仅为候选**（基准 D-10 未定） |
| 2 | `../x`、`a/../../b` | 拒绝（候选） | P2、P3 共用候选；§15.2 L1587 原文针对**目标发布**，是否覆盖 P2/P3 未裁定 |
| 3 | `a//b`、`a/`（空组件） | 拒绝 | **FS-023 草案**（MP 无「空组件」字样） |
| 3b | `.`（单点组件） | 候选拒绝（未裁定） | 候选：MP 无明文，FS-023 只写「穿越与空组件」、未点名 `.` |
| 4 | 空串 | 拒绝 | **P2/P3 由 `minLength 1` 拒；P1 由 `^/` 拒；P4 由 `const "inventory.json"` 拒**（P4 无 minLength，不得写成 minLength 拒） |
| 5 | P1 `input.root = "/abs"` | **shape 接受，授权待核** | shape 层 `^/` 通过；授权依 §19.2/§12.1，属 D-12 |
| 6 | P1 `input.root = "/"` | shape 接受，**不代表已授权读 `/`** | 同 #5；**不得因安全泛拒授权根首斜杠** |
| 7a | P2 `files[].path = "/abs"` | **拒绝**（非 shape 层：shape 接受 ≠ 许可） | 依据**相对 snapshot 语义**（规则 4）＋FS-026 缺口，属候选验证规则；**Manifest 是否继承 §11.2 规则须 D-09 裁定** |
| 7b | P3 `prepared_relpath` / `target_relpath` = 绝对形态 | **候选拒绝**（非 shape 层：shape 接受 ≠ 许可） | 字段名 `relpath` 语义为候选依据；**D-10 基准根未定** → 不据此定论 |
| 8 | 含 NUL `a\u0000b` | 候选拒绝 | 候选，无原文 |
| 9 | 含反斜杠 `a\b` | 候选拒绝（**不判为分隔符**） | 候选，无原文 |
| 10 | drive-like `C:`、`\\host\share` | 候选拒绝 | 候选，无原文 |
| 11 | NFC `é`(U+00E9) 与 NFD `e`+U+0301 | **未裁定**（两者皆不得写成已允许或已拒绝） | 候选；MP 未指定规范化形式 |
| 12 | `d1/…/d16/f` → 候选深度 **16**（接受）／`d1/…/d17/f` → 候选 **17**（拒绝） | 16 接受 / 17 拒绝 | §11.1 L1173 数值；**候选计数口径**：根计 0、只计目录组件、**不含 leaf `f`**、未裁定 |
| 13 | `size_bytes` = 20971520 / 20971521 | 候选接受 / 候选拒绝 | §11.1 L1172 默认值，**可受控调整** |
| 14 | `total_bytes` = 20971520 / 20971521 | 候选接受 / 候选拒绝（默认配置同界） | §11.1 L1171 ＋ schema 上限 |
| 15 | `output.name` = `inventory.json` / 其他 | 接受 / **拒绝**（P4 已 `const` 固化） | §15.2 L1586 ＋ schema `const` |

## 5. 字符串验证不保证什么（边界声明）

- `^/`、`minLength 1` 这类字符串/正则校验**不保证**：① 授权（哪个根被谁授权 —— §12.1 L1271、§19.2 L2003，与 D-12 落库议题相邻）；② TOCTOU（检查与打开间的路径替换 —— FS-020）；③ `O_NOFOLLOW` / `O_DIRECTORY` 语义（FS-022 / FS-025）；④ 特殊文件拒绝（FS-022）；⑤ 目录存在性与绝对路径真实性。
- `^/` 只是**前缀条件**，不得当作「绝对路径已核实」；同理 `input.root = "/"` 通过 shape 不等于已授权。
- **dirfd 优先不得升格为 MUST**：§12.3 L1320 原文「K0 **应优先使用**」，能力检测要求见 L1329，FS-021 明示「不擅自升格」，O-10 未裁定 → 本文既不写成 MUST，也不写成可不用。

## 6. 未决依赖与不关闭声明

- **FS-026**（= O-2 侧）：绝对/相对口径、UTF-8 名称约束、最大目录深度、单文件大小上限的**机器契约仍未表达** → **保留 OPEN**。
- **O-10**：FS-021 是否升格为强制要求 → **保留 OPEN**（契约评审＋先生）。**D-10**：P3 基准根 → **保留 OPEN**。**D-12**：`input.root` / `output.name` 是否落库、恢复如何还原授权根 → **保留 OPEN**。
- **Manifest 是否继承 §11.2 inventory 规则**（含排序与 13 条）→ **未定**，属 FS-016 / O-1 / D-09，本文不裁定。
- 不关闭 DOC-R07（`TRACEABILITY_MATRIX.md` 记 PARTIAL）、FS-016 / O-1 / D-09 / D-14；本文不改其任何格。
- 本文不含代码、测试与 fixture，不声称任何条目「已允许」「已禁止」「可执行」，不预判任何裁定结果。

## 7. STOP-WRITE 与 STOP 证据

- 本文为**最终版本**：**STOP-WRITE**，此后不再修改（含措辞、格式、行尾、空格）；首部快照为一次性采样，不重复采样。
- **STOP 证据（`sha256 / bytes / lines`，一次性实测）**由主代理与独立验收方绑定复核；本文**不自引自身哈希以避自指**，故不在此处列出该三元组。
- 作者**不自评 PASS**：本文为 DRAFT / PROPOSAL，评审、验收、冻结与批准由独立方与先生决定；文件再改即失效，须重新实测。
