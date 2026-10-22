# JARVIS 激活模块（③）

**状态：工程实现与合成 WAV 回放通过；真实人声/击掌/响指验收未完成。**

不改 App 骨架、不创建第二个麦克风 tap。模块只消费现有采集器复制出来的音频。

## 交付

- `WakeWordDetector.swift`：本地唤醒检测、≥0.8 概率门槛、独立冷却、可注入 `WakeWordScoring`。
- `CoreMLWakeWordScorer`：加载 `.mlmodel` / `.mlmodelc`，验证 float32 原始 PCM 固定张量输入与概率输出；CPU 本地推理。
- `GestureDetector.swift`：1–5kHz 双峰、200–500ms 间隔、>-30dBFS；8–12kHz 单峰、>-25dBFS 上升沿；释放滞回、峰值间隔、峰均比与带内能量占比过滤。
- `FFTAnalyzer.swift`：Accelerate FFT、Hann 窗、功率归一化、单边带功率与 dBFS。
- `ActivationDetector.swift`：三路并行扇出与统一回调；同一脉冲不重复发击掌/响指；模型失败不阻断手势。
- `StreamingResampler.swift`：带抗混叠的流式 windowed-sinc，保持跨块相位；只给唤醒模型降采样，不改变手势原始采样率。
- `Tests/ActivationRegression/main.swift`：43 个边界/状态/PCM/并行/抗混叠回归断言。
- `Tests/ActivationReplay/main.swift`：真实 `AVAudioFile` 读取 WAV，10ms 块回放，记录命中、误触和音频时间轴延迟；支持真实模型和外部录音清单。

所有阈值、频段、FFT 大小、hop、上升沿、滞回、峰均比、能量占比、冷却均在配置结构中可调。构造后更换参数应创建新实例，避免跨状态热改。

## 模型缺失的处理

当前没有收到任何训练好的 Jarvis 模型，**不能声称能区分“Jarvis”和普通讲话**。默认 `.model` 状态是 `.unavailable`，不会拿音量假装词义识别。

按任务允许的简易方案，可显式启用 `energyFallback`：>-40dBFS 持续 3–5 秒触发 `.voiceActivityFallback`，没有伪造的 0.8 置信度。它不是词义识别，无法承诺真实场景误触 <5%，且其 3 秒检测窗口本身不满足“从发声起 <500ms”。此模式仅用于调试/明确接受降级的场景。

CoreML 默认契约是 16kHz、16000 样本、输入 `audio`、输出 `confidence`。模型输入张量、输出索引、实际训练采样率必须与参数一致；mel/状态化模型需专用 scorer，不能套原始 PCM 接口。

## 集成

```swift
import JarvisActivation

var config = ActivationConfiguration()
config.gestures.clapThresholdDBFS = -30
config.gestures.snapThresholdDBFS = -25
let detector = try ActivationDetector(configuration: config)

detector.onActivation = { event in
    switch event.kind {
    case .wakeWord, .doubleClap, .fingerSnap:
        // 转交现有激活状态机；UI 更新自行切主队列。
        print(event.kind, event.timestamp)
    case .voiceActivityFallback:
        // 诊断事件，不冒充 Jarvis 识别。
        break
    }
}
detector.onError = { print("Wake error:", $0) }
// 在同一串行非实时工作队列处理；timestamp 为该块音频开始的单调时钟秒数。
try detector.process(buffer: copiedOriginalMicrophoneBuffer, timestamp: audioBlockStart)
```

**必须输入原始 44.1/48kHz Float32 流**，不能只接录音模块降采样后的 16kHz s16le 输出。16kHz 的奈奎斯特上限是 8kHz，8–12kHz 响指信号已经丢失，事后升采样无效。低采样率时 `snapAvailable == false`。也不要接 3–10 秒合并后的录音通知，否则无法低延迟回调。详见 `INTEGRATION.md`。

## 本次实测

| 测试 | 结果 | 解释 |
|---|---:|---|
| 回归断言 | 43/43 通过 | 真实 Swift + Accelerate 执行，模型分数测试使用 fake scorer |
| 合成双峰 WAV | 3/3，100% | 0.22/0.35/0.48s 双峰间隔 |
| 合成高频脉冲 WAV | 3/3，100% | 8.5/10/11.5kHz |
| 合成负例 WAV | 0/100，0% 片段误触率 | 谐波人声代理、三和弦、平稳白噪声、单击、超时双击、弱响指；**不是真实讲话/音乐** |
| 合成 fallback WAV | 3/3，100% | 仅证明 3 秒音量状态机，不证明 Jarvis 词义 |
| 手势最大回调音频延迟 | 20ms | 从响指/第二下击掌起，到实际同步回调所在 10ms 输入块结束；分析帧延迟最大 12.667ms |
| Xcode Debug 回放最慢处理块 | 88.712708ms | 本轮本机最大值 <500ms；不含硬件采集/调度/CoreML模型耗时 |
| 破坏性负控 | 6 个断言如期失败 | 仅 scratch 副本把响指阈值改为 0dB，正式源码保持 -25dB |

最新证据：`Evidence/Xcode/regression.json`、`gesture-replay.json`、`fallback-replay.json`；`Evidence/Xcode/execution.json` 与 `*-stdout.log` / `*-stderr.log` 保留 xcodebuild 和真实产物运行记录。`Evidence/WAV/` 保留 109 个生成 WAV 与两份清单。旧版 CLT/优化构建证据另存，不覆盖。合成数据在已知规则上生成，只是可重复的信号链回归，不能外推真实精度。

真实“Jarvis”各三遍、真实手势各三遍、真实背景音乐/聊天误触率、真实模型端到端 <500ms：**NOT_RUN / 未验收**。

## 复现（Xcode 27.1 / SDK 27.0）

已按最新环境切换到完整 Xcode，两条 scheme 均 `BUILD SUCCEEDED`，随后运行其 Debug 产物得到上表结果。根因不是源码或缺 Xcode，而是 **TMPDIR 含中文使 Swift driver SIGTRAP**。Swift/xcodebuild 命令按主脑确认的 workaround 使用 `TMPDIR=/tmp`；其他构建/缓存/证据仍放在 scratch，不更改系统工具链。

```bash
# 推荐：自动设置 TMPDIR/DEVELOPER_DIR，构建并运行全部测试。
python3 scripts/verify_xcode.py \
  --output '/Volumes/扩展硬盘/hermes/.hermes/cache/scratch/activation-xcode-new'

# 等价单条构建命令（不要改 App 骨架）：
env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -scheme activation-regression -destination 'platform=macOS,arch=arm64' \
  -derivedDataPath '/Volumes/扩展硬盘/hermes/.hermes/cache/scratch/activation-derived-new' \
  CODE_SIGNING_ALLOWED=NO build
```

旧 CLT `swift-frontend` 直编脚本仍保留作诊断替代：

```bash
# 在本目录运行，所有构建/缓存写入 scratch；不要覆盖旧证据目录。
SCRATCH='/Volumes/扩展硬盘/hermes/.hermes/cache/scratch'
python3 scripts/verify.py --output "$SCRATCH/activation-regression-new"
python3 scripts/generate_wav_fixtures.py --output "$SCRATCH/activation-wav-new"
python3 scripts/verify.py --target replay \
  --manifest "$SCRATCH/activation-wav-new/manifest.json" \
  --output "$SCRATCH/activation-replay-new"
python3 scripts/verify.py --target replay --fallback \
  --manifest "$SCRATCH/activation-wav-new/fallback-manifest.json" \
  --output "$SCRATCH/activation-fallback-new"
```

正常 Swift 工具链可 `env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift run activation-regression`，回放同前缀运行 `swift run activation-replay --manifest ... --report ...`。真实 WAV 可替换 manifest：`synthetic: false`，每个 clip 指定 `path`、`label`、`expectedKind`（负例 null）、`triggerTime`（第二下击掌/响指起点秒数）。命中要求每正例恰好一次；误触率分母是负例片段数。给入 CoreML 模型时增加 `--coreml ...` 与契约参数。
