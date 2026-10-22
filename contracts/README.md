# JARVIS K0 Contract Pack

- Version: 1.0-draft
- Status: DRAFT
- Frozen: NO
- Implementation Verified: NO

## 范围

- TaskSpec
- Task 状态机
- Step 状态机
- Operation Intent
- File Write Intent
- Reconciliation
- Snapshot Manifest
- Tool Manifest
- 数据库约束
- 核心执行不变量

## 强制原则

1. K0 只允许 inventory.generate。
2. K0 同时最多一个活动执行器。
3. Prepared Artifact 必须先持久化。
4. Intent 必须先于目标副作用提交。
5. Operation Key 必须稳定。
6. 未知结果必须先 Reconciliation。
7. 不得覆盖冲突目标。
8. 任务成功必须独立验证。
9. 取消请求与任务状态正交。
10. 状态与事件同事务提交。

## 冻结条件

- Schema 验证通过。
- YAML 解析通过。
- 状态转换 Guard 完整。
- 数据库约束一致。
- 正反例测试通过。
- 相关 ADR 已批准。
- 无阻断性规范冲突。

当前仅为初始草案。
不得直接用于宣称 K0 契约已经验收。

## 2026-10-18 契约精化（仍 DRAFT / NOT_APPROVED）

- `task_transitions.yaml`（31 边）与 `step_transitions.yaml`（37 边）保留原边集，
  用 `transition_guards.yaml` 的显式谓词替代三个通用占位符。每条边的 Guard 全部合取；
  缺证据、未知谓词或失败均保留源态并回滚，不隐式选择另一条边。
- 新谓词语言是有界数据树，定义强类型事实、CAS/事件同事务、当前全局 epoch fencing、
  取消来源、Intent/Attempt 绑定、核对及重试必要条件。**事实适配器和执行器未实现**；
  `runtime_enabled: false`，不得从离线谓词 PASS 推导执行许可。
- `draft/plan.schema.json`、`draft/step.schema.json`、`draft/attempt.schema.json` 为
  字段结构投影；`draft/execution_graph.schema.json` 为闭合关联图夹具，不是数据库迁移。
  放在 `draft/` 是显式隔离未批准的领域 Schema，同时保持既有根目录六份 Schema 测试清单不变，
  不隐藏为已纳入旧验证：新 Schema 必须单独运行下述检查。
- `domain_relationships.yaml` 单独声明跨记录唯一性、组合 FK、存在性与值关系。
  JSON Schema 本身**不能**证明跨行 FK/唯一性，也不能证明时间上的 Intent 不变性。
- `examples/valid_execution_graph.json` 是结构/关系正例，不是完整执行轨迹；
  `INTERRUPTED_CANDIDATE` 只是演示 D-02 仍缺枚举的非空字符串，**不是新批准状态**。
  两个 Attempt 引用同一 Intent 行，Operation Key 仍全局唯一。
- Plan/Attempt 完整状态枚举、摘要基准、错误/恢复载体与取消—发布线性化仍未定，
  不通过编造字段或状态消除这些缺口。11 条 Step 来源仍 `UNRESOLVED`。

离线候选检查（不是安全批准/冻结门禁）：

```sh
PYTHONDONTWRITEBYTECODE=1 /Volumes/扩展硬盘/jarvis/.venv/bin/python -m pytest docs/verification/test_k0002_contract_refinement.py -q --tb=line -p no:cacheprovider
```

工作树回归必须使用 `PYTHONPATH=src:packaging`：单独 `PYTHONPATH=packaging` 会令
`packaging/jarvis.py` 遮蔽 `src/jarvis/`，出现既有导入错误；未修改 packaging 或测试绕过。
本轮边界、剩余项和证据见 `docs/reports/K0-002_CONTRACT_REFINEMENT_20261018.md`。
