# 语音链路操作手册

用于排查「说了没反应 / 听不到声音 / 只有一句就断 / 别人说话它也答」这类问题。
先按下面顺序取三条证据，再动手改代码。

## 一、先取证（不要一上来改代码）

```bash
# 1) 网关在看什么（每轮都有这些行）
grep -v arbitrate ~/.jarvis/voice-gateway.log | tail -30
```

关键日志行与含义：

| 日志 | 含义 |
|---|---|
| `hello device=… device_type=…` | 设备握手成功 |
| `audio_commit … pcm_bytes=N generation=G` | 收到一句话，N 字节能算出时长（16k×2 字节/秒） |
| `speaker_score … score=0.xx owner=true/false mode=enforce` | 声纹判定；`owner=false` 会在别人说话时让路 |
| `asr_text … chars=N text=…` | 识别结果 |
| `asr_filler` | 只有「嗯/呃/啊」，按设计不回答 |
| `turn_timing … asr_ms brain_ms answer_chars` | 识别与思考耗时 |
| `tts_first_chunk … ms=` / `tts_timing … frames_sent=N status=success` | 语音合成首包与总量 |
| `tts_mode=fallback` | **不是失败**：表示走 MP3 分块通道（火山适配器不实现 PCM 流）。`tts_mode=stream` 才表示走 PCM 流式。运维看到 fallback 不要当成降级报警 |
| `playing_stale_reset` | 上一轮播放回执丢了，已自动清标志 |
| `conversation_ended` | 让路结束，播了那句追问 |

```bash
# 2) Mac 客户端在干什么（沙盒容器内）
tail -50 ~/Library/Containers/com.jarvis.macos/Data/Library/Logs/JARVIS/mac-events.jsonl
```

事件名：`capture`（含 sample_rate）、`vad_audio_end`、`mp3_received`、`player_playing`、
`player_finished`、`playback_done`、`stale_response_discarded`、`error`。

```bash
# 3) 拍手/响指到底听到了什么（原始冲激数据）
tail -20 ~/.jarvis/mac-gestures.jsonl
```

每行是一次冲激候选：`clap` / `snap`（dBFS）、`cf` / `sf`（频段能量占比）、`crest`（尖峰比）。
判定阈值在 `GestureDetector.swift`：clap −38 dBFS、snap −36 dBFS、突升 ≥4 dB、尖峰比 ≥2.0。

## 二、常见故障对照

| 现象 | 先看 | 常见原因 |
|---|---|---|
| 点球体没反应 | 网关 `hello` + `audio_commit` | 没连上网关；或麦克风权限被拒（Mac 事件日志里有 `error`） |
| 说完了没识别 | 有没有 `audio_commit` | 客户端 VAD 没判定说完（噪声底噪标定问题，看 `mac-gestures.jsonl` 的底噪） |
| 识别到但不回答 | `speaker_score owner=false` | 声纹把你判成别人了；用 `~/.jarvis/utterances/` 里的真实录音重建参考 |
| 回答只有一句就断 | `tts_timing frames_sent` 与客户端 `player_finished` | 服务端发完了但客户端播放被中断（旧版是插话打断，新版播放期不收音） |
| 声音小/音量乱跳 | 是否走引擎播放 | 回复必须从采集引擎的播放节点出声，独立播放器不在回声消除总线上 |
| 别人说话它也答 | `speaker_score` | 声纹阈值或参考音质量；先把 `mode=enforce` 打开并确认参考音是本人 |
| 连不上、反复"尝试下一个地址" | `listening hosts=` | 只绑了 127.0.0.1（启动时 Tailscale/局域网地址还没就绪，已有 10 秒重绑） |

## 三、一键体检

```bash
python3 scripts/healthcheck.py          # 端口、设备连接、心跳、库体积、磁盘、RSS
python3 scripts/healthcheck.py --json   # 给告警系统用
```

## 四、改参数之前先仿真

一句话切分（何时算说完）的规则在 `apps/mac/JarvisMac/Sources/JarvisIntegration/UtteranceGate.swift`，
iOS 对应 `apps/ios/JarvisVoice/JarvisVoice/Core/AudioPrimitives.swift` 的 `SpeechGate`。

改完必须跑仿真，16 种说话方式要全过：

```bash
cd apps/mac/JarvisMac
swiftc -O Sources/JarvisIntegration/UtteranceGate.swift Tests/UtteranceSimulation/main.swift -o /tmp/gate_sim
/tmp/gate_sim
```

## 五、当前参数与出处

| 参数 | 我们 | 参考 |
|---|---|---|
| 常规结束静音 | 0.9 s | 豆包 `end_window_size` 推荐 800–1000 ms |
| 短句结束静音 | 1.4 s | 豆包端到端 `end_smooth_window_ms` 默认 1500 ms |
| 长独白结束静音 | 0.8 s | 豆包语义 VAD 无法判定时 700 ms |
| 结束观察窗 | 0.5 s | 豆包句末标点 300 / 逗号 400 / 无法判定 700 ms |
| 开口预卷 | 300 ms | OpenAI Realtime `prefix_padding_ms` 默认 300 ms |
| 回复后待续窗口 | 25 s | Google 约 8 s、Alexa 5–8 s、小爱 15 s 量级 |

## 六、已知限制

- 没有可用的回声消除证据前，播放期间不收音（半双工）；要它停只能点菜单栏。
- 声纹参考来自本机近讲录音；换房间、压低嗓子可能被判成他人，需要重新录参考。
- 语音识别目前只有百炼可用，火山识别未开通（会返回 403 `requested resource not granted`）。
