# Wake-word integration handoff

## Bounded integration

- `JarvisVoice/AudioCapture.swift` now owns the offline detector on the serial `app.jarvis.ios.capture` queue.
- Public integration surface for the parent is:
  - `capture.wakeEnabled = true/false` — synchronized with capture processing; default is `false` and should be set from foreground lifecycle.
  - `capture.onWake = { confidence in ... }` — model-backed `.wakeWord` only.
  - `capture.onWakeError = { message in ... }` — first inference error per capture start.
- Every converted mono Float32 block at exactly 16,000 Hz is sent to `WakeWordDetector` independently of `SpeechGate`; VAD idle does not suppress wake inference.
- Detector/scorer are constructed on the capture processing queue, not on the main thread. `start()` and `stop()` reset state; toggling `wakeEnabled` also resets timestamp and model state to avoid discontinuities.
- Model is CPU-only and receives consecutive 16 kHz mono samples. The scorer uses the existing 1280-sample / 80 ms non-overlapping model contract, with the existing 480-sample frontend tail and mel/embedding history.

## Copied implementation and resources

- `JarvisVoice/WakeWord/ActivationTypes.swift`
- `JarvisVoice/WakeWord/JarvisWakeWordScorer.swift`
- `JarvisVoice/WakeWord/WakeWordDetector.swift`
- `JarvisVoice/WakeWord/Models/Melspectrogram.mlpackage`
- `JarvisVoice/WakeWord/Models/SpeechEmbedding.mlpackage`
- `JarvisVoice/WakeWord/Models/JarvisClassifier.mlpackage`
- `JarvisVoice/WakeWord/OPENWAKEWORD-APACHE-2.0.txt`
- `JarvisVoice/WakeWord/CC-BY-NC-SA-4.0.txt`

The three package byte sizes match the source assets: 1,092,615; 1,355,181; and 860,735 bytes respectively. The package model metadata declares iOS 16.0 availability and the expected tensor shapes. The frontend/model provenance remains personal noncommercial only; the license files are bundled beside the copied implementation.

## Parent Xcode resource step

The project file was intentionally not changed. Add `JarvisVoice/WakeWord/Models` as a resource folder reference, or add all three `.mlpackage` directories to the app target's Copy Bundle Resources. The loader searches `Bundle.main` recursively, so either flat or nested resource layout works. Core ML may compile `.mlpackage` to `.mlmodelc` on first capture start; compilation and model construction happen on the capture queue. If the parent precompiles for iOS, the loader also accepts nested `.mlmodelc` resources.

No fallback to VAD, ASR, network, or canned dialogue exists. Missing resources fail `start()` with `CaptureError.wakeWordUnavailable`.

## Verification evidence

- iOS-target Swift typecheck passed with `xcrun swiftc -typecheck`, `arm64-apple-ios16.0`, SDK `iphoneos27.1`, `TMPDIR=/tmp`, and `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer` over the three WakeWord files, `AudioCapture.swift`, and `AudioPrimitives.swift`.
- Source wakeword iOS SDK build evidence exists at `apps/wakeword/evidence/ios-build-v2.log`; existing detector/scorer replay evidence is in `apps/wakeword/evidence/swift-main.json` and `apps/wakeword/evidence/final-offline-verification.json`.
- A direct local `coremlcompiler compile` probe of the copied packages was attempted, but this installed Xcode tool returned Cocoa error 513 while modifying the output `weights/weight.bin` path. This does not invalidate the copied package or the existing iOS SDK evidence; parent should verify final target resource compilation in Xcode/device build.
