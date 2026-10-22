# voice/ — 语音子系统（已隔离）

这一层是 JARVIS 的耳朵和嘴：把设备上的音频变成文字、把大脑的回答变成声音、
在多人环境里分清谁在说话。**它与 `packaging/` 下的产品主体分开存放**，
目的是让语音链路的改动、评审和替换不牵动主程序。

## 目录

| 文件 | 职责 |
|---|---|
| `voice.py` | 语音基础能力：WAV 读写与分块、ASR 后端抽象、`VoiceConfig`、`speak_stream()` 播放入口、`self_evaluate()` 自检钩子 |
| `voice_gateway.py` | WebSocket 网关（默认 8765）：设备接入、鉴权、一轮对话的状态机、让路逻辑、TTS 分发 |
| `voice_port.py` | 端口协议：`VoiceContext` / `VoiceEventSink` / `BrainVoiceSink` / `SpeakPriority` / `VoiceMode` |
| `voice_live.py` | 实时双向链路：流式 ASR → 大脑 → 流式 TTS，含打断与轮次切换 |
| `voice_stream_tts.py` | 流式合成基座：`SentenceBuffer`（按句切分）、`StreamingTTS` 抽象 |
| `voice_dashscope.py` | 阿里百炼适配器：ASR（含情绪）与 CosyVoice 流式合成 |
| `voice_volcengine.py` | 火山引擎适配器：`seed-tts-2.0` 双向流式合成（当前默认 TTS） |
| `voice_speaker.py` | 声纹识别（sherpa-onnx CAM++，192 维），判定是不是 Sir 本人在说话 |
| `voice_ledger.py` | 语音账本：每次用户说话/助手回应落 `inner_loop` 的 `entries`，供审计与画像 |
| `voice_xiaoai.py` | 小爱音箱控制（外部设备通道，非主链路） |
| `tests/` | 上述模块的测试（213 passed） |

## 怎么跑

```bash
# 单元测试（不需要真实设备、不需要联网）
PYTHONPATH=voice:packaging .venv/bin/python3 -m pytest voice/tests -q

# 本机启动网关
PYTHONPATH=voice:packaging .venv/bin/python3 voice/voice_gateway.py \
  --token-file ~/.jarvis/voice-gateway.token \
  --config ~/.jarvis/config.json \
  --brain-key ~/.jarvis/api.key \
  --data-dir ~/.jarvis/loop-data \
  --log ~/.jarvis/voice-gateway.log
```

macOS 上由 launchd 的 `com.jarvis.voice-gateway` 拉起，启动脚本 `~/.jarvis/run_voice_gateway.sh`；
Linux 上由 `deploy/systemd/jarvis-voice-gateway.service` 拉起。两者的 `PYTHONPATH`
都必须同时包含 `voice/` 和 `packaging/` —— 语音层要读 `packaging/inner_loop.py` 的账本。

## 与主程序的关系

- **依赖方向是单向的**：`voice/` 可以 import `packaging/`（`inner_loop`、`device_routing` 等），
  反过来只有 `packaging/web_server.py` 为了显示网关状态按需加载 `voice_gateway`。
- 主程序**没有语音 SDK 也必须能跑**：`web_server` 里对 `voice_ledger` / `voice_gateway`
  的引用都是可选导入，缺失时降级而不是崩。
- 网关是独立进程，主程序挂掉不影响已建立的音频会话，反之亦然。

## 客户端

语音的另一半在设备端，不在这个目录里：

| 端 | 位置 | 负责什么 |
|---|---|---|
| Mac | `apps/mac/JarvisMac/Sources/JarvisIntegration/` | `MicrophoneCapture`（VPIO 采集）、`UtteranceGate`（自适应端点检测：标定底噪、分档静音、预卷、观察窗）、`AudioCapture` |
| Mac | `apps/mac/JarvisMac/Sources/JarvisAudio/` | 播放与音频会话 |
| Mac | `apps/mac/JarvisMac/Sources/JarvisTransport/` | WebSocket 传输与重连 |
| Mac | `apps/mac/Modules/Activation/` | 唤醒词、响指/拍手手势检测 |
| iPhone | `apps/ios/JarvisVoice/` | 点球体说话，已移除唤醒词；`SpeechGate` 固定门限 |
| Watch | `apps/watchos/JarvisWatch/` | 球体界面与录音触发 |

客户端与服务端的接口是 `voice_gateway.py` 里定义的 WebSocket 消息（音频帧、`playback_done`、
`tts_failed` 等），协议细节见 `docs/guides/VOICE.md`。

## 已知边界

- 端点检测两端规则不同：Mac 用自适应 `UtteranceGate`（标定底噪、分档静音、观察窗、预卷），
  iOS 用固定门限 `SpeechGate`（900ms 静音 + 25s 上限）。这是刻意的，不要强行统一。
- 声纹阈值 0.40 来自三类声音的实测间隔（本人参考 1.000、克隆音 0.619、女声 0.030），
  换模型或换麦克风要重新标定。
- 让路逻辑：检测到非本人说话就不转写不回答，最后一次人声后 20 秒自动问
  「Sir，您沟通完了吗？还需要我跟您继续汇报吗？」。
