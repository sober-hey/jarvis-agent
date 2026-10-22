# Module ③ integration contract

- Package directory: `/Volumes/扩展硬盘/jarvis/apps/mac/Modules/Activation/`; local SwiftPM package/product `JarvisActivation` or directly include `Sources/JarvisActivation/*.swift`.
- Entry: `try ActivationDetector(configuration: ..., scorer: ...)`.
- Unified callback: `onActivation: (ActivationEvent) -> Void`, kinds `.wakeWord`, `.doubleClap`, `.fingerSnap`; opt-in diagnostic `.voiceActivityFallback` must **not** be relabeled Jarvis recognition.
- Audio input: call `try detector.process(buffer: AVAudioPCMBuffer, timestamp: seconds)` or `process(samples: [Float], sampleRate: Double, timestamp: seconds)` on one serial non-realtime worker. Timestamp = monotonic start of the input block, not dispatch completion time.
- **Critical upstream constraint:** Feed the original microphone Float32 44.1/48kHz stream **before** conversion to 16kHz. The 8–12kHz snap band cannot exist in 16kHz audio (Nyquist=8kHz). `snapAvailable` is false if sample rate is too low. Never upsample 16kHz and claim snap support.
- The module installs no microphone taps; share the existing recorder's tap and bounded copied PCM buffers. Do not feed coalesced 3–10 second recorder notifications: that makes <500ms callbacks impossible.
- FFT+gestures run at capture rate. For CoreML, the internal streaming anti-aliased resampler supplies the scorer's declared sample rate. Model errors call `onError` and don't suppress gesture processing.
- No .mlmodel was supplied. Default wake status = `.unavailable`, gestures remain usable. `CoreMLWakeWordScorer` accepts explicit raw PCM fixed float32 tensor -> confidence contracts; models requiring mel features/state need a dedicated `WakeWordScoring` adapter.
- No callback dispatch to main by default. UI consumer should dispatch activation events to MainActor/main queue.
- Actual microphone permissions/recordings and human FPR acceptance are not claimed; see README and verification JSON.
