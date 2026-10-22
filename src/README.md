# src/jarvis — 内核层（K0–K4 草案）

**这一层是设计中的确定性执行内核，不是当前生产路径。** 生产路径在 `packaging/`（守护进程 + 模型调用）
和 `voice/`（语音），它们目前不依赖这里的代码。

放到这里，是因为它记录了"如果要做成一个可审计、可恢复、有预算与审批的执行系统，需要哪些部件"。
下面的模块大多有单元测试（`tests/unit/`），但**没有接线到生产**，也没有过 K 阶段门禁。

## 分块

| 主题 | 模块 |
|---|---|
| 任务与计划 | `planner.py`、`structured_planner.py`、`plan_history.py`、`repository.py`、`worker.py` |
| 意图与执行 | `intent.py`、`outbox.py`、`publisher.py`、`isolated_runner.py`、`test_runner.py` |
| 事件与一致性 | `event_store.py`、`event_schema.py`、`event_dedup.py`、`snapshot.py`、`provenance.py` |
| 恢复与对账 | `reconciler.py`、`recovery.py`、`rollback.py`、`backup.py`、`retention.py` |
| 预算与审批 | `budget.py`、`autonomy_budget.py`、`autonomy_controls.py`、`approval.py`、`release_approval.py`、`controlled_release.py` |
| 记忆 | `episodic_memory.py`、`semantic_memory.py`、`working_memory.py`、`memory_schema.py`、`retrieval.py`、`preference_store.py` |
| 模型接入 | `model_provider.py`、`openai_adapter.py`、`provider_registry.py`、`mock_provider.py` |
| 边界与安全 | `path_control.py`、`prompt_guard.py`、`skill_registry.py`、`skill_manifest.py`、`environment.py` |
| 人格与改进 | `persona.py`、`improvement_proposal.py`、`version_manager.py`、`health.py`、`cli.py` |

## 设计依据

- 契约：`contracts/`（DRAFT，未冻结）
- 设计推演：`docs/design/`（领域契约、文件系统协议、恢复协议、重试决策）
- 决策理由：`docs/adr/ADR-0001..0006`

## 规矩

- 不要在未过门禁前把它接到生产路径上（`packaging/` 与 `voice/` 里出现 `from jarvis import ...` 就是越线）。
- 改这里的语义要同步 `contracts/` 与 ADR；只改代码不改契约，下一轮评审会被打回。
- 测试：`TMPDIR=$HOME/.tmp-pytest PYTHONPATH=src:packaging:. .venv/bin/python3 -m pytest tests -q`
