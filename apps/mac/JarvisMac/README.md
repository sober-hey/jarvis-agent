# JARVIS Mac 菜单栏 App

正式工程：`JarvisMacApp.xcodeproj`，scheme `JarvisMacApp`。可启动产物：**`Output/JARVIS.app`**（Apple Development 签名、arm64、macOS 13+）。原骨架 `/Volumes/扩展硬盘/hermes/默认项目/JarvisMacApp` 与 `../Modules` 不删除、不原地修改。

## 使用

```sh
open '/Volumes/扩展硬盘/jarvis/apps/mac/JarvisMac/Output/JARVIS.app'
```

1. App 只在菜单栏显示，没有 Dock 主窗口；默认连接 `ws://127.0.0.1:8765`。
2. 菜单选择「开启麦克风与激活检测」，首次由 macOS 询问麦克风权限。拒绝时菜单显示如何在系统设置中启用。
3. **击掌两下或响指 → 聆听 → 说话 → 静音 1 秒 → 上传 → 播放回复。** 也可菜单点「开始说话」。新激活立即停止旧回复。
4. 菜单可独立开关击掌/响指、停止语音、重连、关闭麦克风、退出；唤醒词保持「未就绪（缺少模型）」且不可选，不拿能量检测伪装词义识别。
5. 关闭麦克风释放引擎；退出结束 socket/定时器/播放器。未安装开机自启，未修改用户网络配置。

真实语音仍需本机语音网关已启动并具备有效 ASR/TTS/brain 配置。本轮验收只连接**本地 Python 夹具网关**，未改动/重启生产服务、未调用云端 ASR/TTS/brain。若网关启用 Bearer 认证，本版本未提供凭据配置 UI，不能以无认证夹具通过代替生产认证验收。

## 接线

- `Sources/JarvisIntegration/MicrophoneCapture.swift`：复用录音模块的 **一个 AVAudioEngine / 一个 tap**，8 槽有界复制，在一条串行非实时 worker 上，**先把原始 Float32 44.1/48kHz 送 Activation，再转成 16kHz/mono/S16LE**。绝不从 16kHz 升采样冒充响指频段。硬件只提供低采样率时，8–12kHz 响指不可用。
- 激活后忽略 120ms 手势尾巴；RMS VAD（阈值 0.012、累计发声至少 120ms）静音 ≥1 秒结束；无发声 6 秒取消；最长 25 秒；仅激活话段存 PCM，不发待机音频。设备格式变化、缓冲溢出、3 秒没有输入显式停止并报错。授权期间的 stop 会阻止迟到授权重开引擎。
- `VoiceCoordinator.swift`：MainActor 状态机、完整话段 `sendAudio → audio_end`；先发送 `hello(device_type=mac)` 和 `device_state`，每 15 秒刷新。无法获取的物理状态传 null，不将前台伪装为亮屏/佩戴。收到 `suppressed` 清除当前录音与播放且不上传；`selected/resumed` 只从下一次完整激活恢复。
- `AudioPlayer` 聚合完整真实 MP3 后通过 AVAudioPlayer 解码播放；新激活 token 丢弃旧回复；只有 native drain 才发对应 `playback_done(generation)`，被打断旧代不补发完成。
- 菜单图标：待机 `waveform`、聆听 `mic.fill`、思考 `ellipsis.bubble`、说话 `speaker.wave.2.fill`、断线 `wifi.slash`。PlaybackMenuPresenter 接既有 status item，保留进度/停止/辅助功能入口，最终状态渲染由宿主控制。辅助功能是可选用户动作，不是录音/播放前提。
- Info.plist 有麦克风用途说明；签名 entitlement 含 App Sandbox、`device.audio-input`、`network.client`，不等于系统隐私已获批。

### 复制模块

| framework | 源码 |
|---|---|
| AudioRecorderModule | `Vendor/AudioRecorder/Sources/*.swift`（4） |
| JarvisActivation | `Vendor/Activation/Sources/JarvisActivation/*.swift`（6） |
| JarvisWebSocket | `Vendor/WebSocket/Sources/JarvisWebSocket/*.swift`（2）+ Starscream 4.0.8 |
| AudioAccessibilityModule | `Vendor/AudioAccessibility/*.swift`（3） |

集成仅在复制源码上做必要适配：公开共享输入/PCMConverter；输入新增保留 capture sample-time 的 `startTimed`；WebSocket 增加 hello/device_state 控制与 suppression 处理；AudioAccessibility XCTest 增加 framework import；另增 2 个 Mac 控制测试。原模块测试断言未删除/放宽。

历史骨架的 `Package.swift`/`Jarvis.xcodeproj`/旧脚本保留，但不是正式集成入口。

## 重建

```sh
cd '/Volumes/扩展硬盘/jarvis/apps/mac/JarvisMac'
/Volumes/扩展硬盘/jarvis/.venv/bin/python scripts/acceptance_e2e.py --prepare
GEM_HOME='/Volumes/扩展硬盘/hermes/默认项目/JarvisMacApp/.build/gems' \
GEM_PATH='/Volumes/扩展硬盘/hermes/默认项目/JarvisMacApp/.build/gems' \
  ruby scripts/generate_integrated.rb
python3 scripts/verify_integrated_project.py
python3 scripts/build_app.py
```

`build_app.py` 实跑 `env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild ... -derivedDataPath /tmp/jarvis-mac-final-derived DEVELOPMENT_TEAM=956UD85933 CODE_SIGN_STYLE=Automatic -allowProvisioningUpdates build`，再 `ditto` 交付到 `Output/JARVIS.app` 并验证完整签名/entitlements。TMPDIR/DerivedData 用 ASCII 避免本机 Swift driver trap。新增 integration Swift 后须重跑生成器。

## 验收复现

```sh
# 原模块全部测试 + 2 个集成控制回归；会调用原来的 AX 请求 API，但不替用户授权。
python3 scripts/verify_integrated_modules.py
# 确认 8765 未被生产网关占用；脚本遇到端口占用直接失败，不杀别的服务。
/Volumes/扩展硬盘/jarvis/.venv/bin/python scripts/acceptance_e2e.py
```

`AllModuleTests` 不跳过原来的 4 个 AudioAccessibility XCTest。`ModuleTests`/默认 App scheme 仍排除显式权限请求用例，避免普通调试触发授权 API。模块测试优先复用 `/tmp/jarvis-mac-final-derived/SourcePackages` 的精确 Starscream checkout，并禁自动更新；它不是绕过签名或修改代理，源缓存丢失时需先重新 resolve/build。

`acceptance_e2e.py` 使用 Activation 原有 `double-clap-2.wav` 原字节 + 明示的 0.8 秒合成谐波人声代理 + 静音，48kHz 回放经过同一个 detector/converter/VAD 路径；Python websockets 在 127.0.0.1:8765 开真实 WS，返回 ffmpeg 编码的 MP3。三场景：完整往返、播放中新激活打断、suppressed 零上传。App sandbox 无法任意读写外部路径，因此夹具作为 App bundle resource，验收 JSON 经 stdout 交给父进程落盘。测试钩子只有显式 `--replay-wav`/`--evidence-json` 参数才生效，不开麦。

**已验证范围**：签名 Xcode build、原模块回归、新控制回归、三种真实 App/WS/原生 MP3 播放时钟夹具、五态图标/菜单对象、1 秒静音实测。

**未验范围**：真实麦克风/真人击掌响指准确率、真实唤醒词模型、云端 ASR/TTS/brain、生产网关认证、多设备实际仲裁、可听音质/人工菜单栏视觉。验收音量为 0；真实 AVAudioPlayer 解码/drain 不等于人耳听辨。

证据各轮写入新的 `Evidence/<timestamp>-signed-build`、`*-project`、`*-e2e`，失败轮也保留；最新入口与计数见 `Evidence/DELIVERY.json` 和 `Evidence/REPORT.md`。
