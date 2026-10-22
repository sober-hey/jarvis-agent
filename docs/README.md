# 文档索引

按"要解决什么问题"找文档，不按文件名找。

## 想先了解这个项目

| 文档 | 内容 |
|---|---|
| [README.md](../README.md) | 项目是什么、怎么跑、目录结构、设计约束 |
| [ARCHITECTURE.md](../ARCHITECTURE.md) | 进程拓扑、一轮语音的数据流、模块边界、数据账本 |
| [FRAMEWORK.md](FRAMEWORK.md) | 代码框架：分层、四个入口、加功能放哪、测试怎么跑（**先看这个再读代码**） |
| [CODE_MAP.md](CODE_MAP.md) | 代码地图：每个模块负责什么、多少行（`scripts/gen_code_map.py` 自动生成） |
| [../voice/README.md](../voice/README.md) | 语音子系统（已隔离）：十个模块各自管什么、怎么跑、客户端在哪 |

## 当前状态与缺口

| 文档 | 内容 |
|---|---|
| [../README.md](../README.md)「还没做 / 没验证」 | 当前未实现与未验证的能力清单 |
| [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) | 上线就绪度总览 |
| [reports/PRODUCTION_GAP_INVENTORY_20261021.md](reports/PRODUCTION_GAP_INVENTORY_20261021.md) | 上线缺口清单，按阻断程度分类，每条带证据等级 |
| [adr/](adr/) | 已做过的架构取舍 |

## 目标与规划

- [planning/JARVIS_GOAL.md](planning/JARVIS_GOAL.md)：长期目标
- [planning/JARVIS_Engineering_Master_Plan_v1.1.2.md](planning/JARVIS_Engineering_Master_Plan_v1.1.2.md)：当前有效总规划
- [planning/](planning/)：长期目标、当前总规划、待决策项

## 架构决策（改设计前必读）

[adr/](adr/) 里每条 ADR 记录一个已经做过的取舍和理由。改到相关部分先读对应那条，
不要重新发明一遍：

| ADR | 主题 |
|---|---|
| ADR-0006 | 取消与发布的线性化 |
| ADR-0008 | TTS 换用火山 `seed-tts-2.0` |
| ADR-0009 | 一句话切分交给客户端（Mac/iOS 规则不同） |
| ADR-0010 | 本地声纹与"别人说话时让路" |
| ADR-0011 | 模型结果未知时拒绝新发送 |
| ADR-0012 | 上下文裁剪与已申报的测试语义变化 |

## 契约（改动数据结构前必读）

`contracts/` 下是数据与领域契约，目前状态 DRAFT / NOT_FROZEN：

- `database_constraints.yaml`：SQLite 约束
- `domain_relationships.yaml`：领域对象关系
- `draft/`：候选契约（未冻结）

K0 内核层的设计推演在 [design/](design/)：领域契约、文件系统协议、恢复协议、重试决策、
路径契约、可追溯矩阵。**这些是设计稿，不是当前实现的规格**，每份文件头部都有状态标记。

## 语音

- [../voice/README.md](../voice/README.md)：模块职责、依赖方向、跑法、已知边界（**先看这个**）
- [guides/VOICE.md](guides/VOICE.md)：出故障时怎么取证、参数与出处
- [voice-docs/](voice-docs/)：网关协议、流式 TTS、百炼实测结论、小爱音箱通道、适配器验证报告

## 验收与证据

- [adr/](adr/)：每条架构决策的理由与取舍
- `evidence/runs/`：长跑原始证据（不进 git）
- `~/.jarvis/loop-data/*.db`：运行账本

## 操作手册（遇到具体故障时看）

| 场景 | 位置 |
|---|---|
| 语音有问题（没反应/只有一句/声音小/别人说话它也答） | [guides/VOICE.md](guides/VOICE.md)：先取证的三条命令、故障对照表、参数与出处 |
| 语音网关起不来 / 设备连不上 | [guides/VOICE.md](guides/VOICE.md) + `ARCHITECTURE.md` |
| 改模型、换供应商、配 key | skill `hermes-model-link-config`（Hermes 侧） |
| 火山 TTS 配置与音色 | `voice/voice_volcengine.py` 文件头 |
| 声纹校准 | `~/.jarvis/speaker-owner.npy` + `voice/voice_speaker.py` |
| 令牌轮换 | `scripts/rotate_gateway_token.sh` |
| 仓库结构约定 | 本文件与 `README.md` 的目录表 |

## 调研（外部方案对照，含出处）

四份调研都是在动代码之前做的，结论里标了证据等级（官方文档/源码/工程博客/推断）：

- [research/voice_pipeline_research.md](research/voice_pipeline_research.md)：语音流水线、回声消除、打断、端点检测。关键结论：`AVAudioPlayer` 不在 VPIO 输出总线上，所以开了 AEC 也抵不掉自己的 TTS
- [research/longrun_reliability_research.md](research/longrun_reliability_research.md)：SQLite 连接级配置、索引与归档、锁分层与优先级、崩溃恢复、systemd/launchd 监督
- [research/production_deploy_research.md](research/production_deploy_research.md)：systemd 单元写法与五个坑、密钥 LoadCredential、3-2-1 备份、监控最小集、迁移清单
- [research/repo_reorg_plan.md](research/repo_reorg_plan.md)：仓库迁移映射、破构建风险、分阶段命令

部署：见 [../deploy/README.md](../deploy/README.md)（草案，未在 Linux 验收）。

## 归档

历史快照、设计草案、逐增量评审、实验产物不随本仓库公开（保留在项目本地），
它们不参与构建，也不代表当前行为。
