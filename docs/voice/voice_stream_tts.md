# 流式 CosyVoice TTS

本模块是独立的、尚未登记到网关的异步文字流 → 音频流适配器：

```python
from voice_stream_tts import StreamingTTS

async for pcm_chunk in StreamingTTS().stream(text_fragments):
    player.write(pcm_chunk)
```

`text_fragments` 是 `AsyncIterable[str]`，适合直接接大脑的增量输出。模块按 `。！？.!?` 和换行立即切句；达到 `comma_threshold`（默认 18 个字符）后的 `，,` 也会切出，输入结束时会 flush 不完整尾句。`cancel()` 是同步方法，可由打断处理器直接调用；它设置取消标记并异步关闭当前 WebSocket，后续片段不会发送。

## 音频格式

输出选 **raw PCM16 little-endian、单声道、24 kHz**，而不是 MP3。PCM 不需要等压缩文件结束即可送入播放器，首包路径更短；播放端按 24,000 Hz / 16-bit / mono 配置。若需要归档，可自行加 WAV 容器（模块不加容器，避免把播放首包延后）。

## 百炼协议

依据阿里云官方文档：

- [CosyVoice WebSocket API](https://help.aliyun.com/zh/model-studio/cosyvoice-websocket-api)
- [CosyVoice 客户端事件](https://help.aliyun.com/zh/model-studio/cosyvoice-client-events)
- [CosyVoice 服务端事件](https://help.aliyun.com/zh/model-studio/cosyvoice-server-events)

连接 `wss://dashscope.aliyuncs.com/api-ws/v1/inference/`，握手头带 `Authorization: Bearer <key>`。同一 UUID `task_id` 贯穿：

1. `run-task`：`task_group=audio`、`task=tts`、`function=SpeechSynthesizer`、`streaming=duplex`、模型 `cosyvoice-v3.5-flash`，音色必须为完整 ID `cosyvoice-v3.5-flash-jarvis-f7bec1631ddf4725841995ba9c711376`。
2. 等待服务端 `task-started`。
3. 多次 `continue-task`，文本放在 `payload.input.text`。服务端在句子完成后发 `result-generated`，每个合成事件后通过 WebSocket binary 通道发送音频帧。
4. 输入结束发送 `finish-task`，继续收二进制帧，直到 `task-finished`。
5. 打断时本模块直接关闭连接，保证本地立刻停止发送；官方协议也定义了 `finish-task` + `payload.input.directive=cancel`，后续接网关时可改成“先发 cancel、等待 task-finished，超时再 close”的可复用连接方案。

官方限制：单次 `continue-task` 最多 20,000 字符，累计最多 200,000 字符，片段发送间隔不能超过 23 秒。模块还在本地执行这些长度检查。

## 错误分类

- `AuthenticationError`：key 文件不可读/不合规，或握手/服务端报鉴权失败。
- `QuotaError`：握手或服务端报配额、余额、欠费。
- `NetworkError`：连接建立失败、连接意外断开。
- `ProviderError`：协议格式、模型参数或其他服务端拒绝。

所有异常消息都经过清洗，不包含 key、签名 URL 或原始 provider 文本。

## 凭据

默认只读取 `~/.jarvis/dashscope.key`；要求当前用户拥有、普通文件、权限 `0600`，不读环境变量、不打印 key。测试使用临时的 0600 假 key 和本地假 WebSocket 服务。

## 接入网关建议（当前不实施）

1. 网关把模型增量文字转成 `AsyncIterable[str]`，把 `StreamingTTS.stream()` 产生的 PCM chunk 直接交给设备播放层；不要先拼成完整答案。
2. 每个回答维护一个 stream handle；设备侧唤醒/用户说话事件调用 `cancel()`，同时让当前回答的剩余文字生产停止。若网关需要复用 WebSocket，再把取消实现为官方 `finish-task`/`directive=cancel`，并以 `task-finished` 作为任务边界。
3. 播放器统一声明 24 kHz、16-bit、mono PCM；用 chunk 首到时间和 `task-finished` 时间分别记录首包/总时长及字符 usage。
4. 在 `integration_registry.py` 增加独立 capability 登记，在 `voice_gateway.py` 增加可选流式 TTS 分支；保留现有 `tts_synthesize` 作为非流式回退。网关修改前应增加真实打断回归，不在本模块内改动这些文件。
