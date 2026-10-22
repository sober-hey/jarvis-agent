# JARVIS B01 独立入口

本目录提供可运行的 JARVIS 自有 `version/check` 命令。产品版本为 `0.1.0-b01`，固定引擎候选 commit 为 `d97007c32d6340f7ba79ac1dfb5773cc00846448`。当前没有对话命令、任务执行、授权数据库或发布协议。

从项目根执行：

```sh
/Users/sober/.local/bin/python3.12 -I -B packaging/jarvis.py --help
/Users/sober/.local/bin/python3.12 -I -B packaging/jarvis.py version --json
/Users/sober/.local/bin/python3.12 -I -B packaging/bootstrap.py --check --json
/Users/sober/.local/bin/python3.12 -B packaging/test_launcher.py -v
```

`--help`、`version` 无需引擎依赖；检查要求控制端 Python 3.12。`bootstrap.py` 只转交入口，不安装环境。检查成功退出 0；缺环境、路径隔离失败、资源缺失、解释器错误或核心包元数据缺失退出 2。当前真实项目没有 `packaging/runtime/engine-venv`，因此 bootstrap 检查实际退出 2。

产品 home 顺序为 `--home`、`JARVIS_HOME`、`~/.jarvis`；拒绝相对路径、空值、项目源码与宿主 Hermes 目录及符号链接回退。检查不创建 home。固定源码、venv 目录不能指向其他目录；引擎解释器只能在固定 venv 中解析，不使用 `sys.executable` 或 PATH 搜索作回退。系统级独立 CPython 可作为 venv 基础解释器，但宿主 Hermes tools/home 不可。

检查以 `-I -S -B` 启动引擎解释器的标准库探针，不执行 `.pth`，不导入上游 bootstrap/CLI。只继承语言与时区白名单；HOME/HERMES_HOME 强制指向产品 home，PATH 重建，无宿主 PYTHONPATH、Hermes 配置或 provider secret。它验证固定源码完整性、许可和资源、Python 3.14、venv 前缀/隔离和核心发行包元数据存在。核心元数据检查不替代完整锁依赖、原生 wheel 和实际引擎导入验证。`launch_ready` 固定为 false。

测试成功分支使用真实临时资源与非 Hermes 管理的现有 CPython 3.14 创建 `--without-pip --copies` 独立 venv，没有编造包元数据，也不联网安装。其资源清单明确没有第三方依赖；随后同一真实 venv 追加核心包要求，验证缺依赖失败。成功夹具的临时目录测试后删除，其结果只证明检查器成功分支，不代表本项目引擎已构建。

MIT 许可原文继续保留在 `components/agent_engine/upstream/LICENSE`。`VENDOR_MANIFEST.json` 包含实际导出树哈希；引擎目录本轮 18,556 个文件逐文件前后 SHA-256 相同。构建步骤、两种 Python 的独立环境、锁来源、资源、禁止懒安装/运行时更新、后续 OCI 步骤见 `BUILD_PLAN.json`。未构建镜像，未生成或虚构镜像 digest。

证据在 `.runs_01/b01-entry-20261014/`：`verification.json` 为命令/退出码/输出及源码前后校验；`real-fixture-success.json` 为真实隔离解释器成功夹具；`real-fixture-missing-dependencies.json` 为缺依赖失败；`red-help.json` 和 `red-isolation.json` 保存先失败后实现证据。后续完整启动前必须审计上游修复/包管理路径，单独实现适配层与已批准的 RPC/核心合同。
