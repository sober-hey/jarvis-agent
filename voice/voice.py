"""File-only speech: injectable ASR/TTS, local lifelog reuse, no recording/playback.

Quick start (from packaging): ``speak(text, 'sample.wav'); transcribe('sample.wav')``.
The default ASR reuses lifelog's sherpa-onnx models and interpreter read-only. No cloud
backend, credential discovery, network requests or package/model downloads occur.

Configuration: JARVIS_VOICE_LIFELOG_ROOT, JARVIS_VOICE_LIFELOG_PYTHON,
JARVIS_VOICE_LIFELOG_HELPERS, JARVIS_VOICE_FFMPEG; VoiceConfig controls say voice/rate.
Backends can be injected with ASRBackend / TTSBackend protocols. Select before speaking
to inspect the TTS backend's name; speak intentionally returns just the published path.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol, TypedDict, cast

SAMPLE_RATE = 16000
DEFAULT_LIFELOG = Path("/Volumes/扩展硬盘/hermes/lifelog")
DEFAULT_HELPERS = Path("/Volumes/扩展硬盘/hermes/.hermes/scripts")
DEFAULT_MALE_VOICE = "Grandpa (中文（中国大陆）)"


def self_evaluate() -> dict[str, Any]:
    """Report implemented file-speech boundaries, without probing local assets."""
    from datetime import UTC, datetime

    return {
        "module": "voice",
        "domain": "local file speech recognition and sentence-stream speech synthesis",
        "current_version": "injectable file ASR/TTS; bounded PCM chunks and no-clobber sentence WAV publishing",
        "known_limitations": [
            "Default lifelog ASR reloads its model in a subprocess for every chunk of at most 30 seconds; chunks run sequentially.",
            "Default ASR segments are chunk boundaries; TTS timing is sentence audio duration, not word, phoneme or viseme alignment.",
            "Default TTS requires macOS say, an installed configured voice and ffmpeg; ASR needs preinstalled lifelog helpers/models, with no downloads.",
            "This module does not record or play audio; completed sentence files survive stream close/failure and cleanup belongs to the consumer.",
        ],
        "improvement_directions": [
            "Add an injectable persistent local recognizer to avoid per-chunk interpreter/model reloads while preserving bounded inputs.",
            "Offer backend-supplied word/phoneme alignment with explicit timing provenance instead of inferring it from sentence duration.",
            "Provide an explicit consumer cleanup helper for retained sentence WAVs and sidecars without deleting unrelated files.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


class VoiceError(RuntimeError):
    """Speech failed. Messages contain stage/backend, never audio/text/provider payloads."""


class BackendUnavailable(VoiceError):
    """No selected backend can run in the current environment."""


class Segment(TypedDict):
    start: float
    end: float
    text: str


class Transcript(TypedDict):
    text: str
    language: str
    duration: float
    segments: list[Segment]
    backend: str
    latency_seconds: float


@dataclass(frozen=True)
class SpeechChunk:
    """Playable sentence WAV and synthetic audio-timeline bounds, never wall-clock cues."""

    path: str
    index: int
    duration: float
    start: float
    end: float
    backend: str
    ready_latency_seconds: float
    synthesis_latency_seconds: float
    timing_kind: str = "sentence_audio_boundary"


@dataclass(frozen=True)
class ChunkResult:
    text: str
    language: str
    duration: float
    segments: list[Segment] = field(default_factory=lambda: list[Segment]())


@dataclass(frozen=True)
class VoiceConfig:
    """macOS say voice and words/minute; slower Chinese male default, no voice cloning."""

    voice: str = DEFAULT_MALE_VOICE
    rate: int = 155

    def __post_init__(self) -> None:
        if not self.voice.strip() or not 80 <= self.rate <= 400:
            raise ValueError("voice must be nonempty and rate must be 80..400 words/minute")


class ASRBackend(Protocol):
    @property
    def name(self) -> str: ...

    def available(self) -> bool: ...

    def transcribe_chunk(self, audio_path: Path) -> ChunkResult: ...


class TTSBackend(Protocol):
    @property
    def name(self) -> str: ...

    def available(self) -> bool: ...

    def synthesize(self, text: str, out_path: Path, config: VoiceConfig) -> None: ...


def _ffmpeg() -> str:
    configured = os.environ.get("JARVIS_VOICE_FFMPEG")
    candidate = configured or shutil.which("ffmpeg")
    if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
        return candidate
    raise BackendUnavailable("ffmpeg unavailable; set JARVIS_VOICE_FFMPEG to an executable")


def _run(
    args: list[str], stage: str, *, timeout: float = 300.0, input_text: str | None = None
) -> str:
    try:
        result = subprocess.run(
            args, input=input_text, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VoiceError(f"{stage} failed ({type(exc).__name__})") from None
    if result.returncode:
        # Raw stderr can contain audio paths, synthesis text, or future provider secrets.
        raise VoiceError(f"{stage} failed (exit {result.returncode}); payload suppressed")
    return result.stdout


def _is_pcm16k(source: Path) -> bool:
    try:
        with wave.open(str(source)) as stream:
            return (
                stream.getframerate() == SAMPLE_RATE
                and stream.getnchannels() == 1
                and stream.getsampwidth() == 2
                and stream.getcomptype() == "NONE"
            )
    except (wave.Error, EOFError):
        return False


def _normalize(source: Path, destination: Path) -> Path:
    if _is_pcm16k(source):
        return source
    _run(
        [
            _ffmpeg(),
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-i",
            str(source),
            "-vn",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        "audio conversion",
    )
    return destination


@dataclass(frozen=True)
class LifelogASR:
    """Read-only process adapter around existing lifelog_lib helpers/model factory.

    Each bounded chunk starts an isolated interpreter; the model is reloaded per chunk.
    SenseVoice is preferred; Paraformer is a Chinese-only availability fallback.
    """

    model: str = "sensevoice"
    root: Path = field(
        default_factory=lambda: Path(
            os.environ.get("JARVIS_VOICE_LIFELOG_ROOT", str(DEFAULT_LIFELOG))
        )
    )
    python: Path | None = None
    helpers: Path = field(
        default_factory=lambda: Path(
            os.environ.get("JARVIS_VOICE_LIFELOG_HELPERS", str(DEFAULT_HELPERS))
        )
    )
    timeout: float = 300.0

    @property
    def name(self) -> str:
        return f"lifelog-{self.model}"

    @property
    def interpreter(self) -> Path:
        return self.python or Path(
            os.environ.get("JARVIS_VOICE_LIFELOG_PYTHON", str(self.root / "venv/bin/python"))
        )

    def available(self) -> bool:
        models = self.root / "models"
        directories = {
            "sensevoice": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
            "paraformer": "paraformer-int8",
        }
        directory = directories.get(self.model)
        if directory is None:
            return False
        required = [
            self.helpers / "lifelog_lib.py",
            models / directory / "model.int8.onnx",
            models / directory / "tokens.txt",
        ]
        if (
            not self.interpreter.is_file()
            or not os.access(self.interpreter, os.X_OK)
            or not all(path.is_file() for path in required)
        ):
            return False
        try:
            probe = subprocess.run(
                [
                    str(self.interpreter),
                    "-B",
                    "-c",
                    "import importlib.util,sys;sys.exit(0 if all(importlib.util.find_spec(n) "
                    "for n in ['sherpa_onnx','numpy']) else 1)",
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return probe.returncode == 0

    def transcribe_chunk(self, audio_path: Path) -> ChunkResult:
        output = _run(
            [
                str(self.interpreter),
                "-B",
                str(Path(__file__).resolve()),
                "--lifelog-worker",
                str(audio_path),
                "--model",
                self.model,
                "--root",
                str(self.root),
                "--helpers",
                str(self.helpers),
            ],
            self.name,
            timeout=self.timeout,
        )
        try:
            data = cast(dict[str, object], json.loads(output))
            text, language, duration = data["text"], data["language"], data["duration"]
            if not isinstance(text, str) or not isinstance(language, str):
                raise ValueError("invalid text/language")
            if not isinstance(duration, (float, int)):
                raise ValueError("invalid duration")
            rows: list[Segment] = []
            raw = data["segments"]
            if not isinstance(raw, list):
                raise ValueError("invalid segments")
            for item in cast(list[object], raw):
                if not isinstance(item, dict):
                    raise ValueError("invalid segment")
                row = cast(dict[str, object], item)
                start, end, value = row["start"], row["end"], row["text"]
                if (
                    not isinstance(start, (float, int))
                    or not isinstance(end, (float, int))
                    or not isinstance(value, str)
                ):
                    raise ValueError("invalid segment fields")
                rows.append(Segment(start=float(start), end=float(end), text=value))
            return ChunkResult(
                text=text, language=language, duration=float(duration), segments=rows
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise VoiceError(
                f"{self.name} returned invalid response ({type(exc).__name__})"
            ) from None


@dataclass(frozen=True)
class SayTTS:
    """Local synthesis to file via macOS say + ffmpeg; never plays the audio."""

    name: str = "macos-say"

    def available(self) -> bool:
        return sys.platform == "darwin" and shutil.which("say") is not None

    def synthesize(self, text: str, out_path: Path, config: VoiceConfig) -> None:
        executable = shutil.which("say")
        if executable is None:
            raise BackendUnavailable("macos-say unavailable")
        listing = _run([executable, "-v", "?"], "voice listing")
        if not any(line.startswith(config.voice + " ") for line in listing.splitlines()):
            raise BackendUnavailable("configured macos-say voice is not installed")
        with TemporaryDirectory(prefix="jarvis-voice-say-") as root:
            aiff = Path(root) / "speech.aiff"
            _run(
                [
                    executable,
                    "-v",
                    config.voice,
                    "-r",
                    str(config.rate),
                    "-o",
                    str(aiff),
                    "-f",
                    "-",
                ],
                "macos-say synthesis",
                input_text=text,
            )
            _run(
                [_ffmpeg(), "-nostdin", "-v", "error", "-n", "-i", str(aiff), str(out_path)],
                "speech encoding",
            )


def select_asr(candidates: Sequence[ASRBackend] | None = None) -> ASRBackend:
    pool = candidates if candidates is not None else [LifelogASR(), LifelogASR(model="paraformer")]
    for backend in pool:
        if backend.available():
            return backend
    raise BackendUnavailable(
        "No available ASR backend: install/configure local lifelog assets "
        "or inject ASRBackend; no automatic downloads"
    )


def select_tts(candidates: Sequence[TTSBackend] | None = None) -> TTSBackend:
    pool = candidates if candidates is not None else [SayTTS()]
    for backend in pool:
        if backend.available():
            return backend
    raise BackendUnavailable("No available TTS backend: macOS say or injected TTSBackend required")


def transcribe(
    audio_path: str | Path, *, backend: ASRBackend | None = None, chunk_seconds: float = 30.0
) -> Transcript:
    """Return text/language/duration/absolute segments/backend for a complete audio file.

    Resamples through ffmpeg if needed. Processes bounded chunks sequentially without
    retries or silent fallback after execution failure. Default adapter segments are
    chunk boundaries, not speech/word alignment. Unknown language is reported as und.
    """
    started = time.perf_counter()
    if not math.isfinite(chunk_seconds) or chunk_seconds < 1 / SAMPLE_RATE or chunk_seconds > 30:
        raise ValueError("chunk_seconds must be finite and between 1/16000 and 30")
    source = Path(audio_path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("audio_path must be a regular file")
    chosen = backend if backend is not None else select_asr()
    if not chosen.available():
        raise BackendUnavailable(f"{chosen.name} unavailable")
    rows: list[Segment] = []
    texts: list[str] = []
    languages: set[str] = set()
    with TemporaryDirectory(prefix="jarvis-voice-asr-") as root:
        directory = Path(root)
        normalized = _normalize(source, directory / "normalized.wav")
        with wave.open(str(normalized)) as stream:
            frames = stream.getnframes()
            duration = frames / SAMPLE_RATE
            if frames == 0:
                raise VoiceError("audio file is empty")
            chunk_frames = int(chunk_seconds * SAMPLE_RATE)
            offset = 0
            index = 0
            while offset < frames:
                payload = stream.readframes(chunk_frames)
                actual = len(payload) // 2
                if actual == 0:
                    raise VoiceError("audio payload ended before declared duration")
                index += 1
                clip = directory / "chunk.wav"
                with wave.open(str(clip), "wb") as chunk:
                    chunk.setnchannels(1)
                    chunk.setsampwidth(2)
                    chunk.setframerate(SAMPLE_RATE)
                    chunk.writeframes(payload)
                try:
                    result = chosen.transcribe_chunk(clip)
                except Exception as exc:
                    raise VoiceError(
                        f"{chosen.name} failed at chunk {index} ({type(exc).__name__}); no retry"
                    ) from exc
                length = actual / SAMPLE_RATE
                if (
                    not math.isfinite(result.duration)
                    or abs(result.duration - length) > 1 / SAMPLE_RATE
                ):
                    raise VoiceError(f"{chosen.name} invalid chunk duration at chunk {index}")
                if result.text.strip():
                    texts.append(result.text.strip())
                    if result.language not in {"", "und", "unknown"}:
                        languages.add(result.language)
                local = result.segments or (
                    [Segment(start=0.0, end=length, text=result.text)]
                    if result.text.strip()
                    else []
                )
                for row in local:
                    if (
                        not math.isfinite(row["start"])
                        or not math.isfinite(row["end"])
                        or not 0 <= row["start"] <= row["end"] <= length + 1 / SAMPLE_RATE
                    ):
                        raise VoiceError(f"{chosen.name} invalid segment at chunk {index}")
                    rows.append(
                        Segment(
                            start=row["start"] + offset / SAMPLE_RATE,
                            end=min(row["end"], length) + offset / SAMPLE_RATE,
                            text=row["text"],
                        )
                    )
                offset += actual
    language = next(iter(languages)) if len(languages) == 1 else "mixed" if languages else "und"
    return Transcript(
        text=" ".join(texts),
        language=language,
        duration=duration,
        segments=rows,
        backend=chosen.name,
        latency_seconds=time.perf_counter() - started,
    )


def _synthesize_file(
    text: str,
    out_path: str | Path,
    *,
    backend: TTSBackend | None = None,
    config: VoiceConfig | None = None,
) -> str:
    """Synthesize a new WAV/MP3/M4A/AIFF file atomically; never overwrite or play it.

    Output parent must already exist. Select TTS first to inspect the backend name.
    Audio encoding is selected by suffix; future cloud adapters must read keys only
    through key-file/FD and must never include them in errors, argv or metadata.
    """
    if not text.strip() or "\x00" in text:
        raise ValueError("text must be nonempty and contain no NUL")
    target = Path(out_path).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError("speech output already exists")
    if target.suffix.lower() not in {".wav", ".mp3", ".m4a", ".aiff", ".aif"}:
        raise ValueError("speech output must use .wav/.mp3/.m4a/.aiff/.aif")
    chosen = backend if backend is not None else select_tts()
    if not chosen.available():
        raise BackendUnavailable(f"{chosen.name} unavailable")
    with TemporaryDirectory(prefix=".jarvis-voice-", dir=target.parent) as root:
        staged = Path(root) / ("speech" + target.suffix.lower())
        try:
            chosen.synthesize(text, staged, config or VoiceConfig())
            if not staged.is_file() or staged.is_symlink() or staged.stat().st_size == 0:
                raise VoiceError("backend produced no audio")
        except Exception as exc:
            raise VoiceError(f"{chosen.name} synthesis failed ({type(exc).__name__})") from exc
        # link is an atomic no-clobber publish on the same filesystem.
        os.link(staged, target)
    return str(target)


def _sentence_units(text: str | Iterable[str]) -> Iterator[str]:
    """Consume incremental model text lazily; emit at punctuation or 120 characters."""
    parts = [text] if isinstance(text, str) else text
    buffer = ""
    for part in parts:
        if "\x00" in part:
            raise ValueError("text must contain no NUL")
        buffer += part
        while buffer:
            match = re.search(r"[。！？!?；;\n]", buffer)
            cut = match.end() if match else min(len(buffer), 120) if len(buffer) >= 120 else 0
            if not cut:
                break
            sentence, buffer = buffer[:cut].strip(), buffer[cut:]
            if sentence:
                yield sentence
    if buffer.strip():
        yield buffer.strip()


def _write_metadata(path: Path, data: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def speak_stream(
    text: str | Iterable[str],
    out_dir: str | Path,
    *,
    backend: TTSBackend | None = None,
    config: VoiceConfig | None = None,
) -> Iterator[SpeechChunk]:
    """Yield playable sentence WAVs immediately; accepts streaming text fragments.

    out_dir must be new. The clock starts when this function receives text, including
    generator scheduling/wait for first sentence. No playback. Each yielded WAV remains
    available after iteration/close, with a text-free JSON sidecar for avatar sync.
    Consumer owns cleanup. On later failure completed chunks remain; failed chunks do
    not. Sentence boundaries come from measured PCM lengths, not phonemes/visemes.
    """
    received = time.perf_counter()

    def generate() -> Iterator[SpeechChunk]:
        chosen = backend if backend is not None else select_tts()
        if not chosen.available():
            raise BackendUnavailable(f"{chosen.name} unavailable")
        directory = Path(out_dir).expanduser().absolute()
        directory.mkdir(parents=False, exist_ok=False)
        offset = 0.0
        count = 0
        for index, sentence in enumerate(_sentence_units(text)):
            count += 1
            started = time.perf_counter()
            target = directory / f"sentence-{index:04d}.wav"
            try:
                _synthesize_file(sentence, target, backend=chosen, config=config)
                with wave.open(str(target)) as stream:
                    duration = stream.getnframes() / stream.getframerate()
                if not math.isfinite(duration) or duration <= 0:
                    raise VoiceError("sentence audio has no duration")
                chunk = SpeechChunk(
                    path=str(target),
                    index=index,
                    duration=duration,
                    start=offset,
                    end=offset + duration,
                    backend=chosen.name,
                    ready_latency_seconds=time.perf_counter() - received,
                    synthesis_latency_seconds=time.perf_counter() - started,
                )
                _write_metadata(Path(str(target) + ".json"), asdict(chunk))
            except Exception:
                target.unlink(missing_ok=True)
                Path(str(target) + ".json").unlink(missing_ok=True)
                if index == 0:
                    directory.rmdir()
                raise
            offset += duration
            yield chunk
        if not count:
            directory.rmdir()
            raise ValueError("text must be nonempty")

    return generate()


def speak(
    text: str,
    out_path: str | Path,
    *,
    backend: TTSBackend | None = None,
    config: VoiceConfig | None = None,
    on_segment: Callable[[SpeechChunk], None] | None = None,
) -> str:
    """Default sentence-at-a-time synthesis; returns final path for compatibility.

    First playable segment is passed to on_segment before synthesizing sentence two.
    All sentence WAVs/sidecars remain in '<out_path>.segments', final metadata is in
    '<out_path>.voice.json'. Use speak_stream for pull-based/incremental model text.
    A failed/cancelled stream may already have been played: never automatically retry.
    """
    if not text.strip() or "\x00" in text:
        raise ValueError("text must be nonempty and contain no NUL")
    target = Path(out_path).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError("speech output already exists")
    if target.suffix.lower() not in {".wav", ".mp3", ".m4a", ".aiff", ".aif"}:
        raise ValueError("speech output must use .wav/.mp3/.m4a/.aiff/.aif")
    segments_dir = Path(str(target) + ".segments")
    metadata = Path(str(target) + ".voice.json")
    if segments_dir.exists() or metadata.exists():
        raise FileExistsError("speech segment/metadata target already exists")
    received = time.perf_counter()
    chunks: list[SpeechChunk] = []
    for chunk in speak_stream(text, segments_dir, backend=backend, config=config):
        chunks.append(chunk)
        if on_segment is not None:
            on_segment(chunk)
    with TemporaryDirectory(prefix=".jarvis-voice-join-", dir=target.parent) as root:
        merged = Path(root) / "joined.wav"
        with wave.open(str(merged), "wb") as output:
            parameters: tuple[int, int, int] | None = None
            for chunk in chunks:
                with wave.open(chunk.path) as source:
                    current = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                    if parameters is None:
                        parameters = current
                        output.setnchannels(current[0])
                        output.setsampwidth(current[1])
                        output.setframerate(current[2])
                    elif current != parameters:
                        raise VoiceError("sentence audio formats differ")
                    while payload := source.readframes(16000):
                        output.writeframes(payload)
        staged = Path(root) / ("published" + target.suffix.lower())
        if target.suffix.lower() == ".wav":
            staged = merged
        else:
            _run(
                [_ffmpeg(), "-nostdin", "-v", "error", "-n", "-i", str(merged), str(staged)],
                "sentence audio joining",
            )
        os.link(staged, target)
    _write_metadata(
        metadata,
        {
            "backend": chunks[0].backend,
            "path": str(target),
            "duration": sum(chunk.duration for chunk in chunks),
            "first_sentence_latency_seconds": chunks[0].ready_latency_seconds,
            "total_latency_seconds": time.perf_counter() - received,
            "timing_kind": "sentence_audio_boundary; no phoneme/viseme alignment",
            "segments": [asdict(chunk) for chunk in chunks],
        },
    )
    return str(target)


def _lifelog_worker(audio_path: Path, model: str, root: Path, helpers: Path) -> None:
    """Private subprocess entry; all untyped third-party state stays in this process."""
    sys.path.insert(0, str(helpers))
    lib = importlib.import_module("lifelog_lib")
    models = root / "models"
    # Rebind the imported helper's model paths; never edit or invoke its ingest registry.
    lib.__dict__.update(
        {
            "SV_DIR": str(models / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"),
            "PF_DIR": str(models / "paraformer-int8"),
        }
    )
    samples = lib.read_wav(str(audio_path))
    duration = len(samples) / SAMPLE_RATE
    # The legacy helper feeds 30 seconds at once into VAD. With installed sherpa
    # 1.13.8 this drops the early speech; bounded full-chunk decoding avoids inheriting
    # that truncation. Do not mutate the shared helper. Segments are chunk boundaries.
    recognizer = lib._make_recognizer(model)
    stream = recognizer.create_stream()
    stream.accept_waveform(SAMPLE_RATE, samples)
    recognizer.decode_stream(stream)
    text = str(stream.result.text).strip()
    language = (
        (str(stream.result.lang).strip().strip("<|>") if model == "sensevoice" else "zh")
        if text
        else "und"
    )
    rows = [Segment(start=0.0, end=duration, text=text)] if text else []
    print(
        json.dumps(
            {"text": text, "language": language or "und", "duration": duration, "segments": rows},
            ensure_ascii=False,
        )
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lifelog-worker", type=Path)
    parser.add_argument("--model", choices=["sensevoice", "paraformer"], default="sensevoice")
    parser.add_argument("--root", type=Path, default=DEFAULT_LIFELOG)
    parser.add_argument("--helpers", type=Path, default=DEFAULT_HELPERS)
    args = parser.parse_args()
    if args.lifelog_worker is None:
        parser.error(
            "This is a library; import transcribe/speak. Worker entry requires an audio file."
        )
    try:
        _lifelog_worker(args.lifelog_worker, args.model, args.root, args.helpers)
    except Exception as exc:
        print(f"lifelog worker failed ({type(exc).__name__}); details suppressed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
