# JARVIS Watch

配套 iPhone 的 SwiftUI Watch App，最低 watchOS 11，目标 Apple Watch Series 8。打包安装经 JarvisVoice iOS App 的 `Watch/JarvisWatch.app`，运行时仍由 Watch 自己可用的 Wi-Fi/蜂窝网络连接网关；没有实现 WatchConnectivity/iPhone 网络转发分支。

## 经 iPhone 安装（当前路径）

- 在 `apps/ios/JarvisVoice/` 打开 `JarvisVoice.xcodeproj`；该工程引用现有 Watch 工程，通过 target dependency 与 **Embed Watch Content** 自动构建/嵌入，不复制 Watch 源码。
- Watch bundle ID 为 `app.jarvis.JarvisVoice.watchkitapp`，`WKCompanionAppBundleIdentifier=app.jarvis.JarvisVoice`、`WKApplication=YES`；不再声明 `WKWatchOnly`。修改 iOS bundle ID 时必须同步这两个标识。
- 先装更新后的 iPhone App，再在 iPhone **Watch App → 可用 App → JarvisVoice（可能显示为 JARVIS）→ 安装**，不要求 Xcode 先发现手表，不需要取消配对。
- 当前签名构建、iPhone 安装与启动已有真实证据：`Validation/20261020T105714Z-companion/REPORT.md`。**Watch 描述文件 devices 目前只有 iPhone UDID，没有手表 UDID**；手表端安装仍未验收，若失败首先检查签名授权设备/Developer Mode，而非宣称嵌入成功就等于真表安装成功。
- 重建工程顺序：先 `python3 Scripts/generate_project.py`，再运行 iOS 的 `python3 scripts/generate_project.py`；两边生成器均保存配套关系。最低部署目标继续固定 11.0。

**协议已改为服务端 voice_gateway 与 iOS 共用的 hello / device_state / audio pcm_s16le / audio_end；下行支持 text / MP3 分片 / interrupt / suppressed。** 详见 [PROTOCOL.md](PROTOCOL.md)。已实跑 watchOS 27.0 unsigned build、Series 12 模拟器 XCTest 与相同 Swift 核心/连接层的 Python websockets loopback，证据在 `Validation/`。真机签名/安装、Series 8 麦克风/扬声器/后台/无线网络与真实 BRAIN 端到端仍未验收。网页 HTTP 地址不是语音 WebSocket 地址。

## 工程

打开 `JarvisWatch.xcodeproj`（共享 scheme `JarvisWatch`），不依赖 XcodeGen、Pods、第三方 Swift 包。`Scripts/generate_project.py` 可重建同一工程；只生成工程，不替代构建。

- `Core/WatchCore.swift`：纯 Swift + Foundation，无 CoreMotion/WatchKit。击掌、抬腕近似、协议校验。
- `App/MotionMonitor.swift`：100Hz raw accelerometer、25Hz device motion、20s 本机轨迹采集。
- `App/WatchAudio.swift`：AVAudioRecorder，16kHz/mono/16bit little-endian PCM WAV；AVAudioPlayer 短回复。
- `App/WatchConnection.swift`：URLSessionWebSocketTask、心跳、单轮关联、30s 超时、Keychain 令牌。
- `App/WorkoutRuntime.swift`：显式启动/停止 HKWorkoutSession、心率时间弱证据、用户主动佩戴认证。
- `App/JarvisWatchApp.swift`：界面、触发协调、隐私开关、设备状态上报。
- `Tests`：watchOS XCTest + 同一套 macOS 可编译回放测试。

## 击掌算法

1. 使用原始三轴加速度的模长（含重力，CoreMotion 单位 g），严格 `> 2.5g` 算一次冲击。
2. 必须先回落到 `< 1.8g` 才重装，宽脉冲不会重复计数。
3. 第二次冲击距第一次 **200–600ms（含端点）** 触发。早于 200ms 的回弹忽略；超过 600ms 的冲击作为新一对的第一个冲击。
4. 触发后冷却 1.2s；重复/倒序时间戳断开待配对冲击，非有限数值丢弃。停止监听清空状态。

这是“两次冲击”探测器，不是掌声语义分类器。撞桌、跑步等也可能产生同型波形；100Hz 采样可能漏窄峰；阈值未经 Sir/Series 8 标定。不把他人 Android 手表回放当成 Series 8 灵敏度/误报率证据。

## 抬腕与佩戴：公开 API 的真实限制

**`WKInterfaceDevice` 没有公开的 `isWristDetectEnabled`，也没有物理 wrist-raise 回调。不能实现任务描述中的原样 API，不能用私有 API 补齐。** 本工程使用：

- SwiftUI `scenePhase -> active` 作“重新亮屏/回到前台”近似，可能由抬腕、点屏、Digital Crown 等造成；首次打开不自动录音。
- app 获 CPU 时用重力转向表面朝上 + 150ms 稳定作运动近似；不是系统抬腕算法，对佩戴方向敏感。
- 两条路径均须用户先开启监听、打开“抬腕后录音”并连接网关；震动提示后录音。默认关闭自动抬腕，点按说话可随时使用。
- `on_wrist`、`wrist_detection_enabled` 上报 **null**，不伪报真实布尔值。当前心率样本 `<30s` 只报 `recent_heart_rate_suggests_worn`，取样延迟/传感器误判/权限拒绝均可导致 unknown。
- “验证此刻佩戴”显式调用 LocalAuthentication 的 `deviceOwnerAuthenticationWithWristDetection`。成功仅证明**该时间点**通过佩戴认证；失败可能是未解锁、没设密码、用户取消或未佩戴，统一 unknown，不能等同未佩戴。
- 状态只上传心率样本时间，不上传心率数值；每 15s（获 CPU 时）以及连接/状态变化时上报电量、充电、抬腕时间/来源、传感器/录音/训练状态。

Apple 参考：
- [WKInterfaceDevice](https://developer.apple.com/documentation/watchkit/wkinterfacedevice)
- [Apple DTS：无实时佩戴公开 API](https://developer.apple.com/forums/thread/811405)
- [LocalAuthentication 佩戴认证](https://developer.apple.com/documentation/localauthentication/lapolicy/deviceownerauthenticationwithwristdetection)

## 后台运行与电量

**不是常驻服务，不能承诺 24×7 击掌唤醒。** 系统挂起/杀死 App 后，CoreMotion、WebSocket、15s 状态心跳都不会继续工作，也不能由击掌重新拉起 App。

- 普通模式：只有 App 活跃或真实录音进行时监听；退至后台/不活跃、且无训练/录音时停止传感器、断开 WebSocket；回前台须手动重连。
- “体能训练期间后台监听”：必须用户显式确认，在真实训练时启动 `HKWorkoutSession(.other)`；声明 `WKBackgroundModes=workout-processing` 和 HealthKit entitlement，结束监听立即停止。**不得为聊天助手伪造训练滥用保活；App Store 审核可能不允许本用途。** 不自动落库训练记录，不干预其他 App 的训练；若启动失败/被另一训练打断则停止。
- `UIBackgroundModes=audio` 仅用于真实录音/播放，不播放静音保活；无声 4s 取消，已检测声音后静音 1.1s 停止并发送，硬上限 12s。能量门不等于真正 VAD，环境声可能触发发送。
- 录音采用近场逐轮模式，**不是全双工流式语音**。回复播放期间新触发会停播放器再录音；不跨轮并行，不重发结果未知的录音。
- 100Hz 加速度、训练与网络均耗电，需真实测量续航；UI 无固定全天承诺。手表锁定、来电、Siri、权限变化、系统资源限制可能中断。
- Series 8 使用 `.playback/.default` 路由做短语音回复，显示实际输出路由；不调用 iOS-only `overrideOutputAudioPort`、不强行设 `.defaultToSpeaker`。系统可能选择耳机或拒绝当前路由。**未验证 Series 8 扬声器；长音频后台播放需要 `.longFormAudio`/蓝牙，不保证抬腕放下后短回复仍从内置扬声器播完。**

参考：[workout sessions](https://developer.apple.com/documentation/healthkit/running-workout-sessions)、[background audio](https://developer.apple.com/documentation/watchkit/playing-background-audio)。

## Sir 用 Xcode 签名安装

1. 使用支持 watchOS 11+ 的完整 Xcode（本轮已安装 Xcode 27.1、watchOS 27.0 SDK），首次启动完成组件安装；新机器可在 Settings → Platforms/Components 安装 watchOS 支持。
2. iPhone 与 Series 8 配对，升级到 watchOS 11+；按系统提示在手表 Privacy & Security 开启 Developer Mode（可能需先与 Xcode 配对），重启并确认。Mac/Xcode 与设备须能配对连接。
3. Xcode → Settings → Accounts 登录 Sir 的 Apple ID。打开此目录的 `JarvisWatch.xcodeproj`。
4. 选 **JarvisWatch** target → Signing & Capabilities：启用 Automatically manage signing，选择自己的 Team，将 `app.jarvis.JarvisVoice.watchkitapp` 与 iOS companion 标识同步修改；测试 target 同步改为唯一 `.tests`。确认 HealthKit、Background Modes 中 Workout processing/Audio 已启用。
5. **HealthKit entitlement 可能需要可用的付费开发者 Team/合适的 provisioning profile。** Personal Team 不支持时，不能把授权失败说成已支持：使用支持 HealthKit 的 Team，或明确移除训练能力与 entitlement 后仅前台构建（功能随之降级）。
6. 顶部 scheme 选 `JarvisWatch`，destination 选真实配对手表（不是 My Mac）；Cmd+B 编译，解决 Xcode 实际报告的签名/SDK问题后 Cmd+R 安装。首次允许麦克风/运动；训练模式另需健康权限。当前尚无此步成功证据。
7. Watch “连接设置”填 Sir 自有网关的 **wss://** 地址与专用短权限 token（不是云模型 API key）。URL 不能带用户名、密码、查询参数或片段，令牌独立保存在 Watch Keychain。保存空令牌会删除。
8. 点连接 → 开始监听 → 两次击掌（200–600ms）或点按说话。先前台验收，再自愿启用抬腕近似，最后在真实训练中测后台；停止监听同时停止训练、麦克风、播放、传感器并断开网络。

## 服务端协议（与 iOS / voice_gateway 对齐）

见 [PROTOCOL.md](PROTOCOL.md) 的字段、大小限制、打断/抑制语义与验收命令。使用 `wss://` 地址和 Keychain 网关令牌；仅本机夹具可使用 loopback `ws://`。默认不连接，不接受 URL 内令牌，拒绝重定向。

上行 `hello` 和 `device_state` 带稳定 `watch-<UUID>`、`device_type=watch`、电量与 5s 内击掌标记；佩戴状态 unavailable 保持 null。录音仍落临时 WAV，但解析 RIFF 后只发送 16kHz/mono/PCM16 裸数据，按 640 字节/20ms 帧发送并以 `audio_end` 结束。下行 text 与 MP3 分片按 generation 处理，final 后完整播放，播放结束回 `playback_done`；interrupt/本地 onset 立即停播，suppressed 取消录音和转发，selected/resumed 只允许下一完整 onset。

录音临时文件完成/取消即删除，无离线待发队列、无自动重连/重发。本机运动 CSV 不自动上传。服务端 BRAIN/鉴权/设备仲裁另行验收，不由 Watch 端协议测试推定上线。

## 测试

运行 macOS 核心/网络验收（日志、退出码、源码 SHA-256 与明确未验项写入新建 `Validation/<UTC时间>/RESULTS.json`；不覆盖历史；watchOS 构建/模拟器由下文独立脚本执行）：

```sh
仓库根/.venv/bin/python3 Scripts/verify.py
```

### 当前可运行：macOS 纯 Swift 编译 + 真实记录回放

```sh
cd apps/watchos/JarvisWatch
python3 Scripts/test_core.py
```

测试脚本使用 macOS SDK，把 `Core/WatchCore.swift`、共享 suite 与 `Tests/main.swift` 编译为本机 Mach-O 再执行。全部 Swift 构建必须使用 `env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`，DerivedData 在 `/tmp`。**不是 Python 复刻算法，不是只做语法检查。** 共享 suite 覆盖击掌/抬腕边界、WAV→裸 PCM、hello/state、MP3 分片/代际/打断/抑制/体积，以及真实录制回放；断言数以当次日志为准。

`Tests/Fixtures/wisdm_1600_clapping_excerpt.txt` 是公开 WISDM 被试 1600 的前 800 行 clapping（O）手表原始加速度，来源/固定 commit/原文件 SHA-256 在 `PROVENANCE.json`。原始 m/s² 除以 9.80665 得 g、纳秒归零变秒。这段约 40s 的真实 Android 手表记录检测到 1 对合规冲击，最后触发 39.12006018s；仅锁定回放行为，没有全部拍手的逐事件人工标注，**不报告召回率**。引用：Weiss, G. (2019), DOI [10.24432/C5HK59](https://doi.org/10.24432/C5HK59)，UCI 数据集 CC BY 4.0。其他边界向量明确为 synthetic，不冒充录制数据。

## macOS 网络集成测试（已实际运行，不等于 Watch 无线网络验收）

```sh
仓库根/.venv/bin/python3 Scripts/test_connection.py
```

依赖 Python `websockets`（项目现有 `.venv` 已有）；测试将相同的 `Core/WatchCore.swift` + `App/WatchConnection.swift` 用完整 Xcode `swiftc` 编译为 macOS 可执行程序，并真实通过 loopback URLSessionWebSocketTask 完成 hello → device_state → 裸 PCM/audio_end → text/MP3 → playback_done → interrupt/suppressed/selected → 新完整一轮。服务器仅为协议夹具；ffmpeg 生成有效的 synthetic MP3，Python 核对 PCM 与 bearer fixture，Swift 核对组装字节；不访问生产/模型/真实 Keychain，不等于网关已接 BRAIN。

### 完整 Xcode：watchOS 构建与 XCTest

本轮 unsigned device build、simulator build-for-testing 与实际 XCTest 均通过；物理 Watch 签名仍需 Sir 的 Apple ID/Team。Xcode 中可选 Watch Simulator/真机，Product → Test。命令行必须给 Swift driver ASCII 的 TMPDIR 和 DerivedData（当前验收固定 `TMPDIR=/tmp`、DerivedData `/tmp`、`CODE_SIGNING_ALLOWED=NO`）：

```sh
env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -project JarvisWatch.xcodeproj -scheme JarvisWatch -sdk watchos27.0 \
  -derivedDataPath /tmp/jarvis-watch-native-dd CODE_SIGNING_ALLOWED=NO build

env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -project JarvisWatch.xcodeproj -scheme JarvisWatch -sdk watchsimulator27.0 \
  -derivedDataPath /tmp/jarvis-watch-native-dd \
  -destination 'platform=watchOS Simulator,id=<已创建模拟器 UUID>' CODE_SIGNING_ALLOWED=NO test
```

须先有 `simctl list runtimes` 可见的 watchOS runtime，SDK 本身不包含可启动的 runtime；本轮按主脑指令安装 watchOS 27.0 Simulator。默认空 simulator 列表不能当测试通过。watchOS 27.0 拒绝创建 Series 8 设备，使用 JarvisWatch-Series12（`54D78C1B-FFC9-40B8-8DE5-BC1B5E822B66`），因此不能把模拟器结果当 Series 8 硬件证据。

可复跑的完整构建/测试并落盘命令：

```sh
仓库根/.venv/bin/python3 Scripts/verify_watchos.py \
  --simulator-id 54D78C1B-FFC9-40B8-8DE5-BC1B5E822B66
```

首次模拟器安装暴露 `WKWatchOnly` 与 `WKRunsIndependentlyOfCompanionApp` 不能同时定义，历史修复曾仅保留 `WKWatchOnly`。当前已改为配套 App，移除 `WKWatchOnly` 并设置 `WKCompanionAppBundleIdentifier`；历史失败/通过证据不覆盖，后续运行日志和 xcresult summary 为准。

### Series 8 真机验收清单（均待做）

- 实测 10 组双击掌与单掌/走路/跑步/敲桌负例；记录漏检/误报，不让手表损伤或用过大冲击。
- 麦克风拒绝、4s 无声取消、手动取消不发、12s 上限、录音实际 16000Hz、来电/系统中断。
- 内置扬声器实际输出、蓝牙切换、静音、放下手腕；不能用 Mac speaker 验证替代。
- iPhone 断开时 Watch Wi-Fi/蜂窝直连 wss，网关超时/断网/乱序回复不重复请求。
- 抬腕近似/点屏/首次打开区分、左/右腕方向，真佩戴/摘下/未解锁时的 unknown。
- 训练开始/拒绝/另一训练介入/结束监听、应用退后台的传感器和网络行为、30min 电量。
- 本机运动 CSV：Xcode → Devices and Simulators → Watch App → Download Container，取 Documents/MotionCaptures；人工标注 clap 时间后另增 Series 8 回放夹具，不覆盖 WISDM 原始数据。

尚缺：真机签名/安装、Series 8 回放/误报标定、真实网关 BRAIN 模式端到端、实际扬声器/后台/续航验证。本工程不是已上线产品；unsigned 编译与模拟器通过不代表已经装到 Sir 手上。
