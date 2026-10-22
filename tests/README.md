# tests/ — 内核层与契约测试

这一组测试只覆盖 `src/jarvis/`（内核草案）与 `contracts/`，**不覆盖生产运行时**。
运行时测试在 `packaging/test_*.py`，语音测试在 `voice/tests/`。

```sh
mkdir -p ~/.tmp-pytest
TMPDIR=$HOME/.tmp-pytest PYTHONPATH=src:packaging:. .venv/bin/python3 -m pytest tests -q
```

当前结果：**2381 passed**。

## 目录

| 目录 | 内容 |
|---|---|
| `unit/` | 内核各模块的单测（规划、事件、预算、审批、路径控制、记忆、模型适配…） |
| `contract/` | 契约测试：对照 `contracts/*.yaml` 与 `docs/planning/` 里的总规划校验数据结构与不变量 |
| `integration/` | 跨模块集成：K1 黄金任务、K2 记忆与人格评估、K3 自主性评估 |
| `fault_injection/` | 故障注入：模型结果未知时不得派生新进程、解除后恢复、显式放行留痕 |

## 写测试的三条规矩

1. **失败就修代码，不要放宽断言。** 确实要改语义，写进 `docs/adr/` 并在提交信息里申报。
2. **超时不要卡在临界值。** 机器满载时进程启动就超过阈值，表现成"偶发失败"。
3. **不许用真实凭据或真实个人数据。** 需要密钥时用假值，需要音频时现场合成。
