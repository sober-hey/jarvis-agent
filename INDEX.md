# 目录索引（INDEX）

> 这份文件回答两个问题：**每个目录在哪、是干什么的**。
> 人和 AI 都从这里找入口。新加目录必须在这里登记，否则等于没有。

## 一、顶层总览

| 路径 | 是什么 | 入口 / 怎么用 |
|---|---|---|
| `voice/` | 语音子系统：网关、ASR/TTS 适配器、声纹、语音账本 | 起网关：`voice/voice_gateway.py`；说明：`voice/README.md` |
| `packaging/` | Python 运行时：守护进程、模型调用、Web 面板、调度、记忆 | 起守护进程：`packaging/jarvis_daemon.py` |
| `packaging/runtime/engine-venv/` | 引擎运行环境（Python 3.14，**不随库分发**） | 重建：`scripts/build_engine_runtime.sh` |
| `src/jarvis/` | 内核层 K0–K4 草案（审批、预算、取消、事件存储）**未接线到生产** | 阅读起点：`src/README.md` |
| `tests/` | 内核层与契约测试（2381 条） | `pytest tests -q`（见 `tests/README.md`） |
| `apps/` | 设备端：mac / ios / watchos / web 面板 | 各目录有自己的 README |
| `contracts/` | 数据与领域契约（YAML，状态 DRAFT） | 改数据结构前必读 |
| `deploy/` | 部署：systemd 单元、launchd 模板、安装步骤 | `deploy/README.md` |
| `docs/` | 全部文档（按主题分目录，见下表） | 索引：`docs/README.md` |
| `scripts/` | 运维与生成脚本（构建引擎、备份、健康检查、令牌轮换） | `scripts/README.md` |
| `components/agent_engine/upstream/` | 第三方上游 agent engine（MIT，246 MB，**仅本机，不随库分发**） | 引擎环境的前置条件 |
| `subprojects/jarvis-avatar/` | 3D 形象工程（Blender，含商业 SDK，**仅本机，不随库分发**） | `subprojects/jarvis-avatar/PROJECT_STATE.md` |
| `subprojects/3d-interactive-demo/` | 另一个 AI 生成的「3D 交互演示」独立小项目，与本项目无耦合（**仅本机**） | `subprojects/3d-interactive-demo/README.md` |
| `subprojects/particle-demo/` | 另一个 AI 生成的「粒子形态转换」网页演示（**仅本机**） | `subprojects/particle-demo/README_PARTICLE.md` |
| `assets/` | 视觉素材：角色设定、三视图、表情表 | `assets/README.md` |
| `archive/` | 历史快照与遗留产物，**不参与构建** | `archive/README.md` |
| `.runs_01/` | 历次长跑/验收的原始运行数据（**仅本机**，4 G，不进 git） | 只有复查历史结论时才看 |
| `public-repo/` | 开源镜像（独立 git 仓库，推到 GitHub；**仅本机**，不进主仓库 git） | 同步：`scripts/sync_public.sh` |
| `_backups/` | 本机备份 tarball（**仅本机**） | 校验和见其中的 `SHA256SUMS` |
| `.hermes/plans/` | 早期「如何使用 JARVIS」的方案草稿（**仅本机**） | 历史参考，别当规格 |

## 二、文档目录（`docs/`）

| 路径 | 放什么 | 什么时候看 |
|---|---|---|
| `docs/FRAMEWORK.md` | **代码框架**：分层、四个入口、加功能放哪、测试怎么跑 | 想读代码或加功能，先看这个 |
| `docs/CODE_MAP.md` | 每个模块一句话职责 + 行数（脚本自动生成） | 找"哪个文件负责什么" |
| `docs/README.md` | 文档总索引（按"要解决什么问题"排） | 找文档 |
| `docs/guides/RUN.md` | **启动手册**：从零到能说话，七步带验证命令 | 第一次部署 |
| `docs/guides/VOICE.md` | 语音故障对照表、取证命令、参数出处 | 语音出问题 |
| `docs/guides/REVIEW.md` | 评审协议：谁审、怎么取证、什么算通过 | 发起评审 |
| `docs/adr/` | 架构决策记录（12 条，含 TTS 选型、端点检测分工、声纹让路） | 改设计前必读 |
| `docs/design/` | K0 内核设计稿（领域契约、文件系统协议、恢复协议） | 读 `src/jarvis/` 前 |
| `docs/planning/` | 长期目标、总规划 v1.1.2、待决策项 | 想知道"要往哪走" |
| `docs/reports/` | 上线缺口清单、最近评审、回归结果、未解决问题的测量记录 | 想知道"还差什么" |
| `docs/research/` | 外部方案调研（语音流水线、长驻可靠性、生产部署） | 选型前 |
| `docs/voice/` | 网关协议、流式 TTS、百炼实测、小爱通道 | 改语音协议前 |

## 三、顶层关键文件

| 文件 | 作用 |
|---|---|
| `README.md` | 项目门面：是什么、已实现/未实现、最短启动路径 |
| `INTRO.md` | 项目介绍（给人看的那一版，讲故事、讲边界） |
| `INDEX.md` | 本文件：目录地图 |
| `ARCHITECTURE.md` | 进程拓扑、一轮语音的数据流、模块边界、数据账本 |
| `CONTRIBUTING.md` | 参与开发：环境、测试、提交约定、同步到公开库 |
| `PROJECT_STATE.md` | 工程状态快照（滚动更新，最新在上） |
| `AGENTS.md` | 接手者必读：恢复顺序、阶段门禁、禁止事项 |
| `pyproject.toml` / `uv.lock` | Python 3.12 主环境依赖 |

## 四、常见任务 → 去哪

| 我要…… | 去这里 |
|---|---|
| 在本机把 JARVIS 跑起来说话 | `docs/guides/RUN.md` |
| 加一个 TTS/ASR 供应商 | `voice/` 下新建 `voice_<vendor>.py`，见 `voice/README.md` |
| 改"一句话什么时候算说完" | Mac：`apps/mac/JarvisMac/Sources/JarvisIntegration/UtteranceGate.swift`；iOS：`apps/ios/JarvisVoice/JarvisVoice/Core/AudioPrimitives.swift` |
| 加后台定时任务 | `packaging/daemon_wiring.py`（必须幂等） |
| 查某个模块干什么 | `docs/CODE_MAP.md` |
| 部署到 Linux 服务器 | `deploy/README.md` + `deploy/systemd/` |
| 发布到 GitHub 公开库 | `scripts/sync_public.sh`（见 `CONTRIBUTING.md`） |
| 复查历史验收结论 | `docs/reports/` + `.runs_01/` 对应目录 |
| 备份记忆库 | `scripts/backup_mind_db.sh` |

## 五、给 AI 的机器可读索引

下面这段是同一份地图的紧凑形式，解析后可直接定位：

```yaml
repo: 仓库根/
purpose: 常驻本机的语音个人智能体（JARVIS v1.0，开发中）
entrypoints:
  voice_gateway: voice/voice_gateway.py        # WebSocket 设备接入，默认 8765
  daemon: packaging/jarvis_daemon.py           # 内心循环、记忆整理
  web_panel: packaging/web_server.py           # 本机回环面板，由 supervisor 拉起
  engine: packaging/engine_entry.py            # 模型调用入口（跑在 engine-venv 里）
build:
  python_main: uv sync --frozen               # Python 3.12 -> .venv
  engine_runtime: scripts/build_engine_runtime.sh   # Python 3.14 -> packaging/runtime/engine-venv
  clients: apps/mac/JarvisMac/Scripts/build_app.py  # macOS .app
tests:
  kernel: "TMPDIR=$HOME/.tmp-pytest PYTHONPATH=src:packaging:. .venv/bin/python3 -m pytest tests -q"
  voice: "PYTHONPATH=voice:packaging .venv/bin/python3 -m pytest voice/tests -q"
  runtime: "TMPDIR=$HOME/.tmp-pytest PYTHONPATH=packaging .venv/bin/python3 -m pytest packaging/ -q"
dirs:
  voice/: 语音子系统（网关/ASR/TTS/声纹/账本）
  packaging/: Python 运行时（守护进程、模型、面板、调度）
  src/: K0-K4 内核草案，未接线
  tests/: 内核与契约测试
  apps/: 设备端（mac/ios/watchos/web）
  contracts/: 数据与领域契约（DRAFT）
  deploy/: systemd 与 launchd 部署
  docs/: 文档（框架/指南/ADR/设计/报告/调研/语音）
  scripts/: 运维脚本（构建引擎、备份、健康检查、同步公开库）
  components/: 第三方上游引擎，不随库分发
  subprojects/: 3D 形象工程与 3D 交互演示，不参与主构建
  assets/: 视觉素材
  archive/: 历史快照，不参与构建
  .runs_01/: 历史运行数据（4G，不进 git）
  public-repo/: 开源镜像（独立 git 仓库）
  _backups/: 本机备份 tarball
external_required:
  - components/agent_engine/upstream（第三方 MIT 代码，需自行获取）
  - ~/.jarvis/config.json 与 ~/.jarvis/api.key（模型配置与密钥）
  - ~/.jarvis/voice-gateway.token（设备共享令牌）
```
