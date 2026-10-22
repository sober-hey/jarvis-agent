# JARVIS WebSocket 音频客户端

独立模块，不修改 `JarvisMacApp` 骨架。实现以 `packaging/voice_gateway.py` 为准，Starscream 固定 4.0.8。macOS 12+ / Combine；App 可把本目录作为本地 Swift Package 依赖，或加入 `Sources/JarvisWebSocket` 的两个源文件并链接 Starscream。

## 接入

```swift
import JarvisWebSocket
import Combine

let client = try JarvisWebSocketClient()
var subscriptions = Set<AnyCancellable>()
client.onTextReceived = { print("助手:", $0) }
client.onTranscriptReceived = { print("转录:", $0) }
client.onAudioReceived = { mp3Chunk in /* 交给音频模块的 MP3 流播放器 */ }
client.onInterrupted = { /* 立即停止并清空本地播放缓冲 */ }
client.onError = { print($0.localizedDescription) }
client.connect() // 默认 ws://localhost:8765

// pcmData 是一次完整话段，不是实时麦克风小块；mono/16kHz/S16LE，最多30秒。
client.sendAudio(pcmData)
    .receive(on: DispatchQueue.main)
    .sink(receiveCompletion: { print($0) }, receiveValue: { response in
        // response.audio 为累积 MP3，response.text 为助手文本。
        // 本地实际播放结束后，再调用：
        if let generation = response.generation {
            client.playbackDone(generation: generation)
        }
    }).store(in: &subscriptions)

// 退出时 client.disconnect()，排队/在途 Future 均明确失败。
```

**示例中的播放完成调用必须放到播放器的真实 drain 回调，而非 Future 到达当刻。** 取消 Combine 订阅不会取消服务器任务。默认回调串行投递到主队列，传入其它 callbackQueue 时亦保序；后订阅已完成的 Future 用 `receive(on:)` 固定线程。

## 协议与可靠性

- 上传：`{"type":"audio","data":"<Base64 PCM>","format":"pcm_s16le","sample_rate":16000}`，随后 `{"type":"audio_end"}`。
- 收到用户/助手 `text` 按 role 分流；MP3 使用 `data/format/final/generation`。空 Base64 且 final=true 是合法完成标记，Future 此时成功。
- 同时只发一个话段；默认内存队列16条/8MiB（含 Base64/JSON），每段最长30秒，响应总音频16MiB，响应/排队总期限120秒。
- **网关没有 request_id/去重协议**。重连只继续尚未发送的音频；已经开始写入的操作失联时返回 `outcomeUnknown`，绝不自动重放可能写入 timeline 的脑操作。不能承诺断线在途数据无损或 exactly-once。
- 失败5秒后重连，明确 connect 周期内最多10次。握手本身最多等10秒，所以服务器恢复不保证恰5秒内重连。即使反复握手成功再断线也不重置预算。
- 30秒无收发则 ping；写入最长15秒、pong最长10秒；写卡住、握手失败、连接失活都会释放会话并进入有限重连。用 monotonic uptime 计时。
- generation 抑制过期/重复 final；坏包/超限/网关错误重置连接，防止无ID协议把迟到回复归给下一段。
- 非回环必须 `wss://`，不关闭 TLS 验证。Starscream 默认添加 Origin，网关 `origins=[None]` 会拒绝；适配器仅去除客户端合成 Origin，不削弱服务端策略，不发送共享浏览器 Cookie。

## 验证

在本模块根执行：

```bash
env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  swift test --scratch-path /Volumes/扩展硬盘/hermes/.hermes/cache/scratch/jarvis-websocket-spm-final

env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -scheme JarvisWebSocket -destination 'platform=macOS,arch=arm64' \
  -derivedDataPath /Volumes/扩展硬盘/hermes/.hermes/cache/scratch/jarvis-websocket-xcode-build \
  CODE_SIGNING_ALLOWED=NO test

python3 scripts/wscat_acceptance.py
/Volumes/扩展硬盘/jarvis/.venv/bin/python scripts/integration.py
```

本机中文 TMPDIR 会导致 swift-driver/xcodebuild SIGTRAP，按主脑实测用 `TMPDIR=/tmp`。CLT-only 备用验收脚本 `scripts/test.py` 使用真实 swift-frontend+clang，未模拟 Swift/Combine/Starscream。

已实跑：Xcode/XCTest **8通过/0失败/0跳过**，SwiftPM同样8通过；真 wscat 上观察PCM JSON并接收文本/音频；实际 VoiceGateway 类的协议联调（brain/speech 边界为明确夹具）；监听器关闭30秒后恢复，38.025秒重新连上；默认心跳首个 ping 31.056秒。结果见 `verification/`；完整 `.xcresult` 位于 scratch `jarvis-websocket-final.xcresult`。

## 范围限制

未接线真实录音/播放模块或App骨架，未测试云端ASR/TTS、真实brain/timeline写入、扬声器MP3解码、物理Wi-Fi断网。音频集成测试的3字节内容是**传输夹具，不是可播放MP3**。30秒测试是停本地监听器，不修改用户网络配置。当前 incoming 限制是 Starscream 交付完整消息后的应用层校验，不是恶意 fragmented frame 的网络层内存硬上限；本模块不作帧级DoS防护声明。队列仅内存，进程退出不持久保存录音。
