# 架构说明

本文描述 JARVIS v1.0 的实际结构：进程、数据流、模块边界与关键约束。改动代码前先读本文与 `README.md` 的设计约束。

## 一、进程拓扑

```
                ┌──────────────────────────────────────────────┐
                │              macOS 宿主 / Linux 服务器        │
                │                                              │
  Apple 设备     │  ┌─────────────┐     ┌──────────────────┐   │
  ┌─────────┐   │  │voice_gateway│     │  jarvis_daemon    │   │
  │ iPhone  │──ws─┼─▶│  asyncio    │     │  内心循环/记忆/复盘 │   │
  │ Watch   │    │  │  :8765      │     └────────┬─────────┘   │
  │ Mac App │    │  └──────┬──────┘              │             │
  └─────────┘    │         │                     │             │
                 │  ┌──────▼─────────────────────▼──────────┐  │
                 │  │ chat_launcher / chat_entry（模型进程） │  │
                 │  └──────┬─────────────────────────────────┘  │
                 │         │                                    │
                 │  ┌──────▼──────┐    ┌──────────────────┐    │
                 │  │  外部供应商  │    │ ~/.jarvis/loop-data│   │
                 │  │ ASR/TTS/LLM │    │ SQLite 记忆账本    │    │
                 │  └─────────────┘    └──────────────────┘    │
                 └──────────────────────────────────────────────┘
```

- **voice_gateway**：WebSocket 服务，多设备接入与仲裁，把语音转成一轮"听→想→说"。
- **jarvis_daemon**：常驻思考循环，写记忆、产生假设、复盘；与语音共用同一个数据根和模型启动锁，因此**语音必须优先**。
- **chat_launcher / chat_entry**：把一次模型调用包装成受控子进程（占位目录、锁、超时、用量记账）。
- Apple 客户端：只负责采集、播放、显示状态；不含模型凭据。

## 二、一轮语音的数据流

1. 设备按 VAD 判定开口，逐帧上传 16 kHz PCM16（`audio`），说完发 `audio_end`。
2. 网关 `ClientSession.commit()` 合并缓冲，先做**声纹判定**（`voice_speaker.py`）：
   - 判为他人 → 不转写、不回答，进入让路状态（`CONVERSATION_HOLD` 秒无话后追问一句）。
   - 判为本人 → 继续。
3. ASR（`BailianSpeech.transcribe`，单次 12 秒上限 + 重试一次）。
4. 纯语气词（嗯/呃/啊…）不送大脑。
5. 大脑回答（`LauncherBrain` → `chat_launcher`）。
6. TTS（`VolcengineTTS`）逐块返回 MP3，网关按 generation 编号推给设备。
7. 设备播完回 `playback_done`；网关据此结束这一轮并记录账本。

断线、打断、换设备都靠 `generation` 编号隔离：旧 generation 的音频与文本一律丢弃。

## 三、模块边界

| 层 | 位置 | 职责 | 不允许做 |
|---|---|---|---|
| 设备客户端 | `apps/{mac,ios,watchos,wakeword,web}` | 采集、播放、状态显示、本地 VAD/唤醒 | 保存模型 key、直接调供应商 |
| 接入层 | `voice/voice_gateway.py` | 鉴权、设备仲裁、turn 管理、账本写入、**两条系统级固定台词**（让路追问、忙碌回执） | 业务决策；除上述两条外不得写死面向用户的台词 |
| 供应商适配 | `voice/voice_dashscope.py`、`voice/voice_volcengine.py`、`voice/voice_speaker.py` | ASR/TTS/声纹，统一接口 | 泄漏凭据到日志 |
| 大脑 | `packaging/chat_launcher.py`、`chat_entry.py` | 受控模型进程、超时、记账 | 越过审批做副作用操作 |
| 认知 | `packaging/inner_*.py`、`packaging/inner_loop.py` | 内心循环、记忆、假设、复盘 | 直接面向用户说话 |
| 内核草案 | `src/jarvis/` | 审批、预算、取消、事件存储、备份（K0–K4） | 在未过门禁前接线到生产 |
| 客户端 UI | `apps/mac/JarvisMac/Sources/JarvisUI` 等 | 展示与菜单 | 持有业务状态 |

## 四、一句话切分（VAD/端点检测）

设备侧负责"何时算说完"，因为只有设备知道实时音频：

- **Mac 与 iOS 的规则不同，不要混用**：
  - **Mac**（`UtteranceGate.swift`）：自适应底噪、分档静音、观察窗、语气词丢弃，可仿真。
  - **iOS**（`SpeechGate`）：固定门限 + 900 ms 静音 + 25 秒上限，没有自适应与观察窗；
    它的优势是简单、可预测，代价是嘈杂环境与小声音不如 Mac。
  下面列的是 Mac 的规则：
  - 启动先 0.5 秒标定房间底噪，门限 = 底噪 × 2.0（下限 0.004），不写死音量。
  - 结束静音分档：短句 1.4 s、常规 0.9 s、长独白 0.8 s。
  - 判定结束后再留 0.5 s 观察窗，期间又出声算同一句（对应豆包"逗号级停顿"）。
  - 开口前保留 300 ms 预卷，第一个字不被切掉。
  - 不足 0.10 s 语音的片段不上传。
- 可离线仿真：`Tests/UtteranceSimulation/main.swift`，覆盖 16 种说话方式。

## 五、多设备仲裁

`coordinate()` 按设备状态（佩戴、锁屏、朝下、蓝牙、信号）算出唯一 owner：

- 正在采集/播放/排队的 owner 被锁定，避免半句话被换设备接走。
- 非 owner 收到 `suppressed`，不采集、不播放。
- 声纹判定为他人时，owner 让路，不回答。

## 六、数据与账本

- 记忆库：`~/.jarvis/loop-data/*.db`（SQLite，`entries` 表为唯一事实表，`seq` 单调递增）。
- 语音账本：每轮写 `voice_turn` / `voice_playback` / `record_interrupt`，可从 `entries` 追溯。
- 语音片段：`~/.jarvis/utterances/*.wav`，保留最近 40 段，用于声纹校准与复盘。
- 凭据：`~/.jarvis/*.key`（0600）；`voice-gateway.token` 为设备共享 Bearer。

## 七、生产就绪的关键约束

1. **语音优先**：`chat_launcher` 的锁等待必须给语音更短路径；后台长调用不得占锁数分钟。
2. **慢查询零容忍**：`entries` 已 23 万行，任何相关子查询都要先取集合再过滤（见 `inner_runtime.py` 的改写）。
3. **副作用幂等**：未过 K3 门禁前不开通用执行权限。
4. **凭据隔离**：模型子进程目前能拿到明文 key，正式版要走 sidecar/代理。
5. **网关不放公网**：当前仅明文 ws + 共享 token，只可用于内网或 Tailscale。

## 八、文档索引

- **代码框架（分层/入口/加功能放哪）**：`docs/FRAMEWORK.md`
- 模块清单与行数：`docs/CODE_MAP.md`（由 `scripts/gen_code_map.py` 从源码生成）
- 语音子系统：`voice/README.md`
- 目标与规划：`docs/planning/`
- 数据/领域契约：`contracts/` 与 `docs/design/`（后者是草案）
- 验收与缺口：`docs/reports/`
- 架构决策：`docs/adr/`
- 操作手册：`docs/guides/`
- 外部调研：`docs/research/`
- 历史交接：`docs/HANDOFF_DIRECTION.md`
- 归档与遗留：`archive/README.md`
