# scripts/ — 运维与生成脚本

每个脚本都能单独跑；带副作用的都有干跑开关或只读模式。

| 脚本 | 用途 | 会不会改数据 |
|---|---|---|
| `run_quality_checks.sh` | 一键跑 ruff + pyright + pytest + 环境检查 | 否 |
| `run_regression.py` | 跑全量回归并落证据 | 只写证据目录 |
| `gen_code_map.py` | 从源码生成 `docs/CODE_MAP.md` | 只写这一份文档 |
| `healthcheck.py` | 探活：8765 端口、日志新鲜度、心跳、DB/WAL/磁盘/RSS 阈值 | 否 |
| `backup_mind_db.sh` | 用 `sqlite3 .backup` 做一致性快照 + `integrity_check` + 行数校验 + sha256，保留最近 14 份 | 只写备份目录 |
| `rotate_gateway_token.sh` | 轮换语音网关令牌：原子落盘、重启、校验监听；`--dry-run` 可先看 | 改令牌文件、重启网关 |
| `check_environment.py` | 校验运行环境（Python 版本、依赖、路径） | 否 |
| `evidence_status.py` | 汇总验收证据状态 | 否 |
| `export_mind_stats.py` | 导出记忆账本统计（行数、类别分布、增长） | 否 |
| `route_compliance_probe.py` | 探针：核对模型路由是否符合配置 | 否 |

## 约定

- 新增脚本要有 `--help`，破坏性操作要有 `--dry-run`。
- 不要在脚本里写死本机路径；用参数或环境变量。
- 脚本输出里不要打印密钥、令牌、手机号。
