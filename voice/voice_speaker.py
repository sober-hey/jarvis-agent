"""声纹验证：判断一段 16 kHz PCM 是不是 Sir 本人在说。

用 sherpa-onnx 的中文说话人嵌入模型（3D-Speaker CAM++，192 维）。
模型文件 ~/.jarvis/models/campplus.onnx，参考声纹由 ~/.jarvis/speaker-owner.npy 缓存。

实测（2026-10-21，参考音 18s）：
  同一人的克隆音 0.56–0.62；女声（思思）0.03 ⇒ 阈值 0.40 有足够间隔。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

LOG = logging.getLogger(__name__)

MODEL_PATH = Path.home() / ".jarvis" / "models" / "campplus.onnx"
OWNER_PATH = Path.home() / ".jarvis" / "speaker-owner.npy"
REFERENCE_WAV = Path.home() / ".jarvis" / "speaker-reference.wav"
DEFAULT_THRESHOLD = 0.35
MIN_SECONDS = 1.0


class SpeakerVerifier:
    """Thread-safe wrapper: sherpa streams are per-call, the extractor is shared."""

    def __init__(
        self,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        model_path: Path = MODEL_PATH,
        owner_path: Path = OWNER_PATH,
    ) -> None:
        self.threshold = threshold
        self._model_path = model_path
        self._owner_path = owner_path
        self._lock = threading.Lock()
        self._extractor = None
        self._owner = None
        self._failed = False

    # -- model -------------------------------------------------
    def _load(self):
        if self._extractor is not None or self._failed:
            return self._extractor
        if not self._model_path.exists():
            LOG.warning("speaker_model_missing path=%s", self._model_path)
            self._failed = True
            return None
        try:
            import sherpa_onnx

            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(self._model_path))
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
            LOG.info("speaker_model_loaded path=%s", self._model_path)
        except Exception as exc:  # pragma: no cover - environment dependent
            LOG.warning("speaker_model_failed error_type=%s", type(exc).__name__)
            self._failed = True
            self._extractor = None
        return self._extractor

    @property
    def available(self) -> bool:
        return self._load() is not None

    # -- embeddings --------------------------------------------
    def embed(self, pcm: bytes, *, sample_rate: int = 16000):
        extractor = self._load()
        if extractor is None or not pcm:
            return None
        try:
            import numpy as np
        except Exception:  # pragma: no cover
            return None
        samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype("float32") / 32768.0
        if samples.size < int(MIN_SECONDS * sample_rate):
            return None
        with self._lock:
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate, samples)
            stream.input_finished()
            vector = extractor.compute(stream)
        if vector is None:
            return None
        return np.asarray(vector, dtype="float32")

    def _owner_embedding(self):
        if self._owner is not None:
            return self._owner
        try:
            import numpy as np
        except Exception:  # pragma: no cover
            return None
        if self._owner_path.exists():
            try:
                self._owner = np.load(self._owner_path)
                return self._owner
            except Exception:
                self._owner = None
        return None

    def enroll(self, wav_path: Path) -> bool:
        """Build the reference embedding from a 16 kHz mono WAV."""
        try:
            import wave

            import numpy as np
        except Exception:  # pragma: no cover
            return False
        try:
            with wave.open(str(wav_path), "rb") as handle:
                if handle.getframerate() != 16000:
                    return False
                pcm = handle.readframes(handle.getnframes())
        except Exception:
            return False
        vector = self.embed(pcm)
        if vector is None:
            return False
        self._owner_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(self._owner_path, vector)
        self._owner = vector
        LOG.info("speaker_enrolled path=%s", self._owner_path)
        return True

    def score(self, pcm: bytes) -> float | None:
        owner = self._owner_embedding()
        if owner is None:
            return None
        vector = self.embed(pcm)
        if vector is None:
            return None
        try:
            import numpy as np
        except Exception:  # pragma: no cover
            return None
        denominator = float(np.linalg.norm(vector) * np.linalg.norm(owner))
        if denominator <= 0:
            return None
        return float(vector @ owner) / denominator

    def is_owner(self, pcm: bytes) -> tuple[bool | None, float | None]:
        """None means "cannot tell" (no model, no reference, clip too short)."""
        if not self.available:
            return None, None
        value = self.score(pcm)
        if value is None:
            return None, None
        return value >= self.threshold, value


def ensure_reference(verifier: SpeakerVerifier | None = None) -> bool:
    """Enroll from the stored reference WAV if no cached embedding exists."""
    verifier = verifier or SpeakerVerifier()
    if verifier._owner_embedding() is not None:
        return True
    if not REFERENCE_WAV.exists():
        return False
    return verifier.enroll(REFERENCE_WAV)


def self_evaluate() -> dict[str, object]:
    """Offline declaration only; never loads the ONNX model, audio or reference."""
    return {
        "module": "voice_speaker",
        "domain": "owner speaker verification from one 16 kHz PCM clip (CAM++ embedding)",
        "current_version": "1.0.0",
        "known_limitations": [
            "A single enrolled reference and a cosine threshold, not authentication.",
            "Model and reference live in the owner's ~/.jarvis; a missing file returns "
            "'cannot tell' instead of rejecting the owner.",
            "Measured separation (owner clone 0.56-0.62, other speaker 0.03) is one session, "
            "not a calibrated false-accept rate.",
            "Clips shorter than 1 s are refused rather than guessed.",
            "No resident caller, mind provenance or KG projection is provided.",
        ],
        "improvement_directions": [
            "Recalibrate the threshold on more than one recording session.",
            "Record verification outcomes in the voice ledger with mind provenance.",
        ],
        "last_evaluated_at": None,
    }
