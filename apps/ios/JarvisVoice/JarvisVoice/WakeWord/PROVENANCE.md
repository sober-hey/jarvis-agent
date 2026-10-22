# Offline Jarvis wake-word model provenance

These three Core ML packages are copied from the existing offline implementation in `apps/wakeword/models/` without retraining or modification:

- `Melspectrogram.mlpackage`: openWakeWord-derived frontend; source release metadata and SHA-256 are recorded in `apps/wakeword/evidence/model-sha256.json`.
- `SpeechEmbedding.mlpackage`: openWakeWord/Google speech-embedding-derived frontend; source metadata and SHA-256 are recorded in the same manifest.
- `JarvisClassifier.mlpackage`: the existing Jarvis-only trained classification head.

Source implementation and evaluation: `apps/wakeword/Sources/JarvisWakeWord/JarvisWakeWordScorer.swift`, `apps/wakeword/README.md`, and `apps/wakeword/REPORT.md`.

Attribution and license material copied beside this file:

- `OPENWAKEWORD-APACHE-2.0.txt` — openWakeWord code attribution, Apache-2.0.
- `CC-BY-NC-SA-4.0.txt` — Creative Commons legal text relevant to the upstream model lineage.

The existing source states that the frontend/shared model licensing has unresolved upstream questions and that the deployed artifact is for personal noncommercial experimentation only. Do not present these resources as commercially cleared or as a general-purpose speech recognizer. No ASR, network service, secret, or VAD fallback is part of this integration.
