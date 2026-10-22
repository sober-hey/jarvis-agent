# apps/mac — macOS 端

菜单栏常驻的语音入口，是三个客户端里功能最全的一个：离线唤醒词、响指/连拍手势、
自适应端点检测、本地 VPA 采集与播放。

```
JarvisMac/
  Sources/
    JarvisApp/          菜单栏与窗口生命周期
    JarvisUI/           视图（状态、设置、会话）
    JarvisAudio/        音频会话与播放
    JarvisSpeech/       端点检测
    JarvisIntegration/  与网关的连接、MicrophoneCapture、UtteranceGate
    JarvisTransport/    WebSocket 传输与重连
    JarvisCore/         共享模型与配置
  Tests/
    UtteranceSimulation/  一句话切分的离线仿真（16 种说话方式）
  Scripts/build_app.py    一键出 .app
Modules/                  四个独立 Swift 包
  Activation/             唤醒词 + 响指/拍手检测（有阈值回归测试）
  AudioAccessibility/     音频设备与权限
  AudioRecorder/          录音原语
  WebSocket/              传输原语
```

## 构建与运行

```sh
cd apps/mac/JarvisMac
python3 Scripts/build_app.py          # → Output/JARVIS.app
open Output/JARVIS.app
```

## 两件容易踩的事

- **端点检测规则和 iOS 不一样**，是刻意的：Mac 用自适应 `UtteranceGate`（标定底噪、分档静音、
  观察窗、300ms 预卷），iOS 用固定门限 `SpeechGate`。不要为了"统一"把它们合并。
- **手势阈值来自真实录音回放标定**（响指 −36dBFS、上升 4dB）。换麦克风或换房间要重新标定：
  `cd Modules/Activation && swift run activation-regression`。

## 调试

- 手势原始数据：`~/.jarvis/mac-gestures.jsonl`
- 应用日志：`~/Library/Containers/com.jarvis.macos/Data/Library/Logs/JARVIS/mac-events.jsonl`
