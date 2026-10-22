# 百炼（DashScope）语音 API 实测结论

实测时间：2026-10-20 16:47 CST
实测人：Hermes（主脑）
Key：`~/.jarvis/dashscope.key`（0600，`sk-ws-` 开头，workspace 级密钥）

## 结论：TTS 与 ASR 均已打通，无需 Workspace ID

端点用不带 workspace 前缀的公共域名即可，**不需要**填 `{WorkspaceId}`。

---

## ① 语音合成 TTS（已验证）

```
POST https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer
Authorization: Bearer <key>
Content-Type: application/json
```

请求体：
```json
{
  "model": "cosyvoice-v3.5-flash",
  "input": {
    "text": "要合成的话",
    "voice": "cosyvoice-v3.5-flash-jarvis-f7bec1631ddf4725841995ba9c711376",
    "format": "mp3",
    "sample_rate": 24000
  }
}
```

**关键坑（实测踩到）**：`voice` 必须填**完整串**，含模型名前缀。
- ✓ `cosyvoice-v3.5-flash-jarvis-f7bec1631ddf4725841995ba9c711376`
- ✗ `jarvis-f7bec1631ddf4725841995ba9c711376` → `Engine return error code: 418`

响应：
```json
{
  "output": {
    "audio": {
      "data": "",
      "url": "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/....mp3?Expires=...",
      "expires_at": 1792572442,
      "id": "audio_..."
    },
    "finish_reason": "stop"
  },
  "usage": {"characters": 42}
}
```

音频不内联在响应里，要给 `url` 再发一次 GET 下载。实测 42 字符 → 103,771 字节 MP3、5.184 秒、24kHz 单声道、160kbps。

计费口径：按 `usage.characters`（字符数）。

**已知约束**：`cosyvoice-v3.5-flash` / `cosyvoice-v3.5-plus` 只在北京区，且**只有复刻音色和声音设计，没有系统音色**。所以必须用 Sir 复刻的音色 ID。

---

## ② 语音识别 ASR（已验证）

```
POST https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
Authorization: Bearer <key>
Content-Type: application/json
```

请求体（音频走 base64 data URL）：
```json
{
  "model": "qwen3-asr-flash",
  "input": {
    "messages": [
      {"role": "user", "content": [{"audio": "data:;base64,<base64音频>"}]}
    ]
  }
}
```

响应：
```json
{
  "output": {
    "choices": [{
      "finish_reason": "stop",
      "message": {
        "annotations": [{"emotion": "neutral", "language": "zh", "type": "audio_info"}],
        "content": [{"text": "Sir，晚上好，备考辛苦了，记得喝水，我是 Jarvis。"}],
        "role": "assistant"
      }
    }]
  },
  "usage": {
    "audio_tokens": 129, "input_tokens": 129, "output_tokens": 22,
    "seconds": 5, "total_tokens": 151
  }
}
```

**额外白送的能力**：`annotations` 里有 `emotion`（情绪）和 `language`（语种）。情绪可以直接喂给 JARVIS 的 affect_engine，不用另做情感识别。

计费口径：按 `usage.seconds`（音频秒数）+ output tokens。

---

## ③ 回环验证（证据）

用 TTS 生成 "Sir，晚上好。备考辛苦了，记得喝水。我是Jarvis。" → 下载 MP3 → 喂给 ASR → 返回 "Sir，晚上好，备考辛苦了，记得喝水，我是 Jarvis。"

文本一致，标点归一化。整条语音链路可用。

产物：`/Volumes/扩展硬盘/hermes/.hermes/cache/scratch/jarvis_tts_test.mp3`

---

## ④ 未测但已知

- **流式 TTS**（低首包延迟）：WebSocket `wss://dashscope.aliyuncs.com/api-ws/v1/inference/`，`action=run-task`、`task=tts`、`function=SpeechSynthesizer`、`streaming=duplex`。本机 jarvis venv 未装 `websockets`，需 `pip install websockets`。
- **实时 ASR**（边说边出字）：同一 WebSocket 端点，`task=asr`。未实测。
- **qwen-audio-3.0-tts-flash / qwen-audio-3.1-tts-flash**：也支持复刻音色，可作为备选。未测。

---

## ⑤ 对实现的要求

- Key 只从 `~/.jarvis/dashscope.key` 读，绝不硬编码、绝不打印。
- TTS 返回的是 URL 不是内联音频 → 必须二次 GET 下载。URL 有 `Expires`，别缓存太久。
- ASR 音频上限按 10MB / 3 分钟算，超了要分段。
- 每次调用记 `usage`，成本可算：TTS 按字符，ASR 按秒。
