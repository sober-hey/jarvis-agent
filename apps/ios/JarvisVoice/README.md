# JARVIS Voice — iPhone

原生 Swift + SwiftUI，最低 iOS 18，目标 iPhone 15 Pro。打开 `JarvisVoice.xcodeproj`，共享 scheme 为 `JarvisVoice`，无第三方 Swift 包 / CocoaPods / XcodeGen 依赖。

> **iOS 目标已编译验证（Xcode 27.1 / iOS SDK 27.1，关闭签名）。** iPhoneOS arm64 `xcodebuild build` 通过；iPhone 15 Pro / iOS 27.0 模拟器 `xcodebuild test` **8 tests / 0 failures**，并用 `simctl install/launch/get_app_container` 验证安装与启动。纯 Swift 核心另编译为 macOS Mach-O 执行 **40 checks / 0 failures**。Action Button、Vocal Shortcuts、后台与真实音频仍未验收；真机签名安装/启动的后继证据见下节。最初仅有 CLT 时的“未编译验证 iOS 目标”状态已被上述真实构建证据取代；见 `Evidence/xcode-27.1/`。

## 配套 Apple Watch App

本工程引用 `../../watchos/JarvisWatch/JarvisWatch.xcodeproj`；`JarvisVoice` target 依赖其 `JarvisWatch` target，通过 **Embed Watch Content** 复制到 `JarvisVoice.app/Watch/JarvisWatch.app`。不要另复制一份 Watch Swift 源码。运行 `python3 scripts/generate_project.py` 会保留该关系（须有同仓 Watch 工程）。

2026-10-20 本机已真实签名构建、安装到 iPhone 15 Pro 并启动，安装后设备读取确认进程在跑。完整证据：`../../watchos/JarvisWatch/Validation/20261020T105714Z-companion/REPORT.md`。仅证明 iPhone 安装/启动，不代表语音链路或手表硬件通过。

Sir 下一步：iPhone **Watch App → 可用 App → JarvisVoice（可能显示 JARVIS）→ 安装**。当前 Watch 描述文件 devices 只有 iPhone UDID，缺少手表 UDID，真表安装可能因此失败；也需遵循系统开发者模式要求。无需取消手表配对。

自检配套结构：`python3 scripts/test_companion_packaging.py`。改 iOS bundle ID 时必须同步 Watch bundle ID 前缀和 `WKCompanionAppBundleIdentifier`；两端营销版本及 build version 必须保持一致。

## 已写入的功能

- `StartJarvisIntent` + `AppShortcutsProvider`：可选文字参数；系统先打开 App，再开始用户可见的语音模式。可绑定 Action Button / Vocal Shortcuts / Siri 快捷指令。
- `AVAudioEngine` 连续收音 + Voice Processing；硬件采样率不强设为 16k，通过 `AVAudioConverter` 转 16kHz mono float，再编码 PCM16 little-endian。每帧 20ms，起音前保留 200ms，本地能量门检测起止；静音 600ms 完成一次语句，25s 硬上限。
- 后台 `.playAndRecord/.voiceChat` + `UIBackgroundModes=audio`。用户开始后，静音期间仍持续真实录音和 WebSocket ping，不播放静音文件作弊保活。结束按钮关闭录音、音频会话、状态采集和网络。
- JSON WebSocket PCM 上行 / MP3 下行；15s ping，1/2/4/8/15s 重连。FIFO 有界发送，不重发结果未知的文字/音频；断线中途的语句丢弃，下一次完整起音才恢复上送。
- `AVAudioPlayer` MP3 播放：先把同 generation 的 MP3 chunks 拼完整，`final=true` 才播放；**不是边下载边播放**。本地 VAD 起音立即停播，不等网络；旧 generation 后续音频丢弃。路由变化/来电中断处理。
- 电量、充电、音频输出路由、耳机连接、前台状态、UIDevice 面朝下观测，每 15s（App 获 CPU 时）及变化时上报。
- 专用网关 bearer token 可选，存 Keychain；模型 key 不应发给 App。明文 ws 仅允许 Tailscale `100.64.0.0/10` 和 loopback，其他目的地必须 wss。URL 不允许 user/password；ATS 的全局 cleartext 例外仅为 raw Tailscale IP，客户端端点策略另做限制。

## Xcode 打开、签名、安装（Sir 操作）

1. 从 Apple 安装**支持手机当前 iOS 版本**的完整 Xcode；最低源码 API 需要 Xcode 16 / iOS 18 SDK，但运行 iOS 27 的手机通常需要匹配的新 Xcode。首次启动完成 iOS 平台组件安装。
2. `open JarvisVoice.xcodeproj`。不必重建工程；如增加源码，执行 `python3 scripts/generate_project.py`。
3. Xcode → Settings → Accounts → 登录 Sir 的 Apple ID（这一步本人完成，不在聊天中发送密码或验证码）。
4. Target **JarvisVoice** → Signing & Capabilities：勾 Automatically manage signing；Team 选自己的 Personal Team / Developer Team；Bundle Identifier `app.jarvis.JarvisVoice` 改成自己的唯一标识。无付费 entitlement 依赖。免费签名通常需周期性重签，具体以 Xcode 提示为准。
5. 用 USB 连接 iPhone，手机点信任；设置 → 隐私与安全性 → 开发者模式，按系统提示开启/重启。必要时在 VPN 与设备管理里信任开发者。
6. 顶部 scheme `JarvisVoice`，destination 选真 iPhone（不是 My Mac）。Cmd+B 后 Cmd+R；按 Xcode 真实错误处理 SDK、代码或签名问题。**当前已有真机签名安装/启动证据，见配套 Apple Watch App 节；关闭签名的 iOS 编译与旧模拟器证据也保留。**
7. 首次打开 App，设置 → 填 `ws://<Mac 的 Tailscale IPv4>:8765`（例如 `ws://100.80.1.2:8765` 仅示例，不是 Sir 的实际 IP）。Mac 与 iPhone 都打开并登录同一 tailnet。`localhost` 指手机自己，真机不要用它连接 Mac。
8. 填网关访问令牌（如服务端要求，**不是 DashScope/OpenAI API key**），保存；点开始语音，允许麦克风和本地网络访问。Mac 网关须先运行并可从 tailnet 到达；服务端应只绑定 loopback/Tailscale，不向公网裸露。
9. Settings & Capabilities 的 Background Modes 应显示 Audio / AirPlay / Picture in Picture；本工程已在 Info.plist 写入 `audio`。iOS 没有可由 App 静默取得的“后台音频权限”对话框，能力声明 + 正在进行的合法录音才是后台路径。

## Action Button（左侧操作按钮，不是右侧电源/Siri 键）

1. 安装并手动打开 JARVIS 一次，保存连接设置，完成麦克风授权，确认前台可用。
2. 系统 **快捷指令** → `+` → 添加操作 → 搜索 JARVIS → **开始 JARVIS 语音**；“文字”留空，命名为“JARVIS 语音”。先点运行验证。
3. 系统 **设置 → 操作按钮 / Action Button → 快捷指令 → 选取快捷指令 → JARVIS 语音**。
4. 长按 iPhone 15 Pro 左侧 Action Button：系统打开 App 并进入语音。已经在语音中时不重复打开录音流。
5. 默认使用本机身份验证，锁屏可能先要求 Face ID/解锁；不能承诺锁屏免解锁冷启动录音。已经开始的录音可通过后台音频继续，仍须真机验证。

绑定是系统用户设置，App 不能替用户写系统偏好。右侧电源/Siri 键替换并非本工程功能，不依赖任何地区限定默认助手权限。

## Vocal Shortcuts（说 “Jarvis”）

1. 完成上面的快捷指令。
2. **设置 → 辅助功能 → 语音快捷指令 / Vocal Shortcuts → 设置/添加操作**。
3. 在操作列表选择快捷指令 **JARVIS 语音**（部分系统版本也会直接列出 App Intent）。
4. 自定义短语填 **Jarvis**，按系统提示说数遍，让 iOS 训练并开启该语音快捷指令。先退出设置，说 “Jarvis” 验证。
5. 如果没出现 JARVIS 操作，先运行一次快捷指令、重开设置；确认不是把它配置为仅打开 App。必须选择调用“开始 JARVIS 语音”的快捷指令。

这是 **iOS 辅助功能的本地短语检测 → 系统快捷指令 → App Intent**，不是本 App 在已被杀掉时自己常听，也不替代 Siri 的唤醒词。锁屏、系统麦克风抢占、语言/地区与系统具体行为需要真机验证；不承诺任何状态都能冷启动。系统 Vocal Shortcuts 与本 App 同时收音的兼容性尤其需要验收。

## 协议与服务端接线

音频消息与 `packaging/voice_gateway.py` 的 Mac 客户端协议相同；完整清单见 [PROTOCOL.md](PROTOCOL.md)。客户端不能运行独立人格/模型，服务端仍须接 `voice_port.py` BRAIN 模式 / 唯一时间线。

**发现的服务端缺口：** 当前网关 `receive()` 只处理 `audio` / `audio_end` / `playback_done`，尚未处理本 App 所需 `hello` / `device_state` / `text`；它们会得到 `invalid_audio`。因此基础音频帧对齐不等于文字参数和设备协商端到端已接通。合并方须在网关补三类消息，测试后才能把对应功能报成可用。此工程没有越权修改正在并行开发的网关文件。

## 公开 API 的硬边界

| 请求 | 本工程实际观测 | 不冒充的能力 |
|---|---|---|
| 电量 | batteryLevel / batteryState；未知为 null | 不把 -1 报成 0 |
| 戴耳机 | 当前音频输出含有线/蓝牙耳机路由 | **不能知道物理佩戴**，`headphones_worn=null` |
| 屏幕亮 | 前台活跃时 `screen_on=true`；其余 null | **无公开全局 screen-on / lock API**，后台不等于熄屏 |
| 面朝下 | UIDevice 最近 orientation 及观测来源 | 未知为 null；后台更新时效不保证 |
| 后台 | 用户开启的真实连续录音允许继续 | 不保证被强制退出/重启后继续，不保证永久后台网络 |
| 打断 | 20ms 能量帧检测起音后主线程立即 stop | 能量门不是 ML VAD，噪声会误触，AEC/扬声器回声需真机测 |

录音不落盘，不建立离线待发队列。麦克风音频发往指定网关，后续 ASR/大脑/TTS 云调用及服务端日志保留政策需在服务端另行设置。后台连续录音耗电；首次请短时测试，不能承诺全天续航。AVAudioPlayer 的回声路径是否被当前设备 Voice Processing 充分消除必须实测，不能以“设置了 voiceChat”当成效果证据。

## 本次可重跑验收

```sh
cd apps/ios/JarvisVoice
python3 scripts/test_core.py
```

- 核心源为 `Core/AudioPrimitives.swift` / `Core/VoiceCore.swift` / `Core/DeviceSnapshot.swift`，只依赖 Foundation/CoreFoundation，不依赖 UIKit。
- **真实编译并执行 macOS Mach-O**，覆盖 JSON/PCM 编码、VAD 起止/静音、预滚、端点安全策略、MP3 拼接与代际打断、断网不发半句、设备状态 unknown 语义。当前为 40 个断言。
- 该 Mac 默认 `swiftc` driver 的 `--version` 会 SIGTRAP；脚本自动降级到同一个 Apple 编译器 `swiftc -frontend -emit-object` 再由 clang 链接，不改工具链、不伪造测试结果。
- 额外 macOS typecheck：`MP3Playback.swift`、`VoiceSocket.swift`、`GatewayCredential.swift` 及 Core；全部 Swift 做语法 parse；plist/pbxproj 用 plutil lint。
- 脚本把命令、返回码、stdout/stderr、二进制路径写入新 TMPDIR evidence 目录。仅证明这些源在 macOS 上的部分，不构成 iOS framework 编译或硬件测试。

### iOS 目标与模拟器测试（本次已运行）

本机 Swift driver 遇到包含中文的 TMPDIR 会 SIGTRAP；不能把它当成源代码错误。实测使用 ASCII 路径别名解决，临时实物仍在 Hermes scratch：`/Users/sober/.jarvis-ios-xcode-tmp` → `/Volumes/扩展硬盘/hermes/.hermes/cache/scratch/jarvis-ios-xcode`。以下路径是本次机器的可重跑命令；换机器时设自己的 ASCII TMPDIR/DerivedData。

```sh
env TMPDIR=/Users/sober/.jarvis-ios-xcode-tmp \
  DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -project JarvisVoice.xcodeproj -scheme JarvisVoice \
  -sdk iphoneos27.1 -destination 'generic/platform=iOS' \
  -derivedDataPath /Users/sober/.jarvis-ios-xcode-tmp/device-dd \
  CODE_SIGNING_ALLOWED=NO build

env TMPDIR=/Users/sober/.jarvis-ios-xcode-tmp \
  DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcrun simctl boot C88394F5-78F5-45BE-96E9-D6CB1B177EB1

env TMPDIR=/Users/sober/.jarvis-ios-xcode-tmp \
  DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -project JarvisVoice.xcodeproj -scheme JarvisVoice \
  -sdk iphonesimulator27.1 \
  -destination 'platform=iOS Simulator,id=C88394F5-78F5-45BE-96E9-D6CB1B177EB1' \
  -derivedDataPath /Users/sober/.jarvis-ios-xcode-tmp/simulator-dd \
  CODE_SIGNING_ALLOWED=NO test
```

`Tests/JarvisVoiceTests.swift` 的 8 个 XCTest 在真实模拟器 App host 内执行：协议/PCM、VAD、unknown 状态、MP3 代际、端点隐私策略、断网不发半句、畸形数据、Intent 前台与 App 空闲状态/Info.plist 声明。它们不是 UI 自动点击或硬件音频验收。SDK 27.1 自带的 27.1 runtime 只支持 iPhone Duo，因此 iPhone 15 Pro 测试用下载的 iOS 27.0 runtime（24A434），仍使用 27.1 SDK 编译。

### iPhone 15 Pro 真机验收清单（仅签名安装与启动已验）

- [x] 真 iOS 目标签名编译、Personal Team 安装与启动；配套验证报告有 devicectl 读回。
- [ ] 拒绝/允许麦克风、保存有效/无效网关 URL、令牌空值删除。
- [ ] 前台录音：真实硬件输入 44.1/48k 也得到实际 16k mono PCM16；ASR/唯一时间线回复/MP3 能听见。
- [ ] 左侧 Action Button 长按，前台/锁屏分别测；Intent 文字参数端到端只提交一次。
- [ ] 系统训练“Jarvis”后，从桌面/其他 App/锁屏分别触发，记录解锁与麦克风争用。
- [ ] 播放中说话：真实 stop 延迟与尾音；安静/噪声/扬声器与耳机，测误打断而非只测回调。
- [ ] 切后台/锁屏连续静音 ≥5min，之后能说话；观察 CPU/麦克风指示/电量。不用假静音音轨。
- [ ] 来电/Siri 抢占、耳机拔插/蓝牙切换、网络断开重连，不重发半句、不重播旧 generation。
- [ ] 服务端逐条收到 hello/device_state；电量、耳机、前台、faceDown 和 unknown 语义正确，多设备 selected/suppressed 协商一次只有一台出声。
- [ ] 结束语音立刻关闭麦克风、播放与连接；强退/重启后不会偷偷自动录音。

## Apple 官方参考

- [App Intents](https://developer.apple.com/documentation/appintents/creating-your-first-app-intent)
- [openAppWhenRun（兼容 iOS 18；新版 SDK 会提示改用 supportedModes）](https://developer.apple.com/documentation/appintents/appintent/openappwhenrun)
- [Vocal Shortcuts](https://support.apple.com/guide/iphone/use-vocal-shortcuts-iph0d9d67065/ios)
- [Action Button](https://support.apple.com/guide/iphone/use-and-customize-the-action-button-iphe89d61d66/ios)
- [后台音频 UIBackgroundModes](https://developer.apple.com/documentation/bundleresources/information-property-list/uibackgroundmodes)
- [Voice Processing](https://developer.apple.com/documentation/avfaudio/using-voice-processing)

本工程使用普通前台 `AppIntent`，而非 `AudioRecordingIntent`，不声称系统后台录音 Intent 的 Live Activity 集成；如后续改用后者，需另实现并持续维护相应 Live Activity，不能只改协议名。
