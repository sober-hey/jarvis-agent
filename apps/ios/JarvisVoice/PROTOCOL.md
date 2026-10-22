# iPhone ↔ voice gateway wire contract

Connection: `ws://<Mac Tailscale IPv4>:8765` within tailnet, or `wss://<TLS-host>`. JSON text frames only. WebSocket ping every 15 seconds. Optional HTTP header `Authorization: Bearer <gateway token>` is a gateway credential, never a cloud/model key. Model identity/personality remains solely on the server via BRAIN mode and the single timeline.

## Audio messages: matches current packaging/voice_gateway.py

Client → server, per 20ms PCM frame (320 samples / 640 bytes):

```json
{"type":"audio","format":"pcm_s16le","sample_rate":16000,"data":"<base64 raw mono signed-int16 little-endian PCM, no WAV header>"}
```

End of utterance and completion of local playback:

```json
{"type":"audio_end"}
{"type":"playback_done","generation":4}
```

Client sends only VAD-bounded utterances including pre-roll/tail; it keeps real capture and socket alive during silence but sends no idle PCM. App ends an utterance after 600ms silence or 25s maximum. Gateway must not mistake periodic device_state messages for PCM data or utterance completion.

Server → client:

```json
{"type":"text","role":"user","text":"<final transcript>","generation":4}
{"type":"text","role":"assistant","text":"<JARVIS reply>","generation":4}
{"type":"interrupt","generation":5}
{"type":"audio","format":"mp3","data":"<base64 MP3 chunk>","generation":5,"final":false}
{"type":"audio","format":"mp3","data":"","generation":5,"final":true}
{"type":"error","code":"invalid_audio"}
```

The server increments generation before every new audio/text utterance and sends interrupt when old playback must stop. All chunks for one MP3 response share generation; final may contain an empty chunk. Client concatenates bounded chunks and plays complete MP3 through AVAudioPlayer after final. Maximum assembled MP3 is 24MiB; per socket frame maximum is 2MiB. No partial MP3 playback claim.

Local onset **always stops device playback immediately**, independent of network. In-flight chunks at/below the locally interrupted generation are discarded. Disconnect resets the new connection's generation state, drops old buffers, never replays uncertain requests, and does not send the tail of a microphone utterance that began while offline.

## Required control messages (gateway integration gap)

These are emitted by this iOS client but **not handled by the initial audio-only voice_gateway.receive()**. They must be supported before claiming App Intent text and device negotiation are end-to-end. Do not silently discard their meaning or route text to a second brain.

Client → server once after successful WS upgrade:

```json
{"type":"hello","device_id":"iphone-<stable UUID>","device_type":"iphone","sample_rate":16000,"channels":1,"encoding":"pcm_s16le","state":{"battery_level":0.6,"charging":false,"headphones":true,"headphones_connected":true,"headphones_worn":null,"screen_on":true,"screen_state_source":"app_foreground","app_foreground":true,"face_down":false,"orientation_source":"UIDevice.orientation","audio_outputs":["BluetoothHFP"],"protected_data_available":true,"observed_at":1792450000}}
```

State on change and every 15s while running; unknown values **must remain null**:

```json
{"type":"device_state","device_id":"iphone-<same UUID>","state":{"battery_level":null,"charging":false,"headphones_connected":false,"headphones_worn":null,"screen_on":null,"screen_state_source":"unavailable_public_api","app_foreground":false,"face_down":null,"orientation_source":"UIDevice.orientation","audio_outputs":[],"protected_data_available":false,"observed_at":1792450015}}
```

The actual full state also includes legacy `headphones` (same as headphones_connected). Headphone route is **not physical wearing**. Protected-data availability is **not lock state**. Background is **not screen off**. Orientation is last public UIDevice observation, not guaranteed continuous background sensor truth.

Optional StartJarvisIntent text parameter:

```json
{"type":"text","device_id":"iphone-<same UUID>","text":"<user input>"}
```

Route exactly once to the same BRAIN-mode timeline entry as final ASR transcripts. It can have real task side effects, so a successful send is not an acknowledgement; client must not replay it on reconnect.

Multi-device coordination server → client:

```json
{"type":"suppressed"}
{"type":"selected"}
```

Suppressed client stops playback and does not forward utterances. `selected`/`resumed` re-enable forwarding at the **next full voice onset**, not halfway through speech. Current defaults permit local response if no arbitration service is present; this is not proof of multi-device single-winner arbitration. Service-side authentication, identity binding, eligibility scoring, heartbeat expiry and one-winner enforcement remain server responsibilities.
