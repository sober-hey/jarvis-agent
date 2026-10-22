# Watch ↔ voice gateway

Canonical shared contract: `../../ios/JarvisVoice/PROTOCOL.md`; server implementation `../../../packaging/voice_gateway.py` (paths relative to this directory). Watch no longer sends custom `voice.clip`, `voice.reply`, or `device.status` messages.

- Direct `wss://` URLSession WebSocket; only loopback `ws://` is allowed for local fixtures. No credentials/query/fragment in URL; no HTTP redirects. Optional gateway token stays in Keychain and is sent as HTTP `Authorization: Bearer <gateway-token>`, exactly like iOS. Never use a model/cloud key.
- First JSON text frame is `hello`, sent after WebSocket upgrade and before `ready=true`. `device_id=watch-<persisted UUID>` survives reconnect; `device_type=watch`, `sample_rate=16000`, `channels=1`, `encoding=pcm_s16le`, with nested `state`.
- State updates use `{"type":"device_state","device_id":"watch-<same UUID>","device_type":"watch","state":{...}}`, on local changes and every 15 seconds while the app runs. WebSocket ping is also every 15 seconds. No background keep-alive claim.

Watch state includes:

```json
{"device_type":"watch","battery_level":0.6,"charging":false,"clap_triggered":true,"last_clap_at":1792450015,"wearing":null,"on_wrist":null,"wrist_detection_enabled":null,"app_foreground":true,"screen_on":null,"screen_state_source":"unavailable_public_api","observed_at":1792450015}
```

`clap_triggered` means the local double-impact trigger occurred within the last **5 seconds**, not a sound classifier. It clears and reports false when that window expires (if the app has CPU). A new impact is reported even if the device is currently suppressed, so server arbitration may observe it; it does not force local unsuppression. `wearing`, `on_wrist`, and `wrist_detection_enabled` remain null: heart-rate/explicit authentication evidence is reported separately, never promoted into continuous wearing truth. Unknown battery/charging is null. State also retains OS, runtime, motion, recording/playback, wrist-proxy and timestamped wear evidence fields.

## Audio up

A completed, bounded local AVAudioRecorder WAV is validated as 16kHz, mono, signed PCM16 little-endian. Parse RIFF chunks (including metadata/padding and PCM extensible format); never assume a 44-byte header. Send **raw PCM only**, paced in 20ms JSON frames (last frame may be shorter), followed by `audio_end`:

```json
{"type":"audio","format":"pcm_s16le","sample_rate":16000,"data":"<base64 raw mono PCM16, no WAV header>"}
{"type":"audio_end"}
```

Maximum local PCM is 600000 bytes; capture still has a 12-second hard cap. This is triggered half-duplex capture then paced upload, **not live full-duplex mic streaming**. No audio is captured while suppressed. A capture interrupted by disconnect/suppression cannot resume after reconnect/selection; a new full onset is required. There is at most one pending utterance; a local UUID guards its timeout, but no invented UUID is put on the gateway wire. No automatic reconnect, offline queue, or uncertain retry.

## Text/MP3 down

```json
{"type":"text","role":"user","text":"recognized words","generation":1}
{"type":"text","role":"assistant","text":"reply","generation":1}
{"type":"interrupt","generation":2}
{"type":"audio","format":"mp3","data":"<base64 MP3 chunk>","generation":2,"final":false}
{"type":"audio","format":"mp3","data":"","generation":2,"final":true}
{"type":"suppressed"}
{"type":"selected"}
{"type":"error","code":"invalid_audio"}
```

Support `interrupt` both with generation (iOS contract) and without (initial gateway implementation). Stop local playback and drop partial/stale MP3 immediately. Local onset also stops playback and rejects old in-flight generations. Concatenate only one generation, cap each JSON frame at 2MiB and assembled MP3 at 24MiB, deliver to AVAudioPlayer only on final (which may carry an empty chunk). Reject non-integer/boolean/negative generations, bad base64 or non-MP3 audio format. Duplicate final does not replay. Disconnect resets generation and buffers.

Natural playback completion sends `{"type":"playback_done","generation":2}`. Suppression cancels current capture, stops playback, drops pending audio and prevents new forwarding; `selected`/`resumed` permits only a new full onset. A successful send is not a BRAIN side-effect acknowledgement. This Watch implementation alone does not establish service-side authentication, multi-device single-winner enforcement, physical device playback, or real BRAIN integration.

## Repeatable local verification

All Swift invocations use `env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`; Xcode DerivedData is under `/tmp`, with `CODE_SIGNING_ALLOWED=NO` for device and simulator builds.

```sh
env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  /Volumes/扩展硬盘/jarvis/.venv/bin/python3 Scripts/verify.py

env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  /Volumes/扩展硬盘/jarvis/.venv/bin/python3 Scripts/verify_watchos.py \
  --simulator-id 54D78C1B-FFC9-40B8-8DE5-BC1B5E822B66
```

`Scripts/test_connection.py` starts a Python `websockets` fake gateway, verifies a fixture-only bearer header and strict hello/state/raw PCM/audio_end frames, sends text plus real ffmpeg-generated synthetic MP3 chunks, and checks the **actual shared Swift core + WatchConnection** receives the byte-identical complete MP3. It additionally exercises interrupt with/without generation, suppression/selection, stale-capture rejection, and a second fresh onset. No model calls, production socket, real Keychain read, Watch microphone, or hardware playback are involved. Requires local `websockets` and `ffmpeg`; the fixture is a synthetic tone, not a cloud response.
