# Audio + Accessibility module

独立 Swift 源码；未修改 `JarvisMacApp` 骨架。将本目录三个顶层 `.swift` 文件加入最终 macOS App target。全部 API 在 `@MainActor` 使用，无启动时录音/播放/权限请求，无第三方运行依赖。链接系统 Foundation、AppKit、AVFoundation、Combine、ApplicationServices、Carbon。

## Xcode 27.1 验证（最新）

独立 `AudioAccessibility.xcodeproj`，没有改主 App 骨架。完整 Xcode/macOS 27.0 SDK 已实测 `xcodebuild test` 成功，4 tests / 0 failures。`testExplicitSystemPermissionPrompt` 真实调用系统权限申请 API，返回 `false`；不代表弹窗可见性已目验或授权已获批。

关键修正：先前 swiftc SIGTRAP 根因是 TMPDIR 含非 ASCII 字符。已创建 `/Users/sober/.cache/jarvis-swift-tmp` ASCII 符号链接，指向规定 scratch 内的 `audio-accessibility-ascii-tmp`，避免把临时内容写到系统临时目录；该 TMPDIR 下完整 swiftc 正常。

```sh
env TMPDIR=/Users/sober/.cache/jarvis-swift-tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -project /Volumes/扩展硬盘/jarvis/apps/mac/Modules/AudioAccessibility/AudioAccessibility.xcodeproj \
  -scheme AudioAccessibility -destination 'platform=macOS,arch=arm64' \
  -derivedDataPath /Volumes/扩展硬盘/hermes/.hermes/cache/scratch/audio-accessibility-xcode-derived \
  CODE_SIGNING_ALLOWED=NO test
```

结果包：`/Volumes/扩展硬盘/hermes/.hermes/cache/scratch/audio-accessibility-xcode-first.xcresult`。

## 音频与新激活打断

```swift
let audio = AudioPlayer()
audio.volume = 0.8 // clamps 0...1; NaN -> 0
let token = audio.activate() // wake handler 立即执行，停止旧声音
// Existing transport fetches an entire MP3/WAV response asynchronously.
// On MainActor after obtaining response Data:
try audio.play(data: responseData, activation: token)
// Late old-token responses are ignored without stopping newer audio.
audio.onProgress = { progress in
    // phase, currentTime, duration, fraction, errorMessage
}
// pause / resume / seek / stop also provided.
```

`AVAudioPlayer(data:)` 接受完整 MP3/WAV 数据，不直接接收 raw PCM 帧，也不把 WebSocket 每个 binary frame 当独立 MP3。传输层须聚合一个完整编码音频响应并保留 activation token。此版本不宣称与骨架的 PCM `AudioPlaybackService.enqueue` 直接兼容；若正式协议采用裸 PCM streaming，后续需独立 AVAudioEngine adapter，不要加假桥接。

限制：32 MiB/clip；此入口做同步有界解码，本机 1s 夹具已验证，不是大文件低延迟流播放器。文件入口只接受调用方已信任的本地 URL；无网络下载器。停止立即释放 native player/timer，旧 delegate completion 用 playbackID 丢弃。每 100ms 在主 RunLoop common modes 发布进度（菜单打开时仍可更新）。出错含中文诊断。

## 菜单栏接线（不改骨架）

```swift
let accessibility = AccessibilityHelper()
let presenter = PlaybackMenuPresenter(player: audio, accessibility: accessibility)
// Existing StatusBarController.install() already created statusItem:
presenter.attach(to: statusItem)
if let menu = statusItem.menu { presenter.appendControls(to: menu) }
// retain presenter in AppDelegate/integration owner
// on app teardown:
presenter.detach()
audio.stop()
```

不创建第二个 status item。按钮显示 `▶ 25%`，菜单显示经过/总时长、停止按钮、辅助功能授权/设置入口与未授权说明。宿主每次重建菜单后需重新 appendControls；宿主自己的 refresh 会清空按钮标题，集成时需把进度更新作为渲染的一部分或在 refresh 后重新挂载。退出前 detach。界面文案/权限拒绝菜单已在 AppKit 测试读取；**未接进最终骨架 App，菜单栏真实视觉效果待主脑集成验收**。

## 可重复测试

从指定工作目录运行：

```sh
/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3 /Volumes/扩展硬盘/jarvis/apps/mac/Modules/AudioAccessibility/run_checks.py
```

脚本用 ffmpeg 本地生成 1s 正弦 MP3/WAV 夹具（测试音量 0，不干扰用户），无网络/密钥；优先完整 Xcode swiftc，固定 ASCII TMPDIR，编译 Swift 6 `-warnings-as-errors` 后执行两个原生二进制。最早 CLT frontend 的运行证据亦保留，但不再把驱动 SIGTRAP 误判为工具链不可用。完整 xcodebuild test 结果见文首；没有安装任何工具。

实测覆盖真实 AVAudioPlayer MP3/WAV 解码、开始/进度/完成、音量边界、暂停继续/seek、激活打断与旧响应丢弃、坏数据/文件失败、停止后无 tick、菜单文案与拒权提示，以及 AX 目标/Unicode/安全输入/拒权分支。测试结果与证据位置由脚本输出 `SUMMARY`/`EVIDENCE`。

边界：MP3 是本地生成的真实编码字节，模拟 transport 返回 Data；尚无真实服务端 TTS 回包 E2E。测试音量为零，只证明 native playback clock/completion，不证明用户听到声音。`AXIsProcessTrusted=false` 是系统预期拒绝；实际窗口标题/输入/点击须 Sir 手动授权后真机验收。详见 `docs/ACCESSIBILITY.md`。
