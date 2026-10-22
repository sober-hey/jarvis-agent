# JARVIS voice gateway

## Start and ports

Install dependencies once, then run under launchd:

```sh
uv pip install --python .venv/bin/python3 -r packaging/requirements-voice-gateway.txt
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jarvis.voice-gateway.plist
launchctl kickstart -k gui/$(id -u)/com.jarvis.voice-gateway
launchctl bootout gui/$(id -u)/com.jarvis.voice-gateway
```

The launchd job runs `/Users/sober/.jarvis/run_voice_gateway.sh` from the project root,
with KeepAlive and RunAtLoad. Its output and errors go to `~/.jarvis/voice-gateway.log`.
The script passes `--token-file ~/.jarvis/voice-gateway.token`; the token must be an
owner-owned regular file with mode 0600 and one non-whitespace ASCII line. It is never
printed or logged. `--config` remains the existing `~/.jarvis/config.json`.

At startup the gateway binds exactly `127.0.0.1:8765`, `100.67.229.1:8765`, and the
current private IPv4 address read from `en0` (currently `192.168.1.5`). A missing
Tailscale or en0 address is skipped; loopback remains required. No wildcard bind is
allowed. Every non-loopback listener requires the Bearer token; wrong/missing/duplicate
headers receive HTTP 401 before microphone admission. This exposes the LAN and tailnet
interfaces, so the token is the access boundary; use the same token in native clients
and do not use a cloud/model key.

8765 is now reserved for iPhone/Mac/Watch voice. `web_server.py` defaults to
**8767** (8766 already belongs to `outreach_server.py`). All explicitly supplied
Web ports, including `--port 0`, retain their meaning; no live process is stopped
or migrated. Existing wrappers that pass `--port 8765` must be changed explicitly
to `--port 8767` before concurrently starting voice. Web uses relative browser
API/SSE URLs; actual-port Origin/authentication/CSRF behavior remains unchanged.
`voice_live.TimelineClient` is always passed an explicit base URL; for a running
Web use `--timeline-url http://127.0.0.1:8767`. This POST is never retried.
Existing acceptance/soak tools already use explicit or ephemeral ports.

Defaults: speech reads owner-only `~/.jarvis/dashscope.key` through
`voice_dashscope`; brain reads existing `~/.jarvis/config.json` and `api.key`.
Speech uses emotion-aware REST ASR and custom-voice MP3 TTS, off the event loop.
Native async `DashScopeSpeech` remains available/tested for injected legacy users.
The single timeline is `~/.jarvis/loop-data`, shared with Web and the daemon.
Brain goes through `voice_port.BrainVoiceSink` → existing launcher/chat entry,
with `source_channel="voice"`; no second personality or brain is instantiated.
No model configuration, launch agent, resident service or live timeline is changed
by installing/testing this module.

## Client wire protocol

Canonical iOS contract: `apps/ios/JarvisVoice/PROTOCOL.md`.

```json
{"type":"hello","device_id":"iphone-<stable UUID>","device_type":"iphone","sample_rate":16000,"channels":1,"encoding":"pcm_s16le","state":{"headphones_connected":true,"headphones_worn":null,"screen_on":null,"face_down":null}}
{"type":"device_state","device_id":"iphone-<same UUID>","state":{"headphones_connected":false,"headphones_worn":null,"app_foreground":false,"screen_on":null}}
{"type":"audio","data":"<base64 raw PCM>","format":"pcm_s16le","sample_rate":16000}
{"type":"audio_end"}
{"type":"text","device_id":"iphone-<same UUID>","text":"Intent input"}
{"type":"playback_done","generation":1}
```

Hello is mandatory on authenticated connections. Identity binds once per socket;
duplicate connected IDs and mismatched state/text IDs are rejected. Allowed kinds
are iphone/watch/mac/xiaoai. Unknown telemetry stays null: headphone connection is
not wearing, foreground is not screen-on, protected-data availability is not lock.
State updates do not flush or reset microphone utterances.

State freshness expires after 45s using **server monotonic receive time**, not
client timestamps. `device_routing.arbitrate()` scores eligible connected snapshots
and deterministically selects one winner; all others receive `suppressed` and are
blocked from audio/text/brain/TTS. Winner gets `selected`. An admitted utterance
holds its owner through ASR/brain/TTS/playback while eligible and fresh, preventing
mid-speech handoff. Suppressed buffers are discarded. Client must wait for the next
complete onset after selection. No claim of hardware wake synchronization/calibrated
DOA: absent signal/DOA is conservatively unscored; physical wear is never invented.

PCM must be mono signed-int16 little-endian, 16kHz, **not WAV**. Explicit audio_end
or the configured 500ms transport gap bounds an utterance; control heartbeats never
count as audio. Maximum 30s; invalid/odd/empty/rate-mismatched audio is rejected.
Per-client utterance queue, client count and provider in-flight calls are bounded.
Interrupted/disconnected REST worker calls retain their slot until they really end.

```json
{"type":"selected"}
{"type":"suppressed"}
{"type":"text","role":"user","text":"recognized","final":true,"generation":1}
{"type":"text","role":"assistant","text":"answer","final":true,"generation":1}
{"type":"audio","format":"mp3","data":"<base64>","final":false,"generation":1}
{"type":"audio","format":"mp3","data":"","final":true,"generation":1}
{"type":"interrupt","generation":2}
```

MP3 chunks share a generation and are bounded to 24MiB in total. Concatenate before
native playback. New valid input advances generation, cancels old TTS forwarding
and suppresses stale brain output. Submitted brain calls finish exactly once and
retain timeline records; they are never cancelled/replayed/retried. Errors include
`invalid_audio`, `invalid_control`, `hello_required`, `busy`,
`brain_provider_rejected` (with integer `provider_status` when the launcher returned
`model_request_state="provider_rejected"`), `brain_outcome_unknown`, `turn_failed` and
tts_failed. Provider rejection is not retried or spoken as an answer.

## Ledger and status

Every admitted final transcript is appended to the existing `voice_user` timeline,
including ASR `emotion` (null if unknown), confidence, device and audio duration.
Answers, generation time and TTS outcomes append through VoiceLedger's same MindStore.
Unknown synthesized MP3 duration stays null, never synthesis elapsed time.
Interruption is a separate linked observation, never an invented playback end.
`playback_done` alone appends a **reported_end** observation: it proves only the
client callback, not that the server observed a playback start or heard audio.
An optional real `playback_started` callback permits full start/end reconstruction.
No fake playback starts are inferred from sending MP3 bytes.

Private atomic `gateway-status.json` in the shared data root is updated on start,
stop, calls and heartbeat. Web `/api/voice/status` reads it through
`read_gateway_status(root)`; no key/network probe. Running requires fresh heartbeat
and a live PID. Bailian validity starts null and becomes true only on an actual
successful vendor operation, false on invalid_key. Missing snapshot remains
not_connected/unknown; insecure/malformed snapshots fail visibly. Logs omit PCM,
transcripts, tokens and model keys.

## Verification

```sh
TMPDIR="$HOME/.jarvis/tmp-test" PYTHONPATH=packaging .venv/bin/python3 -m pytest \
  packaging/test_voice_gateway.py packaging/test_voice_gateway_integration.py \
  packaging/test_voice_dashscope.py packaging/test_device_routing.py \
  packaging/test_voice_xiaoai.py packaging/test_voice_ledger.py \
  packaging/test_voice_registry_web.py packaging/test_web_voice.py -q
.venv/bin/python3 -m ruff check packaging/voice_gateway.py packaging/test_voice_gateway_integration.py
.venv/bin/python3 -m pyright packaging/voice_gateway.py
```

Fixtures use real WebSocket sockets, fake ASR/TTS/brain, isolated MindStore, and a
fixture-only token. They do not spend API credit or establish live vendor validity,
phone background recording, physical speaker playback or resident deployment.
The Python fixture run binds both exact private addresses and then closes them.
Do not run Python product-root tests with TMPDIR=/tmp: the root security gate
correctly rejects its shared parent. Use a private owner-only test temp directory.
