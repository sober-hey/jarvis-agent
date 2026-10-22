# DashScope REST adapter: implementation and local verification

Verified locally: 2026-10-20T17:05:47+08:00. Provider endpoint evidence belongs to the parent agent's `docs/voice-docs/dashscope_probe.md`; this worker did **not** repeat live/provider calls after being told not to probe again.

## Interfaces

```python
# From the packaging import path; standard library only, no SDK installation.
from voice_dashscope import asr_transcribe, tts_synthesize, DashScopeError

transcript = asr_transcribe(raw_pcm16_16khz_mono_bytes, timeout=60.0)
# {"text": str, "emotion": str | None, "language": str | None, "usage": dict}
spoken = tts_synthesize("Sir，晚上好。", timeout=60.0)
# {"audio_bytes": bytes, "format": "mp3", "usage": dict}
```

- ASR uses `qwen3-asr-flash` on the parent-verified Beijing REST endpoint. Raw PCM is wrapped into a mono PCM16 WAV so the provider sees the 16 kHz rate explicitly; no ffmpeg dependency. Data URI: `data:audio/wav;base64,...`.
- TTS uses `cosyvoice-v3.5-flash` with the exact complete voice ID from the task. POST returns a signed OSS URL; adapter downloads the MP3 with a separate unauthenticated GET before returning. Documented HTTP audio URLs are upgraded to HTTPS without changing the signed query/path.
- Keys come only from `~/.jarvis/dashscope.key`. Reject missing/nonregular/symlink files, wrong owner, permissions other than 0600, empty/multiline/non-ASCII values. Never inspect environment keys.
- Fixed audio-download host: `dashscope-result-bj.oss-cn-beijing.aliyuncs.com`. Reject unrelated/private URL targets and redirects. API Authorization is never forwarded to the audio host. Environment/system proxy discovery is disabled per the existing voice probe workflow.
- No retries: timeouts may occur after a charged request; avoid automatic duplicate billing.
- `DashScopeError.code`: `key_file`, `invalid_key`, `quota_exhausted`, `rate_limited`, `permission_denied`, `timeout`, `network_error`, `invalid_parameter`, `provider_error`, `invalid_response`. Invalid local arguments raise `ValueError` before network access. Raw provider errors/signed URLs are not included in exceptions or logs.
- Every call logs elapsed seconds, character count, operation/model, success/error, and an allowlist of numeric usage metrics at INFO. Parent application configures logging; adapter does not install root handlers. Provider-reported usage is returned intact. TTS GET failure still logs POST usage if already received.
- Missing emotion/language are `None`, **not** invented `neutral`/`zh` values.

## Bounds and deferred work

- One ASR call accepts up to 180 seconds of PCM (5,760,000 raw bytes). Longer audio must be split by caller; no invented dominant emotion across segments.
- One TTS call accepts nonempty text up to 2,000 Python Unicode code points. Caller splits longer replies.
- `timeout` is a per-network-operation/socket timeout, not a strict total wall-clock deadline. TTS makes two operations.
- Responses are bounded: JSON 2 MiB, audio 32 MiB. MP3 ID3/frame signature checked; full decode is the consumer's responsibility.
- Streaming APIs deferred by explicit user steering. No placeholder stream functions.
- No daemon startup, microphone, playback, affect-engine wiring, integration registry changes or server deployment were performed. Parent owns product integration and live adapter acceptance. Parent probe validates MP3 ASR; this worker's new raw-PCM-to-WAV adapter path remains **not live-validated**.

## Executed evidence

```sh
PYTHONPATH=packaging .venv/bin/python3 -m pytest \
  packaging/test_voice_dashscope.py packaging/test_voice.py \
  packaging/test_voice_live.py packaging/test_voice_port.py -q
# 98 passed, 22 subtests passed in 11.94s
.venv/bin/python3 -m ruff check packaging/voice_dashscope.py packaging/test_voice_dashscope.py
# All checks passed!
.venv/bin/python3 -m ruff format --check packaging/voice_dashscope.py packaging/test_voice_dashscope.py
# 2 files already formatted
.venv/bin/python3 -m pyright packaging/voice_dashscope.py
# 0 errors, 0 warnings, 0 informations
```

12 adapter tests exercise request bodies, WAV format/rate, annotations, usage, signed URL download, secret redaction, invalid key/quota/rate/network/timeout paths, malformed payloads, limits, no retry, untrusted URLs, missing annotations and cost logs. Two tests run real local HTTP sockets (fixture server, **not** provider responses): ASR round trip and redirect rejection. The redirect test first reproduced urllib forwarding Authorization on a 302 redirect; fixed with a no-redirect handler. Old local voice regressions also pass. No full-product acceptance claimed.

SHA-256 bindings:

- `packaging/voice_dashscope.py`: `aca2abf9e77379d9c109104a465328822d19bd58419ce13ecbfbd457e85b430a`
- `packaging/test_voice_dashscope.py`: `84d61b5de8f394699571387745f5632a011bac3eb5f613c63ac582ceb0a88826`
