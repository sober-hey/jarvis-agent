# Offline Jarvis wake-word research

**Retrieved:** 2026-10-20 (CST). Scope is offline inference and distributable licensing; this is engineering research, not legal advice.

## Decision summary

1. **Best low-risk default for a commercial Jarvis:** train and ship a new classifier/model from data with documented commercial permissions, using an Apache-2.0 runtime such as openWakeWord code, sherpa-onnx, or microWakeWord. Do **not** ship openWakeWord's bundled pretrained `hey_jarvis` model without a separate legal review/licence.
2. **`hey_jarvis` is not a bare `Jarvis` model.** Its official model document says it detects the phrase “hey jarvis”; similar phrases such as “jarvis” may work but likely have higher false-reject rates. Treat bare “Jarvis” as a new model/training/evaluation requirement, not a supported alias.
3. **Porcupine is technically strong and offline at inference, but is not a self-contained permissive stack.** The official SDK requires a valid Picovoice AccessKey and checks account limits. Custom `.ppn` files are platform-specific. The public repository’s code license is Apache-2.0, but the operational AccessKey/account and Picovoice model/service terms remain prerequisites; obtain current commercial terms before product shipment.
4. **watchOS can run Core ML predictions, but watchOS is not an always-on arbitrary background microphone platform.** Apple documents Core ML model prediction availability on watchOS and separately documents narrowly scoped background execution. Background sessions cover workouts, location, or audio playback; extended runtime sessions are time-limited and type-specific. A continuous wake-word listener must be treated as foreground/session-limited until proven on target hardware and approved entitlements. Do not use silent audio or a fake workout as a keep-alive.

## Option matrix

| Option | Offline inference | Code/runtime licence | Model/licence caveat | Jarvis fit |
|---|---|---|---|---|
| **openWakeWord** | Yes; ONNX/TFLite CPU path, 16-kHz audio pipeline | Repository code: Apache-2.0 | Official pretrained models, including `hey_jarvis`, are CC BY-NC-SA 4.0 because training data includes unknown/restrictive licensing. The shared `melspectrogram`, `embedding_model`, and `silero_vad` artefact provenance/licensing is not clearly resolved by the project: issue #348 is open. | Good for a self-trained model; unsuitable to ship bundled pretrained model in a revenue product without permission. |
| **Porcupine** | Yes, on-device | Public repo LICENSE says Apache-2.0 for the repo; product use requires AccessKey | AccessKey is authentication/authorization and usage-limit enforcement. Custom `.ppn` is platform-specific. Official supported list includes embedded, Raspberry Pi, Android/iOS, web, Linux, macOS, Windows; it does not list watchOS. | Good for Mac/iOS/embedded if current Picovoice terms and key lifecycle are acceptable; watchOS requires explicit vendor/platform confirmation. |
| **sherpa-onnx KWS** | Yes, local ONNX | Apache-2.0 repository | Model files are separate release assets; audit each KWS model and upstream dataset licence before redistribution. Official project advertises keyword spotting and Swift/iOS plus WearOS support; this is not proof of watchOS support. | Strong cross-platform alternative, especially if we control model provenance. |
| **microWakeWord** | Yes; TensorFlow Lite Micro target | Framework Apache-2.0 | The ESPHome `micro-wake-word-models` repository explicitly says its listed `.tflite` weights/manifests are Apache-2.0, with no separate model terms in that repository. Audit each model and third-party wake-word name. | Excellent for microcontrollers/low power; not the first choice for Watch unless a compatible Swift/TFLite-Micro path is built and measured. |

## Exact licensing findings

### openWakeWord: code versus models

- `openwakeword` repository README: **all code** is Apache 2.0.
- Hugging Face official model repository README front matter: `license: cc-by-nc-sa-4.0`; it states all official pretrained models use CC BY-NC-SA 4.0 due to unknown/restrictive training data.
- CC BY-NC-SA 4.0 legal text grants reproduction/sharing and adaptations **for NonCommercial purposes only**, requires attribution, and requires ShareAlike for adapted material. The retrieved full legal text is in `CC-BY-NC-SA-4.0-legalcode.txt`.
- Apache 2.0 requires retaining the licence/attribution notices and marking changed files; the retrieved full text is in `openwakeword-code-Apache-2.0.txt`.
- The README's broad “code Apache” statement does not by itself settle the licence of every shipped binary. Issue [#348](https://github.com/dscripka/openWakeWord/issues/348) specifically asks about `melspectrogram.onnx`, `embedding_model.onnx`, and `silero_vad.onnx`; it was retrieved as **open**, so do not present those shared artefacts as cleared for commercial redistribution. `utils.py` identifies the embedding as a reimplementation of Google's Apache-2.0 `speech_embedding` and the mel model as an ONNX implementation of Torch's function, but provenance is not the same as a complete downstream licence grant.
- The `hey_jarvis` model document identifies training sources including ACAV100M, FSD50k, Free Music Archive, BIRD impulse responses, and synthetic TTS. Their individual licences and dataset terms must not be inferred from the runtime licence.

### Porcupine

- The retrieved public repository `LICENSE` is the Apache License 2.0 text. This supports Apache treatment of the repository-covered code, not a conclusion that Picovoice's complete commercial product/model terms are Apache.
- Official Porcupine docs say every Picovoice user needs a **valid AccessKey**, which authenticates/authorizes use and verifies account limits; it must remain secret. Console signup is described as free/no card, but “free signup” is not a perpetual production entitlement.
- Official docs list platform support but not watchOS. They also say a custom wake-word `.ppn` is trained for a selected platform and a model trained for one platform cannot run on another. Get written confirmation for watchOS and the intended distribution model before choosing it.

## watchOS/Core ML constraints

- Apple’s `MLModel` documentation covers loading compiled model assets and prediction, and Apple API availability pages show `MLModel` prediction APIs on watchOS (for example, watchOS 4/5 depending on API). Therefore a compact Core ML feature/classifier pipeline is technically plausible on Watch.
- Apple’s watchOS background-execution documentation says apps primarily run foreground; background refresh receives only a short amount of time. It lists background sessions for **audio, location updates, remote notifications, or workouts** rather than a general always-on microphone mode.
- Apple’s extended-runtime documentation says ordinary apps become suspended when the wrist is lowered; background sessions are limited to monitoring workouts, tracking location, or playing audio files, while extended runtime sessions are type-specific and time-limited. The documented limits include physical therapy (background, 1 hour) and smart alarm (background, 30 minutes); this is not an unlimited hotword daemon.
- Apple’s background-audio documentation is about playback and says watchOS requires Bluetooth audio for long-form audio. It is not evidence that an app may keep its microphone and classifier alive indefinitely.
- `AVAudioSession` documents microphone permission and a built-in microphone port on watchOS. Permission/input availability is distinct from background lifetime. The implementation must measure foreground, active workout, and inactive/background behavior on the target Watch and report each separately.

## Recommended architecture

- **Mac/iPhone:** use a self-trained openWakeWord classifier or sherpa-onnx KWS with a frozen, documented feature pipeline; keep all models local. Use a short post-detection capture window for ASR rather than continuously uploading audio.
- **Watch:** start with explicit user action or a bounded active session. If hands-free wake is required while the wrist is down, make the phone/Mac the always-on listener and use Watch as a client/secondary trigger until a real-device watchOS entitlement/runtime experiment proves otherwise.
- **Licensing gate:** maintain a model manifest with source URL, commit/release, SHA-256, exact model licence, dataset licences, attribution/NOTICE requirements, and whether commercial redistribution is allowed. Do not put the downloaded pretrained openWakeWord models or unresolved shared feature artefacts into the product bundle.

## What was not established

- GitHub API star/fork/latest-commit collection was attempted but GitHub API returned HTTP 403 rate-limit exceeded; no numbers are claimed here.
- No large model was downloaded. No accuracy, latency, battery, or Watch background claim has been hardware-validated.
- Porcupine’s current production pricing/free-tier duration and a watchOS binary/runtime guarantee were not found in authoritative public docs; treat both as vendor-confirmation items.
