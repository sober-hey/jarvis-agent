# 语音相关文档

代码本体在 [../../voice/](../../voice/)（模块职责见那里的 README）。这里放协议说明、实测结论和验证报告。

| 文档 | 内容 | 现在还有效吗 |
|---|---|---|
| `voice_gateway.md` | 网关的 WebSocket 协议：消息类型、鉴权、一轮对话的状态迁移 | 有效，但代码是准的；协议有改动以 `voice/voice_gateway.py` 为准 |
| `voice_stream_tts.md` | 流式 CosyVoice TTS 的分句与推送策略 | 设计仍适用；百炼已不是默认 TTS（见 ADR-0008） |
| `dashscope_probe.md` | 百炼语音 API 的实测结论（延迟、限额、坑） | 有效，ASR 仍走百炼 |
| `dashscope_adapter_verification.md` | 百炼适配器的实现与本地验证记录 | 文件哈希是当时的，代码已变；结论部分仍可参考 |
| `xiaoai_voice.md` | 小爱音箱作为语音出口的可行性 | 探索性，非主链路 |

## 想改语音代码先看哪里

1. [../../voice/README.md](../../voice/README.md) — 十个模块各自管什么，依赖方向
2. [../guides/VOICE.md](../guides/VOICE.md) — 出故障怎么取证、参数与出处
3. `docs/adr/ADR-0008` 到 `ADR-0010` — TTS 选型、端点检测分工、声纹让路这三件事的决定与理由
