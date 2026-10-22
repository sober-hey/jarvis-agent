# ADR-0008 语音合成改用火山 seed-tts-2.0

状态：已实施（2026-10-21）
相关：`packaging/voice_volcengine.py`、`packaging/voice_gateway.py`

## 背景

原链路的合成走百炼 CosyVoice，非流式：整段合成完才返回，首包 1.9–2.2 秒，长句更久。
语音是主要入口，首包直接决定"像不像在对话"。

## 决策

合成改用火山引擎 `seed-tts-2.0`，音色 `zh_female_shuangkuaisisi_uranus_bigtts`（思思 2.0），
WebSocket 双向流式，网关按块下发 MP3。

## 依据（同机同文本实测）

| 引擎 | 首包 | 总耗时 | 备注 |
|---|---|---|---|
| 火山 seed-tts-2.0 | 683–850 ms | 1.1–1.5 s | 逐块推送，真流式 |
| Inworld inworld-tts-2 | 1225 ms | 2.3 s | 音色最像 Sir（F0 127 vs 参考 133 Hz），但慢 |
| 百炼 cosyvoice-v3.5-flash | 1865–2237 ms | 2.2 s | 非流式，首包=总耗时 |

音色相似度（F0 中位数）：Sir 参考 133.3 Hz、Inworld 127.0、百炼克隆 146.8、火山思思 216.2（女声）。
即火山最快但音色不是本人；要本人音色需用火山声音复刻另开通。

## 代价与约束

- 必须 `proxy=None`：`websockets` 会读系统代理，launchd 下拿到 SOCKS 代理且缺 `python-socks`，
  表现为 App 收到 `tts_failed`（已在 `voice_volcengine.py` 注释里写明）。
- 音色与账号开通的资源绑定：`_mars_` 系列属 1.0，`_uranus_` 才属 2.0；资源不匹配会
  `resource ID is mismatched with speaker related resource`。
- 适配器**不实现** `create_stream()`，因此走 MP3 分块而不是 PCM 流；设备端需能播 MP3。

## 备选与被否原因

- 继续百炼：首包 2 秒以上，不满足对话体感。
- Inworld：音色最接近，但首包 1.2 秒且需要海外网络，作备用而非主力。
