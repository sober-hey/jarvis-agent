# K0-002 候选 SQLite 契约夹具：证据边界设计

- 状态：**DRAFT / NOT_VERIFIED / NOT_APPROVED**。本文只记录证据边界，**不批准任何契约**、不关闭任何 DOC-R、不修改 `contracts/`、不实现测试代码。
- 文件身份：`docs/design/DATABASE_FIXTURE_DESIGN.md`（边界设计草案，作者无批准权，非契约、非冻结、非验收）。
- 快照：Git HEAD `f4565b4`；候选夹具 `tests/contract/test_database_contract_fixture.py` 为 **untracked**，sha256 `9c57aca1…48`（10972 B）。他人可能继续修改该文件，引用以其实际读取为准。
- 只读来源：Master Plan v1.1.1 §10.1 L834–1101、§10.2 L1103–1114、§10.3 L1116–1149、§14 L1455–1493、L277、L813、L226；`contracts/database_constraints.yaml`（L1–36）；`planning/tasks/K0-002.md`；`docs/design/TRACEABILITY_MATRIX.md` L19（DOC-R04）/L53（C10 候选证据规则）。

## 1. 判定分级

- **A 级（临时夹具可证）**：仅由 §10.1 已发布 DDL 的 SQL 语义决定，可在 `tmp_path` 一次性库上复现。
- **B 级（可观测、不可裁决）**：夹具能记录 SQLite 的实际行为，该行为是否合规须由契约批准判定。
- **C 级（临时夹具不可证）**：Guard、并发线性化、事务边界、产品库、跨资源语义。

## 2. 逐列判定（DDL 行列号 = Master Plan 行号；契约行 = database_constraints.yaml；列注：夹具使用的插入列是发布 DDL 列子集，未猜额外列）

| 契约条目 | DDL 列与行 | 判定 |
|---|---|---|
| `tasks.request_key UNIQUE`（L23） | `tasks.request_key` L860 | A：第二行同键被 UNIQUE 拒 |
| `task_plans UNIQUE(task_id, plan_version)`（L24） | `task_id` L888、`plan_version` L889、L898 | A |
| one ACTIVE plan per Task（L25） | `state` L890 + 部分唯一索引 L906–908 | A（`state='ACTIVE'` 谓词机制被拒/被留）；B（无已发布 Plan 状态枚举，"ACTIVE" 之外拼写不受约束） |
| Step Plan 必须同 Task（L26） | `task_steps.task_id` L917、`plan_id` L918、复合 FK L928–929（父键 `task_plans(id, task_id)` L899） | A：跨 Task 挂 Plan 被拒 |
| `operation_intents.operation_key UNIQUE`（L27） | `operation_key` L947 | A |
| K0 one Intent per Step（L28） | `step_id TEXT NOT NULL UNIQUE` L946 | A（第二行被拒）；B（Step 无法保留历史 Intent 行属契约问题，未裁决） |
| one ACTIVE Attempt per Step（L29） | `state` L1006 + 部分唯一索引 L1024–1026 | A（机制）；B（同上，状态拼写） |
| Attempt Intent 必须同 Step（L30） | `step_attempts.operation_id` L1001、复合 FK L1014–1015（父键 `operation_intents(id, step_id)` L961） | A |
| active_plan_id 必须同 Task（L31） | `tasks.active_plan_id` L866、FK L877–878（父键 L899） | A（非 NULL 时跨 Task 被拒）；B（NULL 时 SQLite MATCH SIMPLE 跳过检查） |
| （yaml 未列，DDL 有）事件 Step 必须同 Task | `task_events.task_id` L1037、`step_id` L1038、复合 FK L1053–1054 | A（`step_id` 非 NULL）；B（`step_id` 可空即旁路） |
| 存在性 FK | `task_plans.task_id` L888→L895–896；`operation_intents.step_id` L946→L958–959；`file_write_intents.operation_id` L975→L985–986；`task_events.task_id` L1037→L1050–1051 | A |
| 其它 UNIQUE | `UNIQUE(plan_id, ordinal)` L920/L931；`UNIQUE(id, task_id)` L932；`UNIQUE(step_id, attempt_no)` L1003/L1017；`UNIQUE(id, step_id)` L961 | A |
| CHECK | `expected_size` L983、`CHECK(expected_size >= 0)` L988 | A |
| 连接级前置 | `PRAGMA foreign_keys = ON`（MP L813；yaml L6）；`busy_timeout` L814 | A：夹具必须逐连接断言；SQLite 默认 OFF 会静默旁路第 4/9/10/11 行全部 FK |
| 对象集 | §10.1 仅 7 表有 DDL；`system_control` L1062–1074、`artifacts` L1076–1090 只有字段清单、无类型 | A：夹具只能证明这 7 张表与 2 个部分索引 L906/L1024 的存在与 SQL 文本 |

## 3. C 级：临时夹具不能证明的

- **应用 Guard**：`planning/tasks/K0-002.md` L27 已记 Guard 为占位符；READY→RUNNING 执行权、取消、VERIFYING→SUCCEEDED 前提均非 DDL 语义。
- **事务边界**：“state transition and event in one transaction”（yaml L32）可在临时库演示 `BEGIN/COMMIT`，但无法证明产品代码把两者放进同一事务，也不覆盖崩溃点。
- **并发与线性化**：§10.3 L1116–1149 的 `WHERE state=:expected_state AND version=:expected_version` CAS 模式（CAS 示意 L1122–1130）可演示；跨连接竞态、WAL 可见性、`busy_timeout` L814 超时行为、取消提交与新发布启动的检查后竞态（K0-002 L33、DOC-R13/INV-010）不能由单进程一次性库证明。
- **产品库**：迁移与 Schema 版本（MP L850、§10.2 L1103–1114 要求补 CHECK/索引/时间字段）、真实数据、一致性备份（MP L832/L2053）均不在临时夹具范围。
- **跨资源**：MP L277 明确不承诺 SQLite 与文件系统原子事务；§14 L1455–1493 的 1–17 步提交顺序无法由 DB 夹具证明。
- **单 Worker / Epoch**：ADR-0001 与 MP L1818/L1824 的 epoch 递增、拒提交依赖并发 Worker，夹具不含。
- **其它**：索引存在 ≠ 查询计划或性能；FTS5（MP L226）不在 K0 夹具范围。
- **证据等级**：DOC-R04 仍 OPEN（TRACEABILITY_MATRIX L19）；按 L53，夹具仅为候选证据，须由 K0-004 复验，不得用于关闭任务自身验收。

## 4. 观测记录（历史快照与当前状态分离；本文未改动任何代码或契约）

- **历史观测（2026-10-12T20:20，夹具 sha256 `717d99d5…ba`，此后已被作者修订）**：`uv run pytest tests/contract/test_database_contract_fixture.py -q` → **9 passed / 1 failed**。失败点 `test_section_10_1_publishes_ddl_for_seven_of_the_nine_named_tables`：§10.1 代码块顺序为 tasks L857→task_plans L885→task_steps L914→operation_intents L943→file_write_intents L974→step_attempts L997→task_events L1034，与命名清单 L838–848 的顺序在前者第 4 位起不同。属夹具自身的顺序过断言，**不是 DDL 缺陷**。
- **当前状态（2026-10-12T21:03 复读）**：作者已修正该顺序断言，夹具 sha256 `9c57aca1…48`（10972 B），实测 `10 passed`；独立评审与工程验收两份报告已落盘（`docs/reviews/K0-002-database-fixture-review.md`、`docs/reviews/K0-002-database-fixture-acceptance.md`），结论为**局部 PASS**，但 DOC-R04/R05 仍为 `OPEN`（`docs/design/TRACEABILITY_MATRIX.md` L20/L21）；两份报告的结论以报告本身为准，本文不裁定。
- 集合级事实仍成立：DDL 建出的表恰为 7 张、索引恰为 L906/L1024 两个。
- 后续提交核对：最终夹具 `dbe6161e…68fd` 已随 `786f84f` 提交，独立评审附录 A 与验收末段追加确认绑定该哈希；矩阵 R04/R05 登记 `contract_fixture / PARTIAL`，不关闭 DOC-R。上列带时刻的读数均为历史快照，不是当前 Git 状态。

## 5. 效力声明

- 不实现测试、不改 DDL、不改 `contracts/`、不批准、不冻结、不关闭 DOC-R；本文任何行号引用以实际读取的 Master Plan 文本为准。
- 哪些变更需批准，由既有治理文件与先生判定，本文不裁定；本文件不构成任何批准。
