# 语音流水线调研：端点检测 / 打断 / 回声消除

> 目标：为「Apple 客户端（AVAudioEngine 采集）→ Python WebSocket 网关 → 百炼 ASR → LLM → 火山 TTS(MP3 分块) → 设备播放」这条链路，找出大厂与成熟开源项目已经验证过的做法，并给出可直接落地的改造方案。
>
> 调研时间：2026-10。证据分级：**官方文档** > **源码** > **工程博客/issue** > **推断**。查不到的写「未公开」，不编数值。

---

## 0. 先给结论（一句话版）

1. **回声消除必须发生在设备端，而且必须让「播放的声音」和「麦克风的输入」处在同一个 AEC 参照里**——LiveKit 明确写「WebRTC 的 AEC 只在客户端跑」，Pipecat 维护者直接说「AEC 是客户端实现的责任」。服务端做 AEC 需要精确时间对齐的参考流，Deepgram 把它列为「高级、非必要不要做」。
2. **我们现在的根因不是「没有 AEC」，是「播放器不在 AEC 的参照链路上」**：`AVAudioPlayer` 不走 `AVAudioEngine` 的输出总线，而 VPIO（VoiceProcessingIO）的回声参考信号正是从输出总线取的 → 即使开了 `setVoiceProcessingEnabled(true)` 也抵不掉 TTS。必须把播放搬进同一个 engine（`AVAudioPlayerNode`）。
3. **端点检测不要靠调静音阈值，要靠「VAD 给下界 + 语义/音频 EOU 模型给判词 + max_delay 兜底」**。LiveKit 的默认值就是 0.5s 下界 / 3.0s 上限，加了音频 EOU 模型后收窄到 0.3 / 2.5；[OI] 直接提供 `semantic_vad` + `eagerness` 四档。固定阈值（我们现在的做法）是被两家同时淘汰的方案。
4. **打断要做成「三层闸门」而不是「一有声就停」**：VAD 时长/字数门限 → 声学确认（是否是真插话 vs 附和）→ 文本确认（interim transcript 非空且不是自己刚说的话）。LiveKit 官方文档给的推荐起点/示例配置就是这套值：`min_duration=0.5s`、`min_words=0`、`false_interruption_timeout=2.0s`、`resume_false_interruption=true`。
5. **打断必须同时做「音频停」和「上下文截断」**。[OI] 用 `response.cancelled` + `conversation.item.truncate(audio_end_ms)`；Gemini 用 `interrupted` 标志 + 客户端清播放队列；Pipecat 只把「真正播出去的字」写进上下文。少了截断这一步，下一轮 LLM 会以为自己把没播的话说完了。

---

## 1. 逐家做法

### 1.1 [OI] Realtime API：turn detection + interrupt_response

**机制**：服务端做 VAD 分句，通过 `session.audio.input.turn_detection` 配置，两种模式：

- `server_vad`：纯静音切分。参数 `threshold`(0–1，激活阈值)、`prefix_padding_ms`(检测到语音前额外保留的音频)、`silence_duration_ms`(判定说完所需的静音)。文档示例配置即 `threshold=0.5`、`prefix_padding_ms=300`、`silence_duration_ms=500`（官方未逐项标注「默认」，按示例值起步）。
- `semantic_vad`：用一个语义分类器打分「用户这句话说完了没有」。概率低时**等一个超时**，概率高时立刻切。文档原话：话说到一半拖尾（trails off）会比一个明确的陈述得到更长的超时。
- `eagerness` 四档：`low` / `medium` / `high` / `auto`(默认，等价 medium)。`low` 让用户慢慢说，`high` 尽快切。
- `create_response` / `interrupt_response`：仅在语音对话模式生效。文档的表述方式是「想保留 VAD 但自己决定何时生成，就把这两个设成 false」——即**默认是开的**（自动建 response、自动被打断）。

**打断时服务端怎么 cancel**（这是最值得抄的部分）：

- 服务端检测到用户开口 → **自动取消进行中的 response**，并发出 `response.cancelled` 事件。
- 但「取消生成」不等于「上下文正确」。文档专门有一节 *Interruption and Truncation*：WebRTC/SIP 连接下服务端自己管播放缓冲、知道播到哪，**自动截断未播部分**；WebSocket 连接下客户端管播放，所以必须由客户端做三步：
  1. 监听 `input_audio_buffer.speech_started`；
  2. 立刻停播，并记录「最后一条回复播了多少」；
  3. 发 `conversation.item.truncate`，带 `audio_end_ms`（例：1500 表示只保留前 1.5 秒），把没播的音频和对应文字从上下文里删掉。
- 文档也承认这是有损的：`truncate` 能切音频、能删掉未播部分的文字，但**给不出精确对齐的截断转写**。

**对我们的意义**：`eagerness`/`semantic_vad` 这套「语义判词 + 超时」的思路可以照搬到我们网关侧，即使我们不用 [OI] 的模型。

来源：
- https://developers.openai.com/api/docs/guides/realtime-vad （VAD 两种模式、参数默认值、eagerness 四档）
- https://developers.openai.com/api/docs/guides/realtime-conversations （Interruption and Truncation、`response.cancelled`、`conversation.item.truncate`/`audio_end_ms`、push-to-talk 流程）

---

### 1.2 Gemini Live API：自动活动检测 + ActivityHandling

**机制**：`realtimeInputConfig.automaticActivityDetection`：

- `start_of_speech_sensitivity` / `end_of_speech_sensitivity`：各四档（`UNSPECIFIED`/`LOW`/`MEDIUM`/`HIGH`），默认 `START_SENSITIVITY_HIGH` 与 `END_SENSITIVITY_HIGH`（HIGH = 更容易判定）。
- `prefix_padding_ms`、`silence_duration_ms`：官方示例给的是 20 / 100（示例配置，非默认值）。
- `activity_handling`：`START_OF_ACTIVITY_INTERRUPTS` 为默认，即「用户一开口就打断模型输出（barge-in）」，当前回复当场被切断。
- 也可以关掉自动 VAD（`disabled: true`），改由客户端发 `activityStart` / `activityEnd`——这就是官方支持的「自己做端点」路径，也是半双工 push-to-talk 的实现方式。

**打断语义**：VAD 判定打断时，**进行中的生成被取消并丢弃**，只有已经发给客户端的内容留在会话历史里；服务端同时丢弃待执行的 function call 并在 `serverContent.interrupted` 里报出来。文档明确要求客户端「停止播放并清空播放队列」。

来源：
- https://ai.google.dev/gemini-api/docs/live-guide （VAD 章节、打断语义、自动 VAD 配置、关闭自动 VAD）
- https://ai.google.dev/api/live （`ActivityHandling`、`StartSensitivity`/`EndSensitivity` 各档定义与默认值）

---

### 1.3 LiveKit Agents：VAD → TurnDetector → STT → LLM → TTS，打断与 AEC 归属

LiveKit 是目前把「turn-taking 参数」暴露得最细的开源框架，值得逐项对照。

**端点检测**：
- 默认走「VAD + 音频 EOU 模型」两级。TurnDetector 是一个**音频模型**（不依赖转写文本），同时编码「说了什么」和「怎么说的」（语调、音高、节奏），支持 14 种语言，**含中文**。
- `endpointing.min_delay` 默认 0.5s、`max_delay` 默认 3.0s；**启用音频 TurnDetector 后收窄到 0.3s / 2.5s**（模型给得出高置信度判词，就不用等满下界）。`mode` 可设 `fixed` 或 `dynamic`（按会话内停顿统计在区间内自适应）。
- 关键约束：音频 TurnDetector 要求 VAD 的 `min_silence_duration ≥ 0.25s`，低于此会直接抛 `ValueError`；Silero VAD 默认 0.55s，满足要求。
- 模型没在约 1 秒内返回预测 → 直接提交这一轮（不让用户干等）。

**打断（barge-in）**：
- `interruption.mode`：`adaptive`（默认，前提是有 STT 或实时模型且跑在 Cloud/dev）/ `vad`（任何语音都算）。
- `min_duration`：官方推荐起点 **0.5s**；`min_words`：推荐起点 **0**（要开 STT 才有意义，>0 才要求真有词）。（注：这两项的出厂默认值文档未逐项列出，参考页示例即上述取值。）
- 误打断恢复：`false_interruption_timeout`（示例 **2.0s**）、`resume_false_interruption`（示例 **true**）——即「被打断后 2 秒没等到转写 → 判定是假打断 → 从断点继续说完」。这是我认为我们最该直接抄的一条。
- `discard_audio_if_uninterruptible`：不可打断时丢掉缓冲音频。
- 另有轮次边界/backchannel 冷却机制（Python）：智能体刚开口和快说完这两段里，真插话容易被误判成附和，adaptive 模式对此有专门处理。
- **预生成**：`preemptive_generation.enabled` 默认开——拿到最终转写就开跑 LLM（不必等 turn 确认）；`preemptive_tts` 默认关（省算力）；`max_speech_duration` 10s（长句跳过预生成）、`max_retries` 3。

**AEC 归属**（对我们最关键的一句）：LiveKit 文档写得很直白——SDK 支持 WebRTC 的 `echoCancellation`/`noiseSuppression`，但**「WebRTC 的抵消只在客户端跑，因此只适用于会议场景」**；对 agent/telephony（没有浏览器前端）场景，LiveKit Cloud 提供的是**降噪/人声隔离**（Krisp、ai-coustics），**不是 AEC**。也就是说：连 LiveKit 都不在服务端做回声消除，AEC 是端上的事。

来源：
- https://docs.livekit.io/agents/build/turns/ （turn detection 总览、InterruptionOptions、误打断恢复、user turn limit）
- https://docs.livekit.io/agents/logic/turns/turn-detector/ （音频 EOU 模型、14 语言含中文、endpointing 默认 0.5/3.0 → 0.3/2.5、VAD ≥0.25s 约束、`unlikely_threshold`）
- https://docs.livekit.io/agents/logic/turns/tuning/ （全参数表与默认值、症状→参数排障表）
- https://docs.livekit.io/agents/logic/turns/adaptive-interruption-handling/ （adaptive 模式机制、backchannel 过滤、turn boundary cooldown）
- https://docs.livekit.io/transport/media/noise-cancellation （「WebRTC cancellation runs in the client only」、Cloud 只提供 NC/BVC）

---

### 1.4 Pipecat：帧流水线 + InterruptionFrame + 半双工的官方开关

**流水线结构**：`transport.input → stt → context_aggregator.user → llm → tts → transport.output → context_aggregator.assistant`，一切皆 Frame，系统帧（SystemFrame）可以插队。

**端点检测**：
- VAD 用 Silero（CPU 单线程，30ms 块处理 <1ms）。`VADParams`：`confidence=0.7`、`start_secs=0.2`、`stop_secs=0.2`、`min_volume=0.6`。
- **注意版本差异**：官方文档当前默认 `stop_secs=0.2`（早期是 0.8，很多博客还在抄 800ms 这个旧值；一篇 iOS 实践博客就据此论证「300ms 不够，800ms 才够」）。用 0.2 的前提是后面接 Turn 模型兜底，不是纯靠静音。
- 轮次结束默认用 **Smart Turn 模型**（`LocalSmartTurnAnalyzerV3`，本地 CPU 跑），备选是 `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)`。
- 文档明确警告：`confidence`/`min_volume` 是「钝器」，噪声环境应该在上游做降噪（Krisp VIVA / ai-coustics / RNNoise），而不是抬阈值。

**打断（这是最干净的实现范式）**：`InterruptionFrame` 是 SystemFrame，四步级联：
1. 每个 processor 立即取消当前任务，丢弃排队中的 DataFrame/ControlFrame（`interruptible=False` 的帧如 FunctionCallResultFrame 保留）；
2. LLM 中途取消（带 `cancel_on_interruption=True` 的 function call 也取消）；
3. TTS 停止合成、清空文本聚合缓冲和 word timestamps、丢弃待发输出；
4. 输出 transport **排空未播音频**（有背景音混音时只排空 bot 语音，背景音不断）。

**上下文怎么记**：输出 transport 按播放进度把 `TTSTextFrame` 往下推，**没播出去的字永远不会进 assistant 上下文**；打断时聚合器只把「已经说出去的部分」作为 assistant 消息提交。这解决了「LLM 以为自己说完了」的问题，比 [OI] 的 `truncate` 更精确（因为它是按播放进度推文本，而不是按毫秒截）。

**半双工怎么退**：官方提供 mute 策略——`AlwaysUserMuteStrategy`（bot 说话时全程静音用户输入）、`FirstSpeechUserMuteStrategy`（只保护开场白）。这正是我们现在的行为，Pipecat 把它作为「让 bot 说完」的受支持配置，而不是缺失功能。

**AEC 归属**：Pipecat 明确「回声消除应该由客户端实现」；用 DailyTransport + Daily 客户端时 AEC 由客户端库提供。维护者还点名 **Firefox 的 AEC 很差**，不开耳机时 bot 自己的声音会被回采。→ 结论同上：AEC 是端上责任。

来源：
- https://docs.pipecat.ai/pipecat/learn/speech-input （Silero VAD 参数、stop_secs=0.2、Smart Turn 默认、降噪优先于抬阈值）
- https://docs.pipecat.ai/pipecat/fundamentals/interruptions （InterruptionFrame 四步、上下文只记已播文字、mute 策略、speech-to-speech 走 provider 侧打断）
- https://docs.pipecat.ai/pipecat/learn/text-to-speech （默认 `TextAggregationMode.SENTENCE`、可切 TOKEN、word timestamps 用于打断时对齐）
- https://github.com/pipecat-ai/pipecat/issues/670 （「Echo cancellation should be part of the client implementation」、Firefox AEC 差）

---

### 1.5 Deepgram：平台原生 AEC 优先 + 用「文本比对」兜底

工程指南（基于数百个生产部署）给了三条很实用的判断：

1. **先用平台原生 AEC**：浏览器 `getUserMedia({audio:{echoCancellation:true}})`；**iOS 写的是 `AVAudioSession` + Voice Processing I/O**；Android 是 `VOICE_COMMUNICATION` 音频模式；PSTN 电话网自带。理由：平台级 AEC 能同时拿到麦克风和扬声器输出，**时间对齐是自动的**。
2. **AEC 需要「热身」**：算法要在音频流动中学习声学环境。**让 agent 先说一句开场白，AEC 就能在用户开口前完成校准**，显著减少对话最初几秒的回声——这条对我们的"第一句话被自己打断"问题直接有效。
3. **软件层兜底**：拿 STT 输出跟「agent 刚说的 TTS 文本」比对，高度重合就丢掉这条转写。零音频管线改动，是最便宜的自激防护。
4. **服务端 AEC 是高级选项**：必须能拿到 TTS 播放作为参考流并与麦克风输入精确时间对齐，文档原话「在 iOS 或 Web 上做服务端 AEC 明显更复杂」；还警告「过度抑制会让 barge-in 更难——可能把用户的声音也一起压掉」。

来源：
- https://developers.deepgram.com/guides/deep-dives/audio-preprocessing-barge-in

---

### 1.6 Apple 平台：三条可能的 AEC 路径，行为差别很大

这是本次调研里最需要说清楚的部分，因为我们现有的误解就在这。

#### (a) `AVAudioSession` 只设 `.voiceChat` 模式 → **不给 AEC**

Apple 官方文档（`voiceChat` 的 Discussion）明确写着：

> For apps that use one or more chat modes (voice, video, or game), but don't use Voice Processing I/O or `AVAudioEngine` with `setVoiceProcessingEnabled(_:)`, **the system reduces the processing it applies to audio signals. Specifically, it doesn't apply voice-specific processing, like echo cancellation and automatic gain correction**, and disables dynamic processing on input and output, which results in a lower playback level.

即：`.playAndRecord` + `.voiceChat` 只是把路由收窄到适合通话的设备、把 EQ 调成语音、顺带打开 `allowBluetoothHFP`；**AEC 来自 voice processing I/O（VPIO）或 AVAudioEngine 的 voice processing，不是来自 mode 本身**。这条直接否掉了「设个 mode 就有回声消除」的假设。

#### (b) `AVAudioEngine.inputNode.setVoiceProcessingEnabled(true)` → 底层是 VPIO，参照来自**输出总线**

- 可用性：iOS 13 / macOS 10.15 / watchOS 6 起（`AVAudioIONode.setVoiceProcessingEnabled(_:)`）。
- 机制：开启后底层激活 AudioToolbox 的 `kAudioUnitSubType_VoiceProcessingIO`，它是**双总线**结构（bus 0 输出、bus 1 输入），**回声参考信号从输出总线取**。
- 这意味着一个硬约束：**只有经过这个 engine 输出总线的声音才会被当作参考**。用 `AVAudioPlayer`、`AVPlayer`、或另一个 engine 播的声音，VPIO 看不到 → 抵不掉。
- 一份 iOS 语音 agent 实践报告（作者在 Uttero 的 `CallAudioEngine.swift` 里落地）补充了三个 Apple 没文档化的坑：
  - VPIO 更接近「输出信号相减」而不是完整 AEC：**没有 delay 参数、没有可调的房间自适应模型、没有 API 能检查参考路径是否生效**（作者引 Apple DTS 工程师在论坛 thread 97679 的回复）。
  - **初始化顺序决定成败**：必须先建好播放图（playout callback 通），再 `setVoiceProcessingEnabled(true)`，再装采集 tap，最后 `prepare()/start()`。顺序错了 AEC 静默失效。
  - **门控时长**：他实测 300ms 的播放后静音门不够，800ms 才稳；生产实现用 1.5s 冷却 + 抬高 RMS 阈值。
- 副作用（有官方依据）：VPIO 会 duck 其它音频。AudioToolbox 提供 `AUVoiceIOOtherAudioDuckingConfiguration` 来配置这个 ducking；LiveKit Swift SDK 的 issue 里维护者的建议就是「不想要 ducking 就 `setVoiceProcessingEnabled(false)`」。另有大量报告：开了 VPIO 之后，app 内后启动的其它音频源音量明显变小。

#### (c) iOS 18.2+ `setPrefersEchoCancelledInput(_:)` → 系统级、路由级 AEC，**不要求 voiceChat**

这条最适合我们，因为它是「用内置麦克风录音 + 用内置扬声器放音」这个非通话场景专门做的：

- 可用性：iOS/iPadOS/macCatalyst **18.2+**，且**仅部分 2024 年及以后的 iPhone**；用 `isEchoCancelledInputAvailable` 查支持，`isEchoCancelledInputEnabled` 查是否真的生效。
- 限定组合：**必须是 `.playAndRecord` category + `.default` mode**（注意不是 `.voiceChat`）。
- 语义差别（官方原文）：用 Apple voice processing API 的会话不需要这个选项，因为系统已自动对这些路由做 AEC；**这个选项是给「需要更宽频带信号、不只是语音」的场景调的**，而 voice processing 是为语音信号调的。
- 路由变化会失效：切到耳机等不支持 AEC 的路由时，enabled 状态可能变化。

**关于「AVAudioPlayer 能不能被同一个 AEC 参照」**：
- 走 (b) VPIO 路径：**不能**。参照取自 engine 输出总线，`AVAudioPlayer` 不在那条总线上（推断，依据是 VPIO 双总线结构与「参考来自输出总线」的机制说明）。
- 走 (c) 系统级路径：**大概率能**，因为这是会话/路由级处理，不区分你用哪个 API 播（推断：文档把它描述为「让内置扬声器放音时的回声不出现在输入里」，并未限定播放 API；但 Apple 没有点名 `AVAudioPlayer`）。
- 结论：**(b) 必须换播放器；(c) 可以先拿 `AVAudioPlayer` 试，但必须实测 ERLE 才算数**。

#### (d) WebRTC AEC3 能不能用在非通话场景

- WebRTC 的 APM（Audio Processing Module）官方文档写明：**「APM 既可作为 WebRTC 原生管线的一部分，也可以 standalone 使用」**，AEC/NS/AGC 都在里面；AEC3 需要「远端信号（far-end / render stream）」作为参考，并按 10ms 帧处理。
- 但 iOS 上**没有官方公开的 APM API**：`WebRTC.framework` 不导出 APM 头文件。要用有两条路：
  1. 自己编 `webrtc-audio-processing`（PulseAudio 维护的那份）或用第三方封装，把 mic 帧和「我们自己送进扬声器的 PCM」成对喂进去；
  2. 直接用 LiveKit 的 `client-sdk-swift`（它就是 WebRTC + ADM，AEC 已经在里面，还提供 `AudioManager.shared.setVoiceProcessingEnabled`）。
- 代价：AEC3 的收敛需要参考流与播放**样本级对齐**，我们得自己维护「送进扬声器的 PCM 环形缓冲 + 播放时间戳」，这正是服务端 AEC 难做的同一个问题，只是搬到了端上（端上有天然优势：能拿到真实的播放回调）。
- 证据等级：官方文档（APM 可 standalone、AEC3 需要 far-end）+ 推断（iOS 集成路径）。

来源：
- https://developer.apple.com/documentation/avfaudio/avaudiosession/mode-swift.struct/voicechat
- https://developer.apple.com/documentation/avfaudio/avaudioionode/setvoiceprocessingenabled(_:)
- https://developer.apple.com/documentation/avfaudio/avaudiosession/setprefersechocancelledinput(_:)
- https://developer.apple.com/documentation/audiotoolbox/audio-unit-voice-i-o （`kAUVoiceIOProperty_MuteOutput`、`AUVoiceIOOtherAudioDuckingConfiguration`、`kAUVoiceIOProperty_MutedSpeechActivityEventListener`）
- https://developer.apple.com/forums/thread/97679 （Apple DTS：VPIO 是输出相减；论坛正文需登录，转引自下述博客）
- https://barock.dev/2026/04/22/why-your-ios-voice-agent-still-hears-itself （VPIO 双总线、初始化顺序、300ms 不够/800ms 更稳的实测）
- https://stackoverflow.com/questions/77628617/avaudioengine-avaudioplayer-setvoiceprocessingenabled-low-volume-output （input+output 都要开 voice processing，且 `AVAudioPlayer` 场景下音量异常）
- https://github.com/livekit/client-sdk-swift/issues/839 （voice processing 导致其它音频被 duck，关掉即恢复）
- https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/g3doc/audio_processing_module.md （APM 可 standalone、AEC/NS/AGC）

---

### 1.7 我们实际用的两个云服务：能力边界（决定架构可行性）

**百炼 ASR**：`qwen3-asr-flash` 是**非实时**接口（≤5 分钟音频，可流式返回结果），**没有 server VAD、没有 interim 增量**。真正能给我们「边说边出字 + 服务端 VAD」的是 `qwen3-asr-flash-realtime`（WebSocket 实时）：支持 `enableServerVad`，事件里有 `conversation.item.input_audio_transcription.text`（增量）与 `.completed`（完成），推荐 pcm/opus 输入，**当前不返回时间戳**。

→ 直接后果：**我们现在拿不到 interim transcript，所以「min_words 级打断门限」和「文本比对防自激」这两招都没法用**。要么换实时模型，要么在网关侧自己用本地 VAD + 能量门控。

**火山 TTS（seed-tts-2.0）**：双向流式接口有 `audio_params.format` 参数，服务端事件里包含 `TTSSentenceStart` / `TTSSentenceEnd`（句级边界，带该句文本）。这对我们非常有用：**打断时可以用「已完成的最后一句 + 当前句已播比例」换算出等价于 `audio_end_ms` 的截断点**。具体 seed-tts-2.0 是否稳定返回这两个事件需实测（文档给的是双向流式协议的事件定义，未逐模型标注）。

来源：
- https://help.aliyun.com/zh/model-studio/real-time-speech-recognition-user-guide
- https://www.alibabacloud.com/help/zh/model-studio/qwen-asr-api-reference
- https://docs.volcengine.com/docs/DoubaoVoice/bidirectional-streaming-text-to-speech-websocket?lang=zh

---

## 2. 对比表

| 维度 | [OI] Realtime | Gemini Live | LiveKit Agents | Pipecat | 我们现状 |
|---|---|---|---|---|---|
| 端点检测方式 | `server_vad` / `semantic_vad` | 自动活动检测（4 档灵敏度） | VAD + **音频 EOU 模型**（14 语言含中文） | Silero VAD + **Smart Turn v3**（本地 CPU） | 固定静音阈值 ❌ |
| 静音/下界默认 | 示例值 `silence_duration_ms=500`、`prefix_padding=300`、`threshold=0.5` | `silence_duration_ms` 示例 100、`prefix_padding_ms` 示例 20 | VAD 静音 0.55s；endpointing `min_delay=0.5s` | `stop_secs=0.2`、`start_secs=0.2`、`confidence=0.7` | 固定值，未公开 |
| 上限兜底 | 语义分低时等超时（时长未公开） | 未公开 | `max_delay=3.0s`（有 EOU 模型时 2.5s） | Smart Turn 判不出→超时提交 | 无 ❌ |
| 可调档位 | `eagerness` 4 档 | 起/止各 4 档 + `activity_handling` | `turn_detection` 5 种 + `endpointing.mode` fixed/dynamic | start/stop 策略可替换 | 无 |
| 打断判定 | VAD 触发即打断（`interrupt_response` 默认开） | `START_OF_ACTIVITY_INTERRUPTS` 默认 | `min_duration=0.5s`、`min_words=0`、adaptive 模型过滤附和 | VAD 触发即打断，可用 mute 策略反转为半双工 | 播放期硬静音 ❌ |
| 误打断恢复 | 无（靠 `truncate` 收尾） | 无 | `false_interruption_timeout=2.0s` + `resume_false_interruption=true` | 无内建（可自行实现） | 无 |
| 取消生成 | 服务端自动 `response.cancelled` | 服务端取消生成 + 丢弃 function call | 框架取消 LLM/TTS 任务 | `InterruptionFrame` 级联取消 | 无（靠静音避免） ❌ |
| 上下文截断 | `conversation.item.truncate` + `audio_end_ms`（毫秒，有损） | 只保留已发给客户端的部分 | 框架处理 | **只把已播出的字写进上下文**（最精确） | 无 ❌ |
| AEC 归属 | 服务端（WebRTC/SIP 场景）或客户端 | 客户端 | **客户端**（WebRTC）；Cloud 只提供降噪/人声隔离 | **客户端**（Daily 客户端库） | 无 ❌ |
| 端上 AEC 方案 | — | — | WebRTC AEC（iOS SDK 内置） | WebRTC AEC（浏览器/客户端） | 无 ❌ |
| 半双工支持 | `interrupt_response=false` / push-to-talk | 关自动 VAD + `activityStart/End` | `interruption.enabled=false` / `turn_detection="manual"` | `AlwaysUserMuteStrategy` | 唯一模式 ⚠️ |

---

## 3. 行业惯例：什么时候必须退回半双工

综合上面几家的隐含前提，全双工的成立条件是「**AEC 参照链完整**」：播放的声音能被 AEC 拿到、且时间对齐。不满足就必然自激。判定表：

| 条件 | 结论 | 依据 |
|---|---|---|
| 外放（内置扬声器）+ 内置麦克风，且 AEC 参照链完整（VPIO 输出总线 / 系统级 EC / WebRTC ADM） | 可做全双工 | LiveKit/Pipecat 默认全双工；Deepgram「平台原生 AEC 优先」 |
| 播放不经 AEC 参照路径（如 `AVAudioPlayer` + VPIO） | **必须半双工**，或先改播放链路 | VPIO 双总线机制（官方+博客） |
| 耳机/耳麦（无回声路径） | 可全双工，AEC 可关 | 物理上无回声 |
| 蓝牙 A2DP 播放 + 内置麦克风（A2DP 无回传通道，HFP 才双向） | 回声路径不确定，**建议半双工** | `.voiceChat` 只允许 HFP 路由（官方文档）+ 推断 |
| 强噪声/多人说话环境 | 全双工但必须加降噪/人声隔离，否则 VAD 假触发 | Pipecat「别抬阈值，先降噪」；LiveKit voice isolation |
| 端上算力不足以跑 EOU 模型 | 可全双工，但用 `max_delay` 兜底 | LiveKit/Pipecat 都留了超时路径 |
| 播报长文本（>30s，如读报告） | 建议半双工或限制可打断窗口 | LiveKit `user_turn_limit`、`max_speech_duration` 思路 + 推断 |

**惯例总结**（工程共识，非某一家明文）：
- **消费级语音助手默认全双工**，但**首次交互/开场白期间**要保护（Pipecat `FirstSpeechUserMuteStrategy`、LiveKit backchannel cooldown），因为 AEC 还没收敛。
- **电话/IVR/客服机器人默认偏半双工**，因为 PSTN 回声路径不可控，且业务上不希望被打断。
- **"必须退回半双工"的三个硬条件**：① 拿不到 AEC 参照；② 回声路径时延超出 AEC 尾长能力（外接音响、大房间、蓝牙）；③ 播放内容会被误识别为指令（如播报数字、命令词）。

---

## 4. 流式 TTS × LLM 增量输出怎么对接

### 4.1 切分：句边界 + 前瞻，不要按 token 直送

- Pipecat 默认 `TextAggregationMode.SENTENCE`：LLM 的 token 先聚合到句子再送 TTS；也可切 `TOKEN` 直送换更低延迟（代价是韵律破碎）。它的句子聚合器是**带 lookahead 的**（`_check_sentence_with_lookahead`），避免在小数点 `3.14`、英文缩写 `Dr.` 处误切。
- **「避免最后一帧断句」的两条硬规则**：
  1. **LLM 流结束时必须 flush**：聚合器要有一个 `flush()` 把残余文本送出（Pipecat 有），否则最后半句永远不合成。
  2. **不要用「字数/时间」硬切**：中文没有空格，按字符数硬切会切在词中间，TTS 会读出怪停顿。按标点（。！？；…）切，逗号只在句子过长时才切。
- 首句优先：第一句越短首发越快。常见做法是「首句按逗号也切，后续按句号合并」。
- **中文陷阱（这条最容易踩）**：Pipecat 文档写明「句子检测跟随 TTS 的 `language` 设置，未指定时默认英文」。也就是说聚合器如果按英文规则（`. ` `! ` `? ` + 空格）切，中文的 `。！？` 根本不触发 → 整段中文永远凑不成"一句"，只能等 flush，首句延迟直接爆掉。**我们自己的切分器必须显式支持中文标点**，并把 `…`、`；`、换行也算弱边界。

### 4.2 首发延迟：预生成 + 边合成边播

- LiveKit `preemptive_generation` 默认开：拿到最终转写就开跑 LLM，不等 turn 确认；`preemptive_tts` 默认关（取消时浪费算力）。这条对我们直接可用：**LLM 首 token 一出来就开 TTS，不等整段生成完**。
- Pipecat：TTS 音频字节流回来即播，官方称端到端常 <200ms（服务端侧）。
- 我们的瓶颈在 MP3：分块 MP3 必须**有状态解码**（`AudioFileStreamParseBytes` 支持分片喂入，再 `AudioConverter` 解 PCM），且 MP3 帧长 1152 样本、有 bit reservoir 和编码器延迟，**首帧到出声天然比 PCM 晚**，边界处理不当就是「最后一帧断句/爆音」。
- **建议**：让火山直接返回 **PCM（或 ogg_opus）**，端上零解码直接进 `AVAudioPlayerNode` 的 buffer。这一步同时解决三件事：首发延迟、末帧断句、打断精度（能精确知道播到第几个样本）。

### 4.3 打断时的"播到哪了"

三种精度，从粗到细：
1. **毫秒**（[OI] `audio_end_ms`）：客户端记「已写入播放器的样本数 / 采样率」；
2. **句**（火山 `TTSSentenceEnd`）：知道完整播完几句；
3. **字**（Pipecat + word timestamps）：只把已播出的字写进上下文。

我们现实可得的是 1+2 组合：**用句级事件做锚点，句内用样本计数插值**。

---

## 5. 给我们的 10 条具体建议

按「先修根因，再加能力」排序。

### ① 把播放从 `AVAudioPlayer` 搬进 `AVAudioEngine`（`AVAudioPlayerNode`）
- **判定：可实现**（只改客户端，不动网关协议）
- 理由：VPIO 的回声参考取自 engine 输出总线，`AVAudioPlayer` 不在那条总线上，所以现在就算开了 voice processing 也抵不掉 TTS。这是"没有 AEC"的真正原因。
- 证据等级：**官方文档 + 工程博客**（VPIO 双总线 / 参考来自输出总线）

### ② 开 `setVoiceProcessingEnabled(true)`，并且严格按顺序初始化
- **判定：可实现**
- 顺序：建播放图（playout 回调通）→ `inputNode.setVoiceProcessingEnabled(true)` → 装采集 tap → `prepare()` → `start()`。顺序错 = AEC 静默失效。
- **input 和 output 两个节点都要开**：社区实测（2023-12）只开 input 时抵消不干净，两个都开之后「扬声器声音确实被抵消掉了」，代价是**录到的麦克风音频会有点断续（choppy）**。另外要接受它的副作用：**会 duck 其它 app 音频**（可用 `AUVoiceIOOtherAudioDuckingConfiguration` 调）。
- 证据等级：**官方文档（API 存在）+ 工程博客（顺序与副作用）**

### ③ 加一条 iOS 18.2+ 的系统级 AEC 分支，作为首选路径
- **判定：可实现**
- `.playAndRecord` + **`.default` mode** + `setPrefersEchoCancelledInput(true)`，先用 `isEchoCancelledInputAvailable` 探活、`isEchoCancelledInputEnabled` 确认生效。仅部分 2024+ iPhone。
- 好处：不要求 `.voiceChat`，且是给"内置扬声器放音 + 内置麦克风录音"这个非通话场景调的，频带比 voice processing 宽（对 ASR 更友好）。这条路上 `AVAudioPlayer` 有可能被同一 AEC 覆盖（未文档化，**必须实测**）。
- 证据等级：**官方文档**（存在性与限定条件）/ 覆盖 `AVAudioPlayer` 属**推断**

### ④ 麦克风不再硬静音：改「三层闸门」的打断判定
- **判定：可实现**（网关侧逻辑，客户端只需保持采集）
- 三层：**时长/字数门**（VAD 持续 ≥0.4–0.5s，或 interim ≥1 字）→ **声学门**（回声残差能量高于阈值，排除 AEC 漏出的自激）→ **文本门**（interim 非空，且与最近一次 TTS 文本的重合度低于阈值，防自激）。
- 参数起点直接抄 LiveKit 的推荐值：`min_duration=0.5s`、`min_words=0`（先不开，等 ASR 流式化后再开到 1）。
- 证据等级：**官方文档**（LiveKit 推荐起点值）/ 三层组合属**推断**

### ⑤ 加"误打断恢复"：被打断后 2 秒无转写 → 继续说完
- **判定：可实现**
- 客户端不要一打断就丢缓冲，保留剩余 TTS 音频；网关侧 `false_interruption_timeout=2.0s`，期间没收到任何转写就发 `resume` 指令从断点续播。这是把"用户清嗓子"和"真插话"分开的最便宜手段。
- 证据等级：**官方文档**（LiveKit 官方示例即 2.0s / true；出厂默认值文档未逐项列出）

### ⑥ 打断要做"取消 + 截断"两件事，不能只停播
- **判定：可实现**
- 停播之外：网关发 `cancel` 给 LLM 流（中断生成）、abort TTS 连接、清空设备端播放队列；然后把"已播到哪"回写上下文——**assistant 消息只保留真正播出去的文字**（照 Pipecat 的做法），并附一个 `played_ms` 字段（照 [OI] 的 `audio_end_ms`）。
- 不做的后果：下一轮 LLM 以为自己把没播的话说完了，回答会答非所问。
- 证据等级：**官方文档（[OI] truncate / Gemini interrupted）+ 官方文档（Pipecat 上下文语义）**

### ⑦ 端点检测改成「VAD 下界 + 语义/音频 EOU + max_delay 兜底」
- **判定：可实现**
- 起点参数：VAD 静音 0.5s（不要 0.8s，那是旧默认值）、`min_delay=0.5s`、**`max_delay=3.0s` 强制收口**。思考停顿时靠 `max_delay` 收，而不是把静音阈值拉长——这是"思考停顿被切断"和"反应太慢"之间的正确取舍点。
- 升级项：接一个 EOU 模型。**LiveKit 的 TurnDetector 支持中文**（14 语言之一，有 `unlikely_threshold` 可调，v1-mini 可本地 CPU 跑、免费）；或 Pipecat 的 Smart Turn v3（本地 CPU）。有了它，`min_delay` 可收到 0.3s、`max_delay` 收到 2.5s。
- 证据等级：**官方文档**（参数与默认值）/ 具体阈值需按中文语料实测

### ⑧ ASR 换成实时流式模型，拿 interim transcript
- **判定：需换架构**（ASR 通道从"每句一次请求"改成"长连接 + 增量"）
- 理由：`qwen3-asr-flash` 是非实时接口，没有 server VAD、没有增量文本 → 上面 ④ 的"字数门"和"文本门"都用不了，端点只能靠纯能量。换 `qwen3-asr-flash-realtime`（支持 `enableServerVad`、有 `.text` 增量事件、pcm/opus 输入）后，打断判定和端点判定都能多一路信号。
- 注意：该模型**不返回时间戳**；`enableServerVad` 的静音参数具体字段与默认值文档未给全 → **需实测**，不要预设数值。
- 证据等级：**官方文档**（模型能力与事件名）

### ⑨ TTS 输出改 PCM/ogg_opus，并按句边界送合成
- **判定：可实现**
- 端上不要自己解分块 MP3（帧边界 + bit reservoir + 编码器延迟 → 末帧断句/爆音/打断精度差）。让火山返回 PCM，直接进 `AVAudioPlayerNode` buffer。
- 网关侧按标点切句（带 lookahead，别在小数点/缩写处切），**LLM 流结束必须 flush 残余文本**，首句尽量短以压首发延迟。**切分器要显式支持中文标点**（照英文规则写会切不动 `。！？`，首句延迟直接爆）。
- 用火山的 `TTSSentenceStart/End` 事件做播放进度锚点（seed-tts-2.0 是否稳定返回**需实测**）。
- 证据等级：**官方文档**（`audio_params.format`、句级事件）+ **源码**（Pipecat 带 lookahead 的句聚合）/ 端上收益属**推断**

### ⑩ 保留一个可自动触发的半双工降级开关
- **判定：可实现**
- 触发条件（任一）：AEC 探活失败（`isEchoCancelledInputEnabled=false` / VPIO 启动异常）；路由切到不支持 EC 的设备（蓝牙 A2DP、外接声卡）；连续 N 次（如 3 次/分钟）判定为自激或误打断。
- 降级行为：播放期静音 + 播放结束后 **250–400ms 冷却门**（不是 300ms 一刀切；工程实测建议 800ms 更稳，取折中并实测）。恢复条件：路由回内置扬声器且 AEC 探活通过。
- 理由：iPhone 内置麦克风的自激是公开的已知问题——LiveKit 的 issue #3758 里，即便浏览器侧把 `echoCancellation/googEchoCancellation/voiceIsolation` 全开，agent 仍然「听到自己在说话」并被当成用户语音。而 Apple 的 AEC 没有可调尾长参数 → **必须有一个兜底模式**，否则弱设备上体验会崩。
- 证据等级：**官方文档（AEC 无参数可调）+ 工程博客（800ms 实测）+ issue（iPhone 自激）** / 触发阈值属**推断**

### 明确"做不到"的两件事（避免走弯路）

| 事项 | 判定 | 理由 |
|---|---|---|
| 在 Apple 上拿到**可调参数**的 AEC（尾长、delay、双讲灵敏度） | **做不到** | VPIO 没有这些 API；文档与 DTS 回复都确认只有开/关 |
| 在**服务端**做 AEC 来替代端上 | **做不到（性价比极低）** | 需要 TTS 播放的精确参考流 + 与麦克风样本级对齐；Deepgram 明确列为高级选项并警告会压制用户语音、让 barge-in 更难 |

---

## 6. 未公开 / 存疑清单（不要当结论用）

| 项 | 状态 |
|---|---|
| [OI] `semantic_vad` 各 eagerness 档的具体超时毫秒数 | **未公开**（文档只给相对描述：low 让用户慢慢说、high 尽快切） |
| Gemini 自动 VAD 的默认 `silence_duration_ms` / `prefix_padding_ms` | **未公开**（文档只给示例值 100 / 20） |
| LiveKit TurnDetector 的中文 `unlikely_threshold` 标定值 | **未公开**（可覆盖，但基准值未列） |
| 百炼 `qwen3-asr-flash-realtime` 的 server VAD 静音阈值字段与默认值 | **未公开/需实测** |
| 火山 seed-tts-2.0 是否稳定返回 `TTSSentenceStart/End` | **需实测**（协议有定义，未逐模型标注） |
| iOS 18.2 `setPrefersEchoCancelledInput` 是否覆盖 `AVAudioPlayer` 输出 | **推断**（文档未点名播放 API），必须实测 ERLE |
| VPIO 的实际 ERLE / 收敛时间 | **未公开**（Apple 不提供指标；工程博客只给"300ms 不够、800ms 更稳"的经验值） |

---

## 7. 参考链接汇总

**模型厂商**
- [OI] Realtime VAD：https://developers.openai.com/api/docs/guides/realtime-vad
- [OI] Realtime 会话/打断截断：https://developers.openai.com/api/docs/guides/realtime-conversations
- Gemini Live 指南（VAD/打断）：https://ai.google.dev/gemini-api/docs/live-guide
- Gemini Live API 参考（ActivityHandling/灵敏度）：https://ai.google.dev/api/live

**开源框架**
- LiveKit Turn 总览：https://docs.livekit.io/agents/build/turns/
- LiveKit 音频 TurnDetector（含中文）：https://docs.livekit.io/agents/logic/turns/turn-detector/
- LiveKit Turn-taking 调参全表：https://docs.livekit.io/agents/logic/turns/tuning/
- LiveKit Adaptive interruption：https://docs.livekit.io/agents/logic/turns/adaptive-interruption-handling/
- LiveKit 噪声与回声消除（AEC 归属）：https://docs.livekit.io/transport/media/noise-cancellation
- Pipecat 语音输入与轮次检测：https://docs.pipecat.ai/pipecat/learn/speech-input
- Pipecat 打断机制：https://docs.pipecat.ai/pipecat/fundamentals/interruptions
- Pipecat TTS 文本聚合：https://docs.pipecat.ai/pipecat/learn/text-to-speech
- Pipecat AEC 归属 issue：https://github.com/pipecat-ai/pipecat/issues/670

**Apple / WebRTC**
- `AVAudioSession.Mode.voiceChat`：https://developer.apple.com/documentation/avfaudio/avaudiosession/mode-swift.struct/voicechat
- `setVoiceProcessingEnabled(_:)`：https://developer.apple.com/documentation/avfaudio/avaudioionode/setvoiceprocessingenabled(_:)
- `setPrefersEchoCancelledInput(_:)`：https://developer.apple.com/documentation/avfaudio/avaudiosession/setprefersechocancelledinput(_:)
- Audio Unit Voice I/O（ducking/mute 属性）：https://developer.apple.com/documentation/audiotoolbox/audio-unit-voice-i-o
- 论坛 thread 97679（VPIO 语义）：https://developer.apple.com/forums/thread/97679
- iOS 实践报告（VPIO 双总线/初始化顺序/门控时长）：https://barock.dev/2026/04/22/why-your-ios-voice-agent-still-hears-itself
- LiveKit Swift SDK ducking issue：https://github.com/livekit/client-sdk-swift/issues/839
- iPhone 内置麦克风自激 issue（浏览器侧 AEC 全开仍自激）：https://github.com/livekit/agents/issues/3758
- WebRTC APM（AEC3，可 standalone）：https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/g3doc/audio_processing_module.md

**云服务与工程实践**
- Deepgram 音频预处理与 barge-in：https://developers.deepgram.com/guides/deep-dives/audio-preprocessing-barge-in
- 百炼实时语音识别：https://help.aliyun.com/zh/model-studio/real-time-speech-recognition-user-guide
- 百炼 Qwen-ASR API 参考：https://www.alibabacloud.com/help/zh/model-studio/qwen-asr-api-reference
- 火山双向流式 TTS：https://docs.volcengine.com/docs/DoubaoVoice/bidirectional-streaming-text-to-speech-websocket?lang=zh
