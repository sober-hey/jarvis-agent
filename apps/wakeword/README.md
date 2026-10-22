# JarvisWakeWord

个人非商业离线 Jarvis 单词检测原型。真实测试与限制见 [REPORT.md](REPORT.md)；**真实音乐误报 2.803/h，目前不宣称全天候可靠**。

## Swift 接入（Mac / iPhone）

将此目录作为 local Swift package，添加 `JarvisWakeWord` product。将以下三个 `.mlpackage` 加到宿主 app 的资源构建阶段，由 Xcode 为目标设备编译，或显式提供模型目录：

```
models/Melspectrogram.mlpackage
models/SpeechEmbedding.mlpackage
models/JarvisClassifier.mlpackage
```

```swift
import JarvisActivation
import JarvisWakeWord

let scorer = try JarvisWakeWordScorer(modelsDirectory: modelDirectory)
var config = WakeWordConfiguration()
config.inferenceHop = 0.08
config.confidenceThreshold = 0.8
let detector = WakeWordDetector(scorer: scorer, configuration: config)

// Feed consecutive mono Float32 PCM (-1...1), ALREADY resampled to 16kHz.
// Use the capture pipeline's stateful StreamingResampler, not per-block conversion.
// WakeWordDetector chunks but does NOT resample; it rejects other sample rates.
let events = try detector.process(samples: pcm16k,
                                  sampleRate: 16000,
                                  timestamp: monotonicSeconds)
// Only events of kind .wakeWord trigger wakeup, never .voiceActivityFallback.

// Capture discontinuity, restart, large gap, or new user/audio stream:
scorer.reset()
detector.reset()
```

**One serial worker owns scorer+detector; not safe for concurrent scoring.** `score(samples:)` directly expects exactly **1280 new, non-overlapping, 16kHz mono samples**. It already stores past mel/embedding context; never feed it overlapping sliding windows. Default CPU-only compute is verified on Mac. Changing compute units requires fresh parity/performance validation.

Models are deliberately not implicit package resources: target-device model compilation must be owned by the host app. Failing to bundle/load any package throws instead of falling back to VAD. On-device inference code contains no HTTP, secret, ASR or TTS dependency.

## Watch

The library cross-compiles for watchOS. `MLModel.compileModel(at:)` is unavailable there, so **bundle three precompiled `.mlmodelc` directories**. Do not runtime-compile `.mlpackage` on Watch. Example target compilation on this Mac:

```sh
env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcrun coremlcompiler compile models/Melspectrogram.mlpackage models/compiled \
  --platform watchos --deployment-target 9.0
# Repeat for SpeechEmbedding / JarvisClassifier.
```

Watch currently exports a same-shaped PCM scorer protocol without importing Mac Activation (whose generic loader does not compile for Watch). This is a scorer, not a new background microphone entitlement. Watch hardware execution, lockscreen/background availability and power draw remain NOT_RUN.

## Build / replay saved real data

From this directory:

```sh
env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  swift build -c release

# Choose a NEW output path for each run to preserve old evidence.
env TMPDIR=/tmp DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  .build/release/wakeword-replay "$PWD" "$PWD/data/standalone.json" \
  "$PWD/evidence/my-new-replay.json"
```

Real replay reads WAVs from `data/`; all those files exist locally at delivery, but bulky corpus files are gitignored. Check manifests and hashes before rerunning. `data/stress.json`, `data/musan.json`, `data/perturbed.json` select other saved sets. Runtime scores and actual latency are serialized to the supplied report path.

## Rebuild pipeline

Python 3.11 venv isolated to this directory (parent project Python version may differ):

```sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.lock
.venv/bin/python scripts/download_models.py
```

`download_models.py` removes proxy variables and uses GitHub release assets through `https://ghproxy.net/` with resumable curl. Corpus scripts call paid DashScope using the key **only** from `~/.jarvis/dashscope.key`; do not print it. Network generation is a preparation step, never an inference requirement.

Scripts in order: `generate_tts.py`, `generate_standalone.py`, `pipeline.py`, `verify_upstream.py`, `convert.py`, `train.py`, then native `wakeword-replay`. Signal/augmentation and MUSAN tests use `generate_stress.py`, `generate_perturbed.py`, `extract_musan.py`. `verify_ablation.py` exercises a zeroed head negative control; `aggregate.py` derives counts and hashes programmatically.

**Before rerunning generation/training/aggregation, copy existing evidence/manifests to a new run folder.** Some exploratory scripts use fixed filenames and will overwrite those targets. Do not merge new data into the frozen holdout, retrain on test music, or claim derived variants are independent speakers. TTS regenerations are not bitwise reproducible server-side; saved WAV hashes anchor the actual experiment. Torch seed is fixed at 309.

MUSAN download used, with proxy removed and `HF_ENDPOINT=https://hf-mirror.com`:

```
https://hf-mirror.com/datasets/corypaik/musan/resolve/main/music/train-00000-of-00010.parquet
https://hf-mirror.com/datasets/alexwengg/musan_mini50/resolve/main/noise/noise-free-sound-0083.wav
```

## License / attribution

openWakeWord code: dscripka contributors, Apache-2.0. Original `hey_jarvis` pretrained model: CC BY-NC-SA 4.0; this file remains a baseline only, not the deployed custom head. Shared frontend artifact licensing has unresolved upstream questions; **personal noncommercial experimentation only**, no cleared commercial redistribution claim. See full licenses and sources in `research/`.

The frontend conversion is a faithful architectural/weight translation; classifier structure and frozen-embedding approach derive from openWakeWord. Self-training does not remove upstream/frontend provenance obligations. MUSAN: David Snyder, Guoguo Chen, Daniel Povey, *MUSAN: A Music, Speech, and Noise Corpus*, arXiv:1510.08484, dataset CC BY 4.0 with per-source music rights requiring separate review. Evaluation audio is not part of the product bundle.
