# Sources and retrieved evidence

Retrieved 2026-10-20. The `*.txt` files in this directory are the actual licence texts retrieved from the URLs below. Ellipses in this index are only presentation; the files are unabridged HTTP payloads as retrieved.

## openWakeWord

- Code repository: https://github.com/dscripka/openWakeWord
- Code licence: https://raw.githubusercontent.com/dscripka/openWakeWord/main/LICENSE
  - Saved as `openwakeword-code-Apache-2.0.txt`.
- README (code/model distinction): https://raw.githubusercontent.com/dscripka/openWakeWord/main/README.md
- Official model repository/readme (CC BY-NC-SA declaration): https://huggingface.co/davidscripka/openwakeword/raw/main/README.md
- `hey_jarvis` model description and training-data provenance: https://github.com/dscripka/openWakeWord/blob/main/docs/models/hey_jarvis.md
- Open unresolved shared-feature licence question: https://github.com/dscripka/openWakeWord/issues/348
- Feature implementation/provenance in source: https://github.com/dscripka/openWakeWord/blob/main/openwakeword/utils.py
- Creative Commons legal code: https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode.txt
  - Saved as `CC-BY-NC-SA-4.0-legalcode.txt`.

## Porcupine / Picovoice

- Official Porcupine introduction, AccessKey requirement, supported platforms, custom `.ppn` platform-specific models: https://picovoice.ai/docs/porcupine/
- Official general FAQ (account/product context): https://picovoice.ai/docs/faq/general
- Official model API (AccessKey authentication and 403 behavior): https://picovoice.ai/docs/model-api/porcupine
- Public repository: https://github.com/Picovoice/porcupine
- Public repository licence payload: https://raw.githubusercontent.com/Picovoice/porcupine/master/LICENSE
  - Saved as `porcupine-repo-Apache-2.0.txt`.
- Official product page: https://picovoice.ai/products/voice/wake-word/
- Pricing/contact page: https://picovoice.ai/pricing/

## Alternatives

- sherpa-onnx repository: https://github.com/k2-fsa/sherpa-onnx
- sherpa-onnx licence: https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/master/LICENSE
  - Saved as `sherpa-onnx-Apache-2.0.txt`.
- sherpa-onnx README (keyword spotting, APIs/platform list, model release links): https://github.com/k2-fsa/sherpa-onnx/blob/master/README.md
- microWakeWord training framework: https://github.com/OHF-Voice/micro-wake-word
- microWakeWord framework licence: https://raw.githubusercontent.com/OHF-Voice/micro-wake-word/main/LICENSE
  - Saved as `microWakeWord-Apache-2.0.txt`.
- ESPHome model repository and its explicit model-weight licence statement: https://github.com/esphome/micro-wake-word-models
- ESPHome model licence: https://raw.githubusercontent.com/esphome/micro-wake-word-models/main/LICENSE
  - Saved as `micro-wake-word-models-Apache-2.0.txt`.

## Apple official documentation

- Core ML overview: https://developer.apple.com/documentation/coreml
- `MLModel` loading, compiling and prediction: https://developer.apple.com/documentation/coreml/mlmodel
- Apple API availability example: `prediction(from:)` lists watchOS 4.0+: https://developer.apple.com/documentation/coreml/mlmodel/2880280-prediction
- watchOS background execution overview: https://developer.apple.com/documentation/watchkit/background-execution
- Background tasks: https://developer.apple.com/documentation/watchkit/using-background-tasks
- Extended runtime sessions and their types/time limits: https://developer.apple.com/documentation/watchkit/using-extended-runtime-sessions
- Background audio: https://developer.apple.com/documentation/watchkit/playing-background-audio
- Background modes: https://developer.apple.com/documentation/bundleresources/information-property-list/wkbackgroundmodes
- AVAudioSession: https://developer.apple.com/documentation/avfaudio/avaudiosession
- Microphone permission: https://developer.apple.com/documentation/avfaudio/avaudiosession/requestrecordpermission(_:)
- Built-in microphone port (watchOS availability): https://developer.apple.com/documentation/avfaudio/avaudiosessionportbuiltinmic
- Workout session: https://developer.apple.com/documentation/healthkit/hkworkoutsession

## Retrieval caveats

- Search/extraction was used for current page text; URLs above are the authority to re-open. GitHub API calls for stars/forks/latest commit were attempted and returned HTTP 403 rate-limit exceeded, so no popularity numbers are included.
- Apple Developer Forums were not used as primary evidence for a hard platform claim. The conclusions above rely on Apple documentation pages and explicitly separate Core ML prediction availability from background microphone lifetime.
