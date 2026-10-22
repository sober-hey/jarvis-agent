# 代码框架

给两类人看：要**审阅全部代码**的人，和要**加新功能**的人。
看完这份能知道代码分成哪几层、数据从哪进哪出、新东西该放哪。

> 每个文件的具体职责与行数见 [CODE_MAP.md](CODE_MAP.md)（自动生成）。
> 进程拓扑与一轮对话的数据流见 [../ARCHITECTURE.md](../ARCHITECTURE.md)。
> 语音那一层单独成册：[../voice/README.md](../voice/README.md)。

## 一、分层的顺序（从外往里）

```
设备（Mac / iPhone / Watch）
   │  WebSocket + Bearer 令牌
   ▼
voice/voice_gateway.py          接入层：鉴权、会话、一轮对话的状态机、让路
   │  进程内调用
   ▼
packaging/inner_runtime.py      认知层：思考、取舍、节奏（何时开口、说什么）
   │
   ├─→ packaging/chat_launcher.py   模型层：起 CLI、发请求、收流
   ├─→ packaging/inner_loop.py      记忆层：只追加的账本（MindStore.entries）
   └─→ packaging/daemon_wiring.py   调度层：把定时/触发的事接到上面三者
   ▲
   │  launchd 拉起
packaging/jarvis_daemon.py      守护进程：常驻，跑内心循环与后台整理
```

关键约束（改代码前必须知道）：

1. **记忆只有一处可写**：`inner_loop.MindStore.entries`，只追加。任何"改历史"的想法都要先读 ADR。
2. **模型调用只有一个出口**：`chat_launcher`。别在别处直接起 HTTP 请求或 CLI。
3. **语音层可以依赖 packaging/**，反过来只有 `web_server.py` 按需加载网关，其余不许。
4. **import 时不许有副作用**：不起进程、不建连接、不写文件。这条被测试守着。
5. **组件要在 `integration_registry.py` 登记**，但它只是台账，不代表功能已完成。

## 二、四个入口

| 入口 | 文件 | 拉起方式 | 崩了会怎样 |
|---|---|---|---|
| 守护进程 | `packaging/jarvis_daemon.py` | `launchctl kickstart -k gui/$(id -u)/com.jarvis.daemon` | 内心循环停摆，语音仍可答话（网关独立进程） |
| 语音网关 | `voice/voice_gateway.py` | `launchctl kickstart -k gui/$(id -u)/com.jarvis.voice-gateway` | 设备连不上，守护进程不受影响 |
| Web 面板 | `packaging/web_server.py` | 由 `packaging/supervisor_components.py` 作为受监督子进程拉起，仅监听回环地址 | 面板打不开；监督层会重启它，语音与守护进程不受影响 |
| 桌面胶囊 | `~/.jarvis/jarvis_ui.py` | launchd `com.jarvis.ui`（`~/.jarvis/launch_ui.sh`） | 胶囊消失；它只是视图，历史与逻辑都在守护进程里 |
| 模型启动器 | `packaging/engine_entry.py` | 由 `chat_launcher` 调用 | 所有需要模型的功能失败，降级路径接管 |

守护进程内部还有一层监督：`supervisor_components.py` 把 Web 面板与常驻 worker 作为
受监督子组件管起来（各自带进度心跳），任一个挂了单独重启，不牵动主循环。

launchd 单元在 `~/Library/LaunchAgents/com.jarvis.*.plist`；Linux 上的对应物在 `deploy/systemd/`。

## 三、配置与数据放在哪

| 东西 | 位置 | 说明 |
|---|---|---|
| 模型与供应商 | `~/.jarvis/config.json` | `model` / `provider` / `base_url` / `api_mode` / `context_length` / `reasoning_effort` |
| 模型 key | `~/.jarvis/api.key` | 权限 600，不进 git |
| 网关令牌 | `~/.jarvis/voice-gateway.token` | 轮换用 `scripts/rotate_gateway_token.sh` |
| 记忆账本 | `~/.jarvis/loop-data/mind.db` | 只追加；FTS 投影可重建 |
| 声纹参考 | `~/.jarvis/speaker-owner.npy` | 换麦或换模型要重标定 |
| 时间线 | `~/.jarvis/timeline.json` | 对外展示用投影 |
| 语音配置 | `voice/voice_config.json` | 百炼 ASR/TTS 参数 |

## 四、加功能放哪（对照表）

| 想做的事 | 改哪里 | 顺手要做 |
|---|---|---|
| 加一个 TTS/ASR 供应商 | 在 `voice/` 新建 `voice_<供应商>.py`，实现 `voice_stream_tts.StreamingTTS` 或 `voice.VoiceBackend` 的接口 | 在 `voice_gateway` 的供应商选择处挂上；在 `integration_registry.VOICE_MODULES` 加名字；补 `voice/tests/test_<供应商>.py` |
| 改端点检测 / 一句话切分 | Mac 端 `apps/mac/.../UtteranceGate`；iOS 端 `SpeechGate` | 两端规则刻意不同，改一端先想清楚另一端（ADR-0009） |
| 加一个设备通道（新音箱/新传感器） | `packaging/device_controller.py`（显式注册）+ `packaging/device_routing.py`（策略） | 注册表登记；补路由策略测试 |
| 加一个后台定时任务 | `packaging/daemon_wiring.py` 里挂，`packaging/scheduler` 相关的既有写法照抄 | 必须可幂等；重启后不能重复触发（`once_keys`） |
| 改模型 / 换站点 | `~/.jarvis/config.json`，不改代码 | 见 skill `hermes-model-link-config` |
| 加一个新的记忆投影 | 新建 `packaging/inner_loop_<名字>.py`，只读 `MindStore.entries` | 投影必须可重建；不许回写源 |
| 改面板 | `packaging/web_server.py` + `apps/web/`（静态页） | 面板只读真相源，缺失显示 `—`，不许编数 |
| 加一个内核层能力 | `src/jarvis/`（K0–K4，草案） | 先读 `docs/design/` 与 `docs/adr/`，写 ADR 再动手 |

## 五、测试怎么跑

```sh
# Python 侧全部（约 25 分钟，满载会超时，别并行跑两份）
mkdir -p ~/.tmp-pytest
TMPDIR=$HOME/.tmp-pytest PYTHONPATH=packaging .venv/bin/python3 -m pytest packaging/ -q

# 单元测试（快）
mkdir -p ~/.tmp-pytest
TMPDIR=$HOME/.tmp-pytest PYTHONPATH=src:packaging:. .venv/bin/python3 -m pytest tests -q

# 语音子系统（快，213 条）
PYTHONPATH=voice:packaging .venv/bin/python3 -m pytest voice/tests -q

# 契约测试
mkdir -p ~/.tmp-pytest
TMPDIR=$HOME/.tmp-pytest PYTHONPATH=packaging .venv/bin/python3 -m pytest packaging/test_integration_contract.py -q
```

测试布局：

| 位置 | 内容 |
|---|---|
| `packaging/test_*.py` | 运行时行为测试，105 个文件（最多；含 e2e、恢复、安全相关的实际用例） |
| `voice/tests/` | 语音子系统测试（213 条） |
| `tests/unit/` | 内核层与工具函数单测 |
| `tests/contract/` | 契约测试（对照 Master Plan 与 `contracts/`） |
| `tests/integration/` | 跨模块集成（K1/K2/K3 黄金任务） |
| `tests/fault_injection/` | 故障注入（结果未知后的门禁行为） |

关于 `tests/e2e`、`tests/recovery`、`tests/security`、`tests/regression`：这四个目录**曾经建了但一直是空的**，
已删除。这些关注点的真实用例在 `packaging/` 下（`test_outreach_e2e.py`、`test_recovery.py`、
`test_unknown_gate.py`、`test_integration_contract.py` 等）。**不要重建空目录充数**——
一个空目录会被当成"已有覆盖"，比没有更糟。

几条硬规矩：

- **不许为过测试改断言**。真失败就修代码；确实要改语义，写进 ADR 并在提交信息里写明"已申报的语义变化"。
- **超时类用例别把时间卡在临界点**。机器满载时 `pytest` 还没 import 完就被杀，表现成"偶发失败"。
- **证据进 `evidence/`**，不塞进聊天或提交信息。

## 六、仓库结构一览

```
apps/           设备端（mac/ ios/ watch/ wakeword/）
voice/          语音子系统（网关 + ASR/TTS + 声纹 + 账本）
packaging/      Python 运行时（守护进程、模型启动器、面板、适配器）
src/jarvis/     内核层 K0–K4（草案，未全部接线）
components/     第三方上游（agent_engine/upstream 属构建的一部分）
contracts/      数据与领域契约（DRAFT / NOT_FROZEN）
deploy/         systemd 单元与部署说明
docs/           文档（索引 docs/README.md）
  design/       K0 设计稿（草案，非规格）
  adr/          架构决策记录
  planning/     目标、总规划、待决策项
  reports/      验收报告与生产缺口
  guides/       操作手册
  research/     外部方案调研
  voice-docs/   语音协议与实测
scripts/        运维与生成脚本
tests/          内核层与契约测试
archive/        历史快照与遗留（不参与构建，见 archive/README.md）
evidence/       长跑证据（不进 git）
subprojects/    3D 形象子项目（独立构建）
```

## 七、改代码之前的三件事

1. **读对应的 ADR**。`docs/adr/` 里 13 条决策写清了为什么现在是这样，别重新发明。
2. **查真源**。进度看 `PROJECT_STATE.md` 与 `docs/reports/`，不看提交信息也不看登记表。
3. **动手前把最坏情况说清楚**：会影响哪些正在跑的服务、会不会丢数据、回滚怎么做。
