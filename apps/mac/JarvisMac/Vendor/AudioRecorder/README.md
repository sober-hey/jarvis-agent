# AudioRecorder — 独立麦克风录音模块

只依赖 AVFoundation / Foundation / Darwin，不引用菜单栏、网络、唤醒或其他 JARVIS 模块。**未修改 JarvisMacApp 工程骨架。** 最低 macOS 13，Swift 5 language mode。

## 交付文件

- `Sources/AudioRecorder.swift`：权限、状态机、start/stop/pause/resume、文件输出、音量/PCM 回调、超时。
- `Sources/MicrophoneInput.swift`：AVAudioEngine tap、预分配的 8 槽缓冲池、音频设备变化检测。
- `Sources/PCMConverter.swift`：持续 AVAudioConverter 重采样/单声道混音/16-bit 整数编码。
- `Sources/PCMBufferRing.swift`：3–10 秒定长环形缓冲。
- `CLI/main.swift`、`Tests/main.swift`：真实麦克风 CLI 与不访问设备的注入式测试。
- `AudioRecorder.xcodeproj`：独立 CLI/测试 harness，不是菜单栏 App。
- `Resources/Info.plist`：麦克风用途描述；`Resources/AudioInput.entitlements`：供宿主集成参考。
- `Evidence/`：真实运行日志、PCM、ffplay/ffprobe 和编译测试证据。

## 构建与运行

在本模块目录执行：

```sh
env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -project AudioRecorder.xcodeproj -alltargets -configuration Debug \
  SYMROOT="$PWD/.xcodebuild" CODE_SIGNING_ALLOWED=NO build
.xcodebuild/Debug/AudioRecorderTests
.xcodebuild/Debug/AudioRecorderCLI --seconds 10 --output "$PWD/recording.pcm"
```

也可 `python3 scripts/build.py test && .build/audio-tests`，或 `python3 scripts/build.py cli`。该脚本为 Swift 编译子进程显式设置 `TMPDIR=/tmp` 与完整 Xcode 路径，以避开本机含中文 TMPDIR 导致的 Swift driver trap；不会全局修改环境。

```sh
ffplay -f s16le -ar 16000 -ch_layout mono -autoexit recording.pcm
ffprobe -v error -f s16le -ar 16000 -ac 1 -show_streams -show_format recording.pcm
```

RAW PCM 没有头部，格式必须由参数告知播放器；不能仅凭任意裸文件自动推断采样率。CLI 以真实采集样本数到达目标时停止，去掉 tap 边界多出的尾部，**不补静音**。已有 PCM 文件会以 O_EXCL 拒绝，不覆盖。音频文件权限 0600。

## 宿主接入

将 `Sources/` 的四个 Swift 文件加入目标；**不要把 CLI/main.swift 或 Tests/main.swift 加到菜单栏 App**。宿主 Info.plist 必须包含 `NSMicrophoneUsageDescription`；开启 sandbox 或 hardened runtime 时加入 `com.apple.security.device.audio-input=true`。系统首次弹权限框，拒绝时通过错误返回并引导用户到系统设置，模块不尝试重置 TCC。

```swift
private var recorder: AudioRecorder?

func beginRecording() throws {
    recorder = AudioRecorder(
        configuration: try .init(bufferDuration: 10, inputTimeout: 3),
        callbacks: .init(
            onVolume: { rms in /* 更新指示灯，范围 0...1 */ },
            onPCM: { pcm in /* 可选：最新分块，s16le/16kHz/mono */ },
            onState: { state in print(state.rawValue) },
            onError: { error in print(error.localizedDescription) }
        )
    )
    recorder?.start { result in print(result) }
}
// recorder?.pause(); recorder?.resume()
// recorder?.stop { result in /* result.get().pcm 是最新 3–10 秒，不是无限录音 */ }
```

保持 recorder 的强引用直到 stop 回调结束。默认回调在主队列；接口可从任意队列调用，但**不要阻塞回调队列等待结果**。`currentState`/`bufferedPCM` 也通过异步回调读取。

## 行为与边界

- 输出固定 16,000 Hz、单声道、signed 16-bit little-endian。
- 环形缓冲只留最新配置时长；持续完整录音请给 `start(outputURL:)`。`Recording.sampleCount` 是整个会话的样本数，可能大于 `pcm.count/2`。
- pause 排除暂停期间样本；引擎保持打开以便即时 resume，暂停期间不会存储 PCM。**pause 不是释放麦克风**，要撤掉系统麦克风指示请 stop。
- RMS 是线性幅度，不是响度百分比。静音时允许为 0；没有输入回调才触发超时。
- tap 只做有界内存拷贝与派发；转换/文件写入在独立串行处理队列。8 个预分配槽全部占用时显式 overrun 并停止，不悄悄丢帧。使用 Dispatch/ARC，因此不是硬实时认证实现。
- 音量与可选 onPCM 通知合并以防主队列阻塞时无界积压；onPCM 最多保留最新 10 秒，**不是无损录音 API**。无损用 outputURL。
- 输入设备/格式变化采取 fail-fast，不自动切到其他麦克风继续录；调用方可提示并重新 start。
- 权限弹窗期间 stop：start/stop 均返回 cancelled，不产生可被误认为录音的成功结果。迟到的授权回调不能重启。
- 测试模拟了权限拒绝、超时、设备变化、取消、暂停恢复。没有实际拔插外置麦克风，未做人工听辨验收。ffplay 验证静音输出播放至自然结束，证明解码与播放链路成功，不表示主观音质已验收。
- 无网络上传。`Evidence/*.pcm` 是真实本机音频，不应直接提交公开仓库。
